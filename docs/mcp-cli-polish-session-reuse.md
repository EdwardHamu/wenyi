# CLI 原生会话复用于翻译后润色的可行性与改造方案

审计日期：2026-09-17

## 结论

可以实现，而且当前安装的 Pi 与 Agy 都已经提供了所需的非交互式会话恢复能力；但当前项目尚未复用原生 CLI session。

当前 `Polisher.polish_continue()` 会重新启动 CLI，并把
`system → user → assistant → user` 整段 transcript 重新序列化为一个新 prompt。Pi 路径还显式传入
`--no-session`。因此现状只能称为 transcript 重放，不能称为 Pi session 或 Agy conversation 续接。

建议新增批次级、一次性的 opaque session handle：整批翻译成功后把实际 provider、模型和原生会话 ID
随翻译结果返回，润色时只发送新增的 `polisher_continue_user`，并使用显式 ID 恢复同一会话。不要使用
“最近会话”形式的 `--continue`，也不要把 session 存在 `Translator` 的全局“上一条”属性中。

| 路径 | 当前实现 | 当前本机 CLI 能力 | 可行性 |
|---|---|---|---|
| Pi 0.85.1 | 新进程、`--no-session`、完整 transcript 重放 | `--session-id`、`--session`、`--session-dir`、`--continue` | 可实现，优先做 |
| Agy 1.2.5 | 新进程、一个 stream-json user event 内重放完整 transcript | `--conversation <ID>`、`--continue`；stream-json 会返回 conversation ID | 可实现 |
| 其它 CLI provider | 各自无状态调用 | 尚未逐个验证恢复协议 | 后续按 provider 单独接入，不能套用同一 flag |

## 当前调用链为何不是原生 session

1. `trans_novel/agents/translator.py` 在成功翻译后仅保存原始 assistant JSON、请求消息和段落索引。
2. `trans_novel/pipeline/translation.py` 从共享的 `Translator.last_batch_turn` 读取该 transcript。
3. `trans_novel/agents/polisher.py` 再追加一条 user 消息并调用普通 `complete_json()`。
4. `trans_novel/llm/providers/_cli.py` 只负责把多轮消息渲染为带角色标签的文本。
5. `trans_novel/llm/providers/pi.py` 每次启动独立进程并显式传 `--no-session`。
6. `trans_novel/llm/providers/agy.py` 每次启动独立进程，把整段消息包装为一个新的 Agy user event，
   没有传 `--conversation`。

这种实现已经能给自动前缀缓存创造机会，但 Pi 首轮单 user 输入是原始正文，第二轮 transcript 重放时旧 user
会变成带 `[USER]` 标签的文本，CLI 最终提交给模型的前缀未必与首轮完全相同。原生 session 能避免这类
客户端重建差异，并保留 CLI 自身的真实消息结构。

## 已确认的原生协议

### Pi 0.85.1

本机帮助信息明确给出：

- `--session-id <id>`：使用精确的项目 session ID，不存在时创建；
- `--session <path|id>`：打开指定 session 文件或 ID；
- `--session-dir <dir>`：指定 session 存储目录；
- `--continue`：恢复最近会话，但并发环境不可安全使用。

Pi 会把 session 保存为 JSONL，并在恢复时重建历史消息。当前安装包的运行时代码还会把
`SessionManager.getSessionId()` 传入模型运行层；因此同一 Pi session 不只稳定历史消息，也可能帮助底层
provider 维持缓存/路由作用域，但实际效果仍取决于所选模型和 provider。

推荐协议：

1. 翻译调用为该批次生成 UUID，并使用私有临时目录：
   `--session-id <uuid> --session-dir <batch-temp-dir>`；
2. 保留现有 `--no-tools --no-extensions --no-context-files --no-skills --no-prompt-templates`；
3. 润色调用再次使用同一 session ID 和目录，只把新增 user turn 写入 stdin；
4. 两轮都显式传同一模型、thinking 配置和完全相同的 system prompt；
5. 润色结束后删除该临时目录。

