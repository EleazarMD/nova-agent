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
        await db.commit()


async def get_or_create_session(
    user_id: str,
    conversation_id: str,
    path: str = DB_PATH,
) -> Session:
    """Get existing session for this conversation or create one."""
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
    user_id: str,
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
        
        # Insert message
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
    """Search past conversations: SQLite first, then PostgreSQL directly.
    
    No Dashboard dependency — Nova queries her own data directly.
    
    Time intervals:
      days_back=7          → last 7 days from now
      from_days=90, to_days=7  → between 3 months ago and 1 week ago
    """
    # 1. SQLite first — fast, always available, no external dependency
    results = await _search_local_conversations(user_id, query, days_back, limit)
    
    # 2. If SQLite had nothing, try PostgreSQL directly (not via Dashboard)
    if not results:
        results = await _search_postgres_direct(user_id, query, days_back, limit,
                                                 from_days=from_days, to_days=to_days)
    
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
    """Search conversations directly in PostgreSQL via asyncpg.
    
    Bypasses the Dashboard API entirely — Nova owns her data.
    Includes 'default' user's conversations for historical data.
    """
    if not _HAS_ASYNCPG:
        logger.debug("asyncpg not available, skipping direct PG search")
        return []
    
    try:
        pool = await _get_pg_pool()
        terms = [t for t in query.split() if len(t) >= 2]
        if not terms:
            return []
        
        # Build parameterized ILIKE conditions ($3, $4, ... for search terms)
        # Each term needs two placeholders (content + title)
        param_idx = 3  # $1=user_id, $2=limit
        conditions = []
        params = [user_id, limit]
        for t in terms:
            like_val = f"%{t}%"
            conditions.append(f"(m.content ILIKE ${param_idx} OR c.title ILIKE ${param_idx})")
            params.append(like_val)
            param_idx += 1
        conditions_sql = " OR ".join(conditions)
        
        # Time window (parameterized)
        if from_days is not None and to_days is not None:
            from_interval = f"{min(from_days, 365)} days"
            to_interval = f"{max(to_days, 0)} days"
            time_filter = f"c.created_at >= NOW() - INTERVAL ${param_idx} AND c.created_at <= NOW() - INTERVAL ${param_idx + 1}"
            params.extend([from_interval, to_interval])
            param_idx += 2
        else:
            interval = f"{min(days_back, 365)} days"
            time_filter = f"c.created_at >= NOW() - INTERVAL ${param_idx}"
            params.append(interval)
            param_idx += 1
        
        # Include 'default' user for historical data
        user_filter = (
            "c.user_id = $1" if user_id == "default"
            else "c.user_id IN ($1, 'default')"
        )
        
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
                    "source": "postgres_direct",
                })
            
            if results:
                logger.info(f"Direct PG search found {len(results)} conversations for '{query}'")
            return results
    except Exception as e:
        logger.warning(f"Direct PG search error: {e}")
        return []


async def get_backend_conversations(
    user_id: str,
    limit: int = 20,
) -> list[dict]:
    """Get user's conversations from PostgreSQL directly via asyncpg."""
    if not _HAS_ASYNCPG:
        return []
    try:
        pool = await _get_pg_pool()
        # Include 'default' user for historical data
        user_filter = "user_id = $1" if user_id == "default" else "user_id IN ($1, 'default')"
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
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
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
            "created_at": conv["created_at"].isoformat() if conv["created_at"] else None,
            "messages": [
                {
                    "id": str(m["id"]),
                    "role": m["role"],
                    "content": m["content"],
                    "model": m["model"],
                    "tokens_used": m["tokens_used"],
                    "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                    "importance_score": m["importance_score"],
                }
                for m in msgs
            ],
        }
    except Exception as e:
        logger.warning(f"Get conversation error: {e}")
        return None
