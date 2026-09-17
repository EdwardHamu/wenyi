# Configuration

[简体中文](zh/configuration.md)

Wenyi reads `config.yaml` from the current working directory. If the file is missing, running the program creates a documented default configuration.

## Languages

```yaml
language:
  source: auto
  target: zh
```

`source: auto` asks the model to identify the source language. You may instead use an ISO 639-1 code such as `ja`, `en`, `ko`, `ru`, `fr`, `de`, or `es`. The current translation pipeline is primarily designed for Simplified Chinese output.

## Model provider

```yaml
llm:
  provider: deepseek
```

Selecting `deepseek` is enough for the built-in defaults:

- Base URL: `https://api.deepseek.com`
- API key environment variable: `DEEPSEEK_API_KEY`
- Strong tier: `deepseek-v4-pro`
- Cheap and fast tiers: `deepseek-v4-flash`

API keys are always read from environment variables so they are not accidentally committed with the configuration. Use `provider: fake` for offline tests that must not make network requests.

The first PDF import also reads `MINERU_API_KEY` to call the MinerU conversion service. This key is independent of the LLM provider and is not written to `config.yaml`.

Add the advanced fields only when you need a proxy, custom environment variable, timeout, retry policy, or model override:

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: deepseek-v4-pro
      options:
        reasoning_effort: high
        thinking: true
    cheap:
      model: deepseek-v4-flash
      options:
        reasoning_effort: high
        thinking: true
    fast:
      model: deepseek-v4-flash
      options:
        thinking: true
```

`max_retries` is the number of additional attempts managed by Wenyi itself. Provider SDK retries are disabled to prevent nested requests. Wenyi retries transient transport failures, HTTP 408/409/429 and 5xx responses, plus empty model responses; each wait is recorded in the book's `events.jsonl`.

Configured tiers override the corresponding provider defaults; omitted tiers continue to use their defaults. When a requested tier is unavailable, Wenyi follows the fallback chain `fast -> cheap -> strong`.

The selected provider owns and validates the contents of `options`. In the example above, `thinking` and `reasoning_effort` are DeepSeek-specific and do not belong to the common LLM interface.

### Per-Tier Provider Overrides (Mixed Provider Mode)

Wenyi allows each `llm.tiers.<tier>` to configure a different `provider`, enabling you to combine multiple services across pipeline stages (e.g. Codex CLI for `strong` translation, DeepSeek for `cheap` entity extraction, and Pi for `fast` preprocessing):

```yaml
llm:
  provider: pi  # Default provider inherited by tiers without explicit provider

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

Each tier also supports overriding connection and model settings:
- `provider`: The provider for this tier (inherits `llm.provider` when omitted);
- `base_url`: Custom base URL for this tier;
- `api_key_env`: Environment variable name holding the API key for this tier;
- `cli_path`: Executable path for CLI-based providers (`anthropic`, `codex`, `pi`, `codebuddy`, `agy`);
- `reasoning_style`: Reasoning style for `openai-compatible` provider.

**Configuration Precedence**:
1. `tier` field
2. `llm` top-level field
3. Provider built-in default

**Cross-Provider Fallback**:
When a requested tier is not configured, Wenyi follows the fallback chain `fast -> cheap -> strong`, using the full configuration (including provider) of the resolved tier.

**Usage and Credential Validation**:
- In addition to `by_tier`, token usage ledger records a `by_provider` dimension.
- CLI commands only validate credentials for the tiers actually required by the command (e.g., `review` validates `cheap` and `review_agent_tier`; offline commands like `assemble` and `report` skip API checks).

### Multi-LLM Priority Failover (`llm_list` & `llm_priority`)

Wenyi supports configuring multiple LLM configurations via `llm_list` with priority scheduling and automatic failover via `llm_priority`:

