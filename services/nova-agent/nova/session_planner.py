"""
Nova Session Planner — the spine for long-horizon conversations.

PROBLEM
-------
M2.7 (and any reasoning LLM) loses goal coherence when:
  - tool calls fail mid-objective (forgets what the objective was)
  - stream truncates after a promise (no recovery anchor)
  - context compaction strips the original intent
  - the user pivots and comes back later in the same session

The system prompt already has hooks for `## Active Task Plans` (loaded from
`nova_task_plans` via `load_active_plans_for_context`), but Nova rarely
calls `manage_task_plan(action='create')` herself. When she fails to, there
is no spine. This module provides server-side, deterministic plan creation
so the spine exists even when the LLM stumbles.

DESIGN
------
1. `detect_plan_trigger(text)` — pattern-matches multi-step phrases
   (agenda for, prep for, work on, build, draft, plan, project, etc.)
2. `ensure_active_plan_for_turn(...)` — called once per turn after
   `decide_turn`. If a trigger fires AND no active plan covers this topic,
   creates a plan in `nova_task_plans` and stores its id on `TurnState`.
3. `auto_link_workspace_page(...)` — called when manage_workspace
   successfully creates a page; if there's an active plan without a page,
   links them.
4. `build_plan_state_message(plan)` — serializes the plan for the iOS
   `plan_state` server message so iOS can render a planner panel.
5. `emit_plan_state(...)` — sends the message via the server-msg callback.

This module never blocks the user-facing path: every IO is best-effort.
"""

from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from nova.task_plan import (
    add_session_entry,
    add_step,
    create_plan,
    get_plan,
    list_plans,
    set_workspace_page,
    update_step,
)


