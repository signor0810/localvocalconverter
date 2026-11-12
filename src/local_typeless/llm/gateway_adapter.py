"""Internal gateway adapter.

Defaults to the OpenAI payload format. When the real gateway format is known,
only `_transform_request` and `_extract_response` need to change.
"""
from __future__ import annotations

from typing import Any, Dict

from local_typeless.llm.openai_compat import OpenAICompatibleClient


class GatewayAdapter(OpenAICompatibleClient):
    def _transform_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Hook: reshape the OpenAI-style payload into the gateway schema.
        return payload

    def _extract_response(self, data: Any) -> str:
        # Hook: pull the text out of the gateway response schema.
        return super()._extract_response(data)