```yaml
llm_priority: "012"
llm_list:
  - # 0: Primary Pi CLI configuration
    provider: pi
    cli_path: C:\Program Files\nodejs\pi.cmd
    tiers:
      strong:
        model: cdxpp-gpt/gpt-5.6-sol
      cheap:
        model: cdxpp-gpt/gpt-5.6-sol
      fast:
        model: a6-gpt/gemini-2.5-flash

  - # 1: Backup Agy CLI configuration
    provider: agy
    cli_path: C:\Users\username\AppData\Local\agy\bin\agy.EXE
    tiers:
      strong:
        model: gemini-3.8-flash
      cheap:
        model: gemini-3.8-flash
      fast:
        model: gemini-3.8-flash

  - # 2: Backup DeepSeek API configuration
    provider: deepseek
    api_key_env: DEEPSEEK_API_KEY
    tiers:
      strong:
        model: deepseek-v4-pro
      cheap:
        model: deepseek-v4-flash
      fast:
        model: deepseek-v4-flash
```

- `llm_list`: Supports 1 to 10 LLM configuration blocks.
- `llm_priority`: A quoted string of 0-based indices defining the order of precedence (e.g. `"012"`, `"10"`). When omitted, it defaults dynamically to `"0"`, `"01"`, `"012"`, etc., matching the length of `llm_list`.
- **Automatic Audit Failover**: When a Pi provider encounters a content audit restriction on a non-lowest priority configuration, Wenyi sends an alert email and broadcast, atomically advances to the next priority configuration, and immediately replays the failed request. If the lowest priority configuration triggers an audit restriction, Wenyi exits immediately.
- **Quiet Period Recovery**: 10 minutes (600 seconds) after the most recent audit failover, the next request automatically resets back to the highest priority configuration (`llm_priority[0]`).
- **Legacy Compatibility**: Single `llm:` configurations continue to be supported transparently by converting to a 1-item `llm_list` with `llm_priority: "0"`. Specifying both `llm:` and `llm_list:` simultaneously will raise a configuration error.

### OpenAI and OpenRouter

OpenAI and OpenRouter have dedicated providers that select their own default Base URL, API key environment variable, request fields, and reasoning format. Their model tiers must be configured explicitly:

```yaml
llm:
  provider: openrouter
  tiers:
    strong:
      model: anthropic/claude-opus-4.6
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: openai/gpt-5-mini
      options:
        thinking: true
        reasoning_effort: medium
    fast:
      model: google/gemini-3-flash
      options:
        thinking: false
```

The OpenAI provider reads `OPENAI_API_KEY`; OpenRouter reads `OPENROUTER_API_KEY`. Both providers allow `base_url` and `api_key_env` to override their defaults.

### Anthropic（本机 Claude Code CLI）

`anthropic` provider 通过本机已登录的 Claude Code CLI（`claude -p ...`
非交互模式）调用 Claude 模型，而不是 Anthropic SDK/API key。鉴权完全依赖
本机 `claude` 的登录态（OAuth/订阅），无需配置 API key：

```yaml
llm:
  provider: anthropic
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: claude-opus-4-8
      options:
        thinking: true
        reasoning_effort: high # low | medium | high | xhigh | max（xhigh/max 仅 Opus 档支持）
    cheap:
      model: claude-haiku-4-5
      options:
        thinking: false # Haiku 4.5 不支持 effort，关闭思考即可
    fast:
      model: claude-haiku-4-5
      options:
        thinking: false
```

`base_url`、`api_key_env`、`tiers.<tier>.options.extra_body` 在此 provider
下不再生效（配置了会被忽略并打印一条提示）。`thinking: true` 时映射为 CLI
的 `--effort <reasoning_effort>`；关闭思考的档位不发送 `--effort`（Haiku
4.5 等非 Opus 模型不支持该参数）。

默认通过 `shutil.which("claude")` 定位可执行文件；如果本机 PATH 上的
`claude` 命令有问题（比如损坏的全局 shim），可用 `llm.cli_path` 显式指定：

```yaml
llm:
  provider: anthropic
  cli_path: C:\Program Files\nodejs\claude.cmd
```

### Codex（本机 Codex CLI）

