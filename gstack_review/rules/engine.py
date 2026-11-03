"""Rule engine: dispatches each rule to the right matcher.

Matcher types:
  - ast_pattern : tree-sitter query over AST (deterministic, fast, parallel).
  - regex       : regex over file text (fast).
  - graph       : cross-module graph analyzers (e.g. signal-handler safety).
  - semantic    : LLM-based judgment with context slice from the call graph.

Structured matchers (ast/regex/graph) run offline & deterministically; only
`semantic` touches the local LLM — satisfying "rule-based checks" while still
using local AI where genuine semantic judgment is needed.
"""
from __future__ import annotations

import fnmatch
import os
import re
from typing import Any

from ..parser.tslang import get_parser, get_language, TREE_SITTER_AVAILABLE

# Functions that are NOT async-signal-safe (POSIX). Used by the signal-safety
# graph analyzer to demonstrate cross-module call-graph reasoning.
NON_SIGSAFE = {
    "malloc", "free", "calloc", "realloc", "printf", "fprintf", "sprintf",
    "snprintf", "scanf", "fscanf", "sscanf", "gets", "fgets", "puts", "fputs",
    "strcpy", "strcat", "strlen", "rand", "srand", "exit", "abort", "longjmp",
    "open", "close", "read", "write", "fopen", "fclose", "time", "localtime",
    "gmtime", "strtok", "strerror", "malloc", "free",
}


# file_scope.language / rule.language values that mean "match any language".
# The NL compiler sometimes emits `all`; treat it (and `multi`/`any`/`*`) as a
# wildcard instead of a literal language name — otherwise such rules silently
# match zero files.
_WILDCARD_LANGS = {"all", "multi", "any", "*", ""}


def _snippet(source: str, line: int, end_line: int | None = None) -> str:
    lines = source.splitlines()
    if not lines:
        return ""
    lo = max(0, line - 1)
    hi = min(len(lines), (end_line or line))
    return "\n".join(lines[lo:hi]).strip()


def _render_message(template: str, **kw) -> str:
    if not template:
        return ""
    for k, v in kw.items():
        template = template.replace("{{%s}}" % k, str(v))
    return template


class ModelUnavailable(RuntimeError):
    """Raised by the semantic matcher when no LLM client is available.

    Surfaced to the caller as a skipped rule with ``kind="model_offline"`` so the
    UI can distinguish a genuine model outage from an ordinary rule error.
    """
    pass


