"""Turn-level orchestration for Nova voice interactions.

This module is the control-plane layer in front of the LLM/tool loop. It handles
high-confidence workflows deterministically and leaves ambiguous/general turns to
normal LLM tool calling.
"""

from __future__ import annotations

import re
import time
import json
import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger

from nova.turn_policy import build_policy_observation, canonicalize_turn_text, extract_location_prefix, extract_turn_features, label_previous_turn_outcome, log_plan_cache_candidate, log_policy_observation, plan_cache_candidate_from_observation, shadow_policy_predict
from nova.learning import get_shadow_plan_candidates
from nova.learned_router import propose_learned_route
from nova.turn_tool_policy import learned_candidate_allowed


DispatchTool = Callable[[str, dict[str, Any]], Awaitable[str]]
ServerMessage = Callable[[dict[str, Any]], Awaitable[None]]
PersistTurn = Callable[[str, str], Awaitable[None]]
StrategyHandler = Callable[["TurnRuntime"], Awaitable["TurnExecutionResult"]]
STATE_METADATA_KEY = "nova_turn_orchestrator"


class TurnIntent(str, Enum):
    PASS_THROUGH = "pass_through"
    AUTO_ACTION = "auto_action"
    CLARIFICATION = "clarification"
    ACTIVE_ACTION_STATUS = "active_action_status"
    ACTIVE_ACTION_CONFIRMATION = "active_action_confirmation"
    ACTIVE_ACTION_RETRY = "active_action_retry"
    ACTIVE_ACTION_FAILURE_REPORT = "active_action_failure_report"
    TESLA_NAVIGATION_PLAN = "tesla_navigation_plan"
    WEATHER_LOOKUP = "weather_lookup"
    WORKSPACE_CREATION = "workspace_creation"
    LOOKUP_THEN_WORKSPACE_CREATION = "lookup_then_workspace_creation"
    WORKSPACE_CONTEXT_CONTINUATION = "workspace_context_continuation"
    TASK_ARTIFACT_CONTINUATION = "task_artifact_continuation"
    WORKSPACE_CREATION_CONTINUATION = "workspace_creation_continuation"
    EMAIL_LOOKUP = "email_lookup"
    BUSINESS_DIRECTIONS_LOOKUP = "business_directions_lookup"
    CONVERSATION_RECALL = "conversation_recall"
    CONTEXT_CONTINUATION = "context_continuation"
    PERSONAL_MEMORY_RECALL = "personal_memory_recall"
    CURRENT_EVENTS_LOOKUP = "current_events_lookup"
    WORKFLOW_TRIGGER = "workflow_trigger"
    WORKFLOW_STATUS = "workflow_status"
    CALENDAR_LOOKUP = "calendar_lookup"
    WORKSPACE_READ = "workspace_read"
    FOCUS_COLLABORATION = "focus_collaboration"


@dataclass
class TurnPlan:
    intent: TurnIntent
    goal: str
    user_text: str
    evidence_budget: dict[str, int] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    learned_candidate: dict[str, Any] | None = None


@dataclass
class TurnExecutionResult:
    handled: bool
    response: str = ""
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = ""
    intent: str = ""
    display_text: str = ""
    speech_text: str = ""
    result_label: str = "turn_orchestrator"
    is_structured: bool = False
    card: dict[str, Any] | None = None
    workspace_page_id: str = ""


@dataclass
class EvidenceEnvelope:
    intent: str
    claim_type: str
    query: str
    tools_used: list[str] = field(default_factory=list)
    evidence_count: int = 0
    evidence_preview: str = ""
    confidence: str = "low"
    no_evidence: bool = False
    stop_reason: str = ""
    user_id: str = "default"
    conversation_id: str = ""
    session_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "claim_type": self.claim_type,
            "query": self.query,
            "tools_used": list(self.tools_used),
            "evidence_count": self.evidence_count,
            "evidence_preview": self.evidence_preview,
            "confidence": self.confidence,
            "no_evidence": self.no_evidence,
            "stop_reason": self.stop_reason,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "ts": self.ts,
        }


@dataclass
class AgentWorkOrder:
    goal: str
    source_context: list[str] = field(default_factory=list)
    deliverable: str = ""
    constraints: list[str] = field(default_factory=list)
    workspace_target: str = ""
    approval_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source_context": self.source_context[-8:],
            "deliverable": self.deliverable,
            "constraints": self.constraints,
            "workspace_target": self.workspace_target,
            "approval_required": self.approval_required,
        }

    def to_prompt(self) -> str:
        parts = [
            "Agent work order.",
            f"Goal: {self.goal}",
            f"Deliverable: {self.deliverable}",
        ]
        if self.workspace_target:
            parts.append(f"Workspace target: {self.workspace_target}")
        if self.source_context:
            parts.append("Source context:\n" + "\n".join(f"- {item}" for item in self.source_context[-8:]))
        if self.constraints:
            parts.append("Constraints:\n" + "\n".join(f"- {item}" for item in self.constraints))
        parts.append(f"Approval required: {'yes' if self.approval_required else 'no'}")
        return "\n\n".join(parts)


@dataclass
class TurnMetrics:
    total_turns: int = 0
    handled_turns: int = 0
    fallback_turns: int = 0
    total_latency_ms: int = 0
    intent_counts: Counter = field(default_factory=Counter)
    tool_counts: Counter = field(default_factory=Counter)
    stop_reason_counts: Counter = field(default_factory=Counter)

    def record(self, result: TurnExecutionResult, latency_ms: int = 0):
        self.total_turns += 1
        if result.handled:
            self.handled_turns += 1
        else:
            self.fallback_turns += 1
        if result.intent:
            self.intent_counts[result.intent] += 1
        for tool in result.tools_used:
            self.tool_counts[tool] += 1
        if result.stop_reason:
            self.stop_reason_counts[result.stop_reason] += 1
        self.total_latency_ms += latency_ms

    def snapshot(self) -> dict[str, Any]:
        avg_latency = int(self.total_latency_ms / self.handled_turns) if self.handled_turns else 0
        fallback_rate = round(self.fallback_turns / self.total_turns, 3) if self.total_turns else 0
        return {
            "total_turns": self.total_turns,
            "handled_turns": self.handled_turns,
            "fallback_turns": self.fallback_turns,
            "fallback_rate": fallback_rate,
            "avg_handled_latency_ms": avg_latency,
            "intents": dict(self.intent_counts),
            "tools": dict(self.tool_counts),
            "stop_reasons": dict(self.stop_reason_counts),
        }


_METRICS = TurnMetrics()
_RECENT_EVIDENCE: deque[dict[str, Any]] = deque(maxlen=200)


def _evidence_count(evidence: Any) -> int:
    if evidence is None:
        return 0
    if isinstance(evidence, (list, tuple, set, dict)):
        return len(evidence)
    text = str(evidence).strip()
    if not text or text in {"[]", "{}"}:
        return 0
    return 1


def _conversation_recall_has_no_evidence(evidence: str) -> bool:
    normalized = " ".join((evidence or "").lower().split())
    if not normalized or normalized in {"[]", "{}"}:
        return True
    return _contains_any(normalized, (
        "no past conversations found",
        "couldn't find a match",
        "could not find a match",
        "do not retry with a different query",
    ))


def _format_conversation_recall_response(evidence: str) -> str:
    lines = []
    for line in str(evidence or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if "summarize the relevant findings for the user" in lowered:
            continue
        if "these are real past conversations" in lowered:
            continue
        lines.append(stripped)
    body = "\n".join(lines).strip()
    if not body:
        return _no_evidence_response("prior conversations")
    if len(body) > 1400:
        body = body[:1400].rstrip() + "..."
    return "I found prior-conversation evidence. Based only on retrieved conversation records:\n\n" + body


def _current_events_has_no_evidence(evidence: str) -> bool:
    normalized = " ".join((evidence or "").lower().split())
    if not normalized or normalized in {"[]", "{}"}:
        return True
    return _contains_any(normalized, (
        "temporarily rate-limited",
        "could not retrieve current external evidence",
        "search failed",
        "search timed out",
        "search returned no results",
        "no response from perplexity",
    ))


_TOPIC_STOPWORDS = {
    "about", "again", "based", "before", "being", "build", "check", "clinic", "conversation",
    "could", "create", "discussed", "document", "earlier", "every", "everything", "from",
    "have", "keep", "know", "look", "make", "page", "paper", "past", "please", "prior",
    "recall", "report", "search", "that", "them", "there", "this", "thread", "turn",
    "what", "when", "where", "with", "work", "working", "workspace", "would",
}


def _normalized_recall_topic(text: str) -> str:
    words = []
    for word in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        if len(word) < 4 or word in _TOPIC_STOPWORDS:
            continue
        words.append(word)
    return " ".join(list(dict.fromkeys(words))[:16])


def _extract_evidence_conversation_ids(evidence: str) -> list[str]:
    ids = re.findall(r"Nova Conversation\s+([A-Za-z0-9-]+)", evidence or "")
    return list(dict.fromkeys(ids))[:20]


async def _persist_grounded_recall_pattern(envelope: dict[str, Any]) -> None:
    try:
        topic = _normalized_recall_topic(str(envelope.get("query") or ""))
        if not topic:
            return
        from nova.store import upsert_grounded_recall_pattern
        await upsert_grounded_recall_pattern(
            user_id=str(envelope.get("user_id") or "default"),
            normalized_topic=topic,
            trigger_phrase=str(envelope.get("query") or ""),
            route=str(envelope.get("intent") or TurnIntent.CONVERSATION_RECALL.value),
            tool_name="search_past_conversations",
            evidence_conversation_ids=_extract_evidence_conversation_ids(str(envelope.get("evidence_preview") or "")),
            evidence_preview=str(envelope.get("evidence_preview") or ""),
            confidence=str(envelope.get("confidence") or "medium"),
            metadata={"source": "evidence_envelope", "stop_reason": envelope.get("stop_reason")},
        )
    except Exception as e:
        logger.warning(f"Failed to persist grounded recall pattern: {e}")


def _record_evidence_envelope(envelope: EvidenceEnvelope) -> None:
    data = envelope.to_dict()
    _RECENT_EVIDENCE.appendleft(data)
    logger.info(f"NOVA_EVIDENCE_ENVELOPE | {json.dumps(data, sort_keys=True)}")
    _best_effort_task(_persist_evidence_envelope(data))
    if (
        data.get("intent") == TurnIntent.CONVERSATION_RECALL.value
        and data.get("stop_reason") == "conversation_recall_grounded"
        and data.get("tools_used") == ["search_past_conversations"]
        and not data.get("no_evidence")
        and int(data.get("evidence_count") or 0) > 0
    ):
        _best_effort_task(_persist_grounded_recall_pattern(data))


def get_recent_evidence_envelopes(limit: int = 25) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 25), 200))
    return list(_RECENT_EVIDENCE)[:safe_limit]


def _no_evidence_response(source: str, action: str = "answer") -> str:
    return f"I checked {source} but did not find usable evidence to {action}. I won't guess or reconstruct that from model memory."


def _record_tool_evidence(
    runtime: "TurnRuntime",
    *,
    claim_type: str,
    query: str,
    tool_name: str,
    result: Any,
    stop_reason: str,
    confidence: str = "medium",
    no_evidence: bool | None = None,
) -> None:
    evidence = str(result or "").strip()
    no_evidence_value = not evidence or evidence in {"[]", "{}"} if no_evidence is None else bool(no_evidence)
    _record_evidence_envelope(EvidenceEnvelope(
        intent=runtime.plan.intent.value,
        claim_type=claim_type,
        query=query,
        tools_used=[tool_name],
        evidence_count=0 if no_evidence_value else _evidence_count(result),
        evidence_preview="" if no_evidence_value else evidence[:500],
        confidence="low" if no_evidence_value else confidence,
        no_evidence=no_evidence_value,
        stop_reason=stop_reason,
        user_id=runtime.user_id,
        conversation_id=runtime.conversation_id,
        session_id=runtime.session_id,
    ))


async def _persist_evidence_envelope(envelope: dict[str, Any]) -> None:
    try:
        from nova.store import append_turn_evidence_envelope
        await append_turn_evidence_envelope(envelope)
    except Exception as e:
        logger.warning(f"Failed to persist evidence envelope: {e}")


