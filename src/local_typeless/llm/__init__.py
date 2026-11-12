"""Remote LLM clients."""
from __future__ import annotations

from local_typeless.llm.base import LLMClient
from local_typeless.types import LLMProfileConfig


def create_llm_client(profile: LLMProfileConfig) -> LLMClient:
    if profile.provider == "openai_compatible":
        from local_typeless.llm.openai_compat import OpenAICompatibleClient

        return OpenAICompatibleClient(profile)
    if profile.provider == "gateway":
        from local_typeless.llm.gateway_adapter import GatewayAdapter

        return GatewayAdapter(profile)
    raise ValueError(f"Unknown LLM provider: {profile.provider!r}")


__all__ = ["LLMClient", "create_llm_client"]
