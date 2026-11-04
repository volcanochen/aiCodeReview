"""Integration tests for aiReview local code review.

Covers (without requiring a real local model):
  - index build + cross-module call graph
  - ast_pattern / regex / graph matchers
  - NL -> rule compiler (with a fake LLM)
  - semantic matcher (with a fake LLM)
Run:  python -m tests.integration_test
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aireview.parser.index import Index
from aireview.rules.engine import RuleEngine
from aireview.rules.nl_compiler import compile_nl
from aireview.rules import validate_rule


class FakeLLM:
    """Stub LLM: returns a fixed rule for NL compile and a fixed verdict for semantic."""
    def __init__(self):
        self.semantic_calls = 0

    def chat_json(self, messages, response_schema=None, temperature=0.0):
        last = messages[-1]["content"]
        if "编译成规则" in last or "编译" in last:
            # NL -> rule: a regex rule banning strcpy (present in the sample)
            return {
                "id": "nl-no-strcpy", "name": "不要使用 strcpy",
                "severity": "medium", "category": "nl-custom", "type": "regex",
                "language": "c", "description": "由自然语言生成", "rationale": "",
                "fix_suggestion": "改用 strncpy 并补 \\0", "confidence": 0.9,
                "match": {"pattern": "strcpy\\s*\\(", "file_scope": {"language": "c"},
                          "message": "命中自定义规则：避免 strcpy ({{match}})"},
            }
        # semantic prompt
        self.semantic_calls += 1
        if "process" in last or "malloc" in last:
            return {"violation": True, "line": 2, "message": "在缺少空值检查的情况下解引用了指针", "confidence": 0.8}
        return {"violation": False}


def test_review_smoke():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(here, "sample_project")
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "index.db")
    idx = Index(db)
    stats = idx.build([sample], ["c", "cpp"])
    assert stats.files >= 7, stats.files
    # cross-module graph: handler -> process -> malloc/printf
    assert "process" in idx.callees_of("handler"), idx.callees_of("handler")
    eng = RuleEngine(idx, None)
    # load CERT C pack rules
    from aireview.rules import load_pack_by_id
    # 用纯静态子集跑，结果不依赖本地模型
    rules, _ = load_pack_by_id("cert-c", os.path.join(here, "aireview", "rules", "standards"),
                               subsets=["static"])
    res = eng.run(rules)
    ids = {f["rule_id"] for f in res["findings"]}
    assert "ENV33-C" in ids, "system() should be detected"
    assert "SIG30-C" in ids, "cross-module signal safety should fire"
    idx.close()
    print("[ok] review smoke: files=%d rules=%d findings=%d"
          % (stats.files, len(rules), len(res["findings"])))


def test_subsets():
    """子集机制：全集 / 默认子集 / 类别子集 / 空选择 的行为。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.join(here, "aireview", "rules", "standards")
    from aireview.rules import load_pack_by_id, load_standard_pack

    all_rules, manifest = load_standard_pack(os.path.join(root, "cert-c"))
    core, _ = load_pack_by_id("cert-c", root, subsets=["core"])
    static, _ = load_pack_by_id("cert-c", root, subsets=["static"])
    none_selected, _ = load_pack_by_id("cert-c", root, subsets=[])
    dflt, _ = load_pack_by_id("cert-c", root)          # 未指定 → default_subsets

    assert len(all_rules) == 122, len(all_rules)
    assert 0 < len(core) < len(all_rules), len(core)
    assert 0 < len(static) < len(all_rules), len(static)
    assert len(none_selected) == 0, "显式空列表应该一条都不跑"
    assert all(r.type != "semantic" for r in static), "纯静态子集不应含 semantic 规则"
    assert {r.id for r in dflt} == {r.id for r in core}, "默认应等于 default_subsets"
    assert manifest.get("default_subsets") == ["core"]
    # 每条规则都能归到某个子集
    from aireview.rules import rule_subsets
    for r in all_rules:
        assert rule_subsets(r, manifest), r.id
    print("[ok] subsets: all=%d core=%d static=%d empty=%d"
          % (len(all_rules), len(core), len(static), len(none_selected)))