def _best_effort_task(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)


async def _persist_policy_observation(observation) -> None:
    try:
        from nova.store import append_turn_policy_observation
        await append_turn_policy_observation(observation)
    except Exception as e:
        logger.warning(f"Failed to persist turn policy observation: {e}")


async def _label_previous_policy_observation(text: str) -> None:
    try:
        from nova.store import get_recent_turn_policy_observations, label_turn_policy_observation
        rows = await get_recent_turn_policy_observations(1)
        previous = rows[0] if rows else None
        label = label_previous_turn_outcome(text, previous)
        if not label or label.target_observation_id is None:
            return
        updated = await label_turn_policy_observation(
            label.target_observation_id,
            label.outcome,
            label.to_dict(),
        )
        if updated:
            logger.info(f"NOVA_TURN_POLICY_LABEL | {json.dumps(label.to_dict(), sort_keys=True)}")
    except Exception as e:
        logger.warning(f"Failed to label previous turn policy observation: {e}")


async def _log_plan_cache_candidate(features) -> None:
    try:
        from nova.store import get_successful_turn_policy_observations
        rows = await get_successful_turn_policy_observations(200)
        candidates = [
            candidate
            for candidate in (plan_cache_candidate_from_observation(features, row) for row in rows)
            if candidate is not None
        ]
        if not candidates:
            return
        best = max(candidates, key=lambda candidate: candidate.confidence)
        log_plan_cache_candidate(features, best)
    except Exception as e:
        logger.warning(f"Failed to evaluate turn plan cache candidate: {e}")


def _record_policy_outcome(
    text: str,
    state: "TurnState",
    result: "TurnExecutionResult",
    latency_ms: int = 0,
) -> None:
    _best_effort_task(_label_previous_policy_observation(text))
    features = extract_turn_features(text, state)
    shadow_candidate = shadow_policy_predict(features)
    outcome = "handled" if result.handled else "pass_through"
    observation = build_policy_observation(
        features=features,
        deterministic_intent=result.intent or TurnIntent.PASS_THROUGH.value,
        shadow_candidate=shadow_candidate,
        handled=result.handled,
        outcome=outcome,
        tools_used=result.tools_used,
        stop_reason=result.stop_reason,
        latency_ms=latency_ms,
    )
    log_policy_observation(
        features=features,
        deterministic_intent=observation.deterministic_intent,
        shadow_candidate=shadow_candidate,
        handled=result.handled,
        outcome=outcome,
        tools_used=result.tools_used,
        stop_reason=result.stop_reason,
        latency_ms=latency_ms,
    )
    _best_effort_task(_persist_policy_observation(observation))


@dataclass
class TurnState:
    active_goal: str = ""
    known_context: list[str] = field(default_factory=list)
    suggested_topics: list[str] = field(default_factory=list)
    pending_scribe: bool = False
    pending_clarification: str = ""
    active_workflow_run_id: str = ""
    active_workflow_name: str = ""
    active_workflow_goal: str = ""
    active_task_artifact_id: str = ""
    active_action_id: str = ""
    last_intent: str = ""
    last_recall_query: str = ""
    turns_handled: int = 0
    # Session planner — persists across turns so the spine survives tool failures
    active_plan_id: str = ""
    active_plan_topic: str = ""
    active_plan_page_id: str = ""
    # Running page dictionary — real page_ids seen this session, seeded into context
    # so M2.7 never needs to guess a UUID. Each entry: {page_id, title, project_key}
    known_workspace_pages: list[dict] = field(default_factory=list)
    # Non-persisted: rebuilt from PCG at session start
    daily_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_goal": self.active_goal,
            "known_context": self.known_context[-12:],
            "suggested_topics": self.suggested_topics,
            "pending_scribe": self.pending_scribe,
            "pending_clarification": self.pending_clarification,
            "active_workflow_run_id": self.active_workflow_run_id,
            "active_workflow_name": self.active_workflow_name,
            "active_workflow_goal": self.active_workflow_goal,
            "active_task_artifact_id": self.active_task_artifact_id,
            "active_action_id": self.active_action_id,
            "last_intent": self.last_intent,
            "last_recall_query": self.last_recall_query,
            "turns_handled": self.turns_handled,
            "active_plan_id": self.active_plan_id,
            "active_plan_topic": self.active_plan_topic,
            "active_plan_page_id": self.active_plan_page_id,
            "known_workspace_pages": self.known_workspace_pages[-20:],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnState":
        return cls(
            active_goal=str(data.get("active_goal") or ""),
            known_context=[str(item) for item in data.get("known_context", [])],
            suggested_topics=[str(item) for item in data.get("suggested_topics", [])],
            pending_scribe=bool(data.get("pending_scribe", False)),
            pending_clarification=str(data.get("pending_clarification") or ""),
            active_workflow_run_id=str(data.get("active_workflow_run_id") or ""),
            active_workflow_name=str(data.get("active_workflow_name") or ""),
            active_workflow_goal=str(data.get("active_workflow_goal") or ""),
            active_task_artifact_id=str(data.get("active_task_artifact_id") or ""),
            active_action_id=str(data.get("active_action_id") or ""),
            last_intent=str(data.get("last_intent") or ""),
            last_recall_query=str(data.get("last_recall_query") or ""),
            turns_handled=int(data.get("turns_handled") or 0),
            active_plan_id=str(data.get("active_plan_id") or ""),
            active_plan_topic=str(data.get("active_plan_topic") or ""),
            active_plan_page_id=str(data.get("active_plan_page_id") or ""),
            known_workspace_pages=[
                p for p in (data.get("known_workspace_pages") or [])
                if isinstance(p, dict) and p.get("page_id")
            ],
        )


def turn_state_from_metadata(metadata: dict[str, Any]) -> TurnState:
    raw = metadata.get(STATE_METADATA_KEY, {}) if isinstance(metadata, dict) else {}
    return TurnState.from_dict(raw if isinstance(raw, dict) else {})


def turn_state_to_metadata_value(state: TurnState) -> dict[str, Any]:
    return state.to_dict()


def get_orchestrator_metrics() -> dict[str, Any]:
    return _METRICS.snapshot()


@dataclass
class TurnTelemetry:
    intent: str
    goal: str
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = ""
    latency_ms: int = 0

    def to_log_fields(self) -> str:
        return (
            f"intent={self.intent} goal={self.goal[:80]!r} "
            f"tools={','.join(self.tools_used) or 'none'} "
            f"stop_reason={self.stop_reason!r} latency_ms={self.latency_ms}"
        )


@dataclass
class TurnRuntime:
    plan: TurnPlan
    state: TurnState
    dispatch_tool: DispatchTool
    send_server_msg: ServerMessage
    persist_turn: PersistTurn
    telemetry: TurnTelemetry
    started: float
    user_id: str = "default"
    conversation_id: str = ""
    session_id: str = ""

    async def finish(self, response: str, stop_reason: str, workspace_page_id: str = "") -> TurnExecutionResult:
        self.telemetry.stop_reason = stop_reason
        self.telemetry.latency_ms = int((time.monotonic() - self.started) * 1000)
        self.state.last_intent = self.plan.intent.value
        self.state.turns_handled += 1
        await self.persist_turn("assistant", response)
        logger.info(f"NOVA_TURN_ORCHESTRATOR | {self.telemetry.to_log_fields()}")
        # If caller didn't supply a page ID, try to pull it from the active artifact.
        if not workspace_page_id and self.state.active_task_artifact_id:
            try:
                from nova.task_artifacts import get_task_artifact
                _art = await get_task_artifact(self.state.active_task_artifact_id)
                if isinstance(_art, dict):
                    workspace_page_id = (
                        _art.get("execution", {}).get("workspace_page_id", "")
                        or next(
                            (lnk["value"] for lnk in (_art.get("handoff", {}).get("links") or [])
                             if lnk.get("kind") == "workspace_page_id"),
                            "",
                        )
                    )
            except Exception:
                pass
        return TurnExecutionResult(
            handled=True,
            response=response,
            tools_used=list(self.telemetry.tools_used),
            stop_reason=stop_reason,
            intent=self.plan.intent.value,
            display_text=response,
            speech_text=response,
            result_label="turn_orchestrator",
            is_structured=False,
            card=None,
            workspace_page_id=workspace_page_id,
        )

    async def finish_structured(
        self,
        display_text: str,
        speech_text: str,
        stop_reason: str,
        result: str = "turn_orchestrator",
        card: dict[str, Any] | None = None,
    ) -> TurnExecutionResult:
        self.telemetry.stop_reason = stop_reason
        self.telemetry.latency_ms = int((time.monotonic() - self.started) * 1000)
        self.state.last_intent = self.plan.intent.value
        self.state.turns_handled += 1
        await self.persist_turn("assistant", display_text)
        logger.info(f"NOVA_TURN_ORCHESTRATOR | {self.telemetry.to_log_fields()}")
        return TurnExecutionResult(
            handled=True,
            response=display_text,
            tools_used=list(self.telemetry.tools_used),
            stop_reason=stop_reason,
            intent=self.plan.intent.value,
            display_text=display_text,
            speech_text=speech_text,
            result_label=result,
            is_structured=True,
            card=card,
        )


TOPIC_KEYWORDS = [
    "heat exhaustion and hydration guidance",
    "food and water safety",
    "mosquito and insect-borne disease prevention",
    "air quality concerns",
    "emergency medical resources near stadiums",
    "COVID and respiratory illness prevention",
    "traveler health and jet lag",
    "sports injury first aid",
    "access to medical facilities near NRG Stadium",
]


def clean_user_text(text: str) -> str:
    return canonicalize_turn_text(text).canonical_text


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _active_action_followup_intent(lower: str, state: TurnState) -> TurnIntent | None:
    if not state.active_action_id:
        return None
    status_terms = (
        "what's taking",
        "whats taking",
        "taking so long",
        "status",
        "what happened",
        "what did you find",
        "did it work",
        "is it done",
        "is that done",
    )
    failure_terms = (
        "didn't show up",
        "didnt show up",
        "never showed up",
        "not showing",
        "something's not working",
        "somethings not working",
        "it didn't work",
        "it didnt work",
        "not working",
    )
    retry_terms = (
        "try again",
        "send it again",
        "resend",
        "retry",
        "do it again",
    )
    confirmation_terms = (
        "yes",
        "yes please",
        "go ahead",
        "send it",
        "send it now",
        "please do",
        "do it",
        "confirm",
    )
    stripped = lower.strip(" .!?")
    has_confirmation = stripped in confirmation_terms or _contains_any(lower, ("go ahead and", "yes please", "send it"))
    if not state.active_action_id:
        return None
    if _contains_any(lower, failure_terms):
        return TurnIntent.ACTIVE_ACTION_FAILURE_REPORT
    if _contains_any(lower, retry_terms):
        return TurnIntent.ACTIVE_ACTION_RETRY
    if _contains_any(lower, status_terms):
        return TurnIntent.ACTIVE_ACTION_STATUS
    if has_confirmation:
        return TurnIntent.ACTIVE_ACTION_CONFIRMATION
    return None


def _wants_tesla_navigation_plan(lower: str) -> bool:
    navigation_terms = (
        "send me the address",
        "send the address",
        "send directions",
        "send me directions",
        "send it to my tesla",
        "send to my tesla",
        "send to the tesla",
        "navigate to",
        "navigation to",
        "directions to",
        "take me to",
    )
    return _contains_any(lower, navigation_terms) and _contains_any(lower, ("tesla", "directions", "navigate", "address", "starbucks", "destination"))


def _wants_business_directions_lookup(lower: str) -> bool:
    if _contains_any(lower, ("email", "inbox", "thread", "message", "conversation", "workspace", "document", "page", "cig")):
        return False
    place_terms = (
        "business",
        "restaurant",
        "store",
        "studio",
        "clinic",
        "hospital",
        "office",
        "shop",
        "coffee",
        "starbucks",
        "hotel",
        "airport",
        "address",
    )
    directions_terms = (
        "directions to",
        "navigate to",
        "take me to",
        "route to",
        "how do i get to",
        "find directions",
        "find the address",
        "send directions",
        "send me directions",
    )
    find_place = lower.startswith(("find ", "find the ", "look up ", "lookup ", "search for ")) and _contains_any(lower, place_terms)
    return _contains_any(lower, directions_terms) or find_place


