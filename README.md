# GStack 本地 AI 代码审查系统

一个**全程本地、规则驱动、可理解多模块逻辑**的静态代码审查系统。所有模型推理都发生在你本机，无任何云端依赖。

## 功能特性

- **全程本地**：模型推理只连本机 OpenAI 兼容端点（LM Studio / Ollama / llama.cpp / vLLM），不上云。
- **规则驱动**：每条规则自带严重级 / 描述 / 修复建议；命中即报、未命中即过；报告按规则维度聚合，支持 Text / JSON / SARIF，并带严重级门禁退出码。
- **多语言**：C / C++ 走 tree-sitter AST（精确）；Python / JavaScript / TypeScript 走正则规则 + 调用图归属。
- **跨模块理解**：解析出符号表 + 调用图 + 依赖图并落盘 SQLite（hash 增量），支撑跨文件 / 跨模块分析（如信号处理函数调用链展开）。
- **自然语言规则**：一句话（中/英）由本地模型编译为结构化规则（`ast_pattern` / `regex` / `graph` / `semantic`），带 schema 校验与低置信人审闸口。
- **业务逻辑模式**：`logic-only` 跳过语法/正则类检查，仅做"整体推导业务逻辑 → 逐模块回归找矛盾"。
- **内置标准包**：CERT C、CERT C++、Python、JavaScript/TS、logic；均可在 Web 界面可视化启用 / 卸载，也可安装自定义包。
- **零额外依赖的 Web UI**：基于 Python 标准库 `http.server`，配置 / 运行 / 规则管理 / 结果展示一站式，纯本地监听。

## 对应需求的实现

| 需求 | 实现 |
|------|------|
| 1. 本地 AI 模型 | `gstack_review/llm/`：统一客户端抽象，默认对接 **LM Studio**（OpenAI 兼容 `/v1/chat/completions`），同样支持 Ollama / llama.cpp / vLLM，带健康探测与 fallback。 |
| 2. 大项目 / 多模块 / 理解模块间逻辑 | `gstack_review/parser/`：tree-sitter 解析 C/C++ → 符号表 + **调用图 + 依赖图**，落盘 SQLite（hash 增量）。图查询支撑跨文件/跨模块分析（如信号处理函数调用链展开）。 |
| 3. 自然语言自定义规则 | `gstack_review/rules/nl_compiler.py`：本地 LLM 把一句中文/英文描述编译成结构化规则（`ast_pattern` / `regex` / `graph` / `semantic`），带 schema 校验与低置信人审闸口。 |
| 4. 规则驱动检查 + 规则维度报告 | `gstack_review/rules/engine.py` + `gstack_review/reporter/`：逐规则匹配，命中即报；报告按规则维度聚合，支持 文本 / JSON / SARIF，严重级门禁退出码。 |
| 5. 内置检测标准 | `gstack_review/rules/standards/`：内置 **CERT C**（可自动化高价值子集，含 AST/正则/跨模块图/语义四类规则）、**CERT C++** 脚手架包、**Python**、**JavaScript / TypeScript** 通用反模式与安全隐患包（正则类型），以及 **logic（业务逻辑一致性 / 跨模块矛盾）** 分析包（基于本地大模型，先推导各模块业务逻辑与不变量，再从调用图逐对回归检测矛盾）。所有包可裁剪/扩展/卸载，也可在 Web 界面安装自定义包。 |

## 安装

```bash
pip install -r requirements.txt
# 可选但推荐（启用 AST 级精确匹配 ast_pattern 规则）：
pip install tree-sitter tree-sitter-c tree-sitter-cpp
```

> 未安装 tree-sitter 时系统自动降级为正则兜底，仍能运行（`ast_pattern` 规则会被跳过并在报告中说明）。

## 目录结构

```
gstack_review/
├── cli.py                 # 命令行入口（review / rule / standard / config / models / web）
├── config.py              # 配置模型与示例配置
├── llm/                   # OpenAI 兼容本地模型客户端（健康探测 / JSON 自适应）
├── parser/                # tree-sitter 解析、索引（SQLite）、调用图 / 依赖图
├── rules/
│   ├── engine.py          # 规则引擎：ast_pattern / regex / graph / semantic
│   ├── nl_compiler.py     # 自然语言 → 结构化规则
│   └── standards/         # 内置标准包：cert-c / cert-cpp / python / javascript / logic
├── reporter/              # 报告渲染：text / json / sarif + 门禁
└── web/                   # 本地 Web UI（http.server + static/index.html）
tests/integration_test.py  # 集成测试
sample_project/            # C 示例（信号安全 / 空指针等）
sample_pyjs/               # Python / JavaScript 示例
requirements.txt
```

## 快速开始