Pi 的 system prompt 不应假设会自动持久化。handle 应保留首轮的 system prompt（日志中只记录哈希），续接时
重新生成临时 prompt 文件并传入相同的 `--system-prompt`，否则恢复出的消息历史可能搭配不同的系统指令。

### Agy 1.2.5

本机帮助信息明确支持 `--conversation <ID>` 精确恢复和 `--continue` 恢复最近会话。当前 stream-json
协议的会话 ID 可出现在：

- `init` event 顶层的 `conversation_id`；
- `result` event 内部的 `result.conversation_id`。

当前 `parse_agy_events()` 已读取嵌套的 result，却丢弃了 conversation ID。改造后首次翻译应保存非空 ID；
若 init 与 result 同时提供非空 ID，必须校验二者相等。润色时追加
`--conversation <id>`，stdin 只发送新的 user event，并校验返回 ID 仍与 handle 相同。

不要使用裸 `--continue`：多个批次或其它 Agy 调用交错时，“最近会话”可能已经变成另一批。Agy 也支持在
一个长期 stream-json 进程内连续发送多个 user event，但这需要替换当前基于 `subprocess.run()` 的一次性
runner。第一阶段用“两个进程 + 显式 `--conversation`”即可；只有基准测试证明冷启动占比明显时，再做驻留进程。

## 推荐的接口设计

保留现有 `complete()` / `complete_json()`，新增向后兼容的会话能力，而不是修改所有 provider 的返回类型。
概念接口如下：

```python
start_conversation(messages, *, tier, json_mode, stage) -> Completion(text, handle | None)
continue_conversation(handle, user_message, *, json_mode, stage) -> Completion
close_conversation(handle) -> None
```

不支持原生 session 的 provider，`start_conversation()` 默认调用普通 `complete()` 并返回 `handle=None`；
`continue_conversation()` 则明确报告 unsupported。这样 HTTP provider 与其它 CLI 无需一次性全部改造。

handle 必须是 provider opaque 的进程内对象，但至少带以下只读路由元数据：

```text
owner/client identity
provider name + config index
tier + resolved model + reasoning/effort
native session/conversation ID
working directory + initial-prefix hash
provider-private payload（Pi session dir/system prompt；Agy conversation ID）
单次续写锁与 open/in-use/closed 状态
```

provider-private payload 不应由 Agent 解析，也不应完整写日志。尤其 system prompt 和小说内容只能记录长度或
SHA-256，不能出现在 session 生命周期日志中。

### 不再依赖 `last_batch_*`

为避免并发串批，翻译结果应直接携带本批次的 continuation context，例如：

```python
TranslationBatchResult(
    targets=[...],
    continuation=BatchContinuation(
        transcript=[system, user, assistant],
        translated_indices=[...],
        native_handle=handle_or_none,
    ),
)
```

`TranslationService.process_batch()` 使用这个局部返回值立即调用 Polisher。现有
`Translator.last_batch_turn` / `last_batch_indices` 是共享可变状态，即使当前正文翻译通常顺序执行，也不适合作为
原生 session 的所有权载体。若要保持外部 API 兼容，可让旧 `translate_batch()` 包装新的
`translate_batch_result()`，但 pipeline 必须改用显式返回对象。

## provider/model 固定与 Priority 路由

原生 session 续接的前提是翻译和润色落到同一个具体 client、同一 config index、同一 resolved model，不能在
第二轮重新按“当前 strong”独立解析。

- `RoutedLLMClient`：start 时记录实际 sub-client；continue 时按 handle 直接回到该 sub-client。
- `PriorityLLMClient`：start 时允许沿用现有 Pi 审计/502 降级循环，最终 handle 记录真正成功的 config index。
- continue 前检查 handle 的 config/provider/model 与当前有效路由。若期间已发生全局降级或恢复，不要把旧
  handle 交给另一个 provider，也不要伪装成同一会话。
- Pi 续接若出现审计或包含 HTTP 502 status code 的错误，仍执行现有优先级切换；随后作废原生 handle，使用
  transcript 在新 provider 上重放。不能把 Pi session ID 传给下一 provider。

“始终使用同一 provider/model”和“该 provider 失败后自动切换”无法同时无条件成立。建议的精确定义是：