def _business_directions_query(user_text: str, location: str = "") -> str:
    base = user_text.strip()
    location_hint = f" near {location}" if location else ""
    return (
        f"{base}{location_hint}. Find the official business/place address and concise driving directions or navigation-relevant location details. "
        "Do not search email or private CIG data. Cite current public sources."
    )


def _extract_tesla_vehicle_hint(lower: str) -> str:
    if _contains_any(lower, ("model three", "model 3")):
        return "Model 3"
    if _contains_any(lower, ("model x", "model ten")):
        return "Model X"
    if "black panther" in lower:
        return "Black Panther"
    return ""


def _extract_navigation_destination(user_text: str) -> str:
    cleaned = user_text.strip()
    patterns = (
        r"\b(?:send|send me|send the)\s+(?:the\s+)?(?:address|directions)\s+(?:of|for|to)\s+(.+)",
        r"\b(?:navigate|navigation|directions|take me)\s+(?:to)\s+(.+)",
        r"\bsend\s+(.+?)\s+(?:to|into)\s+(?:my\s+)?tesla\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .")
    return cleaned


def _derive_goal(user_text: str, fallback: str) -> str:
    if len(user_text) <= 180:
        return user_text
    return fallback


def _derive_lookup_query(user_text: str) -> str:
    cleaned = re.sub(r"\b(create|make|build|write|draft|construct)\b.*", "", user_text, flags=re.IGNORECASE).strip()
    return cleaned or user_text


def _extract_requested_topics(user_text: str) -> list[str]:
    lower = user_text.lower()
    if "all of the topics above" in lower or "topics above" in lower:
        return []
    separators = r",|;|\n|\band\b"
    parts = [part.strip(" .:-") for part in re.split(separators, user_text, flags=re.IGNORECASE)]
    topic_markers = ("advisory", "advisories", "topic", "topics", "page", "pages")
    if not _contains_any(lower, topic_markers):
        return []
    topics = [part for part in parts if 4 <= len(part) <= 90]
    return topics[:12]


def _answers_workspace_structure(lower: str) -> bool:
    structure_terms = (
        "single page",
        "one page",
        "one polished page",
        "separate pages",
        "page per topic",
        "one page per topic",
        "multiple pages",
        "all of the topics",
        "topics above",
    )
    return _contains_any(lower, structure_terms)


def _wants_workflow_status(lower: str, state: TurnState) -> bool:
    if not state.active_workflow_run_id:
        return False
    status_terms = (
        "is it done",
        "is that done",
        "is the workflow done",
        "check the workflow",
        "workflow status",
        "status of that",
        "what happened with that",
        "how is that going",
        "is it finished",
        "did it finish",
    )
    return _contains_any(lower, status_terms)


def _wants_research_workflow(lower: str) -> bool:
    wants_research = _contains_any(lower, ("research", "brief", "briefing", "deep dive", "investigate"))
    wants_durable_output = _contains_any(lower, ("make me", "create", "prepare", "write", "generate", "summarize", "report"))
    return wants_research and wants_durable_output


def _wants_weather(lower: str) -> bool:
    weather_terms = ("weather", "forecast", "rain", "humidity", "wind", "temperature", "outside", "outdoor")
    indoor_terms = ("in here", "inside", "room", "house", "bedroom", "office")
    explicit_outdoor_terms = ("outside", "outdoor", "weather", "forecast", "rain", "wind", "humidity", "temperature")
    if not _contains_any(lower, weather_terms):
        return False
    if _contains_any(lower, indoor_terms) and not _contains_any(lower, explicit_outdoor_terms):
        return False
    return True


def _derive_research_topic(user_text: str) -> str:
    cleaned = re.sub(r"\b(research|make me|create|prepare|write|generate|a|an|the|brief|briefing|report|on|about)\b", " ", user_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:-")
    return cleaned or user_text


def _wants_workspace_context_continuation(lower: str, state: TurnState) -> bool:
    if not _contains_any(lower, ("workspace", "page", "document", "brief", "report")):
        return False
    if _contains_any(lower, ("talk about", "discuss", "framework", "frameworks", "include", "structure", "outline", "page for", "designed for", "proper workspace page", "try creating the page", "making a new page", "empty or non-functional")):
        return True
    return bool(state.active_task_artifact_id and _contains_any(lower, ("continue", "next", "add", "include", "revise", "update")))


def _wants_workspace_creation(lower: str) -> bool:
    if not _contains_any(lower, ("workspace", "page", "document", "brief", "report")):
        return False
    return _contains_any(lower, (
        "create", "make", "build", "draft", "write", "generate", "put together",
        "turn this into", "turn it into", "send to scribe", "delegate to scribe",
    ))


def _artifact_continuation_action(lower: str, state: TurnState) -> str:
    if not state.active_task_artifact_id:
        return ""
    if _contains_any(lower, ("show artifact", "show the artifact", "show me the artifact", "what do we have", "what we have so far", "artifact status", "show status")):
        return "show"
    if _contains_any(lower, ("revise", "update", "change", "edit", "add", "include", "remove")):
        return "revise"
    if _contains_any(lower, ("continue", "keep going", "next", "finish it", "proceed with it", "work on that")):
        return "continue"
    return ""


def _wants_conversation_recall(lower: str) -> bool:
    if _contains_any(lower, ("workspace", "page", "document")) and _contains_any(lower, ("create", "make", "turn", "put", "try creating")):
        return False
    return _contains_any(lower, (
        "what did we talk about", "what have we talked about", "what was that framework", "what framework",
        "find the conversation", "find that conversation", "pull the thread", "pull up the thread",
        "past conversation", "previous conversation", "earlier conversation", "remember when we discussed",
        "what did i say", "what did you say", "you hallucinated", "hallucinating", "i said liam", "liam framework",
        "what we talked about", "original thread", "look up the conversation", "conversation we had earlier",
        "recall the conversation", "recall our conversation", "recall what we discussed",
        "thread where", "everything we discussed", "we discussed about",
    ))


def _is_ambiguous_context_followup(lower: str) -> bool:
    stripped = lower.strip(" .!?")
    if not stripped:
        return False
    exact = {
        "what did you find",
        "what did you find out",
        "so what did you find",
        "so what did you find out",
        "what are the results",
        "what were the results",
        "did you get it",
        "did you get anything",
        "did you find it",
        "did you find anything",
        "what about the analysis",
        "what about that analysis",
        "continue",
        "go on",
        "keep going",
    }
    if stripped in exact:
        return True
    return _contains_any(lower, (
        "what did you find out",
        "what did you find",
        "did you get the analysis",
        "case study analysis",
        "from before",
        "previous analysis",
        "earlier analysis",
    ))


def _is_recall_status_followup(lower: str) -> bool:
    stripped = lower.strip(" .!?")
    return stripped in {
        "did you find anything yet",
        "did you find anything",
        "did you find it",
        "did you get anything",
        "did you get it",
        "anything yet",
        "any results",
        "what did you find",
        "what did you find out",
        "so what did you find out",
    }


def _is_workspace_read_request(lower: str) -> bool:
    """Detect intent to read existing workspace/canvas page content."""
    return _contains_any(lower, (
        "what's on the workspace",
        "what is on the workspace",
        "what's on the canvas",
        "what is on the canvas",
        "what's on the page",
        "what is on the page",
        "show me the page",
        "show me the workspace",
        "show me the canvas",
        "read the page",
        "read the workspace",
        "pull up the page",
        "pull up the workspace",
        "what does the page say",
        "what does the workspace say",
        "open the page",
        "open the workspace",
        "get the page",
        "fetch the page",
        "what's in the workspace",
        "what is in the workspace",
    ))


def _is_focus_collaboration_request(lower: str, state: "TurnState") -> bool:
    """Detect collaborative focus mode: drafting, writing, redacting, storytelling.
    Only fires when the last intent was also focus/workspace — prevents triggering
    on first-turn requests that need context lookup first.
    """
    focus_phrases = (
        "let's draft",
        "let's write",
        "let's redact",
        "let's start drafting",
        "let's work on",
        "let's continue",
        "let's keep going",
        "section by section",
        "paragraph by paragraph",
        "draft the",
        "write the",
        "redact the",
        "write a section",
        "draft a section",
        "next section",
        "tell me a story",
        "continue the story",
        "keep writing",
        "keep drafting",
        "keep going with",
        "it is all of the above",
        "all of the above",
    )
    if not _contains_any(lower, focus_phrases):
        return False
    # Only activate focus mode if we have prior workspace/task context
    # to avoid stripping tools on a first-turn request with no context
    focus_continuation_intents = {
        TurnIntent.WORKSPACE_READ.value,
        TurnIntent.FOCUS_COLLABORATION.value,
        TurnIntent.WORKSPACE_CREATION.value,
        TurnIntent.WORKSPACE_CONTEXT_CONTINUATION.value,
        TurnIntent.TASK_ARTIFACT_CONTINUATION.value,
    }
    return (
        state.last_intent in focus_continuation_intents
        or bool(state.active_task_artifact_id)
        or bool(state.active_goal)
    )


def _conversation_recall_query(user_text: str, state: TurnState) -> str:
    lower = user_text.lower()
    if state.last_recall_query and state.last_intent == TurnIntent.CONVERSATION_RECALL.value and _is_recall_status_followup(lower):
        return state.last_recall_query
    return user_text


def _wants_personal_memory_recall(lower: str) -> bool:
    return _contains_any(lower, (
        "what do you remember about me", "what do i believe", "what did i decide", "what did i ask you to remember",
        "my preference", "my preferences", "my goals", "my goal", "what do you know about me", "in my memory",
        "personal memory", "remember about me",
    ))


def _wants_calendar_lookup(lower: str) -> bool:
    calendar_terms = (
        "calendar", "schedule", "agenda", "appointment", "appointments",
        "what do i have", "what's on my", "what is on my", "what's today",
        "what's tonight", "what is tonight", "plans for today", "plans tonight",
        "anything today", "anything tonight", "anything scheduled",
        "what time is", "when is my", "what meetings", "what events",
        "do i have anything", "do i have a meeting", "what's coming up",
    )
    return _contains_any(lower, calendar_terms)


def _wants_current_events_lookup(lower: str) -> bool:
    if _wants_weather(lower):
        return False
    if _wants_calendar_lookup(lower):
        return False
    # ── General class: "look up / search for / find info about [entity]" ──
    # The user wants current/real-time info about something that requires web_search.
    # This covers concerts, sports, events, people, places, prices — anything factual
    # that isn't covered by a dedicated tool (weather, calendar, email, memory).
    search_intent_phrases = (
        "look up", "search for", "find info", "find information", "look it up",
        "find out", "can you search", "google", "look online",
        "tell me about", "what do you know about", "info on", "information on",
        "details on", "details about", "what's the latest on", "what is the latest on",
    )
    # Exclude queries that have dedicated tools
    exclude_terms = (
        "email", "inbox", "calendar", "schedule", "weather", "temperature",
        "forecast", "memory", "preference", "tesla", "car", "vehicle",
        "service", "homelab", "docker", "container",
    )
    has_search_intent = _contains_any(lower, search_intent_phrases)
    mentions_excluded = _contains_any(lower, exclude_terms)
    if has_search_intent and not mentions_excluded:
        return True
    # ── Explicit news / current-events patterns ──
    if _contains_any(lower, ("stock market", "s&p", "s & p", "dow jones", "nasdaq")) and _contains_any(lower, ("today", "yesterday", "last", "recent", "current", "this week", "this month")):
        return True
    return _contains_any(lower, (
        "latest news", "current news", "what happened today", "today's news", "recent news",
        "what's happening with", "what is happening with", "latest from", "latest on",
        "current events", "right now", "as of today", "last three days", "last few days",
    ))


def _with_skill_binding(plan: TurnPlan) -> TurnPlan:
    try:
        from nova.skill_loader import get_skill_binding_for_intent
        binding = get_skill_binding_for_intent(plan.intent.value)
    except Exception as e:
        logger.warning(f"Failed to bind skill for intent {plan.intent.value}: {e}")
        binding = None
    if binding:
        plan.context.setdefault("skill", binding)
    return plan


def _plan_from_learned_route(user_text: str, candidate: dict[str, Any]) -> TurnPlan | None:
    intent = str(candidate.get("intent") or "")
    tools = [str(tool) for tool in (candidate.get("suggested_tools") or [])]
    context = {"learned_route_candidate": candidate}
    if intent == TurnIntent.CURRENT_EVENTS_LOOKUP.value and "web_search" in tools:
        return TurnPlan(
            intent=TurnIntent.CURRENT_EVENTS_LOOKUP,
            goal="Retrieve current external information before answering using a learned safe read-only route.",
            user_text=user_text,
            evidence_budget={"web_search": 1},
            allowed_tools=["web_search"],
            stop_conditions=["call web_search exactly once", "answer only from returned current-data evidence"],
            context={**context, "query": user_text},
            learned_candidate=candidate,
        )
    if intent == TurnIntent.CONVERSATION_RECALL.value and "search_past_conversations" in tools:
        return TurnPlan(
            intent=TurnIntent.CONVERSATION_RECALL,
            goal="Retrieve relevant prior conversation context using a learned safe read-only route.",
            user_text=user_text,
            evidence_budget={"search_past_conversations": 1},
            allowed_tools=["search_past_conversations"],
            stop_conditions=["call search_past_conversations exactly once", "answer only from retrieved evidence"],
            context={**context, "query": user_text},
            learned_candidate=candidate,
        )
    if intent == TurnIntent.PERSONAL_MEMORY_RECALL.value and "recall_memory" in tools:
        return TurnPlan(
            intent=TurnIntent.PERSONAL_MEMORY_RECALL,
            goal="Retrieve personal memory context using a learned safe read-only route.",
            user_text=user_text,
            evidence_budget={"recall_memory": 1},
            allowed_tools=["recall_memory"],
            stop_conditions=["call recall_memory exactly once", "answer only from retrieved memory evidence"],
            context={**context, "query": user_text},
            learned_candidate=candidate,
        )
    if intent == TurnIntent.WEATHER_LOOKUP.value and "get_weather" in tools:
        return TurnPlan(
            intent=TurnIntent.WEATHER_LOOKUP,
            goal="Return current outdoor weather with structured visual display and natural speech using a learned safe read-only route.",
            user_text=user_text,
            evidence_budget={"get_weather": 1},
            allowed_tools=["get_weather"],
            stop_conditions=["call get_weather exactly once", "send structured display and clean speech"],
            context=context,
            learned_candidate=candidate,
        )
    if intent == TurnIntent.EMAIL_LOOKUP.value and any(tool in tools for tool in ("query_cig", "check_studio")):
        return TurnPlan(
            intent=TurnIntent.EMAIL_LOOKUP,
            goal="Find the requested email or calendar context using a learned safe read-only route.",
            user_text=user_text,
            evidence_budget={"query_cig": 1},
            allowed_tools=["query_cig"],
            stop_conditions=["return concise email lookup result"],
            context=context,
            learned_candidate=candidate,
        )
    return None


def _learned_recall_query(user_text: str, pattern: dict[str, Any]) -> str:
    topic = str(pattern.get("normalized_topic") or "").strip()
    preview = str(pattern.get("evidence_preview") or "").strip()
    if topic and topic not in user_text.lower():
        return f"{user_text}\nKnown grounded topic hint: {topic}"
    if preview:
        return f"{user_text}\nKnown grounded recall preview: {preview[:300]}"
    return user_text


async def _find_learned_recall_pattern(user_text: str, user_id: str = "default") -> dict[str, Any] | None:
    try:
        from nova.store import find_grounded_recall_patterns
        patterns = await find_grounded_recall_patterns(user_id, user_text, limit=3)
    except Exception as e:
        logger.warning(f"Failed to find grounded recall patterns: {e}")
        return None
    if not patterns:
        return None
    best = patterns[0]
    score = float(best.get("match_score") or 0)
    if score < 0.34 and int(best.get("success_count") or 0) < 2:
        return None
    return best


def _route_from_semantic(
    resolution: Any,
    user_text: str,
    state: TurnState,
    features: Any,
    shadow_candidate: Any,
) -> "TurnPlan | None":
    """Convert a high-confidence SemanticTurnResolution into a deterministic TurnPlan."""
    intent_str = str(getattr(resolution, "intent", "") or "")
    resolved_query = str(getattr(resolution, "resolved_query", "") or user_text)
    confidence = float(getattr(resolution, "confidence", 0.0))

    if intent_str == "calendar_lookup":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CALENDAR_LOOKUP.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.CALENDAR_LOOKUP,
            goal="Return today's calendar from pre-loaded snapshot or a single check_studio call.",
            user_text=user_text,
            evidence_budget={"check_studio": 1},
            allowed_tools=["check_studio"],
            stop_conditions=[
                "call check_studio(studio=calendar, action=briefing) at most once",
                "if calendar data is already in the system prompt Today section, use that directly",
                "answer concisely with time, title, and location",
            ],
            context={"semantic_routed": True, "confidence": confidence},
        ))

    if intent_str == "email_lookup":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.EMAIL_LOOKUP.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.EMAIL_LOOKUP,
            goal="Find the requested email using one bounded CIG query.",
            user_text=user_text,
            evidence_budget={"query_cig": 1},
            allowed_tools=["query_cig"],
            stop_conditions=[
                "call query_cig exactly once with the resolved query",
                "summarize the result or state that nothing matched",
            ],
            context={"resolved_query": resolved_query, "semantic_routed": True, "confidence": confidence},
        ))

    if intent_str == "weather_lookup":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WEATHER_LOOKUP.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.WEATHER_LOOKUP,
            goal="Get current weather for the user's location.",
            user_text=user_text,
            evidence_budget={"get_weather": 1},
            allowed_tools=["get_weather"],
            stop_conditions=["call get_weather exactly once", "report conditions concisely"],
            context={"semantic_routed": True, "confidence": confidence},
        ))

    if intent_str == "current_events_lookup":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CURRENT_EVENTS_LOOKUP.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.CURRENT_EVENTS_LOOKUP,
            goal="Search for the requested real-time information.",
            user_text=user_text,
            evidence_budget={"web_search": 1},
            allowed_tools=["web_search"],
            stop_conditions=["call web_search exactly once with the resolved query", "cite the source"],
            context={"resolved_query": resolved_query, "semantic_routed": True, "confidence": confidence},
        ))

    if intent_str == "personal_memory_recall":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.PERSONAL_MEMORY_RECALL.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.PERSONAL_MEMORY_RECALL,
            goal="Recall the user's stored preferences or facts.",
            user_text=user_text,
            evidence_budget={"recall_memory": 1},
            allowed_tools=["recall_memory"],
            stop_conditions=["call recall_memory once with the resolved query", "answer only from retrieved evidence"],
            context={"resolved_query": resolved_query, "semantic_routed": True, "confidence": confidence},
        ))

    if intent_str == "conversation_recall":
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CONVERSATION_RECALL.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.CONVERSATION_RECALL,
            goal="Retrieve prior conversation context using LLM-resolved semantics.",
            user_text=user_text,
            evidence_budget={"search_past_conversations": 1},
            allowed_tools=["search_past_conversations"],
            stop_conditions=["call search_past_conversations once with the resolved query", "answer only from retrieved evidence"],
            context={"query": resolved_query, "semantic_routed": True, "confidence": confidence},
        ))

    return None


