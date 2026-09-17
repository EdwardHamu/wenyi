"""根据配置创建内置 LLM provider。"""

from __future__ import annotations

from ..config import Config, LLMConfig
from .base import LLMClient


def create_provider_client(
    provider: str,
    cfg: LLMConfig,
    *,
    require_strong: bool = True,
) -> LLMClient:
    """根据 provider 名称延迟导入并构造对应客户端。"""
    p = provider.strip().lower().replace("_", "-")
    if p == "deepseek":
        from .providers.deepseek import DeepSeekClient

        return DeepSeekClient(cfg, require_strong=require_strong)
    if p == "openai":
        from .providers.openai import OpenAIClient

        return OpenAIClient(cfg, require_strong=require_strong)
    if p == "anthropic":
        from .providers.anthropic import AnthropicClient

        return AnthropicClient(cfg, require_strong=require_strong)
    if p == "codex":
        from .providers.codex import CodexClient

        return CodexClient(cfg, require_strong=require_strong)
    if p == "pi":
        from .providers.pi import PiClient

        return PiClient(cfg, require_strong=require_strong)
    if p in ("codebuddy", "code-buddy"):
        from .providers.codebuddy import CodeBuddyClient

        return CodeBuddyClient(cfg, require_strong=require_strong)
    if p == "agy":
        from .providers.agy import AgyClient

        return AgyClient(cfg, require_strong=require_strong)
    if p == "openrouter":
        from .providers.openrouter import OpenRouterClient

        return OpenRouterClient(cfg, require_strong=require_strong)
    if p in ("orcarouter", "orca-router"):
        from .providers.orcarouter import OrcaRouterClient

        return OrcaRouterClient(cfg, require_strong=require_strong)
    if p == "openai-compatible":
        from .providers.openai_compatible import OpenAICompatibleClient

        return OpenAICompatibleClient(cfg, require_strong=require_strong)
    if p == "ollama":
        from .providers.ollama import OllamaClient

        return OllamaClient(cfg, require_strong=require_strong)
    if p == "vllm":
        from .providers.vllm import VLLMClient

        return VLLMClient(cfg, require_strong=require_strong)
    if p in ("gemini", "google"):
        from .providers.gemini import GeminiClient

        return GeminiClient(cfg, require_strong=require_strong)
    if p == "fake":
        from .providers.fake import FakeClient

        return FakeClient()
    raise ValueError(
        f"未知 provider：{provider}"
        "（支持 deepseek / openai / anthropic / codex / pi / codebuddy / agy / openrouter / "
        "orcarouter / "
        "openai-compatible / ollama / vllm / gemini / fake）"
    )


def build_client(config: Config) -> LLMClient:
    """根据 llm.provider 或 tier provider 覆盖延迟导入并构造对应客户端。"""
    if len(config.llm_list) > 1:
        from .priority import PriorityLLMClient

        return PriorityLLMClient(config)

    has_tier_provider = any(t.provider is not None for t in config.llm.tiers.values())
    if not has_tier_provider:
        return create_provider_client(config.llm.provider, config.llm)

    from .router import RoutedLLMClient

    return RoutedLLMClient(config)
