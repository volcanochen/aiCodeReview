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
    load_pack_by_id, load_standard_pack, load_user_rules_dir as _lur, validate_rule,
    RuleValidationError, list_packs, rule_subsets, rule_matches_subset,
    parse_subset_specs,
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
    "semantic_timeout_ms", "only_structured", "logic_only", "enabled_subsets",
}


def _resolve(base: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(base, p)


def _merge_subsets(cfg, overrides: dict | None) -> dict:
    """本次运行的子集选择 = 配置值 + 本次覆盖（按包覆盖，未提到的包沿用配置）。

    `overrides` 里出现某个包 → 以它为准（可以是空列表 = 该包一条规则都不跑）；
    没出现的包保持配置里的 `enabled_subsets`，配置里也没有则交给
    `filter_by_subsets` 走 manifest 的 `default_subsets`。
    """
    merged = {k: list(v or []) for k, v in (getattr(cfg, "enabled_subsets", None) or {}).items()}
    for pack, ids in (overrides or {}).items():
        merged[str(pack)] = list(ids or [])
    return merged


# ---------------------------------------------------------------------------
# 规则「实现逻辑」说明
#
# 这些文案必须与 aireview/rules/engine.py 中的实际匹配行为一致，不能凭字段名
# 想象。每一段的依据写在注释里，改引擎时请同步改这里。
# ---------------------------------------------------------------------------
_ENGINE_NAMES = {
    "regex": "正则扫描（Python re）",
    "ast_pattern": "AST 模式匹配（tree-sitter query）",
    "graph": "图分析（符号表 / 调用图）",
    "semantic": "本地模型语义判定（LLM）",
}

_GRAPH_ANALYZERS = {
    "signal_safety": {
        "title": "信号安全链式检查（signal_safety）",
        "steps": [
            "先定位信号处理函数：扫描注册点（signal / sigaction 等），得到处理函数清单。",
            "以处理函数为起点，沿调用图做 BFS 展开（深度上限 max_depth，默认 8），支持跨模块。",
            "展开过程中任何被调用的函数命中“非异步信号安全函数”集合即记为违规；"
            "集合默认取引擎内置的 NON_SIGSAFE，规则可用 params.unsafe_functions 覆盖。",
            "命中的函数名会去重后写进消息，并定位到处理函数定义行。",
        ],
        "requires": ["符号表 + 调用图索引（需要先建索引）"],
    },
    "logic_consistency": {
        "title": "业务逻辑一致性交叉回归（logic_consistency）",
        "steps": [
            "第一趟：按模块把源码交给本地模型，推断该模块的业务职责、关键不变量与契约"
            "（前置/后置条件、错误处理预期、状态机规则）。",
            "第二趟：沿调用图取相互调用的模块对，把双方的源码与不变量一起交给模型，"
            "判断调用方是否违背被调方契约（或反之），输出矛盾条目。",
            "模块内部再做一次自检（不变量与实现是否自相矛盾）。",
            "模型返回 JSON，只保留 contradictions 非空的条目，confidence 原样带入。",
        ],
        "requires": ["调用图索引", "本地模型在线（离线则该规则以 model_offline 跳过）"],
    },
}


def _describe_impl(rule) -> dict:
    """把一条规则翻译成「它到底怎么跑」的结构化说明。"""
    m = getattr(rule, "match", {}) or {}
    rtype = getattr(rule, "type", "")
    lang = getattr(rule, "language", "multi") or "multi"
    base = {"engine": _ENGINE_NAMES.get(rtype, rtype), "type": rtype, "language": lang}

    if rtype == "regex":
        scope = m.get("file_scope") or {}
        scope_lang = scope.get("language") or "(不限)"
        glob = scope.get("path_glob")
        flags = "re.MULTILINE" + (" + re.IGNORECASE" if m.get("ignore_case") else "")
        target = "文件语言 = %s" % scope_lang + ("，路径匹配 %s" % glob if glob else "")
        steps = [
            "按 file_scope 选中文件：语言过滤为 %s%s；all / multi / any / * / 留空 均表示不做语言过滤。"
            % (scope_lang, "，且路径需 fnmatch 命中 %s" % glob if glob else ""),
            "把整个文件内容作为一段文本，用 %s 编译正则后 finditer 全量扫描"
            "——注意不是逐行匹配，跨行结构也能命中。" % flags,
            "每个命中位置换算成起始/结束行号，截取该处代码片段作为 evidence。",
            "消息模板渲染占位符：{{match}}（命中文本）、{{line_number}} / {{line}}（行号）。",
        ]
        base.update({
            "summary": "在 %s 的文件上，用一条正则扫全文，命中即报。" % (scope_lang if scope_lang != "(不限)" else "所选语言"),
            "target": target,
            "steps": steps,
            "params": {
                "pattern": m.get("pattern", ""),
                "message": m.get("message", ""),
                "ignore_case": bool(m.get("ignore_case")),
                "file_scope": scope,
            },
            "requires": [],
        })
        return base

    if rtype == "ast_pattern":
        base.update({
            "summary": "用 tree-sitter 把 %s 源码解析成语法树，再用一条 query 匹配结构。" % lang,
            "target": "文件语言 = %s" % (lang if lang.lower() not in ("all", "multi", "any", "*", "") else "任意"),
            "steps": [
                "按规则 language 挑出文件，用对应语言的 tree-sitter 解析器建语法树。",
                "在该树上执行 match.query，取 @node 捕获作为命中节点，@name 捕获作为名字。",
                "命中节点的起止行/列直接来自语法树位置，比正则更准（不受注释、换行影响）。",
                "消息模板可用 {{name}} / {{fn}} 渲染捕获到的名字。",
            ],
            "params": {"query": m.get("query", ""), "message": m.get("message", "")},
            "requires": ["对应语言的 tree-sitter 解析器（当前安装包仅内置 c / cpp）"],
        })
        return base

    if rtype == "graph":
        analyzer = m.get("analyzer", "")
        info = _GRAPH_ANALYZERS.get(analyzer)
        params = {}
        for k in ("params", "context"):
            if m.get(k):
                params[k] = m.get(k)
        if info:
            base.update({
                "summary": info["title"],
                "target": "索引中的符号表与调用图（跨文件）",
                "analyzer": analyzer,
                "steps": info["steps"],
                "params": params,
                "requires": info["requires"],
            })
        else:
            base.update({
                "summary": "图分析：%s" % (analyzer or "(未指定 analyzer)"),
                "target": "索引中的符号表与调用图（跨文件）",
                "analyzer": analyzer,
                "steps": [
                    "引擎按 analyzer 名称分派到具体分析器；未知名称会报 unknown graph analyzer 并跳过该规则。",
                    "已实现的分析器：signal_safety、logic_consistency。",
                ],
                "params": params,
                "requires": ["对应分析器"],
            })
        return base

    if rtype == "semantic":
        ctx = m.get("context") or {}
        cand = ctx.get("candidate_regex")
        limit = ctx.get("limit", 50)
        base.update({
            "summary": "把候选函数源码交给本地模型，由模型判断是否违规。",
            "target": "C / C++ 文件中的函数（semantic 目前只对 c/cpp 生效）",
            "steps": [
                "只扫描 c / cpp 文件，把文件按函数切分。",
                "若配置了 candidate_regex，只保留函数体命中该正则的候选，"
                "并按 limit（默认 %s）截断，避免一次调用模型太多次。" % limit,
                "每个候选拼成提示词：规则 prompt + 函数源码 + 该函数的直接被调用者（callees），"
                "要求模型只回 JSON。",
                "模型返回 violation=true 时，用返回的 line / message / confidence 生成发现；"
                "模型不可用则该规则整体以 model_offline 跳过（--only-structured 可显式跳过）。",
            ],
            "params": {
                "candidate_regex": cand or "",
                "limit": limit,
                "prompt": (m.get("prompt") or "")[:400],
            },
            "requires": ["本地模型在线", "文件语言为 c / cpp"],
        })
        return base

    base.update({"summary": "未知规则类型 %s" % rtype, "target": "—",
                 "steps": [], "params": {}, "requires": []})
    return base


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
                    return self._json(200, server.api_standards_get())
                if p == "/api/rules":
                    # 可选 query：standards=a,b（空串=一个包都不要）/
                    # include_user_rules=0|1 / only_structured=0|1 / logic_only=0|1 /
                    # subset=cert-c:core,STR（可重复，写法与 CLI --subset 相同）
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
                        subsets=parse_subset_specs(q.get("subset")) or None,
                    ))
                if p.startswith("/api/standards/") and p.endswith("/rules"):
                    # /api/standards/<id>/rules —— 标准包规则详情（含实现逻辑）
                    pid = p[len("/api/standards/"):-len("/rules")]
                    return self._json(200, server.api_standard_rules(pid))
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
                      only_structured: bool = False, logic_only: bool = False,
                      subsets: dict | None = None) -> list:
        """返回规则列表。

        `standards=None` 表示使用配置里的 `enabled_standards`；传列表（含空列表）
        表示严格按该列表取包——这与 `_run_review` 的语义保持一致。

        `subsets` 是本次运行的子集覆盖（`{pack_id: [subset_id, ...]}`），同样按
        `_merge_subsets` 与配置合并，保证面板展示的规则池与本次审查真正会跑的
        规则完全一致。

        `only_structured` / `logic_only` 会做与 `_run_review` **同一套**过滤，这样
        「逐条选择规则」面板展示的，就是本次审查真正会执行的规则池，不会出现
        「勾了却没跑」的错位。
        """
        cfg = self._load_cfg()
        pack_ids = cfg.enabled_standards if standards is None else list(standards)
        subs_map = _merge_subsets(cfg, subsets)
        rules = []
        for rid in pack_ids:
            try:
                rs, _ = load_pack_by_id(rid, STANDARDS_ROOT, subsets=subs_map.get(rid))
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

    def api_standards_get(self) -> list:
        """标准包列表，附带规则总数与每个子集的规则（供 UI 显示"已选 N / M 条"）。

        `subset_counts` 给「（N 条）」这样的定值文案用；`subset_rules` 给前端做
        并集计数用——子集之间会重叠（一条规则可以同时属于 `core` 和 `str`），
        把 count 相加会重复计算，只有拿到 id 列表才能算准并集。
        """
        out = []
        for m in list_packs(STANDARDS_ROOT):
            pid = m.get("id")
            total = 0
            counts = {}
            subset_rules = {}
            try:
                rules, _ = load_standard_pack(os.path.join(STANDARDS_ROOT, pid))
                total = len(rules)
                for sub in (m.get("subsets") or []):
                    if sub.get("id"):
                        hit = [r.id for r in rules if rule_matches_subset(r, sub)]
                        counts[sub["id"]] = len(hit)
                        subset_rules[sub["id"]] = hit
            except Exception:
                pass
            item = dict(m)
            item["rule_total"] = total
            item["subset_counts"] = counts
            item["subset_rules"] = subset_rules
            out.append(item)
        return out

    def api_standard_rules(self, pack_id: str) -> dict:
        """某个标准包的元信息 + 全部规则 + 每条规则的实现逻辑说明。

        与启用状态无关——未启用的包也要能查看它有什么规则。
        """
        meta = next((p for p in list_packs(STANDARDS_ROOT) if p.get("id") == pack_id), None)
        if meta is None:
            return {"error": "未找到标准包：%s" % pack_id}
        try:
            # 文档视图要看到包里**全部**规则，不受 default_subsets 影响
            rules, _ = load_standard_pack(os.path.join(STANDARDS_ROOT, pack_id))
        except FileNotFoundError:
            return {"error": "标准包规则目录不存在：%s" % pack_id}
        out = []
        for r in rules:
            d = r.to_dict()
            d["impl"] = _describe_impl(r)
            d["subsets"] = rule_subsets(r, meta)
            out.append(d)
        return {**meta, "id": pack_id, "total": len(out), "rules": out}

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
            # 本次运行的子集覆盖（{pack: [subset_id,...]}）：只在这一次生效，不写回配置。
            subsets = body.get("subsets") if isinstance(body.get("subsets"), dict) else None
            subs_map = _merge_subsets(cfg, subsets)
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
                    rs, _ = load_pack_by_id(sid, STANDARDS_ROOT, subsets=subs_map.get(sid))
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
