from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import json

from loguru import logger

from nova.learning import get_shadow_plan_candidates
from nova.turn_policy import normalize_turn_text
from nova.turn_tool_policy import GENERIC_LEARNING_TRIGGERS, learned_candidate_allowed


LEGACY_INTENT_MAP = {
    "web_research_request": "current_events_lookup",
    "conversation_recall_request": "conversation_recall",
    "memory_recall_request": "personal_memory_recall",
    "weather_lookup": "weather_lookup",
    "email_lookup": "email_lookup",
    "workspace_creation": "workspace_creation",
    "lookup_then_workspace_creation": "lookup_then_workspace_creation",
    "workspace_creation_continuation": "workspace_creation_continuation",
    "hub_delegate": "workflow_trigger",
}

READ_ONLY_PROMOTION_INTENTS = {
    "current_events_lookup",
    "conversation_recall",
    "personal_memory_recall",
    "weather_lookup",
    "email_lookup",
}

SIDE_EFFECT_TOOLS = {
    "save_memory",
    "forget_memory",
    "hub_delegate",
    "tesla_control",
    "manage_workspace",
    "manage_notes",
    "homelab_operations",
}

NOISY_TRIGGER_PREFIXES = (
    "i don't see",
    "i dont see",
    "what are you talking about",
    "that's not",
    "thats not",
    "that is not",
    "you didn't",
    "you did not",
    "try again",
    "wait no",
    "no stop",
    "wrong",
    "cancel",
    "nevermind",
)


@dataclass
class LearnedRouteCandidate:
    intent: str
    confidence: float
    suggested_tools: list[str] = field(default_factory=list)
    similar_examples: list[dict[str, Any]] = field(default_factory=list)
    positive_evidence: list[str] = field(default_factory=list)
    negative_evidence: list[str] = field(default_factory=list)
    safety_level: str = "unknown"
    action: str = "shadow"
    source: str = "learned_router"
    raw_candidate: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_intent(intent: str) -> str:
    return LEGACY_INTENT_MAP.get(str(intent or ""), str(intent or ""))


def _safety_level(tools: list[str], intent: str) -> str:
    if any(tool in SIDE_EFFECT_TOOLS for tool in tools):
        return "side_effect"
    if intent in READ_ONLY_PROMOTION_INTENTS:
        return "safe_read_only"
    return "unknown"


def _quality_block_reason(raw: dict[str, Any]) -> str:
    trigger = normalize_turn_text(str(raw.get("trigger_text") or ""))
    if not trigger:
        return "missing_trigger_text"
    if trigger in GENERIC_LEARNING_TRIGGERS:
        return "generic_trigger_text"
    if any(trigger.startswith(prefix) for prefix in NOISY_TRIGGER_PREFIXES):
        return "noisy_correction_or_status_trigger"
    if len(trigger.split()) < 3:
        return "trigger_too_short"
    return ""


def _text_supports_intent(text: str, intent: str) -> bool:
    normalized = normalize_turn_text(text)
    if intent == "current_events_lookup":
        return any(term in normalized for term in (
            "latest", "recent", "current", "today", "right now", "this week", "last week",
            "news", "market", "stock", "stocks", "release", "launched", "announced",
        ))
    if intent == "conversation_recall":
        return any(term in normalized for term in (
            "conversation", "discussed", "talked about", "previous", "earlier", "recall",
            "remember when", "what did we", "past conversation",
        ))
    if intent == "personal_memory_recall":
        return any(term in normalized for term in (
            "remember about me", "my preference", "my preferences", "my goal", "my goals",
            "what do you know about me", "personal memory",
        ))
    if intent == "weather_lookup":
        return any(term in normalized for term in ("weather", "forecast", "rain", "temperature", "outside", "humidity", "wind"))
    if intent == "email_lookup":
        return any(term in normalized for term in ("email", "inbox", "message", "thread", "calendar", "meeting"))
    return False


def arbitrate_learned_route(
    candidate: LearnedRouteCandidate | None,
    deterministic_intent: str,
    text: str = "",
) -> LearnedRouteCandidate | None:
    if candidate is None:
        return None
    if candidate.safety_level != "safe_read_only":
        candidate.action = "block"
        candidate.negative_evidence.append("candidate_not_safe_read_only")
        return candidate
    if deterministic_intent != "pass_through":
        candidate.action = "shadow"
        candidate.negative_evidence.append("deterministic_intent_already_selected")
        return candidate
    if candidate.intent not in READ_ONLY_PROMOTION_INTENTS:
        candidate.action = "block"
        candidate.negative_evidence.append("intent_not_read_only_promotable")
        return candidate
    if candidate.confidence < 0.90:
        candidate.action = "shadow"
        candidate.negative_evidence.append("confidence_below_promotion_threshold")
        return candidate
    if text and not _text_supports_intent(text, candidate.intent):
        candidate.action = "shadow"
        candidate.negative_evidence.append("current_text_lacks_intent_support")
        return candidate
    candidate.action = "promote"
    candidate.positive_evidence.append("pass_through_safe_read_only_high_confidence")
    return candidate


