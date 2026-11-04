"""Local web UI server for aiReview code review.

A dependency-free HTTP server (stdlib ``http.server`` + ``ThreadingHTTPServer``)
that wraps the existing ``aireview`` engine and exposes a JSON API plus a
single-page static frontend (``static/index.html``). Runs entirely on localhost.

Run:
    python -m aireview.web [--host 127.0.0.1] [--port 8765] [--root DIR] [--config PATH]
or via the CLI:
    python -m aireview.cli web
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yaml

from .. import __version__
from ..config import load_config, example_config_text
from ..llm import LLMClient
from ..parser.index import Index
from ..rules import (
    load_pack_by_id, load_user_rules_dir as _lur, validate_rule,
    RuleValidationError, list_packs,
)
from ..rules.nl_compiler import compile_nl
from ..rules.engine import RuleEngine, ModelUnavailable
from ..reporter import build_report, render_json, render_sarif, render_text, gate_exit
from ..reporter.report import GATE_DEFAULT

STANDARDS_ROOT = os.path.join(os.path.dirname(__file__), "..", "rules", "standards")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

CONFIG_FIELDS = {
    "endpoint", "model", "api_key", "languages", "enabled_standards",
    "scope", "perf_budget", "user_rules_dir", "index_db",
    "semantic_timeout_ms", "only_structured", "logic_only",
}


def _resolve(base: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(base, p)


class WebServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 root: str | None = None, config_path: str | None = None):
        self.host = host
        self.port = port
        self.root = os.path.abspath(root or os.getcwd())
        self.config_path = (os.path.abspath(config_path)
                            if config_path else os.path.join(self.root, ".aireview.yaml"))
        self.jobs: dict[str, dict] = {}
        self.jobs_lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ API
    def _load_cfg(self):
        return load_config(self.config_path)

    def _user_rule_path(self, rid: str) -> str | None:
        cfg = self._load_cfg()
        urd = _resolve(self.root, cfg.user_rules_dir)
        for ext in (".yaml", ".yml"):
            cand = os.path.join(urd, f"{rid}{ext}")
            if os.path.isfile(cand):
                return cand
        return None

    # ------------------------------------------------------------------ HTTP
    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # -- plumbing --------------------------------------------------
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, body: bytes, content_type: str):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def _serve_static(self, rel: str):
                if not rel or rel == "/":
                    rel = "index.html"
                # prevent path traversal
                full = os.path.normpath(os.path.join(STATIC_DIR, rel))
                if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                ext = os.path.splitext(full)[1]
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                }.get(ext, "application/octet-stream")
                with open(full, "rb") as fh:
                    data = fh.read()
                # Inject the single-source version into the HTML shell so the
                # browser tab title shows it without duplicating the number.
                if ext in (".html", ".htm"):
                    data = data.replace(b"{{VERSION}}", __version__.encode("utf-8"))
                self._send(200, data, ctype)

            # -- routing ---------------------------------------------------
            def do_GET(self):
                u = urlparse(self.path)
                p = u.path
                # keep_blank_values=True：`standards=` 必须被识别为「空列表」，
                # 而不是「参数缺失」——否则勾掉所有标准包后会静默回落到配置默认值。
                q = parse_qs(u.query, keep_blank_values=True)
                if p == "/" or p.startswith("/static/"):
                    self._serve_static(p[len("/static/"):] if p.startswith("/static/") else p[1:])
                    return
                if p.startswith("/api/"):
                    self._api_get(p, q)
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def do_POST(self):
                u = urlparse(self.path)
                if u.path.startswith("/api/"):
                    self._api_post(u.path, self._body())
                    return
                self._send(405, b"method not allowed", "text/plain; charset=utf-8")

            def do_PUT(self):
                u = urlparse(self.path)
                if u.path.startswith("/api/"):
                    self._api_post(u.path, self._body())
                    return
                self._send(405, b"method not allowed", "text/plain; charset=utf-8")

            def do_PATCH(self):
                u = urlparse(self.path)
                if u.path.startswith("/api/"):
                    self._api_patch(u.path, self._body())
                    return
                self._send(405, b"method not allowed", "text/plain; charset=utf-8")

            def do_DELETE(self):
                u = urlparse(self.path)
                if u.path.startswith("/api/"):
                    self._api_delete(u.path)
                    return
                self._send(405, b"method not allowed", "text/plain; charset=utf-8")

            # -- GET endpoints --------------------------------------------
            def _api_get(self, p, q):
                if p == "/api/config":
                    return self._json(200, server.api_config_get())
                if p == "/api/standards":
                    return self._json(200, list_packs(STANDARDS_ROOT))
                if p == "/api/rules":
                    # 可选 query：standards=a,b（空串=一个包都不要）/
                    # include_user_rules=0|1 / only_structured=0|1 / logic_only=0|1
                    # 供运行页「逐条选择规则」面板取到与本次审查一致的规则池。
                    def _flag(name, default=False):
                        raw = q.get(name)
                        if raw is None:
                            return default
                        return str(raw[0]).lower() not in ("0", "false", "no", "off", "")

                    raw_std = q.get("standards")
                    standards = None
                    if raw_std is not None:
                        standards = [s for s in raw_std[0].split(",") if s]
                    return self._json(200, server.api_rules_get(
                        standards=standards,
                        include_user_rules=_flag("include_user_rules", True),
                        only_structured=_flag("only_structured"),
                        logic_only=_flag("logic_only"),
                    ))
                if p == "/api/models":
                    return self._json(200, server.api_models_get())
                if p.startswith("/api/review/") and p.endswith("/download"):
                    job_id = p[len("/api/review/"):-len("/download")]
                    fmt = (q.get("fmt") or ["json"])[0]
                    return server.api_review_download(job_id, fmt, self)
                if p.startswith("/api/review/"):
                    job_id = p[len("/api/review/"):]
                    return self._json(200, server.api_review_status(job_id))
                if p == "/api/fs/list":
                    path = (q.get("path") or [None])[0]
                    return self._json(200, server.api_fs_list(path))
                if p == "/api/fs/stat":
                    path = (q.get("path") or [None])[0]
                    return self._json(200, server.api_fs_stat(path))
                self._json(404, {"error": "unknown endpoint"})

            # -- POST endpoints -------------------------------------------
            def _api_post(self, p, body):
                if p == "/api/config":
                    return self._json(200, server.api_config_put(body))
                if p == "/api/config/test":
                    return self._json(200, server.api_config_test(body))
                if p == "/api/config/init":
                    return self._json(200, server.api_config_init())
                if p == "/api/rules/compile":
                    return self._json(200, server.api_rules_compile(body))
                if p == "/api/rules":
                    return self._json(200, server.api_rules_post(body))
                if p == "/api/standards/install":
                    return self._json(200, server.api_standards_install(body))
                if p == "/api/review":
                    return self._json(200, server.api_review_start(body))
                self._json(404, {"error": "unknown endpoint"})

            # -- PATCH endpoints ------------------------------------------
            def _api_patch(self, p, body):
                if p.startswith("/api/rules/") and p.endswith("/enabled"):
                    rid = p[len("/api/rules/"):-len("/enabled")]
                    return self._json(200, server.api_rule_toggle(rid, body))
                self._json(404, {"error": "unknown endpoint"})

            # -- DELETE endpoints -----------------------------------------
            def _api_delete(self, p):
                if p.startswith("/api/rules/"):
                    rid = p[len("/api/rules/"):]
                    return self._json(200, server.api_rule_delete(rid))
                if p.startswith("/api/standards/") and len(p) > len("/api/standards/"):
                    pid = p[len("/api/standards/"):]
                    return self._json(200, server.api_standards_delete(pid))
                self._json(404, {"error": "unknown endpoint"})

        return Handler

    def run(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        url = f"http://{self.host}:{self.port}/"
        print(f"[aireview-web] 服务已启动: {url}")
        print(f"[aireview-web] 工作根目录: {self.root}")
        print(f"[aireview-web] 配置文件:   {self.config_path}")
        print("[aireview-web] 按 Ctrl+C 停止。")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[aireview-web] 已停止。")

    # ================================================================== API impl
    def api_config_get(self) -> dict:
        cfg = self._load_cfg()
        d = cfg.to_dict()
        d["api_key"] = ""  # masked
        d["api_key_set"] = bool(cfg.api_key)
        d["root"] = self.root
        d["config_path"] = self.config_path
        d["version"] = __version__
        return d

    def api_config_put(self, body: dict) -> dict:
        cfg = self._load_cfg()
        existing = cfg.to_dict()
        merged = {}
        for k in CONFIG_FIELDS:
            if k in body and body[k] is not None:
                merged[k] = body[k]
            else:
                merged[k] = existing.get(k)
        # api_key: keep previous if not supplied / left blank
        if not (body.get("api_key") or "").strip():
            merged["api_key"] = existing.get("api_key", "lmstudio")
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
        return {"ok": True, "config": self.api_config_get()}

    def api_config_test(self, body: dict) -> dict:
        endpoint = body.get("endpoint") or self._load_cfg().endpoint
        model = body.get("model") or self._load_cfg().model
        api_key = body.get("api_key") or self._load_cfg().api_key or "lmstudio"
        client = LLMClient(endpoint, model, api_key, timeout_ms=8000)
        t0 = time.time()
        try:
            ok = client.healthcheck()
            models = client.list_models() if ok else []
            latency = int((time.time() - t0) * 1000)
            return {"ok": ok, "models": models, "latency_ms": latency,
                    "endpoint": endpoint, "error": "" if ok else "端点不可达"}
        except Exception as e:
            return {"ok": False, "models": [], "latency_ms": int((time.time() - t0) * 1000),
                    "endpoint": endpoint, "error": str(e)}

    def api_config_init(self) -> dict:
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(example_config_text())
        return {"ok": True, "path": self.config_path}

    def api_standards(self):
        return list_packs(STANDARDS_ROOT)

    # ---- standards install / uninstall --------------------------------
    def api_standards_install(self, body: dict) -> dict:
        """Install a new standard pack from a structured payload or a YAML bundle.

        Accepts either:
          {"id": "...", "manifest": {...}, "rules": [ ... ]}
        or a single YAML string under "yaml" that parses to a mapping with
        id / manifest / rules. Rules are validated before anything is written.
        """
        raw = body.get("yaml")
        if isinstance(raw, str) and raw.strip():
            try:
                doc = yaml.safe_load(raw)
            except Exception as e:
                return {"ok": False, "error": f"YAML 解析失败：{e}"}
            if not isinstance(doc, dict):
                return {"ok": False, "error": "YAML 顶层应为包含 id/manifest/rules 的映射"}
            pid = (body.get("id") or doc.get("id") or "").strip()
            manifest = doc.get("manifest") or {}
            rules = doc.get("rules") or []
        else:
            pid = (body.get("id") or "").strip()
            manifest = body.get("manifest") or {}
            rules = body.get("rules") or []

        if not pid or not re.fullmatch(r"[A-Za-z0-9_.-]+", pid):
            return {"ok": False, "error": "非法的标准包 id（仅允许字母/数字/_/./-）"}
        if pid in ("cert-c", "cert-cpp", "python", "javascript"):
            return {"ok": False, "error": f"id '{pid}' 为内置标准包，不可覆盖安装"}
        if not isinstance(rules, list) or not rules:
            return {"ok": False, "error": "请提供非空 rules 列表"}
        validated = []
        for r in rules:
            try:
                validated.append(validate_rule(r))
            except RuleValidationError as e:
                return {"ok": False, "error": f"规则校验失败（{r.get('id', '?')}）：{e}"}

        pack_dir = os.path.join(STANDARDS_ROOT, pid)
        if os.path.isdir(pack_dir):
            return {"ok": False, "error": f"标准包 {pid} 已存在（如需更新请先卸载）"}
        os.makedirs(os.path.join(pack_dir, "rules"), exist_ok=True)
        manifest = dict(manifest)
        manifest["id"] = pid
        manifest.setdefault("name", pid)
        manifest["builtin"] = False
        with open(os.path.join(pack_dir, "manifest.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump(manifest, fh, allow_unicode=True, sort_keys=False)
        with open(os.path.join(pack_dir, "rules", f"{pid}.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump([v.to_dict() for v in validated], fh, allow_unicode=True, sort_keys=False)
        return {"ok": True, "id": pid, "rule_count": len(validated)}

    def api_standards_delete(self, pack_id: str) -> dict:
        pack_dir = os.path.join(STANDARDS_ROOT, pack_id)
        if not os.path.isdir(pack_dir):
            return {"ok": False, "error": "标准包不存在"}
        mp = os.path.join(pack_dir, "manifest.yaml")
        if os.path.isfile(mp):
            try:
                builtin = bool((yaml.safe_load(open(mp, encoding="utf-8")) or {}).get("builtin"))
            except Exception:
                builtin = False
        else:
            builtin = False
        if builtin:
            return {"ok": False, "error": f"内置标准包 {pack_id} 不可卸载"}
        # also drop it from enabled_standards in the active config
        try:
            cfg = self._load_cfg()
            if pack_id in (cfg.enabled_standards or []):
                cfg.enabled_standards = [x for x in cfg.enabled_standards if x != pack_id]
                d = cfg.to_dict()
                d["api_key"] = cfg.api_key or "lmstudio"
                os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
                with open(self.config_path, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(d, fh, allow_unicode=True, sort_keys=False)
        except Exception:
            pass
        shutil.rmtree(pack_dir)
        return {"ok": True, "id": pack_id}

    def api_rules_get(self, standards=None, include_user_rules: bool = True,
                      only_structured: bool = False, logic_only: bool = False) -> list:
        """返回规则列表。

        `standards=None` 表示使用配置里的 `enabled_standards`；传列表（含空列表）
        表示严格按该列表取包——这与 `_run_review` 的语义保持一致。

        `only_structured` / `logic_only` 会做与 `_run_review` **同一套**过滤，这样
        「逐条选择规则」面板展示的，就是本次审查真正会执行的规则池，不会出现
        「勾了却没跑」的错位。
        """
        cfg = self._load_cfg()
        pack_ids = cfg.enabled_standards if standards is None else list(standards)
        rules = []
        for rid in pack_ids:
            try:
                rs, _ = load_pack_by_id(rid, STANDARDS_ROOT)
                rules.extend(rs)
            except FileNotFoundError:
                pass
        if include_user_rules:
            try:
                rules.extend(_lur(_resolve(self.root, cfg.user_rules_dir)))
            except Exception:
                pass
        if only_structured:
            rules = [r for r in rules if getattr(r, "type", "") != "semantic"]
        if logic_only:
            rules = [r for r in rules if getattr(r, "type", "") in ("graph", "semantic")]
        out = []
        for r in rules:
            d = r.to_dict()
            d["source"] = r.source
            out.append(d)
        return out

    def api_models_get(self) -> dict:
        cfg = self._load_cfg()
        client = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=8000)
        try:
            ok = client.healthcheck()
            models = client.list_models() if ok else []
            return {"ok": ok, "models": models, "endpoint": cfg.endpoint, "error": "" if ok else "端点不可达"}
        except Exception as e:
            return {"ok": False, "models": [], "endpoint": cfg.endpoint, "error": str(e)}

    def api_rule_toggle(self, rid: str, body: dict) -> dict:
        path = self._user_rule_path(rid)
        if not path:
            return {"ok": False, "error": "仅用户规则可切换启用状态（内置规则请通过标准包开关）"}
        enabled = bool(body.get("enabled", True))
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data["enabled"] = enabled
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
        return {"ok": True}

    def api_rule_delete(self, rid: str) -> dict:
        path = self._user_rule_path(rid)
        if not path:
            return {"ok": False, "error": "仅用户规则可删除"}
        os.remove(path)
        return {"ok": True}

    def api_rules_compile(self, body: dict) -> dict:
        text = (body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "请提供自然语言规则描述"}
        cfg = self._load_cfg()
        llm = None
        if not cfg.only_structured:
            probe = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=4000)
            try:
                if probe.healthcheck():
                    # real chat client needs a generous timeout (JSON-schema gen is slow)
                    llm = LLMClient(cfg.endpoint, cfg.model, cfg.api_key,
                                    timeout_ms=max(120000, int(cfg.semantic_timeout_ms)))
            except Exception:
                llm = None
        res = compile_nl(text, llm, category="nl-custom")
        preview = None
        if res.rule:
            preview = yaml.safe_dump(res.rule, allow_unicode=True, sort_keys=False)
        template_rule = None
        if res.template:
            try:
                template_rule = yaml.safe_load(res.template)
            except Exception:
                template_rule = None
        return {
            "ok": res.ok, "rule": res.rule, "yaml_preview": preview,
            "needs_review": res.needs_review, "confidence": res.confidence,
            "template": res.template, "template_rule": template_rule, "error": res.error,
        }

    def api_rules_post(self, body: dict) -> dict:
        # Accept either a bare rule dict or a wrapper {"rule": {...}}.
        if "rule" in body and isinstance(body.get("rule"), dict) and "id" not in body:
            body = body["rule"]
        try:
            rule = validate_rule(body)
        except RuleValidationError as e:
            return {"ok": False, "error": f"规则校验失败：{e}"}
        cfg = self._load_cfg()
        urd = _resolve(self.root, cfg.user_rules_dir)
        os.makedirs(urd, exist_ok=True)
        path = os.path.join(urd, f"{rule.id}.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(rule.to_dict(), fh, allow_unicode=True, sort_keys=False)
        return {"ok": True, "path": path}

    # ---- review job -----------------------------------------------------
    def api_review_start(self, body: dict) -> dict:
        path = (body.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "请提供审查路径"}
        path = _resolve(self.root, path) if not os.path.isabs(path) else path
        if not os.path.isdir(path):
            return {"ok": False, "error": f"路径不存在或不是目录：{path}"}
        job_id = uuid.uuid4().hex[:12]
        with self.jobs_lock:
            self.jobs[job_id] = {
                "status": "queued", "progress": {"phase": "queued", "current": 0,
                                                  "total": 1, "message": "任务已排队…"},
                "result": None, "error": None,
            }
        t = threading.Thread(target=self._run_review, args=(job_id, path, body), daemon=True)
        t.start()
        return {"ok": True, "job_id": job_id, "status": "queued"}

    def _run_review(self, job_id: str, path: str, body: dict):
        job = self.jobs[job_id]
        job["status"] = "running"

        def set_progress(phase, current, total, message, indeterminate=False):
            job["progress"] = {"phase": phase, "current": current, "total": total,
                               "message": message, "indeterminate": indeterminate}

        try:
            cfg = self._load_cfg()
            # "standards" === null means "use the configured defaults"; an explicit
            # list (even an empty one) means "run exactly these packs".
            standards = body.get("standards")
            if standards is not None:
                cfg.enabled_standards = list(standards)
            # Custom rules (from user_rules_dir) are a selectable set of their own.
            # Default to True so older callers keep their previous behaviour.
            include_user_rules = body.get("include_user_rules", True)
            exclude_rules = set(body.get("exclude_rules") or [])
            if body.get("only_structured"):
                cfg.only_structured = True
            if body.get("logic_only"):
                cfg.logic_only = True
            urd = _resolve(self.root, cfg.user_rules_dir)
            idb = _resolve(self.root, cfg.index_db)

            set_progress("index", 0, 1, "正在构建索引…")
            index = Index(idb)
            stats = index.build([path], cfg.languages)

            rules = []
            for sid in cfg.enabled_standards:
                try:
                    rs, _ = load_pack_by_id(sid, STANDARDS_ROOT)
                    rules.extend(rs)
                except FileNotFoundError:
                    pass
            try:
                if include_user_rules:
                    rules.extend(_lur(urd))
            except Exception:
                pass
            if exclude_rules:
                rules = [r for r in rules if r.id not in exclude_rules]
            # In "only structured" mode, drop semantic (LLM) rules up front so they
            # are NOT reported as "skipped / model unreachable" — that banner must
            # only surface for a genuine model outage, not an intentional choice.
            if getattr(cfg, "only_structured", False):
                rules = [r for r in rules if getattr(r, "type", "") != "semantic"]
            # In "logic only" mode, keep ONLY logic-class rules (graph/semantic) and
            # drop syntax-class rules (regex/ast_pattern) — business-logic review.
            if getattr(cfg, "logic_only", False):
                rules = [r for r in rules if getattr(r, "type", "") in ("graph", "semantic")]
            set_progress("rules", 0, len(rules), f"已加载 {len(rules)} 条规则")

            llm = None
            if not cfg.only_structured:
                client = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=60000)
                probe = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=4000)
                try:
                    llm = client if probe.healthcheck() else None
                except Exception:
                    llm = None

            engine = RuleEngine(index, llm)
            findings, rule_status, skipped = [], [], []
            total, done = len(rules), 0
            for rule in rules:
                if not getattr(rule, "enabled", True):
                    done += 1
                    continue
                if rule.type == "semantic":
                    set_progress("semantic", done, total,
                                 "调用本地 LLM 进行语义分析（预计约 20s）…", indeterminate=True)
                else:
                    set_progress("structured", done, total,
                                 f"结构化规则检查中 ({done}/{total})")
                try:
                    fs, applied = engine._run_one(rule)
                except Exception as e:
                    kind = "model_offline" if isinstance(e, ModelUnavailable) else "matcher_error"
                    skipped.append({"rule_id": rule.id, "reason": f"matcher error: {e}", "kind": kind})
                    done += 1
                    continue
                findings.extend(fs)
                rule_status.append({
                    "id": rule.id, "name": rule.name, "severity": rule.severity,
                    "type": rule.type, "status": "failed" if fs else "passed",
                    "applied_count": applied, "findings": len(fs),
                })
                done += 1

            index.close()

            from datetime import datetime, timezone
            metadata = {
                "project": os.path.basename(os.path.abspath(path)),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": (cfg.model if llm
                          else ("n/a (仅结构化模式)" if getattr(cfg, "only_structured", False)
                                else "n/a (local model offline)")),
                "scope_files": stats.files, "scope_loc": stats.loc,
                "duration_ms": 0, "standards": cfg.enabled_standards,
            }
            report = build_report(metadata,
                                 {"findings": findings, "rule_status": rule_status,
                                  "skipped": skipped}, rules)
            gate_passed = gate_exit(report) == 0
            fail_sev = [s for s in GATE_DEFAULT
                        if report["summary"]["findings_by_severity"].get(s, 0) > 0]
            downloads = {
                "json": render_json(report),
                "sarif": render_sarif(report),
                "text": render_text(report),
            }
            job["result"] = {
                "report": report,
                "gate": {"passed": gate_passed, "fail_severities": fail_sev},
                "downloads": downloads,
            }
            job["status"] = "done"
        except Exception as e:
            job["error"] = f"{e}\n{traceback.format_exc()}"
            job["status"] = "error"

    def api_review_status(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            return {"status": "not_found", "error": "任务不存在或已过期"}
        out = {"status": job["status"], "progress": job["progress"]}
        if job["status"] == "done" and job["result"]:
            out["result"] = job["result"]
        if job["status"] == "error":
            out["error"] = job["error"]
        return out

    def api_review_download(self, job_id: str, fmt: str, handler):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "done" or not job["result"]:
            handler._json(404, {"error": "报告尚未生成"})
            return
        content = job["result"]["downloads"].get(fmt)
        if content is None:
            handler._json(400, {"error": f"不支持的格式: {fmt}"})
            return
        fname = f"aireview-report.{fmt}"
        body = content.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        handler.end_headers()
        handler.wfile.write(body)

    # ---- fs --------------------------------------------------------------
    def api_fs_list(self, path: str | None) -> dict:
        base = path or self.root
        if not os.path.isabs(base):
            base = _resolve(self.root, base)
        if not os.path.isdir(base):
            return {"path": base, "parent": None, "children": []}
        children = []
        try:
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                try:
                    is_dir = os.path.isdir(full)
                except Exception:
                    is_dir = False
                children.append({"name": name, "is_dir": is_dir,
                                "path": full})
        except Exception:
            children = []
        children.sort(key=lambda c: (not c["is_dir"], c["name"].lower()))
        parent = os.path.dirname(base) if os.path.dirname(base) != base else None
        return {"path": base, "parent": parent, "children": children}

    def api_fs_stat(self, path: str | None) -> dict:
        if not path:
            return {"exists": False, "is_dir": False, "readable": False}
        if not os.path.isabs(path):
            path = _resolve(self.root, path)
        return {
            "exists": os.path.exists(path),
            "is_dir": os.path.isdir(path),
            "readable": os.access(path, os.R_OK),
        }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="aireview-web", description="aiReview 本地代码审查 Web 界面")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=None, help="工作根目录（配置与待审查项目所在处）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 <root>/.aireview.yaml）")
    args = ap.parse_args(argv)
    WebServer(args.host, args.port, args.root, args.config).run()


if __name__ == "__main__":
    main()
