# 新增 Agy CLI Provider

## Summary

新增基于本机 `agy` 命令的 LLM provider，沿用 `codex`、`pi`、`codebuddy` 的 CLI 调用模式。Agy 使用本机登录态，不新增 API 依赖，也不要求 `base_url` 或 `api_key_env`。

## Implementation Changes

- 新增 `trans_novel/llm/providers/agy.py`：
  - 定义 `AgyTierOptions`，支持 `reasoning_effort: low|medium|high`，默认 `high`。
  - 不提供模型默认值；`llm.tiers.strong.model` 必须显式配置。
  - 将通用消息转换为带角色标记的纯文本 prompt，并在 `json_mode` 下附加严格 JSON 输出指令。
  - 使用 Agy 的 NDJSON 协议调用：
    - `agy --print= --input-format stream-json --output-format stream-json`
    - `--disable-slash-commands`
    - `--model <tier.model>`
    - `--effort <reasoning_effort>`
  - 通过 stdin 发送 `{"event":"user","message":...}`，避免长 prompt 的命令行长度和 Windows 参数解析问题。
  - 解析 `event: "result"`，校验 `status == "SUCCESS"` 和非空 `response`。
  - 将 Agy 用量字段映射为统一账本：
    - `input_tokens`
    - `output_tokens`
    - `cache_read_tokens`
    - `total_tokens`
  - 支持 CLI 路径解析：优先 tier `cli_path`，其次顶层 `llm.cli_path`，最后 `shutil.which("agy")`；缺失时给出包含 `llm.cli_path` 的可操作错误。
  - 复用现有请求状态、重试、超时、事件和用量记录机制。

- 更新 `trans_novel/llm/factory.py`：
  - 注册 `agy` provider。
  - 在未知 provider 的错误提示中加入 `agy`。

- 更新配置：
  - `trans_novel/config.py` 默认配置模板的 provider 列表加入 `agy`，并注明其为本机 Agy CLI。
  - 根目录 `config.yaml` 增加注释示例，展示 `provider: agy`、`cli_path`、strong/cheap/fast 模型和 `reasoning_effort`。
  - 保持当前实际启用的 provider 配置不变。

- 更新英文和中文配置文档：
  - 说明 Agy CLI 的安装/登录前提、模型必须显式配置、`cli_path` 覆盖方式和 `reasoning_effort` 可选值。
  - 在 tier provider 与 `cli_path` 支持列表中加入 `agy`。

## Test Plan

在 `tests/test_llm.py` 增加离线回归测试：

- prompt 构造及 JSON 模式指令。
- tier options 校验和 `low|medium|high` 映射。
- Agy NDJSON 输入消息结构和完整 CLI 参数。
- 成功 `result` 事件的文本与用量解析。
- 缓存 token、总 token 和缺失字段的计算。
- 非法 JSON、失败状态、缺失 result、空 response 的异常。
- `subprocess.run` 重试、超时、非零退出码和缺失 CLI 路径。
- provider factory 能构造 `AgyClient`。

验证命令：

```text
uv run pytest -q tests/test_llm.py tests/test_config.py
uv run ruff check .
uv run ruff format --check .
git diff --check
```

## Assumptions

- `agy` 指本机已安装并登录的 Agy CLI，而不是 HTTP 服务。
- 使用 Agy 已验证的 `stream-json` 输入/输出协议。
- 不猜测 Agy 的默认模型；用户必须在 `llm.tiers.strong.model` 中填写实际可用模型。
- Agy 当前没有与 CodeBuddy 等价的 `--tools none` 参数，因此 provider 只发送纯文本翻译请求并关闭 slash command 扩展，不改变模型本身的 agent 工具能力。
