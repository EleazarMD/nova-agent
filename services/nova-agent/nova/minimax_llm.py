"""
MiniMax LLM Service — Pipecat adapter with streaming tool_call.index fix.

MiniMax sends tool_call.index=None in streaming chunks.
Pipecat's _process_context compares tool_call.index != func_idx (int),
so None != 0 incorrectly triggers the multi-tool branch.
Fix: wrap the stream to normalize index=None → index=0.

Also sanitizes Unicode surrogates from iOS speech recognition that cause
JSON encoding errors ('utf-8' codec can't encode surrogates).
"""

import os
import httpx
from pipecat.services.openai.llm import OpenAILLMService
from openai import AsyncOpenAI


def _sanitize_surrogates(obj):
    """Recursively remove Unicode surrogates from strings in dicts/lists."""
    if isinstance(obj, str):
        # Remove surrogate pairs that can't be encoded to UTF-8
        return obj.encode('utf-8', errors='surrogateescape').decode('utf-8', errors='replace')
    elif isinstance(obj, dict):
        return {k: _sanitize_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_surrogates(item) for item in obj]
    return obj


class MiniMaxLLMService(OpenAILLMService):
    """Patches tool_call.index for MiniMax streaming compatibility.
    
    Also adds X-Budget-Override header to bypass AI Gateway budget limits
    for internal LLM calls (MiniMax is a local model, not a paid API).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Get the budget override key (same as admin API key)
        budget_override_key = os.environ.get(
            "AI_GATEWAY_BUDGET_OVERRIDE",
            os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-admin-key-2024")
        )
        # Recreate the client with the X-Budget-Override header
        self._client = AsyncOpenAI(
            api_key=kwargs.get("api_key", ""),
            base_url=kwargs.get("base_url", ""),
            http_client=httpx.AsyncClient(
                headers={"X-Budget-Override": budget_override_key}
            ),
        )

    def set_thinking(self, level: str):
        """Dynamically change the thinking level for subsequent LLM calls.

        Levels: 'low' (fast, no thinking), 'medium' (brief), 'high' (extended 16K).
        """
        # Pipecat stores InputParams.extra_body in self._settings.extra dict
        if hasattr(self, '_settings') and hasattr(self._settings, 'extra'):
            self._settings.extra["extra_body"] = {"thinking": level}
        else:
            # Fallback: try direct attribute
            if hasattr(self, '_params') and hasattr(self._params, 'extra_body'):
                self._params.extra_body["thinking"] = level

    async def get_chat_completions(self, params_from_context):
        params = self.build_chat_completion_params(params_from_context)
        # Sanitize surrogates from iOS speech recognition text
        if "messages" in params:
            params["messages"] = _sanitize_surrogates(params["messages"])
        raw_stream = await self._client.chat.completions.create(**params)

        class _PatchedStream:
            """Normalizes tool_call.index=None→0 for Pipecat compatibility."""
            def __init__(self, raw):
                self._raw = raw
            def __aiter__(self):
                return self._iter()
            async def _iter(self):
                async for chunk in self._raw:
                    if chunk.choices:
                        for c in chunk.choices:
                            if c.delta and c.delta.tool_calls:
                                for tc in c.delta.tool_calls:
                                    if tc.index is None:
                                        tc.index = 0
                    yield chunk
            async def close(self):
                if hasattr(self._raw, "close"):
                    await self._raw.close()
            async def aclose(self):
                if hasattr(self._raw, "aclose"):
                    await self._raw.aclose()
                elif hasattr(self._raw, "close"):
                    await self._raw.close()

        return _PatchedStream(raw_stream)
