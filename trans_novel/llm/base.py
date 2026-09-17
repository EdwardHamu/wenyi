"""LLM provider 的稳定抽象接口。"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, local
from typing import Any, Iterator

from .json_parser import parse_json_loose
from .status import (
    ERROR,
    REQUESTING,
    SUCCESS,
    RequestEvent,
    RequestStatusListener,
    classify_error,
    error_details,
    monotonic_time,
)
from .usage import UsageTracker

Messages = list[dict[str, str]]
EventSink = Callable[..., None]
_LOGGER = logging.getLogger(__name__)


class NativeConversationError(RuntimeError):
    """原生 provider 会话不可安全继续。"""


class NativeConversationUnsupportedError(NativeConversationError):
    """当前 provider 不支持原生会话续接。"""


class NativeConversationInvalidError(NativeConversationError):
    """会话 handle 已失效、已消费或属于其它 client。"""


class NativeConversationBusyError(NativeConversationInvalidError):
    """同一会话正在被另一个线程消费。"""


class NativeConversationRouteChangedError(NativeConversationInvalidError):
    """优先级或 provider 路由已变化，不能再声称续接原会话。"""


def conversation_fingerprint(messages: Messages) -> str:
    """仅返回消息前缀哈希，供 handle 校验和无正文日志使用。"""
    payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class NativeConversation:
    """一次翻译批次独占、最多消费一次的 provider opaque 会话 handle。"""

    provider: str
    config_index: int
    tier: str
    model: str
    native_id: str
    cwd: str
    prefix_hash: str
    _owner: object = field(repr=False, compare=False)
    payload: Any = field(default=None, repr=False, compare=False)
    _state: str = field(default="open", init=False, repr=False, compare=False)
    _consumer_thread_id: int | None = field(default=None, init=False, repr=False, compare=False)
    _state_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def id_hash(self) -> str:
        return hashlib.sha256(self.native_id.encode("utf-8")).hexdigest()[:16]

    def belongs_to(self, owner: object) -> bool:
        return self._owner is owner

    def claim(self, owner: object) -> None:
        """原子地取得该一次性 handle；并发或重复消费直接拒绝。"""
        with self._state_lock:
            if self._owner is not owner:
                raise NativeConversationInvalidError(
                    "native conversation belongs to another client"
                )
            if self._state == "in_use":
                raise NativeConversationBusyError("native conversation is already in use")
            if self._state != "open":
                raise NativeConversationInvalidError("native conversation is already closed")
            self._state = "in_use"
            self._consumer_thread_id = threading.get_ident()

    def mark_closed(self, owner: object) -> bool:
        """幂等关闭，返回本次调用是否首次完成关闭。"""
        with self._state_lock:
            if self._owner is not owner:
                raise NativeConversationInvalidError(
                    "native conversation belongs to another client"
                )
            if self._state == "closed":
                return False
            if self._state == "in_use" and self._consumer_thread_id != threading.get_ident():
                raise NativeConversationBusyError(
                    "native conversation can only be closed by its active consumer"
                )
            self._state = "closed"
            self._consumer_thread_id = None
            return True


@dataclass(frozen=True, slots=True)
class ConversationCompletion:
    text: str
    handle: NativeConversation | None = None
    request_id: int | None = None


@dataclass(frozen=True, slots=True)
class JsonConversationCompletion:
    data: Any
    text: str
    handle: NativeConversation | None = None
    request_id: int | None = None


class LLMClient(ABC):
    """所有 provider 实现此接口。"""

    def __init__(self) -> None:
        """为 provider 初始化独立的用量统计器和可选事件出口。"""
        self.provider_name: str | None = None
        self.config_index: int = 0
        self.usage = UsageTracker()
        self._status_listener: RequestStatusListener | None = None

        self._status_lock = Lock()
        self._request_context = local()
        self._next_request_id = 0
        self._event_sink: EventSink | None = None
        self._event_sink_lock = threading.Lock()

    def set_event_sink(self, sink: EventSink | None) -> None:
        """绑定运行事件出口；Orchestrator 用它把重试实时写入书籍日志。"""
        with self._event_sink_lock:
            self._event_sink = sink

    def _emit_event(self, event: str, **data: Any) -> None:
        """线程安全地发送 provider 事件，日志失败不得掩盖原始模型异常。"""
        with self._event_sink_lock:
            sink = self._event_sink
            if sink is None:
                return
            try:
                sink(event, **data)
            except Exception:  # noqa: BLE001 - 可观察性故障不能改变模型调用语义
                _LOGGER.exception("Failed to write LLM event: %s", event)

    def emit_event(self, event: str, **data: Any) -> None:
        """发布 Agent 层诊断事件到当前运行的事件日志。"""
        self._emit_event(event, **data)

    def usage_summary(self) -> dict[str, Any]:
        """返回累计 token 用量快照（totals + by_tier + cache_hit_rate）。"""
        return self.usage.summary()

    def set_status_listener(
        self, listener: RequestStatusListener | None
    ) -> RequestStatusListener | None:
        """安装请求状态监听器并返回之前的监听器。"""
        with self._status_lock:
            previous = self._status_listener
            self._status_listener = listener
            return previous

    def _new_request_id(self) -> int:
        """为一次 complete() 调用分配稳定 ID，重试共用该 ID。"""
        with self._status_lock:
            self._next_request_id += 1
            return self._next_request_id

    def _request_context_id(self) -> int | None:
        """返回当前 complete() 请求 ID，供 JSON 解析失败沿用。"""
        return getattr(self._request_context, "request_id", None)

    def _last_request_id(self) -> int | None:
        """返回本线程最近一次实际请求 ID。"""
        return getattr(self._request_context, "last_request_id", None)

    def last_json_response(self) -> str | None:
        """返回本线程最近一次 ``complete_json`` 的原始响应，供协议诊断记录。"""
        return getattr(self._request_context, "last_json_response", None)

    def last_json_request_id(self) -> int | None:
        """返回 ``last_json_response`` 对应的 provider 请求 ID。"""
        return getattr(self._request_context, "last_json_request_id", None)

    def _notify_status(self, event: RequestEvent) -> None:
        """通知 UI；监听器故障不得影响翻译请求。"""
        with self._status_lock:
            listener = self._status_listener
        if listener is None:
            return
        try:
            listener(event)
        except Exception:
            # CLI 状态是旁路观测能力，不能让 Rich 或用户回调中断翻译。
            return

    @contextmanager
    def request_status(
        self,
        *,
        tier: str,
        stage: str | None,
        request_id: int | None = None,
        attempt: int = 1,
        provider: str | None = None,
    ) -> Iterator[int]:
        """标记一次实际 provider 尝试，并将异常原样重新抛出。"""
        request_id = request_id if request_id is not None else self._new_request_id()
        started = monotonic_time()
        previous_context_id = self._request_context_id()
        self._request_context.request_id = request_id
        self._request_context.last_request_id = request_id
        effective_provider = provider or getattr(self, "provider_name", None)
        cfg_index = getattr(self, "config_index", 0)
        self._notify_status(
            RequestEvent(
                request_id=request_id,
                status=REQUESTING,
                tier=tier,
                stage=stage,
                attempt=attempt,
                elapsed=0.0,
                provider=effective_provider,
                config_index=cfg_index,
            )
        )
        try:
            yield request_id
        except Exception as error:
            error_type, message = error_details(error)
            self._notify_status(
                RequestEvent(
                    request_id=request_id,
                    status=classify_error(error),
                    tier=tier,
                    stage=stage,
                    attempt=attempt,
                    elapsed=max(0.0, monotonic_time() - started),
                    error_type=error_type,
                    error_message=message,
                    provider=effective_provider,
                    config_index=cfg_index,
                )
            )
            raise
        else:
            self._notify_status(
                RequestEvent(
                    request_id=request_id,
                    status=SUCCESS,
                    tier=tier,
                    stage=stage,
                    attempt=attempt,
                    elapsed=max(0.0, monotonic_time() - started),
                    provider=effective_provider,
                    config_index=cfg_index,
                )
            )

        finally:
            if previous_context_id is None:
                try:
                    del self._request_context.request_id
                except AttributeError:
                    pass
            else:
                self._request_context.request_id = previous_context_id

    def _notify_complete_json_error(
        self,
        error: BaseException,
        *,
        request_id: int,
        tier: str,
        stage: str | None,
        provider: str | None = None,
    ) -> None:
        """将响应后的 JSON 解析失败更新为当前请求的错误状态。"""
        error_type, message = error_details(error)
        self._notify_status(
            RequestEvent(
                request_id=request_id,
                status=ERROR,
                tier=tier,
                stage=stage,
                attempt=1,
                elapsed=0.0,
                error_type=error_type,
                error_message=message,
                provider=provider or getattr(self, "provider_name", None),
                config_index=getattr(self, "config_index", 0),
            )
        )

    def _on_complete_json_error(
        self,
        error: BaseException,
        response: str,
        *,
        request_id: int,
        tier: str,
        stage: str | None,
    ) -> None:
        """Provider hook for retaining raw responses after JSON parsing fails."""
        return

    def validate_credentials(self, tiers: Sequence[str] | None = None) -> None:
        """校验 provider 调用所需凭据；本地或测试 provider 默认免检。"""

    def owns_conversation(self, handle: NativeConversation) -> bool:
        """返回 handle 是否由当前 client 直接拥有；路由 client 会递归覆盖。"""
        return handle.belongs_to(self)

    def start_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        """请求 provider 创建原生会话；不支持时保持普通 complete 语义。"""
        text = self.complete(
            messages,
            tier=tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )
        return ConversationCompletion(text=text, request_id=self._last_request_id())

    def continue_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        """仅发送新增 user turn；默认 provider 明确报告不支持。"""
        del handle, user_message, json_mode, max_tokens, stage
        raise NativeConversationUnsupportedError(
            f"{self.provider_name or type(self).__name__} does not support native conversations"
        )

    def close_conversation(self, handle: NativeConversation) -> None:
        """幂等释放直接拥有的 handle；provider 可覆盖以删除本地资源。"""
        handle.mark_closed(self)

    def _parse_conversation_json(
        self,
        completion: ConversationCompletion,
        *,
        tier: str,
        stage: str | None,
    ) -> JsonConversationCompletion:
        """解析带 handle 的文本结果，并沿用 complete_json 的诊断契约。"""
        text = completion.text
        request_id = completion.request_id or self._last_request_id() or self._new_request_id()
        self._request_context.last_json_response = text
        self._request_context.last_json_request_id = request_id
        try:
            data = parse_json_loose(text)
        except Exception as error:
            self._notify_complete_json_error(
                error,
                request_id=request_id,
                tier=tier,
                stage=stage,
            )
            try:
                self._on_complete_json_error(
                    error,
                    text,
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                )
            except Exception:
                pass
            if completion.handle is not None:
                try:
                    self.close_conversation(completion.handle)
                except Exception:
                    pass
            raise
        return JsonConversationCompletion(
            data=data,
            text=text,
            handle=completion.handle,
            request_id=request_id,
        )

    def start_json_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> JsonConversationCompletion:
        """创建会话并解析首轮 JSON；不支持的 provider 返回 handle=None。"""
        self._request_context.last_json_response = None
        self._request_context.last_json_request_id = None
        completion = self.start_conversation(
            messages,
            tier=tier,
            json_mode=True,
            max_tokens=max_tokens,
            stage=stage,
        )
        return self._parse_conversation_json(completion, tier=tier, stage=stage)

    def continue_json_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> JsonConversationCompletion:
        """在原生会话追加一轮 user 并解析 JSON。"""
        self._request_context.last_json_response = None
        self._request_context.last_json_request_id = None
        completion = self.continue_conversation(
            handle,
            user_message,
            json_mode=True,
            max_tokens=max_tokens,
            stage=stage,
        )
        return self._parse_conversation_json(completion, tier=handle.tier, stage=stage)

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """返回模型回复的纯文本；stage 仅用于用量归因。"""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> Any:
        """要求 JSON 输出并解析。"""
        # 每次调用先清掉本线程上一次诊断，避免传输异常时把旧响应误记到新请求。
        self._request_context.last_json_response = None
        self._request_context.last_json_request_id = None
        text = self.complete(
            messages, tier=tier, json_mode=True, max_tokens=max_tokens, stage=stage
        )
        self._request_context.last_json_response = text
        self._request_context.last_json_request_id = self._last_request_id()
        try:
            return parse_json_loose(text)
        except Exception as error:
            request_id = self._last_request_id() or self._new_request_id()
            self._notify_complete_json_error(
                error,
                request_id=request_id,
                tier=tier,
                stage=stage,
            )
            try:
                self._on_complete_json_error(
                    error,
                    text,
                    request_id=request_id,
                    tier=tier,
                    stage=stage,
                )
            except Exception:
                # Retaining diagnostics must never replace the parse exception.
                pass
            raise