ServerMessageFn = Callable[[dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

# "Balanced" trigger set — explicit phrases the user uses when starting
# multi-session work. We deliberately keep these tight to avoid spurious
# plan creation; the workspace-creation hook (`auto_link_workspace_page`)
# is the second branch that catches LLM-initiated work.
_PLAN_TRIGGER_PATTERNS = (
    # Direct planning phrases
    r"\blet[' ]?s (plan|prep|prepare|work on|build|draft|write|outline|design|map out)\b",
    r"\bi (?:want|need) to (plan|prep|prepare|work on|build|draft|write|outline|design|map out)\b",
    r"\b(can|could) (we|you) (plan|prep|prepare|work on|build|draft|write|outline|design)\b",
    # Continuation phrases
    r"\b(continue|keep|finish|resume) (?:working on|building|drafting|writing|the|with) the\b",
    r"\bback to (?:the|our) (?:agenda|plan|project|draft|document|page)\b",
    # Domain-specific objects
    r"\bagenda for\b",
    r"\bprep(?:aration)? for\b(?! a (?:moment|second|sec))",  # exclude "prep for a sec"
    r"\b(?:meeting|interview) prep\b",
    r"\b(?:briefing|talking points|action items) (?:for|on)\b",
    r"\bcase study (?:on|about|for)\b",
    r"\barticle (?:on|about) \w+\b",
    # "Tomorrow's X meeting" / "the X meeting"
    r"\b(?:tomorrow's|next week's|today's|the) [\w\s]{0,40}(?:meeting|call|interview|review|discussion)\b",
)
_PLAN_TRIGGER_RE = re.compile("|".join(_PLAN_TRIGGER_PATTERNS), re.IGNORECASE)

# Phrases that explicitly opt OUT of plan creation
_PLAN_OPT_OUT_PATTERNS = (
    r"\bno plan needed\b",
    r"\bdon[' ]?t (?:track|plan|create a plan)\b",
    r"\bjust (?:answer|tell me|a quick)\b",
)
_PLAN_OPT_OUT_RE = re.compile("|".join(_PLAN_OPT_OUT_PATTERNS), re.IGNORECASE)

# Reflective / past-tense phrases that describe completed events.
# These must NOT trigger plan creation — they reference work already done,
# not new work being started. Matching any one of these suppresses the
# plan trigger even if a meeting/action keyword was also detected.
_PLAN_PAST_TENSE_PATTERNS = (
    r"\bwe already (?:had|did|covered|finished|completed|went over|met|discussed)\b",
    r"\balready had (?:the|our|a)\b",
    r"\b(?:let me|just) remind(?:ing)? you\b",
    r"\b(?:it|everything|that) (?:went|goes|went over) well\b",
    r"\b(?:already|just) (?:done|completed|finished|happened|took place|occurred)\b",
    r"\bwe (?:already|just) (?:met|talked|spoke|discussed|covered|wrapped up)\b",
    r"\b(?:to tell|reminding|letting) you (?:that|about|how)\b",
    r"\bfyi[,:]?\s",
    r"\bjust (?:wanted|letting you|confirming|to confirm|checking)\b",
    r"\b(?:this morning|yesterday|last night|earlier today),? (?:we|i|the meeting)\b",
)
_PLAN_PAST_TENSE_RE = re.compile("|".join(_PLAN_PAST_TENSE_PATTERNS), re.IGNORECASE)


def detect_plan_trigger(text: str) -> bool:
    """Return True if the text suggests starting/continuing multi-step work."""
    if not text or not text.strip():
        return False
    # Opt-out wins
    if _PLAN_OPT_OUT_RE.search(text):
        return False
    # Reflective / past-tense — describes completed work, not new work
    if _PLAN_PAST_TENSE_RE.search(text):
        return False
    return bool(_PLAN_TRIGGER_RE.search(text))


# ---------------------------------------------------------------------------
# Topic extraction
# ---------------------------------------------------------------------------

def derive_plan_topic(text: str, fallback_goal: str = "") -> str:
    """Pull a short topic line out of the user's text.

    Heuristics, not perfect — better than nothing. The LLM can rename it
    later via manage_task_plan(action='update_topic') if we add that.
    """
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return (fallback_goal or "Untitled session").strip()[:120]

    # Try to capture object after "agenda for", "prep for", "work on", etc.
    object_patterns = (
        r"agenda for ([^.!?\n]{3,80})",
        r"prep(?:aration)? for ([^.!?\n]{3,80})",
        r"meeting (?:with|about) ([^.!?\n]{3,80})",
        r"work on ([^.!?\n]{3,80})",
        r"build (?:the |a )?([^.!?\n]{3,80})",
        r"draft (?:the |a )?([^.!?\n]{3,80})",
        r"continue (?:the |our )?([^.!?\n]{3,80})",
        r"plan (?:the |a )?([^.!?\n]{3,80})",
    )
    for pattern in object_patterns:
        m = re.search(pattern, cleaned, re.IGNORECASE)
        if m:
            obj = m.group(1).strip().rstrip(".,;:!?")
            # Drop trailing filler
            obj = re.sub(r"\b(please|now|today|tomorrow|right now)$", "", obj, flags=re.IGNORECASE).strip()
            if obj:
                return obj[:120]

    if fallback_goal:
        return fallback_goal.strip()[:120]
    # Last resort: first 80 chars of the user text
    return cleaned[:80]


# ---------------------------------------------------------------------------
# Topic similarity (avoid creating duplicate plans)
# ---------------------------------------------------------------------------

_TOPIC_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on",
    "with", "about", "your", "my", "our", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "being", "been",
    "let", "lets", "let's", "i", "we", "you",
    "want", "need", "would", "could", "should", "do",
    "tomorrow", "today", "yesterday",
}


def _topic_tokens(topic: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (topic or "").lower())
        if len(w) >= 3 and w not in _TOPIC_STOPWORDS
    }


def _topics_match(a: str, b: str, threshold: float = 0.35) -> bool:
    """Cheap Jaccard-on-content-words. Adequate for de-duping in this volume.

    Lowered threshold from 0.5 → 0.35: ASR transcription variability and
    user phrasing changes ("tomorrow's agenda" vs "meeting prep tomorrow")
    were preventing legitimate dedup. 0.35 still avoids cross-topic matches.
    """
    ta, tb = _topic_tokens(a), _topic_tokens(b)
    if not ta or not tb:
        return False
    intersection = ta & tb
    union = ta | tb
    if not union:
        return False
    return (len(intersection) / len(union)) >= threshold


# ---------------------------------------------------------------------------
# Project-key derivation — stable identity across ASR/phrasing variation
# ---------------------------------------------------------------------------

