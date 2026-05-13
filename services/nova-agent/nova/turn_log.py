"""
Cross-turn memory log for Nova.

Persists a one-line summary of each completed turn so the next turn — and
future sessions — can see what was tried, what worked, and what failed.
This is the durability layer of vertical reasoning: it lets Nova resume
a problem with awareness of prior attempts instead of starting from scratch.

A "turn summary" captures only what's reusable across sessions:
  - goal: what the user wanted
  - intent: the routed intent class
  - posture_at_close: how the turn ended (surfacing | blocked | diving | pivoting)
  - useful_tools: tools that produced evidence
  - failed_tools: tools that returned empty/error
  - outcome_hint: one-line natural summary derived from evidence

Storage: same SQLite DB as the rest of Nova (NOVA_DB_PATH).
"""

from __future__ import annotations

import os
import time
import json
from typing import Optional

import aiosqlite
from loguru import logger


def _db_path() -> str:
    return os.environ.get("NOVA_DB_PATH", "/home/eleazar/Projects/AIHomelab/services/nova-agent/services/nova-agent/nova.db")


_TABLE_READY = False


async def _ensure_table() -> None:
    """Idempotent table init. Called lazily on first write/read."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nova_turn_log (
                turn_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT DEFAULT '',
                created_at REAL NOT NULL,
                goal TEXT NOT NULL,
                intent TEXT NOT NULL,
                posture_at_close TEXT NOT NULL,
                tool_calls INTEGER NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                useful_tools_json TEXT NOT NULL DEFAULT '[]',
                failed_tools_json TEXT NOT NULL DEFAULT '[]',
                outcome_hint TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_nova_turn_log_user_time
            ON nova_turn_log(user_id, created_at DESC)
        """)
        await db.commit()
    _TABLE_READY = True


async def record_turn_summary(
    *,
    turn_id: str,
    user_id: str,
    conversation_id: str,
    goal: str,
    intent: str,
    posture_at_close: str,
    tool_history: list[str],
    useful_tools: list[str],
    failed_tools: list[str],
    outcome_hint: str = "",
) -> None:
    """Persist a turn summary. Safe to call multiple times — uses PRIMARY KEY upsert."""
    try:
        await _ensure_table()
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO nova_turn_log (
                    turn_id, user_id, conversation_id, created_at,
                    goal, intent, posture_at_close,
                    tool_calls, evidence_count,
                    useful_tools_json, failed_tools_json, outcome_hint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    posture_at_close = excluded.posture_at_close,
                    tool_calls       = excluded.tool_calls,
                    evidence_count   = excluded.evidence_count,
                    useful_tools_json= excluded.useful_tools_json,
                    failed_tools_json= excluded.failed_tools_json,
                    outcome_hint     = excluded.outcome_hint
                """,
                (
                    turn_id,
                    user_id,
                    conversation_id or "",
                    time.time(),
                    goal[:300],
                    intent[:80],
                    posture_at_close[:32],
                    len(tool_history),
                    len(useful_tools),
                    json.dumps(useful_tools[:10]),
                    json.dumps(failed_tools[:10]),
                    outcome_hint[:300],
                ),
            )
            await db.commit()
        logger.info(
            f"NOVA_TURN_LOG_WRITE | turn_id={turn_id} user={user_id} intent={intent} "
            f"posture={posture_at_close} useful={len(useful_tools)} failed={len(failed_tools)}"
        )
    except Exception as e:
        logger.warning(f"record_turn_summary failed (non-fatal): {e}")


async def load_recent_turn_summaries(
    user_id: str,
    limit: int = 3,
    max_age_seconds: int = 86400,  # 24h default
) -> list[dict]:
    """Load the user's most recent turn summaries for context injection.

    Only returns turns that produced meaningful signal — skips trivial
    turns (0 tool calls) so the injected context stays concise.
    """
    try:
        await _ensure_table()
        cutoff = time.time() - max_age_seconds
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT turn_id, created_at, goal, intent, posture_at_close,
                       tool_calls, evidence_count,
                       useful_tools_json, failed_tools_json, outcome_hint
                FROM nova_turn_log
                WHERE user_id = ?
                  AND created_at >= ?
                  AND tool_calls > 0
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, cutoff, limit),
            )
            rows = await cursor.fetchall()
            results: list[dict] = []
            for row in rows:
                try:
                    useful = json.loads(row["useful_tools_json"] or "[]")
                except Exception:
                    useful = []
                try:
                    failed = json.loads(row["failed_tools_json"] or "[]")
                except Exception:
                    failed = []
                results.append({
                    "turn_id": row["turn_id"],
                    "created_at": row["created_at"],
                    "goal": row["goal"],
                    "intent": row["intent"],
                    "posture_at_close": row["posture_at_close"],
                    "tool_calls": row["tool_calls"],
                    "evidence_count": row["evidence_count"],
                    "useful_tools": useful,
                    "failed_tools": failed,
                    "outcome_hint": row["outcome_hint"] or "",
                })
            return results
    except Exception as e:
        logger.warning(f"load_recent_turn_summaries failed: {e}")
        return []


def derive_outcome_hint(
    *,
    posture: str,
    evidence_summaries: list[str],
    failed_tools: list[str],
) -> str:
    """Produce a one-line natural summary of how the turn ended.

    This is a simple heuristic — not an LLM call. It surfaces the most
    salient fact about the turn for the next session to read.
    """
    if posture == "surfacing" and evidence_summaries:
        first_evidence = evidence_summaries[0][:120]
        return f"completed; key result: {first_evidence}"
    if posture == "blocked":
        if failed_tools:
            return f"BLOCKED — tried {', '.join(failed_tools[:3])} without success"
        return "BLOCKED — could not produce evidence"
    if posture == "pivoting":
        return "incomplete — pivoting between approaches"
    if evidence_summaries:
        return f"partial; saw: {evidence_summaries[0][:120]}"
    return "no meaningful evidence collected"
