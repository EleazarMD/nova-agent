"""
Persistent conversation store backed by SQLite + PostgreSQL.

SQLite: Primary local store — fast, always available, no external dependency.
PostgreSQL (direct asyncpg): Long-term retention, cross-device sync, full-text search.

Nova owns her data pipeline end-to-end. No Dashboard API dependency.

Stores conversation turns per conversation_id, supports session lookup by
user_id, and syncs to PostgreSQL for cross-device access and retention.
"""

import aiosqlite
import asyncio
import datetime
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from loguru import logger

try:
    import asyncpg
    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False

DB_PATH = os.environ.get("SQLITE_PATH", "./data/nova.db")
PG_DSN = os.environ.get("DATABASE_URL", "postgresql://eleazar@localhost/ecosystem_unified")

# NVIDIA NIM Embeddings (llama-3.2-nv-embedqa-1b-v2, 2048-dim, TensorRT on RTX GPU)
_NIM_EMBED_URL = os.environ.get("NIM_EMBED_URL", "http://localhost:8006/v1/embeddings")
_NIM_EMBED_MODEL = os.environ.get("NIM_EMBED_MODEL", "nvidia/llama-3.2-nv-embedqa-1b-v2")
_nim_available: Optional[bool] = None

# Reusable asyncpg pool (lazy-initialized)
_pg_pool: Optional[asyncpg.Pool] = None


async def _get_pg_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool."""
    global _pg_pool
    if _pg_pool is not None and not _pg_pool._closed:
        return _pg_pool
    if not _HAS_ASYNCPG:
        raise RuntimeError("asyncpg not installed")
    _pg_pool = await asyncpg.create_pool(dsn=PG_DSN, min_size=1, max_size=5)
    return _pg_pool


async def _check_nim_available() -> bool:
    """Check if NVIDIA NIM embedding service is running."""
    global _nim_available
    if _nim_available is None:
        try:
            import httpx
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: __import__("httpx").get("http://localhost:8006/v1/models", timeout=5)
            )
            _nim_available = resp.status_code == 200
        except Exception:
            try:
                import httpx as _hx
                async with _hx.AsyncClient(timeout=5.0) as c:
                    resp = await c.get("http://localhost:8006/v1/models")
                    _nim_available = resp.status_code == 200
            except Exception:
                _nim_available = False
        if _nim_available:
            logger.info("NVIDIA NIM embeddings available (llama-3.2-nv-embedqa-1b-v2, 2048-dim)")
        else:
            logger.warning("NVIDIA NIM embeddings not reachable")
    return _nim_available


async def generate_embedding(text: str, input_type: str = "passage") -> list[float] | None:
    """Generate embedding via NVIDIA NIM (llama-3.2-nv-embedqa-1b-v2, 2048-dim, TensorRT on RTX GPU).
    
    Truncates to 500 chars to stay under NIM's 512-token limit.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _NIM_EMBED_URL,
                json={
                    "model": _NIM_EMBED_MODEL,
                    "input": [text[:500]],
                    "input_type": input_type,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [{}])[0].get("embedding")
            logger.warning(f"NIM embedding error: {resp.status_code}")
    except Exception as e:
        logger.warning(f"NIM embedding request failed: {e}")
    return None


@dataclass
class Turn:
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float
    tool_calls: Optional[str] = None  # JSON-serialized tool calls if any


@dataclass
class Session:
    session_id: str
    user_id: str
    conversation_id: str
    created_at: float
    last_active: float
    metadata: Optional[str] = None  # JSON blob