async def decide_turn(text: str, state: TurnState, semantic_resolution: Any | None = None) -> TurnPlan:
    user_text = clean_user_text(text)
    lower = user_text.lower()
    features = extract_turn_features(text, state)
    shadow_candidate = shadow_policy_predict(features)
    _best_effort_task(_log_plan_cache_candidate(features))
    active_action_intent = _active_action_followup_intent(lower, state)

    if active_action_intent:
        log_policy_observation(features=features, deterministic_intent=active_action_intent.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=active_action_intent,
            goal=state.active_goal or "Continue the active action.",
            user_text=user_text,
            evidence_budget={},
            allowed_tools=[],
            stop_conditions=["read active action ledger", "do not claim side-effect completion without ledger evidence"],
            context={"action_id": state.active_action_id},
        ))

    if semantic_resolution and getattr(semantic_resolution, "is_routable", lambda: False)():
        semantic_plan = _route_from_semantic(semantic_resolution, user_text, state, features, shadow_candidate)
        if semantic_plan:
            logger.info(
                f"NOVA_SEMANTIC_ROUTED | intent={semantic_resolution.intent} "
                f"confidence={semantic_resolution.confidence:.3f} query={semantic_resolution.resolved_query[:80]}"
            )
            return semantic_plan

    if state.last_intent == TurnIntent.CONVERSATION_RECALL.value and state.last_recall_query and _is_recall_status_followup(lower):
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CONVERSATION_RECALL.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.CONVERSATION_RECALL,
            goal="Continue the prior conversation recall using the same topic-specific query.",
            user_text=user_text,
            evidence_budget={"search_past_conversations": 1},
            allowed_tools=["search_past_conversations"],
            stop_conditions=["reuse the previous recall query", "call search_past_conversations exactly once", "answer only from retrieved evidence"],
            context={"query": state.last_recall_query, "continuation_of_recall_query": True},
        ))

    if _is_ambiguous_context_followup(lower):
        if state.known_context:
            log_policy_observation(features=features, deterministic_intent=TurnIntent.CONTEXT_CONTINUATION.value, shadow_candidate=shadow_candidate)
            return _with_skill_binding(TurnPlan(
                intent=TurnIntent.CONTEXT_CONTINUATION,
                goal=state.active_goal or "Continue from the recent grounded context.",
                user_text=user_text,
                evidence_budget={},
                allowed_tools=[],
                stop_conditions=["answer from recent known context only", "do not call unrelated learned tools"],
                context={"known_context": state.known_context[-3:]},
            ))
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CLARIFICATION.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.CLARIFICATION,
            goal="Clarify the ambiguous follow-up before using tools.",
            user_text=user_text,
            evidence_budget={},
            allowed_tools=[],
            stop_conditions=["ask one focused clarification question", "do not guess the referenced context"],
            context={
                "clarification_key": "ambiguous_context_followup",
                "question": "Which thing do you want me to continue from — the case study analysis, a prior conversation, email, or something else?",
            },
        ))

    artifact_action = _artifact_continuation_action(lower, state)
    if artifact_action:
        log_policy_observation(features=features, deterministic_intent=TurnIntent.TASK_ARTIFACT_CONTINUATION.value, shadow_candidate=shadow_candidate)
        return _with_skill_binding(TurnPlan(
            intent=TurnIntent.TASK_ARTIFACT_CONTINUATION,
            goal=state.active_goal or "Continue the active task artifact.",
            user_text=user_text,
            evidence_budget={},
            allowed_tools=["hub_delegate"],
            stop_conditions=["use the active task artifact", "do not claim completion without Scribe evidence"],
            context={"action": artifact_action, "task_id": state.active_task_artifact_id},
        ))

    # ── P3a: Workspace continuation routing ─────────────────────────────────
    # If the session has an active workspace page (planner spine) and the user
    # is using continuation verbs (expand/add/update/show/open/include/etc),
    # route to WORKSPACE_CONTEXT_CONTINUATION so M2.7 gets a focused tool
    # budget instead of all 53 tools. This catches the common pattern observed
    # in the 8 PM CEO meeting session where every turn fell through to
    # pass_through despite obvious workspace continuation intent.
    if state.active_plan_page_id or state.active_plan_id:
        _ws_continuation_verbs = (
            "expand", "add to", "add a", "add the", "update", "revise",
            "edit", "include", "append", "extend", "open the", "open it",
            "show me the", "show the", "show what", "pull up", "pull it up",
            "review the", "review what", "what's on", "what is on",
            "what do we have", "what have we", "build out", "build on",
            "flesh out", "fill in", "elaborate", "add more", "section",
            "agenda", "talking point", "briefing", "the page", "the doc",
            "the document", "this page", "that page", "our page", "our doc",
            "keep working", "continue working", "let's work", "lets work",
            "work on", "tomorrow's agenda",
        )
        if _contains_any(lower, _ws_continuation_verbs):
            log_policy_observation(
                features=features,
                deterministic_intent=TurnIntent.WORKSPACE_CONTEXT_CONTINUATION.value,
                shadow_candidate=shadow_candidate,
            )
            logger.info(
                f"NOVA_DETERMINISTIC_ROUTE | intent=workspace_context_continuation "
                f"plan_id={state.active_plan_id!r} page_id={state.active_plan_page_id!r}"
            )
            return _with_skill_binding(TurnPlan(
                intent=TurnIntent.WORKSPACE_CONTEXT_CONTINUATION,
                goal=state.active_goal or state.active_plan_topic or "Continue the active workspace page.",
                user_text=user_text,
                evidence_budget={"manage_workspace": 6, "manage_task_plan": 2},
                allowed_tools=[
                    "manage_workspace", "manage_task_plan",
                    "search_past_conversations", "query_cig",
                    "recall_memory", "save_memory", "web_search",
                ],
                stop_conditions=[
                    "use the active workspace page",
                    "do not create a new plan; this work has an active plan_id",
                    "fan out independent reads/writes as parallel tool_calls",
                ],
                context={
                    "active_plan_id": state.active_plan_id,
                    "active_plan_page_id": state.active_plan_page_id,
                    "active_plan_topic": state.active_plan_topic,
                },
            ))

    learned_candidate = None
    learned_route_candidate = None
    try:
        candidates = await get_shadow_plan_candidates(user_text)
        if candidates:
            logger.info(f"NOVA_LEARNING_SHADOW | {json.dumps({'text': user_text, 'candidates': candidates[:5]}, sort_keys=True)}")
            for candidate in candidates:
                allowed, reason = learned_candidate_allowed(user_text, candidate, TurnIntent.PASS_THROUGH.value)
                if not allowed:
                    logger.info(
                        f"NOVA_LEARNING_GUARD | candidate_id={candidate.get('id')} "
                        f"intent={candidate.get('intent')} confidence={candidate.get('confidence')} blocked={reason}"
                    )
                    continue
                learned_candidate = candidate
                break
    except Exception as e:
        logger.warning(f"Failed to fetch shadow candidates: {e}")

    learned_route_candidate = await propose_learned_route(user_text, TurnIntent.PASS_THROUGH.value)
    if learned_route_candidate is not None:
        learned_route = learned_route_candidate.to_dict()
        logger.info(f"NOVA_LEARNED_ROUTER_SHADOW | {json.dumps({'text': user_text, 'candidate': learned_route}, sort_keys=True)}")
        if learned_route_candidate.action == "promote":
            promoted_plan = _plan_from_learned_route(user_text, learned_route)
            if promoted_plan is not None:
                logger.info(f"NOVA_LEARNED_ROUTER_PROMOTED | {json.dumps({'text': user_text, 'intent': promoted_plan.intent.value, 'confidence': learned_route_candidate.confidence}, sort_keys=True)}")
                log_policy_observation(features=features, deterministic_intent=promoted_plan.intent.value, shadow_candidate=shadow_candidate)
                return _with_skill_binding(promoted_plan)
            logger.info(f"NOVA_LEARNED_ROUTER_BLOCKED | {json.dumps({'text': user_text, 'reason': 'no_plan_for_promoted_candidate', 'candidate': learned_route}, sort_keys=True)}")

    log_policy_observation(features=features, deterministic_intent=TurnIntent.PASS_THROUGH.value, shadow_candidate=shadow_candidate)
    plan = TurnPlan(intent=TurnIntent.PASS_THROUGH, goal="", user_text=user_text)
    plan.learned_candidate = learned_route_candidate.to_dict() if learned_route_candidate is not None else learned_candidate
    return _with_skill_binding(plan)


