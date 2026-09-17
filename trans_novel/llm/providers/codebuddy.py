"""通过本机已登录的 codebuddy CLI（CodeBuddy Code）调用模型（非 SDK/API）。

与 anthropic provider 同源：CodeBuddy Code 是 Claude Code 形态的 CLI，调用方式
（`-p` + `--output-format json` + `--system-prompt-file`）和 usage 字段名都与
Anthropic 原生格式一致，因此复用 anthropic.py 里协议无关的帮助函数。

与 anthropic provider 的三点差异：
- codebuddy CLI 没有 `--safe-mode`（传了会退出码非 0），改用
  `--strict-mcp-config --mcp-config {"mcpServers":{}}` 关掉 MCP server，避免
  每次翻译请求都拉起用户全局配置的 MCP 子进程；
- `--output-format json` 返回的是**事件数组**（末尾一条 `type: "result"`），
  而不是 claude 的单个 JSON 对象，故单独实现 parse_codebuddy_output；
- `--effort` 与 `--model` 无耦合限制，但沿用 thinking 开关控制是否发送，
  与 anthropic provider 的档位语义保持一致。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import LLMConfig
from ..base import LLMClient, Messages
from ..tiers import resolve_tier
from ..usage import UsageSample
from ._cli import run_cli_process
from ._openai_compatible import ResolvedTier, resolve_provider_tiers
from .anthropic import _split_system, normalize_anthropic_usage

_JSON_MODE_INSTRUCTION = "Output must be valid json."

# 关掉 MCP：--strict-mcp-config 让 CLI 只认 --mcp-config 里的服务，空表即全关。
_EMPTY_MCP_CONFIG = json.dumps({"mcpServers": {}})


class CodeBuddyTierOptions(BaseModel):
    """CodeBuddy 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    thinking: bool = True
    # minimal | low | medium | high | xhigh | max；仅在 thinking=True 时随请求
    # 发出，映射为 CLI 的 `--effort <value>`。
    reasoning_effort: str = "high"


def _default_tiers() -> dict[str, ResolvedTier[CodeBuddyTierOptions]]:
    return {
        "strong": ResolvedTier(
            model="gpt-5.6-sol",
            options=CodeBuddyTierOptions(reasoning_effort="high"),
        ),
        "cheap": ResolvedTier(
            model="gpt-5.6-sol",
            options=CodeBuddyTierOptions(reasoning_effort="medium"),
        ),
        "fast": ResolvedTier(
            model="gpt-5.6-sol",
            options=CodeBuddyTierOptions(reasoning_effort="low"),
        ),
    }