async def propose_learned_route(
    text: str,
    deterministic_intent: str = "pass_through",
    *,
    path: str | None = None,
) -> LearnedRouteCandidate | None:
    normalized = normalize_turn_text(text)
    if not normalized:
        return None
    try:
        candidates = await get_shadow_plan_candidates(text, path=path) if path else await get_shadow_plan_candidates(text)
    except Exception as e:
        logger.warning(f"NOVA_LEARNED_ROUTER_ERROR | failed_to_fetch_candidates={e}")
        return None
    if not candidates:
        return None

    blocked: list[str] = []
    for raw in candidates:
        quality_reason = _quality_block_reason(raw)
        if quality_reason:
            blocked.append(f"{raw.get('id')}:{quality_reason}")
            continue
        allowed, reason = learned_candidate_allowed(text, raw, deterministic_intent)
        if not allowed:
            blocked.append(f"{raw.get('id')}:{reason}")
            continue
        intent = _canonical_intent(str(raw.get("intent") or ""))
        tools = [str(tool) for tool in (raw.get("tools_used") or [])]
        confidence = float(raw.get("confidence") or 0.0)
        candidate = LearnedRouteCandidate(
            intent=intent,
            confidence=confidence,
            suggested_tools=tools,
            similar_examples=[
                {
                    "id": raw.get("id"),
                    "trigger_text": raw.get("trigger_text"),
                    "similarity": raw.get("similarity"),
                    "match_type": raw.get("match_type"),
                    "confidence": raw.get("confidence"),
                }
            ],
            positive_evidence=[
                f"matched_learned_candidate:{raw.get('id')}",
                f"match_type:{raw.get('match_type')}",
            ],
            negative_evidence=blocked,
            safety_level=_safety_level(tools, intent),
            raw_candidate=raw,
        )
        return arbitrate_learned_route(candidate, deterministic_intent, text)

    return None


def _row_text(row: dict[str, Any]) -> str:
    return str(row.get("normalized_text") or row.get("canonical_text") or row.get("raw_text") or "").strip()


def _row_tools(row: dict[str, Any]) -> list[str]:
    tools = row.get("tools_used")
    if isinstance(tools, list):
        return [str(tool) for tool in tools]
    if isinstance(tools, str) and tools.strip():
        try:
            parsed = json.loads(tools)
            if isinstance(parsed, list):
                return [str(tool) for tool in parsed]
        except json.JSONDecodeError:
            return []
    return []


def learned_router_example_from_observation(row: dict[str, Any]) -> dict[str, Any]:
    intent = str(row.get("deterministic_intent") or "")
    tools = _row_tools(row)
    handled = row.get("handled")
    outcome = str(row.get("outcome") or "")
    return {
        "id": row.get("id"),
        "text": _row_text(row),
        "label_intent": intent,
        "label_tools": tools,
        "handled": handled,
        "outcome": outcome,
        "positive": handled == 1 and intent != "pass_through" and outcome not in {"user_correction", "repeat_request", "near_repeat_request"},
        "stop_reason": row.get("stop_reason") or "",
        "latency_ms": int(row.get("latency_ms") or 0),
    }


async def build_learned_router_eval_dataset(limit: int = 500, *, path: str | None = None) -> list[dict[str, Any]]:
    from nova.store import get_recent_turn_policy_observations

    rows = await get_recent_turn_policy_observations(limit=limit, path=path) if path else await get_recent_turn_policy_observations(limit=limit)
    examples = [learned_router_example_from_observation(dict(row)) for row in rows]
    return [example for example in examples if example["text"]]


async def evaluate_learned_router(limit: int = 200, *, path: str | None = None) -> dict[str, Any]:
    examples = await build_learned_router_eval_dataset(limit=limit, path=path)
    evaluated = []
    correct_intent = 0
    promotions = 0
    blocked = 0
    shadow = 0
    for example in examples:
        candidate = await propose_learned_route(example["text"], "pass_through", path=path)
        candidate_dict = candidate.to_dict() if candidate else None
        predicted_intent = candidate.intent if candidate else ""
        if predicted_intent and predicted_intent == example["label_intent"]:
            correct_intent += 1
        if candidate and candidate.action == "promote":
            promotions += 1
        elif candidate and candidate.action == "block":
            blocked += 1
        elif candidate:
            shadow += 1
        evaluated.append({
            "example": example,
            "candidate": candidate_dict,
            "intent_match": bool(predicted_intent and predicted_intent == example["label_intent"]),
        })
    total = len(examples)
    with_candidate = sum(1 for item in evaluated if item["candidate"])
    return {
        "source": "learned_router_offline_eval",
        "window": limit,
        "examples": total,
        "with_candidate": with_candidate,
        "intent_matches": correct_intent,
        "intent_match_rate": round(correct_intent / with_candidate, 3) if with_candidate else 0.0,
        "coverage_rate": round(with_candidate / total, 3) if total else 0.0,
        "promotions": promotions,
        "shadow": shadow,
        "blocked": blocked,
        "items": evaluated,
    }
