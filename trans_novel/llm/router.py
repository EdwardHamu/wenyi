"""基于 Tier 路由到不同 Provider 客户端的 RoutedLLMClient。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..config import Config, TierConfig
from .base import (
    ConversationCompletion,
    JsonConversationCompletion,
    LLMClient,
    Messages,
    NativeConversation,
    NativeConversationInvalidError,
    RequestEvent,
)
from .tiers import resolve_tier_name


class RoutedLLMClient(LLMClient):
    """根据 tier 将请求路由至对应 provider 的客户端。"""

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.tiers = config.llm.tiers
        if "strong" not in self.tiers:
            raise ValueError("配置缺少 llm.tiers.strong.model")
        self._tier_to_provider: dict[str, str] = {}
        self.tier_providers: dict[str, str] = {}
        self.sub_clients: dict[str, LLMClient] = {}
        self._init_sub_clients()

    def _init_sub_clients(self) -> None:
        from .factory import create_provider_client

        provider_tiers: dict[str, dict[str, TierConfig]] = {}

        for tier_name, tier_cfg in self.tiers.items():
            prov = (tier_cfg.provider or self.config.llm.provider).strip().lower().replace("_", "-")
            self._tier_to_provider[tier_name] = prov
            self.tier_providers[tier_name] = prov

            resolved_tier = tier_cfg.model_copy(
                update={
                    "provider": prov,
                    "base_url": tier_cfg.base_url or self.config.llm.base_url,
                    "api_key_env": tier_cfg.api_key_env or self.config.llm.api_key_env,
                    "cli_path": tier_cfg.cli_path or self.config.llm.cli_path,
                    "reasoning_style": tier_cfg.reasoning_style or self.config.llm.reasoning_style,
                }
            )
            provider_tiers.setdefault(prov, {})[tier_name] = resolved_tier

        for prov, tiers_dict in provider_tiers.items():
            has_strong = "strong" in tiers_dict
            sub_cfg = self.config.llm.model_copy(
                update={
                    "provider": prov,
                    "tiers": tiers_dict,
                }
            )
            sub_client = create_provider_client(prov, sub_cfg, require_strong=has_strong)
            sub_client.usage = self.usage.bind(sub_client.provider_name or prov)
            self.sub_clients[prov] = sub_client

    def set_event_sink(self, sink: Callable[..., None] | None) -> None:
        super().set_event_sink(sink)
        for client in self.sub_clients.values():
            client.set_event_sink(sink)

    def set_status_listener(self, listener: Callable[[RequestEvent], None] | None) -> None:
        super().set_status_listener(listener)
        for client in self.sub_clients.values():
            client.set_status_listener(listener)

    def owns_conversation(self, handle: NativeConversation) -> bool:
        return any(client.owns_conversation(handle) for client in self.sub_clients.values())

    def _conversation_client(self, handle: NativeConversation) -> LLMClient:
        matches = [
            client for client in self.sub_clients.values() if client.owns_conversation(handle)
        ]
        if len(matches) != 1:
            raise NativeConversationInvalidError("native conversation is not owned by this router")
        return matches[0]

    def start_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        effective_tier = resolve_tier_name(self.tiers, tier)
        prov = self._tier_to_provider[effective_tier]
        return self.sub_clients[prov].start_conversation(
            messages,
            tier=effective_tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )

    def start_json_conversation(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> JsonConversationCompletion:
        effective_tier = resolve_tier_name(self.tiers, tier)
        prov = self._tier_to_provider[effective_tier]
        client = self.sub_clients[prov]
        try:
            return client.start_json_conversation(
                messages,
                tier=effective_tier,
                max_tokens=max_tokens,
                stage=stage,
            )
        finally:
            self._request_context.last_json_response = client.last_json_response()
            self._request_context.last_json_request_id = client.last_json_request_id()

    def continue_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> ConversationCompletion:
        return self._conversation_client(handle).continue_conversation(
            handle,
            user_message,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )

    def continue_json_conversation(
        self,
        handle: NativeConversation,
        user_message: str,
        *,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> JsonConversationCompletion:
        client = self._conversation_client(handle)
        try:
            return client.continue_json_conversation(
                handle,
                user_message,
                max_tokens=max_tokens,
                stage=stage,
            )
        finally:
            self._request_context.last_json_response = client.last_json_response()
            self._request_context.last_json_request_id = client.last_json_request_id()

    def close_conversation(self, handle: NativeConversation) -> None:
        self._conversation_client(handle).close_conversation(handle)

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        effective_tier = resolve_tier_name(self.tiers, tier)
        prov = self._tier_to_provider[effective_tier]
        sub_client = self.sub_clients[prov]
        return sub_client.complete(
            messages,
            tier=effective_tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> Any:
        effective_tier = resolve_tier_name(self.tiers, tier)
        prov = self._tier_to_provider[effective_tier]
        sub_client = self.sub_clients[prov]
        result = sub_client.complete_json(
            messages,
            tier=effective_tier,
            max_tokens=max_tokens,
            stage=stage,
        )
        self._request_context.last_json_response = sub_client.last_json_response()
        self._request_context.last_json_request_id = sub_client.last_json_request_id()
        return result

    def validate_credentials(self, tiers: Sequence[str] | None = None) -> None:
        target_tiers = tiers if tiers is not None else list(self.tiers.keys())
        client_tiers: dict[LLMClient, set[str]] = {}
        for t in target_tiers:
            effective_tier = resolve_tier_name(self.tiers, t)
            prov = self._tier_to_provider.get(effective_tier)
            if prov is not None:
                sub_client = self.sub_clients[prov]
                client_tiers.setdefault(sub_client, set()).add(effective_tier)

        for sub_client, sub_tiers in client_tiers.items():
            sub_client.validate_credentials(list(sub_tiers))
