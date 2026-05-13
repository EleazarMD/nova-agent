import json
import os
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from loguru import logger


ROUTABLE_INTENTS = {
    "calendar_lookup",
    "email_lookup",
    "weather_lookup",
    "current_events_lookup",
    "personal_memory_recall",
    "conversation_recall",
}

ROUTING_CONFIDENCE_THRESHOLD = 0.72


@dataclass
class SemanticTurnResolution:
    intent: str = "pass_through"
    confidence: float = 0.0
    references_previous_turn: bool = False
    resolved_query: str = ""
    allowed_tool: str = ""
    suggested_tools: list[str] = field(default_factory=list)
    grounded_in_transcript: bool = False
    rationale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemanticTurnResolution":
        confidence = data.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.0
        raw_tools = data.get("suggested_tools") or []
        suggested_tools = [str(t) for t in raw_tools if str(t or "").strip()] if isinstance(raw_tools, list) else []
        return cls(
            intent=str(data.get("intent") or "pass_through"),
            confidence=max(0.0, min(1.0, confidence)),
            references_previous_turn=bool(data.get("references_previous_turn", False)),
            resolved_query=str(data.get("resolved_query") or ""),
            allowed_tool=str(data.get("allowed_tool") or ""),
            suggested_tools=suggested_tools,
            grounded_in_transcript=bool(data.get("grounded_in_transcript", False)),
            rationale=str(data.get("rationale") or ""),
            raw=data,
        )

    def is_routable(self, threshold: float = ROUTING_CONFIDENCE_THRESHOLD) -> bool:
        """True when the resolver is confident enough to drive a deterministic route."""
        return self.intent in ROUTABLE_INTENTS and self.confidence >= threshold

    def is_actionable_conversation_recall(self) -> bool:
        return (
            self.intent == "conversation_recall"
            and self.confidence >= 0.65
            and self.references_previous_turn
            and self.grounded_in_transcript
            and self.allowed_tool == "search_past_conversations"
            and bool(self.resolved_query.strip())
        )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(stripped[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _compact_recent_transcript(recent_messages: list[dict[str, Any]], max_messages: int = 8) -> list[dict[str, str]]:
    compacted: list[dict[str, str]] = []
    for msg in recent_messages[-max_messages:]:
        role = str(msg.get("role") or "")
        content = " ".join(str(msg.get("content") or "").split())[:900]
        if role in {"user", "assistant"} and content:
            compacted.append({"role": role, "content": content})
    return compacted


async def resolve_semantic_turn(
    *,
    current_text: str,
    recent_messages: list[dict[str, Any]],
    model: str | None = None,
    ai_gateway_url: str | None = None,
    api_key: str | None = None,
    timeout_secs: float = 8.0,
) -> SemanticTurnResolution | None:
    transcript = _compact_recent_transcript(recent_messages)
    if not transcript or not current_text.strip():
        return None

    ai_gateway_url = (ai_gateway_url or os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/v1")).rstrip("/")
    api_key = api_key or os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
    model = model or os.environ.get("LLM_MODEL", "minimax-m2.7")
    prompt = {
        "task": (
            "Classify the user's current turn into the best matching intent. "
            "Return a single JSON object only. Do not answer the user or call tools."
        ),
        "allowed_intents": [
            "calendar_lookup",
            "email_lookup",
            "weather_lookup",
            "current_events_lookup",
            "personal_memory_recall",
            "conversation_recall",
            "context_continuation",
            "pass_through",
            "clarification",
        ],
        "intent_guide": {
            "calendar_lookup": "user asks about their schedule, events, appointments, plans, where they're going, what's tonight",
            "email_lookup": "user asks to find, check, or look up an email, message, inbox, order confirmation, receipt, tracking",
            "weather_lookup": "user asks about weather, temperature, forecast, rain",
            "current_events_lookup": "user asks about real-time external facts: news, concerts, sports, prices, store hours, anything requiring a web search",
            "personal_memory_recall": "user asks what Nova remembers about them, their preferences, goals, stored facts",
            "conversation_recall": "user references something discussed earlier in this or a prior conversation",
            "context_continuation": "user is following up on the last assistant response without a new topic",
            "pass_through": "general chat, opinion, creative, or anything not in the above categories",
            "clarification": "user's intent is genuinely ambiguous and a clarifying question is needed",
        },
        "schema": {
            "intent": "one of the allowed_intents",
            "confidence": "0.0-1.0 — how certain you are",
            "references_previous_turn": "boolean — does this reference something from the transcript",
            "resolved_query": "the specific search/lookup query to pass to the tool, empty if not needed",
            "suggested_tools": ["list of tool names: check_studio, query_cig, get_weather, web_search, recall_memory, search_past_conversations"],
            "grounded_in_transcript": "boolean — is resolved_query supported by the transcript",
            "rationale": "one sentence",
        },
        "recent_transcript": transcript,
        "current_user_text": current_text,
        "policy": (
            "Pick the most specific applicable intent. "
            "Only use pass_through or clarification when nothing more specific fits. "
            "Set confidence < 0.72 when genuinely unsure so the orchestrator can fall back to keyword rules. "
            "resolved_query should be a clean, tool-ready query string."
        ),
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are Nova's semantic turn resolver. Output a single JSON object only."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "stream": False,
        "max_tokens": 700,
        "temperature": 0.0,
        "extra_body": {"thinking": "low"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Budget-Override": os.environ.get("AI_GATEWAY_BUDGET_OVERRIDE", api_key),
    }
    url = f"{ai_gateway_url}/chat/completions"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=timeout_secs)) as resp:
                if resp.status != 200:
                    logger.warning(f"NOVA_SEMANTIC_RESOLVER_FAILED | status={resp.status} body={(await resp.text())[:400]}")
                    return None
                data = await resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _extract_json_object(content)
        if not parsed:
            logger.warning(f"NOVA_SEMANTIC_RESOLVER_PARSE_FAILED | content={content[:400]}")
            return None
        resolution = SemanticTurnResolution.from_dict(parsed)
        logger.info(
            "NOVA_SEMANTIC_RESOLVER | "
            f"intent={resolution.intent} confidence={resolution.confidence:.3f} "
            f"references_previous_turn={resolution.references_previous_turn} "
            f"grounded={resolution.grounded_in_transcript} tool={resolution.allowed_tool}"
        )
        return resolution
    except Exception as e:
        logger.warning(f"NOVA_SEMANTIC_RESOLVER_ERROR | {e}")
        return None
