"""Learned-policy scaffolding for Nova turn orchestration.

This module is intentionally conservative: it extracts interpretable turn features
and logs shadow policy candidates, but it does not auto-promote learned behavior
or bypass deterministic orchestrator guardrails.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger


@dataclass
class TurnFeatures:
    utterance_hash: str
    normalized_text: str
    text_length: int
    token_count: int
    has_location_prefix: bool
    location: str
    asks_current_data: bool
    asks_weather: bool
    asks_personal_data: bool
    asks_side_effect: bool
    asks_workspace: bool
    asks_email: bool
    asks_workflow: bool
    pending_clarification: str
    active_goal: bool
    active_workflow: bool
    last_intent: str
    preference_flags: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyCandidate:
    intent: str
    confidence: float
    reason: str
    required_tools: list[str] = field(default_factory=list)
    evidence_budget: dict[str, int] = field(default_factory=dict)
    presentation: str = ""
    source: str = "interpretable_shadow_policy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TurnPolicyObservation:
    ts: int
    features: TurnFeatures
    deterministic_intent: str
    shadow_candidate: PolicyCandidate | None
    handled: bool | None = None
    outcome: str = "observed"
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "features": self.features.to_dict(),
            "deterministic_intent": self.deterministic_intent,
            "shadow_candidate": self.shadow_candidate.to_dict() if self.shadow_candidate else None,
            "handled": self.handled,
            "outcome": self.outcome,
            "tools_used": self.tools_used,
            "stop_reason": self.stop_reason,
            "latency_ms": self.latency_ms,
        }


_CURRENT_TERMS = (
    "current",
    "right now",
    "now",
    "today",
    "tonight",
    "this morning",
    "this afternoon",
    "this evening",
    "latest",
    "recent",
)
_WEATHER_TERMS = ("weather", "forecast", "rain", "humidity", "wind", "temperature", "outside", "outdoor")
_PERSONAL_TERMS = ("my ", "me ", "i ", "email", "calendar", "tesla", "workspace", "memory")
_SIDE_EFFECT_TERMS = (
    "create",
    "make",
    "send",
    "delete",
    "restart",
    "start",
    "stop",
    "turn on",
    "turn off",
    "set ",
    "change",
)


def normalize_turn_text(text: str) -> str:
    cleaned = re.sub(r"^\[User location:[^\]]+\]\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?🧭 MODE POLICY:.*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def extract_location_prefix(text: str) -> str:
    match = re.search(r"\[User location:\s*([^\]]+)\]", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def extract_turn_features(text: str, state: Any) -> TurnFeatures:
    normalized = normalize_turn_text(text)
    location = extract_location_prefix(text)
    utterance_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    token_count = len(normalized.split()) if normalized else 0
    asks_weather = any(term in normalized for term in _WEATHER_TERMS)
    asks_current_data = asks_weather or any(term in normalized for term in _CURRENT_TERMS)
    asks_personal_data = any(term in normalized for term in _PERSONAL_TERMS)
    asks_side_effect = any(term in normalized for term in _SIDE_EFFECT_TERMS)
    return TurnFeatures(
        utterance_hash=utterance_hash,
        normalized_text=normalized[:500],
        text_length=len(normalized),
        token_count=token_count,
        has_location_prefix=bool(location),
        location=location,
        asks_current_data=asks_current_data,
        asks_weather=asks_weather,
        asks_personal_data=asks_personal_data,
        asks_side_effect=asks_side_effect,
        asks_workspace=any(term in normalized for term in ("workspace", "page", "document", "report", "brief")),
        asks_email=any(term in normalized for term in ("email", "message", "thread", "inbox")),
        asks_workflow=any(term in normalized for term in ("workflow", "briefing", "research", "deep dive")),
        pending_clarification=str(getattr(state, "pending_clarification", "") or ""),
        active_goal=bool(getattr(state, "active_goal", "")),
        active_workflow=bool(getattr(state, "active_workflow_run_id", "")),
        last_intent=str(getattr(state, "last_intent", "") or ""),
        preference_flags={
            "prefers_weather_visual": asks_weather,
            "requires_grounding_for_current_data": asks_current_data,
            "avoid_llm_direct_for_personal_data": asks_personal_data,
        },
    )


def shadow_policy_predict(features: TurnFeatures) -> PolicyCandidate | None:
    if features.asks_weather and features.asks_current_data:
        confidence = 0.92 if features.has_location_prefix else 0.82
        return PolicyCandidate(
            intent="weather_lookup",
            confidence=confidence,
            reason="Outdoor/current weather request should use grounded weather evidence and structured display.",
            required_tools=["get_weather"],
            evidence_budget={"get_weather": 1},
            presentation="weather_card_table_plus_clean_speech",
        )
    return None


def log_policy_observation(
    *,
    features: TurnFeatures,
    deterministic_intent: str,
    shadow_candidate: PolicyCandidate | None,
    handled: bool | None = None,
    outcome: str = "observed",
    tools_used: list[str] | None = None,
    stop_reason: str = "",
    latency_ms: int = 0,
) -> None:
    observation = TurnPolicyObservation(
        ts=int(time.time()),
        features=features,
        deterministic_intent=deterministic_intent,
        shadow_candidate=shadow_candidate,
        handled=handled,
        outcome=outcome,
        tools_used=tools_used or [],
        stop_reason=stop_reason,
        latency_ms=latency_ms,
    )
    logger.info(f"NOVA_TURN_POLICY | {json.dumps(observation.to_dict(), sort_keys=True)}")


def build_policy_observation(
    *,
    features: TurnFeatures,
    deterministic_intent: str,
    shadow_candidate: PolicyCandidate | None,
    handled: bool | None = None,
    outcome: str = "observed",
    tools_used: list[str] | None = None,
    stop_reason: str = "",
    latency_ms: int = 0,
) -> TurnPolicyObservation:
    return TurnPolicyObservation(
        ts=int(time.time()),
        features=features,
        deterministic_intent=deterministic_intent,
        shadow_candidate=shadow_candidate,
        handled=handled,
        outcome=outcome,
        tools_used=tools_used or [],
        stop_reason=stop_reason,
        latency_ms=latency_ms,
    )
