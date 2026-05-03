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
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from loguru import logger

from nova.turn_policy import build_policy_observation, extract_location_prefix, extract_turn_features, log_policy_observation, shadow_policy_predict


DispatchTool = Callable[[str, dict[str, Any]], Awaitable[str]]
ServerMessage = Callable[[dict[str, Any]], Awaitable[None]]
PersistTurn = Callable[[str, str], Awaitable[None]]
StrategyHandler = Callable[["TurnRuntime"], Awaitable["TurnExecutionResult"]]
STATE_METADATA_KEY = "nova_turn_orchestrator"


class TurnIntent(str, Enum):
    PASS_THROUGH = "pass_through"
    CLARIFICATION = "clarification"
    WEATHER_LOOKUP = "weather_lookup"
    WORKSPACE_CREATION = "workspace_creation"
    LOOKUP_THEN_WORKSPACE_CREATION = "lookup_then_workspace_creation"
    WORKSPACE_CREATION_CONTINUATION = "workspace_creation_continuation"
    EMAIL_LOOKUP = "email_lookup"
    WORKFLOW_TRIGGER = "workflow_trigger"
    WORKFLOW_STATUS = "workflow_status"


@dataclass
class TurnPlan:
    intent: TurnIntent
    goal: str
    user_text: str
    evidence_budget: dict[str, int] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnExecutionResult:
    handled: bool
    response: str = ""
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = ""
    intent: str = ""


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


async def _persist_policy_observation(observation) -> None:
    try:
        from nova.store import append_turn_policy_observation
        await append_turn_policy_observation(observation)
    except Exception as e:
        logger.warning(f"Failed to persist turn policy observation: {e}")


