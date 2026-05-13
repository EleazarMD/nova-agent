from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
import uuid
from typing import Any


ACTIVE_ACTION_STATUSES = {
    "planned",
    "awaiting_confirmation",
    "running",
    "tool_failed",
    "evidence_missing",
    "needs_clarification",
}

TERMINAL_ACTION_STATUSES = {
    "completed",
    "cancelled",
}

VALID_ACTION_STATUSES = ACTIVE_ACTION_STATUSES | TERMINAL_ACTION_STATUSES


@dataclass
class ActionLedgerEntry:
    action_id: str
    intent: str
    status: str
    user_id: str = ""
    conversation_id: str = ""
    session_id: str = ""
    parent_turn_id: str = ""
    active_goal: str = ""
    target_json: dict[str, Any] = field(default_factory=dict)
    required_tools: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    tool_attempts: list[dict[str, Any]] = field(default_factory=list)
    last_tool_result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    evidence_status: str = "missing"
    user_visible_status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionEvidence:
    source: str
    status: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_action_id() -> str:
    return str(uuid.uuid4())


def create_action_entry(
    *,
    intent: str,
    active_goal: str,
    status: str = "planned",
    user_id: str = "",
    conversation_id: str = "",
    session_id: str = "",
    parent_turn_id: str = "",
    target_json: dict[str, Any] | None = None,
    required_tools: list[str] | None = None,
    required_evidence: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: float = 0.0,
) -> ActionLedgerEntry:
    if status not in VALID_ACTION_STATUSES:
        raise ValueError(f"Invalid action ledger status: {status}")
    return ActionLedgerEntry(
        action_id=new_action_id(),
        intent=intent,
        status=status,
        user_id=user_id,
        conversation_id=conversation_id,
        session_id=session_id,
        parent_turn_id=parent_turn_id,
        active_goal=active_goal,
        target_json=target_json or {},
        required_tools=required_tools or [],
        required_evidence=required_evidence or [],
        metadata=metadata or {},
        expires_at=expires_at,
    )


def action_is_active(entry: dict[str, Any] | ActionLedgerEntry | None) -> bool:
    if entry is None:
        return False
    status = entry.status if isinstance(entry, ActionLedgerEntry) else str(entry.get("status") or "")
    return status in ACTIVE_ACTION_STATUSES
