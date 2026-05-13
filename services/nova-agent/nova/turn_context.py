"""
Turn-level reasoning scaffold for Nova.

Provides a lightweight `TurnContext` that travels with each user turn and is
rendered as a compact header appended to tool results. The goal is to anchor
the LLM's attention on:

  1. The original user goal (so it doesn't drift across long tool chains)
  2. What evidence has been collected and what's still missing
  3. Whether to dive deeper, pivot, or surface

This is a *scaffold*, not a harness. The LLM is free to ignore the header —
it's a contextual nudge that gives a strong reasoning model (MiniMax M2.x)
the situational awareness to keep its long-horizon orientation without
constraining its expert routing.

Design principles:
  - One Python-side data object per turn, mutated as tools execute
  - Compact rendered output (~80-150 tokens) appended to tool results
  - No required schema for the LLM to follow
  - Completion gate fires once at turn end if work is incomplete
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Tools that should NOT trigger TurnContext header injection — they are
# cheap, narrow lookups where the extra context adds noise without value.
_LIGHT_TOOLS = {
    "get_time",
    "get_weather",
    "get_workstation_status",
    "manage_timer",
    "control_lights",
}


@dataclass
class EvidenceEntry:
    """A single tool call's contribution to the turn's evidence."""
    tool: str
    args_preview: str
    summary: str
    useful: bool
    seq: int


@dataclass
class TurnContext:
    """Reasoning scaffold for a single user turn.

    Created when a new user message arrives. Updated by the tool handler
    after each tool call. Rendered as a header appended to the tool result
    string so the LLM sees its own goal and progress at every decision point.
    """
    turn_id: str
    user_text: str
    goal: str
    intent: str
    evidence_budget: int = 3

    # Tools called this turn, in order
    tool_history: list[str] = field(default_factory=list)
    # Evidence collected (one entry per useful tool result)
    evidence_log: list[EvidenceEntry] = field(default_factory=list)
    # Tools that returned errors / empty / not-found
    failures: list[str] = field(default_factory=list)

    # Posture: how Nova should approach the next decision
    #   diving      — gathering evidence, more tool calls expected
    #   pivoting    — last attempt failed, try a different angle
    #   surfacing   — enough evidence, prepare to respond
    #   blocked     — stuck; should escalate to user
    posture: str = "diving"

    # Once true, completion gate has fired and we should not re-fire
    completion_check_emitted: bool = False

    # ── Updates ────────────────────────────────────────────────────────────

    def record_tool_call(
        self,
        tool: str,
        args_preview: str,
        result_preview: str,
        useful: bool,
    ) -> None:
        """Record one tool call's outcome and update posture."""
        self.tool_history.append(tool)
        seq = len(self.tool_history)
        if useful:
            self.evidence_log.append(EvidenceEntry(
                tool=tool,
                args_preview=args_preview[:80],
                summary=_summarize_result(result_preview),
                useful=True,
                seq=seq,
            ))
        else:
            self.failures.append(f"#{seq} {tool}")
        self._refresh_posture()

    def _refresh_posture(self) -> None:
        n_calls = len(self.tool_history)
        n_evidence = len(self.evidence_log)
        n_failures = len(self.failures)

        # Stuck: many calls, little evidence
        if n_calls >= 6 and n_evidence == 0:
            self.posture = "blocked"
            return
        # Enough evidence: budget reached or exceeded
        if n_evidence >= max(1, self.evidence_budget):
            self.posture = "surfacing"
            return
        # Recent failure: pivot before next call
        if n_failures > 0 and self.tool_history and self.tool_history[-1] in self.failures[-1]:
            self.posture = "pivoting"
            return
        self.posture = "diving"

    # ── Rendering ──────────────────────────────────────────────────────────

    def should_inject_header(self, tool_name: str) -> bool:
        """Light tools and the very first tool call don't need the header."""
        if tool_name in _LIGHT_TOOLS:
            return False
        # Skip header on call #1 — the user's own message is the anchor
        if len(self.tool_history) <= 1:
            return False
        return True

    def render_header(self) -> str:
        """Compact goal/progress anchor. Appended after tool results."""
        n_calls = len(self.tool_history)
        evidence_lines: list[str] = []
        # Show last 3 evidence entries, most recent first
        for ev in self.evidence_log[-3:]:
            evidence_lines.append(f"  • #{ev.seq} {ev.tool}: {ev.summary}")
        evidence_block = "\n".join(evidence_lines) if evidence_lines else "  (none yet)"

        failures_line = ""
        if self.failures:
            recent_failures = ", ".join(self.failures[-3:])
            failures_line = f"\nFailures: {recent_failures}"

        posture_hint = _POSTURE_HINTS.get(self.posture, "")

        return (
            f"\n\n[TURN ANCHOR — do not echo to user]\n"
            f"Goal: {self.goal}\n"
            f"Calls so far: {n_calls} | Evidence: {len(self.evidence_log)}/{self.evidence_budget}\n"
            f"Evidence collected:\n{evidence_block}"
            f"{failures_line}\n"
            f"Posture: {self.posture} — {posture_hint}\n"
            f"[/TURN ANCHOR]"
        )

    def render_completion_check(self) -> Optional[str]:
        """Inject ONCE at turn close if work appears incomplete.

        Returns None if completion check should not fire (simple turn,
        already fired, or work clearly complete).
        """
        if self.completion_check_emitted:
            return None
        # Only fire on complex turns
        if self.evidence_budget < 2:
            return None
        # Already plenty of evidence — turn is succeeding
        if len(self.evidence_log) >= self.evidence_budget:
            return None
        # Trigger conditions:
        #   (a) totally blocked (no evidence after many calls)
        #   (b) any failures with zero useful evidence
        #   (c) pivot-loop: 6+ calls AND 3+ failures AND evidence below budget
        #       (catches the case where 1 useful result keeps posture in "pivoting"
        #       while many subsequent calls fail — the morning's actual loop pattern)
        n_calls = len(self.tool_history)
        n_failures = len(self.failures)
        n_evidence = len(self.evidence_log)
        pivot_loop = (
            n_calls >= 6
            and n_failures >= 3
            and n_evidence < self.evidence_budget
        )
        if self.posture == "blocked" or (self.failures and n_evidence == 0) or pivot_loop:
            self.completion_check_emitted = True
            return (
                "\n\n[COMPLETION CHECK — internal, do not echo]\n"
                f"Original goal: {self.goal}\n"
                f"Tools called: {len(self.tool_history)} | Useful evidence: {len(self.evidence_log)}\n"
                f"Failures: {', '.join(self.failures) if self.failures else 'none'}\n"
                "This turn appears INCOMPLETE. Before closing, do ONE of:\n"
                "  1. Try a different approach (pivot tool or strategy) if you haven't yet.\n"
                "  2. Tell the user concretely what you tried, what failed, and what they can do.\n"
                "Do NOT close on a vague 'I couldn't find anything' — be specific about what was attempted.\n"
                "[/COMPLETION CHECK]"
            )
        return None


