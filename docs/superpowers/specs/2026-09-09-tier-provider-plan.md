# 按 Tier 分别选择 LLM Provider

## Summary

扩展 `config.yaml`，允许每个 `llm.tiers.<tier>` 单独指定 provider，并保持旧配置完全兼容：

```yaml
llm:
  provider: pi

  tiers:
    strong:
      provider: codex
      model: gpt-5.6-sol
      cli_path: C:\Program Files\nodejs\codex.cmd

    cheap:
      provider: deepseek
      model: deepseek-v4-flash
      api_key_env: DEEPSEEK_API_KEY

    fast:
      provider: pi
      model: cdxpp-gpt/gpt-5.6-terra
```

未填写 `tier.provider` 时继承 `llm.provider`。

## Implementation Changes

- 扩展 `TierConfig`，增加以下可选字段：
  - `provider`
  - `base_url`
  - `api_key_env`
  - `cli_path`
  - `reasoning_style`
  - 现有 `model`、`options` 保持不变
- 明确定义配置优先级：
  - tier 字段
  - `llm` 顶层字段
  - provider 内置默认值
- 保持 `options` 由具体 provider 解析，不把 provider 专属参数提升到公共配置模型。

- 新增一个路由型 `LLMClient`：
  - 对每个 provider 创建独立子 client。
  - 根据请求的 tier 选择对应 provider。
  - 继续使用现有回退链 `fast -> cheap -> strong`。
  - 回退时使用最终选中的 tier 的完整配置，因此允许跨 provider 回退。
  - 单一 provider 且没有 tier provider 覆盖时，继续返回现有具体 client，保持现有类型和行为兼容。
  - provider client 增加“允许缺少 strong tier”的内部构造模式，使某个 provider 只负责 `cheap` 或 `fast` 时无需伪造配置。

- 路由型 client 负责聚合现有运行时能力：
  - 转发 `set_event_sink` 和 `set_status_listener`。
  - 保持 retry 事件中的真实 provider 名称。
  - 为状态事件增加可选 provider 信息，不破坏已有事件构造和消费者。
  - 保持 `complete`、`complete_json`、`validate_credentials` 的公共接口。

- 用量账本保持兼容：
  - 继续保留现有 `by_tier` 聚合结果。
  - 新增 `by_provider` 维度，用于区分同一 tier 在不同 provider 上的消耗。
  - `usage_delta`、`merge_usage_summaries`、运行状态恢复均兼容缺少 `by_provider` 的旧账本。
  - 指标中的配置摘要记录每个 tier 的最终 provider 和连接配置，但仍脱敏 API key、URL 凭据等敏感信息。

- CLI 凭据校验按入口需要的 tier 集合执行：
  - `translate` / `prepare`：校验可能使用的 `strong`、`cheap`、`fast`，以及开启 Review 时的 `review_agent_tier`。
  - `review`：校验 `cheap` 和 `review_agent_tier`。
  - provider 只在对应 tier 实际路由到它时校验。
  - `assemble`、`report`、`status` 和 glossary 查询继续跳过模型凭据校验。
  - 保留首次真实调用时的二次校验，防止配置或环境在启动后发生变化。

- 更新以下文档和示例：
  - `config.yaml`：加入 tier provider 和 tier 级连接字段示例，不改写用户当前实际模型选择。
  - `trans_novel/config.py` 默认 YAML。
  - `docs/configuration.md`
  - `docs/zh/configuration.md`

## Tests

新增或调整测试覆盖：

- tier provider 配置解析、全局 provider 继承和字段优先级。
- tier 级 `base_url`、`api_key_env`、`cli_path`、`reasoning_style` 的继承与覆盖。
- 混合 provider 路由到正确子 client。
- `fast -> cheap -> strong` 跨 provider 回退。
- 某 provider 只负责一个 tier 时仍能正常构造。
- 单 provider 旧配置的 client 类型和行为不变。
- 混合 provider 下事件、状态监听和 retry 日志包含正确 provider。
- `by_tier` 与新增 `by_provider` 用量统计、增量合并及旧账本兼容。
- CLI 按命令校验实际可能使用的 provider，且 assemble/report 等离线命令不触发校验。
- 运行配置 fingerprint 会随 tier provider 或 tier 连接字段变化。
- 运行现有配置、LLM、CLI、usage、request-status、架构边界测试。

验证命令：

```powershell
rtk uv run pytest -q tests/test_config.py tests/test_llm.py tests/test_llm_gemini.py tests/test_usage.py tests/test_cli.py tests/test_request_status.py
rtk uv run ruff check .
rtk uv run ruff format --check .
rtk git diff --check
```

## Assumptions

- `llm.provider` 保留并继续作为旧配置和未指定 tier 的默认 provider。
- tier 级连接字段采用与 `llm` 顶层相同的扁平命名。
- provider-specific 参数继续放在 `options` 中。
- 缺失 tier 仍按现有回退链处理；回退到哪个 tier，就使用哪个 tier 的 provider。
- 本次不引入独立的 `llm.providers` profile 层，避免无必要的配置迁移。
- 用户当前已修改的 `config.yaml` 活跃配置值保留，只补充兼容的新字段说明和示例。