`codex` provider 通过本机已登录的 Codex CLI（`codex exec --json` 非交互模式）
调用模型，不需要 API key。`thinking` 和 `extra_body` 不适用；每个档位的
`reasoning_effort` 会作为 Codex CLI 的 `model_reasoning_effort` 配置传入。为避免翻译请求
启动用户全局配置的 MCP 服务，调用时还会传入 `--config mcp_servers={}`；它仅对该次
`codex exec` 生效：

```yaml
llm:
  provider: codex
  cli_path: C:\Program Files\nodejs\codex.cmd # 可省略，默认从 PATH 查找 codex
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: gpt-5.6-terra
      options:
        reasoning_effort: high
    cheap:
      model: gpt-5.6-terra
      options:
        reasoning_effort: medium
    fast:
      model: gpt-5.6-terra
      options:
        reasoning_effort: low
```

### Pi（本机 pi agent CLI）

`pi` provider 通过本机已登录的 pi agent CLI（`pi -p --mode json` 非交互模式）
调用底层模型，不需要 API key。鉴权完全依赖 pi 自身的登录态与模型提供商配置
（pi 会按它的模型列表解析 `model` 字段）。与 anthropic/codex 一样，每次
调用 spawn 一个独立的 `pi -p` 子进程，从 `--mode json` 的 JSON 事件流里
读取最终回复文本与 token 用量。

为避免把 coding agent 的默认能力带进翻译请求，调用时固定带上
`--no-tools --no-extensions --no-skills --no-prompt-templates`
`--no-context-files --no-session`：翻译只需纯文本回答，不需要工具、扩展、
技能或 AGENTS.md 上下文，同时避免每次请求重新拉起用户全局配置的扩展进程。

档位选项与 anthropic provider 类似：`thinking=true` 时把 `reasoning_effort`
作为 pi 的 `--thinking <level>` 传入（可选 off / minimal / low / medium /
high / xhigh / max）；`thinking=false` 则传 `--thinking off`。

`model` 需填 pi 中已配置的模型 ID（`pi --list-models` 可查看当前可用模型）：

```yaml
llm:
  provider: pi
  cli_path: C:\Program Files\nodejs\pi.cmd # 可省略，默认从 PATH 查找 pi
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: gpt-5.6-terra
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: gpt-5.6-sol
      options:
        thinking: true
        reasoning_effort: medium
    fast:
      model: gpt-5.6-luna
      options:
        thinking: false
```

Pi provider 运行期间的 CLI、JSONL 或模型 JSON 解析错误会追加写入当前工作目录的
`pi_errors.log`，其中包含异常 traceback 以及相关 stdout/stderr，便于定位重试失败。

当 Pi 调用捕获到错误且异常文本包含 `审计` 两个字时（例如触发内容审计命中风险规则）：
- 立即终止后续重试；
- 向广播端点 `https://meamoe.top/koa/notify` 发送 error 级别告警通知；
- 读取项目根目录的 `pi_alert_mail.yaml` 本地配置，通过 Gmail SMTP 向指定邮箱（如 `a50541853@gmail.com`）发送告警邮件；
- 广播与邮件两通道并发尝试，共享 15 秒总等待上限；通知完成或超时后，立即调用 `os._exit(1)` 强制终止整个 Python 进程。

本地邮件配置文件 `pi_alert_mail.yaml` 格式示例（该文件已被 `.gitignore` 忽略，请勿提交到版本库）：

```yaml
smtp_host: "smtp.gmail.com"
smtp_port: 465
username: "your-email@gmail.com"
password: "your-app-password"
from_email: "your-email@gmail.com"
to_email: "a50541853@gmail.com"
```

### CodeBuddy（本机 CodeBuddy Code CLI）

`codebuddy` provider 通过本机已登录的 CodeBuddy Code CLI
（`codebuddy -p --output-format json` 非交互模式）调用模型，不需要 API key。
CodeBuddy Code 是 Claude Code 形态的 CLI，档位选项与 `anthropic` provider
一致：`thinking=true` 时把该档的 `reasoning_effort` 作为 CLI 的
`--effort <level>` 传入（可选 minimal / low / medium / high / xhigh / max），
`thinking=false` 则不发送 `--effort`。

