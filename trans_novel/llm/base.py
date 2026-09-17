"""LLM provider 的稳定抽象接口。"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import contextmanager
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