def test_nl_compile():
    llm = FakeLLM()
    res = compile_nl("不要使用 strcat 拼接字符串", llm)
    assert res.ok, res.error
    rule = validate_rule(res.rule)
    assert rule.type == "regex"
    # run it against the sample to confirm it produces a finding
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(here, "sample_project")
    tmp = tempfile.mkdtemp()
    idx = Index(os.path.join(tmp, "index.db"))
    idx.build([sample], ["c", "cpp"])
    eng = RuleEngine(idx, None)
    out, _ = eng._run_one(rule)
    assert any("strcpy" in f["message"] for f in out), out
    idx.close()
    print("[ok] NL compile -> regex rule fires on sample")


def test_semantic():
    llm = FakeLLM()
    rule = validate_rule({
        "id": "sem-null", "name": "null deref", "severity": "high", "type": "semantic",
        "language": "c", "description": "x", "fix_suggestion": "y",
        "match": {"prompt": "是否解引用可能为 NULL 的指针", "context": {"candidate_regex": "malloc|process", "limit": 20}},
    })
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(here, "sample_project")
    tmp = tempfile.mkdtemp()
    idx = Index(os.path.join(tmp, "index.db"))
    idx.build([sample], ["c", "cpp"])
    eng = RuleEngine(idx, llm)
    out, applied = eng._run_one(rule)
    assert applied > 0, "semantic should have candidate functions"
    assert any(f["rule_id"] == "sem-null" for f in out), out
    idx.close()
    print("[ok] semantic matcher produced %d finding(s) via fake LLM" % len(out))


def test_index_scope_pruning():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(here, "sample_project")
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "index.db")
    idx = Index(db)
    idx.build([sample], ["c", "cpp"])
    assert len(idx.get_files()) == 7, len(idx.get_files())
    # Switching scope to a path with no C/C++ files must prune the old project.
    idx.build([tempfile.mkdtemp()], ["c", "cpp"])  # empty temp dir
    assert len(idx.get_files()) == 0, "stale files from other projects must be pruned"
    # A nonexistent path must also yield an empty index (no crash).
    idx.build(["/nonexistent/path/xyz"], ["c", "cpp"])
    assert len(idx.get_files()) == 0
    idx.close()
    print("[ok] index scope pruning: stale projects pruned, missing path safe")


def test_subset_overrides():
    """`PACK:ID` 写法解析 / 未知子集要出声 / 本次覆盖与配置的合并规则。"""
    import warnings
    from types import SimpleNamespace
    from aireview.rules import load_pack_by_id, parse_subset_specs
    from aireview.web.server import _merge_subsets

    # CLI --subset 与 Web ?subset= 共用同一套写法
    got = parse_subset_specs(["cert-c:core,str", "python:security", "cert-c:mem", "bad-no-colon"])
    assert got == {"cert-c": ["core", "str", "mem"], "python": ["security"]}, got
    assert parse_subset_specs(["cert-c:"]) == {"cert-c": []}, "空 id 列表要能表达「一条都不跑」"
    assert parse_subset_specs(None) == {}

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.join(here, "aireview", "rules", "standards")

    # 子集 id 是小写（str），tag 才是大写（STR）：写错必须告警，不能静默返回 0 条
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        bad, _ = load_pack_by_id("cert-c", root, subsets=["STR"])
    assert bad == [], "未知子集不应命中任何规则"
    assert any("没有这些子集" in str(x.message) for x in w), [str(x.message) for x in w]

    # 合并：本次覆盖优先，未提到的包沿用配置
    cfg = SimpleNamespace(enabled_subsets={"cert-c": ["core"], "python": ["all"]})
    assert _merge_subsets(cfg, None) == {"cert-c": ["core"], "python": ["all"]}
    merged = _merge_subsets(cfg, {"cert-c": ["str", "mem"], "logic": []})
    assert merged["cert-c"] == ["str", "mem"], merged
    assert merged["python"] == ["all"], "未覆盖的包必须保持配置值"
    assert merged["logic"] == [], "显式空列表表示该包一条都不跑"
    # 不改动入参（避免把一次运行的临时选择写回配置字典）
    assert cfg.enabled_subsets == {"cert-c": ["core"], "python": ["all"]}, cfg.enabled_subsets
    print("[ok] subset overrides: parse/merge/warn-on-typo")


if __name__ == "__main__":
    test_review_smoke()
    test_subsets()
    test_subset_overrides()
    test_nl_compile()
    test_semantic()
    test_index_scope_pruning()
    print("\nALL INTEGRATION TESTS PASSED")
