"""
Nova Context Layer (NCL) — unified read/write face for Nova's distributed memory.

Design
------
Nova's knowledge lives across 5 stores:

  Neo4j (CIG)            — persons, emails, events, topics, derivative layers
  PCG HTTP service       — preferences, identity, dream insights, daily snapshot
  SQLite (nova.db)       — turns, sessions, learning_events, action_ledger,
                           task_artifacts, turn_policy_embeddings
  Postgres workspace.ai_*— cross-device conversation history
  Pi Workspace HTTP API  — pages, blocks, databases

The NCL is a thin Python facade in front of all of them. It does NOT add a new
store. It exposes six verbs the MoE LLM can reason about without knowing where
any individual fact lives:

  self_state(user_id)              -> who am I right now (dreams + goals + favorites)
  about(entity_name, user_id)      -> everything we know about <entity>
  timeline(user_id, hours=24)      -> unified episodic stream
  similar(text, k=10)              -> embedding search across embedded content
  observe(text, source, salience)  -> write an observation (auto-routed)
  promote(observation_id, ...)     -> episodic -> semantic (lift to PCG preference)

This file is intentionally light. Each verb wraps existing modules; over time,
the heavy lifting (provenance, salience, decay) accretes here rather than in
callers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# self_state — what does Nova know about her own present?
# ---------------------------------------------------------------------------

async def self_state(user_id: str) -> dict[str, Any]:
    """Return a snapshot of Nova's self-context for the current moment.

    This is what the `query_self_state` tool returns when Nova introspects.
    It's also what `prompt.py` should call once at session start to compose
    the system prompt's self-context sections.

    Returns:
        {
          "dreamed_last_night": bool,
          "dream_insights": [{"text", "date", "category"}, ...],
          "active_goals": [{"goal", "intent", "workspace_page_id"}, ...],
          "favorites": [{"label", "category", "value"}, ...],
          "recent_session_topics": [str, ...],
          "as_of": "<iso8601>",
        }
    """
    # Fan out in parallel; each branch swallows its own errors.
    dreams_task = asyncio.create_task(_load_dream_insights(limit=8, max_age_days=3))
    goals_task = asyncio.create_task(_load_active_goals(user_id))
    favs_task = asyncio.create_task(_load_favorites(user_id))
    topics_task = asyncio.create_task(_load_recent_session_topics(user_id))

    dreams = await dreams_task
    goals = await goals_task
    favs = await favs_task
    topics = await topics_task

    dreamed_last_night = False
    if dreams:
        # Two detectors: prefer parseable date in key, fall back to "any insight
        # exists at all" since PCG dream_insight keys are often semantic
        # (e.g. 'work_title') rather than date-prefixed.
        from datetime import datetime, timezone
        for d in dreams:
            raw = d.get("date", "")
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if age_hours <= 36:
                    dreamed_last_night = True
                    break
            except Exception:
                continue
        # If no date in any insight, fall back to systemd last-run signal.
        if not dreamed_last_night:
            dreamed_last_night = _dream_service_ran_recently()

    return {
        "dreamed_last_night": dreamed_last_night,
        "dream_insights": dreams,
        "active_goals": goals,
        "favorites": favs,
        "recent_session_topics": topics,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ---------------------------------------------------------------------------
# about — everything we know about an entity, joined across stores
# ---------------------------------------------------------------------------

async def about(entity_name: str, user_id: str = "") -> dict[str, Any]:
    """Best-effort join across CIG (graph), PCG (preferences), workspace, and
    past conversations for a single named entity.

    Returns a dict like:
        {
          "entity": "<name>",
          "cig":  { ... } | None,
          "pcg":  [ ...preferences mentioning entity... ],
          "workspace": [ ...pages mentioning entity... ],
          "recent_mentions": [ ...turns mentioning entity in last 7d... ],
        }
    """
    result: dict[str, Any] = {
        "entity": entity_name,
        "cig": None,
        "pcg": [],
        "workspace": [],
        "recent_mentions": [],
    }
    if not entity_name or not entity_name.strip():
        return result

    # CIG lookup (best effort — PCG knowledge graph search)
    try:
        from nova.pcg import search_knowledge_graph
        hits = await search_knowledge_graph(entity_name, limit=3)
        if hits:
            result["cig"] = hits
    except Exception as e:
        logger.debug(f"NCL.about cig lookup failed: {e}")

    # PCG preferences that reference the entity
    try:
        from nova.pcg import get_preferences
        prefs = await get_preferences()
        needle = entity_name.lower()
        result["pcg"] = [
            p for p in prefs
            if needle in str(p.get("value", "")).lower()
            or needle in str(p.get("key", "")).lower()
        ][:8]
    except Exception as e:
        logger.debug(f"NCL.about prefs lookup failed: {e}")

    # Recent turns mentioning entity (SQLite scan; cheap for ≤500 rows)
    try:
        from nova.store import DB_PATH
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT timestamp, role, content FROM turns
                 WHERE timestamp > strftime('%s','now','-7 days')
                   AND content LIKE ?
                 ORDER BY timestamp DESC LIMIT 5
                """,
                (f"%{entity_name}%",),
            )
            result["recent_mentions"] = [
                {
                    "ts": r["timestamp"],
                    "role": r["role"],
                    "snippet": str(r["content"])[:200],
                }
                for r in rows
            ]
    except Exception as e:
        logger.debug(f"NCL.about recent_mentions lookup failed: {e}")

    return result


