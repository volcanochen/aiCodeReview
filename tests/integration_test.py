"""Integration tests for GStack local code review.

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

from gstack_review.parser.index import Index
from gstack_review.rules.engine import RuleEngine
from gstack_review.rules.nl_compiler import compile_nl
from gstack_review.rules import validate_rule


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
    from gstack_review.rules import load_pack_by_id
    rules, _ = load_pack_by_id("cert-c", os.path.join(here, "gstack_review", "rules", "standards"))
    res = eng.run(rules)
    ids = {f["rule_id"] for f in res["findings"]}
    assert "cert-c-fio34" in ids, "gets() should be detected"
    assert "cert-c-con33" in ids, "cross-module signal safety should fire"
    idx.close()
    print("[ok] review smoke: files=%d findings=%d" % (stats.files, len(res["findings"])))


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


if __name__ == "__main__":
    test_review_smoke()
    test_nl_compile()
    test_semantic()
    test_index_scope_pruning()
    print("\nALL INTEGRATION TESTS PASSED")
