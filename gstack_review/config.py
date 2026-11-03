"""Configuration layer.

Reads `.gstack.yaml` (or `--config`) and merges CLI overrides into a single
``Config`` object consumed by every other layer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml

# Defaults target LM Studio (OpenAI-compatible /v1). Ollama/llama.cpp/vLLM are
# equally supported — just point --endpoint / --model at them.
DEFAULT_ENDPOINT = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen/qwen2.5-coder-14b"


@dataclass
class Config:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    api_key: str = "ollama"  # OpenAI-compatible clients need a non-empty key; Ollama ignores it.
    languages: list[str] = field(default_factory=lambda: ["c", "cpp", "python", "javascript"])
    enabled_standards: list[str] = field(default_factory=lambda: ["cert-c"])
    scope: dict[str, Any] = field(default_factory=lambda: {"include": ["**/*.c", "**/*.cpp", "**/*.h", "**/*.hpp", "**/*.py", "**/*.js", "**/*.ts"], "exclude": []})
    perf_budget: dict[str, Any] = field(default_factory=lambda: {"max_files": 200000, "max_loc": 200_000_000})
    user_rules_dir: str = ".gstack/rules"
    index_db: str = ".gstack/index.db"
    semantic_timeout_ms: int = 30000
    only_structured: bool = False  # when True, skip semantic (LLM) rules entirely
    logic_only: bool = False      # when True, keep only logic-class rules (graph/semantic), skip syntax-class (regex/ast_pattern)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    """Load config from a YAML file and apply overrides."""
    data: dict[str, Any] = {}
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    # Environment variables take priority for the most sensitive knobs.
    if os.environ.get("GSTACK_ENDPOINT"):
        data["endpoint"] = os.environ["GSTACK_ENDPOINT"]
    if os.environ.get("GSTACK_MODEL"):
        data["model"] = os.environ["GSTACK_MODEL"]

    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})

    # Merge CLI overrides (only non-None values).
    if overrides:
        for k, v in overrides.items():
            if v is not None and k in Config.__dataclass_fields__:
                setattr(cfg, k, v)
    return cfg


def example_config_text() -> str:
    return """# .gstack.yaml — GStack code review configuration
# 本地大模型端点（OpenAI 兼容 /v1）。以下为 LM Studio 默认值：
endpoint: http://localhost:1234/v1
model: qwen/qwen2.5-coder-14b        # LM Studio 中的模型 id（用 `gstack models` 查看）
# 若改用 Ollama，改为：
# endpoint: http://localhost:11434/v1
# model: qwen2.5-coder:7b
api_key: lmstudio                    # 本地服务通常忽略，填任意非空值即可
languages: [c, cpp, python, javascript]   # languages to parse
enabled_standards: [cert-c]           # built-in standard packs to load (cert-c, cert-cpp, python, javascript)
scope:
  include: ["**/*.c", "**/*.cpp", "**/*.h", "**/*.hpp", "**/*.py", "**/*.js", "**/*.ts"]
  exclude: ["**/third_party/**", "**/build/**", "**/node_modules/**"]
perf_budget:
  max_files: 200000
  max_loc: 200000000
user_rules_dir: .gstack/rules
index_db: .gstack/index.db
only_structured: false   # set true to skip LLM-based semantic rules
logic_only: false       # set true to skip syntax-class rules (regex/ast_pattern), keep logic-class (graph/semantic)
"""
