"""Report generation: rule-structured output (text / JSON / SARIF) + gate exit code."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .. import __version__

SEV_ORDER = ["critical", "high", "medium", "low", "info"]
GATE_DEFAULT = ("critical", "high")


def build_report(metadata: dict, result: dict, rules: list) -> dict:
    findings = result["findings"]
    rule_status = result["rule_status"]
    sev_counter = Counter(f["severity"] for f in findings)
    by_cat: dict[str, dict] = defaultdict(lambda: {"enabled_rules": 0, "failed_rules": 0})
    for rs in rule_status:
        cat = next((r.category for r in rules if r.id == rs["id"]), rs.get("category", "custom"))
        by_cat[cat]["enabled_rules"] += 1
        if rs["status"] == "failed":
            by_cat[cat]["failed_rules"] += 1

    report = {
        "metadata": metadata,
        "summary": {
            "total_rules": len(rule_status),
            "rules_passed": sum(1 for r in rule_status if r["status"] == "passed"),
            "rules_failed": sum(1 for r in rule_status if r["status"] == "failed"),
            "total_findings": len(findings),
            "findings_by_severity": {s: sev_counter.get(s, 0) for s in SEV_ORDER},
        },
        "skipped": result.get("skipped", []),
        "standards": [{"id": k, **v} for k, v in by_cat.items()],
        "rules": rule_status,
        "findings": findings,
    }
    return report


def render_text(report: dict) -> str:
    s = report["summary"]
    lines = []
    lines.append("=" * 72)
    lines.append(f" aiReview 本地代码审查报告  v{__version__}")
    lines.append("=" * 72)
    m = report["metadata"]
    lines.append(f" 项目     : {m.get('project', '-')}")
    lines.append(f" 模型     : {m.get('model', 'n/a')}")
    lines.append(f" 范围     : {m.get('scope_files', 0)} 文件 / {m.get('scope_loc', 0)} LOC")
    lines.append(f" 耗时     : {m.get('duration_ms', 0)} ms")
    lines.append(f" 规则总数 : {s['total_rules']}  (通过 {s['rules_passed']} / 命中 {s['rules_failed']})")
    lines.append(f" 问题总数 : {s['total_findings']}")
    fb = s["findings_by_severity"]
    lines.append(f" 严重分布 : " + "  ".join(f"{k}={fb[k]}" for k in SEV_ORDER))
    lines.append("-" * 72)
    if report["skipped"]:
        lines.append(" 跳过的规则（需本地模型或依赖不可用）:")
        for sk in report["skipped"]:
            lines.append(f"   - {sk['rule_id']}: {sk['reason']}")
        lines.append("-" * 72)
    if not report["findings"]:
        lines.append(" ✅ 未在启用规则下发现问题。")
    for f in report["findings"]:
        lines.append(f" [{f['severity'].upper():>8}] {f['rule_id']}  ({f['rule_name']})")
        lines.append(f"            {f['file']}:{f['line']}:{f['column']}")
        lines.append(f"            说明: {f['message']}")
        if f.get("snippet"):
            lines.append(f"            代码: {f['snippet'][:120]}")
        if f.get("suggestion"):
            lines.append(f"            建议: {f['suggestion']}")
        lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def render_sarif(report: dict) -> str:
    rules_meta = {r["id"]: r for r in report["rules"]}
    sarif_rules = []
    for rid, rm in rules_meta.items():
        sarif_rules.append({
            "id": rid,
            "name": rm.get("name", rid),
            "shortDescription": {"text": rm.get("name", rid)},
            "fullDescription": {"text": rm.get("description", "")},
            "defaultConfiguration": {"level": _sarif_level(rm.get("severity", "medium"))},
        })
    results = []
    for f in report["findings"]:
        results.append({
            "ruleId": f["rule_id"],
            "level": _sarif_level(f["severity"]),
            "message": {"text": f["message"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f["file"]},
                    "region": {"startLine": f["line"], "startColumn": f.get("column", 1),
                               "endLine": f.get("end_line", f["line"])},
                }
            }],
        })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "aiReview Code Review", "version": __version__,
                                "rules": sarif_rules}},
            "results": results,
        }],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _sarif_level(sev: str) -> str:
    return {"critical": "error", "high": "error", "medium": "warning",
            "low": "warning", "info": "note"}.get(sev, "warning")


def gate_exit(report: dict, fail_on: tuple = GATE_DEFAULT) -> int:
    fb = report["summary"]["findings_by_severity"]
    for sev in fail_on:
        if fb.get(sev, 0) > 0:
            return 1
    return 0