def _build_scribe_context(plan: TurnPlan, state: TurnState, evidence: str = "") -> str:
    source_context = [f"Latest user instruction: {plan.user_text}"]
    if state.known_context:
        source_context.extend(state.known_context[-8:])
    if evidence:
        source_context.append(f"Evidence summary: {evidence[:2500]}")
    topics = plan.context.get("topics") or state.suggested_topics or TOPIC_KEYWORDS
    constraints = [
        "Use Pi Workspace block batching.",
        "Create polished, user-ready workspace content.",
    ]
    if topics:
        constraints.append("Use the requested page/topic structure.")
        deliverable = "Create polished Pi Workspace page(s) covering:\n" + "\n".join(f"- {topic}" for topic in topics)
    else:
        deliverable = "Create polished Pi Workspace page(s) using the latest user instruction."
    work_order = AgentWorkOrder(
        goal=plan.goal or state.active_goal,
        source_context=source_context,
        deliverable=deliverable,
        constraints=constraints,
        workspace_target="Pi Workspace",
        approval_required=False,
    )
    return work_order.to_prompt()


def _build_scribe_work_order(plan: TurnPlan, state: TurnState, evidence: str = "") -> AgentWorkOrder:
    source_context = [f"Latest user instruction: {plan.user_text}"]
    if state.known_context:
        source_context.extend(state.known_context[-8:])
    if evidence:
        source_context.append(f"Evidence summary: {evidence[:2500]}")
    topics = plan.context.get("topics") or state.suggested_topics or TOPIC_KEYWORDS
    deliverable = "Create polished Pi Workspace page(s) using the latest user instruction."
    if topics:
        deliverable = "Create polished Pi Workspace page(s) covering:\n" + "\n".join(f"- {topic}" for topic in topics)
    return AgentWorkOrder(
        goal=plan.goal or state.active_goal,
        source_context=source_context,
        deliverable=deliverable,
        constraints=[
            "Use Pi Workspace block batching.",
            "Create polished, user-ready workspace content.",
            "Return a structured summary with created artifacts or failure reason.",
        ],
        workspace_target="Pi Workspace",
        approval_required=False,
    )


async def _ensure_workspace_task_artifact(runtime: TurnRuntime, evidence: str = "", status: str = "grounding") -> dict[str, Any] | None:
    if not runtime.conversation_id:
        return None
    try:
        from nova.task_artifacts import create_task_artifact, get_task_artifact, merge_handoff_links, set_active_task_artifact, upsert_task_artifact, TaskArtifact
        artifact_data = await get_task_artifact(runtime.state.active_task_artifact_id) if runtime.state.active_task_artifact_id else None
        if artifact_data:
            artifact = TaskArtifact(**artifact_data)
            artifact.status = status
            artifact.goal = runtime.plan.goal or artifact.goal
            if evidence:
                artifact.source_context.append({"type": "grounding_result", "text": evidence[:2500], "tool": runtime.telemetry.tools_used[-1] if runtime.telemetry.tools_used else ""})
        else:
            artifact = await create_task_artifact(
                user_id=runtime.user_id,
                conversation_id=runtime.conversation_id,
                session_id=runtime.session_id,
                kind="workspace_page",
                goal=runtime.plan.goal,
                status=status,
                metadata={"intent": runtime.plan.intent.value, "user_text": runtime.plan.user_text},
            )
        skill_binding = runtime.plan.context.get("skill")
        if isinstance(skill_binding, dict):
            artifact.metadata["skill"] = skill_binding
            artifact.metadata["intent"] = runtime.plan.intent.value
        artifact.requirements = list(dict.fromkeys([*artifact.requirements, *[str(t) for t in runtime.plan.context.get("topics", [])]]))
        artifact.execution.setdefault("tools_used", [])
        for tool in runtime.telemetry.tools_used:
            if tool not in artifact.execution["tools_used"]:
                artifact.execution["tools_used"].append(tool)
        if status in {"handoff", "complete"}:
            artifact = merge_handoff_links(artifact, evidence)
        artifact = await upsert_task_artifact(artifact)
        runtime.state.active_task_artifact_id = artifact.task_id
        if runtime.session_id:
            await set_active_task_artifact(runtime.session_id, artifact.task_id)
        await runtime.send_server_msg({
            "type": "turn_status",
            "status": "task_artifact_updated",
            "message": "Updated the workspace task artifact.",
            "taskId": artifact.task_id,
            "kind": artifact.kind,
            "artifactStatus": artifact.status,
        })
        return artifact.to_dict()
    except Exception as e:
        logger.warning(f"Failed to update workspace task artifact: {e}")
        return None


async def _run_workspace_task_artifact_qa(runtime: TurnRuntime) -> dict[str, Any] | None:
    if not runtime.state.active_task_artifact_id:
        return None
    try:
        from nova.task_artifacts import mark_task_artifact_qa
        artifact = await mark_task_artifact_qa(runtime.state.active_task_artifact_id)
        if not artifact:
            return None
        qa = artifact.get("qa") if isinstance(artifact.get("qa"), dict) else {}
        status = str(qa.get("status") or "not_run")
        await runtime.send_server_msg({
            "type": "turn_status",
            "status": "task_artifact_qa",
            "message": f"Task artifact QA {status}.",
            "taskId": artifact.get("task_id"),
            "qaStatus": status,
            "failedCount": qa.get("failed_count", 0),
        })
        return artifact
    except Exception as e:
        logger.warning(f"Failed to run workspace task artifact QA: {e}")
        return None


def _artifact_summary_text(artifact: dict[str, Any]) -> str:
    qa = artifact.get("qa") if isinstance(artifact.get("qa"), dict) else {}
    execution = artifact.get("execution") if isinstance(artifact.get("execution"), dict) else {}
    return (
        f"Active artifact {artifact.get('task_id')} is a {artifact.get('kind')} task with status {artifact.get('status')}. "
        f"Goal: {artifact.get('goal') or 'not set'}. "
        f"Source context entries: {len(artifact.get('source_context') or [])}. "
        f"Requirements: {len(artifact.get('requirements') or [])}. "
        f"Tools used: {', '.join(execution.get('tools_used') or []) or 'none'}. "
        f"QA status: {qa.get('status') or 'not_run'}."
    )