_POSTURE_HINTS = {
    "diving": "keep gathering; next tool should advance the goal",
    "pivoting": "last attempt failed — try a different tool/angle, not the same one",
    "surfacing": "you have enough; respond to the user now",
    "blocked": "tools aren't yielding — escalate to the user with what you tried",
}


# ── Result classification ────────────────────────────────────────────────

_FAILURE_MARKERS = (
    "tool execution error",
    "tool error",
    "timed out",
    "returned http 4",
    "returned http 5",
    "no results",
    "no past conversations found",
    "no emails found",
    "thread lookup failed",
    "thread not found",
    "not found",
    "page is empty",
    "search failed",
    "could not retrieve",
    "couldn't retrieve",
    "no usable result",
    "system: duplicate",
    "system: runaway",
    "system hard stop",
)


def classify_result_useful(result_str: str) -> bool:
    """Heuristic: did this tool call produce useful evidence?"""
    if not result_str or not result_str.strip():
        return False
    if len(result_str.strip()) < 25:
        return False
    lower = result_str.lower()
    return not any(marker in lower for marker in _FAILURE_MARKERS)


def _summarize_result(result_preview: str) -> str:
    """One-line summary of a tool result for the evidence log."""
    s = (result_preview or "").strip().replace("\n", " ")
    if len(s) > 110:
        s = s[:107] + "..."
    return s or "(empty)"


