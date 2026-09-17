"""线程安全的 LLM 请求状态事件与 CLI 快照。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Literal

RequestState = Literal["requesting", "success", "timeout", "error"]

REQUESTING: RequestState = "requesting"
SUCCESS: RequestState = "success"
TIMEOUT: RequestState = "timeout"
ERROR: RequestState = "error"


@dataclass(frozen=True)
class RequestEvent:
    """一次逻辑模型请求或其重试尝试的状态快照。"""

    request_id: int
    status: RequestState
    tier: str
    stage: str | None
    attempt: int
    elapsed: float
    error_type: str | None = None
    error_message: str | None = None
    provider: str | None = None
    config_index: int = 0

    @property
    def failed(self) -> bool:
        """当前请求是否以超时或错误结束。"""
        return self.status in {TIMEOUT, ERROR}


@dataclass(frozen=True)
class RequestSnapshot:
    """供 UI 读取的当前请求和上一次完成请求。"""

    current: RequestEvent | None
    previous: RequestEvent | None
    active_count: int
    config_index: int = 0


RequestStatusListener = Callable[[RequestEvent], None]


def classify_error(error: BaseException) -> RequestState:
    """把不同 provider 的超时异常归一化为 ``timeout``。"""
    if isinstance(error, TimeoutError):
        return TIMEOUT
    error_name = type(error).__name__.lower()
    error_module = type(error).__module__.lower()
    if "timeout" in error_name or "timedout" in error_name:
        return TIMEOUT
    if "timeout" in error_module or "timedout" in error_module:
        return TIMEOUT
    return ERROR


def error_details(error: BaseException, *, max_length: int = 240) -> tuple[str, str]:
    """返回适合终端展示的异常类型和单行短消息。"""
    error_type = type(error).__name__
    message = " ".join(str(error).split()) or error_type
    return error_type, message[:max_length]


def monotonic_time() -> float:
    """隔离时钟调用，便于测试替换。"""
    return time.monotonic()


class RequestStatusTracker:
    """收集并发 LLM 请求状态，保留最近两个逻辑请求结果。"""

    def __init__(self, default_config_index: int = 0) -> None:
        self._lock = Lock()
        self._active: dict[int, RequestEvent] = {}
        self._completed: list[RequestEvent] = []
        self._current_config_index: int = default_config_index

    @property
    def current_config_index(self) -> int:
        with self._lock:
            return self._current_config_index

    def set_config_index(self, index: int) -> None:
        with self._lock:
            self._current_config_index = index

    def __call__(self, event: RequestEvent) -> None:
        """作为 ``LLMClient`` listener 接收事件。"""
        self.handle(event)

    def handle(self, event: RequestEvent) -> None:
        """合并一个事件；同一 request_id 的重试结果保留最后一次结果。"""
        with self._lock:
            self._current_config_index = event.config_index
            if event.status == REQUESTING:
                self._active[event.request_id] = event
                return

            self._active.pop(event.request_id, None)
            for index, previous in enumerate(self._completed):
                if previous.request_id == event.request_id:
                    self._completed.pop(index)
                    break
            self._completed.insert(0, event)
            del self._completed[2:]

    def snapshot(self) -> RequestSnapshot:
        """在线程安全快照中选择最新活动请求和最近完成结果。"""
        with self._lock:
            active = tuple(sorted(self._active.values(), key=lambda event: event.request_id))
            current = active[-1] if active else (self._completed[0] if self._completed else None)
            current_id = current.request_id if current else None
            previous = next(
                (event for event in self._completed if event.request_id != current_id),
                None,
            )
            return RequestSnapshot(
                current=current,
                previous=previous,
                active_count=len(active),
                config_index=self._current_config_index,
            )

    def reset(self) -> None:
        """清除当前 CLI 运行的状态。"""
        with self._lock:
            self._active.clear()
            self._completed.clear()