class RuleEngine:
    def __init__(self, index, llm=None):
        self.index = index
        self.llm = llm

    # ------------------------------------------------------------------
    def run(self, rules: list) -> dict:
        findings: list[dict] = []
        rule_status: list[dict] = []
        skipped: list[dict] = []
        for rule in rules:
            if not getattr(rule, "enabled", True):
                continue
            try:
                fs, applied = self._run_one(rule)
            except Exception as e:
                kind = "model_offline" if isinstance(e, ModelUnavailable) else "matcher_error"
                skipped.append({"rule_id": rule.id, "reason": f"matcher error: {e}", "kind": kind})
                continue
            for f in fs:
                findings.append(f)
            rule_status.append({
                "id": rule.id, "name": rule.name, "severity": rule.severity,
                "type": rule.type, "status": "failed" if fs else "passed",
                "applied_count": applied, "findings": len(fs),
            })
        return {"findings": findings, "rule_status": rule_status, "skipped": skipped}

    # ------------------------------------------------------------------
    def _run_one(self, rule) -> tuple[list[dict], int]:
        if rule.type == "ast_pattern":
            return self._match_ast(rule)
        if rule.type == "regex":
            return self._match_regex(rule)
        if rule.type == "graph":
            return self._match_graph(rule)
        if rule.type == "semantic":
            return self._match_semantic(rule)
        return [], 0

    # ---- AST ----------------------------------------------------------
    def _match_ast(self, rule) -> tuple[list[dict], int]:
        if not TREE_SITTER_AVAILABLE:
            raise RuntimeError("tree-sitter 不可用，ast_pattern 规则无法执行（已跳过）")
        query_str = rule.match["query"]
        msg_tpl = rule.match.get("message", "Matched {{name}}")
        files = [f for f in self.index.get_files() if self._lang_ok(rule, f["language"])]
        applied = 0
        out: list[dict] = []
        for f in files:
            lang = f["language"]
            parser = get_parser(lang)
            if parser is None:
                continue
            source = self.index.read_source(f["path"])
            if not source:
                continue
            tree = parser.parse(source.encode("utf-8", "replace"))
            caps = _run_query(get_language(lang), query_str, tree.root_node)
            node_list = caps.get("node", [])
            name_list = caps.get("name", [])
            applied += 1
            for i, node in enumerate(node_list):
                name = name_list[i].text.decode("utf-8", "replace") if i < len(name_list) else ""
                line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                out.append(self._finding(rule, f["path"], line, node.start_point[1] + 1,
                                        end_line, node.end_point[1] + 1,
                                        _render_message(msg_tpl, name=name, fn=name),
                                        _snippet(source, line, end_line)))
        return out, applied

    # ---- regex --------------------------------------------------------
    def _match_regex(self, rule) -> tuple[list[dict], int]:
        pattern = rule.match["pattern"]
        flags = re.MULTILINE
        if rule.match.get("ignore_case"):
            flags |= re.IGNORECASE
        rx = re.compile(pattern, flags)
        msg_tpl = rule.match.get("message", "Matched {{match}}")
        scope = rule.match.get("file_scope", {}) or {}
        scope_lang = scope.get("language")
        scope_glob = scope.get("path_glob")
        files = self.index.get_files()
        applied = 0
        out: list[dict] = []
        for f in files:
            # `language: all` / `multi` / `any` / `*` (or empty) means "no language filter".
            if scope_lang and scope_lang.lower() not in _WILDCARD_LANGS and f["language"] != scope_lang:
                continue
            if scope_glob and not fnmatch.fnmatch(f["path"], scope_glob):
                continue
            source = self.index.read_source(f["path"])
            if not source:
                continue
            applied += 1
            for m in rx.finditer(source):
                line = source[:m.start()].count("\n") + 1
                end_line = source[:m.end()].count("\n") + 1
                text = m.group(0)
                out.append(self._finding(rule, f["path"], line, m.start() - source.rfind("\n", 0, m.start()) - 1,
                                        end_line, 0,
                                        _render_message(msg_tpl, match=text, line_number=line, line=line),
                                        _snippet(source, line, end_line)))
        return out, applied

    # ---- logic-consistency prompt templates (LLM-driven) --------------
    @staticmethod
    def _fmt_invariants(invariants) -> str:
        if not invariants:
            return "(无显式不变量)"
        lines = []
        for iv in invariants:
            if isinstance(iv, dict):
                syms = ", ".join(iv.get("symbols", []) or []) or "-"
                lines.append(f"- [{iv.get('id', '?')}] {iv.get('text', '')}  (symbols: {syms})")
            else:
                lines.append(f"- {iv}")
        return "\n".join(lines) if lines else "(无显式不变量)"

    _LOGIC_MODULE_PROMPT = """你是资深代码审查专家。分析下面这个模块（目录：{module}）的业务逻辑，仅输出 JSON，不要额外文字：
{{
  "summary": "<模块的业务职责与核心流程摘要>",
  "invariants": [
    {{"id": "inv1", "text": "<关键业务不变量/契约描述>", "symbols": ["<相关函数名>"]}}
  ]
}}
invariants 指：关键业务契约、前置/后置条件、错误处理预期、状态机规则，以及该模块对调用方/被调方的不假设。不要编造不存在的函数；若模块无明显不变量，invariants 可为空数组。

模块源码：
{source}
"""

    _LOGIC_PAIR_PROMPT = """你是资深代码审查专家。下面有两个相互调用的模块 A 与 B。请判断 B 对 A 契约的使用（或 A 对 B 的假设）是否存在业务逻辑矛盾（例如：A 要求调用方先初始化才能调用，但 B 未初始化就调用；A 的错误码语义与 B 的处理方式冲突；状态机转移被对方违背；参数/返回值的业务约定被违反等）。仅输出 JSON：
{{
  "contradictions": [
    {{"message": "<矛盾描述>", "severity": "high|medium|low|info", "confidence": 0.0}}
  ]
}}
若无矛盾，contradictions 为空数组 []。

模块 A（{A}）摘要：{a_summary}
模块 A 不变量/契约：
{a_invariants}

模块 A 源码（节选）：
{a_source}

模块 B（{B}）摘要：{b_summary}

模块 B 源码（节选）：
{b_source}
"""

    _LOGIC_SELF_PROMPT = """你是资深代码审查专家。下面这个模块声明了若干业务不变量/契约，请判断这些不变量之间、以及它们与该模块自身实现之间是否存在内部逻辑矛盾（例如相互冲突的约束、不可能同时满足的状态、自相矛盾的错误处理）。仅输出 JSON：
{{
  "contradictions": [
    {{"message": "<矛盾描述>", "severity": "high|medium|low|info", "confidence": 0.0}}
  ]
}}
若无矛盾，contradictions 为空数组 []。

模块（{module}）摘要：{summary}
不变量/契约：
{invariants}

模块源码（节选）：
{source}
"""

    # ---- graph --------------------------------------------------------
    def _match_graph(self, rule) -> tuple[list[dict], int]:
        analyzer = rule.match.get("analyzer")
        if analyzer == "signal_safety":
            return self._analyze_signal_safety(rule)
        if analyzer == "logic_consistency":
            return self._analyze_logic_consistency(rule)
        raise RuntimeError(f"unknown graph analyzer: {analyzer}")

    def _analyze_signal_safety(self, rule) -> tuple[list[dict], int]:
        handlers = self._find_signal_handlers()
        graph = self.index.call_graph()
        # Prefer the rule's own unsafe-function list; fall back to the built-in set.
        custom = rule.match.get("params", {}).get("unsafe_functions") or []
        unsafe_set = set(custom) if custom else NON_SIGSAFE
        max_depth = int(rule.match.get("params", {}).get("max_depth", 8))
        out: list[dict] = []
        for name, info in handlers.items():
            file_path = info["file"]
            callees = info["callees"]
            # BFS over the call graph (cross-module: callees may live in other files).
            visited, queue, depth = set(), list(callees), 0
            unsafe_hits: list[str] = []
            while queue and depth < max_depth:
                nxt = []
                for fn in queue:
                    if fn in visited:
                        continue
                    visited.add(fn)
                    if fn in unsafe_set:
                        unsafe_hits.append(fn)
                    nxt.extend(graph.get(fn, set()))
                queue, depth = nxt, depth + 1
            if not unsafe_hits:
                continue
            syms = self.index.symbols_named(name)
            line = syms[0]["line"] if syms else 1
            detail = ", ".join(sorted(set(unsafe_hits)))
            out.append(self._finding(
                rule, file_path, line, 1, line, 1,
                f"信号处理函数 '{name}' 的调用链中调用了非异步信号安全函数：{detail}（跨模块调用链已展开）",
                f"处理函数: {name}(); 不安全调用: {detail}"))
        return out, len(handlers)

    def _find_signal_handlers(self) -> dict:
        """Return {handler_name: {"file": path, "callees": set}}."""
        result: dict[str, dict] = {}
        files = [f for f in self.index.get_files() if f["language"] in ("c", "cpp")]
        for f in files:
            parser = get_parser(f["language"])
            if parser is None:
                continue
            source = self.index.read_source(f["path"])
            if not source:
                continue
            tree = parser.parse(source.encode("utf-8", "replace"))
            caps = _run_query(get_language(f["language"]), _SIGNAL_HANDLER_QUERY, tree.root_node)
            fns = caps.get("fn", [])
            # `signal(SIGINT, handler)` -> handler is the last identifier in args.
            for i, arglist in enumerate(caps.get("args", [])):
                fn_name = fns[i].text.decode("utf-8", "replace") if i < len(fns) else ""
                if fn_name != "signal":
                    continue
                ids = [c for c in arglist.children if c.type == "identifier"]
                if ids:
                    name = ids[-1].text.decode("utf-8", "replace")
                    if name and name not in result:
                        result[name] = {"file": f["path"], "callees": set(self.index.callees_of(name))}
            # `sa_handler = handler` (sigaction style).
            fs = caps.get("f", [])
            for i, node in enumerate(caps.get("handler", [])):
                field = fs[i].text.decode("utf-8", "replace") if i < len(fs) else ""
                if field != "sa_handler":
                    continue
                name = node.text.decode("utf-8", "replace")
                if name and name not in result:
                    result[name] = {"file": f["path"], "callees": set(self.index.callees_of(name))}
        return result

    # ---- logic consistency (business-logic, cross-module) --------------
    def _analyze_logic_consistency(self, rule) -> tuple[list[dict], int]:
        """Whole-codebase business-logic extraction + per-module contradiction regression.

        Pass 1: group files by directory (module); ask the local LLM to summarise
        each module's business logic and enumerate its invariants/contracts.
        Pass 2: for each interacting module pair (edges in the cross-module call
        graph), ask the LLM whether the contracts of one contradict the actual
        usage/implementation in the other. Emits findings for contradictions.
        """
        if self.llm is None:
            raise ModelUnavailable("本地模型不可用，logic_consistency 需要 LLM（已跳过）")
        import os
        ctx = rule.match.get("context", {}) or {}
        max_pairs = int(ctx.get("max_pairs", 40))
        module_src_limit = int(ctx.get("module_src_limit", 8000))
        prompt_template = rule.match.get("prompt")

        files = self.index.get_files()
        modules: dict[str, list[str]] = {}
        module_rep: dict[str, str] = {}
        for f in files:
            mod = os.path.dirname(f["path"]) or "(root)"
            modules.setdefault(mod, []).append(f["path"])
            if mod not in module_rep:
                module_rep[mod] = f["path"]

        # ---- Pass 1: per-module logic + invariants ----
        modules_logic: dict[str, dict] = {}
        for mod, paths in modules.items():
            src = self._concat_module(paths, module_src_limit)
            if not src.strip():
                continue
            prompt = (prompt_template or self._LOGIC_MODULE_PROMPT).format(module=mod, source=src)
            try:
                data = self.llm.chat_json([{"role": "user", "content": prompt}], temperature=0.0)
            except Exception:
                continue
            if isinstance(data, dict) and (data.get("invariants") or data.get("summary")):
                modules_logic[mod] = data

        # ---- map functions -> module via the call graph ----
        cg = self.index.call_graph()
        fn_module: dict[str, str] = {}
        names = set(cg.keys())
        for callees in cg.values():
            names.update(callees)
        for name in names:
            syms = self.index.symbols_named(name)
            if syms:
                fn_module[name] = os.path.dirname(syms[0]["path"]) or "(root)"

        # ---- Pass 2: per-pair contradiction regression ----
        pairs: set[tuple[str, str]] = set()
        for caller, callees in cg.items():
            a = fn_module.get(caller)
            for callee in callees:
                b = fn_module.get(callee)
                if a and b and a != b:
                    pairs.add(tuple(sorted((a, b))))
        pairs = sorted(pairs)[:max_pairs]

        out: list[dict] = []
        for (A, B) in pairs:
            out.extend(self._logic_pair_check(rule, A, B, modules_logic, module_rep, modules, module_src_limit))

        # ---- intra-module self-consistency ----
        for mod, data in modules_logic.items():
            out.extend(self._logic_self_check(rule, mod, data, module_rep, modules, module_src_limit))

        return out, len(pairs) + len(modules_logic)

    def _concat_module(self, paths: list[str], limit: int) -> str:
        parts, total = [], 0
        for p in paths:
            src = self.index.read_source(p)
            if not src:
                continue
            head = f"\n# === file: {p} ===\n"
            room = max(0, limit - total - len(head))
            if room <= 0:
                break
            src = src[:room]
            parts.append(head + src)
            total += len(head) + len(src)
            if total >= limit:
                break
        return "\n".join(parts)

    def _logic_pair_check(self, rule, A, B, modules_logic, module_rep, modules, src_limit) -> list[dict]:
        la = modules_logic.get(A, {})
        lb = modules_logic.get(B, {})
        if not la and not lb:
            return []
        a_src = self._concat_module(modules.get(A, []), src_limit)
        b_src = self._concat_module(modules.get(B, []), src_limit)
        prompt = self._LOGIC_PAIR_PROMPT.format(
            A=A, B=B,
            a_summary=la.get("summary", ""),
            a_invariants=self._fmt_invariants(la.get("invariants", [])),
            b_summary=lb.get("summary", ""),
            a_source=a_src,
            b_source=b_src,
        )
        try:
            data = self.llm.chat_json([{"role": "user", "content": prompt}], temperature=0.0)
        except Exception:
            return []
        return self._collect_contradictions(rule, data, module_rep.get(A, A), module_rep.get(B, B), A, B)

    def _logic_self_check(self, rule, mod, data, module_rep, modules, src_limit) -> list[dict]:
        src = self._concat_module(modules.get(mod, []), src_limit)
        prompt = self._LOGIC_SELF_PROMPT.format(
            module=mod,
            summary=data.get("summary", ""),
            invariants=self._fmt_invariants(data.get("invariants", [])),
            source=src,
        )
        try:
            resp = self.llm.chat_json([{"role": "user", "content": prompt}], temperature=0.0)
        except Exception:
            return []
        return self._collect_contradictions(rule, resp, module_rep.get(mod, mod), module_rep.get(mod, mod), mod, mod)

    def _collect_contradictions(self, rule, data, file_a, file_b, label_a, label_b) -> list[dict]:
        contradictions = (data or {}).get("contradictions") or []
        out: list[dict] = []
        for c in contradictions:
            if not isinstance(c, dict):
                continue
            msg = c.get("message") or c.get("text") or ""
            if not msg:
                continue
            sev = c.get("severity") or rule.severity
            if sev not in ("critical", "high", "medium", "low", "info"):
                sev = rule.severity
            target = file_b if (c.get("module") == label_b) else file_a
            out.append(self._finding(
                rule, target, 1, 1, 1, 1,
                f"[{label_a} ↔ {label_b}] {msg}",
                f"模块 {label_a} 与 {label_b} 之间存在业务逻辑矛盾：{msg}",
                confidence=float(c.get("confidence", 0.7)),
            ))
        return out

    # ---- semantic -----------------------------------------------------
    def _match_semantic(self, rule) -> tuple[list[dict], int]:
        if self.llm is None:
            raise ModelUnavailable("本地模型不可用，semantic 规则需要 LLM（已跳过；可用 --only-structured 明确跳过）")
        ctx = rule.match.get("context", {}) or {}
        cand_rx = ctx.get("candidate_regex")
        limit = int(ctx.get("limit", 50))
        files = self.index.get_files()
        candidates: list[tuple[str, int, str, str]] = []  # (path, line, source, name)
        for f in files:
            if f["language"] not in ("c", "cpp"):
                continue
            source = self.index.read_source(f["path"])
            if not source:
                continue
            for fn in self._iter_functions(f["path"], source):
                if cand_rx and not re.search(cand_rx, fn["source"]):
                    continue
                candidates.append((f["path"], fn["line"], fn["source"], fn["name"]))
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break
        if not candidates:
            raise RuntimeError("semantic 规则无候选函数（缺少 candidate_regex 或函数为空），已跳过")
        out: list[dict] = []
        applied = 0
        for path, line, src, fname in candidates:
            applied += 1
            callees = self.index.callees_of(fname) if fname else []
            prompt = (f"{rule.match['prompt']}\n\n函数源码:\n{src}\n"
                      f"该函数的直接被调用者(callees): {', '.join(callees)}\n"
                      "若违反规则，输出 JSON: {\"violation\": true, \"line\": <函数内偏移行>, \"message\": \"...\", \"confidence\": 0-1}; "
                      "否则 {\"violation\": false}.")
            try:
                resp = self.llm.chat_json([{"role": "user", "content": prompt}], temperature=0.0)
            except Exception:
                continue
            if resp.get("violation"):
                conf = float(resp.get("confidence", 0.6))
                out.append(self._finding(rule, path, line + int(resp.get("line", 0)), 1,
                                        line + int(resp.get("line", 0)), 1,
                                        resp.get("message", "semantic violation"),
                                        src[:200], confidence=conf))
        return out, applied

    # ---- helpers ------------------------------------------------------
    def _lang_ok(self, rule, file_lang: str) -> bool:
        if (rule.language or "").lower() in _WILDCARD_LANGS:
            return True
        return rule.language == file_lang

    def _finding(self, rule, file, line, col, end_line, end_col, message, snippet, confidence: float | None = None) -> dict:
        return {
            "rule_id": rule.id, "rule_name": rule.name, "severity": rule.severity,
            "category": rule.category, "file": file, "line": line, "column": col,
            "end_line": end_line, "end_column": end_col, "message": message,
            "snippet": snippet, "suggestion": rule.fix_suggestion,
            "confidence": confidence if confidence is not None else (rule.confidence if rule.type == "semantic" else None),
        }

    def _iter_functions(self, path: str, source: str):
        parser = get_parser("c" if path.endswith((".c", ".h")) else "cpp")
        if parser is None:
            return
        tree = parser.parse(source.encode("utf-8", "replace"))
        # reuse a simple walk to yield function definitions with source
        from ..parser.extractors import _function_name_from_declarator
        root = tree.root_node
        def walk(n):
            if n.type == "function_definition":
                decl = n.child_by_field_name("declarator")
                name = _function_name_from_declarator(decl) if decl else ""
                src = n.text.decode("utf-8", "replace")
                yield {"name": name, "line": n.start_point[0] + 1, "source": src}
            for ch in n.children:
                yield from walk(ch)
        yield from walk(root)

    def _first_func_name(self, src: str) -> str:
        m = re.search(r"\n(\w+)\s*\(", src)
        return m.group(1) if m else ""


