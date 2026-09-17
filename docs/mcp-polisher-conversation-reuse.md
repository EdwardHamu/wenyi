# Polisher 对话复用审计

审计日期：2026-09-17

## 结论

`Polisher.polish_continue()` 能在同一进程、同一批次的翻译后，把上一轮翻译的
system/user/assistant 内容带入润色请求；当前主配置使用的 Pi CLI 也通过了真实冒烟测试。

但这不是 CLI 原生会话或 session ID 的复用，而是一次新的、无状态的模型调用：程序重新提交
上一轮 transcript，再追加润色指令。因而应把当前能力准确称为“transcript 重放”或“上下文重放”，
不能理解为恢复 CLI 的上一会话，也不保证减少输入 token。

| 场景 | 结论 |
|---|---|
| 同一批次、同一进程内复用翻译上下文 | 是 |
| HTTP/API provider 保留消息角色 | 是，完整 messages 被重新提交 |
| Pi/Claude Code/CodeBuddy CLI 原生会话复用 | 否 |
| Codex/Agy CLI 原生会话复用 | 否 |
| 进程重启或断点续跑后恢复上一对话 | 否 |
| 当前 Pi CLI 路径能否实际完成续写润色 | 能，本次冒烟测试成功 |

## 调用链证据

1. `trans_novel/agents/translator.py:115-145` 构造翻译请求，并在成功解析后把原始
   assistant JSON 追加到 `[system, user]`，形成三条消息的 `turn`。
2. `trans_novel/agents/translator.py:250-251` 仅把该 turn 和实际翻译段落索引保存在
   `Translator.last_batch_turn`、`last_batch_indices` 内存属性中。
3. `trans_novel/pipeline/translation.py:798-810` 紧接翻译调用
   `Polisher.polish_continue()`，并把成功结果映射回未被过滤的段落。
4. `trans_novel/agents/polisher.py:64-73` 在 turn 后追加新的 user 消息，再通过
   `complete_json(messages)` 发起第二次请求。
5. 续写失败或数量不符时，`trans_novel/pipeline/translation.py:811-814` 会改走独立
   `Polisher.polish()`；批量翻译降级为逐段翻译时，
   `trans_novel/agents/translator.py:265-267` 会主动清空 transcript。

因此，复用范围严格限于“成功的整批翻译之后立即润色”。它不是可持久化会话状态。

## CLI provider 行为

### Pi

- `trans_novel/llm/providers/pi.py:693-695` 明确说明每次 `complete()` 都创建独立子进程。
- `trans_novel/llm/providers/pi.py:775` 显式传入 `--no-session`。
- `trans_novel/llm/providers/pi.py:578-584` 抽出 system 后，通过共享 CLI renderer
  序列化其余消息；单轮请求保持原始正文，多轮请求保留 user/assistant 角色标记。

实际四轮输入会被转换为：

```text
[USER]
FIRST-USER

[ASSISTANT]
FIRST-ASSISTANT

[USER]
SECOND-USER
```

上一轮内容和角色边界现在都存在，所以模型能依据明确的“上一条 JSON”指令完成润色；但这些
角色仍是新请求里的文本标签，不是 CLI 原生多轮消息结构。

### Claude Code 与 CodeBuddy

- `trans_novel/llm/providers/anthropic.py:144-147`、
  `trans_novel/llm/providers/codebuddy.py:293-368` 均为每次调用创建独立 CLI 进程。
- Claude Code 使用 `--no-session-persistence`（`anthropic.py:206-216`）。
- CodeBuddy 使用 `--no-session-persistence`（`codebuddy.py:307-322`）。
- 两者与 Pi 一样，通过共享 renderer 为多轮 transcript 增加角色边界
  （`anthropic.py:130-138`、`codebuddy.py:87-96`）。

所以这两条路径现在能可靠区分 user/assistant 文本，但仍只能算带角色的内容重放，不能算
原生对话复用。

### Codex 与 Agy

- Codex 使用 `codex exec --ephemeral`（`trans_novel/llm/providers/codex.py:170-190`）。
- Agy 每次 `complete()` 都启动新的 stream-json CLI 调用
  （`trans_novel/llm/providers/agy.py:316-370`）。
