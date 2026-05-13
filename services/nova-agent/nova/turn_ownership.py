from __future__ import annotations

from typing import Any


ORCHESTRATOR_CONSUMED_LLM_FRAME_TYPES = {
    "LLMRunFrame",
    "LLMMessagesAppendFrame",
    "LLMMessagesUpdateFrame",
}


def should_consume_llm_frame_after_orchestrator(
    frame: Any,
    active_turn: Any,
    orchestrator_consumed_turn_id: str,
) -> bool:
    snapshot = getattr(active_turn, "snapshot", None)
    if snapshot is None:
        return False
    if not getattr(snapshot, "turn_complete_sent", False):
        return False
    turn_id = str(getattr(snapshot, "turn_id", "") or "")
    if not orchestrator_consumed_turn_id or orchestrator_consumed_turn_id != turn_id:
        return False
    return type(frame).__name__ in ORCHESTRATOR_CONSUMED_LLM_FRAME_TYPES


def should_fail_closed_after_turn_ingress_error(text: str, active_turn: Any) -> bool:
    if not str(text or "").strip():
        return False
    return getattr(active_turn, "snapshot", None) is not None
