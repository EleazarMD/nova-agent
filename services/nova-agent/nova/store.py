"""
Persistent conversation store backed by SQLite + PostgreSQL sync.

SQLite: Local session state and fast access for current conversation.
PostgreSQL (via Dashboard API): Source of truth for all conversation history.

Stores conversation turns per conversation_id, supports session lookup by
user_id, and syncs to backend for RFI retention policy.
"""

import aiohttp
import aiosqlite
import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional
from loguru import logger

DB_PATH = os.environ.get("SQLITE_PATH", "./data/nova.db")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8404")
DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "ai-gateway-api-key-2024")


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
# PostgreSQL Sync (via Dashboard API) - Source of Truth
# ---------------------------------------------------------------------------

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
    """Sync a message to PostgreSQL via Dashboard API.
    
    Retries once on transient failures. Called via await (bot.py) or
    asyncio.create_task (text_chat.py) — errors are logged prominently.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/memory/conversations/{conversation_id}/messages",
                headers={
                    "X-API-Key": DASHBOARD_API_KEY,
                    "X-User-Id": user_id,
                    "Content-Type": "application/json",
                },
                json={
                    "role": role,
                    "content": content,
                    "model": model,
                    "tokens_used": tokens_used,
                    "tool_calls": tool_calls,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"✅ Synced {role} message to backend: {conversation_id[:16]}... ({len(content)} chars)")
                else:
                    text = await resp.text()
                    if _retry < 1 and resp.status >= 500:
                        logger.warning(f"Backend sync retryable ({resp.status}), retrying: {text[:80]}")
                        await asyncio.sleep(1)
                        return await _sync_message_to_backend(
                            conversation_id, user_id, role, content,
                            model, tokens_used, tool_calls, _retry=_retry + 1,
                        )
                    logger.warning(f"Backend sync failed: {resp.status} {text[:100]}")
    except Exception as e:
        if _retry < 1:
            logger.warning(f"Backend sync error, retrying: {e}")
            await asyncio.sleep(1)
            return await _sync_message_to_backend(
                conversation_id, user_id, role, content,
                model, tokens_used, tool_calls, _retry=_retry + 1,
            )
        logger.error(f"❌ Backend sync failed after retry: {e}")


async def ensure_backend_conversation(
    conversation_id: str,
    user_id: str,
    title: str = "Nova Conversation",
    session_context: dict | None = None,
) -> bool:
    """Ensure conversation exists in PostgreSQL backend.
    
    Uses external_id to map Nova's string conversation IDs to PostgreSQL UUIDs.
    
    session_context (optional) is set once at session start:
      client: "ios" | "dashboard" | "tesla" | "web"
      audio_mode: "native" | "server" | "text"
      device: str or None (e.g. "iPhone 16 Pro")
      app_version: str or None
      location: {city, state, zip, lat, lng} or None
      timezone: str or None
    
    Clients without location (Tesla, Dashboard) simply omit the field.
    """
    try:
        body: dict = {
            "title": title,
            "source": "nova",
            "external_id": conversation_id,
        }
        if session_context:
            body["session_context"] = session_context

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DASHBOARD_URL}/api/memory/conversations",
                headers={
                    "X-API-Key": DASHBOARD_API_KEY,
                    "X-User-Id": user_id,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    existing = data.get("existing", False)
                    logger.info(f"Backend conversation {'found' if existing else 'created'}: {conversation_id}")
                    return True
                else:
                    logger.warning(f"Failed to ensure backend conversation: {resp.status}")
                    return False
    except Exception as e:
        logger.warning(f"Backend conversation check error: {e}")
        return False


async def search_past_conversations(
    user_id: str,
    query: str,
    days_back: int = 30,
    limit: int = 5,
    from_days: int | None = None,
    to_days: int | None = None,
) -> list[dict]:
    """Search past conversations via Dashboard API with SQLite fallback.
    
    Time intervals:
      days_back=7          → last 7 days from now
      from_days=90, to_days=7  → between 3 months ago and 1 week ago
    """
    results = []
    
    # Try Dashboard API first (PostgreSQL + ChromaDB vector search)
    try:
        params: dict = {"q": query, "limit": limit}
        if from_days is not None and to_days is not None:
            params["from_days"] = from_days
            params["to_days"] = to_days
        else:
            params["days_back"] = days_back

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/memory/conversations/search",
                params=params,
                headers={
                    "X-API-Key": DASHBOARD_API_KEY,
                    "X-User-Id": user_id,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                else:
                    logger.warning(f"Conversation search failed: {resp.status}")
    except Exception as e:
        logger.warning(f"Conversation search error: {e}")
    
    # If backend returned nothing, fall back to local SQLite search
    if not results:
        results = await _search_local_conversations(user_id, query, days_back, limit)
    
    return results


async def _search_local_conversations(
    user_id: str,
    query: str,
    days_back: int,
    limit: int,
) -> list[dict]:
    """Fallback: search recent conversations in local SQLite.
    
    Searches across the given user_id AND 'default' user, since historical
    conversations were stored under user_id='default' before proper user
    identification was implemented. Also falls back to all users if the
    specific user_id yields nothing.
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
                rows = await db.execute_fetchall(
                    f"""SELECT DISTINCT s.conversation_id, s.session_id,
                               MIN(CASE WHEN t.role='user' THEN t.content END) as first_user_msg,
                               COUNT(*) as match_count
                        FROM turns t
                        JOIN sessions s ON t.session_id = s.session_id
                        WHERE {user_filter}
                          AND t.timestamp >= ?
                          AND ({placeholders})
                        GROUP BY s.conversation_id
                        ORDER BY match_count DESC, MAX(t.timestamp) DESC
                        LIMIT ?""",
                    [user_id, cutoff] + params + [limit] if label == "user+default"
                    else [cutoff] + params + [limit],
                )
                
                if rows:
                    results = []
                    for r in rows:
                        snippet = r["first_user_msg"] or ""
                        results.append({
                            "conversation_id": r["conversation_id"],
                            "title": f"Conversation {r['conversation_id'][:8]}",
                            "snippet": snippet[:200],
                            "relevance_score": min(r["match_count"] / 10.0, 1.0),
                            "source": f"sqlite_fallback({label})",
                        })
                    logger.info(f"Local SQLite fallback found {len(results)} conversations for '{query}' (scope={label})")
                    return results
            
            return []
    except Exception as e:
        logger.warning(f"Local conversation search error: {e}")
        return []


async def get_backend_conversations(
    user_id: str,
    limit: int = 20,
) -> list[dict]:
    """Get user's conversations from PostgreSQL backend."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/memory/conversations",
                params={"limit": limit},
                headers={
                    "X-API-Key": DASHBOARD_API_KEY,
                    "X-User-Id": user_id,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("conversations", [])
                else:
                    return []
    except Exception as e:
        logger.warning(f"Get conversations error: {e}")
        return []


async def get_backend_conversation(
    conversation_id: str,
    user_id: str,
) -> dict | None:
    """Get a single conversation with messages from PostgreSQL backend."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/memory/conversations/{conversation_id}",
                headers={
                    "X-API-Key": DASHBOARD_API_KEY,
                    "X-User-Id": user_id,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("conversation")
                else:
                    return None
    except Exception as e:
        logger.warning(f"Get conversation error: {e}")
        return None
