"""Rule schema, validation and standard-pack loading."""
from __future__ import annotations

import glob
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import Any

import yaml

SEVERITIES = ("critical", "high", "medium", "low", "info")
RULE_TYPES = ("ast_pattern", "regex", "graph", "semantic")


@dataclass
class Rule:
    id: str
    name: str
    severity: str = "medium"
    category: str = "custom"
    type: str = "regex"
    language: str = "multi"          # c | cpp | multi
    description: str = ""
    rationale: str = ""
    fix_suggestion: str = ""
    enabled: bool = True
    match: dict = field(default_factory=dict)
    references: list = field(default_factory=list)
    confidence: float = 1.0
    source: str = "user"
    tags: list = field(default_factory=list)      # 子集/自动化等级标签，如 ["STR", "auto"]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "severity": self.severity,
            "category": self.category, "type": self.type, "language": self.language,
            "description": self.description, "rationale": self.rationale,
            "fix_suggestion": self.fix_suggestion, "enabled": self.enabled,
            "match": self.match, "references": self.references,
            "confidence": self.confidence, "source": self.source,
            "tags": list(self.tags),
        }


class RuleValidationError(Exception):
    pass


def validate_rule(d: dict) -> Rule:
    rid = d.get("id")
    if not rid:
        raise RuleValidationError("rule missing 'id'")
    rtype = d.get("type", "regex")
    if rtype not in RULE_TYPES:
        raise RuleValidationError(f"unknown rule type '{rtype}' (allowed: {RULE_TYPES})")
    sev = d.get("severity", "medium")
    if sev not in SEVERITIES:
        raise RuleValidationError(f"invalid severity '{sev}' (allowed: {SEVERITIES})")
    match = d.get("match", {})
    if rtype == "ast_pattern" and "query" not in match:
        raise RuleValidationError("ast_pattern rule requires match.query")
    if rtype == "regex" and "pattern" not in match:
        raise RuleValidationError("regex rule requires match.pattern")
    if rtype == "graph" and "analyzer" not in match:
        raise RuleValidationError("graph rule requires match.analyzer")
    if rtype == "semantic" and "prompt" not in match:
        raise RuleValidationError("semantic rule requires match.prompt")
    return Rule(
        id=rid, name=d.get("name", rid), severity=sev, category=d.get("category", "custom"),
        type=rtype, language=d.get("language", "multi"), description=d.get("description", ""),
        rationale=d.get("rationale", ""), fix_suggestion=d.get("fix_suggestion", ""),
        enabled=bool(d.get("enabled", True)), match=match, references=d.get("references", []),
        confidence=float(d.get("confidence", 1.0)), source=d.get("source", "user"),
        tags=list(d.get("tags") or []),
    )


# --------------------------------------------------------------------------
# Standard-pack loading
# --------------------------------------------------------------------------
def load_standard_pack(pack_dir: str) -> tuple[list[Rule], dict]:
    """Load a standard pack directory: manifest.yaml + rules/*.yaml."""
    manifest_path = os.path.join(pack_dir, "manifest.yaml")
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = yaml.safe_load(fh) or {}
    rules: list[Rule] = []
    rule_files = sorted(glob.glob(os.path.join(pack_dir, "rules", "*.yaml")))
    rule_files += sorted(glob.glob(os.path.join(pack_dir, "rules", "*.yml")))
    for rf in rule_files:
        with open(rf, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, list):
            items = data
        else:
            items = data.get("rules", [data])
        for item in items:
            try:
                rule = validate_rule(item)
            except RuleValidationError as e:
                raise RuleValidationError(f"{os.path.basename(rf)}: {e}")
            rule.source = manifest.get("id", os.path.basename(pack_dir))
            rule.category = rule.category or manifest.get("id", "custom")
            rules.append(rule)
    return rules, manifest


# --------------------------------------------------------------------------
# Subsets（标准包内的规则子集）
#
# manifest.yaml 里这样声明：
#   subsets:
#     - {id: core,  name: 核心高危, tags: [core]}
#     - {id: str,   name: 字符串 (STR), tags: [STR]}
#     - {id: manual, name: 我挑的几条, rules: [STR31-C, MEM30-C]}
# 规则侧用 tags 打标（如 tags: [STR, auto, core]）。
# 命中规则：subset.rules 含该 id，或 subset.tags 与 rule.tags 有交集。
# 既无 rules 又无 tags 的子集表示"本包全部规则"。
# --------------------------------------------------------------------------
def subset_index(manifest: dict) -> dict[str, dict]:
    return {s.get("id"): s for s in (manifest.get("subsets") or []) if s.get("id")}