def _record_policy_outcome(
    text: str,
    state: "TurnState",
    result: "TurnExecutionResult",
    latency_ms: int = 0,
) -> None:
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
    asyncio.create_task(_persist_policy_observation(observation))


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
    last_intent: str = ""
    turns_handled: int = 0

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
            "last_intent": self.last_intent,
            "turns_handled": self.turns_handled,
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
            last_intent=str(data.get("last_intent") or ""),
            turns_handled=int(data.get("turns_handled") or 0),
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

    async def finish(self, response: str, stop_reason: str) -> TurnExecutionResult:
        self.telemetry.stop_reason = stop_reason
        self.telemetry.latency_ms = int((time.monotonic() - self.started) * 1000)
        self.state.last_intent = self.plan.intent.value
        self.state.turns_handled += 1
        await self.send_server_msg({
            "type": "validated",
            "text": response,
            "speechText": response,
            "result": "turn_orchestrator",
            "suppressSpeech": False,
        })
        await self.send_server_msg({"type": "turn_complete"})
        await self.send_server_msg({"phase": "done"})
        await self.persist_turn("assistant", response)
        logger.info(f"NOVA_TURN_ORCHESTRATOR | {self.telemetry.to_log_fields()}")
        return TurnExecutionResult(
            handled=True,
            response=response,
            tools_used=list(self.telemetry.tools_used),
            stop_reason=stop_reason,
            intent=self.plan.intent.value,
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
        if card:
            await self.send_server_msg({
                "type": "card",
                "kind": card.get("kind", "generic"),
                "tool": result,
                "data": card,
            })
        await self.send_server_msg({
            "type": "validated",
            "text": display_text,
            "speechText": speech_text,
            "result": result,
            "suppressSpeech": False,
        })
        await self.send_server_msg({"type": "turn_complete"})
        await self.send_server_msg({"phase": "done"})
        await self.persist_turn("assistant", display_text)
        logger.info(f"NOVA_TURN_ORCHESTRATOR | {self.telemetry.to_log_fields()}")
        return TurnExecutionResult(
            handled=True,
            response=display_text,
            tools_used=list(self.telemetry.tools_used),
            stop_reason=stop_reason,
            intent=self.plan.intent.value,
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
    cleaned = re.sub(r"^\[User location:[^\]]+\]\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?🧭 MODE POLICY:.*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


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


def decide_turn(text: str, state: TurnState) -> TurnPlan:
    user_text = clean_user_text(text)
    lower = user_text.lower()
    features = extract_turn_features(text, state)
    shadow_candidate = shadow_policy_predict(features)

    wants_workspace = _contains_any(lower, ("workspace", "page", "pages", "document", "documents", "advisory", "advisories", "report", "brief"))
    wants_lookup = _contains_any(lower, ("find", "lookup", "search", "email", "thread", "message"))
    confirms_structure = _answers_workspace_structure(lower)
    complex_workspace = wants_workspace and _contains_any(lower, ("create", "make", "build", "write", "construct", "draft"))

    if _wants_workflow_status(lower, state):
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WORKFLOW_STATUS.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WORKFLOW_STATUS,
            goal=state.active_workflow_goal or "Check workflow status.",
            user_text=user_text,
            evidence_budget={"hub_delegate": 1},
            allowed_tools=["hub_delegate"],
            stop_conditions=["return durable workflow status"],
            context={
                "workflow_run_id": state.active_workflow_run_id,
                "workflow_name": state.active_workflow_name,
            },
        )

    if _wants_research_workflow(lower):
        topic = _derive_research_topic(user_text)
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WORKFLOW_TRIGGER.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WORKFLOW_TRIGGER,
            goal=_derive_goal(user_text, "Start a durable research briefing workflow."),
            user_text=user_text,
            evidence_budget={"hub_delegate": 1},
            allowed_tools=["hub_delegate"],
            stop_conditions=["trigger durable workflow", "store workflow run id"],
            context={
                "workflow_name": "atlas-research-brief",
                "input": {"topic": topic, "depth": "standard"},
            },
        )

    if state.pending_clarification == "workspace_structure" and confirms_structure:
        requested_topics = _extract_requested_topics(user_text)
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WORKSPACE_CREATION_CONTINUATION.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WORKSPACE_CREATION_CONTINUATION,
            goal=state.active_goal or "Create the requested workspace document(s).",
            user_text=user_text,
            evidence_budget={"hub_delegate": 1},
            allowed_tools=["hub_delegate"],
            stop_conditions=["user answered clarification", "delegate to Scribe"],
            context={"confirmed_structure": user_text, "topics": requested_topics},
        )

    if state.pending_scribe and (confirms_structure or wants_workspace):
        requested_topics = _extract_requested_topics(user_text)
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WORKSPACE_CREATION_CONTINUATION.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WORKSPACE_CREATION_CONTINUATION,
            goal=state.active_goal or "Create the requested workspace document(s).",
            user_text=user_text,
            evidence_budget={"hub_delegate": 1},
            allowed_tools=["hub_delegate"],
            stop_conditions=["user confirmed output structure", "delegate to Scribe"],
            context={"confirmed_structure": user_text, "topics": requested_topics},
        )

    if wants_workspace and wants_lookup:
        log_policy_observation(features=features, deterministic_intent=TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION,
            goal=_derive_goal(user_text, "Create workspace document(s) from relevant retrieved context."),
            user_text=user_text,
            evidence_budget={"query_cig": 1, "hub_delegate": 1},
            allowed_tools=["query_cig", "hub_delegate"],
            stop_conditions=["found relevant email context", "user confirms output structure", "delegate to Scribe"],
            context={"lookup_query": _derive_lookup_query(user_text)},
        )

    if complex_workspace and not _extract_requested_topics(user_text) and not confirms_structure:
        log_policy_observation(features=features, deterministic_intent=TurnIntent.CLARIFICATION.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.CLARIFICATION,
            goal=_derive_goal(user_text, "Clarify the requested workspace document structure."),
            user_text=user_text,
            evidence_budget={},
            allowed_tools=[],
            stop_conditions=["ask one focused clarification question"],
            context={
                "clarification_key": "workspace_structure",
                "question": "Do you want this as one polished page, or one page per topic?",
            },
        )

    if complex_workspace:
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WORKSPACE_CREATION.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WORKSPACE_CREATION,
            goal=_derive_goal(user_text, "Create the requested workspace document(s) through Scribe."),
            user_text=user_text,
            evidence_budget={"hub_delegate": 1},
            allowed_tools=["hub_delegate"],
            stop_conditions=["delegate complex workspace creation to Scribe"],
            context={"topics": _extract_requested_topics(user_text)},
        )

    if wants_lookup and "email" in lower and not wants_workspace:
        log_policy_observation(features=features, deterministic_intent=TurnIntent.EMAIL_LOOKUP.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.EMAIL_LOOKUP,
            goal="Find the requested email context.",
            user_text=user_text,
            evidence_budget={"query_cig": 1},
            allowed_tools=["query_cig"],
            stop_conditions=["return concise email lookup result"],
        )

    if _wants_weather(lower):
        location = extract_location_prefix(text)
        log_policy_observation(features=features, deterministic_intent=TurnIntent.WEATHER_LOOKUP.value, shadow_candidate=shadow_candidate)
        return TurnPlan(
            intent=TurnIntent.WEATHER_LOOKUP,
            goal="Return current outdoor weather with structured visual display and natural speech.",
            user_text=user_text,
            evidence_budget={"get_weather": 1},
            allowed_tools=["get_weather"],
            stop_conditions=[
                "call get_weather exactly once",
                "send structured display and clean speech",
                "do not let the LLM fabricate current weather",
            ],
            context={"location": location},
        )

    log_policy_observation(features=features, deterministic_intent=TurnIntent.PASS_THROUGH.value, shadow_candidate=shadow_candidate)
    return TurnPlan(intent=TurnIntent.PASS_THROUGH, goal="", user_text=user_text)


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


