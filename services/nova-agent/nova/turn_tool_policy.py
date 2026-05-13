from __future__ import annotations

from dataclasses import dataclass, field


CORE_TOOL_NAMES = {
    "get_time",
    "get_weather",
    "recall_memory",
    "save_memory",
    "search_past_conversations",
    "web_search",
    "hub_delegate",
    "set_active_goal",
    "complete_active_goal",
    "manage_task_plan",
    "query_self_state",
}

TOOL_GROUPS = {
    "communications": {"query_cig", "check_studio", "search_past_conversations"},
    "workspace": {"manage_workspace", "manage_notes", "query_workspace", "hub_delegate"},
    "homelab": {"service_status", "service_logs", "service_health_check", "homelab_diagnostics", "homelab_operations"},
    "tesla": {"tesla_control", "tesla_stream_monitor", "tesla_location_refresh", "tesla_wake", "tesla_navigation"},
    "context": {"query_context", "kg_query", "knowledge_query", "get_enriched_context", "query_frameworks"},
    "vision": {"analyze_image"},
    "learning": {"staar_tutor"},
}

INTENT_TOOL_NAMES = {
    "pass_through": CORE_TOOL_NAMES,
    "auto_action": CORE_TOOL_NAMES,
    "clarification": set(),
    "weather_lookup": {"get_weather"},
    "calendar_lookup": {"check_studio"},
    "email_lookup": {"query_cig", "check_studio"},
    "conversation_recall": {"search_past_conversations"},
    "personal_memory_recall": {"recall_memory"},
    "current_events_lookup": {"web_search"},
    "workspace_creation": {"hub_delegate", "manage_workspace", "manage_notes"},
    "lookup_then_workspace_creation": {"query_cig", "search_past_conversations", "hub_delegate"},
    "workspace_context_continuation": {"search_past_conversations", "hub_delegate"},
    "task_artifact_continuation": {"hub_delegate"},
    "workspace_creation_continuation": {"hub_delegate", "manage_workspace", "manage_notes"},
    "workflow_trigger": {"hub_delegate"},
    "workflow_status": {"hub_delegate"},
    "llm_active_action_confirmation": {"tesla_navigation", "tesla_control"},
}

GENERIC_LEARNING_TRIGGERS = {
    "try again",
    "that's correct",
    "thats correct",
    "yes go ahead",
    "go ahead",
    "do it",
    "ok",
    "okay",
    "sure",
    "please",
    "continue",
    "that's right",
    "thats right",
    "no",
    "stop",
    "wrong",
    "cancel",
}

TOOL_INTENT_FAMILY = {
    "get_weather": "weather",
    "query_cig": "communications",
    "check_studio": "communications",
    "web_search": "web",
    "search_past_conversations": "memory",
    "recall_memory": "memory",
    "save_memory": "memory",
    "forget_memory": "memory",
    "tesla_control": "tesla",
    "tesla_stream_monitor": "tesla",
    "tesla_location_refresh": "tesla",
    "tesla_wake": "tesla",
    "tesla_navigation": "tesla",
    "hub_delegate": "delegation",
    "manage_workspace": "workspace",
    "manage_notes": "workspace",
    "query_workspace": "workspace",
}

INTENT_FAMILY = {
    "weather_lookup": "weather",
    "email_lookup": "communications",
    "web_research_request": "web",
    "conversation_recall_request": "memory",
    "memory_recall_request": "memory",
    "memory_save_request": "memory",
    "tesla_control": "tesla",
    "workspace_creation": "workspace",
    "lookup_then_workspace_creation": "workspace",
    "workspace_creation_continuation": "workspace",
    "hub_delegate": "delegation",
}


@dataclass(frozen=True)
class ToolBudget:
    names: list[str]
    reason: str
    groups: list[str] = field(default_factory=list)
    nudge_level: int = 0
    activation: float = 0.0
    confidence: float = 0.0
    learning_rate: float = 0.05
    optimizer: str = "bounded_relu_softmax"
    candidate_id: int | None = None
    candidate_intent: str = ""
    suggested_tools: list[str] = field(default_factory=list)
    gradient_hint: str = "none"