调用时固定带上 `--tools none`（翻译不需要工具）和
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`，避免每次请求都拉起
用户全局配置的 MCP 服务；两者都只对该次调用生效。

`model` 需填 CodeBuddy 中已配置的模型 ID（`codebuddy --help` 的 `--model`
一行会列出当前支持的取值）。默认通过 `shutil.which("codebuddy")` 定位可执行
文件，也可用 `llm.cli_path` 显式指定：

```yaml
llm:
  provider: codebuddy
  cli_path: C:\Program Files\nodejs\codebuddy.cmd # 可省略，默认从 PATH 查找 codebuddy
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: gpt-5.6-sol
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: gpt-5.6-sol
      options:
        thinking: true
        reasoning_effort: medium
    fast:
      model: gpt-5.6-sol
      options:
        thinking: false
```

### Agy (Local Agy CLI)

The `agy` provider calls models through the locally authenticated Agy CLI (`agy --print= --input-format stream-json --output-format stream-json` non-interactive mode) without requiring an API key. Authentication relies entirely on the local Agy CLI login state.

Models must be explicitly configured in `tiers.*.model`; the provider does not provide default models. Each tier supports `reasoning_effort: low | medium | high` (default `high`), passed to the CLI as `--effort <reasoning_effort>`. Invocations automatically pass `--disable-slash-commands` and send NDJSON user prompt messages via stdin.

By default, the executable is located via `shutil.which("agy")`. It can also be overridden with `cli_path` (supports per-tier overrides or top-level `llm.cli_path`):

```yaml
llm:
  provider: agy
  cli_path: C:\Users\username\.gemini\antigravity-cli\bin\agy.cmd # Optional, defaults to finding agy in PATH
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: gemini-3.1-pro
      options:
        reasoning_effort: high
    cheap:
      model: gemini-3-flash
      options:
        reasoning_effort: medium
    fast:
      model: gemini-3-flash
      options:
        reasoning_effort: low