> 原生续接成功路径严格固定 provider/model；一旦路由变化或原 provider 失败，就显式退出 native 模式，
> 使用 transcript fallback。fallback 可能换 provider，但绝不声称仍在原 session 中。

## 并发与生命周期约束

1. **每批唯一 ID**：Pi 每批独立 UUID/目录；Agy 每批保存自己返回的 conversation ID。
2. **禁止 latest**：代码中永远不使用 Pi/Agy 的裸 `--continue`。
3. **单 handle 单消费者**：handle 内有锁和状态机；同一会话的两个 continuation 不能并行。
4. **最多续写一次**：本场景只有翻译后一次润色，成功或失败后都关闭 handle。
5. **局部所有权**：handle 只随 `TranslationBatchResult` 流动，不放入 client、Translator 或模块级
   `last_session`。
6. **路由校验**：owner、config index、provider、model、cwd 任一不匹配都拒绝 native 续接。
7. **清理**：正常返回、对齐失败、JSON 失败、fallback、用户中断和异常退出都必须走幂等 close；Pi 还需
   注册进程退出时的临时目录兜底清理。
8. **不跨进程持久化**：第一版不把 handle 写入 run store。程序在翻译和润色之间崩溃时按现有恢复逻辑重新
   翻译该批，不恢复陈旧 CLI session。

### 重试的特殊风险

原生 continuation 不是天然幂等。第一次请求可能已经把 user/assistant 写入会话，但调用方因超时或解析失败而
认为失败；在同一 ID 上自动重试会重复追加润色指令。

因此：

- **新会话首轮翻译**：provider 级重试每次使用新的 Pi session ID/目录；只返回成功那次的 handle。Agy
  fresh retry 自然会创建新 conversation，只保留成功结果的 ID。
- **原生润色续接**：不要在同一 handle 上做 tenacity 自动重试。一次失败后立即关闭/作废并进入 transcript
  fallback。除非将来能读取并验证 CLI 的提交位置，否则不能安全重试。
- **对齐重试**：模型请求成功但翻译 JSON 数量不符时，必须先关闭该次 handle，再开始下一次整批翻译。

## 回退顺序

建议保持三层、可观察的回退：

1. **native continuation**：同一 provider/model/session，只发送新增 user turn；
2. **transcript replay**：使用现有 `[system, user, assistant, user]` 完整重放，可由 Priority 切到新 provider；
3. **standalone polish**：使用现有 `polisher_system + polisher_user`；仍失败或段数不符则保留原译。

只捕获普通 `Exception` 进入业务 fallback；`KeyboardInterrupt` / `SystemExit` 只做清理后继续传播。每次降级记录
`native_session_started`、`native_session_continued`、`native_session_fallback`、`native_session_closed`，包含
provider/model/config index、匿名 ID 哈希、原因和耗时，不记录正文。

## 对缓存、token 与延迟的真实收益边界

### 能确定的收益

- Python 到 CLI 的第二轮输入从“完整 transcript”变成一条短 user turn；
- CLI 使用自己保存的真实消息历史，不再依赖文本标签模拟角色；
- 翻译与润色的历史前缀、session identity、模型和 system prompt 更稳定；
- Pi/Agy 都已经暴露 cache-read 用量字段，可量化润色轮的实际缓存命中。

### 不能承诺的收益

- CLI/底层 API 很可能仍会在第二轮提交完整逻辑历史；因此 logical input/prompt token 不一定减少；
- cache-read token 通常仍计入 prompt token，只是计费或预填充成本可能更低；
- 缓存命中取决于模型/provider、最小前缀长度、TTL、租户/账号、网关和 session 路由；
- “两个进程 + 显式恢复”仍有 CLI 冷启动和会话读取开销，端到端延迟未必下降；
- Agy 驻留进程可进一步减少冷启动，但属于另一项复杂优化。

评估时不要只看命中率百分比。应对比润色阶段的
`cache_hit_tokens`、`cache_miss_tokens`、`prompt_tokens`、首 token/总耗时、失败率及 fallback 率，并按
provider/model 分组。缓存命中不能作为稳定单元测试断言，只能作为线上/基准指标。

