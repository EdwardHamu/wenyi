# Pi Provider 代码审查结论

## 审查对象

- 目标文件：`trans_novel/llm/providers/pi.py`
- 参考文档：<https://pi.dev/docs/latest>
- 参考的 Pi 文档范围：`usage`、`json`、`rpc`、`sdk`、`custom-provider`
- 本机 Pi 版本：`0.84.3`

## 总体结论

`pi.py` 的主路径在当前本机 Pi `0.84.3` 和当前 `config.yaml` 配置下基本可以工作。正常成功响应且使用现有带 provider 前缀的模型配置时，当前实现能够调用 Pi CLI 并提取 assistant 文本及 usage。

本次已修复自动重试事件解析、`cacheWrite` token 统计和 Unicode 行分隔符解析三个问题。仍有默认模型选择、`max_tokens` 语义和扩展 provider 兼容性等待处理事项。

当前仍建议优先处理以下问题：

1. 内置默认模型使用完整的 `provider/model` 名称，避免 Pi 在多个 provider 注册同名模型时产生歧义。
2. 明确 `max_tokens` 在该 provider 中不受支持的行为，或通过 SDK/模型配置实现限制。

`--no-extensions` 是否保留则取决于是否需要使用 extension 注册的 provider。

## 发现的问题

### 已修复：解析器会错误地中断 Pi 自动重试

位置：`trans_novel/llm/providers/pi.py:224-308`

修复前，代码遇到第一条 assistant `message_end` 且 `stopReason == "error"` 时就立即抛出异常。但 Pi 的 RPC/JSON 事件流可能先输出一次失败的 assistant 消息，随后发出自动重试事件，最后才输出成功的 assistant 消息。

典型事件顺序可能是：

```text
message_end: assistant, stopReason=error
agent_end
auto_retry_start
message_end: assistant, stopReason=stop
agent_settled
```

当前实现会先遍历完整事件流，保存最后一条 assistant `message_end`，因此后续成功重试可以生效；只有最终消息的 `stopReason` 为 `error` 或 `aborted` 时才抛错。

修复前，代码还会将多个 assistant 消息的文本直接追加到 `text_parts`。当第一次响应因 `length` 或其他原因结束、随后 Pi 重试成功时，最终结果可能被拼接为：

```text
第一次被截断的文本
完整的第二次文本
```

当前实现改为只提取最后一条 assistant 消息的文本和 usage，避免把失败或被截断的中间响应拼入最终结果。回归测试：`test_parse_events_waits_for_successful_automatic_retry`。

修复采用的处理方式：

- 遍历完整 JSONL 事件流；
- 保存最后一个 assistant `message_end` 消息；
- 事件流结束后再判断最终消息的 `stopReason`；
- 只返回最后一条成功 assistant 消息的文本；
- 仅当最终消息的 `stopReason` 为 `error` 或 `aborted` 时抛出异常。

### P1：内置默认模型名在多 provider 环境中会产生歧义

位置：`trans_novel/llm/providers/pi.py:136-145`

内置默认档位使用了裸模型名，例如：

```text
gpt-5.6-terra
gpt-5.6-sol
gpt-5.6-luna
```

Pi 的 `--model` 会在可用 provider 中匹配模型。当前机器上相同模型名存在于多个 provider，因此执行：

```text
pi --model gpt-5.6-terra
```

会退出并报告：

```text
Model "gpt-5.6-terra" is ambiguous across providers
```

当前 `config.yaml` 使用的是完整模型名，例如：

```yaml
model: cdxpp-gpt/gpt-5.6-terra
model: a6-gpt/gpt-5.6-luna
```

所以当前配置能够避开这个问题，但内置默认值仍然不稳健。建议使用完整的 `provider/model` 名称，或者增加一个明确的 Pi provider 配置并通过 `--provider` 传递。

### 已修复：`cacheWrite` 被漏算

位置：`trans_novel/llm/providers/pi.py:162-189`

修复前的实现读取了 `input`、`output`、`cacheRead` 和 `totalTokens`，但没有读取 `cacheWrite`，并将统计逻辑写成了：

```python
cache_miss_tokens = input_tokens
prompt_tokens = input_tokens + cache_read_tokens
```

Pi usage 中的输入相关 token 包括 `input`、`cacheRead` 和 `cacheWrite`。修复采用：

```python
cache_write_tokens = read_usage_int(usage, "cacheWrite")
cache_hit_tokens = read_usage_int(usage, "cacheRead")
cache_miss_tokens = input_tokens + cache_write_tokens
prompt_tokens = cache_miss_tokens + cache_hit_tokens
```

例如 usage 为：

```json
{
  "input": 100,
  "output": 20,
  "cacheRead": 30,
  "cacheWrite": 40,
  "totalTokens": 190
}
```

修复前的实现会得到：

```text
prompt_tokens = 130
cache_miss_tokens = 100
```

当前实现已经将 `cacheWrite` 加入未命中输入 token，得到：

```text
prompt_tokens = 170
cache_miss_tokens = 140
```

`totalTokens` 仍使用 Pi 提供的值，因此总 token 与 prompt token 明细保持一致。回归测试：`test_usage_normalization`。

### 已修复：`splitlines()` 会破坏部分合法 JSONL 内容

位置：`trans_novel/llm/providers/pi.py:256-260`

修复前的代码使用：

