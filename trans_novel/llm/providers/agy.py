"""通过本机已登录的 agy CLI 调用模型。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ...config import LLMConfig
from ..base import (
    ConversationCompletion,
    LLMClient,
    Messages,
    NativeConversation,
    NativeConversationInvalidError,
    conversation_fingerprint,
)
from ..tiers import resolve_tier
from ..usage import UsageSample, read_usage_int
from ._cli import run_cli_process
from ._openai_compatible import ResolvedTier, resolve_provider_tiers

_JSON_MODE_INSTRUCTION = "Return only valid JSON, with no markdown fence or explanation."
AGY_ERROR_LOG_FILE = "agy_errors.log"
_AGY_ERROR_LOGGED_ATTRIBUTE = "_agy_error_logged"
_AGY_ERROR_LOG_LOCK = threading.Lock()
_AGY_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][^\s\x00-\x1f\x7f]{0,511}$")


def _agy_error_log_path() -> str:
    """Return the single append-only Agy error log path."""
    if os.path.isabs(AGY_ERROR_LOG_FILE):
        return AGY_ERROR_LOG_FILE
    return os.path.abspath(AGY_ERROR_LOG_FILE)


def _log_agy_error(
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
) -> bool:
    """Append one Agy failure with context without masking the original error."""
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

    try:
        with _AGY_ERROR_LOG_LOCK:
            with open(
                _agy_error_log_path(),
                "a",
                encoding="utf-8",
                errors="replace",
            ) as log_file:
                log_file.write("\n".join(lines))
    except Exception:
        # Logging must never replace the provider exception or break retries.
        return False

    try:
        setattr(error, _AGY_ERROR_LOGGED_ATTRIBUTE, True)
    except Exception:
        pass
    return True


class AgyTierOptions(BaseModel):
    """Agy 档位的专属请求选项。"""

    model_config = ConfigDict(extra="forbid")

    reasoning_effort: Literal["low", "medium", "high"] = "high"

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_effort(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


def build_agy_prompt(messages: Messages, *, json_mode: bool = False) -> str:
    """将通用消息转换为带角色标记的纯文本 prompt。"""
    parts: list[str] = []
    for message in messages:
        content = str(message.get("content", ""))
        if content:
            parts.append(f"[{message.get('role', 'user').upper()}]\n{content}")
    if json_mode:
        parts.append(f"[OUTPUT FORMAT]\n{_JSON_MODE_INSTRUCTION}")
    return "\n\n".join(parts)


def build_agy_input_payload(prompt: str) -> str:
    """通过 stdin 发送 Agy stream-json 用户消息。"""
    return (
        json.dumps(
            {
                "event": "user",
                "message": {"role": "user", "content": prompt},
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def build_agy_argv(
    cli_path: str,
    tier_config: ResolvedTier[AgyTierOptions],
) -> list[str]:
    """构造 Agy CLI 命令行参数。"""
    return [
        cli_path,
        "--print=",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--model",
        tier_config.model,
        "--effort",
        tier_config.options.reasoning_effort,
    ]


def build_agy_invocation(
    tier_config: ResolvedTier[AgyTierOptions],
    messages: Messages,
    *,
    cli_path: str = "agy",
    json_mode: bool = False,
) -> tuple[list[str], str]:
    """把通用 messages 转换成 Agy CLI 的 argv 与 stdin payload。"""
    argv = build_agy_argv(cli_path, tier_config)
    prompt = build_agy_prompt(messages, json_mode=json_mode)
    stdin_payload = build_agy_input_payload(prompt)
    return argv, stdin_payload


def normalize_agy_usage(usage: Any) -> UsageSample | None:
    """将 Agy 用量字段映射为统一账本。"""
    if usage is None:
        return None
    prompt_tokens = read_usage_int(usage, "input_tokens")
    completion_tokens = read_usage_int(usage, "output_tokens")
    cache_hit_tokens = read_usage_int(usage, "cache_read_tokens")
    cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
    total_tokens = read_usage_int(usage, "total_tokens") or (prompt_tokens + completion_tokens)
    return UsageSample(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


@dataclass(frozen=True, slots=True)
class AgyParsedEvents:
    text: str
    usage: UsageSample | None
    conversation_id: str | None


def _parse_agy_events_full(
    output: str,
    stderr: str = "",
    *,
    error_context: dict[str, Any] | None = None,
    require_conversation_id: bool = False,
    expected_conversation_id: str | None = None,
) -> AgyParsedEvents:
    """解析 Agy NDJSON，并在原生会话路径严格校验 conversation ID。"""
    log_context = dict(error_context or {})
    # stderr is supplied as a dedicated argument below; do not pass it again
    # through the extensible context mapping.
    log_context.pop("stderr", None)

    def fail(message: str, cause: BaseException | None = None) -> None:
        error = RuntimeError(message)
        if cause is not None:
            error.__cause__ = cause
            error.__suppress_context__ = True
        _log_agy_error(
            error,
            operation="parse_agy_events",
            stdout=output,
            stderr=stderr,
            **log_context,
        )
        raise error

    result_event: dict[str, Any] | None = None
    conversation_ids: list[tuple[str, str]] = []
    validate_conversation_id = require_conversation_id or expected_conversation_id is not None

    def collect_conversation_id(container: dict[str, Any], source: str) -> None:
        if not validate_conversation_id:
            return
        for key in ("conversation_id", "conversationId"):
            if key not in container:
                continue
            value = container[key]
            if (
                not isinstance(value, str)
                or value != value.strip()
                or _AGY_CONVERSATION_ID_RE.fullmatch(value) is None
            ):
                fail(f"agy CLI returned invalid {source} {key}: {value!r}")
            conversation_ids.append((source, value))

    try:
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, json.JSONDecodeError) as error:
                fail(f"agy CLI output contains non-JSON data: {line[:500]!r}", cause=error)
            if not isinstance(event, dict):
                continue
            event_kind = event.get("event") or event.get("type")
            if event_kind == "init" or event.get("subtype") == "init":
                collect_conversation_id(event, "init")
            if event.get("event") == "result":
                result_event = event

        if result_event is None:
            fail(f"agy CLI did not return a result event: {output[:500]!r}")

        result_data = result_event.get("result", result_event)
        if not isinstance(result_data, dict):
            fail(f"agy CLI result event has invalid result payload: {result_event!r}")
        collect_conversation_id(result_data, "result")

        status = result_data.get("status")
        if status != "SUCCESS":
            error_msg = result_data.get("error") or result_data.get("message")
            detail = f" ({error_msg})" if error_msg else ""
            fail(f"agy CLI returned non-success status {status!r}{detail}: {output[:500]!r}")

        response = result_data.get("response")
        if not isinstance(response, str) or not response.strip():
            fail(f"agy CLI returned empty or missing response: {result_data!r}")

        usage_data = result_data.get("usage")
        if usage_data is None and ("input_tokens" in result_data or "output_tokens" in result_data):
            usage_data = result_data
        usage = normalize_agy_usage(usage_data)
        unique_ids = {conversation_id for _source, conversation_id in conversation_ids}
        if len(unique_ids) > 1:
            rendered = ", ".join(f"{source}={value!r}" for source, value in conversation_ids)
            fail(f"agy CLI returned conflicting conversation IDs: {rendered}")
        conversation_id = next(iter(unique_ids), None)
        if require_conversation_id and conversation_id is None:
            fail("agy CLI did not return a conversation ID")
        if expected_conversation_id is not None and conversation_id != expected_conversation_id:
            fail(
                "agy CLI resumed a different conversation ID: "
                f"expected {expected_conversation_id!r}, got {conversation_id!r}"
            )
        return AgyParsedEvents(response, usage, conversation_id)
    except Exception as error:
        if not getattr(error, _AGY_ERROR_LOGGED_ATTRIBUTE, False):
            _log_agy_error(
                error,
                operation="parse_agy_events",
                stdout=output,
                stderr=stderr,
                **log_context,
            )
        raise


def parse_agy_events(
    output: str,
    stderr: str = "",
    *,
    error_context: dict[str, Any] | None = None,
) -> tuple[str, UsageSample | None]:
    """向后兼容解析入口；普通 complete 不强制旧版事件提供 conversation ID。"""
    parsed = _parse_agy_events_full(output, stderr, error_context=error_context)
    return parsed.text, parsed.usage


parse_agy_output = parse_agy_events


@dataclass(frozen=True, slots=True)
class _AgyConversationPayload:
    argv: tuple[str, ...]
    json_mode: bool


class AgyClient(LLMClient):
    """通过本机已登录的 `agy` CLI 调用模型。"""

    def __init__(self, cfg: LLMConfig, *, require_strong: bool = True):
        super().__init__()
        self.provider_name = "Agy"
        self.cfg = cfg
        if cfg.base_url or cfg.api_key_env:
            print("agy provider uses the local Agy CLI; llm.base_url / api_key_env are ignored.")
        self.tiers = resolve_provider_tiers(
            cfg.tiers,
            options_type=AgyTierOptions,
            require_strong=require_strong,
        )
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
        _log_agy_error(
            error,
            operation="complete_json_parse",
            request_id=request_id,
            tier=tier,
            stage=stage,
            response=response,
        )

    def _ensure_cli_path(self, tier_config: ResolvedTier[AgyTierOptions] | None = None) -> str:
        with self._cli_path_lock:
            if tier_config is not None and tier_config.cli_path:
                return tier_config.cli_path
            if self._cli_path is None:
                path = self.cfg.cli_path or shutil.which("agy")
                if not path:
                    raise RuntimeError(
                        "未找到 agy CLI。请安装并登录 Agy CLI，或在 config.yaml 中配置 llm.cli_path。"
                    )
                self._cli_path = path
        return self._cli_path

    def _run_conversation_attempt(
        self,
        argv: list[str],
        stdin_payload: str,
        *,
        cwd: str,
        request_id: int,
        tier: str,
        stage: str | None,
        attempt: int,
        json_mode: bool,
        expected_conversation_id: str | None = None,
    ) -> AgyParsedEvents:
        proc = None
        try:
            with self.request_status(
                tier=tier,
                stage=stage,
                request_id=request_id,
                attempt=attempt,
            ):
                proc = run_cli_process(
                    argv,
                    input_text=stdin_payload,
                    timeout=self.cfg.timeout,
                    provider="Agy",
                    cwd=cwd,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"agy CLI exited {proc.returncode}: {proc.stderr[:500]}")
                parsed = _parse_agy_events_full(
                    proc.stdout,
                    proc.stderr,
                    error_context={
                        "request_id": request_id,
                        "tier": tier,
                        "stage": stage,
                        "attempt": attempt,
                        "json_mode": json_mode,
                        "argv": argv,
                    },
                    require_conversation_id=True,
                    expected_conversation_id=expected_conversation_id,
                )
                self.usage.record(tier, parsed.usage, stage, provider=self.provider_name)
                return parsed
        except Exception as error:
            if not getattr(error, _AGY_ERROR_LOGGED_ATTRIBUTE, False):
                stdout = getattr(proc, "stdout", None)
                if stdout is None:
                    stdout = getattr(error, "stdout", None) or getattr(error, "output", None)
                stderr = getattr(proc, "stderr", None)
                if stderr is None:
                    stderr = getattr(error, "stderr", None)
                _log_agy_error(
                    error,
                    operation="agy_cli_attempt",
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                    attempt=attempt,
                    json_mode=json_mode,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                )
            raise

    def start_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> ConversationCompletion:
        del max_tokens
        tier_config = resolve_tier(self.tiers, tier)
        cli_path = self._ensure_cli_path(tier_config)
        argv = build_agy_argv(cli_path, tier_config)
        stdin_payload = build_agy_input_payload(build_agy_prompt(messages, json_mode=json_mode))
        request_id = self._new_request_id()
        cwd = os.path.abspath(os.getcwd())
        attempt = 0

        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call_fresh() -> ConversationCompletion:
            nonlocal attempt
            attempt += 1
            parsed = self._run_conversation_attempt(
                argv,
                stdin_payload,
                cwd=cwd,
                request_id=request_id,
                tier=tier,
                stage=stage,
                attempt=attempt,
                json_mode=json_mode,
            )
            conversation_id = parsed.conversation_id
            if conversation_id is None:
                raise NativeConversationInvalidError(
                    "Agy conversation ID is missing after validation"
                )
            handle = NativeConversation(
                provider=self.provider_name,
                config_index=self.config_index,
                tier=tier,
                model=tier_config.model,
                native_id=conversation_id,
                cwd=cwd,
                prefix_hash=conversation_fingerprint(messages),
                _owner=self,
                payload=_AgyConversationPayload(tuple(argv), json_mode),
            )
            self.emit_event(
                "native_session_started",
                provider=self.provider_name,
                model=tier_config.model,
                config_index=handle.config_index,
                session_id_hash=handle.id_hash,
            )
            return ConversationCompletion(parsed.text, handle, request_id)

        return _call_fresh()

    def continue_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> ConversationCompletion:
        del max_tokens
        handle.claim(self)
        request_id = self._new_request_id()
        payload = handle.payload
        try:
            if not isinstance(payload, _AgyConversationPayload):
                raise NativeConversationInvalidError("invalid Agy conversation payload")
            if payload.json_mode != json_mode:
                raise NativeConversationInvalidError(
                    "Agy conversation JSON mode cannot change during continuation"
                )
            if not os.path.isdir(handle.cwd):
                raise NativeConversationInvalidError(
                    "Agy conversation working directory no longer exists"
                )
            argv = [*payload.argv, "--conversation", handle.native_id]
            # 只发送新增 user event；禁止使用依赖全局最近会话的裸 --continue。
            stdin_payload = build_agy_input_payload(user_message)
            parsed = self._run_conversation_attempt(
                argv,
                stdin_payload,
                cwd=handle.cwd,
                request_id=request_id,
                tier=handle.tier,
                stage=stage,
                attempt=1,
                json_mode=json_mode,
                expected_conversation_id=handle.native_id,
            )
            return ConversationCompletion(parsed.text, request_id=request_id)
        finally:
            self.close_conversation(handle)

    def close_conversation(self, handle: NativeConversation) -> None:
        first_close = handle.mark_closed(self)
        if first_close:
            self.emit_event(
                "native_session_closed",
                provider=self.provider_name,
                model=handle.model,
                config_index=handle.config_index,
                session_id_hash=handle.id_hash,
            )

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
        tier_config = resolve_tier(self.tiers, tier)
        cli_path = self._ensure_cli_path(tier_config)
        argv = build_agy_argv(cli_path, tier_config)
        prompt = build_agy_prompt(messages, json_mode=json_mode)
        stdin_payload = build_agy_input_payload(prompt)

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
            proc = None
            try:
                with self.request_status(
                    tier=tier, stage=stage, request_id=request_id, attempt=attempt
                ):
                    proc = run_cli_process(
                        argv,
                        input_text=stdin_payload,
                        timeout=self.cfg.timeout,
                        provider="Agy",
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(f"agy CLI exited {proc.returncode}: {proc.stderr[:500]}")
                    text, usage = parse_agy_events(
                        proc.stdout,
                        proc.stderr,
                        error_context={
                            "request_id": request_id,
                            "tier": tier,
                            "stage": stage,
                            "attempt": attempt,
                            "json_mode": json_mode,
                            "argv": argv,
                        },
                    )
                    self.usage.record(tier, usage, stage, provider=self.provider_name)
                    return text
            except Exception as error:
                if not getattr(error, _AGY_ERROR_LOGGED_ATTRIBUTE, False):
                    stdout = getattr(proc, "stdout", None)
                    if stdout is None:
                        stdout = getattr(error, "stdout", None) or getattr(error, "output", None)
                    stderr = getattr(proc, "stderr", None)
                    if stderr is None:
                        stderr = getattr(error, "stderr", None)
                    _log_agy_error(
                        error,
                        operation="agy_cli_attempt",
                        request_id=request_id,
                        tier=tier,
                        stage=stage,
                        attempt=attempt,
                        json_mode=json_mode,
                        argv=argv,
                        stdout=stdout,
                        stderr=stderr,
                    )
                raise

        try:
            return _call()
        except Exception as error:
            if not getattr(error, _AGY_ERROR_LOGGED_ATTRIBUTE, False):
                _log_agy_error(
                    error,
                    operation="agy_complete",
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                    attempt=attempt or None,
                    json_mode=json_mode,
                    argv=argv,
                )
            raise