async def init_db(path: str = DB_PATH):
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_active REAL NOT NULL,
                metadata TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON sessions(user_id, last_active DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                tool_calls TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turns_session
            ON turns(session_id, timestamp ASC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turn_policy_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                utterance_hash TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                deterministic_intent TEXT NOT NULL,
                shadow_intent TEXT,
                shadow_confidence REAL,
                handled INTEGER,
                outcome TEXT NOT NULL,
                tools_used TEXT,
                stop_reason TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                features_json TEXT NOT NULL,
                shadow_candidate_json TEXT,
                observation_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_policy_observations_hash
            ON turn_policy_observations(utterance_hash, timestamp DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_policy_observations_intent
            ON turn_policy_observations(deterministic_intent, timestamp DESC)
        """)
        await db.commit()


async def get_or_create_session(
    user_id: str,
    conversation_id: str,
    path: str = DB_PATH,
) -> Session:
    """Get existing session for this conversation or create one."""
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    session_id = f"{user_id}:{conversation_id}"
    now = time.time()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute_fetchall(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        if row:
            r = row[0]
            await db.execute(
                "UPDATE sessions SET last_active = ? WHERE session_id = ?",
                (now, session_id),
            )
            await db.commit()
            return Session(
                session_id=r["session_id"],
                user_id=r["user_id"],
                conversation_id=r["conversation_id"],
                created_at=r["created_at"],
                last_active=now,
                metadata=r["metadata"],
            )
        else:
            await db.execute(
                "INSERT INTO sessions (session_id, user_id, conversation_id, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, conversation_id, now, now),
            )
            await db.commit()
            return Session(
                session_id=session_id,
                user_id=user_id,
                conversation_id=conversation_id,
                created_at=now,
                last_active=now,
            )


async def get_session_metadata(session_id: str, path: str = DB_PATH) -> dict:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT metadata FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if not rows or not rows[0]["metadata"]:
            return {}
        try:
            data = json.loads(rows[0]["metadata"])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            logger.warning(f"Invalid session metadata JSON for {session_id}")
            return {}


async def update_session_metadata(session_id: str, metadata: dict, path: str = DB_PATH):
    now = time.time()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE sessions SET metadata = ?, last_active = ? WHERE session_id = ?",
            (json.dumps(metadata), now, session_id),
        )
        await db.commit()


async def update_session_metadata_key(session_id: str, key: str, value, path: str = DB_PATH):
    metadata = await get_session_metadata(session_id, path=path)
    metadata[key] = value
    await update_session_metadata(session_id, metadata, path=path)


async def append_turn(
    session_id: str,
    role: str,
    content: str,
    tool_calls: Optional[list] = None,
    path: str = DB_PATH,
):
    """Append a turn to the conversation."""
    now = time.time()
    tc_json = json.dumps(tool_calls) if tool_calls else None
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO turns (session_id, role, content, timestamp, tool_calls) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, now, tc_json),
        )
        await db.execute(
            "UPDATE sessions SET last_active = ? WHERE session_id = ?",
            (now, session_id),
        )
        await db.commit()


async def append_turn_policy_observation(observation, path: str = DB_PATH):
    features = observation.features.to_dict()
    shadow = observation.shadow_candidate.to_dict() if observation.shadow_candidate else None
    payload = observation.to_dict()
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turn_policy_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                utterance_hash TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                deterministic_intent TEXT NOT NULL,
                shadow_intent TEXT,
                shadow_confidence REAL,
                handled INTEGER,
                outcome TEXT NOT NULL,
                tools_used TEXT,
                stop_reason TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                features_json TEXT NOT NULL,
                shadow_candidate_json TEXT,
                observation_json TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_policy_observations_hash
            ON turn_policy_observations(utterance_hash, timestamp DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_policy_observations_intent
            ON turn_policy_observations(deterministic_intent, timestamp DESC)
        """)
        await db.execute(
            """
            INSERT INTO turn_policy_observations (
                timestamp,
                utterance_hash,
                normalized_text,
                deterministic_intent,
                shadow_intent,
                shadow_confidence,
                handled,
                outcome,
                tools_used,
                stop_reason,
                latency_ms,
                features_json,
                shadow_candidate_json,
                observation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(observation.ts),
                str(features.get("utterance_hash") or ""),
                str(features.get("normalized_text") or ""),
                observation.deterministic_intent,
                shadow.get("intent") if shadow else None,
                float(shadow.get("confidence")) if shadow and shadow.get("confidence") is not None else None,
                None if observation.handled is None else int(bool(observation.handled)),
                observation.outcome,
                json.dumps(observation.tools_used),
                observation.stop_reason,
                int(observation.latency_ms or 0),
                json.dumps(features),
                json.dumps(shadow) if shadow else None,
                json.dumps(payload),
            ),
        )
        await db.commit()


async def get_recent_turn_policy_observations(limit: int = 100, path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM turn_policy_observations ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


async def get_successful_turn_policy_observations(limit: int = 200, path: str = DB_PATH) -> list[dict]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """
            SELECT *
            FROM turn_policy_observations
            WHERE handled = 1
              AND deterministic_intent != 'pass_through'
              AND outcome NOT IN ('user_correction', 'repeat_request', 'near_repeat_request')
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]


async def label_turn_policy_observation(
    observation_id: int,
    outcome: str,
    label: dict,
    path: str = DB_PATH,
) -> bool:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT observation_json FROM turn_policy_observations WHERE id = ?",
            (observation_id,),
        )
        if not rows:
            return False
        try:
            payload = json.loads(rows[0]["observation_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        payload["outcome"] = outcome
        payload["outcome_label"] = label
        await db.execute(
            """
            UPDATE turn_policy_observations
            SET outcome = ?, observation_json = ?
            WHERE id = ?
            """,
            (outcome, json.dumps(payload), observation_id),
        )
        await db.commit()
        return True


async def get_history(
    session_id: str,
    limit: int = 40,
    path: str = DB_PATH,
) -> list[Turn]:
    """Get the most recent turns for a session."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        )
        turns = [
            Turn(
                role=r["role"],
                content=r["content"],
                timestamp=r["timestamp"],
                tool_calls=r["tool_calls"],
            )
            for r in reversed(rows)  # reverse to chronological order
        ]
        return turns


async def get_user_sessions(
    user_id: str,
    limit: int = 20,
    path: str = DB_PATH,
) -> list[Session]:
    """Get recent sessions for a user."""
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_active DESC LIMIT ?",
            (user_id, limit),
        )
        return [
            Session(
                session_id=r["session_id"],
                user_id=r["user_id"],
                conversation_id=r["conversation_id"],
                created_at=r["created_at"],
                last_active=r["last_active"],
                metadata=r["metadata"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# PostgreSQL Direct (asyncpg) — Nova owns her data pipeline
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# PII redaction patterns (mirrors Dashboard API logic)
_CRITICAL_REDACTIONS = [
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[REDACTED:SSN]'),
    (re.compile(r'\b(?:\d{4}[-\s]?){3}\d{4}\b'), '[REDACTED:CARD]'),
    (re.compile(r'(?:password|passwd|pwd)\s*[:=]\s*["\']?(\S{4,})["\']?', re.I), '[REDACTED:PASSWORD]'),
    (re.compile(r'(?:api[_-]?key|apikey|secret|token)\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', re.I), '[REDACTED:API_KEY]'),
    (re.compile(r'Bearer\s+[a-zA-Z0-9_\-.]+', re.I), '[REDACTED:BEARER]'),
]

def _sanitize_content(content: str) -> str:
    """Redact critical PII before storage."""
    for pattern, replacement in _CRITICAL_REDACTIONS:
        content = pattern.sub(replacement, content)
    return content

def _calculate_importance(content: str, role: str, has_tool_calls: bool = False) -> int:
    """Calculate message importance score (0-100)."""
    score = 50
    lower = content.lower()
    if has_tool_calls:
        score += 15
    for kw in ['remember', 'important', 'never forget', 'always', 'critical', 'must', 'decision', 'agreed']:
        if kw in lower:
            score += 10
            break
    for kw in ['prefer', 'like', 'want', 'need', 'should']:
        if kw in lower:
            score += 5
            break
    for kw in ['hello', 'hi', 'thanks', 'okay', 'ok', 'bye', 'goodbye']:
        if lower == kw or lower.startswith(kw + ' ') or lower.startswith(kw + ','):
            score -= 10
            break
    if len(content) > 500:
        score += 10
    elif len(content) < 20:
        score -= 10
    if role == 'user' and '?' in content:
        score += 5
    return max(0, min(100, score))


async def _resolve_or_create_conversation(
    conversation_id: str,
    user_id: str,
    pool: asyncpg.Pool,
) -> Optional[str]:
    """Resolve Nova's string conversation_id to PostgreSQL UUID.
    
    Tries: UUID match → external_id lookup → auto-create.
    Returns the PostgreSQL UUID or None on failure.
    """
    # 1. Direct UUID match
    if _UUID_RE.match(conversation_id):
        row = await pool.fetchrow(
            "SELECT id FROM workspace.ai_conversations WHERE id = $1 AND user_id = $2",
            conversation_id, user_id,
        )
        if row:
            return str(row["id"])
    
    # 2. Lookup by external_id in config JSONB
    row = await pool.fetchrow(
        """SELECT id FROM workspace.ai_conversations
           WHERE user_id = $1 AND config->>'external_id' = $2
           ORDER BY created_at DESC LIMIT 1""",
        user_id, conversation_id,
    )
    if row:
        return str(row["id"])
    
    # 3. Auto-create
    row = await pool.fetchrow(
        """INSERT INTO workspace.ai_conversations
           (title, user_id, source, config, importance_score, retention_tier)
           VALUES ($1, $2, 'nova', $3::jsonb, 50, 'hot')
           RETURNING id""",
        f"Nova Conversation {conversation_id[:8]}",
        user_id,
        json.dumps({"external_id": conversation_id}),
    )
    if row:
        logger.info(f"Auto-created PG conversation: {conversation_id[:16]}...")
        return str(row["id"])
    
    return None


async def _sync_message_to_backend(
    conversation_id: str,
    user_id: str,  # canonical_user_id() already applied by caller (sync_turn_to_backend)
    role: str,
    content: str,
    model: str = None,
    tokens_used: int = 0,
    tool_calls: list = None,
    _retry: int = 0,
):
    """Sync a message directly to PostgreSQL via asyncpg.
    
    No Dashboard dependency. Handles conversation resolution, PII redaction,
    importance scoring, and conversation stats updates.
    """
    if not _HAS_ASYNCPG:
        logger.debug("asyncpg not available, skipping backend sync")
        return
    
    try:
        pool = await _get_pg_pool()
        
        # Resolve conversation UUID
        pg_conv_id = await _resolve_or_create_conversation(conversation_id, user_id, pool)
        if not pg_conv_id:
            logger.error(f"Failed to resolve/create conversation: {conversation_id}")
            return
        
        # Sanitize and score
        safe_content = _sanitize_content(content)
        importance = _calculate_importance(safe_content, role, bool(tool_calls))
        is_preserved = importance >= 70
        
        # Insert message (with embedding for user messages and important assistant messages)
        embedding = None
        if role == "user" or (role == "assistant" and importance >= 60):
            embedding = await generate_embedding(safe_content, input_type="passage")

        if embedding:
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await pool.execute(
                """INSERT INTO workspace.ai_messages
                   (conversation_id, role, content, model, tokens_used, cost, metadata,
                    importance_score, is_preserved, embedding)
                   VALUES ($1::uuid, $2, $3, $4, $5, 0, $6::jsonb, $7, $8, $9::vector)""",
                pg_conv_id, role, safe_content, model, tokens_used or 0,
                json.dumps({"tool_calls": tool_calls} if tool_calls else {}),
                importance, is_preserved, embedding_str,
            )
        else:
            await pool.execute(
                """INSERT INTO workspace.ai_messages
                   (conversation_id, role, content, model, tokens_used, cost, metadata,
                    importance_score, is_preserved)
                   VALUES ($1::uuid, $2, $3, $4, $5, 0, $6::jsonb, $7, $8)""",
                pg_conv_id, role, safe_content, model, tokens_used or 0,
                json.dumps({"tool_calls": tool_calls} if tool_calls else {}),
                importance, is_preserved,
            )
        
        # Update conversation stats
        await pool.execute(
            """UPDATE workspace.ai_conversations
               SET last_message_at = NOW(),
                   updated_at = NOW(),
                   message_count = message_count + 1,
                   total_tokens = total_tokens + $2,
                   importance_score = (
                       SELECT COALESCE(AVG(importance_score)::INTEGER, 50)
                       FROM workspace.ai_messages WHERE conversation_id = $1::uuid
                   )
               WHERE id = $1::uuid""",
            pg_conv_id, tokens_used or 0,
        )
        
        logger.info(f"✅ Synced {role} message to PG: {conversation_id[:16]}... ({len(content)} chars)")
    
    except Exception as e:
        if _retry < 1:
            logger.warning(f"PG sync error, retrying: {e}")
            await asyncio.sleep(1)
            return await _sync_message_to_backend(
                conversation_id, user_id, role, content,
                model, tokens_used, tool_calls, _retry=_retry + 1,
            )
        logger.error(f"❌ PG sync failed after retry: {e}")


async def ensure_backend_conversation(
    conversation_id: str,
    user_id: str,
    title: str = "Nova Conversation",
    session_context: dict | None = None,
) -> bool:
    """Ensure conversation exists in PostgreSQL via asyncpg.
    
    Uses external_id to map Nova's string conversation IDs to PostgreSQL UUIDs.
    No Dashboard dependency.
    """
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    if not _HAS_ASYNCPG:
        return False
    
    try:
        pool = await _get_pg_pool()
        
        # Check if exists (by UUID or external_id)
        pg_id = await _resolve_or_create_conversation(conversation_id, user_id, pool)
        if pg_id:
            # If auto-created, update title and session_context if provided
            if session_context:
                config_update = {"external_id": conversation_id}
                for key in ("client", "audio_mode", "device", "app_version", "timezone", "started_at"):
                    if key in session_context:
                        config_update[key] = session_context[key]
                if "location" in session_context:
                    config_update["location"] = session_context["location"]
                await pool.execute(
                    "UPDATE workspace.ai_conversations SET config = config || $2::jsonb WHERE id = $1::uuid AND NOT (config ? 'location')",
                    pg_id, json.dumps(config_update),
                )
            logger.info(f"Backend conversation ensured: {conversation_id[:16]}...")
            return True
        
        return False
    except Exception as e:
        logger.warning(f"Backend conversation ensure error: {e}")
        return False


async def search_past_conversations(
    user_id: str,
    query: str,
    days_back: int = 30,
    limit: int = 5,
    from_days: int | None = None,
    to_days: int | None = None,
) -> list[dict]:
    """Search past conversations: PostgreSQL vector search first, then keyword fallbacks.
    
    No Dashboard dependency — Nova queries her own data directly.
    
    Search order:
      1. PostgreSQL vector similarity (NVIDIA NIM embeddings + pgvector)
      2. PostgreSQL ILIKE keyword search
      3. SQLite keyword search (local fallback)
    
    Time intervals:
      days_back=7          → last 7 days from now
      from_days=90, to_days=7  → between 3 months ago and 1 week ago
    """
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    # 1. PostgreSQL vector search first (semantic — understands meaning, not just keywords)
    results = await _search_postgres_direct(user_id, query, days_back, limit,
                                             from_days=from_days, to_days=to_days)
    if results:
        return results
    
    # 2. SQLite keyword search as local fallback
    results = await _search_local_conversations(user_id, query, days_back, limit)
    
    return results


async def _search_local_conversations(
    user_id: str,
    query: str,
    days_back: int,
    limit: int,
) -> list[dict]:
    """Search recent conversations in local SQLite.
    
    Returns actual matching messages with surrounding context, not just
    conversation metadata. Searches across the given user_id AND 'default'
    user, since historical conversations were stored under user_id='default'.
    """
    try:
        terms = [t.lower() for t in query.split() if len(t) >= 2]
        if not terms:
            return []
        
        cutoff = time.time() - (days_back * 86400)
        placeholders = " OR ".join(["LOWER(t.content) LIKE ?" for _ in terms])
        params = [f"%{t}%" for t in terms]
        
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            
            # Try specific user_id first, then fall back to all users
            for user_filter, label in [
                (f"s.user_id IN (?, 'default')", "user+default"),
                ("1=1", "all_users"),
            ]:
                # Find matching messages directly — not grouped by conversation
                match_rows = await db.execute_fetchall(
                    f"""SELECT t.rowid, t.role, t.content, t.timestamp,
                               s.conversation_id, s.session_id, s.user_id
                        FROM turns t
                        JOIN sessions s ON t.session_id = s.session_id
                        WHERE {user_filter}
                          AND t.timestamp >= ?
                          AND ({placeholders})
                        ORDER BY t.timestamp DESC
                        LIMIT ?""",
                    [user_id, cutoff] + params + [limit * 3] if label == "user+default"
                    else [cutoff] + params + [limit * 3],
                )
                
                if not match_rows:
                    continue
                
                # For each match, get surrounding context (prev + next message)
                results = []
                seen_conversations = set()
                for m in match_rows:
                    conv_id = m["conversation_id"]
                    
                    # Get context: 1 message before and 1 after the match
                    context_parts = []
                    try:
                        context_rows = await db.execute_fetchall(
                            """SELECT role, content FROM turns
                               WHERE session_id = ? AND rowid BETWEEN ? AND ?
                               ORDER BY rowid""",
                            (m["session_id"], m["rowid"] - 1, m["rowid"] + 1),
                        )
                        for cr in context_rows:
                            role_label = "User" if cr["role"] == "user" else "Nova"
                            context_parts.append(f"{role_label}: {cr['content'][:150]}")
                    except Exception:
                        context_parts.append(f"{'User' if m['role']=='user' else 'Nova'}: {m['content'][:200]}")
                    
                    # Derive a title from the conversation's first user message
                    title_row = await db.execute_fetchall(
                        """SELECT content FROM turns
                           WHERE session_id = ? AND role = 'user'
                           ORDER BY rowid LIMIT 1""",
                        (m["session_id"],),
                    )
                    title = title_row[0]["content"][:60] if title_row else f"Conversation {conv_id[:8]}"
                    
                    # Timestamp for display
                    ts = m["timestamp"] or 0
                    date_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"
                    
                    results.append({
                        "conversation_id": conv_id,
                        "title": title,
                        "snippet": "\n".join(context_parts)[:400],
                        "date": date_str,
                        "relevance_score": 0.7,
                        "source": f"sqlite({label})",
                    })
                    
                    seen_conversations.add(conv_id)
                    if len(results) >= limit:
                        break
                
                if results:
                    logger.info(f"Local SQLite found {len(results)} matches for '{query}' (scope={label})")
                    return results
            
            return []
    except Exception as e:
        logger.warning(f"Local conversation search error: {e}")
        return []


async def _search_postgres_direct(
    user_id: str,
    query: str,
    days_back: int,
    limit: int,
    from_days: int | None = None,
    to_days: int | None = None,
) -> list[dict]:
    """Search conversations in PostgreSQL via asyncpg using vector similarity.

    Primary: NVIDIA NIM embedding → pgvector cosine similarity search.
    Fallback: ILIKE keyword search if embeddings unavailable.
    Includes 'default' user's conversations for historical data.
    """
    if not _HAS_ASYNCPG:
        logger.debug("asyncpg not available, skipping direct PG search")
        return []

    try:
        pool = await _get_pg_pool()

        # Include 'default' user for historical data
        user_filter = (
            "c.user_id = $1" if user_id == "default"
            else "c.user_id IN ($1, 'default')"
        )

        # Time window
        if from_days is not None and to_days is not None:
            interval_str = f"NOW() - INTERVAL '{min(from_days, 365)} days' AND NOW() - INTERVAL '{max(to_days, 0)} days'"
            time_filter = f"c.created_at BETWEEN {interval_str}"
        else:
            time_filter = f"c.created_at >= NOW() - INTERVAL '{min(days_back, 365)} days'"

        # ── Primary: Vector similarity search via NVIDIA NIM embeddings ──
        query_embedding = await generate_embedding(query, input_type="query")
        if query_embedding:
            embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
            async with pool.acquire() as conn:
                # Search by cosine similarity on embedded messages, then get context
                rows = await conn.fetch(
                    f"""SELECT DISTINCT ON (c.id)
                           c.id as conversation_id,
                           c.title,
                           m.content as snippet,
                           1 - (m.embedding <=> $3::vector) as similarity,
                           c.created_at,
                           c.message_count
                       FROM workspace.ai_conversations c
                       JOIN workspace.ai_messages m ON m.conversation_id = c.id
                       WHERE {user_filter}
                         AND c.retention_tier != 'archived'
                         AND {time_filter}
                         AND m.embedding IS NOT NULL
                       ORDER BY c.id, m.embedding <=> $3::vector
                       LIMIT $2""",
                    user_id, limit, embedding_str,
                )

                if rows:
                    results = []
                    for r in rows:
                        # Get surrounding context for the best matching message
                        snippet = (r["snippet"] or "")[:300]
                        # Try to get more context from surrounding messages
                        conv_id = r["conversation_id"]
                        context_rows = await conn.fetch(
                            """SELECT role, content FROM workspace.ai_messages
                               WHERE conversation_id = $1::uuid
                               ORDER BY created_at DESC LIMIT 5""",
                            conv_id,
                        )
                        if context_rows:
                            parts = []
                            for cr in context_rows:
                                label = "User" if cr["role"] == "user" else "Nova"
                                parts.append(f"{label}: {cr['content'][:150]}")
                            snippet = "\n".join(parts)[:400]

                        results.append({
                            "conversation_id": str(r["conversation_id"]),
                            "title": r["title"] or "Untitled",
                            "snippet": snippet,
                            "relevance_score": float(r["similarity"]) if r["similarity"] else 0.5,
                            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                            "message_count": r["message_count"] or 0,
                            "source": "postgres_vector",
                        })

                    logger.info(f"Vector search found {len(results)} conversations for '{query}'")
                    return results

        # ── Fallback: ILIKE keyword search ──
        terms = [t for t in query.split() if len(t) >= 2]
        if not terms:
            return []

        param_idx = 3  # $1=user_id, $2=limit
        conditions = []
        params = [user_id, limit]
        for t in terms:
            like_val = f"%{t}%"
            conditions.append(f"(m.content ILIKE ${param_idx} OR c.title ILIKE ${param_idx})")
            params.append(like_val)
            param_idx += 1
        conditions_sql = " OR ".join(conditions)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT DISTINCT ON (c.id)
                       c.id as conversation_id,
                       c.title,
                       m.content as snippet,
                       c.importance_score as relevance_score,
                       c.created_at,
                       c.message_count
                   FROM workspace.ai_conversations c
                   JOIN workspace.ai_messages m ON m.conversation_id = c.id
                   WHERE {user_filter}
                     AND c.retention_tier != 'archived'
                     AND {time_filter}
                     AND ({conditions_sql})
                   ORDER BY c.id, c.importance_score DESC, c.last_message_at DESC
                   LIMIT $2""",
                *params,
            )

            results = []
            for r in rows:
                results.append({
                    "conversation_id": str(r["conversation_id"]),
                    "title": r["title"] or "Untitled",
                    "snippet": (r["snippet"] or "")[:200],
                    "relevance_score": (r["relevance_score"] or 50) / 100,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                    "message_count": r["message_count"] or 0,
                    "source": "postgres_keyword",
                })

            if results:
                logger.info(f"Keyword search found {len(results)} conversations for '{query}'")
            return results
    except Exception as e:
        logger.warning(f"Direct PG search error: {e}")
        return []


async def get_backend_conversations(
    user_id: str,
    limit: int = 20,
) -> list[dict]:
    """Get user's conversations from PostgreSQL directly via asyncpg."""
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    if not _HAS_ASYNCPG:
        return []
    try:
        pool = await _get_pg_pool()
        # All historical aliases now collapse to canonical_user_id at the
        # entry boundary, so a single equality check is sufficient.
        user_filter = "user_id = $1"
        rows = await pool.fetch(
            f"""SELECT id, title, user_id, source, importance_score, summary,
                       retention_tier, message_count, total_tokens,
                       created_at, updated_at, last_message_at
                FROM workspace.ai_conversations
                WHERE {user_filter}
                  AND retention_tier != 'archived'
                ORDER BY last_message_at DESC NULLS LAST, updated_at DESC
                LIMIT $2""",
            user_id, limit,
        )
        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "user_id": r["user_id"],
                "source": r["source"],
                "importance_score": r["importance_score"],
                "message_count": r["message_count"],
                "created_at": r["created_at"].timestamp() if r["created_at"] else None,
                "updated_at": r["updated_at"].timestamp() if r["updated_at"] else None,
                "last_message_at": r["last_message_at"].timestamp() if r["last_message_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"Get conversations error: {e}")
        return []


async def get_backend_conversation(
    conversation_id: str,
    user_id: str,
) -> dict | None:
    """Get a single conversation with messages from PostgreSQL directly via asyncpg."""
    from nova.user_resolver import canonical_user_id
    user_id = canonical_user_id(user_id)
    if not _HAS_ASYNCPG:
        return None
    try:
        pool = await _get_pg_pool()
        
        # Resolve conversation UUID
        pg_id = await _resolve_or_create_conversation(conversation_id, user_id, pool)
        if not pg_id:
            return None
        
        # Get conversation metadata
        conv = await pool.fetchrow(
            """SELECT id, title, user_id, source, importance_score, message_count,
                      created_at, updated_at, last_message_at
               FROM workspace.ai_conversations WHERE id = $1::uuid""",
            pg_id,
        )
        if not conv:
            return None
        
        # Get messages
        msgs = await pool.fetch(
            """SELECT id, role, content, model, tokens_used, created_at,
                      importance_score, is_preserved
               FROM workspace.ai_messages
               WHERE conversation_id = $1::uuid
               ORDER BY created_at ASC""",
            pg_id,
        )
        
        return {
            "id": str(conv["id"]),
            "title": conv["title"],
            "user_id": conv["user_id"],
            "message_count": conv["message_count"],
            "created_at": conv["created_at"].timestamp() if conv["created_at"] else None,
            "updated_at": conv["updated_at"].timestamp() if conv["updated_at"] else None,
            "last_message_at": conv["last_message_at"].timestamp() if conv["last_message_at"] else None,
            "messages": [
                {
                    "id": str(m["id"]),
                    "role": m["role"],
                    "content": m["content"],
                    "model": m["model"],
                    "tokens_used": m["tokens_used"],
                    "created_at": m["created_at"].timestamp() if m["created_at"] else None,
                    "importance_score": m["importance_score"],
                }
                for m in msgs
            ],
        }
    except Exception as e:
        logger.warning(f"Get conversation error: {e}")
        return None


# ---------------------------------------------------------------------------
# Conversation Compaction with Negative Exponential Decay
# ---------------------------------------------------------------------------

# Decay rate λ — controls how quickly conversation fidelity drops
# weight = e^(-λ * age_days)
# λ=0.05: half-life ~14 days, 30-day weight=0.22, 90-day weight=0.01
# λ=0.03: half-life ~23 days, 30-day weight=0.41, 90-day weight=0.07
_COMPACTION_LAMBDA = float(os.environ.get("COMPACTION_LAMBDA", "0.05"))
_COMPACTION_MIN_AGE_DAYS = int(os.environ.get("COMPACTION_MIN_AGE_DAYS", "7"))
_COMPACTION_BATCH_SIZE = int(os.environ.get("COMPACTION_BATCH_SIZE", "5"))

AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")


def _decay_weight(age_days: float) -> float:
    """Negative exponential decay: e^(-λ * age_days). Returns 0..1."""
    import math
    return math.exp(-_COMPACTION_LAMBDA * age_days)


async def _llm_summarize(prompt: str, system: str = "You are a precise summarizer.") -> str:
    """Call the AI Gateway for a single summarization/extraction task."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{AI_GATEWAY_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "minimax-m2.5",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            logger.warning(f"LLM summarize error: {resp.status_code}")
    except Exception as e:
        logger.warning(f"LLM summarize failed: {e}")
    return ""


async def compact_conversation(conv_id: str, pool=None) -> dict | None:
    """Compact a single conversation using negative exponential decay.
    
    Recent messages (high weight) are preserved verbatim.
    Older messages (low weight) are summarized into topics/subtopics.
    Extracted facts are stored in conversation config metadata — 
    Nova decides what to save to PCG via save_memory.
    
    Returns compaction result dict or None on failure.
    """
    if not _HAS_ASYNCPG:
        return None
    
    _pool = pool or await _get_pg_pool()
    
    try:
        # Get conversation metadata
        conv = await _pool.fetchrow(
            """SELECT id, title, user_id, message_count, created_at, 
                      last_message_at, compacted_at, summary, topics
               FROM workspace.ai_conversations WHERE id = $1::uuid""",
            conv_id,
        )
        if not conv:
            return None
        
        # Skip if already compacted recently (within 24h)
        if conv["compacted_at"] and conv["compacted_at"] > datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24):
            logger.debug(f"Skipping recently compacted: {conv_id}")
            return {"status": "skipped", "reason": "recently_compacted"}
        
        # Get all messages
        msgs = await _pool.fetch(
            """SELECT id, role, content, importance_score, created_at
               FROM workspace.ai_messages
               WHERE conversation_id = $1::uuid
               ORDER BY created_at ASC""",
            conv_id,
        )
        
        if len(msgs) < 6:
            return {"status": "skipped", "reason": "too_few_messages", "count": len(msgs)}
        
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # ── Apply negative exponential decay ──
        # Partition messages into: preserved (high weight) vs compacted (low weight)
        preserved_msgs = []
        compacted_msgs = []
        
        for m in msgs:
            age_days = (now - m["created_at"]).total_seconds() / 86400
            weight = _decay_weight(age_days)
            
            # Always preserve high-importance messages regardless of age
            if m["importance_score"] >= 80:
                preserved_msgs.append(m)
            elif weight >= 0.3:  # Recent enough to keep verbatim
                preserved_msgs.append(m)
            else:
                compacted_msgs.append(m)
        
        if not compacted_msgs:
            return {"status": "skipped", "reason": "all_recent", "preserved": len(preserved_msgs)}
        
        # ── Summarize compacted messages into topics/subtopics ──
        compacted_text = "\n".join(
            f"{'User' if m['role']=='user' else 'Nova'}: {m['content'][:200]}"
            for m in compacted_msgs
        )[:2500]  # Limit to avoid LLM output truncation
        
        summary_prompt = f"""Summarize this conversation segment into a structured format.

CONVERSATION (older messages, being compacted):
{compacted_text}

Respond in this EXACT JSON format:
{{
  "summary": "1-2 sentence overview of what was discussed",
  "topics": [
    {{"topic": "Topic Name", "subtopics": ["subtopic 1", "subtopic 2"], "key_points": ["point 1", "point 2"]}}
  ],
  "facts": [
    {{"category": "preference|identity|schedule|relationship|health|work|home|other", "key": "short_key", "value": "the fact", "context": "how it came up"}}
  ]
}}

Extract ALL important facts the user stated — preferences, names, dates, habits, health info, 
work details, home details, relationships. These will be saved to long-term memory."""

        summary_text = await _llm_summarize(
            summary_prompt,
            system="You are a precise conversation analyst. Extract structured summaries and facts. Always respond with valid JSON.",
        )
        
        if not summary_text:
            return {"status": "failed", "reason": "llm_error"}
        
        # Parse the summary — handle markdown code blocks and nested braces
        import re as _re
        # Strip markdown code fences if present
        cleaned = _re.sub(r'```(?:json)?\s*', '', summary_text)
        cleaned = cleaned.strip()
        # Try to find the outermost JSON object
        depth = 0
        start = None
        for i, ch in enumerate(cleaned):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    json_str = cleaned[start:i+1]
                    try:
                        summary_data = json.loads(json_str)
                        break
                    except json.JSONDecodeError:
                        start = None
                        continue
        else:
            return {"status": "failed", "reason": "json_error", "raw": summary_text[:300]}
        
        # ── Store extracted facts in conversation metadata (for Nova to review) ──
        # Facts are NOT auto-saved to PCG. Nova decides what's important via save_memory.
        facts = summary_data.get("facts", [])
        
        # ── Update conversation in PostgreSQL ──
        topics_list = [t.get("topic", "") for t in summary_data.get("topics", []) if t.get("topic")]
        summary_str = summary_data.get("summary", "")
        
        # Store facts in config JSONB so Nova can review them later
        existing_config = await _pool.fetchval(
            "SELECT config FROM workspace.ai_conversations WHERE id = $1::uuid", conv_id
        ) or {}
        if isinstance(existing_config, str):
            try:
                existing_config = json.loads(existing_config)
            except Exception:
                existing_config = {}
        existing_config["extracted_facts"] = facts
        existing_config["compaction_count"] = existing_config.get("compaction_count", 0) + 1
        
        await _pool.execute(
            """UPDATE workspace.ai_conversations
               SET summary = $2,
                   topics = $3,
                   config = $4::jsonb,
                   compacted_at = NOW(),
                   retention_tier = CASE 
                       WHEN EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400 > 90 THEN 'cold'
                       WHEN EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400 > 30 THEN 'warm'
                       ELSE retention_tier
                   END
               WHERE id = $1::uuid""",
            conv_id, summary_str, topics_list, json.dumps(existing_config),
        )
        
        # ── Mark compacted messages as not preserved (they're now in the summary) ──
        # Keep them in DB for vector search, but mark as compacted
        compacted_ids = [m["id"] for m in compacted_msgs]
        if compacted_ids:
            await _pool.execute(
                """UPDATE workspace.ai_messages
                   SET is_preserved = false
                   WHERE id = ANY($1::uuid[])""",
                compacted_ids,
            )
        
        result = {
            "status": "compacted",
            "conversation_id": str(conv_id),
            "title": conv["title"],
            "total_messages": len(msgs),
            "preserved": len(preserved_msgs),
            "compacted": len(compacted_msgs),
            "topics": topics_list,
            "summary": summary_str,
            "facts_extracted": len(facts),
            "facts_stored_in_metadata": len(facts),
        }
        
        logger.info(
            f"Compacted {conv['title'][:40]}: {len(compacted_msgs)}/{len(msgs)} msgs → "
            f"{len(topics_list)} topics, {len(facts)} facts in metadata"
        )
        return result
        
    except Exception as e:
        logger.error(f"Compaction error for {conv_id}: {e}")
        return {"status": "error", "reason": str(e)}


async def run_compaction_cycle(user_id: str = "default") -> list[dict]:
    """Run a compaction cycle over all conversations for a user.
    
    Uses negative exponential decay to determine which conversations need compaction.
    Only processes conversations older than _COMPACTION_MIN_AGE_DAYS.
    """
    if not _HAS_ASYNCPG:
        return []
    
    pool = await _get_pg_pool()
    results = []
    
    try:
        # Find conversations eligible for compaction
        # Not recently compacted, older than min age, have enough messages
        rows = await pool.fetch(
            """SELECT id, title, message_count, created_at, compacted_at
               FROM workspace.ai_conversations
               WHERE user_id IN ($1, 'default')
                 AND retention_tier != 'archived'
                 AND message_count >= 6
                 AND created_at < NOW() - ($2 || ' days')::interval
                 AND (compacted_at IS NULL OR compacted_at < NOW() - INTERVAL '24 hours')
               ORDER BY created_at ASC
               LIMIT $3""",
            user_id, str(_COMPACTION_MIN_AGE_DAYS), _COMPACTION_BATCH_SIZE,
        )
        
        if not rows:
            logger.debug("No conversations need compaction")
            return []
        
        logger.info(f"Compaction cycle: {len(rows)} conversations to process")
        
        for row in rows:
            result = await compact_conversation(str(row["id"]), pool=pool)
            if result:
                results.append(result)
            # Small delay between compactions
            await asyncio.sleep(0.5)
        
        compacted = sum(1 for r in results if r.get("status") == "compacted")
        logger.info(f"Compaction cycle complete: {compacted}/{len(rows)} compacted")
        
    except Exception as e:
        logger.error(f"Compaction cycle error: {e}")
    
    return results


async def get_compacted_context(
    conversation_id: str,
    user_id: str,
    max_recent_turns: int = 20,
) -> list[dict]:
    """Get conversation context with compaction-aware retrieval.
    
    Returns messages with negative exponential decay applied:
    - Recent messages (high weight): full verbatim
    - Older messages (low weight): replaced by compacted summary
    - Very old (weight < 0.05): just topic headers
    
    This is the function that should be used when building LLM context
    instead of raw get_history().
    """
    if not _HAS_ASYNCPG:
        return []
    
    pool = await _get_pg_pool()
    now = datetime.datetime.now(datetime.timezone.utc)
    messages = []
    
    try:
        pg_id = await _resolve_or_create_conversation(conversation_id, user_id, pool)
        if not pg_id:
            return []
        
        # Get conversation metadata (summary, topics from compaction)
        conv = await pool.fetchrow(
            """SELECT summary, topics, compacted_at FROM workspace.ai_conversations WHERE id = $1::uuid""",
            pg_id,
        )
        
        # Get recent messages (verbatim)
        recent_msgs = await pool.fetch(
            """SELECT role, content, importance_score, created_at
               FROM workspace.ai_messages
               WHERE conversation_id = $1::uuid
               ORDER BY created_at DESC
               LIMIT $2""",
            pg_id, max_recent_turns,
        )
        
        # Add recent messages in chronological order
        for m in reversed(recent_msgs):
            messages.append({"role": m["role"], "content": m["content"]})
        
        # If there's a compacted summary, prepend it as context
        if conv and conv["summary"]:
            topics_str = ""
            if conv["topics"]:
                topics_str = "\nTopics: " + ", ".join(conv["topics"])
            
            compacted_context = (
                f"[Earlier conversation summary: {conv['summary']}{topics_str}]"
            )
            # Insert after system message position
            messages.insert(0, {"role": "assistant", "content": compacted_context})
        
        return messages
        
    except Exception as e:
        logger.warning(f"Get compacted context error: {e}")
        return []