```

Any CLI, NDJSON, or model JSON parsing errors occurring during Agy provider execution are appended to `agy_errors.log` in the current working directory, including the exception traceback and relevant stdout/stderr to help troubleshoot failures.

### OrcaRouter

OrcaRouter exposes an OpenAI-compatible endpoint. The built-in `orcarouter`
provider uses `https://api.orcarouter.ai/v1` and reads `ORCAROUTER_API_KEY` by
default. [Create an OrcaRouter API key](https://api.orcarouter.ai/ref/ref_262c8b8e6a274286a90a),
then configure the model IDs available to your account:

```bash
export ORCAROUTER_API_KEY=sk-orca-...
```

```yaml
llm:
  provider: orcarouter
  tiers:
    strong:
      model: your-model-id
    cheap:
      model: your-cheap-model-id
    fast:
      model: your-fast-model-id
```

Model tiers must be configured explicitly. OrcaRouter uses the generic
OpenAI-compatible options described below, including `reasoning_style` and
per-tier `request_overrides`. You may override `base_url` or `api_key_env` when
needed.

### Google Gemini

Google Gemini is supported natively through the official `google-genai` SDK using `provider: gemini` (or `provider: google`). It reads `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) from environment variables:

```yaml
llm:
  provider: gemini
  api_key_env: GEMINI_API_KEY
  tiers:
    strong:
      model: gemini-3.6-flash
    cheap:
      model: gemini-3.6-flash
    fast:
      model: gemini-3.6-flash
```

Gemini options also support `thinking_level` (e.g. `low`, `high`) or `thinking_budget` (in tokens) for Gemini reasoning models.

### Other OpenAI-compatible endpoints

Use `openai-compatible` for any endpoint implementing OpenAI Chat Completions:

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  api_key_env: EXAMPLE_API_KEY
  # deepseek | openai | openrouter | none
  reasoning_style: deepseek
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        reasoning_effort: high
        request_overrides:
          thinking:
            budget: 8192
```

`reasoning_style` converts the common `thinking` and `reasoning_effort` options into the request dialect accepted by the endpoint:

- `deepseek`: `thinking.type` plus `reasoning_effort`
- `openai`: `reasoning_effort`, with `none` sent when reasoning is disabled
- `openrouter`: `reasoning.effort`, with `reasoning.enabled: false` sent when disabled
- `none`: no conversion, for endpoints that rely on model defaults or custom request fields

By default Wenyi trusts only the standard `content` response field and retries an empty response. Set `json_response_fallback: reasoning_content` on each applicable tier only for endpoints known to place the final JSON answer in `reasoning_content`; Wenyi then accepts that field only when it contains one complete JSON value.

```yaml
llm:
  provider: openai-compatible
  tiers:
    strong:
      model: provider-model-name
      options:
        json_response_fallback: reasoning_content
```

`request_overrides` is an escape hatch for provider-specific fields that Wenyi does not know about. Its contents are merged recursively into the raw top-level request body after the selected reasoning dialect is generated. For example, an endpoint using `enable_thinking: true` can be configured as follows:

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  reasoning_style: none
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        request_overrides:
          enable_thinking: true
```

Choose a reasoning dialect according to the endpoint protocol, not the underlying model name. A relay serving a DeepSeek model should still use `reasoning_style: openai` when that relay expects OpenAI reasoning fields.

Local Ollama and vLLM endpoints are available through the `ollama` and `vllm` providers. Their default addresses are `http://localhost:11434/v1` and `http://localhost:8000/v1`, and neither requires an API key by default. Both require explicit model tiers. Ollama's OpenAI-compatible endpoint may use `reasoning_style: openai`; vLLM reasoning support depends on the model template and server arguments. When necessary, pass `enable_thinking` through `request_overrides.chat_template_kwargs`.

## Pipeline

```yaml
pipeline:
  review: false
  polish: true
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
  annotation_alignment_concurrency: 4
  review_concurrency: 4
  review_output_retries: 2
  review_agent_loop: true
  review_agent_tier: strong
  review_agent_max_evidence_rounds: 2
  review_conflict_arbitration: true
  review_fix_loop: true
  review_fix_max_rounds: 2
  review_clean_confirmations: 2
  review_autofix: false
  glossary_scope: chapter
```

- `review`: disabled by default; when enabled, automatically run the evidence-driven whole-book review after the complete book has been translated. The explicit `trans-novel review` command remains available while this is disabled.
- `polish`: run the strong model over translated batches again for style. This may improve quality but significantly increases runtime and cost.
- `rolling_context_segments`: number of recent translated segments included with each translation batch.
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `annotation_alignment`: enabled by default. After each annotated logical paragraph has been fully translated and polished, finalize its punctuation and immediately locate EPUB footnote/endnote links with one sequential model call. Split continuations are rejoined first, and segments without internal links do not call the model. When disabled, translated links remain clickable but fall back to end-of-paragraph markers; untranslated text and the source side of bilingual output retain the original link positions. This option controls link placement only; resolved source-language note content is supplied to translation automatically.
- `annotation_alignment_concurrency`: when a paragraph carries more than one annotation, each annotation is aligned through its own independent, concurrently issued request instead of asking one call to place every marker at once (a single mistake used to invalidate the whole paragraph's markers, which is why heavily annotated books tended to fall back to end-of-paragraph placement far more often). This caps how many of those per-annotation requests may run at once for a single paragraph.
- `review_concurrency`: concurrency limit for contiguous review chunks and same-round Fixer calls against an immutable translation snapshot; set it to `1` for sequential work.
- `review_output_retries`: extra attempts for a single-segment review whose output still lacks a valid completion receipt after local JSON repair and larger-chunk splitting; `2` means at most three attempts including the first call.
- `review_agent_loop`: after the unchanged initial Reviewer finds candidates in a successful leaf chunk, let an Agent Loop selectively request evidence and confirm, dismiss, or refine those candidates.
- `review_agent_tier`: model tier used by the evidence loop, cross-chunk arbiter, and provisional Review Fixer. The default is `strong`.
- `review_agent_max_evidence_rounds`: maximum selective evidence rounds per Agent Loop; the allowed range is `0` to `2`, after which the agent must return a final decision.
- `review_conflict_arbitration`: after all chunks finish, run a recommendation-only arbiter when consistency proposals for the same term, pronoun, or fixed expression contradict one another.
- `review_fix_loop`: generate complete provisional segment replacements for confirmed issues in a run-local shadow translation, then blindly review the whole book again. Disabling it keeps the single-pass recommendation-only behavior.
- `review_fix_max_rounds`: maximum number of provisional Fix rounds, from `0` to `4`; this is not the total number of Review passes.
- `review_clean_confirmations`: consecutive issue-free whole-book Review passes required after shadow fixing, from `1` to `2`; the default is `2`.
- `review_autofix`: disabled by default. After the read-only Review engine finishes, publish its folded `changes` to a working translation, run the existing bounded Review Agent Loop once more over each remaining issue against that updated text, and pass confirmed issues to the existing Review Fixer. The resulting complete segments replace only the formal chapter `target`; the manifest and glossary remain unchanged. Full before/after chains, issue IDs, decisions, failures, and write status are kept in the Review run's `autofix/index.json` instead of adding history fields to chapter JSON.
- `glossary_scope`: `chapter` includes terms relevant to the current chapter; `full` includes the complete glossary.

The command-line flags `--polish`, `--no-polish`, `--review`, and `--no-review`
override the corresponding configuration values for a `translate` run.

Run final review independently with `trans-novel review INPUT`. Each invocation
reviews the complete translated book from the beginning. By default, Review may
modify only a run-local shadow translation. Use `trans-novel review INPUT --autofix`
to override the setting for that invocation, or `--no-autofix` to force read-only
behavior. Autofix first applies folded Review changes, then reuses the same Agent
Loop and Fixer for final unresolved issues; there is no separate Autofix loop or
prompt. The consolidated result and internal round records are written under
`state/<book>/reviews/review-<timestamp>/`. Review usage is stored both as the
run-local delta and in the book's cumulative usage totals.

## Output

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
```

- `mono`: produce the monolingual Chinese edition as `<book-name>.zh.epub`.
- `bilingual`: produce a source-and-translation edition as `<book-name>.zh-bi.epub`.
- `bilingual_order`: `target_first` places the translation before the source; `source_first` reverses the order.
- `bilingual_preserve_source_style`: when `true`, source blocks inherit the book's normal text style instead of using the subdued gray style. This affects EPUB and HTML output only.
- `about_page`: append an “About this translation” project page to the book; set it to `false` to disable it.

Only the monolingual edition is enabled by default. `--bilingual` enables both editions, and configuration plus command-line switches can be combined to produce only the bilingual edition.

## Segmentation, honorifics, punctuation, and paths

```yaml
segment:
  max_tokens_per_batch: 1800
  max_tokens_per_segment: 1200

honorific:
  strategy: keep_style

punctuation:
  normalize: true

paths:
  state_dir: state
```

- `max_tokens_per_batch`: source-token budget for one model translation request, counted with tiktoken `cl100k_base` (a universal estimator, not the live provider tokenizer).
- `max_tokens_per_segment`: token threshold for splitting an exceptionally long source paragraph at sentence boundaries. Existing prepared states are not re-split automatically unless re-prepared. Legacy `max_chars_*` settings are rejected.
- `honorific.strategy`: Japanese-source honorific policy: `keep_style`, `normalize`, or `drop`.
- `punctuation.normalize`: normalize output to common full-width Simplified Chinese punctuation.
- `state_dir`: location of book checkpoints, chapter files, the glossary database, usage data, and reports. Subtitle runs store a separate tree at `<state_dir>/srt/<slug>/` (manifest, cues, batches, usage, events) and never create a glossary or review directory.