- 两者会把完整 transcript 渲染成带 `[SYSTEM]`、`[USER]`、`[ASSISTANT]` 标记的
  单个 prompt（`codex.py:44-53`、`agy.py:113-122`）。

它们仍不是原生 session；修复后三组 CLI 路径都能在单个文本 prompt 中保留多轮角色语义。

## 角色标签修复

`trans_novel/llm/providers/_cli.py` 新增共享的 `render_cli_chat_messages()`：

- 单条非 system 消息继续输出原始正文，避免改变普通翻译请求的 prompt；
- 两条及以上消息输出 `[USER]`、`[ASSISTANT]` 等角色标签；
- Pi、Claude Code、CodeBuddy 统一调用该 renderer，避免三份实现再次漂移。

`tests/test_llm.py` 为三个 provider 分别增加 user/assistant/user 多轮回归测试，并保留原有
单轮 payload 断言。本次先运行测试确认修复前 3 项全部失败，再实现共享 renderer。

## 验证结果

### 现有自动测试

执行：

```text
uv run --no-sync pytest -q tests/test_review_polish.py tests/test_translation_context.py tests/test_llm.py -k "polish_continue or continuation_failure or continuation_with_filtered or invocation_splits_system or prompt_preserves_roles_and_json_instruction"
```

完整相关测试结果：`140 passed, 12 subtests passed in 6.90s`。

这些测试既证明业务层会组装 `[system, user, assistant, user]` 并验证失败回退，也固定了
Pi、Claude Code、CodeBuddy 的单轮兼容和多轮角色边界。

### CLI 序列化探针

修复后对同一四轮 transcript 直接调用各 builder：

- Pi、Claude Code、CodeBuddy：system 仍走各 CLI 的 system prompt 参数，stdin 保留
  `[USER] / [ASSISTANT] / [USER]` 文本标签。
- Codex、Agy：继续保留 `[SYSTEM] / [USER] / [ASSISTANT] / [USER]` 文本标签。

### 当前 Pi CLI 真实冒烟

修复后通过当前配置的 `PiClient` 实际调用 `Polisher.polish_continue()`，关闭重试并限制超时，
向预制翻译 turn 追加润色请求。结果成功返回：

```text
POLISH_CONTINUE_RESULT= ['夜色深沉。']
PROVIDER= Pi
```

这证明当前 Pi 路径在行为上可用，但不改变其“新进程 + 无 session + transcript 文本重放”的性质。

## 风险与建议

1. **术语误导（中）**：代码中的 `continue`、测试名中的 `reuses_translation_transcript`
   容易被解释为 CLI session 续接。建议在公开文档和注释中明确“无状态 transcript 重放”。
2. **文本标签仍非原生角色（低）**：角色丢失已修复，但 CLI 接收的仍是带标签的单个文本
   prompt；如果 CLI 将来支持稳定的结构化多消息输入，后者仍会比文本标签更可靠。
3. **成本预期（中）**：第二次调用重发原翻译 system、user 和 assistant 响应；除非底层 provider
   恰好命中 prompt cache，否则不会获得原生会话带来的 token 或延迟优势。
4. **恢复边界（低）**：`last_batch_turn` 不落盘，重启后不能续接；整批对齐失败也会放弃该路径。
5. **优先级切换（低）**：`PriorityLLMClient` 可能在审计降级或静默恢复后，把润色 transcript
   交给另一个 provider。上下文仍在，但不再是同一模型的“上一轮”。

建议的最小改进顺序：

1. 已完成：为 Pi、Claude Code、CodeBuddy 增加多轮序列化回归测试。
2. 已完成：三个 provider 使用统一的带角色标记 transcript renderer。
3. 后续可将功能文案统一为“复用翻译 transcript 进行无状态续写润色”。
4. 如果对应 CLI 支持可靠的结构化多消息 stdin，应优先使用结构化格式代替文本角色标签。
5. 若目标确实是原生会话续接，需要新增 provider capability/session handle，固定翻译与润色的
   provider、模型和进程生命周期，并设计持久化、失效及回退协议；仅修改 `polisher.py` 不足以实现。
