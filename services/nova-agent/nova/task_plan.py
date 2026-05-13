"""
Nova Task Planner — cross-session work continuity

Stores structured work plans with full session history so Nova can resume
long-horizon tasks across separate conversations.

Each plan tracks:
  - topic / goal description
  - ordered steps / checklist
  - per-session entries: conversation_id, timestamp, summary, content, sources, next_steps
  - optional Pi Workspace page anchor for human-readable view
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def _parse_json(raw: str | None, default: Any = None) -> Any:
    try:
        return json.loads(raw or "{}") if default is None else json.loads(raw or "[]")
    except Exception:
        return default if default is not None else {}


async def _db():
    import aiosqlite
    from nova.store import DB_PATH
    return aiosqlite.connect(DB_PATH)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

async def create_plan(
    topic: str,
    description: str = "",
    user_id: str = "",
    workspace_page_id: str = "",
) -> dict:
    """Create a new task plan and return its dict."""
    plan_id = _new_id()
    now = _now()
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO nova_task_plans
               (plan_id, topic, description, status, workspace_page_id, user_id, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
            (plan_id, topic, description, workspace_page_id, user_id, now, now),
        )
        await db.commit()
    return {
        "plan_id": plan_id,
        "topic": topic,
        "description": description,
        "status": "active",
        "workspace_page_id": workspace_page_id,
        "created_at": now,
        "updated_at": now,
        "sessions": [],
        "steps": [],
    }


async def get_plan(plan_id: str) -> dict | None:
    """Load a plan with its sessions and steps."""
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM nova_task_plans WHERE plan_id = ?", (plan_id,)
        )
        if not rows:
            return None
        plan = dict(rows[0])
        sessions = await db.execute_fetchall(
            "SELECT * FROM nova_task_plan_sessions WHERE plan_id = ? ORDER BY timestamp DESC LIMIT 15",
            (plan_id,),
        )
        steps = await db.execute_fetchall(
            "SELECT * FROM nova_task_plan_steps WHERE plan_id = ? ORDER BY order_num ASC, created_at ASC",
            (plan_id,),
        )
    plan["sessions"] = []
    for s in sessions:
        entry = dict(s)
        entry["sources"] = _parse_json(entry.pop("sources_json", None), [])
        entry["next_steps"] = _parse_json(entry.pop("next_steps_json", None), [])
        plan["sessions"].append(entry)
    plan["steps"] = [dict(s) for s in steps]
    return plan


async def list_plans(user_id: str = "", status: str = "active") -> list[dict]:
    """List plans, optionally filtered by user and status."""
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            rows = await db.execute_fetchall(
                """SELECT * FROM nova_task_plans
                   WHERE (user_id = ? OR user_id = '') AND status = ?
                   ORDER BY updated_at DESC LIMIT 20""",
                (user_id, status),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM nova_task_plans WHERE status = ? ORDER BY updated_at DESC LIMIT 20",
                (status,),
            )
    return [dict(r) for r in rows]


async def set_workspace_page(plan_id: str, page_id: str) -> bool:
    """Link a Pi Workspace page to a plan."""
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nova_task_plans SET workspace_page_id = ?, updated_at = ? WHERE plan_id = ?",
            (page_id, _now(), plan_id),
        )
        await db.commit()
    return True


async def complete_plan(plan_id: str) -> bool:
    """Mark a plan as completed."""
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nova_task_plans SET status = 'completed', updated_at = ? WHERE plan_id = ?",
            (_now(), plan_id),
        )
        await db.commit()
    return True


# ---------------------------------------------------------------------------
# Session Entries
# ---------------------------------------------------------------------------

async def add_session_entry(
    plan_id: str,
    conversation_id: str = "",
    session_id: str = "",
    summary: str = "",
    content: str = "",
    sources: list | None = None,
    next_steps: list | None = None,
) -> dict:
    """Append a session log entry to a plan."""
    entry_id = _new_id()
    now = _now()
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO nova_task_plan_sessions
               (entry_id, plan_id, conversation_id, session_id, timestamp,
                summary, content, sources_json, next_steps_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')""",
            (
                entry_id, plan_id, conversation_id, session_id, now,
                summary, content,
                json.dumps(sources or []),
                json.dumps(next_steps or []),
            ),
        )
        await db.execute(
            "UPDATE nova_task_plans SET updated_at = ? WHERE plan_id = ?", (now, plan_id)
        )
        await db.commit()
    return {
        "entry_id": entry_id,
        "plan_id": plan_id,
        "conversation_id": conversation_id,
        "timestamp": now,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def add_step(
    plan_id: str,
    title: str,
    order_num: int = 0,
    notes: str = "",
) -> dict:
    """Add a step/checklist item to a plan."""
    step_id = _new_id()
    now = _now()
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO nova_task_plan_steps
               (step_id, plan_id, title, status, order_num, notes, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (step_id, plan_id, title, order_num, notes, now, now),
        )
        await db.execute(
            "UPDATE nova_task_plans SET updated_at = ? WHERE plan_id = ?", (now, plan_id)
        )
        await db.commit()
    return {"step_id": step_id, "plan_id": plan_id, "title": title, "status": "pending"}


async def update_step(step_id: str, status: str, notes: str = "") -> bool:
    """Update a step's status (pending | in_progress | done | skipped)."""
    import aiosqlite
    from nova.store import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        if notes:
            await db.execute(
                "UPDATE nova_task_plan_steps SET status = ?, notes = ?, updated_at = ? WHERE step_id = ?",
                (status, notes, _now(), step_id),
            )
        else:
            await db.execute(
                "UPDATE nova_task_plan_steps SET status = ?, updated_at = ? WHERE step_id = ?",
                (status, _now(), step_id),
            )
        await db.commit()
    return True


# ---------------------------------------------------------------------------
# Prompt context helper
# ---------------------------------------------------------------------------

async def load_active_plans_for_context(user_id: str, limit: int = 3) -> list[dict]:
    """Return a compact summary of active plans for the system prompt."""
    import aiosqlite
    from nova.store import DB_PATH
    cutoff = _now() - 30 * 86400  # 30-day window
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """SELECT p.plan_id, p.topic, p.description, p.status, p.workspace_page_id,
                      p.created_at, p.updated_at,
                      COUNT(s.entry_id) as session_count,
                      MAX(s.timestamp) as last_session_ts,
                      (SELECT s2.summary FROM nova_task_plan_sessions s2
                       WHERE s2.plan_id = p.plan_id ORDER BY s2.timestamp DESC LIMIT 1) as last_summary,
                      (SELECT s2.next_steps_json FROM nova_task_plan_sessions s2
                       WHERE s2.plan_id = p.plan_id ORDER BY s2.timestamp DESC LIMIT 1) as pending_next_steps_json
               FROM nova_task_plans p
               LEFT JOIN nova_task_plan_sessions s ON s.plan_id = p.plan_id
               WHERE (p.user_id = ? OR p.user_id = '')
                 AND p.status = 'active'
                 AND p.updated_at > ?
               GROUP BY p.plan_id
               ORDER BY p.updated_at DESC
               LIMIT ?""",
            (user_id, cutoff, limit),
        )
        steps_by_plan: dict[str, list[dict]] = {}
        if rows:
            plan_ids = [r["plan_id"] for r in rows]
            placeholders = ",".join("?" * len(plan_ids))
            step_rows = await db.execute_fetchall(
                f"""SELECT * FROM nova_task_plan_steps
                    WHERE plan_id IN ({placeholders}) AND status NOT IN ('done','skipped')
                    ORDER BY order_num ASC, created_at ASC""",
                plan_ids,
            )
            for sr in step_rows:
                steps_by_plan.setdefault(sr["plan_id"], []).append(dict(sr))

    result = []
    for r in rows:
        entry = dict(r)
        entry["pending_steps"] = steps_by_plan.get(r["plan_id"], [])
        try:
            entry["next_steps"] = json.loads(entry.pop("pending_next_steps_json") or "[]")
        except Exception:
            entry["next_steps"] = []
        result.append(entry)
    return result
