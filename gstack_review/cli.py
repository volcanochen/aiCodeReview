"""GStack code review CLI.

Subcommands:
  review        Run the rule-based review over a path.
  rule add      Compile a natural-language rule via the local LLM (or emit a template).
  rule list     List all loaded rules.
  standard list List installed standard packs.
  config init   Write an example .gstack.yaml.
  models        Probe the local LLM endpoint.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from .config import load_config, example_config_text, DEFAULT_ENDPOINT, DEFAULT_MODEL
from .llm import LLMClient
from .parser.index import Index
from .rules import (
    load_pack_by_id, load_user_rules_dir as _lur, validate_rule, RuleValidationError,
    list_packs,
)
from .rules.nl_compiler import compile_nl
from .rules.engine import RuleEngine
from .reporter import build_report, render_text, render_json, render_sarif, gate_exit
from .web.server import WebServer

STANDARDS_ROOT = os.path.join(os.path.dirname(__file__), "rules", "standards")


# --------------------------------------------------------------------------
def make_llm(cfg, *, allow_probe: bool = True) -> LLMClient | None:
    if cfg.only_structured:
        return None
    client = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=60000)
    if not allow_probe:
        return client
    # Quick liveness probe with a short timeout so we don't hang when offline.
    probe = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=4000)
    try:
        ok = probe.healthcheck()
    except Exception:
        ok = False
    if not ok:
        print(f"[warn] 本地模型端点 {cfg.endpoint} 不可达，语义类(semantic)规则将被跳过。", file=sys.stderr)
        return None
    return client


def load_all_rules(cfg) -> list:
    rules = []
    for sid in cfg.enabled_standards:
        try:
            rs, _ = load_pack_by_id(sid, STANDARDS_ROOT)
            rules.extend(rs)
        except FileNotFoundError as e:
            print(f"[warn] 标准包未找到: {e}", file=sys.stderr)
    try:
        rules.extend(_lur(cfg.user_rules_dir))
    except Exception as e:
        print(f"[warn] 用户规则目录读取失败: {e}", file=sys.stderr)
    return rules


# --------------------------------------------------------------------------
def cmd_review(args, cfg):
    root = args.path or "."
    index = Index(cfg.index_db)
    print(f"[index] 正在构建索引: {root}", file=sys.stderr)
    stats = index.build([root], cfg.languages)
    print(f"[index] 完成: {stats.files} 文件 / {stats.loc} LOC / 耗时 {stats.duration_ms}ms", file=sys.stderr)

    rules = load_all_rules(cfg)
    if getattr(args, "logic_only", False):
        before = len(rules)
        rules = [r for r in rules if r.type in ("graph", "semantic")]
        print(f"[logic-only] 已过滤语法/正则类规则，保留 {len(rules)}/{before} 条逻辑类规则", file=sys.stderr)
    print(f"[rules] 加载 {len(rules)} 条规则", file=sys.stderr)

    llm = make_llm(cfg)
    engine = RuleEngine(index, llm)
    t0 = time.time()
    result = engine.run(rules)
    elapsed = int((time.time() - t0) * 1000)

    metadata = {
        "project": os.path.basename(os.path.abspath(root)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": cfg.model if llm else "n/a (local model offline)",
        "scope_files": stats.files,
        "scope_loc": stats.loc,
        "duration_ms": elapsed,
        "standards": cfg.enabled_standards,
    }
    report = build_report(metadata, result, rules)

    if args.format == "json":
        out = render_json(report)
    elif args.format == "sarif":
        out = render_sarif(report)
    else:
        out = render_text(report)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"[report] 已写入 {args.report}", file=sys.stderr)
    else:
        print(out)

    index.close()
    code = gate_exit(report)
    sys.exit(code)


def cmd_rule_add(args, cfg):
    text = args.text
    llm = make_llm(cfg, allow_probe=True)
    res = compile_nl(text, llm, category="nl-custom")
    os.makedirs(cfg.user_rules_dir, exist_ok=True)
    if res.ok and res.rule:
        rid = res.rule["id"]
        path = os.path.join(cfg.user_rules_dir, f"{rid}.yaml")
        import yaml
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(res.rule, fh, allow_unicode=True, sort_keys=False)
        print(f"[ok] 规则已编译并保存: {path}")
        if res.needs_review:
            print(f"[注意] 模型置信度 {res.confidence:.2f} 偏低，请人工复核 {path} 后再启用。")
    else:
        print(f"[warn] {res.error}")
        if res.template:
            tid = f"nl-{int(time.time())}"
            path = os.path.join(cfg.user_rules_dir, f"{tid}.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(res.template)
            print(f"[模板] 已生成可编辑的规则模板: {path}")


def cmd_rule_list(args, cfg):
    rules = load_all_rules(cfg)
    for r in rules:
        flag = "" if r.enabled else " (disabled)"
        print(f"{r.id:<24} [{r.severity:<8}] {r.type:<11} {r.name}{flag}")
    print(f"\n共 {len(rules)} 条规则。")


def cmd_standard_list(args, cfg):
    packs = list_packs(STANDARDS_ROOT)
    if not packs:
        print("未发现标准包。")
        return
    for p in packs:
        print(f"{p.get('id'):<14} {p.get('name', '')}  (v{p.get('version','?')})")
        if p.get("coverage_note"):
            print(f"    {p['coverage_note']}")


def cmd_config_init(args, cfg):
    path = args.output or ".gstack.yaml"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(example_config_text())
    print(f"[ok] 已生成示例配置: {path}")


def cmd_models(args, cfg):
    client = LLMClient(cfg.endpoint, cfg.model, cfg.api_key, timeout_ms=8000)
    if client.healthcheck():
        models = client.list_models()
        print(f"[ok] 端点 {cfg.endpoint} 可用。")
        print("可用模型:" if models else "（端点未返回模型列表，但仍可用）")
        for m in models:
            print(f"  - {m}")
    else:
        print(f"[fail] 端点 {cfg.endpoint} 不可达。请确认本地推理服务已启动（如 `ollama serve`）。")


def cmd_web(args, cfg):
    server = WebServer(
        host=getattr(args, "host", "127.0.0.1"),
        port=getattr(args, "port", 8765),
        root=getattr(args, "root", None),
        config=args.config,
    )
    server.run()


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    # Shared options usable before OR after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=None, help="覆盖模型 id（需与端点中的模型一致）")
    common.add_argument("--endpoint", default=None, help="覆盖本地模型端点（OpenAI 兼容 /v1）")
    common.add_argument("--only-structured", action="store_true", help="跳过语义(LLM)规则")
    common.add_argument("--config", default=None, help="配置文件路径")

    p = argparse.ArgumentParser(prog="gstack", parents=[common], description="本地 AI 代码审查系统")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("review", parents=[common], help="运行规则审查")
    pr.add_argument("--path", default=".")
    pr.add_argument("--format", choices=["text", "json", "sarif"], default="text")
    pr.add_argument("--report", default=None, help="将报告写入文件")
    pr.add_argument("--standard", action="append", help="覆盖启用的标准包（可多次）")
    pr.add_argument("--logic-only", action="store_true",
                    help="仅运行业务逻辑类规则（graph/semantic），跳过语法/正则类（regex/ast_pattern）")
    pr.set_defaults(func=cmd_review)

    ru = sub.add_parser("rule", parents=[common], help="规则管理")
    ru_sub = ru.add_subparsers(dest="rule_cmd", required=True)
    ra = ru_sub.add_parser("add", parents=[common], help="用自然语言新增规则")
    ra.add_argument("text", help="自然语言规则描述")
    ra.set_defaults(func=cmd_rule_add)
    rl = ru_sub.add_parser("list", parents=[common], help="列出已加载规则")
    rl.set_defaults(func=cmd_rule_list)

    st = sub.add_parser("standard", parents=[common], help="标准包管理")
    st_sub = st.add_subparsers(dest="std_cmd", required=True)
    sl = st_sub.add_parser("list", parents=[common], help="列出标准包")
    sl.set_defaults(func=cmd_standard_list)

    ci = sub.add_parser("config", parents=[common], help="配置")
    ci_sub = ci.add_subparsers(dest="cfg_cmd", required=True)
    ci_init = ci_sub.add_parser("init", parents=[common], help="生成示例配置")
    ci_init.add_argument("--output", default=".gstack.yaml")
    ci_init.set_defaults(func=cmd_config_init)

    md = sub.add_parser("models", parents=[common], help="探测本地模型端点")
    md.set_defaults(func=cmd_models)

    wb = sub.add_parser("web", parents=[common], help="启动本地 Web 审查界面")
    wb.add_argument("--host", default="127.0.0.1", help="监听地址")
    wb.add_argument("--port", type=int, default=8765, help="监听端口")
    wb.add_argument("--root", default=None, help="工作根目录（配置与待审查项目所在处）")
    wb.set_defaults(func=cmd_web)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {}
    if getattr(args, "standard", None):
        overrides["enabled_standards"] = args.standard
    if getattr(args, "model", None):
        overrides["model"] = args.model
    if getattr(args, "endpoint", None):
        overrides["endpoint"] = args.endpoint
    if getattr(args, "only_structured", False):
        overrides["only_structured"] = True
    cfg = load_config(getattr(args, "config", None), overrides)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