### 本地模型（OpenAI 兼容 /v1）
本系统对接任何 OpenAI 兼容端点，**默认指向 LM Studio**（`http://localhost:1234/v1`）。
也可用 Ollama / llama.cpp / vLLM，只需改 `--endpoint` / `--model`。

- **LM Studio**：在 LM Studio 里 Load 一个代码模型（如 `qwen2.5-coder-14b`），并开启 "Start Server"（默认 1234 端口）。
- **Ollama**：`ollama serve` 并 `ollama pull qwen2.5-coder:7b`，端点为 `http://localhost:11434/v1`。

```bash
# 1) 生成示例配置（默认已按 LM Studio 填好；换 Ollama 见注释）
python -m gstack_review.cli config init --output .gstack.yaml

# 2) 确认端点与可用模型
python -m gstack_review.cli models

# 3) 运行审查（结构化规则纯离线；语义/自然语言规则会调用本地模型）
python -m gstack_review.cli review --path ./your_project --format text
python -m gstack_review.cli review --path ./your_project --format sarif --report report.sarif.json

# 4) 用自然语言新增规则（调用本地模型编译为结构化规则）
python -m gstack_review.cli rule add "禁止在循环里调用 malloc" --model qwen/qwen2.5-coder-14b

# 5) 查看内置标准包与规则
python -m gstack_review.cli standard list
python -m gstack_review.cli rule list

# 6) 跳过需模型的语义规则（纯静态离线）
python -m gstack_review.cli review --path ./your_project --only-structured
```

> 若 LM Studio 的模型 id 含 `/`（如 `qwen/qwen2.5-coder-14b`），`--model` 直接传该完整 id 即可。
> JSON 输出模式已对 LM Studio（仅支持 `json_schema`）、OpenAI、Ollama 做了自适应兼容。

## Web 界面（可视化配置 + 执行 + 结果）

除命令行外，本系统内置一个**纯本地**网页界面（Python 标准库 `http.server`，零额外依赖，不上云），把全部 CLI 能力可视化：配置、运行审查、规则管理、结果友好展示。

```bash
# 启动（默认 127.0.0.1:8765，--root 为待审查项目/配置所在根目录）
python -m gstack_review.cli web --root .
# 或等价地：
python -m gstack_review.web --root . --port 8765

# 浏览器打开：http://127.0.0.1:8765/
```

界面分 5 个标签页：
- **仪表盘**：模型连通状态、启用标准包数、规则数、上次结果摘要、一键运行。
- **配置**：端点 / 模型 / 扫描范围 / 高级参数全部可编辑保存；「测试连接」探测本地模型；「生成示例配置」。
- **标准包**：勾选启用 / 停用 CERT C / CERT C++ / Python / JavaScript / **logic（跨模块业务逻辑矛盾）** 等内置包（启用 Python / JS 包时会自动把对应语言加入解析范围）；非内置包可一键**卸载**；并提供「安装新标准包」卡片，粘贴 YAML（含 `id` / `manifest` / `rules`）即可安装自定义规则集。
- **规则管理**：用自然语言描述规则 → 调用本地模型编译 → YAML 预览 → 保存为用户规则；内置/用户规则列表、启用开关、删除；离线时降级为可编辑模板。
- **运行与结果**：选择本地目录（内置目录浏览器）、选格式与标准包、运行审查；长任务轮询进度；结果含门禁横幅、严重度卡片、按严重度分组可展开的发现项（代码片段 + 修复建议 + 置信度）、跳过规则提示，以及 JSON / SARIF / Text 下载。

> 界面与命令行共用同一份 `.gstack.yaml` 与用户规则目录，二者配置互通。

## 规则 Schema（自定义 / 标准包通用）

```yaml
id: cert-c-fio34
name: "Do not use gets()"
severity: high            # critical|high|medium|low|info
category: cert-c
type: ast_pattern         # ast_pattern | regex | graph | semantic
language: c               # c | cpp | python | javascript | multi
description: "..."
fix_suggestion: "..."
enabled: true
match:
  # ast_pattern: tree-sitter 查询，必须捕获 @node（被报告节点）与 @name（消息占位）
  query: '(call_expression function: (identifier) @name (#eq? @name "gets")) @node'
  message: "使用了不安全的 gets()（FIO34-C）"
  # regex: 文本正则，可选 file_scope；消息可用 {{match}}
  # pattern: "strtok\\s*\\("
  # graph: 跨模块图分析器（signal_safety | logic_consistency）
  # analyzer: signal_safety
  # analyzer: logic_consistency
  # context: { max_pairs: 40, module_src_limit: 8000 }
  # semantic: 需本地模型，按上下文切片做语义判定
  # prompt: "..."
  # context: { candidate_regex: "malloc|fopen", limit: 30 }
```

## 扩展

