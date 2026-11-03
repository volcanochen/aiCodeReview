"""Index & cross-module graph store (SQLite backed, hash-incremental).

Builds a project index from source files: symbol table, call graph and
dependency (include) graph. The graph queries power both the `graph` matcher
type (e.g. signal-handler safety) and the context slicing used by `semantic`
rules — this is what lets the system understand logic *across* files/modules.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .extractors import extract
from .tslang import lang_for_path


@dataclass
class IndexStats:
    files: int = 0
    loc: int = 0
    functions: int = 0
    calls: int = 0
    duration_ms: int = 0


class Index:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._file_cache: dict[str, str] = {}
        self._init_schema()

    def _init_schema(self):
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE,
                language TEXT,
                content_hash TEXT,
                loc INTEGER,
                indexed_at REAL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                name TEXT,
                line INTEGER, col INTEGER,
                end_line INTEGER, end_col INTEGER,
                signature TEXT
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                caller TEXT,
                callee TEXT,
                line INTEGER, col INTEGER,
                end_line INTEGER, end_col INTEGER
            );
            CREATE TABLE IF NOT EXISTS includes (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                included TEXT,
                line INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_call_callee ON calls(callee);
            CREATE INDEX IF NOT EXISTS idx_call_caller ON calls(caller);
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    def build(self, root_paths: list[str], languages: list[str]) -> IndexStats:
        """Walk root_paths, parse matching files, store increments."""
        t0 = time.time()
        src_files = self._collect(root_paths, languages)
        stats = IndexStats()
        c = self.conn.cursor()
        for raw_path in src_files:
            path = os.path.abspath(raw_path)  # always store absolute, avoid dup rows
            lang = lang_for_path(path)
            if lang is None or lang not in languages:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except Exception:
                continue
            h = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:16]
            existing = c.execute("SELECT id, content_hash FROM files WHERE path=?", (path,)).fetchone()
            if existing and existing["content_hash"] == h:
                stats.files += 1
                continue
            if existing:
                self._delete_file(c, existing["id"])
            loc = source.count("\n") + 1
            cur = c.execute(
                "INSERT INTO files(path, language, content_hash, loc, indexed_at) VALUES(?,?,?,?,?)",
                (path, lang, h, loc, time.time()),
            )
            fid = cur.lastrowid
            ex = extract(source, lang)
            for fn in ex.functions:
                c.execute(
                    "INSERT INTO symbols(file_id,name,line,col,end_line,end_col,signature) VALUES(?,?,?,?,?,?,?)",
                    (fid, fn["name"], fn["line"], fn["col"], fn["end_line"], fn["end_col"], fn["signature"]),
                )
            for ca in ex.calls:
                c.execute(
                    "INSERT INTO calls(file_id,caller,callee,line,col,end_line,end_col) VALUES(?,?,?,?,?,?,?)",
                    (fid, ca["caller"], ca["callee"], ca["line"], ca["col"], ca["end_line"], ca["end_col"]),
                )
            for inc in ex.includes:
                c.execute("INSERT INTO includes(file_id,included,line) VALUES(?,?,?)", (fid, inc["path"], inc["line"]))
            stats.files += 1
            stats.loc += loc
            stats.functions += len(ex.functions)
            stats.calls += len(ex.calls)
            self.conn.commit()
            stats.duration_ms = int((time.time() - t0) * 1000)
        # Scope the index to the requested roots: prune files from previously
        # indexed, now-out-of-scope projects so a review only covers its target.
        # Also normalize any legacy (relative) stored paths to absolute so
        # different invocations don't leave duplicate rows.
        roots_abs = [os.path.abspath(r) for r in root_paths]
        for fid, path in c.execute("SELECT id, path FROM files").fetchall():
            pa = os.path.abspath(path)
            if pa != path:
                c.execute("UPDATE files SET path=? WHERE id=?", (pa, fid))
            if not any(pa == ra or pa.startswith(ra + os.sep) for ra in roots_abs):
                self._delete_file(c, fid)
        self.conn.commit()
        # Reflect true totals (incremental runs skip re-extraction but still count).
        row = c.execute("SELECT COUNT(*), COALESCE(SUM(loc),0) FROM files").fetchone()
        stats.files = row[0]
        stats.loc = row[1]
        stats.functions = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        stats.calls = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return stats

    def _collect(self, root_paths, languages) -> list[str]:
        exts = {".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".h++",
                ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
        out = []
        for root in root_paths:
            if os.path.isfile(root):
                out.append(root)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # skip obvious build/vendor dirs to save time
                dirnames[:] = [d for d in dirnames if d not in ("build", ".git", "node_modules", "third_party", "vendor")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in exts:
                        out.append(os.path.join(dirpath, fn))
        return out

    def _delete_file(self, c, fid):
        c.execute("DELETE FROM symbols WHERE file_id=?", (fid,))
        c.execute("DELETE FROM calls WHERE file_id=?", (fid,))
        c.execute("DELETE FROM includes WHERE file_id=?", (fid,))
        c.execute("DELETE FROM files WHERE id=?", (fid,))

    # ------------------------------------------------------------------
    # Query API (used by matchers)
    # ------------------------------------------------------------------
    def get_files(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT id,path,language,loc FROM files").fetchall()]

    def file_id(self, path: str) -> int | None:
        r = self.conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        return r["id"] if r else None

    def symbols_named(self, name: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT f.path, s.* FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=?", (name,)).fetchall()]

    def callees_of(self, func_name: str) -> list[str]:
        return [r["callee"] for r in self.conn.execute(
            "SELECT DISTINCT callee FROM calls WHERE caller=?", (func_name,)).fetchall()]

    def callers_of(self, func_name: str) -> list[str]:
        return [r["caller"] for r in self.conn.execute(
            "SELECT DISTINCT caller FROM calls WHERE callee=?", (func_name,)).fetchall() if r["caller"]]

    def call_graph(self) -> dict[str, set[str]]:
        g: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT DISTINCT caller, callee FROM calls WHERE caller <> ''"):
            g.setdefault(r["caller"], set()).add(r["callee"])
        return g

    def calls_matching(self, callee: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT f.path, c.* FROM calls c JOIN files f ON f.id=c.file_id WHERE c.callee=?", (callee,)).fetchall()]

    def read_source(self, path: str) -> str:
        if path in self._file_cache:
            return self._file_cache[path]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except Exception:
            src = ""
        self._file_cache[path] = src
        return src

    def close(self):
        self.conn.close()