# Canonical project entities: (slug, [trigger_terms]).
# When the user mentions ANY of the trigger terms, the project_key is forced
# to the slug. This collapses ASR typos like "Metalist" → "methodist" and
# phrasing variants ("the CEO meeting" vs "agenda for tomorrow") to one key.
_PROJECT_ENTITY_RULES: list[tuple[str, list[str]]] = [
    ("ceo-meeting-houston-methodist-baytown",
     ["adrienne joseph", "houston methodist baytown", "methodist baytown",
      "metalist baytown", "hmb ceo", "baytown ceo", "hospital ceo"]),
    ("managerial-overreach-case-study",
     ["managerial overreach", "overreach case study"]),
    ("world-cup-physician-tickets",
     ["world cup ticket", "fifa world cup", "physician ticket"]),
    ("coumadin-warfarin-clinic",
     ["coumadin clinic", "warfarin management", "khumedin", "khumedin clinic"]),
]


def derive_project_key(text: str, topic: str = "") -> str:
    """Return a stable project slug for the given text/topic, or empty.

    Resolution order:
      1. Match against `_PROJECT_ENTITY_RULES` (canonical projects)
      2. Heuristic slug from topic (lowercase, remove stopwords, dash-join)
         — only if topic has ≥3 content tokens to avoid noise like "meeting"
    """
    haystack = f"{topic} {text}".lower()
    for slug, triggers in _PROJECT_ENTITY_RULES:
        if any(trig in haystack for trig in triggers):
            return slug
    # Heuristic fallback
    tokens = sorted(_topic_tokens(topic or text))
    # Drop very generic tokens that don't help identify projects
    generic = {"meeting", "agenda", "session", "planner", "prep", "plan",
               "morning", "evening", "work", "work’s", "items", "item",
               "talk", "discussion", "notes", "note"}
    tokens = [t for t in tokens if t not in generic]
    if len(tokens) < 3:
        return ""  # Not specific enough — don't fabricate a key
    return "-".join(tokens[:6])


# ---------------------------------------------------------------------------
# Core entry points
# ---------------------------------------------------------------------------

async def find_active_plan_for_topic(
    user_id: str,
    topic: str,
    project_key: str = "",
) -> Optional[dict]:
    """Return an existing active plan that matches by project_key OR topic overlap.

    Match priority:
      1. project_key exact match (highest signal)
      2. Topic Jaccard similarity ≥ 0.35
    """
    # 1) project_key match — strongest signal, immune to ASR variation
    if project_key:
        try:
            from nova.task_plan import find_plans_by_project_key
            matches = await find_plans_by_project_key(user_id, project_key)
            if matches:
                return matches[0]
        except Exception as e:
            logger.warning(f"NOVA_PLANNER | find_by_project_key failed: {e}")
    # 2) Topic similarity fallback
    try:
        plans = await list_plans(user_id=user_id, status="active")
    except Exception as e:
        logger.warning(f"NOVA_PLANNER | list_plans failed: {e}")
        return None
    for plan in plans:
        if _topics_match(plan.get("topic", ""), topic):
            return plan
    return None


async def ensure_active_plan_for_turn(
    *,
    text: str,
    plan_intent: str,
    plan_goal: str,
    user_id: str,
    conversation_id: str,
    session_id: str,
    force: bool = False,
) -> Optional[dict]:
    """Auto-create a plan if the turn looks like multi-step work.

    Called once per turn from the voice/text path after `decide_turn`.

    Returns the active plan dict (newly created OR existing match), or
    None if no plan was created/matched.

    Idempotent: matching on topic prevents duplicate plans across
    consecutive turns about the same objective.
    """
    if not force and not detect_plan_trigger(text):
        return None
    topic = derive_plan_topic(text, fallback_goal=plan_goal)
    if not topic:
        return None
    # Compute stable project_key (immune to ASR variation)
    project_key = derive_project_key(text, topic)
    # Match: project_key first, then topic similarity
    existing = await find_active_plan_for_topic(user_id, topic, project_key=project_key)
    if existing:
        # Backfill project_key on legacy plans if we have one and they don't
        if project_key and not (existing.get("project_key") or ""):
            try:
                from nova.task_plan import set_project_key
                await set_project_key(existing["plan_id"], project_key)
                existing["project_key"] = project_key
                logger.info(
                    f"NOVA_PLANNER | backfilled_project_key | "
                    f"plan_id={existing['plan_id']} project_key={project_key}"
                )
            except Exception as e:
                logger.warning(f"NOVA_PLANNER | set_project_key failed: {e}")
        logger.info(
            f"NOVA_PLANNER | matched_existing | plan_id={existing['plan_id']} "
            f"topic={topic[:60]!r} project_key={project_key!r} user_id={user_id}"
        )
        return existing
    # Create
    description = f"Auto-created from intent={plan_intent}. User text: {text[:200]}"
    try:
        plan = await create_plan(
            topic=topic,
            description=description,
            user_id=user_id,
            project_key=project_key,
        )
    except Exception as e:
        logger.warning(f"NOVA_PLANNER | create_plan failed: {e}")
        return None
    logger.info(
        f"NOVA_PLANNER | created | plan_id={plan['plan_id']} "
        f"topic={topic[:60]!r} project_key={project_key!r} intent={plan_intent} "
        f"user_id={user_id} conversation_id={conversation_id}"
    )
    return plan