- **新增语言**：装对应 `tree-sitter-<lang>`，在 `parser/tslang.py` 注册解析器与符号抽取。
- **新增标准包（文件方式）**：在 `gstack_review/rules/standards/<pack_id>/` 下放 `manifest.yaml` + `rules/*.yaml`，即被 `standard list` / `review --standard` 识别。
- **新增规则类型**：在 `rules/engine.py` 实现新的 Matcher，并在 schema 的 `type` 枚举里注册。

### 安装 / 卸载标准包

除文件方式外，Web 界面「标准包」标签页提供「安装新标准包」卡片：粘贴一段 YAML（顶层含 `id` / `manifest` / `rules`）即可安装；非内置包支持一键卸载（删除其规则文件并从 `enabled_standards` 移除）。

打包格式（与 `gstack_review/rules/standards/<pack_id>/` 目录结构等价）：

```yaml
id: my-corp           # 包 id，仅允许字母/数字/_/./-，内置 id（cert-c/cert-cpp/python/javascript）不可覆盖
manifest:
  name: "My Corp Rules"
  version: "0.1.0"
  languages: [python]          # 启用时 Web 界面会自动加入解析范围；CLI 需在 config 显式配置 languages
rules:
  - id: my-rule-1
    name: "禁止明文密码"
    severity: high             # critical|high|medium|low|info
    category: my-corp
    type: regex                # 非 C/C++ 语言建议用 regex
    language: python
    enabled: true
    match:
      pattern: 'password\s*=\s*["''][^"'']+["'']'
      message: "检测到疑似硬编码明文密码"
      file_scope: {language: python, path_glob: "*.py"}
```

> **语言支持说明**：当前 tree-sitter 仅内置 **C / C++**，因此 `ast_pattern`（AST 级精确匹配）与 `semantic`（需本地模型）规则只对 C/C++ 生效；Python / JavaScript / TypeScript 文件已纳入索引，但其规则须以 `regex` 类型编写。后续接入对应 `tree-sitter-<lang>` 后，AST 级规则即可扩展到这些语言。

> **跨模块图说明**：`graph` 分析器依赖调用图。C/C++ 的调用图由 tree-sitter 构建；Python / JavaScript / TypeScript 由正则回退抽取，但已把调用归属到所在函数（此前 `caller` 恒为空导致图不可用），因此逻辑类分析对 Python/JS 也可用。

## 业务逻辑 / 跨模块矛盾检查（logic-only 模式）

除"语法/结构类"检查外，系统支持**只看业务逻辑**的模式：

- **Web**：运行页勾选「仅业务逻辑（跳过语法/正则规则）」。
- **CLI**：`python -m gstack_review.cli review --path ./proj --logic-only`。
- 该模式只保留 `type ∈ {graph, semantic}` 的规则，丢弃 `regex` / `ast_pattern`（语法/文本类）。

内置 `logic` 标准包提供 `logic-consistency` 规则（`graph` 类型，`analyzer=logic_consistency`），采用两趟流程：

1. **整体推导**：按模块（目录）把源码送入本地大模型，产出该模块的业务逻辑摘要与**不变量/契约**（前置后置条件、错误处理策略、状态机规则、对调用方/被调方的假设）。
2. **逐模块回归**：沿跨模块调用图的每条边，把"A 的契约 + A/B 的实际源码"交给模型判定 B 是否违背 A（或反之）；同时对每个模块做内部自洽检查。命中即产出 `logic-consistency` 发现项（带严重级与置信度）。

> 该分析需要本地模型在线；离线时以 `model_offline` 跳过并在界面提示。`max_pairs` 控制跨模块比对对数（默认 40），`module_src_limit` 控制每模块送入模型的最大字符数（默认 8000）。

## 示例项目

仓库自带两个示例，可直接跑起来看效果：

```bash
# C 示例（CERT / 信号安全 / 跨模块调用链）
python -m gstack_review.cli review --path sample_project --format text

# Python / JavaScript 示例（Python/JS 反模式与安全隐患规则）
python -m gstack_review.cli review --path sample_pyjs --standard python --standard javascript --format text

# 仅业务逻辑（跨模块矛盾回归，需本地模型在线）
python -m gstack_review.cli review --path sample_project --standard logic --logic-only --format text
```

## 测试

```bash
python -m tests.integration_test
```

## 已知局限

- **CERT 覆盖率**：CERT C 共 100+ 条，本包仅纳入可静态自动化的高价值子集；依赖运行行为/深层语义的规则以 `semantic` 类型标注，需本地模型参与判定，未做全量覆盖。
- **跨文件符号解析精度**：基于 include + 命名约定的多策略解析，tree-sitter 下可能存在少量漏/误；对 C/C++ 精度敏感场景可后续接入 clang。
- **本地小模型准确性**：NL→规则 与 semantic 判定受模型能力影响，已用 schema 校验 + 低置信人审闸口兜底；关键规则建议优先用 `ast_pattern`/`regex`。
