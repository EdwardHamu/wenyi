"""通过本机已登录的 pi agent CLI（pi -p ...）调用底层模型。

与 anthropic / codex provider 一样：不走 HTTP API，而是 spawn 本机
`pi -p --mode json ...` 子进程，把系统提示与用户消息分别喂给
`--system-prompt`（用临时文件传递）与 stdin，再从 `--mode json` 输出的
JSON 事件流（JSONL）里读取最终的 assistant 回复文本与 token 用量。

pi 是带工具/上下文发现的 coding agent，翻译只需要纯文本回答，因此：
- `--no-tools`：关闭全部工具，避免 agent 去执行 bash / 读文件；
- `--no-extensions --no-skills --no-prompt-templates --no-context-files`：
  不加载扩展、技能、提示词模板与 AGENTS.md/CLAUDE.md 上下文，避免把 coding
  agent 的默认上下文和「信任项目文件」的交互确认带进翻译请求，也避免每次
  请求重新拉起用户全局配置的扩展进程；
- `--no-session`：不落盘会话（每次调用都是独立、可重复的一次性请求）；
- `--mode json`：输出 JSON 事件流，便于解析回复文本与用量。

鉴权完全依赖本机 pi 的登录态/已配置的模型提供商，不需要 API key / base_url。
"""

from __future__ import annotations

import json
import os
import shutil
import smtplib
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from email.message import EmailMessage
from typing import Optional

import httpx
import yaml
from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...config import LLMConfig
from ..base import LLMClient, Messages
from ..tiers import resolve_tier
from ..usage import UsageSample, read_usage_int
from ._cli import (
    render_cli_chat_messages,
    run_cli_process,
    terminate_all_active_processes,
)
from ._openai_compatible import ResolvedTier, resolve_provider_tiers

_JSON_MODE_INSTRUCTION = "Return only valid JSON, with no markdown fence or explanation."
PI_ERROR_LOG_FILE = "pi_errors.log"
_PI_ERROR_LOGGED_ATTRIBUTE = "_pi_error_logged"
_PI_ERROR_LOG_LOCK = threading.Lock()

PI_ALERT_MAIL_CONFIG_FILE = "pi_alert_mail.yaml"
PI_AUDIT_NOTIFY_URL = "https://meamoe.top/koa/notify"
AUDIT_ALERT_TOTAL_TIMEOUT = 15.0

_PROMPT_TEMP_FILES: set[str] = set()
_PROMPT_TEMP_FILES_LOCK = threading.Lock()


def _register_prompt_temp_file(path: str) -> None:
    with _PROMPT_TEMP_FILES_LOCK:
        _PROMPT_TEMP_FILES.add(path)


def _unregister_prompt_temp_file(path: str) -> None:
    with _PROMPT_TEMP_FILES_LOCK:
        _PROMPT_TEMP_FILES.discard(path)


def _cleanup_all_prompt_temp_files() -> None:
    with _PROMPT_TEMP_FILES_LOCK:
        paths = list(_PROMPT_TEMP_FILES)
        _PROMPT_TEMP_FILES.clear()
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as err:
            _append_pi_error_raw(f"清理临时 prompt 文件失败 ({path}): {type(err).__name__}: {err}")


def _pi_error_log_path() -> str:
    """Return the single append-only Pi error log path."""
    if os.path.isabs(PI_ERROR_LOG_FILE):
        return PI_ERROR_LOG_FILE
    return os.path.abspath(PI_ERROR_LOG_FILE)


def _append_pi_error_raw(text: str) -> None:
    """Append raw diagnostic line to error log without audit detection."""
    try:
        with _PI_ERROR_LOG_LOCK:
            with open(
                _pi_error_log_path(),
                "a",
                encoding="utf-8",
                errors="replace",
            ) as log_file:
                log_file.write(text.rstrip() + "\n")
    except Exception:
        pass