@dataclass(frozen=True)
class LearnedNudgeParams:
    confidence_floor: float = 0.60
    quarantine_threshold: float = 0.60
    bounded_assist_threshold: float = 0.70
    deterministic_threshold: float = 0.85
    promotion_threshold: float = 0.90
    learning_rate: float = 0.05
    momentum: float = 0.90
    weight_decay: float = 0.01
    temperature: float = 0.15
    max_gradient_norm: float = 0.25


DEFAULT_LEARNED_NUDGE_PARAMS = LearnedNudgeParams()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _float_candidate_value(candidate: dict | None, key: str) -> float:
    if not candidate:
        return 0.0
    try:
        return float(candidate.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _learned_candidate_tools(candidate: dict | None) -> list[str]:
    if not candidate:
        return []
    raw_tools = candidate.get("suggested_tools") or candidate.get("tools_used") or []
    return [str(tool) for tool in raw_tools if str(tool or "").strip()]


def _learned_nudge_level(confidence: float, params: LearnedNudgeParams) -> int:
    if confidence >= params.deterministic_threshold:
        return 3
    if confidence >= params.bounded_assist_threshold:
        return 2
    if confidence >= params.quarantine_threshold:
        return 1
    return 0


def _learned_activation(confidence: float, params: LearnedNudgeParams) -> float:
    return max(0.0, confidence - params.confidence_floor)


def _text_supports_learned_intent(text: str, intent: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    if not normalized:
        return False
    if intent in {"email_lookup", "email"}:
        return _contains_any(normalized, ("email", "inbox", "message", "thread", "calendar", "meeting", "contact", "studio", "cig"))
    if intent in {"conversation_recall", "conversation_recall_request"}:
        return _contains_any(normalized, (
            "conversation", "conversations", "discussed", "talked about", "previous", "earlier",
            "recall", "remember when", "what did we", "past conversation", "analysis", "case study",
            "what did you find", "what did you find out", "did you get", "results", "from before",
        ))
    if intent in {"personal_memory_recall", "memory_recall_request"}:
        return _contains_any(normalized, ("remember about me", "my preference", "my preferences", "my goal", "my goals", "what do you know about me", "personal memory"))
    if intent in {"current_events_lookup", "web_research_request"}:
        return _contains_any(normalized, ("latest", "recent", "current", "today", "right now", "this week", "news", "market", "stock", "release", "launched", "announced"))
    if intent == "weather_lookup":
        return _contains_any(normalized, ("weather", "forecast", "rain", "temperature", "outside", "humidity", "wind"))
    return False


def select_tool_budget(
    text: str,
    all_tool_names: list[str],
    intent: str = "pass_through",
    learned_candidate: dict | None = None,
    nudge_params: LearnedNudgeParams = DEFAULT_LEARNED_NUDGE_PARAMS,
) -> ToolBudget:
    lower = (text or "").lower()
    if intent == "pass_through" and _contains_any(lower, ("[active action binding context]", "active_action_binding_context")):
        selected = INTENT_TOOL_NAMES["llm_active_action_confirmation"]
        return ToolBudget([name for name in all_tool_names if name in selected], "active_action_binding_context:tesla_only", ["tesla"])
    
    if intent == "pass_through":
        selected = set(all_tool_names)
    else:
        selected = set(INTENT_TOOL_NAMES.get(intent, CORE_TOOL_NAMES))
    groups: list[str] = []

    if intent != "pass_through":
        return ToolBudget([name for name in all_tool_names if name in selected], f"intent:{intent}", groups)

    confidence = _float_candidate_value(learned_candidate, "confidence")
    activation = _learned_activation(confidence, nudge_params)
    nudge_level = _learned_nudge_level(confidence, nudge_params)
    suggested_tools = _learned_candidate_tools(learned_candidate)
    candidate_intent = str((learned_candidate or {}).get("intent") or "")
    text_supports_candidate = _text_supports_learned_intent(text, candidate_intent)
    candidate_id = (learned_candidate or {}).get("id")
    if candidate_id is None and isinstance((learned_candidate or {}).get("raw_candidate"), dict):
        candidate_id = (learned_candidate or {}).get("raw_candidate", {}).get("id")
    candidate_id = int(candidate_id) if isinstance(candidate_id, int) or str(candidate_id or "").isdigit() else None

    if nudge_level >= 2 and suggested_tools and text_supports_candidate:
        bounded = set(suggested_tools) | {"get_time"}
        return ToolBudget(
            [name for name in all_tool_names if name in bounded],
            f"learned_nudge:bounded_assist:{candidate_intent or 'unknown'}",
            ["learned_bounded_assist"],
            nudge_level=nudge_level,
            activation=activation,
            confidence=confidence,
            learning_rate=nudge_params.learning_rate,
            candidate_id=candidate_id,
            candidate_intent=candidate_intent,
            suggested_tools=suggested_tools,
            gradient_hint="increase_on_final_success_decrease_on_stall_or_correction",
        )

    if nudge_level >= 1:
        selected = {"get_time", "recall_memory", "save_memory"}
        return ToolBudget(
            [name for name in all_tool_names if name in selected],
            f"learned_nudge:quarantine:{candidate_intent or 'unknown'}",
            ["learned_quarantine"],
            nudge_level=nudge_level,
            activation=activation,
            confidence=confidence,
            learning_rate=nudge_params.learning_rate,
            candidate_id=candidate_id,
            candidate_intent=candidate_intent,
            suggested_tools=suggested_tools,
            gradient_hint="require_current_text_support_before_tool_activation" if not text_supports_candidate else "hold_until_more_margin_or_success_evidence",
        )

    if _contains_any(lower, ("email", "calendar", "meeting", "inbox", "thread", "contact", "studio", "cig")):
        selected |= TOOL_GROUPS["communications"]
        groups.append("communications")
    if _contains_any(lower, ("workspace", "page", "document", "note", "notes", "notion", "picode")):
        selected |= TOOL_GROUPS["workspace"]
        groups.append("workspace")
    if _contains_any(lower, ("service", "container", "homelab", "diagnostic", "logs", "restart", "health", "nova slow")):
        selected |= TOOL_GROUPS["homelab"]
        groups.append("homelab")
    if _contains_any(lower, ("tesla", "car", "vehicle", "charge", "climate", "trunk", "lock")):
        selected |= TOOL_GROUPS["tesla"]
        groups.append("tesla")
    if _contains_any(lower, ("framework", "liam", "knowledge graph", "memory", "preference", "goal", "decision")):
        selected |= TOOL_GROUPS["context"]
        groups.append("context")
    if _contains_any(lower, ("image", "photo", "picture", "screenshot", "attachment")):
        selected |= TOOL_GROUPS["vision"]
        groups.append("vision")
    if _contains_any(lower, ("staar", "math", "problem", "teks", "tutor")):
        selected |= TOOL_GROUPS["learning"]
        groups.append("learning")

    return ToolBudget(
        [name for name in all_tool_names if name in selected],
        "fallback:keyword_groups",
        groups,
        nudge_level=nudge_level,
        activation=activation,
        confidence=confidence,
        learning_rate=nudge_params.learning_rate,
        candidate_id=candidate_id,
        candidate_intent=candidate_intent,
        suggested_tools=suggested_tools,
    )


def learned_candidate_allowed(text: str, candidate: dict, deterministic_intent: str = "pass_through") -> tuple[bool, str]:
    normalized = " ".join((text or "").lower().strip().split())
    if normalized in GENERIC_LEARNING_TRIGGERS or len(normalized.split()) < 3:
        return False, "generic_or_too_short_trigger"

    confidence = float(candidate.get("confidence") or 0.0)
    if confidence < 0.80:
        return False, "confidence_below_assistive_threshold"

    tools = candidate.get("tools_used") or []
    if not tools:
        return False, "missing_tools"

    candidate_intent = str(candidate.get("intent") or "")
    candidate_family = INTENT_FAMILY.get(candidate_intent)
    tool_families = {TOOL_INTENT_FAMILY.get(str(tool)) for tool in tools}
    tool_families.discard(None)

    if candidate_family and tool_families and candidate_family not in tool_families:
        return False, "intent_tool_family_mismatch"

    if deterministic_intent != "pass_through":
        deterministic_family = INTENT_FAMILY.get(deterministic_intent)
        if deterministic_family and candidate_family and deterministic_family != candidate_family:
            return False, "deterministic_intent_mismatch"

    return True, "allowed"