# ── Goal derivation ──────────────────────────────────────────────────────

def derive_goal(user_text: str, plan_goal: str, intent: str) -> str:
    """Pick the most informative goal string available.

    Plans from `decide_turn` carry a `.goal` field that's already crafted
    for the intent. If empty, fall back to a clean version of the user text.
    """
    pg = (plan_goal or "").strip()
    if pg and len(pg) >= 10:
        return pg[:200]
    ut = (user_text or "").strip()
    if ut:
        return ut[:200]
    return f"({intent})"


def derive_evidence_budget(plan_evidence_budget: int, intent: str) -> int:
    """Use plan budget if set; otherwise default by intent family."""
    if plan_evidence_budget and plan_evidence_budget > 0:
        return min(plan_evidence_budget, 8)
    # Sensible defaults for pass-through-style turns
    if intent in ("pass_through", "auto_action"):
        return 3
    return 2


# ── Unified scaffold injection ────────────────────────────────────────────

def augment_tool_result(
    tc: Optional["TurnContext"],
    tool_name: str,
    args_preview: str,
    result_str: str,
) -> str:
    """Update the TurnContext with this tool call and return the result
    string augmented with scaffold markers (TURN ANCHOR / SIGNAL / COMPLETION CHECK).

    Single source of truth used by both bot.py (voice/iOS) and text_chat.py
    (dashboard /chat and OpenAI-compatible /v1/chat/completions). If `tc` is
    None, returns the result unchanged.

    Args:
        tc: the active TurnContext (or None on first call before init)
        tool_name: name of the tool that just returned
        args_preview: short preview of the args (≤80 chars, for evidence log)
        result_str: the trimmed result string ready for the LLM

    Returns:
        The (possibly augmented) result string to pass to the LLM.
    """
    if tc is None:
        return result_str
    # Lazy import to avoid cycles
    from nova.result_signals import detect_signal

    useful = classify_result_useful(result_str)
    tc.record_tool_call(
        tool=tool_name,
        args_preview=args_preview[:80],
        result_preview=result_str[:300],
        useful=useful,
    )

    parts = [result_str]
    signal = detect_signal(
        tool_name=tool_name,
        result_str=result_str,
        tool_history=tc.tool_history,
        failures=tc.failures,
    )
    if signal:
        parts.append(signal)
    if tc.should_inject_header(tool_name):
        parts.append(tc.render_header())
    completion = tc.render_completion_check()
    if completion:
        parts.append(completion)

    return "".join(parts) if len(parts) > 1 else result_str


async def finalize_and_persist(
    tc: Optional["TurnContext"],
    user_id: str,
    conversation_id: str,
) -> None:
    """Persist a completed TurnContext's summary to nova_turn_log.

    Single source of truth for turn-close persistence. Used by both
    bot.py (voice/iOS reset paths) and text_chat.py (after the final
    response in /chat and /v1/chat/completions). Safe to call with None
    or with a TurnContext that has no tool history — both are no-ops.
    """
    if tc is None or not tc.tool_history:
        return
    # Lazy import to avoid cycles at module load time
    from nova.turn_log import record_turn_summary, derive_outcome_hint

    useful_tools = [ev.tool for ev in tc.evidence_log]
    evidence_summaries = [ev.summary for ev in tc.evidence_log]
    hint = derive_outcome_hint(
        posture=tc.posture,
        evidence_summaries=evidence_summaries,
        failed_tools=tc.failures,
    )
    await record_turn_summary(
        turn_id=tc.turn_id,
        user_id=user_id,
        conversation_id=conversation_id,
        goal=tc.goal,
        intent=tc.intent,
        posture_at_close=tc.posture,
        tool_history=tc.tool_history,
        useful_tools=useful_tools,
        failed_tools=tc.failures,
        outcome_hint=hint,
    )