# ---------------------------------------------------------------------------
# timeline — unified episodic stream across stores
# ---------------------------------------------------------------------------

async def timeline(user_id: str, hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
    """Return a chronologically merged stream of events from the last N hours.

    Sources merged:
      - SQLite turns (user/assistant utterances)
      - SQLite learning_events (tool calls, orchestrator decisions)
      - PCG dream insights (date-tagged)

    Each item: {"ts", "kind", "actor", "summary"}
    """
    items: list[dict[str, Any]] = []
    cutoff_ts = time.time() - hours * 3600

    try:
        from nova.store import DB_PATH, get_recent_learning_events
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT timestamp, role, content FROM turns
                 WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?
                """,
                (cutoff_ts, limit),
            )
            for r in rows:
                items.append({
                    "ts": r["timestamp"],
                    "kind": "turn",
                    "actor": r["role"],
                    "summary": str(r["content"])[:200],
                })

        evts = await get_recent_learning_events(limit=200)
        for e in evts:
            if (e.get("timestamp") or 0) < cutoff_ts:
                continue
            tool = e.get("tool_name") or ""
            etype = e.get("event_type") or ""
            outcome = e.get("outcome") or ""
            items.append({
                "ts": e["timestamp"],
                "kind": etype,
                "actor": tool or e.get("source_layer", ""),
                "summary": f"{tool} -> {outcome}" if tool else outcome,
            })
    except Exception as e:
        logger.debug(f"NCL.timeline sqlite read failed: {e}")

    # Dream insights as events
    try:
        dreams = await _load_dream_insights(limit=10, max_age_days=max(1, hours // 24 + 1))
        for d in dreams:
            try:
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(d.get("date", "")).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                ts = time.time()
            if ts < cutoff_ts:
                continue
            items.append({
                "ts": ts,
                "kind": "dream_insight",
                "actor": "nova-dream",
                "summary": d.get("text", "")[:200],
            })
    except Exception as e:
        logger.debug(f"NCL.timeline dream read failed: {e}")

    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# similar — embedding search across embedded content
# ---------------------------------------------------------------------------

async def similar(text: str, k: int = 10) -> list[dict[str, Any]]:
    """Embedding search. First pass uses turn_policy_embeddings; future passes
    will fan out to dream-insight embeddings and CIG topic embeddings as those
    indexes are populated.
    """
    results: list[dict[str, Any]] = []
    try:
        from nova.store import generate_embedding, DB_PATH
        import aiosqlite
        import json as _json

        query_vec = await generate_embedding(text, input_type="query")
        if not query_vec:
            return []

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT e.observation_id, e.embedding_json, o.normalized_text, o.deterministic_intent
                  FROM turn_policy_embeddings e
                  JOIN turn_policy_observations o ON o.id = e.observation_id
                 ORDER BY o.timestamp DESC LIMIT 2000
                """
            )

        # Cosine on the fly. SQLite has ~thousands of rows; this is fine.
        import math
        q = query_vec
        q_norm = math.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for r in rows:
            try:
                v = _json.loads(r["embedding_json"])
            except Exception:
                continue
            if len(v) != len(q):
                continue
            dot = sum(a * b for a, b in zip(q, v))
            v_norm = math.sqrt(sum(x * x for x in v)) or 1.0
            score = dot / (q_norm * v_norm)
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        for score, r in scored[:k]:
            results.append({
                "score": round(score, 4),
                "text": r["normalized_text"],
                "intent": r["deterministic_intent"],
                "kind": "turn_policy_observation",
            })
    except Exception as e:
        logger.debug(f"NCL.similar failed: {e}")
    return results


# ---------------------------------------------------------------------------
# observe / promote — write path
# ---------------------------------------------------------------------------

async def observe(text: str, source: str, salience: float = 0.5) -> bool:
    """Light-weight observation write. Currently routes to PCG observation log.

    `source` examples: 'user:confirmed', 'dream:phase2', 'tool:web_search',
                       'learning:success_pattern'.
    `salience` is a 0..1 hint for downstream decay/promotion logic.
    """
    try:
        from nova.pcg import record_observation
        ok = await record_observation(
            observation=text,
            tags=[f"source:{source}", f"salience:{salience:.2f}"],
        )
        return bool(ok)
    except Exception as e:
        logger.warning(f"NCL.observe failed: {e}")
        return False


