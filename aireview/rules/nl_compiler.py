"""Natural-language -> structured rule compiler (local LLM).

Turns a user sentence like "禁止在信号处理函数里调用非异步信号安全函数"
into a structured Rule. The LLM only emits the rule *spec*; the rule engine
does the actual checking. If no local model is reachable we refuse to guess and
hand back a ready-to-edit template instead.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from . import validate_rule, RuleValidationError

RULE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "name", "severity", "type", "description", "fix_suggestion", "match"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "name": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
        "category": {"type": "string"},
        "type": {"type": "string", "enum": ["ast_pattern", "regex", "graph", "semantic"]},
        "language": {"type": "string", "enum": ["c", "cpp", "multi"]},
        "description": {"type": "string"},
        "rationale": {"type": "string"},
        "fix_suggestion": {"type": "string"},
        "references": {"type": "array", "items": {"type": "string"}},
        "match": {
            "type": "object",
            "oneOf": [
                {"required": ["query"], "properties": {"query": {"type": "string"}, "message": {"type": "string"}}},
                {"required": ["pattern"], "properties": {"pattern": {"type": "string"},
                                                         "file_scope": {"type": "object"},
                                                         "message": {"type": "string"}}},
                {"required": ["analyzer"], "properties": {"analyzer": {"type": "string"},
                                                         "params": {"type": "object"}}},
                {"required": ["prompt"], "properties": {"prompt": {"type": "string"},
                                                       "context": {"type": "object"}}},
            ],
        },
    },
}

SYSTEM_PROMPT = """你是一个代码规则编译器。把用户的自然语言编码规范，编译成一条结构化静态分析规则。
只输出 JSON，不要解释。规则类型说明：
- ast_pattern: 用 tree-sitter 查询匹配 AST 节点。match.query 是 S-expression 查询，必须捕获 @node（被报告节点）和 @name（用于消息）。示例：
  "(call_expression function: (identifier) @name (#eq? @name \\"gets\\")) @node"
- regex: 用正则表达式匹配源码文本。match.pattern 是正则，match.file_scope 可选 {language, path_glob}。match.message 可用 {{match}} 占位。
- graph: 基于调用/依赖图的跨模块分析。match.analyzer 是分析器名（如 signal_safety），match.params 为参数。
- semantic: 需要 LLM 语义判断。match.prompt 是判定指令，match.context 描述取哪些上下文（如 {kind: call_chain, depth: 3}）。
severity 取值: critical|high|medium|low|info。id 用小写连字符。如果规则难以自动化，type 用 semantic 或 graph，并在 description 中说明。
"""


@dataclass
class CompileResult:
    ok: bool
    rule: dict | None = None
    error: str = ""
    needs_review: bool = False
    confidence: float = 0.0
    template: str = ""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "-", text.lower()).strip("-")
    return f"nl-{s[:40]}" if s else "nl-rule"


def compile_nl(text: str, llm, *, category: str = "nl-custom", fallback_id: str | None = None) -> CompileResult:
    """Compile NL text into a rule using the local LLM. Returns template if LLM unavailable."""
    if llm is None:
        tid = fallback_id or _slug(text)
        template = _template(text, tid, category)
        return CompileResult(ok=False, error="本地模型不可用，已生成规则模板供手工编辑（见 template 字段）。",
                             needs_review=True, template=template)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"把下面这条规范编译成规则：\n{text}"},
    ]
    try:
        raw = llm.chat_json(messages, response_schema=RULE_JSON_SCHEMA, temperature=0.0)
    except Exception as e:  # pragma: no cover
        return CompileResult(ok=False, error=f"模型调用失败：{e}", needs_review=True,
                             template=_template(text, fallback_id or _slug(text), category))

    raw.setdefault("category", category)
    raw.setdefault("language", "multi")
    if not raw.get("id"):
        raw["id"] = fallback_id or _slug(text)
    try:
        rule = validate_rule(raw)
    except RuleValidationError as e:
        return CompileResult(ok=False, error=f"编译结果校验失败：{e}", needs_review=True,
                             template=_template(text, raw.get("id", _slug(text)), category))

    confidence = float(raw.get("confidence", 0.8))
    needs_review = confidence < 0.7
    return CompileResult(ok=True, rule=rule.to_dict(), needs_review=needs_review,
                         confidence=confidence)


def _template(text: str, rid: str, category: str) -> str:
    return f"""# 规则模板（本地模型不可用，请手工补全 match 后保存到 .aireview/rules/<id>.yaml）
id: {rid}
name: "{text[:40]}"
severity: medium
category: {category}
type: regex            # 可选 ast_pattern | regex | graph | semantic
language: c
description: "{text}"
rationale: ""
fix_suggestion: ""
enabled: true
match:
  pattern: "TODO: 填写匹配正则"
  file_scope: {{ language: c }}
  message: "命中自定义规则：{{match}}"
references: []
"""