async def _run_lookup_then_workspace_creation(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    state.active_goal = plan.goal
    state.pending_scribe = True
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Finding the relevant email context, then preparing the Scribe work order..."})
    result = await runtime.dispatch_tool(
        "query_cig",
        {
            "domain": "search",
            "query": plan.context.get("lookup_query") or plan.user_text,
        },
    )
    runtime.telemetry.tools_used.append("query_cig")
    if result:
        state.known_context.append(str(result)[:1200])
    response = (
        "I found enough context to proceed. Tell me the page structure or topics you want, "
        "and I’ll send a focused work order to Scribe instead of continuing to search."
    )
    return await runtime.finish(response, "awaiting_output_structure")


async def _run_workspace_creation(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    state = runtime.state
    state.active_goal = plan.goal or state.active_goal
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Delegating the workspace work to Scribe now..."})
    work_order = _build_scribe_work_order(plan, state)
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
    response = f"I sent the workspace work order to Scribe. {str(result)[:600]}"
    state.pending_scribe = False
    state.pending_clarification = ""
    return await runtime.finish(response, "delegated_to_scribe")


async def _run_email_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking the requested email context..."})
    result = await runtime.dispatch_tool(
        "query_cig",
        {"domain": "search", "query": plan.user_text},
    )
    runtime.telemetry.tools_used.append("query_cig")
    response = str(result)[:900] if result else "I couldn't find matching email context."
    return await runtime.finish(response, "email_lookup_complete")


async def _run_clarification(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    runtime.state.active_goal = plan.goal or runtime.state.active_goal
    runtime.state.pending_clarification = str(plan.context.get("clarification_key") or "")
    question = str(plan.context.get("question") or "Can you clarify the structure you want?")
    return await runtime.finish(question, "awaiting_clarification")


async def _run_weather_lookup(runtime: TurnRuntime) -> TurnExecutionResult:
    plan = runtime.plan
    location = str(plan.context.get("location") or "").strip() or "Humble, TX"
    await runtime.send_server_msg({"phase": "thinking"})
    await runtime.send_server_msg({"type": "thinking", "text": "Checking the current outdoor weather..."})
    result = await runtime.dispatch_tool("get_weather", {"location": location})
    runtime.telemetry.tools_used.append("get_weather")
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
    response = f"Workflow status for {state.active_workflow_name or 'the active workflow'}: {str(result)[:700]}"
    return await runtime.finish(response, "workflow_status_returned")


STRATEGY_HANDLERS: dict[TurnIntent, StrategyHandler] = {
    TurnIntent.CLARIFICATION: _run_clarification,
    TurnIntent.WEATHER_LOOKUP: _run_weather_lookup,
    TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION: _run_lookup_then_workspace_creation,
    TurnIntent.WORKSPACE_CREATION: _run_workspace_creation,
    TurnIntent.WORKSPACE_CREATION_CONTINUATION: _run_workspace_creation,
    TurnIntent.EMAIL_LOOKUP: _run_email_lookup,
    TurnIntent.WORKFLOW_TRIGGER: _run_workflow_trigger,
    TurnIntent.WORKFLOW_STATUS: _run_workflow_status,
}


async def execute_turn_plan_result(
    plan: TurnPlan,
    state: TurnState,
    dispatch_tool: DispatchTool,
    send_server_msg: ServerMessage,
    persist_turn: PersistTurn,
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
) -> bool:
    result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
    return result.handled