## 建议改动范围与优先级

### P0：会话抽象与批次结果

- `trans_novel/llm/base.py`：新增 completion/handle、start/continue/close 默认能力和错误类型；
- `trans_novel/llm/router.py`：透传并验证具体 sub-client route；
- `trans_novel/llm/priority.py`：记录成功 config index，续接失败时正确触发 Pi 审计/502 降级；
- `trans_novel/agents/base.py`：增加返回 raw JSON 与 handle 的解析帮助方法；
- `trans_novel/agents/translator.py`：返回 batch-local continuation context，删除 pipeline 对共享
  `last_batch_*` 的依赖；
- `trans_novel/agents/polisher.py`：native → transcript 的两级续写逻辑；
- `trans_novel/pipeline/translation.py`：负责 handle 的最终 close 与 standalone fallback。

### P1：Pi（建议先落地）

- 普通请求继续使用 `--no-session`；只有“翻译后预计立即润色”的调用创建 session；
- 新增显式 `--session-id/--session-dir` 调用形状、session 文件存在性/ID 校验和临时目录注册清理；
- start retry 使用新 ID，continue 不做同 session 自动重试；
- 两轮固定 model、thinking、system prompt 和 cwd。

### P2：Agy

- `parse_agy_events()` 返回 response、usage、conversation ID；
- `build_agy_argv()` 支持可选但显式的 `--conversation`；
- resumed result 必须回显同一 ID；只发送增量 user event；
- 暂不实现常驻子进程。

### P3：其它 CLI 与性能增强

逐个确认 Claude Code、Codex、CodeBuddy 等的精确 ID 协议、恢复输出和并发语义后再接入。不要因为都叫
session/resume 就共享未经验证的命令行参数。Agy 常驻进程也应等 A/B 数据证明值得维护后再做。

## 测试与验收

### 离线单元/集成测试

1. Pi start argv 含唯一 `--session-id/--session-dir` 且不含 `--no-session`；普通调用仍含
   `--no-session`。
2. Pi continue 只发送新增 user，复用相同模型/system/cwd；缺失或错误 session 文件拒绝续接。
3. Pi start 重试更换 ID；continue 失败不在同 ID 上自动重试；所有路径清理目录。
4. Agy parser 覆盖 init 顶层 ID、result 嵌套 ID、缺失 ID、ID 冲突和 resume 回显不一致。
5. Agy continue argv 使用精确 `--conversation <ID>`，从不使用 `--continue`，payload 不含旧 transcript。
6. Priority/Routed 测试固定实际 client/model；Pi 审计与 502 都切换下一配置并改走 transcript。
7. 用 barrier + fake provider 并发运行至少两个批次，断言 handle、ID、正文和映射索引不交叉。
8. 覆盖翻译 JSON/对齐失败、润色 JSON/段数失败、用户中断、close 幂等和三层 fallback。
9. 用可执行 fake CLI 做有状态测试：首轮保存随机口令，续接只发新消息仍能返回该口令；无需消耗模型额度。

### 可选真实冒烟与 A/B

真实测试需显式 opt-in，使用极短提示并记录 CLI 版本：

- Pi/Agy 各验证一次“首轮记住 nonce，第二轮通过显式 ID 取回”；
- 验证两轮 provider/model/ID 一致及第二轮只发送增量；
- 不把 cache hit 大于零作为通过条件；
- 在足够数量的真实翻译批次上对比 native 与 transcript 的用量和延迟，再决定是否默认开启及是否开发
  Agy 常驻进程。

## 最终建议

值得做，但应作为 provider/pipeline 级能力实现，而不是在 `Polisher` 中拼一个 `--continue`。最稳妥的顺序是：

1. 先引入显式 batch result、opaque handle、路由固定和三层 fallback；
2. 先接 Pi，利用可控 UUID 和私有 session 目录把生命周期做正确；
3. 再接 Agy 的 `conversation_id/--conversation`；
4. 用实际 `cacheRead/cache_read_tokens` 和延迟数据决定默认开关，而不是预设原生 session 必然省 token。
