"""Rule schema, validation and standard-pack loading."""
from __future__ import annotations

import glob
import os
import sys
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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "severity": self.severity,
            "category": self.category, "type": self.type, "language": self.language,
            "description": self.description, "rationale": self.rationale,
            "fix_suggestion": self.fix_suggestion, "enabled": self.enabled,
            "match": self.match, "references": self.references,
            "confidence": self.confidence, "source": self.source,
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


def load_pack_by_id(pack_id: str, standards_root: str) -> tuple[list[Rule], dict]:
    pack_dir = os.path.join(standards_root, pack_id)
    if not os.path.isdir(pack_dir):
        raise FileNotFoundError(f"standard pack not found: {pack_id} (looked in {standards_root})")
    return load_standard_pack(pack_dir)


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
