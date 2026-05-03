import json
import time
import hashlib
from loguru import logger
import aiosqlite
from typing import Optional

from nova.store import DB_PATH

async def upsert_learned_plan_candidate(
    trigger_text: str,
    intent: str,
    tools_used: list[str],
    source_session_id: str,
    path: str = DB_PATH
):
    if not trigger_text or len(trigger_text.split()) < 2:
        return

    trigger_hash = hashlib.sha256(trigger_text.encode("utf-8")).hexdigest()[:16]
    now = time.time()
    tools_json = json.dumps(tools_used)
    
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS learned_plan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_text TEXT NOT NULL,
                trigger_hash TEXT NOT NULL,
                intent TEXT NOT NULL,
                tools_used_json TEXT NOT NULL,
                success_score REAL DEFAULT 1.0,
                confidence REAL DEFAULT 0.5,
                source_session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(trigger_hash, intent)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_learned_plan_candidates_hash
            ON learned_plan_candidates(trigger_hash)
        """)
        
        # Check if exists
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM learned_plan_candidates WHERE trigger_hash = ? AND intent = ?",
            (trigger_hash, intent)
        )
        
        if rows:
            # Increment score and confidence
            row = rows[0]
            new_score = row["success_score"] + 1.0
            new_conf = min(0.95, row["confidence"] + 0.1)
            await db.execute(
                "UPDATE learned_plan_candidates SET success_score = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (new_score, new_conf, now, row["id"])
            )
        else:
            await db.execute(
                """
                INSERT INTO learned_plan_candidates (
                    trigger_text, trigger_hash, intent, tools_used_json, 
                    success_score, confidence, source_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trigger_text, trigger_hash, intent, tools_json, 1.0, 0.5, source_session_id, now, now)
            )
        await db.commit()

async def consolidate_session_learning(session_id: str, path: str = DB_PATH):
    """
    Look back at the recent events for a session and extract learned plan candidates.
    Specifically targeting: memory_save_request, memory_recall_request, etc.
    """
    async with aiosqlite.connect(path) as db:
        # Just create table if not exists (in case it wasn't created yet)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS learned_plan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_text TEXT NOT NULL,
                trigger_hash TEXT NOT NULL,
                intent TEXT NOT NULL,
                tools_used_json TEXT NOT NULL,
                success_score REAL DEFAULT 1.0,
                confidence REAL DEFAULT 0.5,
                source_session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(trigger_hash, intent)
            )
        """)
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM learning_events WHERE session_id = ? ORDER BY timestamp ASC LIMIT 200",
            (session_id,)
        )
        events = [dict(r) for r in rows]

    events_so_far = []
    
    for event in events:
        events_so_far.append(event)
        if event["event_type"] == "tool_call_completed" and event.get("success"):
            tool_name = event.get("tool_name")
            if tool_name in ("save_memory", "recall_memory", "query_cig", "get_weather", "tesla_control", "hub_delegate"):
                # Find substantive user text leading to this
                substantive_text = ""
                for e in reversed(events_so_far):
                    if e["event_type"] == "user_turn_received":
                        text = (e.get("canonical_text") or "").strip()
                        if len(text.split()) > 1 and text.lower() not in ("yes", "no", "yeah", "nope", "do it", "please", "sure", "ok", "okay"):
                            substantive_text = text
                            break
                
                if substantive_text:
                    intent_map = {
                        "save_memory": "memory_save_request",
                        "recall_memory": "memory_recall_request",
                        "query_cig": "email_lookup",
                        "get_weather": "weather_lookup",
                        "tesla_control": "tesla_control",
                        "hub_delegate": "hub_delegate"
                    }
                    intent = intent_map.get(tool_name, tool_name)
                    await upsert_learned_plan_candidate(
                        trigger_text=substantive_text,
                        intent=intent,
                        tools_used=[tool_name],
                        source_session_id=session_id,
                        path=path
                    )
                    logger.info(f"LEARNING | Extracted episode: '{substantive_text}' -> {intent}")

async def get_shadow_plan_candidates(text: str, path: str = DB_PATH) -> list[dict]:
    if not text or len(text.split()) < 2:
        return []
    
    import re
    from nova.turn_policy import _jaccard_similarity
    
    async with aiosqlite.connect(path) as db:
        # Check if table exists
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='learned_plan_candidates'")
        if not await cursor.fetchone():
            return []
            
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM learned_plan_candidates ORDER BY confidence DESC LIMIT 100")
        candidates = [dict(r) for r in rows]
        
    text_lower = text.lower()
    matches = []
    
    for c in candidates:
        sim = _jaccard_similarity(text_lower, c["trigger_text"].lower())
        if sim > 0.45:
            matches.append({
                "intent": c["intent"],
                "confidence": round(min(0.95, c["confidence"] * sim * 1.5), 3),
                "trigger_text": c["trigger_text"],
                "tools_used": json.loads(c["tools_used_json"]),
                "similarity": round(sim, 3)
            })
            
    return sorted(matches, key=lambda x: x["confidence"], reverse=True)