async def _run_task_artifact_continuation(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    action = str(plan.context.get("action") or "show")
    task_id = str(plan.context.get("task_id") or state.active_task_artifact_id)
    try:
        from nova.task_artifacts import TaskArtifact, get_task_artifact, mark_task_artifact_qa, merge_handoff_links, upsert_task_artifact
        artifact_data = await get_task_artifact(task_id)
    except Exception as e:
        logger.warning(f"Failed to load active task artifact: {e}")
        artifact_data = None
    if not artifact_data:
        state.active_task_artifact_id = ""
        return await runtime.finish("I could not find an active task artifact to continue.", "active_task_artifact_missing")
    if action == "show":
        return await runtime.finish(_artifact_summary_text(artifact_data), "active_task_artifact_shown")
    artifact = TaskArtifact(**artifact_data)
    artifact.status = "executing"
    skill_binding = plan.context.get("skill")
    if isinstance(skill_binding, dict):
        artifact.metadata["skill"] = skill_binding
        artifact.metadata["intent"] = plan.intent.value
    artifact.source_context.append({"type": "user_instruction", "text": plan.user_text[:2500], "tool": ""})
    artifact.decisions.append({"type": action, "text": plan.user_text[:1200], "ts": time.time()})
    artifact.qa = {"status": "not_run", "checks": []}
    artifact = await upsert_task_artifact(artifact)
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Continuing the active task artifact with Scribe..."})
    context = "\n\n".join([
        "Continue the active Nova task artifact.",
        f"User instruction: {plan.user_text}",
        f"Action: {action}",
        f"Task artifact JSON: {json.dumps(artifact.to_dict(), default=str)[:3500]}",
    ])
    result = await runtime.dispatch_tool(
        "hub_delegate",
        {
            "agent": "scribe",
            "method": "document",
            "context": context,
            "params": {"agentId": "scribe", "task": context, "task_artifact": artifact.to_dict()},
        },
    )
    runtime.telemetry.tools_used.append("hub_delegate")
    artifact.source_context.append({"type": "delegation_result", "text": str(result)[:2500], "tool": "hub_delegate"})
    artifact.execution.setdefault("tools_used", [])
    if "hub_delegate" not in artifact.execution["tools_used"]:
        artifact.execution["tools_used"].append("hub_delegate")
    artifact.execution.setdefault("delegations", [])
    artifact.execution["delegations"].append({"agent": "scribe", "action": action, "result": str(result)[:1200], "ts": time.time()})
    artifact.status = "handoff"
    artifact = merge_handoff_links(artifact, result)
    artifact = await upsert_task_artifact(artifact)
    artifact_data = await mark_task_artifact_qa(artifact.task_id) or artifact.to_dict()
    qa = artifact_data.get("qa") if isinstance(artifact_data.get("qa"), dict) else {}
    await runtime.send_server_msg({
        "type": "turn_status",
        "status": "task_artifact_qa",
        "message": f"Task artifact QA {qa.get('status') or 'not_run'}.",
        "taskId": artifact.task_id,
        "qaStatus": qa.get("status") or "not_run",
        "failedCount": qa.get("failed_count", 0),
    })
    response = f"I continued the active artifact and sent the update to Scribe. {str(result)[:600]}"
    if qa.get("status") == "failed":
        response += " I saved the task artifact, but QA found missing durable evidence, so I will not claim the handoff is fully complete yet."
    return await runtime.finish(response, "active_task_artifact_continued")


async def _run_lookup_then_workspace_creation(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    state.active_goal = plan.goal
    state.pending_scribe = True
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Finding the relevant context, then preparing the Scribe work order..."})
    lookup_tool = str(plan.context.get("lookup_tool") or "query_cig")
    lookup_query = plan.context.get("lookup_query") or plan.user_text
    lookup_args = (
        {"query": lookup_query, "days_back": 180, "limit": 5}
        if lookup_tool == "search_past_conversations"
        else {"domain": "search", "query": lookup_query}
    )
    result = await runtime.dispatch_tool(lookup_tool, lookup_args)
    runtime.telemetry.tools_used.append(lookup_tool)
    _record_tool_evidence(
        runtime,
        claim_type="retrieved",
        query=str(lookup_query),
        tool_name=lookup_tool,
        result=result,
        stop_reason="workspace_lookup_grounded",
    )
    if result:
        state.known_context.append(str(result)[:1200])
    artifact = await _ensure_workspace_task_artifact(runtime, str(result), status="executing")
    work_order = _build_scribe_work_order(plan, state, str(result))
    if artifact:
        work_order.source_context.append(f"Task artifact JSON: {json.dumps(artifact, default=str)[:2500]}")
    context = work_order.to_prompt()
    scribe_result = await runtime.dispatch_tool(
        "hub_delegate",
        {
            "agent": "scribe",
            "method": "document",
            "context": context,
            "params": {"agentId": "scribe", "task": context, "work_order": work_order.to_dict()},
        },
    )
    runtime.telemetry.tools_used.append("hub_delegate")
    _record_tool_evidence(
        runtime,
        claim_type="handoff",
        query=plan.goal or plan.user_text,
        tool_name="hub_delegate",
        result=scribe_result,
        stop_reason="delegated_to_scribe",
    )
    await _ensure_workspace_task_artifact(runtime, str(scribe_result), status="handoff")
    artifact = await _run_workspace_task_artifact_qa(runtime)
    state.pending_scribe = False
    state.pending_clarification = ""
    response = f"I found the relevant context and sent the workspace page work order to Scribe. {str(scribe_result)[:600]}"
    qa = artifact.get("qa") if isinstance(artifact, dict) and isinstance(artifact.get("qa"), dict) else {}
    if qa.get("status") == "failed":
        response += " I saved the task artifact, but QA found missing durable evidence, so I will not claim the workspace handoff is fully complete yet."
    return await runtime.finish(response, "delegated_to_scribe")


async def _run_workspace_creation(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    state.active_goal = plan.goal or state.active_goal
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Delegating the workspace work to Scribe now..."})
    artifact = await _ensure_workspace_task_artifact(runtime, status="executing")
    work_order = _build_scribe_work_order(plan, state)
    if artifact:
        work_order.source_context.append(f"Task artifact JSON: {json.dumps(artifact, default=str)[:2500]}")
    context = work_order.to_prompt()
    result = await runtime.dispatch_tool(
        "hub_delegate",
        {
            "agent": "scribe",
            "method": "document",
            "context": context,
            "params": {"agentId": "scribe", "task": context, "work_order": work_order.to_dict()},
        },
    )
    runtime.telemetry.tools_used.append("hub_delegate")
    _record_tool_evidence(
        runtime,
        claim_type="handoff",
        query=plan.goal or plan.user_text,
        tool_name="hub_delegate",
        result=result,
        stop_reason="delegated_to_scribe",
    )
    await _ensure_workspace_task_artifact(runtime, str(result), status="handoff")
    artifact = await _run_workspace_task_artifact_qa(runtime)
    response = f"I sent the workspace work order to Scribe. {str(result)[:600]}"
    qa = artifact.get("qa") if isinstance(artifact, dict) and isinstance(artifact.get("qa"), dict) else {}
    if qa.get("status") == "failed":
        response += " I saved the task artifact, but QA found missing durable evidence, so I will not claim the workspace handoff is fully complete yet."
    state.pending_scribe = False
    state.pending_clarification = ""
    return await runtime.finish(response, "delegated_to_scribe")


async def _run_calendar_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    snapshot = runtime.state.daily_snapshot
    calendar_briefing = snapshot.get("calendar_briefing", "") if isinstance(snapshot, dict) else ""
    calendar_briefing_lower = str(calendar_briefing).lower()
    calendar_snapshot_failed = _contains_any(calendar_briefing_lower, (
        "returned http",
        "api returned",
        "tool error",
        "tool execution error",
        "timed out",
        "couldn't retrieve",
        "could not retrieve",
        "failed",
        "error",
    ))

    # If daily snapshot already has calendar data, use it directly — no tool call needed
    if calendar_briefing and len(calendar_briefing) > 20 and not calendar_snapshot_failed:
        logger.info(f"NOVA_CALENDAR_SNAPSHOT_HIT | briefing_len={len(calendar_briefing)}")
        runtime.state.known_context.append(calendar_briefing[:1200])
        response = calendar_briefing[:1400]
        return await runtime.finish(response, "calendar_lookup_snapshot")

    # Snapshot empty or missing — fall through to check_studio
    user_tz = snapshot.get("user_timezone", "America/Chicago") if isinstance(snapshot, dict) else "America/Chicago"
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking today\u2019s calendar..."})
    result = await runtime.dispatch_tool(
        "check_studio",
        {"studio": "calendar", "action": "briefing", "user_tz": user_tz},
    )
    runtime.telemetry.tools_used.append("check_studio")
    _record_tool_evidence(
        runtime,
        claim_type="retrieved",
        query=plan.user_text,
        tool_name="check_studio",
        result=result,
        stop_reason="calendar_lookup_complete",
    )
    evidence = str(result or "").strip()
    if evidence:
        runtime.state.known_context.append(evidence[:1200])
    response = evidence[:1400] if evidence else "I couldn\u2019t retrieve today\u2019s calendar right now."
    return await runtime.finish(response, "calendar_lookup_complete")


async def _run_email_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking the requested email context..."})
    result = await runtime.dispatch_tool(
        "query_cig",
        {"domain": "search", "query": plan.user_text},
    )
    runtime.telemetry.tools_used.append("query_cig")
    _record_tool_evidence(
        runtime,
        claim_type="retrieved",
        query=plan.user_text,
        tool_name="query_cig",
        result=result,
        stop_reason="email_lookup_complete",
    )
    response = str(result)[:900] if result else "I couldn't find matching email context."
    return await runtime.finish(response, "email_lookup_complete")


async def _run_conversation_recall(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    query = str(plan.context.get("query") or plan.user_text)
    runtime.state.last_recall_query = query
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Searching prior conversations before answering..."})
    args = {"query": query, "days_back": 365, "limit": 5}
    if runtime.conversation_id:
        args["exclude_conversation_id"] = runtime.conversation_id
    result = await runtime.dispatch_tool("search_past_conversations", args)
    runtime.telemetry.tools_used.append("search_past_conversations")
    evidence = str(result or "").strip()
    if _conversation_recall_has_no_evidence(evidence):
        _record_evidence_envelope(EvidenceEnvelope(
            intent=plan.intent.value,
            claim_type="retrieved",
            query=query,
            tools_used=["search_past_conversations"],
            evidence_count=0,
            no_evidence=True,
            stop_reason="conversation_recall_no_evidence",
            user_id=runtime.user_id,
            conversation_id=runtime.conversation_id,
            session_id=runtime.session_id,
        ))
        return await runtime.finish(
            _no_evidence_response("prior conversations"),
            "conversation_recall_no_evidence",
        )
    runtime.state.known_context.append(evidence[:1200])
    _record_evidence_envelope(EvidenceEnvelope(
        intent=plan.intent.value,
        claim_type="retrieved",
        query=query,
        tools_used=["search_past_conversations"],
        evidence_count=_evidence_count(result),
        evidence_preview=evidence[:500],
        confidence="medium",
        stop_reason="conversation_recall_grounded",
        user_id=runtime.user_id,
        conversation_id=runtime.conversation_id,
        session_id=runtime.session_id,
    ))
    response = _format_conversation_recall_response(evidence)
    return await runtime.finish(response, "conversation_recall_grounded")


async def _run_personal_memory_recall(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    query = str(plan.context.get("query") or plan.user_text)
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking stored personal memory before answering..."})
    result = await runtime.dispatch_tool("recall_memory", {"query": query})
    runtime.telemetry.tools_used.append("recall_memory")
    evidence = str(result or "").strip()
    if not evidence or "nothing found" in evidence.lower():
        _record_evidence_envelope(EvidenceEnvelope(
            intent=plan.intent.value,
            claim_type="personal_memory",
            query=query,
            tools_used=["recall_memory"],
            evidence_count=0,
            no_evidence=True,
            stop_reason="personal_memory_recall_no_evidence",
            user_id=runtime.user_id,
            conversation_id=runtime.conversation_id,
            session_id=runtime.session_id,
        ))
        return await runtime.finish(
            _no_evidence_response("stored personal memory"),
            "personal_memory_recall_no_evidence",
        )
    _record_evidence_envelope(EvidenceEnvelope(
        intent=plan.intent.value,
        claim_type="personal_memory",
        query=query,
        tools_used=["recall_memory"],
        evidence_count=_evidence_count(result),
        evidence_preview=evidence[:500],
        confidence="medium",
        stop_reason="personal_memory_recall_grounded",
        user_id=runtime.user_id,
        conversation_id=runtime.conversation_id,
        session_id=runtime.session_id,
    ))
    response = "I found stored personal-memory evidence. Based only on that retrieved memory:\n\n" + evidence[:1200]
    return await runtime.finish(response, "personal_memory_recall_grounded")


async def _run_current_events_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    query = str(plan.context.get("query") or plan.user_text)
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "heartbeat", "text": "I’ll check current sources for that."})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking current external information before answering..."})
    result = await runtime.dispatch_tool("web_search", {"query": query})
    runtime.telemetry.tools_used.append("web_search")
    evidence = str(result or "").strip()
    if _current_events_has_no_evidence(evidence):
        _record_evidence_envelope(EvidenceEnvelope(
            intent=plan.intent.value,
            claim_type="current_data",
            query=query,
            tools_used=["web_search"],
            evidence_count=0,
            no_evidence=True,
            stop_reason="current_events_no_evidence",
            user_id=runtime.user_id,
            conversation_id=runtime.conversation_id,
            session_id=runtime.session_id,
        ))
        return await runtime.finish(
            _no_evidence_response("current external information"),
            "current_events_no_evidence",
        )
    _record_evidence_envelope(EvidenceEnvelope(
        intent=plan.intent.value,
        claim_type="current_data",
        query=query,
        tools_used=["web_search"],
        evidence_count=_evidence_count(result),
        evidence_preview=evidence[:500],
        confidence="medium",
        stop_reason="current_events_grounded",
        user_id=runtime.user_id,
        conversation_id=runtime.conversation_id,
        session_id=runtime.session_id,
    ))
    response = "I found current external evidence. Based only on that search result:\n\n" + evidence[:1200]
    return await runtime.finish(response, "current_events_grounded")


async def _run_business_directions_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    query = str(plan.context.get("query") or _business_directions_query(plan.user_text))
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "heartbeat", "text": "I’ll look up the public address and directions details."})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking public location sources..."})
    result = await runtime.dispatch_tool("web_search", {"query": query})
    runtime.telemetry.tools_used.append("web_search")
    evidence = str(result or "").strip()
    if _current_events_has_no_evidence(evidence):
        _record_evidence_envelope(EvidenceEnvelope(
            intent=plan.intent.value,
            claim_type="public_location",
            query=query,
            tools_used=["web_search"],
            evidence_count=0,
            no_evidence=True,
            stop_reason="business_directions_no_evidence",
            user_id=runtime.user_id,
            conversation_id=runtime.conversation_id,
            session_id=runtime.session_id,
        ))
        return await runtime.finish(
            _no_evidence_response("public business/location directions"),
            "business_directions_no_evidence",
        )
    _record_evidence_envelope(EvidenceEnvelope(
        intent=plan.intent.value,
        claim_type="public_location",
        query=query,
        tools_used=["web_search"],
        evidence_count=_evidence_count(result),
        evidence_preview=evidence[:500],
        confidence="medium",
        stop_reason="business_directions_grounded",
        user_id=runtime.user_id,
        conversation_id=runtime.conversation_id,
        session_id=runtime.session_id,
    ))
    response = "I found public location evidence. Based only on that search result:\n\n" + evidence[:1200]
    return await runtime.finish(response, "business_directions_grounded")


async def _run_clarification(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    runtime.state.active_goal = plan.goal or runtime.state.active_goal
    runtime.state.pending_clarification = str(plan.context.get("clarification_key") or "")
    question = str(plan.context.get("question") or "Can you clarify the structure you want?")
    return await runtime.finish(question, "awaiting_clarification")


async def _run_context_continuation(runtime: TurnRuntime) -> TurnExecutionResult:
    context_items = [str(item).strip() for item in runtime.plan.context.get("known_context", []) if str(item).strip()]
    if not context_items:
        return await runtime.finish(
            "Which thing do you want me to continue from — the case study analysis, a prior conversation, email, or something else?",
            "context_continuation_missing_context",
        )
    response = "From the recent context I have, here is what I found:\n\n" + "\n\n".join(context_items[-3:])
    return await runtime.finish(response[:2400], "context_continuation_grounded")


async def _run_weather_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    location = str(plan.context.get("location") or "").strip() or "Humble, TX"
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking the current outdoor weather..."})
    result = await runtime.dispatch_tool("get_weather", {"location": location, "query": plan.user_text})
    runtime.telemetry.tools_used.append("get_weather")
    _record_tool_evidence(
        runtime,
        claim_type="current_data",
        query=location,
        tool_name="get_weather",
        result=result,
        stop_reason="weather_lookup_grounded",
    )
    data: Any = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            data = result
    if isinstance(data, dict) and data.get("display") and data.get("speech"):
        display_text = str(data.get("display") or "")
        speech_text = str(data.get("speech") or data.get("speakable") or display_text)
        card = data.get("card") if isinstance(data.get("card"), dict) else None
        return await runtime.finish_structured(
            display_text=display_text,
            speech_text=speech_text,
            result="get_weather",
            card=card,
            stop_reason="weather_structured_response_sent",
        )
    response = str(data or f"I couldn't get the weather for {location} right now.")
    return await runtime.finish(response, "weather_lookup_unstructured_or_failed")


async def _run_workflow_trigger(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    workflow_name = str(plan.context.get("workflow_name") or "")
    workflow_input = plan.context.get("input") if isinstance(plan.context.get("input"), dict) else {}
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Starting the durable workflow now..."})
    result = await runtime.dispatch_tool(
        "hub_delegate",
        {
            "agent": "orchestrator",
            "method": "workflows.trigger",
            "params": {"name": workflow_name, "input": workflow_input},
            "context": plan.goal,
        },
    )
    runtime.telemetry.tools_used.append("hub_delegate")
    _record_tool_evidence(
        runtime,
        claim_type="workflow",
        query=workflow_name,
        tool_name="hub_delegate",
        result=result,
        stop_reason="workflow_trigger_tool_returned",
    )
    run_id = ""
    if isinstance(result, dict):
        run_id = str(result.get("workflowRunId") or result.get("run_id") or "")
    else:
        match = re.search(r'"workflowRunId"\s*:\s*"([^"]+)"|"run_id"\s*:\s*"([^"]+)"', str(result))
        if match:
            run_id = match.group(1) or match.group(2) or ""
    if not run_id:
        response = f"I tried to start the {workflow_name} workflow, but Hub did not return a workflow run ID. {str(result)[:500]}"
        return await runtime.finish(response, "workflow_trigger_failed")
    state.active_workflow_run_id = run_id
    state.active_workflow_name = workflow_name
    state.active_workflow_goal = plan.goal
    response = f"I started the {workflow_name} workflow."
    if run_id:
        response += f" Run ID: {run_id}."
    response += " You can ask me for its status."
    return await runtime.finish(response, "workflow_triggered")


async def _run_workflow_status(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    run_id = str(plan.context.get("workflow_run_id") or state.active_workflow_run_id)
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking the workflow status..."})
    result = await runtime.dispatch_tool(
        "hub_delegate",
        {
            "agent": "orchestrator",
            "method": "workflows.getRun",
            "params": {"run_id": run_id},
            "context": plan.goal,
        },
    )
    runtime.telemetry.tools_used.append("hub_delegate")
    _record_tool_evidence(
        runtime,
        claim_type="workflow",
        query=run_id,
        tool_name="hub_delegate",
        result=result,
        stop_reason="workflow_status_returned",
    )
    response = f"Workflow status for {state.active_workflow_name or 'the active workflow'}: {str(result)[:700]}"
    return await runtime.finish(response, "workflow_status_returned")


async def _run_auto_action(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    candidate = plan.learned_candidate or {}
    tools = candidate.get("tools_used")
    tool_name = tools[0] if tools else None
    
    if not tool_name:
        return await runtime.finish("I encountered an issue executing this learned action.", "auto_action_failed")
        
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": f"Auto-executing learned action for {tool_name}..."})
    
    # Fast heuristic parameter extraction (Zero-Wait bypasses the LLM)
    args = {}
    user_text = plan.user_text
    lower_text = user_text.lower()
    if tool_name == "save_memory":
        content = re.sub(r"^(memorize|save|remember|recall)\s*(this|that)?\s*", "", user_text, flags=re.IGNORECASE).strip()
        content = re.sub(r"\b(please|thanks|to my memory)\b", "", content, flags=re.IGNORECASE).strip()
        args = {"content": content}
    elif tool_name == "get_weather":
        location = str(plan.context.get("location") or runtime.state.location or "Humble, TX").strip()
        args = {"location": location}
    elif tool_name == "control_lights":
        state_arg = "on" if "on" in lower_text else "off"
        args = {"state": state_arg}
    elif tool_name == "query_cig":
        args = {"domain": "search", "query": user_text}
    elif tool_name == "search_past_conversations":
        args = {"query": user_text}
    elif tool_name == "web_search":
        args = {"query": user_text}
    elif tool_name == "tesla_control":
        action_arg = "wake"
        command = "wake"
        if "lock" in lower_text and "unlock" not in lower_text:
            action_arg = "lock"
            command = "lock"
        elif "unlock" in lower_text:
            action_arg = "lock"
            command = "unlock"
        elif "climate" in lower_text or "ac" in lower_text:
            action_arg = "climate"
            command = "climate_on"
        elif "stop" in lower_text or "off" in lower_text:
            action_arg = "climate"
            command = "climate_off"
        args = {"action": action_arg, "command": command}
    else:
        args = {"query": user_text, "context": user_text}

    logger.info(f"NOVA_LEARNING_AUTO_ACTION | tool={tool_name} args={args}")
    
    result = await runtime.dispatch_tool(tool_name, args)
    runtime.telemetry.tools_used.append(tool_name)
    
    data: Any = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            data = result
            
    if isinstance(data, dict) and data.get("display") and data.get("speech"):
        display_text = str(data.get("display") or "")
        speech_text = str(data.get("speech") or data.get("speakable") or display_text)
        card = data.get("card") if isinstance(data.get("card"), dict) else None
        return await runtime.finish_structured(
            display_text=display_text,
            speech_text=speech_text,
            result=tool_name,
            suppress_speech=False,
            stop_reason="auto_action_complete"
        )
        
    response = str(result)[:900] if result else f"Action {tool_name} completed."
    return await runtime.finish(response, "auto_action_complete")


async def _load_active_action(runtime: TurnRuntime) -> dict[str, Any] | None:
    action_id = str(runtime.plan.context.get("action_id") or runtime.state.active_action_id or "")
    try:
        from nova.store import get_action_ledger_entry, get_active_action_ledger_entry
        if action_id:
            entry = await get_action_ledger_entry(action_id)
            if entry:
                return entry
        return await get_active_action_ledger_entry(
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            conversation_id=runtime.conversation_id,
        )
    except Exception as e:
        logger.warning(f"Failed to load active action ledger entry: {e}")
        return None


def _active_action_summary(entry: dict[str, Any]) -> str:
    goal = str(entry.get("active_goal") or "the active action")
    status = str(entry.get("status") or "unknown")
    evidence_status = str(entry.get("evidence_status") or "missing")
    visible = str(entry.get("user_visible_status") or "").strip()
    last_error = str(entry.get("last_error") or "").strip()
    attempts = entry.get("tool_attempts") if isinstance(entry.get("tool_attempts"), list) else []
    response = f"Active action status: {goal}. Ledger status is {status}; evidence is {evidence_status}."
    if attempts:
        response += f" Tool attempts recorded: {len(attempts)}."
    if visible:
        response += f" Latest status: {visible}"
    if last_error:
        response += f" Last error: {last_error}."
    if evidence_status != "satisfied" and status != "completed":
        response += " I will not claim the action completed without successful ledger evidence."
    return response


def _tesla_navigation_result_succeeded(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("success") is True:
            return True
        if result.get("error"):
            return False
    text = str(result or "").lower()
    if not text:
        return False
    failure_markers = ("error", "failed", "not connected", "unavailable", "offline", "requires", "could not", "couldn't")
    if any(marker in text for marker in failure_markers):
        return False
    return "navigation sent to tesla" in text or "sent to tesla" in text


def _parse_tesla_vehicles(result: Any) -> list[dict[str, str]]:
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            vehicles = []
            for line in result.splitlines():
                match = re.search(r"-\s*(?P<name>.+?)(?:\s+\((?P<model>[^)]+)\))?:.*\[VIN:\s*(?P<vin>[^\]]+)\]", line)
                if match:
                    vehicles.append({
                        "display_name": match.group("name").strip(),
                        "model": (match.group("model") or "").strip(),
                        "vin": match.group("vin").strip(),
                    })
            return vehicles
    if isinstance(data, dict):
        raw = data.get("response") or data.get("vehicles") or data.get("data") or []
    else:
        raw = data
    if not isinstance(raw, list):
        return []
    vehicles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        vin = str(item.get("vin") or "").strip()
        if not vin:
            continue
        vehicles.append({
            "display_name": str(item.get("display_name") or item.get("name") or "").strip(),
            "model": str(item.get("model") or "").strip(),
            "vin": vin,
        })
    return vehicles


def _resolve_tesla_vehicle_hint(vehicle_hint: str, vehicles: list[dict[str, str]]) -> dict[str, str] | None:
    normalized_hint = " ".join(vehicle_hint.lower().replace("model three", "model 3").split())
    if not normalized_hint:
        return None
    matches = []
    for vehicle in vehicles:
        name = str(vehicle.get("display_name") or "").lower()
        model = str(vehicle.get("model") or "").lower().replace("model three", "model 3")
        vin = str(vehicle.get("vin") or "").lower()
        haystack = " ".join(part for part in (name, model, vin) if part)
        if normalized_hint in haystack or haystack in normalized_hint:
            matches.append(vehicle)
    return matches[0] if len(matches) == 1 else None


async def _resolve_tesla_vehicle_for_action(runtime: TurnRuntime, entry: dict[str, Any], target: dict[str, Any]) -> str:
    vehicle_hint = str(target.get("vehicle_hint") or "").strip()
    vin = str(target.get("vin") or "").strip()
    if vin:
        return vin
    if not vehicle_hint:
        return ""
    await runtime.send_server_msg({"type": "thinking", "text": f"Resolving the Tesla vehicle for {vehicle_hint}..."})
    vehicles_result = await runtime.dispatch_tool("tesla_control", {"action": "vehicles"})
    runtime.telemetry.tools_used.append("tesla_control")
    vehicles = _parse_tesla_vehicles(vehicles_result)
    resolved = _resolve_tesla_vehicle_hint(vehicle_hint, vehicles)
    if not resolved:
        return ""
    resolved_vin = str(resolved.get("vin") or "")
    try:
        from nova.store import upsert_action_ledger_entry
        updated = dict(entry)
        updated_target = dict(target)
        updated_target["vin"] = resolved_vin
        updated_target["resolved_vehicle"] = {
            "display_name": resolved.get("display_name") or "",
            "model": resolved.get("model") or "",
            "vin": resolved_vin,
        }
        updated["target"] = updated_target
        updated["target_json"] = updated_target
        updated["updated_at"] = time.time()
        await upsert_action_ledger_entry(updated)
    except Exception as e:
        logger.warning(f"Failed to persist resolved Tesla VIN: {e}")
    return resolved_vin


async def _run_active_action_status(runtime: TurnRuntime) -> TurnExecutionResult:
    entry = await _load_active_action(runtime)
    if not entry:
        runtime.state.active_action_id = ""
        return await runtime.finish("I do not have an active action recorded right now.", "active_action_missing")
    runtime.state.active_action_id = str(entry.get("action_id") or runtime.state.active_action_id)
    return await runtime.finish(_active_action_summary(entry), "active_action_status_returned")


async def _execute_tesla_navigation_action(runtime: TurnRuntime, entry: dict[str, Any], *, retry: bool = False) -> TurnExecutionResult:
    target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
    destination = str(target.get("destination") or "").strip()
    vehicle_hint = str(target.get("vehicle_hint") or "").strip()
    vin = str(target.get("vin") or "").strip()
    if not destination:
        return await runtime.finish("I found the pending Tesla navigation action, but it has no destination recorded. I will not send an incomplete command.", "tesla_navigation_missing_destination")
    if vehicle_hint and not vin:
        vin = await _resolve_tesla_vehicle_for_action(runtime, entry, target)
    if vehicle_hint and not vin:
        return await runtime.finish(
            f"I found the pending Tesla navigation action for {destination}, but the vehicle hint is {vehicle_hint} and I do not have a resolved VIN in the ledger. I will not risk sending it to the wrong Tesla.",
            "tesla_navigation_vehicle_unresolved",
        )
    try:
        from nova.store import append_action_ledger_evidence, update_action_ledger_status
        status_text = "Retrying Tesla navigation command." if retry else "Sending Tesla navigation command."
        await update_action_ledger_status(entry["action_id"], "running", user_visible_status=status_text)
        await runtime.send_server_msg({"phase": "thinking"})
        await runtime.send_server_msg({"type": "thinking", "text": "Retrying the navigation destination with Tesla..." if retry else "Sending the navigation destination to Tesla now..."})
        args = {"destination": destination}
        if vin:
            args["vin"] = vin
        result = await runtime.dispatch_tool("tesla_navigation", args)
        runtime.telemetry.tools_used.append("tesla_navigation")
        succeeded = _tesla_navigation_result_succeeded(result)
        evidence = {
            "source": "tesla_navigation",
            "status": "success" if succeeded else "failed",
            "summary": str(result)[:700],
            "payload": {"args": args, "result": str(result)[:1200], "retry": retry},
            "created_at": time.time(),
        }
        await append_action_ledger_evidence(
            entry["action_id"],
            evidence,
            status="completed" if succeeded else "tool_failed",
        )
    except Exception as e:
        try:
            from nova.store import update_action_ledger_status
            await update_action_ledger_status(entry["action_id"], "tool_failed", evidence_status="failed", last_error=str(e))
        except Exception:
            pass
        return await runtime.finish(f"I tried to send the Tesla navigation command, but the executor failed: {e}", "tesla_navigation_executor_failed")
    if succeeded:
        prefix = "Retried and sent" if retry else "Sent"
        return await runtime.finish(f"{prefix} the navigation destination to Tesla: {destination}.", "tesla_navigation_retry_sent" if retry else "tesla_navigation_sent")
    return await runtime.finish(f"I tried to send the navigation destination to Tesla, but the tool did not confirm success: {str(result)[:700]}", "tesla_navigation_retry_failed" if retry else "tesla_navigation_failed")


async def _run_active_action_confirmation(runtime: TurnRuntime) -> TurnExecutionResult:
    entry = await _load_active_action(runtime)
    if not entry:
        runtime.state.active_action_id = ""
        return await runtime.finish("I do not have a pending action to confirm.", "active_action_missing")
    runtime.state.active_action_id = str(entry.get("action_id") or runtime.state.active_action_id)
    status = str(entry.get("status") or "")
    if status != "awaiting_confirmation":
        return await runtime.finish(
            _active_action_summary(entry),
            "active_action_confirmation_not_pending",
        )
    if str(entry.get("intent") or "") == TurnIntent.TESLA_NAVIGATION_PLAN.value:
        return await _execute_tesla_navigation_action(runtime, entry)
    return await runtime.finish(
        f"I found the pending action: {entry.get('active_goal') or 'active action'}. I have not executed it yet because the executor is not wired to this ledger path. I will not pretend it completed.",
        "active_action_confirmation_requires_executor",
    )


async def _run_active_action_retry(runtime: TurnRuntime) -> TurnExecutionResult:
    entry = await _load_active_action(runtime)
    if not entry:
        runtime.state.active_action_id = ""
        return await runtime.finish("I do not have an active action to retry.", "active_action_missing")
    runtime.state.active_action_id = str(entry.get("action_id") or runtime.state.active_action_id)
    status = str(entry.get("status") or "")
    if str(entry.get("intent") or "") == TurnIntent.TESLA_NAVIGATION_PLAN.value and status in {"tool_failed", "evidence_missing", "running"}:
        return await _execute_tesla_navigation_action(runtime, entry, retry=True)
    return await runtime.finish(
        f"I found the action to retry: {entry.get('active_goal') or 'active action'}, but retry is only enabled for unresolved Tesla navigation actions right now. Current ledger state: {status or 'unknown'}.",
        "active_action_retry_not_supported",
    )


async def _run_active_action_failure_report(runtime: TurnRuntime) -> TurnExecutionResult:
    entry = await _load_active_action(runtime)
    if not entry:
        runtime.state.active_action_id = ""
        return await runtime.finish("I do not have an active action recorded to diagnose.", "active_action_missing")
    runtime.state.active_action_id = str(entry.get("action_id") or runtime.state.active_action_id)
    response = _active_action_summary(entry)
    response += " Since you reported it did not work, this should be treated as unresolved until a retry executor records successful evidence."
    return await runtime.finish(response, "active_action_failure_reported")


async def _run_tesla_navigation_plan(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    destination = str(plan.context.get("destination") or plan.user_text).strip()
    vehicle_hint = str(plan.context.get("vehicle_hint") or "").strip()
    try:
        from nova.action_ledger import create_action_entry
        from nova.store import upsert_action_ledger_entry
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal=plan.goal,
            status="awaiting_confirmation",
            user_id=runtime.user_id,
            conversation_id=runtime.conversation_id,
            session_id=runtime.session_id,
            target_json={
                "destination": destination,
                "vehicle_hint": vehicle_hint,
            },
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
            metadata={
                "source": "turn_orchestrator",
                "user_text": plan.user_text,
            },
        )
        await upsert_action_ledger_entry(entry)
    except Exception as e:
        logger.warning(f"Failed to create Tesla navigation action ledger entry: {e}")
        return await runtime.finish("I recognized the Tesla navigation request, but I could not create a durable action record. I will not try to send it without that ledger.", "tesla_navigation_ledger_failed")
    state.active_action_id = entry.action_id
    state.active_goal = plan.goal
    vehicle_text = f" to {vehicle_hint}" if vehicle_hint else ""
    response = (
        f"I prepared a Tesla navigation action{vehicle_text} for: {destination}. "
        "I have not sent it yet. Say yes or go ahead if you want me to execute it."
    )
    return await runtime.finish(response, "tesla_navigation_awaiting_confirmation")


STRATEGY_HANDLERS: dict[TurnIntent, StrategyHandler] = {
    TurnIntent.AUTO_ACTION: _run_auto_action,
    TurnIntent.CLARIFICATION: _run_clarification,
    TurnIntent.ACTIVE_ACTION_STATUS: _run_active_action_status,
    TurnIntent.ACTIVE_ACTION_CONFIRMATION: _run_active_action_confirmation,
    TurnIntent.ACTIVE_ACTION_RETRY: _run_active_action_retry,
    TurnIntent.ACTIVE_ACTION_FAILURE_REPORT: _run_active_action_failure_report,
    TurnIntent.TESLA_NAVIGATION_PLAN: _run_tesla_navigation_plan,
    TurnIntent.WEATHER_LOOKUP: _run_weather_lookup,
    TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION: _run_lookup_then_workspace_creation,
    TurnIntent.WORKSPACE_CREATION: _run_workspace_creation,
    TurnIntent.WORKSPACE_CONTEXT_CONTINUATION: _run_lookup_then_workspace_creation,
    TurnIntent.TASK_ARTIFACT_CONTINUATION: _run_task_artifact_continuation,
    TurnIntent.WORKSPACE_CREATION_CONTINUATION: _run_workspace_creation,
    TurnIntent.CALENDAR_LOOKUP: _run_calendar_lookup,
    TurnIntent.EMAIL_LOOKUP: _run_email_lookup,
    TurnIntent.CONVERSATION_RECALL: _run_conversation_recall,
    TurnIntent.CONTEXT_CONTINUATION: _run_context_continuation,
    TurnIntent.PERSONAL_MEMORY_RECALL: _run_personal_memory_recall,
    TurnIntent.CURRENT_EVENTS_LOOKUP: _run_current_events_lookup,
    TurnIntent.BUSINESS_DIRECTIONS_LOOKUP: _run_business_directions_lookup,
    TurnIntent.WORKFLOW_TRIGGER: _run_workflow_trigger,
    TurnIntent.WORKFLOW_STATUS: _run_workflow_status,
}


async def execute_turn_plan_result(
    plan: TurnPlan,
    state: TurnState,
    dispatch_tool: DispatchTool,
    send_server_msg: ServerMessage,
    persist_turn: PersistTurn,
    user_id: str = "default",
    conversation_id: str = "",
    session_id: str = "",
) -> TurnExecutionResult:
    if plan.intent == TurnIntent.PASS_THROUGH:
        result = TurnExecutionResult(handled=False, intent=plan.intent.value)
        _METRICS.record(result)
        _record_policy_outcome(plan.user_text, state, result)
        return result

    started = time.monotonic()
    telemetry = TurnTelemetry(intent=plan.intent.value, goal=plan.goal)
    logger.info(f"TurnOrchestrator executing intent={plan.intent} goal={plan.goal[:80]}")
    await persist_turn("user", plan.user_text)
    handler = STRATEGY_HANDLERS.get(plan.intent)
    if handler is None:
        result = TurnExecutionResult(handled=False, intent=plan.intent.value)
        _METRICS.record(result)
        _record_policy_outcome(plan.user_text, state, result)
        return result
    runtime = TurnRuntime(
        plan=plan,
        state=state,
        dispatch_tool=dispatch_tool,
        send_server_msg=send_server_msg,
        persist_turn=persist_turn,
        telemetry=telemetry,
        started=started,
        user_id=user_id,
        conversation_id=conversation_id,
        session_id=session_id,
    )
    result = await handler(runtime)
    _METRICS.record(result, telemetry.latency_ms)
    _record_policy_outcome(plan.user_text, state, result, telemetry.latency_ms)
    return result


async def execute_turn_plan(
    plan: TurnPlan,
    state: TurnState,
    dispatch_tool: DispatchTool,
    send_server_msg: ServerMessage,
    persist_turn: PersistTurn,
    user_id: str = "default",
    conversation_id: str = "",
    session_id: str = "",
) -> bool:
    result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn, user_id, conversation_id, session_id)
    return result.handled