def rule_matches_subset(rule: "Rule", subset: dict) -> bool:
    ids = subset.get("rules") or []
    tags = subset.get("tags") or []
    if not ids and not tags:
        return True
    if rule.id in ids:
        return True
    if tags and set(tags) & set(rule.tags or []):
        return True
    return False


def rule_subsets(rule: "Rule", manifest: dict) -> list[str]:
    """这条规则属于哪些子集 id —— 供 UI 展示。"""
    return [s["id"] for s in (manifest.get("subsets") or [])
            if s.get("id") and rule_matches_subset(rule, s)]


def filter_by_subsets(rules: list["Rule"], manifest: dict,
                      subset_ids: list[str] | None = None) -> list["Rule"]:
    """按启用的子集过滤规则。

    `subset_ids=None` 表示「调用方没有指定」→ 优先用 manifest 的 `default_subsets`，
    没有声明默认子集才返回全部规则（对老包保持向后兼容）。
    显式传空列表表示「该包的子集一个都没选」→ 返回空。
    """
    defs = manifest.get("subsets") or []
    if not defs:
        return rules
    if subset_ids is None:
        defaults = manifest.get("default_subsets")
        if not defaults:
            return rules
        subset_ids = list(defaults)
    selected = [s for s in defs if s.get("id") in set(subset_ids)]
    if not selected:
        return []
    return [r for r in rules if any(rule_matches_subset(r, s) for s in selected)]


def parse_subset_specs(specs) -> dict[str, list[str]]:
    """把 `PACK:ID[,ID]` 形式的子集写法解析成 `{pack_id: [subset_id, ...]}`。

    CLI 的 `--subset` 与 Web 的 `?subset=` 共用这一套写法，避免两处各解析一遍：
        --subset cert-c:core,STR --subset python:security
        ?subset=cert-c:core,STR&subset=python:security
    不带冒号的项忽略（CLI 侧另行报错）。
    """
    out: dict[str, list[str]] = {}
    for spec in specs or []:
        if not spec or ":" not in spec:
            continue
        pack, _, ids = spec.partition(":")
        pack = pack.strip()
        if not pack:
            continue
        out.setdefault(pack, []).extend([x.strip() for x in ids.split(",") if x.strip()])
    return out


def unknown_subsets(manifest: dict, subset_ids) -> list[str]:
    """请求了但 manifest 里不存在的子集 id。

    不做大小写/标签的模糊匹配——`STR` 是规则 tag、`str` 才是子集 id，静默把 `STR`
    当成 `str` 会让「写错了」看起来像「本来就没有规则」。调用方拿到这个列表后
    应当提示用户，而不是悄悄返回 0 条规则。
    """
    known = set(subset_index(manifest))
    out = []
    for sid in subset_ids or []:
        if sid and sid not in known and sid not in out:
            out.append(sid)
    return out


def load_pack_by_id(pack_id: str, standards_root: str,
                    subsets: list[str] | None = None) -> tuple[list[Rule], dict]:
    pack_dir = os.path.join(standards_root, pack_id)
    if not os.path.isdir(pack_dir):
        raise FileNotFoundError(f"standard pack not found: {pack_id} (looked in {standards_root})")
    rules, manifest = load_standard_pack(pack_dir)
    bad = unknown_subsets(manifest, subsets)
    if bad:
        avail = ", ".join(s["id"] for s in (manifest.get("subsets") or []) if s.get("id"))
        warnings.warn(f"标准包 {pack_id} 没有这些子集：{', '.join(bad)}（可用：{avail}）")
    return filter_by_subsets(rules, manifest, subsets), manifest


def list_packs(standards_root: str) -> list[dict]:
    out = []
    if not os.path.isdir(standards_root):
        return out
    for name in sorted(os.listdir(standards_root)):
        mp = os.path.join(standards_root, name, "manifest.yaml")
        if os.path.isfile(mp):
            with open(mp, "r", encoding="utf-8") as fh:
                m = yaml.safe_load(fh) or {}
            out.append({"id": name, **m})
    return out


def load_user_rules_dir(rules_dir: str) -> list[Rule]:
    """Load all *.yaml/*.yml rule files from a user rules directory."""
    rules: list[Rule] = []
    if not os.path.isdir(rules_dir):
        return rules
    for name in sorted(os.listdir(rules_dir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(rules_dir, name)
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        items = data if isinstance(data, list) else data.get("rules", [data])
        for item in items:
            try:
                rule = validate_rule(item)
            except RuleValidationError as e:
                print(f"[warn] 用户规则 {name} 校验失败: {e}", file=sys.stderr)
                continue
            rule.source = "user"
            rules.append(rule)
    return rules
