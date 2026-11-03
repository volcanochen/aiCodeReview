"""Source extraction: functions, calls, includes.

Primary path uses tree-sitter (real AST). A regex fallback keeps the system
functional when tree-sitter grammars are not installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tslang import get_parser, TREE_SITTER_AVAILABLE


@dataclass
class Extraction:
    language: str
    loc: int
    functions: list[dict] = field(default_factory=list)   # {name, line, col, end_line, end_col, signature}
    calls: list[dict] = field(default_factory=list)       # {caller, callee, line, col, end_line, end_col}
    includes: list[dict] = field(default_factory=list)    # {path, line}


# --------------------------------------------------------------------------
# tree-sitter based extraction
# --------------------------------------------------------------------------
def _node_text(node) -> str:
    return node.text.decode("utf-8", "replace") if node and node.text else ""


def _function_name_from_declarator(decl) -> str:
    """Walk a declarator chain to the base function name."""
    cur = decl
    while cur is not None:
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            break
        cur = nxt
    if cur is None:
        return ""
    if cur.type in ("identifier", "field_identifier"):
        return _node_text(cur)
    if cur.type == "qualified_identifier":
        name = cur.child_by_field_name("name")
        return _node_text(name) if name else _node_text(cur)
    return _node_text(cur)


def _callee_name(call_node) -> str:
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return ""
    if fn.type in ("identifier", "field_identifier"):
        return _node_text(fn)
    if fn.type == "qualified_identifier":
        name = fn.child_by_field_name("name")
        return _node_text(name) if name else _node_text(fn)
    if fn.type == "field_expression":
        field_id = fn.child_by_field_name("field")
        return _node_text(field_id) if field_id else _node_text(fn)
    return _node_text(fn)


def _extract_ts(source: str, language: str) -> Extraction:
    parser = get_parser(language)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    ex = Extraction(language=language, loc=source.count("\n") + 1)

    # Walk once collecting function definitions (with caller context for calls).
    func_stack: list[str] = []

    def walk(node, in_func: str | None):
        nonlocal ex
        t = node.type
        if t == "function_definition":
            decl = node.child_by_field_name("declarator")
            name = _function_name_from_declarator(decl) if decl else ""
            sig = _node_text(decl)[:120] if decl else ""
            ex.functions.append({
                "name": name,
                "line": node.start_point[0] + 1,
                "col": node.start_point[1] + 1,
                "end_line": node.end_point[0] + 1,
                "end_col": node.end_point[1] + 1,
                "signature": sig,
            })
            func_stack.append(name)
            for ch in node.children:
                walk(ch, name)
            func_stack.pop()
            return
        if t == "call_expression":
            callee = _callee_name(node)
            if callee:
                ex.calls.append({
                    "caller": in_func or "",
                    "callee": callee,
                    "line": node.start_point[0] + 1,
                    "col": node.start_point[1] + 1,
                    "end_line": node.end_point[0] + 1,
                    "end_col": node.end_point[1] + 1,
                })
        if t == "preproc_include":
            path_node = node.child_by_field_name("path")
            p = _node_text(path_node).strip().strip('"<>') if path_node else ""
            if p:
                ex.includes.append({"path": p, "line": node.start_point[0] + 1})
        for ch in node.children:
            walk(ch, in_func)

    walk(root, None)
    return ex


# --------------------------------------------------------------------------
# regex fallback extraction (low fidelity, but keeps the pipeline alive)
# For C/C++ prefer the tree-sitter path; this fallback is used for all other
# languages (python / javascript / typescript / ...). Previously every call was
# recorded with an empty `caller`, which left the cross-module call graph empty
# for those languages. We now attribute calls to the enclosing function (via
# indentation for Python, brace depth for JS/TS) so the call graph is usable for
# logic-level (cross-module) analysis too.
# --------------------------------------------------------------------------
_RE_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
_RE_CALL = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
# constructs that look like `name(` but are NOT calls — skip as both call and def
_CALL_KW = {
    "if", "for", "while", "switch", "return", "sizeof", "catch", "decltype",
    "elif", "else", "def", "class", "async", "function", "await", "new",
    "with", "assert", "raise", "yield", "include", "var", "let", "const",
    "do", "case", "using", "import", "from", "except", "try", "finally",
    "foreach", "lock", "fixed", "unsafe",
}
# Python: `def` / `async def` at line start (indent captured)
_RE_FUNC_PY = re.compile(r'^(?P<ind>\s*)(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\(')
# JavaScript / TS style function definitions
_RE_FUNC_JS_FN = re.compile(r'\bfunction\s+(?P<name>[A-Za-z_$]\w*)\s*\(')
_RE_FUNC_JS_BODY = re.compile(r'\b(?P<name>[A-Za-z_$]\w*)\s*\([^;{]*\)\s*\{')
_RE_ARROW = re.compile(r'(?:\b(?:const|let|var)\s+)?(?P<name>[A-Za-z_$]\w*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>')


def _record_func(ex: Extraction, name: str, line: int, col: int, end: int) -> None:
    ex.functions.append({"name": name, "line": line, "col": col,
                         "end_line": line, "end_col": end, "signature": ""})


def _record_call(ex: Extraction, caller: str, callee: str, line: int, col: int, end: int) -> None:
    if not callee or callee in _CALL_KW:
        return
    ex.calls.append({"caller": caller, "callee": callee, "line": line,
                     "col": col, "end_line": line, "end_col": end})


def _extract_py(source: str, ex: Extraction) -> None:
    """Indentation-driven caller attribution (Python has no braces)."""
    stack = []  # (indent, name) — enclosing function scopes, deepest last
    for i, line in enumerate(source.splitlines(), 1):
        m = _RE_FUNC_PY.match(line)
        if m:
            ind = len(m.group("ind"))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            stack.append((ind, m.group("name")))
            _record_func(ex, m.group("name"), i, m.start(), m.end())
            current = m.group("name")
            def_start = m.start("name")  # skip the `def` name itself as a call
        else:
            def_start = -1
            # enclosing function = deepest opened scope whose indent <= this line
            cur_ind = len(line) - len(line.lstrip(" "))
            current = ""
            for fi, fn in reversed(stack):
                if fi <= cur_ind:
                    current = fn
                    break
        for cm in _RE_CALL.finditer(line):
            if cm.start() == def_start:
                continue
            _record_call(ex, current, cm.group(1), i, cm.start(), cm.end())


def _extract_js(source: str, ex: Extraction) -> None:
    """Brace-depth-driven caller attribution (C/JS family)."""
    depth = 0
    func_stack = []  # (open_depth, name)
    for i, line in enumerate(source.splitlines(), 1):
        name = None
        def_start = -1
        for rx in (_RE_FUNC_JS_FN, _RE_FUNC_JS_BODY, _RE_ARROW):
            mm = rx.search(line)
            if mm:
                cand = mm.group("name")
                if cand in _CALL_KW:
                    continue
                name = cand
                def_start = mm.start("name")
                break
        opens = line.count("{")
        closes = line.count("}")
        if name is not None:
            _record_func(ex, name, i, line.find(name), line.find(name) + len(name))
        depth += opens - closes
        if name is not None:
            # push AFTER counting this line's braces so open_depth reflects the
            # function body; it will be popped when its closing brace returns depth.
            func_stack.append((depth, name))
        else:
            while func_stack and func_stack[-1][0] > depth:
                func_stack.pop()
        current = func_stack[-1][1] if func_stack else ""
        for cm in _RE_CALL.finditer(line):
            if cm.start() == def_start:
                continue
            _record_call(ex, current, cm.group(1), i, cm.start(), cm.end())


def _extract_regex(source: str, language: str) -> Extraction:
    ex = Extraction(language=language, loc=source.count("\n") + 1)
    for m in _RE_INCLUDE.finditer(source):
        ex.includes.append({"path": m.group(1), "line": source[:m.start()].count("\n") + 1})
    if language == "python":
        _extract_py(source, ex)
    else:
        _extract_js(source, ex)
    return ex


def extract(source: str, language: str) -> Extraction:
    if TREE_SITTER_AVAILABLE and language in ("c", "cpp"):
        try:
            return _extract_ts(source, language)
        except Exception:
            return _extract_regex(source, language)
    return _extract_regex(source, language)