async def promote(observation_text: str, category: str, key: str = "") -> bool:
    """Lift an episodic observation to a stable PCG preference.

    Use this when an observation has been confirmed by the user or reinforced
    by multiple sessions. The PCG preference becomes part of the system prompt
    on every future session start.
    """
    try:
        from nova.pcg import create_preference
        import hashlib
        if not key:
            key = hashlib.md5(observation_text.encode()).hexdigest()[:10]
        return await create_preference(
            category=category,
            key=key,
            value=observation_text,
            context="ncl.promote",
        )
    except Exception as e:
        logger.warning(f"NCL.promote failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Internal loaders — small helpers reused across verbs
# ---------------------------------------------------------------------------

async def _load_dream_insights(limit: int = 8, max_age_days: int = 3) -> list[dict[str, Any]]:
    """Explicit dream-insight loader. Pulls directly from PCG preferences
    (category=dream_insight) sorted by date desc. Independent of the
    `/api/pcg/insights/recent` endpoint so dream insights are never
    crowded out by the daily-consolidation feed.
    """
    import aiohttp
    from nova.pcg import PCG_URL, _read_headers, _TIMEOUT  # type: ignore
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pic/preferences?category=dream_insight",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                prefs = data.get("preferences", []) or []
    except Exception as e:
        logger.debug(f"NCL._load_dream_insights HTTP failed: {e}")
        return []

    # Keys are 'YYYY-MM-DD_<hash>' — sort by date desc, filter by age.
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out: list[dict[str, Any]] = []
    for p in sorted(prefs, key=lambda x: x.get("key", ""), reverse=True):
        key = str(p.get("key", ""))
        date_part = key[:10]
        try:
            d = datetime.fromisoformat(date_part).replace(tzinfo=timezone.utc)
            if d < cutoff:
                continue
        except Exception:
            pass
        value = str(p.get("value", ""))
        # Strip optional '[category] ' prefix used by dream cycle
        category = ""
        if value.startswith("[") and "]" in value:
            close = value.index("]")
            category = value[1:close]
            value = value[close + 1:].strip()
        out.append({
            "text": value,
            "date": date_part,
            "category": category or "behavior",
        })
        if len(out) >= limit:
            break
    return out


async def _load_active_goals(user_id: str) -> list[dict[str, Any]]:
    try:
        from nova.pcg import _load_active_goals as _impl
        return await _impl(user_id)
    except Exception as e:
        logger.debug(f"NCL._load_active_goals failed: {e}")
        return []


async def _load_favorites(user_id: str) -> list[dict[str, Any]]:
    """Pull favorites from PCG preferences (category prefix 'favorite_' or
    category 'favorites'). Light filter, no AI."""
    try:
        from nova.pcg import get_preferences
        prefs = await get_preferences()
    except Exception:
        return []
    favs: list[dict[str, Any]] = []
    for p in prefs:
        cat = str(p.get("category", "")).lower()
        if cat == "favorites" or cat.startswith("favorite_") or "favorite" in str(p.get("key", "")).lower():
            favs.append({
                "label": p.get("key", ""),
                "category": p.get("category", ""),
                "value": p.get("value", ""),
            })
    return favs[:12]


def _dream_service_ran_recently(max_age_hours: int = 36) -> bool:
    """Check systemd for the last successful run of nova-dream.service.
    Returns True if a successful run happened within the freshness window.
    Safe to call from any context; on any failure returns False.
    """
    try:
        import subprocess
        from datetime import datetime, timezone, timedelta
        out = subprocess.run(
            ["systemctl", "show", "nova-dream.service", "--property=ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode != 0:
            return False
        line = (out.stdout or "").strip()
        # Format: 'ExecMainExitTimestamp=Wed 2026-05-12 03:04:23 CDT'
        if "=" not in line:
            return False
        value = line.split("=", 1)[1].strip()
        if not value or value == "n/a":
            return False
        # Parse "Day YYYY-MM-DD HH:MM:SS TZ"
        parts = value.split()
        if len(parts) < 3:
            return False
        date_str = parts[1] + " " + parts[2]
        ts = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        return age <= timedelta(hours=max_age_hours)
    except Exception:
        return False


async def _load_recent_session_topics(user_id: str, days: int = 3, limit: int = 6) -> list[str]:
    try:
        from nova.store import get_recent_session_digest
        digest = await get_recent_session_digest(user_id, max_conversations=limit)
    except Exception:
        return []
    topics: list[str] = []
    for entry in digest or []:
        for t in entry.get("topics") or []:
            if t and t not in topics:
                topics.append(str(t))
        if len(topics) >= limit:
            break
    return topics[:limit]