```python
for line in output.splitlines():
```

Python 的 `str.splitlines()` 会将 U+2028 和 U+2029 也视为行分隔符。如果模型文本中包含这些 Unicode 字符，完整的 JSON 对象会被拆成多个片段，解析器随后会报告输出不是合法 JSONL。

Pi JSONL/RPC 协议的记录分隔符是 LF（`\n`）。当前实现仅按真实 LF 分割，并兼容每行末尾的 CR：

```python
for line in output.split("\n"):
    if line.endswith("\r"):
        line = line[:-1]
```

该问题曾通过包含 U+2028 和 U+2029 的合成 assistant 事件复现，现由回归测试 `test_parse_events_preserves_unicode_line_separators_in_json` 覆盖。

### P2：`max_tokens` 参数被静默忽略

位置：`trans_novel/llm/providers/pi.py:360-369`

当前实现直接删除了调用方传入的 `max_tokens`：

```python
del max_tokens
```

项目调用方确实会传递该参数，例如 `SynopsisAgent` 会传入 `600` 或 `1200`，`Agent._ask_json()` 和 `_ask_text()` 也支持该参数。

当前 Pi CLI 没有对应的 `--max-tokens` 参数，因此该 provider 无法实现与其他 provider 一致的输出长度限制。这不会必然导致调用失败，但可能增加响应过长、JSON 输出不完整或耗时增加的风险。

建议选择以下方案之一：

- 在 provider 接口文档和配置说明中明确声明 Pi provider 不支持 `max_tokens`；
- 如果必须支持该限制，改用 Pi SDK 或配置 Pi 模型的 `maxTokens`，并确认该限制会传递到实际 provider。

### 条件性风险：`--no-extensions` 会禁用扩展注册的 provider

位置：`trans_novel/llm/providers/pi.py:381-389`

当前调用固定传入：

```text
--no-extensions
```

Pi 的 custom provider 可以通过 extension 中的 `pi.registerProvider(...)` 注册。如果某个模型只由 extension 提供，而不是写入 `~/.pi/agent/models.json`，那么该模型在本次调用中不会被加载。

当前本机使用的模型来自 `models.json`，因此现有配置不受影响。这是对扩展 provider 的兼容性限制，而不是当前环境下的立即故障。若后续需要使用 extension 注册的 provider，应将该选项改为可配置，或移除它并评估扩展副作用。

## 已确认正确的部分

以下实现与当前 Pi 版本及官方文档一致：

- 使用 `-p` / `--print` 执行非交互调用；
- 使用 `--mode json` 获取 JSONL 事件流；
- 使用 `--no-tools` 禁止工具调用；
- 使用 `--no-session` 避免持久化会话；
- 使用 `--no-extensions`、`--no-skills`、`--no-prompt-templates` 和 `--no-context-files` 控制运行环境；
- 使用 `--model <pattern>` 指定模型；
- 使用 `--thinking <level>` 指定思考档位；
- 从 `message_end.message` 中读取最终 assistant 消息；
- 从 `message.content` 中提取 `type == "text"` 的文本块；
- 使用 Pi usage 中的 `input`、`output`、`cacheRead`、`cacheWrite` 和 `totalTokens` 字段名；
- Windows 环境下通过 `pi.cmd` 启动 CLI；
- 当前 Pi 版本能够将存在的 `--system-prompt` 路径读取为系统提示词文件。

最后一点依赖当前 Pi 实现中的路径解析行为。官方 CLI 文档将 `--system-prompt` 描述为接收文本，并未将文件路径作为稳定的公开接口，因此未来升级 Pi 时应重新验证。

## 验证结果

已执行以下检查：

- `pi --version`：`0.84.3`；
- `pi --help`：目标代码使用的 CLI 参数均存在；
- `pi --list-models`：当前配置中的 `cdxpp-gpt/...` 和 `a6-gpt/...` 模型均可找到；
- `python -m compileall trans_novel/llm/providers/pi.py`：通过；
- `pytest tests/test_llm.py -k Pi -q`：`29 passed`；
- `pytest tests/test_llm.py -q`：`69 passed, 9 subtests passed`；
- 全量测试：`281 passed, 2 failed, 19 subtests passed`。

全量测试中的两个失败位于 `tests/test_bilingual.py`，原因是测试硬编码了 POSIX 路径 `/tmp/output`，而 Windows 环境实际生成的是 `D:\tmp\output`。这两个失败与 `pi.py` 无关。

## 修复状态与后续优先级

已完成：

1. JSONL 事件流解析等待最终结果并正确处理 Pi 自动重试。
2. `cacheWrite` 已纳入 prompt 和 cache miss token 统计，并有 usage 回归测试。
3. `splitlines()` 已改为仅按 LF 分隔，并有 U+2028/U+2029 回归测试。

后续优先级：

1. 将内置默认模型改为完整的 `provider/model` 名称。
2. 明确或实现 `max_tokens` 的语义。
3. 根据是否需要 extension provider，评估 `--no-extensions` 是否应该可配置。

## 最终判断

当前代码可以在受控配置下运行，且本次审查指出的自动重试、`cacheWrite` 和 Unicode JSONL 分隔符问题已经修复。仍需处理默认模型歧义、`max_tokens` 语义和 extension provider 兼容性；当前测试尚未覆盖多个同名模型及这些未解决配置边界。