# --------------------------------------------------------------------------
# tree-sitter query helper (version-tolerant)
# --------------------------------------------------------------------------
_SIGNAL_HANDLER_QUERY = """
(
  (call_expression
    function: (identifier) @fn
    arguments: (argument_list) @args) @call
)
(
  (assignment_expression
    left: (field_expression field: (field_identifier) @f)
    right: (identifier) @handler)
) @assign
"""


def _run_query(lang, query_str: str, root) -> dict:
    """Run a tree-sitter query, returning {capture_name: [nodes]} (version tolerant)."""
    from tree_sitter import Query, QueryCursor
    try:
        q = Query(lang, query_str)
    except TypeError:
        q = lang.query(query_str)
    # tree-sitter >=0.23: execute via QueryCursor.
    try:
        cursor = QueryCursor(q)
        return cursor.captures(root) or {}
    except (AttributeError, NameError):
        pass
    # older API variants
    try:
        caps = q.captures(root)
    except AttributeError:
        raw = q.matches(root)
        caps = {}
        for _idx, c in raw:
            for name, nodes in c.items():
                caps.setdefault(name, []).extend(nodes)
        return caps
    if isinstance(caps, tuple):
        names, nodes = caps
        caps = {}
        for nm, nd in zip(names, nodes):
            caps.setdefault(nm, []).append(nd)
    return caps or {}