def build_cli_invocation(
    tier_config: ResolvedTier[CodeBuddyTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
) -> tuple[list[str], str, str]:
    """把通用 messages 转换成 `codebuddy` CLI 的调用形状。

    返回 (extra_argv, system_prompt_text, stdin_text)，语义与 anthropic
    provider 的同名函数一致。
    """
    system_text, chat_messages = _split_system(messages)
    if json_mode:
        system_text = (
            f"{system_text}\n\n{_JSON_MODE_INSTRUCTION}" if system_text else _JSON_MODE_INSTRUCTION
        )
    stdin_text = "\n\n".join(str(message.get("content", "")) for message in chat_messages)
    extra_argv: list[str] = ["--model", tier_config.model]
    if tier_config.options.thinking:
        extra_argv += ["--effort", tier_config.options.reasoning_effort]
    return extra_argv, system_text, stdin_text


def parse_codebuddy_output(output: str, stderr: str = "") -> tuple[str, UsageSample | None]:
    """从 `codebuddy -p --output-format json` 的事件数组里取回复文本和用量。

    输出是一个 JSON 数组：中间是 user/assistant 消息，最后一条
    `{"type": "result", "subtype": "success", "result": ..., "usage": {...}}`
    才是最终结果。为兼容将来可能改成单对象的形态，也接受顶层对象。
    """
    # 策略1：直接解析
    data = _try_parse_json(output)

    # 策略2：去除首尾空白和常见干扰后再解析
    if data is None:
        cleaned = output.strip()
        data = _try_parse_json(cleaned)

    # 策略3：提取 markdown 代码块中的 JSON
    if data is None:
        import re

        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", output, re.DOTALL)
        if match:
            data = _try_parse_json(match.group(1))

    # 策略4：查找最大的 JSON 数组或对象
    if data is None:
        data = _extract_json_from_text(output)

    # 所有策略都失败
    if data is None:
        log_file = _save_parse_error_log(output, stderr)
        raise RuntimeError(
            f"codebuddy CLI 输出不是合法 JSON，已尝试多种解析策略。\n"
            f"完整输出已保存到：{log_file}\n"
            f"原始输出（前1000字符）：\n{output[:1000]!r}\n"
            f"原始输出（后500字符）：\n{output[-500:]!r}"
        )

    result: Any = None
    if isinstance(data, dict):
        result = data
    elif isinstance(data, list):
        for event in data:
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
    if not isinstance(result, dict):
        raise RuntimeError(
            f"codebuddy CLI 输出中没有 result 事件。\n"
            f"解析出的数据类型：{type(data)}\n"
            f"数据内容（前500字符）：{str(data)[:500]!r}"
        )
    if result.get("is_error"):
        raise RuntimeError(f"codebuddy CLI 返回错误：{result!r}")

    text = result.get("result")
    if not isinstance(text, str):
        raise RuntimeError(f"codebuddy CLI 的 result 事件缺少文本。\nresult 内容：{result!r}")
    # usage 字段名与 Anthropic Messages API 完全一致，直接复用换算逻辑。
    return text, normalize_anthropic_usage(result.get("usage"))


def _try_parse_json(text: str) -> Any | None:
    """尝试解析 JSON，失败返回 None 而不抛异常。"""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _extract_json_from_text(text: str) -> Any | None:
    """从文本中提取最大的 JSON 数组或对象。

    扫描文本，查找 '[' 或 '{' 开头的子串，尝试解析为 JSON。
    优先返回最大的合法 JSON 结构。
    """
    import re

    # 查找所有可能的 JSON 起始位置
    candidates: list[tuple[int, Any]] = []

    # 查找数组起始
    for match in re.finditer(r"\[", text):
        start = match.start()
        for end in range(len(text), start, -1):
            data = _try_parse_json(text[start:end])
            if data is not None:
                candidates.append((end - start, data))
                break

    # 查找对象起始
    for match in re.finditer(r"\{", text):
        start = match.start()
        for end in range(len(text), start, -1):
            data = _try_parse_json(text[start:end])
            if data is not None:
                candidates.append((end - start, data))
                break

    # 返回最大的合法 JSON
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return None


def _save_parse_error_log(output: str, stderr: str = "") -> str:
    """保存解析失败的输出到日志文件，返回文件路径。"""
    import datetime

    log_dir = os.path.join(tempfile.gettempdir(), "codebuddy_parse_errors")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_file = os.path.join(log_dir, f"parse_error_{timestamp}.log")

    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write("CodeBuddy CLI Output Parse Error\n")
            f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n\n")

            f.write("STDOUT (Raw Output):\n")
            f.write("-" * 80 + "\n")
            f.write(output if output else "(empty)")
            f.write("\n" + "-" * 80 + "\n\n")

            f.write("STDERR:\n")
            f.write("-" * 80 + "\n")
            f.write(stderr if stderr else "(empty)")
            f.write("\n" + "-" * 80 + "\n\n")

            f.write(f"STDOUT Length: {len(output)} characters\n")
            f.write(f"STDOUT Lines: {output.count(chr(10)) + 1 if output else 0}\n")
            f.write(f"STDERR Length: {len(stderr)} characters\n")
            f.write(f"STDERR Lines: {stderr.count(chr(10)) + 1 if stderr else 0}\n")
            f.write("\n" + "=" * 80 + "\n")
    except Exception as e:
        # 如果保存失败，至少返回路径信息
        return f"{log_file} (保存失败: {e})"

    return log_file


class CodeBuddyClient(LLMClient):
    """通过本机已登录的 `codebuddy` CLI（CodeBuddy Code）调用模型。

    每次 complete() 调用 spawn 一个独立的 `codebuddy -p ... --output-format json`
    子进程，解析其输出得到回复文本和 usage 统计。鉴权完全依赖本机 codebuddy 的
    登录态，不需要配置 API key / base_url。
    """

    def __init__(self, cfg: LLMConfig, *, require_strong: bool = True):
        super().__init__()
        self.cfg = cfg
        self.provider_name = "CodeBuddy"
        if cfg.base_url:
            print(
                "提示：codebuddy provider 通过本机 codebuddy CLI 调用，"
                "llm.base_url 不生效，已忽略。"
            )
        if cfg.api_key_env:
            print(
                "提示：codebuddy provider 通过本机 codebuddy CLI 调用，"
                "llm.api_key_env 不生效，已忽略。"
            )
        self.tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=CodeBuddyTierOptions,
            defaults=_default_tiers(),
            require_strong=require_strong,
        )
        if require_strong and "strong" not in self.tiers:
            raise ValueError("配置缺少 llm.tiers.strong.model")
        self._cli_path: str | None = None
        self._cli_path_lock = threading.Lock()

    def _ensure_cli_path(
        self, tier_config: ResolvedTier[CodeBuddyTierOptions] | None = None
    ) -> str:
        with self._cli_path_lock:
            if tier_config is not None and tier_config.cli_path:
                return tier_config.cli_path
            if self._cli_path is None:
                path = self.cfg.cli_path or shutil.which("codebuddy")
                if not path:
                    raise RuntimeError(
                        "找不到 codebuddy CLI 可执行文件。请确认已安装并登录 "
                        "CodeBuddy Code（codebuddy --version 可正常运行），或在 "
                        "config.yaml 的 llm.cli_path 显式指定可执行文件路径。"
                    )
                self._cli_path = path
        return self._cli_path

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        del max_tokens  # CLI 模式无对应参数
        tier_config = resolve_tier(self.tiers, tier)
        extra_argv, system_prompt, stdin_text = build_cli_invocation(
            tier_config, messages, json_mode=json_mode
        )
        argv = (
            [self._ensure_cli_path(tier_config)]
            + [
                "--no-session-persistence",
                "--output-format",
                "json",
                "--tools",
                "none",
                # 翻译请求用不到 MCP；不关掉的话每次 exec 都会重新拉起用户全局
                # 配置的 MCP 子进程（如 npx playwright），既慢又占资源。
                "--strict-mcp-config",
                "--mcp-config",
                _EMPTY_MCP_CONFIG,
            ]
            + extra_argv
        )

        system_prompt_file: str | None = None
        if system_prompt:
            # 用临时文件而非 `--system-prompt <text>` 传递：Windows 上解析到的
            # codebuddy 可执行文件是 npm 生成的 .cmd 包装脚本，即使 shell=False，
            # Windows 也会隐式经由 cmd.exe 执行它；cmd.exe 自身的命令行解析会把
            # 参数里未转义的 < / > 当作重定向符处理（哪怕被"引号"包裹），而
            # system prompt 里常见的 "<占位符>" 写法就会触发"系统找不到指定的
            # 文件"。文件传递完全绕开 argv/cmd.exe 解析，从根上避免这个问题。
            fd, system_prompt_file = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            argv += ["--system-prompt-file", system_prompt_file]
        argv += ["-p"]

        try:
            request_id = self._new_request_id()
            attempt = 0

            @retry(
                stop=stop_after_attempt(self.cfg.max_retries + 1),
                wait=wait_exponential(multiplier=1, max=30),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            )
            def _call() -> str:
                nonlocal attempt
                attempt += 1
                with self.request_status(
                    tier=tier, stage=stage, request_id=request_id, attempt=attempt
                ):
                    proc = run_cli_process(
                        argv,
                        input_text=stdin_text,
                        timeout=self.cfg.timeout,
                        provider="CodeBuddy",
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(
                            f"codebuddy CLI 退出码非 0（{proc.returncode}）：{proc.stderr[:500]}"
                        )
                    text, sample = parse_codebuddy_output(proc.stdout, proc.stderr)
                    self.usage.record(tier, sample, stage, provider=self.provider_name)
                    return text

            return _call()
        finally:
            if system_prompt_file is not None:
                os.remove(system_prompt_file)
