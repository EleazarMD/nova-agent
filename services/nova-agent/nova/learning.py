import json
import time
import hashlib
from loguru import logger
import aiosqlite
from typing import Optional

from nova.store import DB_PATH, generate_embedding

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
    
    embedding = await generate_embedding(trigger_text, input_type="passage")
    embedding_json = json.dumps(embedding) if embedding else None
    
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS learned_plan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_text TEXT NOT NULL,
                trigger_hash TEXT NOT NULL,
                trigger_embedding_json TEXT,
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
                "UPDATE learned_plan_candidates SET success_score = ?, confidence = ?, updated_at = ?, trigger_embedding_json = coalesce(trigger_embedding_json, ?) WHERE id = ?",
                (new_score, new_conf, now, embedding_json, row["id"])
            )
        else:
            await db.execute(
                """
                INSERT INTO learned_plan_candidates (
                    trigger_text, trigger_hash, trigger_embedding_json, intent, tools_used_json, 
                    success_score, confidence, source_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trigger_text, trigger_hash, embedding_json, intent, tools_json, 1.0, 0.5, source_session_id, now, now)
            )
        await db.commit()

async def penalize_learned_plan_candidate(candidate_id: int, path: str = DB_PATH):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM learned_plan_candidates WHERE id = ?", (candidate_id,))
        row = await cursor.fetchone()
        if row:
            new_conf = row["confidence"] * 0.5
            if new_conf < 0.30:
                await db.execute("DELETE FROM learned_plan_candidates WHERE id = ?", (candidate_id,))
                logger.info(f"LEARNING_PENALTY | Purged bad candidate {candidate_id}")
            else:
                await db.execute("UPDATE learned_plan_candidates SET confidence = ? WHERE id = ?", (new_conf, candidate_id))
                logger.info(f"LEARNING_PENALTY | Penalized candidate {candidate_id} -> {new_conf:.2f}")
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
                trigger_embedding_json TEXT,
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
    last_applied_candidate_id = None
    
    for event in events:
        events_so_far.append(event)
        
        # Track if we just applied a candidate
        if event["event_type"] == "candidate_applied":
            payload = json.loads(event["payload_json"])
            last_applied_candidate_id = payload.get("candidate_id")
            continue
            
        # Detect corrections immediately following an applied candidate
        if event["event_type"] == "user_turn_received" and last_applied_candidate_id is not None:
            raw_text = (event.get("canonical_text") or "").strip().lower()
            import re
            text = re.sub(r'[^\w\s]', '', raw_text)  # Strip punctuation for easier matching
            correction_terms = ("no", "stop", "wrong", "cancel", "undo", "thats not what i meant", "no stop", "wait no", "incorrect", "nevermind")
            if text in correction_terms or any(text.startswith(t) for t in ("no ", "stop ", "wait ", "wrong ")):
                logger.info(f"LEARNING_PENALTY | Detected user correction '{raw_text}' for candidate {last_applied_candidate_id}")
                await penalize_learned_plan_candidate(last_applied_candidate_id, path=path)
                last_applied_candidate_id = None  # Consume the correction
            elif text:
                last_applied_candidate_id = None  # User moved on normally
        
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

def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = sum(a * a for a in vec1) ** 0.5
    norm_b = sum(b * b for b in vec2) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

async def get_shadow_plan_candidates(text: str, path: str = DB_PATH) -> list[dict]:
    if not text or len(text.split()) < 2:
        return []
    
    from nova.store import generate_embedding
    from nova.turn_policy import _jaccard_similarity
    
    query_embedding = await generate_embedding(text, input_type="passage")
    
    async with aiosqlite.connect(path) as db:
        # Check if table exists
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='learned_plan_candidates'")
        if not await cursor.fetchone():
            return []
            
        db.row_factory = aiosqlite.Row
        # Get highest confidence matches first to minimize processing
        rows = await db.execute_fetchall("SELECT * FROM learned_plan_candidates ORDER BY confidence DESC LIMIT 100")
        candidates = [dict(r) for r in rows]
        
    text_lower = text.lower()
    matches = []
    
    for c in candidates:
        sim = 0.0
        if query_embedding and c.get("trigger_embedding_json"):
            try:
                candidate_embedding = json.loads(c["trigger_embedding_json"])
                sim = _cosine_similarity(query_embedding, candidate_embedding)
            except Exception:
                sim = _jaccard_similarity(text_lower, c["trigger_text"].lower())
        else:
            sim = _jaccard_similarity(text_lower, c["trigger_text"].lower())
            
        # Semantic thresholds usually range 0.70-0.85 for 'similar' intent
        if (query_embedding and sim > 0.65) or (not query_embedding and sim > 0.45):
            # Scale confidence by similarity
            confidence_multiplier = sim if query_embedding else (sim * 1.5)
            matches.append({
                "id": c["id"],
                "intent": c["intent"],
                "confidence": round(min(0.95, c["confidence"] * confidence_multiplier), 3),
                "trigger_text": c["trigger_text"],
                "tools_used": json.loads(c["tools_used_json"]),
                "similarity": round(sim, 3),
                "match_type": "semantic" if query_embedding and c.get("trigger_embedding_json") else "lexical"
            })
            
    return sorted(matches, key=lambda x: x["confidence"], reverse=True)
