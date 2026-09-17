"""多 LLM 优先级调度与审计自动切换客户端。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from ..config import Config, LLMConfig
from .base import LLMClient, Messages, RequestEvent
from .providers.pi import (
    AUDIT_ALERT_TOTAL_TIMEOUT,
    PiAuditError,
    _dispatch_audit_notifications,
    _handle_audit_error,
    _record_audit_notification_failure,
)
from .usage import UsageTracker

_LOGGER = logging.getLogger(__name__)

AUDIT_RECOVERY_SECONDS: float = 600.0


def _build_single_client(cfg: LLMConfig, base_config: Config) -> LLMClient:
    """为单项 LLMConfig 构造对应的底层客户端。"""
    has_tier_provider = any(t.provider is not None for t in cfg.tiers.values())
    if not has_tier_provider:
        from .factory import create_provider_client

        return create_provider_client(cfg.provider, cfg)

    from .router import RoutedLLMClient

    sub_config = base_config.model_copy(update={"llm_list": [cfg], "llm_priority": "0"})
    return RoutedLLMClient(sub_config)


def _enable_raise_on_audit(client: LLMClient) -> None:
    """递归为 Pi 客户端开启 raise_on_audit。"""
    if hasattr(client, "raise_on_audit"):
        setattr(client, "raise_on_audit", True)
    if hasattr(client, "sub_clients") and isinstance(client.sub_clients, dict):
        for sub in client.sub_clients.values():
            _enable_raise_on_audit(sub)


def _set_config_index(client: LLMClient, idx: int) -> None:
    """递归为客户端及其子客户端注入对应的全局配置索引。"""
    client.config_index = idx
    if hasattr(client, "sub_clients") and isinstance(client.sub_clients, dict):
        for sub in client.sub_clients.values():
            _set_config_index(sub, idx)


def _bind_client_usage(parent_usage: UsageTracker, client: LLMClient) -> None:
    """递归把子客户端的用量统计重定向到主客户端的聚合 Tracker。"""
    if hasattr(client, "sub_clients") and isinstance(client.sub_clients, dict):
        client.usage = parent_usage
        for _prov, sub in client.sub_clients.items():
            _bind_client_usage(parent_usage, sub)
    else:
        prov = (
            getattr(client, "provider_name", None)
            or (getattr(client, "cfg", None) and getattr(client.cfg, "provider", None))
            or "unknown"
        )
        client.usage = parent_usage.bind(prov)


def _validate_client_credentials(client: LLMClient, tiers: Sequence[str] | None = None) -> None:
    """校验单客户端凭据，包括 CLI 路径存在性。"""
    client.validate_credentials(tiers)
    if hasattr(client, "sub_clients") and isinstance(client.sub_clients, dict):
        for sub in client.sub_clients.values():
            _validate_client_credentials(sub, tiers)
    elif hasattr(client, "_ensure_cli_path") and hasattr(client, "tiers"):
        target_tiers = tiers if tiers is not None else list(client.tiers.keys())
        for t in target_tiers:
            if t in client.tiers:
                client._ensure_cli_path(client.tiers[t])


class PriorityLLMClient(LLMClient):
    """支持优先级顺序、Pi 审计自动降级和静默期自动恢复的多配置 LLM 客户端。"""

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.provider_name = "Priority"
        self._priority_order: list[int] = [int(ch) for ch in config.llm_priority]
        self._current_step: int = 0
        self._last_audit_time: float | None = None
        self._lock = threading.Lock()
        self._clients: dict[int, LLMClient] = {}
        self._init_sub_clients()

    def _init_sub_clients(self) -> None:
        for idx in self._priority_order:
            cfg = self.config.llm_list[idx]
            client = _build_single_client(cfg, self.config)
            _set_config_index(client, idx)
            _enable_raise_on_audit(client)
            _bind_client_usage(self.usage, client)
            self._clients[idx] = client

    @property
    def current_config_index(self) -> int:
        """返回当前活跃配置在 llm_list 中的 0-based 索引。"""
        with self._lock:
            return self._priority_order[self._current_step]

    def set_event_sink(self, sink: Callable[..., None] | None) -> None:
        super().set_event_sink(sink)
        for client in self._clients.values():
            client.set_event_sink(sink)

    def set_status_listener(self, listener: Callable[[RequestEvent], None] | None) -> None:
        super().set_status_listener(listener)
        for client in self._clients.values():
            client.set_status_listener(listener)

    def _notify_status_config_index(self, idx: int) -> None:
        listener = self._status_listener
        if listener is not None and hasattr(listener, "set_config_index"):
            listener.set_config_index(idx)

    def validate_credentials(self, tiers: Sequence[str] | None = None) -> None:
        for idx in self._priority_order:
            _validate_client_credentials(self._clients[idx], tiers)

    def _check_recovery_under_lock(self) -> None:
        """若已跨过 10 分钟静默期且当前处于降级档位，恢复为首选配置。"""
        if self._current_step > 0 and self._last_audit_time is not None:
            elapsed = time.monotonic() - self._last_audit_time
            if elapsed >= AUDIT_RECOVERY_SECONDS:
                old_step = self._current_step
                self._current_step = 0
                self._last_audit_time = None
                from_idx = self._priority_order[old_step]
                to_idx = self._priority_order[0]
                self._notify_status_config_index(to_idx)
                self._emit_event(
                    "llm_quiet_period_recovery",
                    from_index=from_idx,
                    to_index=to_idx,
                    elapsed=elapsed,
                    reason="quiet_period_elapsed",
                )

    def _handle_audit_failover(
        self,
        *,
        step: int,
        operation: str,
        stage: str | None,
        tier: str | None,
        error: PiAuditError,
    ) -> None:
        """处理 Pi 审计异常：并发只降级一次，最低档强制退出，其余档推进并通知。"""
        with self._lock:
            if self._current_step != step:
                # 并发请求中已有其他线程完成了本档位的降级，直接重放
                return

            if self._current_step >= len(self._priority_order) - 1:
                # 已是最低优先级配置，按旧行为退出
                _handle_audit_error(
                    operation=operation,
                    stage=stage,
                    tier=tier,
                    error=error,
                )
                raise error

            old_step = self._current_step
            self._current_step += 1
            self._last_audit_time = time.monotonic()
            from_idx = self._priority_order[old_step]
            to_idx = self._priority_order[self._current_step]
            self._notify_status_config_index(to_idx)

        # 锁外触发事件与告警通知，避免阻塞并发重试线程
        self._emit_event(
            "llm_audit_downgrade",
            from_index=from_idx,
            to_index=to_idx,
            reason=str(error),
            stage=stage,
            tier=tier,
        )

        try:
            _dispatch_audit_notifications(
                operation=operation,
                stage=stage,
                tier=tier,
                error=error,
                timeout=AUDIT_ALERT_TOTAL_TIMEOUT,
            )
        except Exception as exc:
            _record_audit_notification_failure("降级通知", exc)

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        while True:
            with self._lock:
                self._check_recovery_under_lock()
                step = self._current_step
                cfg_idx = self._priority_order[step]
                client = self._clients[cfg_idx]

            try:
                return client.complete(
                    messages,
                    tier=tier,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    stage=stage,
                )
            except PiAuditError as audit_err:
                self._handle_audit_failover(
                    step=step,
                    operation="complete",
                    stage=stage,
                    tier=tier,
                    error=audit_err,
                )
                continue

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> Any:
        while True:
            with self._lock:
                self._check_recovery_under_lock()
                step = self._current_step
                cfg_idx = self._priority_order[step]
                client = self._clients[cfg_idx]

            try:
                try:
                    result = client.complete_json(
                        messages,
                        tier=tier,
                        max_tokens=max_tokens,
                        stage=stage,
                    )
                    return result
                finally:
                    self._request_context.last_json_response = client.last_json_response()
                    self._request_context.last_json_request_id = client.last_json_request_id()
            except PiAuditError as audit_err:
                self._handle_audit_failover(
                    step=step,
                    operation="complete_json",
                    stage=stage,
                    tier=tier,
                    error=audit_err,
                )
                continue
