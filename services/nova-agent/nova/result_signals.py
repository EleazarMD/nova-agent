"""
Situational result signals for Nova's tool loop.

After a tool call completes, this module inspects the result and decides
whether to append a short, contextual signal to the result string before
it's returned to the LLM.

Unlike a checkpoint harness, signals fire ONLY when something specific is
detected (empty result, repeated tool family, stale duplicate). The LLM
sees a one-line nudge, not a required reasoning protocol — preserving its
freedom to use its own expert routing while preventing the most common
shallow-loop failure modes.
"""

from __future__ import annotations

from typing import Optional


# Tool family groupings — used to detect "deep on a single area" patterns
_TOOL_FAMILIES = {
    "manage_workspace": "workspace",
    "manage_notes": "workspace",
    "query_workspace": "workspace",
    "manage_task_plan": "planning",
    "set_active_goal": "planning",
    "complete_active_goal": "planning",
    "query_cig": "communications",
    "check_studio": "communications",
    "search_past_conversations": "memory",
    "recall_memory": "memory",
    "save_memory": "memory",
    "query_frameworks": "frameworks",
    "search_framework_catalog": "frameworks",
    "web_search": "web",
    "service_status": "homelab",
    "homelab_diagnostics": "homelab",
    "homelab_operations": "homelab",
}

_EMPTY_MARKERS = (
    "no results",
    "no past conversations found",
    "no emails found",
    "thread lookup failed",
    "thread not found",
    "page is empty",
    "no events on your calendar",
    "no upcoming calendar events",
    "could not retrieve",
    "couldn't retrieve",
    "not found",
)


def family_of(tool_name: str) -> Optional[str]:
    return _TOOL_FAMILIES.get(tool_name)


def detect_signal(
    tool_name: str,
    result_str: str,
    tool_history: list[str],
    failures: list[str],
) -> Optional[str]:
    """Return a one-line signal to append, or None if the result is fine.

    Args:
        tool_name: the tool that just returned
        result_str: the tool's result string
        tool_history: ordered list of all tools called this turn
        failures: list of tool calls that failed/returned empty so far
    """
    lower = (result_str or "").lower()

    # Signal 1 — Empty / not-found result
    if any(marker in lower for marker in _EMPTY_MARKERS):
        return (
            "[SIGNAL: empty result. This source has no data for the query. "
            "Do NOT call this tool again with the same args. "
            "Either pivot to a different tool/source, or surface what you have to the user.]"
        )

    # Signal 2 — Deep on one family (≥3 calls in same family this turn)
    fam = family_of(tool_name)
    if fam:
        same_family_count = sum(
            1 for t in tool_history if family_of(t) == fam
        )
        if same_family_count >= 3:
            return (
                f"[SIGNAL: {same_family_count} {fam}-family calls this turn. "
                "If you're not making forward progress on the goal, pivot to a different "
                "approach or surface what you have. Don't keep drilling the same well.]"
            )

    # Signal 3 — High overall call count, no recent failure but lots of churn
    if len(tool_history) >= 8:
        return (
            f"[SIGNAL: {len(tool_history)} tool calls this turn. "
            "Time to surface — review what you've collected and respond to the user. "
            "Additional calls should only happen if a USER-requested action is still incomplete.]"
        )

    # Signal 4 — Repeated failure in same family
    if fam and len(failures) >= 2:
        recent_fail_families = [
            family_of(f.split()[-1]) for f in failures[-2:] if f.split()
        ]
        if all(f == fam for f in recent_fail_families if f):
            return (
                f"[SIGNAL: {fam} family has failed twice. "
                "Stop trying the same approach. Either pivot to a different source "
                "or tell the user what you tried and ask how they want to proceed.]"
            )

    return None