async def auto_link_workspace_page(
    *,
    user_id: str,
    plan_id: Optional[str],
    page_id: str,
    page_title: str = "",
    text: str = "",
    plan_goal: str = "",
    conversation_id: str = "",
) -> Optional[dict]:
    """Link a freshly-created workspace page to the active plan.

    Two branches:
      1. If `plan_id` is provided and the plan has no `workspace_page_id`,
         link directly.
      2. If `plan_id` is None but the page title / user text suggests
         multi-step work, auto-create a plan AND link.

    Returns the linked plan dict, or None.
    """
    if not page_id:
        return None

    plan: Optional[dict] = None
    if plan_id:
        plan = await get_plan(plan_id)

    if plan is None:
        # Branch 2: page was created without an active plan. If the title
        # or user text looks like a multi-step objective, auto-create a
        # plan around it.
        topic_seed = page_title or text or plan_goal
        if not detect_plan_trigger(topic_seed) and not page_title:
            # No strong signal — skip auto-creation
            return None
        topic = derive_plan_topic(topic_seed, fallback_goal=plan_goal) or page_title or "Workspace session"
        existing = await find_active_plan_for_topic(user_id, topic)
        if existing:
            plan = existing
        else:
            try:
                plan = await create_plan(
                    topic=topic,
                    description=f"Auto-created from workspace page '{page_title or page_id[:8]}'. User text: {text[:200]}",
                    user_id=user_id,
                )
                logger.info(
                    f"NOVA_PLANNER | created_from_page | plan_id={plan['plan_id']} "
                    f"page_id={page_id} topic={topic[:60]!r}"
                )
            except Exception as e:
                logger.warning(f"NOVA_PLANNER | create_plan from page failed: {e}")
                return None

    if not plan:
        return None
    # Already linked? skip.
    current_page = (plan.get("workspace_page_id") or "").strip()
    if current_page == page_id:
        return plan
    if current_page:
        # A page is already linked; don't clobber. Log so we can review.
        logger.info(
            f"NOVA_PLANNER | link_skipped_existing | plan_id={plan['plan_id']} "
            f"existing_page={current_page} new_page={page_id}"
        )
        return plan
    try:
        await set_workspace_page(plan["plan_id"], page_id)
        plan["workspace_page_id"] = page_id
        logger.info(
            f"NOVA_PLANNER | linked_page | plan_id={plan['plan_id']} page_id={page_id}"
        )
    except Exception as e:
        logger.warning(f"NOVA_PLANNER | set_workspace_page failed: {e}")
    return plan


# ---------------------------------------------------------------------------
# iOS state emission
# ---------------------------------------------------------------------------