def _pi_error_contains_audit(error: BaseException) -> bool:
    """Traverse error and explicit cause/context chain to check for '审计'."""
    visited: set[int] = set()
    stack: list[BaseException] = [error]
    while stack:
        curr = stack.pop()
        if id(curr) in visited:
            continue
        visited.add(id(curr))
        error_text = f"{type(curr).__name__}: {curr}"
        if "审计" in error_text:
            return True
        if curr.__cause__ is not None:
            stack.append(curr.__cause__)
        if curr.__context__ is not None:
            stack.append(curr.__context__)
    return False


def _pi_alert_mail_config_path() -> str:
    if os.path.isabs(PI_ALERT_MAIL_CONFIG_FILE):
        return PI_ALERT_MAIL_CONFIG_FILE
    return os.path.abspath(PI_ALERT_MAIL_CONFIG_FILE)


def _load_audit_mail_config() -> dict[str, str | int]:
    path = _pi_alert_mail_config_path()
    if not os.path.isfile(path):
        raise RuntimeError(f"邮件配置文件不存在: {os.path.basename(path)}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as err:
        raise RuntimeError(f"邮件配置文件 YAML 解析失败: {type(err).__name__}") from err

    if not isinstance(raw, dict):
        raise RuntimeError("邮件配置格式错误：根对象必须是 mapping")

    required_str_keys = ("smtp_host", "username", "password", "from_email", "to_email")
    for key in required_str_keys:
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            raise RuntimeError(f"邮件配置缺少非空字符串字段: {key}")

    try:
        port = int(raw.get("smtp_port"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as err:
        raise RuntimeError("邮件配置 smtp_port 必须是合法整数") from err

    if not (1 <= port <= 65535):
        raise RuntimeError("邮件配置 smtp_port 超出端口范围 1-65535")

    return {
        "smtp_host": str(raw["smtp_host"]).strip(),
        "smtp_port": port,
        "username": str(raw["username"]).strip(),
        "password": str(raw["password"]).strip(),
        "from_email": str(raw["from_email"]).strip(),
        "to_email": str(raw["to_email"]).strip(),
    }


def _format_audit_alert_content(
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    error: BaseException,
) -> str:
    parts = [
        f"阶段: {stage or '未知'}",
        f"档位: {tier or '未知'}",
        f"请求 ID: {request_id if request_id is not None else '未知'}",
        f"尝试次数: {attempt if attempt is not None else '未知'}",
        f"操作: {operation}",
        f"异常摘要: {type(error).__name__}: {error}",
    ]
    return "\n".join(parts)


def _send_audit_broadcast(
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    error: BaseException,
) -> None:
    payload = {
        "title": "Wenyi Pi 内容审计告警",
        "content": _format_audit_alert_content(
            operation=operation,
            request_id=request_id,
            tier=tier,
            stage=stage,
            attempt=attempt,
            error=error,
        ),
        "level": "error",
        "sentAt": datetime.now().astimezone().isoformat(),
    }
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(PI_AUDIT_NOTIFY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("code") != 200:
            status = data.get("code") if isinstance(data, dict) else "非字典响应"
            raise RuntimeError(f"广播通知响应业务状态码异常: {status}")


def _send_audit_email(
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    error: BaseException,
) -> None:
    cfg = _load_audit_mail_config()
    content = _format_audit_alert_content(
        operation=operation,
        request_id=request_id,
        tier=tier,
        stage=stage,
        attempt=attempt,
        error=error,
    )
    body = (
        f"{content}\n\n"
        f"时间: {datetime.now().astimezone().isoformat()}\n"
        f"异常类型: {type(error).__name__}\n"
        f"完整异常消息: {error}\n"
    )
    msg = EmailMessage()
    msg["Subject"] = "Wenyi Pi 内容审计告警"
    msg["From"] = str(cfg["from_email"])
    msg["To"] = str(cfg["to_email"])
    msg.set_content(body, charset="utf-8")

    with smtplib.SMTP_SSL(str(cfg["smtp_host"]), int(cfg["smtp_port"]), timeout=10.0) as smtp:
        smtp.login(str(cfg["username"]), str(cfg["password"]))
        smtp.send_message(msg)


def _hard_exit_after_audit() -> None:
    """Terminates active processes, cleans up prompt files, and immediately exits."""
    try:
        terminate_all_active_processes()
    except Exception as err:
        _append_pi_error_raw(f"终止活跃 CLI 进程失败: {type(err).__name__}: {err}")
    try:
        _cleanup_all_prompt_temp_files()
    except Exception as err:
        _append_pi_error_raw(f"清理 prompt 临时文件失败: {type(err).__name__}: {err}")
    os._exit(1)


_AUDIT_STATE_LOCK = threading.Lock()
_AUDIT_ALERT_STARTED = False
_AUDIT_COMPLETED_EVENT = threading.Event()


def _reset_audit_alert_state_for_testing() -> None:
    """Reset the module-level one-shot audit alert state for testing."""
    global _AUDIT_ALERT_STARTED, _AUDIT_COMPLETED_EVENT
    with _AUDIT_STATE_LOCK:
        _AUDIT_ALERT_STARTED = False
        _AUDIT_COMPLETED_EVENT = threading.Event()
    with _PROMPT_TEMP_FILES_LOCK:
        _PROMPT_TEMP_FILES.clear()


def _record_audit_notification_failure(channel: str, err: BaseException) -> None:
    err_type = type(err).__name__
    err_msg = str(err).strip()[:200]
    msg = f"Wenyi Pi 审计告警{channel}通道失败: {err_type}: {err_msg}"
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    _append_pi_error_raw(msg)


def _dispatch_audit_notifications(
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    error: BaseException,
    timeout: float = AUDIT_ALERT_TOTAL_TIMEOUT,
) -> None:
    done_event = threading.Event()
    active_count = 2
    active_lock = threading.Lock()

    def mark_done() -> None:
        nonlocal active_count
        with active_lock:
            active_count -= 1
            if active_count <= 0:
                done_event.set()

    def run_broadcast() -> None:
        try:
            _send_audit_broadcast(
                operation=operation,
                request_id=request_id,
                tier=tier,
                stage=stage,
                attempt=attempt,
                error=error,
            )
        except Exception as exc:
            _record_audit_notification_failure("广播", exc)
        finally:
            mark_done()

    def run_email() -> None:
        try:
            _send_audit_email(
                operation=operation,
                request_id=request_id,
                tier=tier,
                stage=stage,
                attempt=attempt,
                error=error,
            )
        except Exception as exc:
            _record_audit_notification_failure("邮件", exc)
        finally:
            mark_done()

    broadcast_thread = threading.Thread(
        target=run_broadcast,
        name="pi-audit-broadcast",
        daemon=True,
    )
    email_thread = threading.Thread(
        target=run_email,
        name="pi-audit-email",
        daemon=True,
    )

    start_time = time.monotonic()
    broadcast_thread.start()
    email_thread.start()

    deadline = start_time + timeout
    while not done_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done_event.wait(timeout=min(remaining, 0.05))


def _handle_audit_error(
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    error: BaseException,
) -> None:
    is_owner = False
    with _AUDIT_STATE_LOCK:
        global _AUDIT_ALERT_STARTED
        if not _AUDIT_ALERT_STARTED:
            _AUDIT_ALERT_STARTED = True
            is_owner = True

    if not is_owner:
        _AUDIT_COMPLETED_EVENT.wait(timeout=AUDIT_ALERT_TOTAL_TIMEOUT)
        _hard_exit_after_audit()
        return

    try:
        _dispatch_audit_notifications(
            operation=operation,
            request_id=request_id,
            tier=tier,
            stage=stage,
            attempt=attempt,
            error=error,
        )
    finally:
        _AUDIT_COMPLETED_EVENT.set()
        _hard_exit_after_audit()


class PiAuditError(RuntimeError):
    """Pi 内容审计异常类型化信号，用于优先级客户端自动切换。"""

    pass


def _log_pi_error(
    error: BaseException,
    *,
    operation: str,
    request_id: int | None = None,
    tier: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    json_mode: bool | None = None,
    argv: list[str] | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
    response: object | None = None,
    raise_on_audit: bool = False,
) -> bool:
    """Append one Pi failure with context without masking the original error."""
    lines = [
        "=" * 80,
        f"Timestamp: {datetime.now().astimezone().isoformat()}",
        f"Operation: {operation}",
        f"Request ID: {request_id}",
        f"Tier: {tier}",
        f"Stage: {stage}",
        f"Attempt: {attempt}",
        f"JSON mode: {json_mode}",
        f"Exception: {type(error).__name__}: {error}",
        "Traceback:",
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    ]
    if argv is not None:
        lines.extend([f"Argv: {argv!r}"])

    def add_payload(label: str, value: object | None) -> None:
        if value is None:
            return
        payload = value if isinstance(value, str) else repr(value)
        lines.extend([f"{label}:", payload or "(empty)"])

    add_payload("STDOUT", stdout)
    add_payload("STDERR", stderr)
    add_payload("MODEL RESPONSE", response)
    lines.append("\n")

    logged_ok = False
    try:
        with _PI_ERROR_LOG_LOCK:
            with open(
                _pi_error_log_path(),
                "a",
                encoding="utf-8",
                errors="replace",
            ) as log_file:
                log_file.write("\n".join(lines))
        logged_ok = True
    except Exception:
        # Logging must never replace the provider exception or break retries.
        logged_ok = False

    try:
        setattr(error, _PI_ERROR_LOGGED_ATTRIBUTE, True)
    except Exception:
        pass

    if operation in {"parse_pi_events", "pi_cli_attempt"}:
        if _pi_error_contains_audit(error):
            if not raise_on_audit:
                _handle_audit_error(
                    operation=operation,
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                    attempt=attempt,
                    error=error,
                )

    return logged_ok


class PiTierOptions(BaseModel):
    """pi 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    # True 时把 reasoning_effort 作为 pi 的 `--thinking <level>` 传入；
    # False 时传 `--thinking off`。
    thinking: bool = True
    # pi 的思考档位：off | minimal | low | medium | high | xhigh | max。
    # 字段名沿用其它 provider 的 reasoning_effort。
    reasoning_effort: str = "high"


def _default_tiers() -> dict[str, ResolvedTier[PiTierOptions]]:
    return {
        "strong": ResolvedTier(
            "gpt-5.6-terra", PiTierOptions(thinking=True, reasoning_effort="high")
        ),
        "cheap": ResolvedTier(
            "gpt-5.6-sol", PiTierOptions(thinking=True, reasoning_effort="medium")
        ),
        "fast": ResolvedTier("gpt-5.6-luna", PiTierOptions(thinking=False, reasoning_effort="off")),
    }


def _split_system(messages: Messages) -> tuple[str, list[dict[str, str]]]:
    """把 system 角色消息提取为单独文本，其余消息留给 stdin。"""
    system_parts: list[str] = []
    rest: list[dict[str, str]] = []
    for message in messages:
        if message.get("role") == "system":
            content = message.get("content", "")
            if content:
                system_parts.append(str(content))
        else:
            rest.append(dict(message))
    return "\n".join(system_parts), rest


def normalize_pi_usage(usage: object) -> UsageSample | None:
    """把 pi `--mode json` 事件里的 usage 换算成统一用量。

    pi 的 usage 字段：
    - input:     本轮实际消费的输入 token（不含命中的缓存读取）
    - output:    输出 token
    - cacheRead: 命中缓存的 token
    - cacheWrite: 写入缓存的输入 token
    - totalTokens: 总计 token
    - cost:      计费信息（与统一用量无关，忽略）
    """
    if usage is None:
        return None
    input_tokens = read_usage_int(usage, "input")
    output_tokens = read_usage_int(usage, "output")
    cache_hit_tokens = read_usage_int(usage, "cacheRead")
    cache_write_tokens = read_usage_int(usage, "cacheWrite")
    cache_miss_tokens = input_tokens + cache_write_tokens
    total_tokens = read_usage_int(usage, "totalTokens") or (
        cache_miss_tokens + cache_hit_tokens + output_tokens
    )
    return UsageSample(
        prompt_tokens=cache_miss_tokens + cache_hit_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


def build_pi_invocation(
    tier_config: ResolvedTier[PiTierOptions],
    messages: Messages,
    *,
    json_mode: bool = False,
) -> tuple[list[str], str, str]:
    """把通用 messages 转换成 `pi` CLI 的调用形状。

    返回 (extra_argv, system_prompt_text, stdin_text)：
    - extra_argv：追加到固定 CLI flags 后的档位专属参数（--model、--thinking）
    - system_prompt_text：喂给 `--system-prompt`（临时文件）的文本
      （json_mode 时追加 JSON 指令）
    - stdin_text：喂给 `-p` 的 stdin 内容（多轮消息保留角色标记）
    """
    system_text, chat_messages = _split_system(messages)
    if json_mode:
        system_text = (
            f"{system_text}\n\n{_JSON_MODE_INSTRUCTION}" if system_text else _JSON_MODE_INSTRUCTION
        )
    stdin_text = render_cli_chat_messages(chat_messages)
    extra_argv: list[str] = ["--model", tier_config.model]
    if tier_config.options.thinking:
        extra_argv += ["--thinking", tier_config.options.reasoning_effort]
    else:
        extra_argv += ["--thinking", "off"]
    return extra_argv, system_text, stdin_text


def parse_pi_events(
    output: str,
    *,
    error_context: dict[str, object] | None = None,
) -> tuple[str, UsageSample | None]:
    """从 `pi -p --mode json` 的 JSONL 里读取最终的 assistant 文本与用量。

    事件流里可能包含失败的 assistant 消息及其后的自动重试，因此必须先
    读完整个事件流，再从最后一条 role == "assistant" 的 `message_end`
    中取 content 和 usage。最终消息的 stopReason 为 `error` 或 `aborted`
    时才视为请求失败并抛错（带 errorMessage）。
    """
    log_context = dict(error_context or {})
    raise_on_audit = bool(log_context.pop("raise_on_audit", False))

    def fail(message: str, cause: BaseException | None = None) -> None:
        is_audit = "审计" in message or (cause is not None and _pi_error_contains_audit(cause))
        if raise_on_audit and is_audit:
            error = PiAuditError(message)
        else:
            error = RuntimeError(message)
        if cause is not None:
            # Attach the JSON decoder error before formatting the diagnostic log.
            error.__cause__ = cause
            error.__suppress_context__ = True
        _log_pi_error(
            error,
            operation="parse_pi_events",
            stdout=output,
            raise_on_audit=raise_on_audit,
            **log_context,
        )
        raise error

    final_message: dict[str, object] | None = None
    try:
        # JSONL 只使用 LF 作为记录分隔符；str.splitlines() 还会把 U+2028
        # 和 U+2029 视为换行符，可能拆坏 JSON 字符串中的合法模型文本。
        for line in output.split("\n"):
            if line.endswith("\r"):
                line = line[:-1]
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                fail(f"pi CLI 输出包含非 JSONL 数据：{line[:500]!r}", error)
            if not isinstance(event, dict):
                fail(f"pi CLI JSONL 事件不是对象：{line[:500]!r}")
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if not isinstance(message, dict):
                fail(f"pi CLI message_end 的 message 不是对象：{line[:500]!r}")
            if message.get("role") != "assistant":
                continue
            final_message = message

        if final_message is None:
            fail(f"pi CLI 未返回 assistant 消息：{output[:500]!r}")

        stop_reason = final_message.get("stopReason")
        if stop_reason in {"error", "aborted"}:
            fail(f"pi agent 返回错误：{final_message.get('errorMessage') or '未知错误'}")

        text_parts: list[str] = []
        for block in final_message.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                text_parts.append(block["text"])
        text = "\n".join(text_parts)
        if not text.strip():
            fail(f"pi CLI 返回空文本：{output[:500]!r}")
        return text, normalize_pi_usage(final_message.get("usage"))
    except Exception as error:
        if not getattr(error, _PI_ERROR_LOGGED_ATTRIBUTE, False):
            if (
                raise_on_audit
                and _pi_error_contains_audit(error)
                and not isinstance(error, PiAuditError)
            ):
                audit_err = PiAuditError(str(error))
                audit_err.__cause__ = error
                audit_err.__suppress_context__ = True
                error = audit_err
            _log_pi_error(
                error,
                operation="parse_pi_events",
                stdout=output,
                raise_on_audit=raise_on_audit,
                **log_context,
            )
        raise


class PiClient(LLMClient):
    """通过本机已登录的 `pi` CLI（pi -p ...）调用底层模型。

    每次 complete() 都 spawn 一个独立的 `pi -p --mode json` 子进程，解析其
    JSON 事件流得到回复文本与用量。鉴权完全依赖 pi 自身的登录态/模型配置，
    不需要 API key / base_url。
    """

    def __init__(self, cfg: LLMConfig, *, require_strong: bool = True):
        super().__init__()
        self.cfg = cfg
        self.provider_name = "Pi"
        if cfg.base_url or cfg.api_key_env:
            print(
                "提示：pi provider 通过本机已登录的 pi CLI 调用，"
                "llm.base_url / api_key_env 不再生效，已忽略。"
            )
        self.tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=PiTierOptions,
            defaults=_default_tiers(),
            require_strong=require_strong,
        )
        if require_strong and "strong" not in self.tiers:
            raise ValueError("配置缺少 llm.tiers.strong.model")
        self.raise_on_audit: bool = False
        self._cli_path: str | None = None
        self._cli_path_lock = threading.Lock()

    def _on_complete_json_error(
        self,
        error: BaseException,
        response: str,
        *,
        request_id: int,
        tier: str,
        stage: str | None,
    ) -> None:
        """Persist model JSON parsing failures together with the raw response."""
        _log_pi_error(
            error,
            operation="complete_json_parse",
            request_id=request_id,
            tier=tier,
            stage=stage,
            response=response,
        )

    def _ensure_cli_path(self, tier_config: ResolvedTier[PiTierOptions] | None = None) -> str:
        with self._cli_path_lock:
            if tier_config is not None and tier_config.cli_path:
                return tier_config.cli_path
            if self._cli_path is None:
                path = self.cfg.cli_path or shutil.which("pi")
                if not path:
                    raise RuntimeError(
                        "找不到 pi CLI 可执行文件。请确认已安装并登录 "
                        "pi（pi --version 可正常运行），或在 config.yaml 的 "
                        "llm.cli_path 显式指定可执行文件路径。"
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
        del max_tokens
        request_id = self._new_request_id()
        attempt = 0
        argv: list[str] | None = None
        system_prompt_file: str | None = None
        try:
            tier_config = resolve_tier(self.tiers, tier)
            extra_argv, system_prompt, stdin_text = build_pi_invocation(
                tier_config, messages, json_mode=json_mode
            )
            cli_path = self._ensure_cli_path(tier_config)

            argv = (
                [cli_path, "-p", "--no-tools", "--no-session", "--no-extensions"]
                + [
                    "--no-context-files",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--mode",
                    "json",
                ]
                + extra_argv
            )

            if system_prompt:
                # 用临时文件而非 `--system-prompt <text>` 传递：与 anthropic.py
                # 相同的理由——Windows 上 npm 生成的 .cmd 包装脚本会经 cmd.exe
                # 解析 argv，未转义的 < > 会被当重定向符；且 pi 的
                # --system-prompt 对「存在的文件路径」会直接读文件内容，正好利用
                # 这一点绕开 argv 解析。临时文件在 finally 中清理。
                fd, system_prompt_file = tempfile.mkstemp(suffix=".txt")
                _register_prompt_temp_file(system_prompt_file)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(system_prompt)
                argv += ["--system-prompt", system_prompt_file]

            @retry(
                stop=stop_after_attempt(self.cfg.max_retries + 1),
                wait=wait_exponential(multiplier=1, max=30),
                retry=retry_if_exception_type(Exception)
                & retry_if_not_exception_type(PiAuditError),
                reraise=True,
            )
            def _call() -> str:
                nonlocal attempt
                attempt += 1
                proc = None
                try:
                    with self.request_status(
                        tier=tier, stage=stage, request_id=request_id, attempt=attempt
                    ):
                        proc = run_cli_process(
                            argv,
                            input_text=stdin_text,
                            timeout=self.cfg.timeout,
                            provider="pi",
                        )
                        if proc.returncode != 0:
                            err_msg = f"pi CLI 退出码非 0（{proc.returncode}）：{proc.stderr[:500]}"
                            if self.raise_on_audit and _pi_error_contains_audit(
                                RuntimeError(err_msg)
                            ):
                                raise PiAuditError(err_msg)
                            raise RuntimeError(err_msg)
                        text, sample = parse_pi_events(
                            proc.stdout,
                            error_context={
                                "request_id": request_id,
                                "tier": tier,
                                "stage": stage,
                                "attempt": attempt,
                                "json_mode": json_mode,
                                "argv": argv,
                                "stderr": proc.stderr,
                                "raise_on_audit": self.raise_on_audit,
                            },
                        )
                        self.usage.record(tier, sample, stage, provider=self.provider_name)
                        return text
                except Exception as error:
                    if self.raise_on_audit and _pi_error_contains_audit(error):
                        if not isinstance(error, PiAuditError):
                            audit_err = PiAuditError(str(error))
                            audit_err.__cause__ = error
                            audit_err.__suppress_context__ = True
                            error = audit_err
                    if not getattr(error, _PI_ERROR_LOGGED_ATTRIBUTE, False):
                        stdout = getattr(proc, "stdout", None)
                        if stdout is None:
                            stdout = getattr(error, "stdout", None) or getattr(
                                error, "output", None
                            )
                        stderr = getattr(proc, "stderr", None)
                        if stderr is None:
                            stderr = getattr(error, "stderr", None)
                        _log_pi_error(
                            error,
                            operation="pi_cli_attempt",
                            request_id=request_id,
                            tier=tier,
                            stage=stage,
                            attempt=attempt,
                            json_mode=json_mode,
                            argv=argv,
                            stdout=stdout,
                            stderr=stderr,
                            raise_on_audit=self.raise_on_audit,
                        )
                    raise

            return _call()
        except Exception as error:
            if not getattr(error, _PI_ERROR_LOGGED_ATTRIBUTE, False):
                _log_pi_error(
                    error,
                    operation="pi_complete",
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                    attempt=attempt or None,
                    json_mode=json_mode,
                    argv=argv,
                    raise_on_audit=self.raise_on_audit,
                )
            raise

        finally:
            if system_prompt_file is not None:
                _unregister_prompt_temp_file(system_prompt_file)
                had_active_error = sys.exc_info()[0] is not None
                try:
                    if os.path.exists(system_prompt_file):
                        os.remove(system_prompt_file)
                except OSError as error:
                    _log_pi_error(
                        error,
                        operation="pi_temp_file_cleanup",
                        request_id=request_id,
                        tier=tier,
                        stage=stage,
                        attempt=attempt or None,
                        json_mode=json_mode,
                        argv=argv,
                    )
                    if not had_active_error:
                        raise