def build_plan_state_message(plan: dict) -> dict[str, Any]:
    """Serialize a plan dict into the `plan_state` server message envelope.

    iOS subscribers render this in a planner panel.
    """
    if not isinstance(plan, dict):
        return {}
    pending_steps = []
    completed_steps = []
    for step in plan.get("steps", []) or []:
        item = {
            "step_id": step.get("step_id"),
            "title": step.get("title"),
            "status": step.get("status"),
            "order": step.get("order_num", 0),
            "notes": step.get("notes") or "",
        }
        if (step.get("status") or "").lower() in ("done", "skipped"):
            completed_steps.append(item)
        else:
            pending_steps.append(item)
    sessions_summary = []
    for entry in (plan.get("sessions") or [])[:5]:
        sessions_summary.append({
            "entry_id": entry.get("entry_id"),
            "timestamp": entry.get("timestamp"),
            "summary": (entry.get("summary") or "")[:280],
            "next_steps": entry.get("next_steps") or [],
        })
    return {
        "type": "plan_state",
        "plan_id": plan.get("plan_id"),
        "topic": plan.get("topic") or "",
        "description": plan.get("description") or "",
        "status": plan.get("status") or "active",
        "workspace_page_id": plan.get("workspace_page_id") or "",
        "created_at": plan.get("created_at"),
        "updated_at": plan.get("updated_at"),
        "pending_steps": pending_steps,
        "completed_steps": completed_steps,
        "recent_sessions": sessions_summary,
        "emitted_at": time.time(),
    }


async def emit_plan_state(send_server_msg: ServerMessageFn, plan: Optional[dict]) -> None:
    """Send a `plan_state` message to the iOS client. Best-effort."""
    if not plan:
        # Emit an explicit "no active plan" so iOS can clear its panel
        try:
            await send_server_msg({
                "type": "plan_state",
                "plan_id": None,
                "topic": "",
                "status": "none",
                "pending_steps": [],
                "completed_steps": [],
                "recent_sessions": [],
                "emitted_at": time.time(),
            })
        except Exception as e:
            logger.warning(f"NOVA_PLANNER | emit_plan_state(none) failed: {e}")
        return
    # Re-load the plan with sessions/steps so the message is complete
    plan_id = plan.get("plan_id")
    full = None
    if plan_id:
        try:
            full = await get_plan(plan_id)
        except Exception as e:
            logger.warning(f"NOVA_PLANNER | get_plan failed: {e}")
    payload = build_plan_state_message(full or plan)
    if not payload:
        return
    try:
        await send_server_msg(payload)
        logger.info(
            f"NOVA_PLANNER | emitted_plan_state | plan_id={plan_id} "
            f"pending={len(payload.get('pending_steps') or [])} "
            f"completed={len(payload.get('completed_steps') or [])}"
        )
    except Exception as e:
        logger.warning(f"NOVA_PLANNER | emit_plan_state failed: {e}")


async def fetch_project_pages(
    user_id: str,
    plan_id: str | None = None,
    project_key: str | None = None,
) -> list[dict]:
    """Return a deduplicated list of real workspace pages for this project.

    Each entry is ``{"page_id": str, "title": str, "project_key": str}``.
    Sources:
    1. The canonical active plan's ``workspace_page_id``.
    2. All sibling plans that share the same ``project_key`` and have a page.

    This is the seed data that fills ``## Known Workspace Pages`` in context
    so M2.7 can pick a real page_id instead of confabulating one.
    """
    pages: dict[str, dict] = {}

    try:
        from nova.task_plan import list_plans as _list_plans, get_plan as _get_plan

        # Pull the canonical plan first
        if plan_id:
            canon = await _get_plan(plan_id)
            if canon:
                pid = (canon.get("workspace_page_id") or "").strip()
                if pid:
                    pages[pid] = {
                        "page_id": pid,
                        "title": canon.get("topic", ""),
                        "project_key": canon.get("project_key", "") or "",
                    }
                if not project_key:
                    project_key = canon.get("project_key", "") or ""

        # Pull siblings by project_key
        if project_key:
            siblings = await _list_plans(
                user_id=user_id,
                status=None,
                limit=30,
            )
            for sib in (siblings or []):
                if sib.get("project_key") != project_key:
                    continue
                pid = (sib.get("workspace_page_id") or "").strip()
                if pid and pid not in pages:
                    pages[pid] = {
                        "page_id": pid,
                        "title": sib.get("topic", ""),
                        "project_key": project_key,
                    }
    except Exception as e:
        logger.warning(f"NOVA_PLANNER | fetch_project_pages failed: {e}")

    return list(pages.values())


__all__ = [
    "detect_plan_trigger",
    "derive_plan_topic",
    "find_active_plan_for_topic",
    "ensure_active_plan_for_turn",
    "auto_link_workspace_page",
    "build_plan_state_message",
    "emit_plan_state",
    "fetch_project_pages",
]
