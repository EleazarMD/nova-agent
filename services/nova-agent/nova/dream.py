"""
Nova Dreaming — idle-time memory consolidation.

Mirrors Anthropic's "Dreams" concept: Nova processes her own past sessions
between conversations to compact transcripts, extract behavioral insights,
and reconcile PCG memory — so each new session starts informed.

Dream cycle:
  1. Compact unprocessed conversations  →  summary + topics + extracted facts
  2. Cross-session insight extraction  →  behavioral patterns across sessions
  3. Memory reconciliation             →  detect + update stale PCG preferences
  4. Write insights to PCG             →  visible at next session start

Run via: python -m nova.dream [--dry-run] [--user-id <uid>]
Or via systemd timer (nova-dream.timer, nightly at 3 AM).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_USER_ID = os.environ.get("NOVA_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")
_PCG_URL = os.environ.get("PCG_URL", "http://localhost:8765")
_PCG_READ_KEY = os.environ.get("PCG_READ_KEY", "dev-read-key-change-in-prod")
_PCG_ADMIN_KEY = os.environ.get("PCG_ADMIN_KEY", "dev-admin-key-change-in-prod")
_AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/v1")
_AI_GATEWAY_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
_DREAM_MAX_SESSIONS = int(os.environ.get("DREAM_MAX_SESSIONS", "10"))
_DREAM_INSIGHT_SESSIONS = int(os.environ.get("DREAM_INSIGHT_SESSIONS", "5"))
_DREAM_MAX_AGE_DAYS = int(os.environ.get("DREAM_MAX_AGE_DAYS", "14"))


# ---------------------------------------------------------------------------
# LLM helper (same gateway as compaction)
# ---------------------------------------------------------------------------

async def _llm(prompt: str, system: str, max_tokens: int = 2000) -> str:
    """Call AI Gateway for a single LLM task. Returns empty string on failure.

    Handles MiniMax M2.7 (reasoning model): content may be null while
    reasoning_content contains the chain-of-thought. We prefer content;
    fall back to reasoning_content if content is null/empty.
    """
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_AI_GATEWAY_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {_AI_GATEWAY_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "default",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"LLM call failed: HTTP {resp.status} — {body[:200]}")
                    return ""
                data = await resp.json()
                msg = data["choices"][0]["message"]
                content = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or ""
                text = (content or reasoning).strip()
                if not text:
                    logger.warning(f"LLM returned empty content and reasoning_content")
                return text
    except Exception as e:
        logger.warning(f"LLM call error: {e}")
        return ""


# ---------------------------------------------------------------------------
# PCG write helpers
# ---------------------------------------------------------------------------

async def _write_insight(date: str, insight: str, category: str = "behavior") -> bool:
    """Write a dream insight to PCG as a preference under category 'dream_insight'.

    Uses POST /api/pic/preferences so insights are readable via get_recent_insights()
    fallback in nova/pcg.py. Key is date-scoped to allow multiple insights per day.
    Also fires /api/pic/learn for the observation log.
    """
    import aiohttp
    import hashlib
    # Deterministic short key: date + hash of insight text
    key = f"{date}_{hashlib.md5(insight.encode()).hexdigest()[:8]}"
    value = f"[{category}] {insight}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_PCG_URL}/api/pic/preferences",
                headers={"X-PIC-Admin-Key": _PCG_ADMIN_KEY},
                json={
                    "category": "dream_insight",
                    "key": key,
                    "value": value,
                    "context": f"Dream cycle {date} — source: nova-dream",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    return True
                logger.warning(f"PCG insight pref write failed: HTTP {resp.status}")
                return False
    except Exception as e:
        logger.warning(f"PCG insight write error: {e}")
        return False


async def _upsert_preference(category: str, key: str, value: str, context: str = "") -> bool:
    """POST a preference update to PCG (upsert by key)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_PCG_URL}/api/pic/preferences",
                headers={"X-PIC-Admin-Key": _PCG_ADMIN_KEY},
                json={"category": category, "key": key, "value": value, "context": context},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status in (200, 201)
    except Exception as e:
        logger.warning(f"PCG preference upsert error: {e}")
        return False


# ---------------------------------------------------------------------------
# Phase 1 — Compaction
# ---------------------------------------------------------------------------

async def _run_compaction(user_id: str, dry_run: bool) -> list[dict]:
    """Compact unprocessed conversations. Returns list of compaction results."""
    from nova.store import run_compaction_cycle
    if dry_run:
        logger.info("[DRY-RUN] Would run compaction cycle")
        return []
    logger.info("Phase 1: Running compaction cycle...")
    results = await run_compaction_cycle(user_id=user_id)
    compacted = [r for r in results if r.get("status") == "compacted"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    failed = [r for r in results if r.get("status") in ("failed", "error")]
    logger.info(
        f"Compaction complete: {len(compacted)} compacted, "
        f"{len(skipped)} skipped, {len(failed)} failed"
    )
    return results


# ---------------------------------------------------------------------------
# Phase 2 — Cross-session insight extraction (works on ANY recent session)
# ---------------------------------------------------------------------------

async def _load_recent_sessions(user_id: str, limit: int) -> list[dict]:
    """Fetch recent sessions with their raw messages for dreaming.

    Works on ALL sessions with messages — not gated by compaction age.
    If a session has been compacted (summary available), uses that.
    Otherwise, reads the raw messages directly.
    """
    try:
        from nova.store import _get_pg_pool
        pool = await _get_pg_pool()

        rows = await pool.fetch(
            """SELECT id, title, summary, topics, config, message_count,
                      COALESCE(last_message_at, updated_at) AS ts
               FROM workspace.ai_conversations
               WHERE user_id IN ($1, 'default')
                 AND message_count >= 4
                 AND COALESCE(last_message_at, updated_at) > NOW() - ($2 || ' days')::interval
               ORDER BY ts DESC
               LIMIT $3""",
            user_id, str(_DREAM_MAX_AGE_DAYS), limit,
        )

        sessions = []
        for r in rows:
            cfg = r["config"] or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception:
                    cfg = {}

            # If no summary yet, pull raw messages to build a mini-transcript
            summary = r["summary"] or ""
            raw_transcript = ""
            if not summary:
                msgs = await pool.fetch(
                    """SELECT role, content FROM workspace.ai_messages
                       WHERE conversation_id = $1::uuid
                       ORDER BY created_at ASC LIMIT 30""",
                    r["id"],
                )
                lines = []
                for m in msgs:
                    role = "User" if m["role"] == "user" else "Nova"
                    lines.append(f"{role}: {str(m['content'])[:150]}")
                raw_transcript = "\n".join(lines)

            sessions.append({
                "id": str(r["id"]),
                "title": r["title"] or "Untitled",
                "summary": summary,
                "raw_transcript": raw_transcript,
                "topics": r["topics"] or [],
                "facts": cfg.get("extracted_facts", []),
                "message_count": r["message_count"] or 0,
                "ts": r["ts"].isoformat() if r["ts"] else "",
            })
        return sessions
    except Exception as e:
        logger.warning(f"Could not load sessions: {e}")
        return []


def _parse_llm_json(raw: str, phase: str) -> dict | None:
    """Robustly extract the outermost JSON object from an LLM response.

    Handles:
    - Pure JSON responses
    - Markdown code-fenced JSON
    - JSON embedded after prose preamble
    - Reasoning model output (chain-of-thought before the JSON)
    """
    import re
    # Strip markdown fences first
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
    cleaned = cleaned.replace("```", "").strip()

    # Try full string first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Walk the string to find the outermost balanced { ... }
    depth = 0
    start = None
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = cleaned[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # Keep searching
                        start = None

    logger.warning(f"{phase}: Could not parse LLM JSON. Raw (first 300): {raw[:300]}")
    return None


async def _extract_cross_session_insights(
    sessions: list[dict],
    existing_prefs: list[dict],
    dry_run: bool,
) -> list[str]:
    """Find behavioral patterns across sessions and write to PCG.

    Works on raw transcripts when compacted summaries are not yet available.
    """
    if not sessions:
        logger.info("Phase 2: No sessions to analyze")
        return []

    session_text = ""
    for s in sessions:
        session_text += f"\n[{s['ts'][:10]}] {s['title']} ({s['message_count']} msgs)\n"
        if s["summary"]:
            session_text += f"  Summary: {s['summary'][:300]}\n"
            topics_str = ", ".join(s["topics"][:6])
            if topics_str:
                session_text += f"  Topics: {topics_str}\n"
        elif s["raw_transcript"]:
            session_text += f"  Transcript (truncated):\n"
            for line in s["raw_transcript"].split("\n")[:12]:
                session_text += f"    {line}\n"

    existing_keys = {p.get("key", "") for p in existing_prefs}
    existing_summary = "\n".join(
        f"  - [{p.get('category','?')}] {p.get('key','?')}: {str(p.get('value',''))[:80]}"
        for p in existing_prefs[:20]
    )

    prompt = f"""You are analyzing Nova's recent conversation sessions to extract behavioral insights.

RECENT SESSIONS ({len(sessions)}):
{session_text}

EXISTING KNOWN PREFERENCES (already in memory — do NOT duplicate):
{existing_summary}

Extract 2-5 NEW behavioral insights NOT already known. Look for:
- Recurring topics or concerns across multiple sessions
- Time-of-day or day-of-week patterns
- Recurring people, places, or contexts
- Emotional patterns (stress triggers, excitement)
- Knowledge gaps or repeated questions Nova could proactively address

Also detect STALE or CONTRADICTED existing preferences the sessions reveal.

Respond ONLY in this JSON format:
{{
  "insights": [
    {{"text": "User frequently asks about Tesla battery state in the evening", "category": "behavior"}},
    {{"text": "User discusses family scheduling on Sunday nights", "category": "routine"}}
  ],
  "stale_preferences": [
    {{"key": "existing_key_to_update", "new_value": "updated value", "category": "category", "reason": "why it changed"}}
  ]
}}"""

    logger.info(f"Phase 2: Analyzing {len(sessions)} sessions for behavioral patterns...")
    raw = await _llm(
        prompt,
        system="You are a behavioral pattern analyst. Output ONLY a raw JSON object with no prose, no markdown, no explanation before or after. Start your response with { and end with }.",
        max_tokens=1200,
    )

    if not raw:
        logger.warning("Phase 2: LLM returned empty response")
        return []

    data = _parse_llm_json(raw, "Phase 2")
    if not data:
        return []

    written_insights = []

    for ins in data.get("insights", [])[:5]:
        text = ins.get("text", "").strip()
        category = ins.get("category", "behavior")
        if not text:
            continue
        if dry_run:
            logger.info(f"[DRY-RUN] Would write insight: [{category}] {text}")
            written_insights.append(text)
        else:
            ok = await _write_insight(datetime.date.today().isoformat(), text, category)
            if ok:
                logger.info(f"  ✓ Insight: [{category}] {text[:80]}")
                written_insights.append(text)
            else:
                logger.warning(f"  ✗ Failed insight: {text[:80]}")

    for sp in data.get("stale_preferences", [])[:3]:
        key = sp.get("key", "")
        new_value = sp.get("new_value", "")
        category = sp.get("category", "other")
        reason = sp.get("reason", "")
        if not key or not new_value or key not in existing_keys:
            continue
        if dry_run:
            logger.info(f"[DRY-RUN] Would update pref [{category}] {key} = {new_value[:60]} ({reason})")
        else:
            ok = await _upsert_preference(category, key, new_value, context=f"dream-rot: {reason}")
            if ok:
                logger.info(f"  ✓ Stale pref updated: [{category}] {key} = {new_value[:60]}")

    return written_insights


# ---------------------------------------------------------------------------
# Phase 3 — Memory rot + deduplication audit
# ---------------------------------------------------------------------------

async def _memory_rot_audit(existing_prefs: list[dict], sessions: list[dict], dry_run: bool) -> dict:
    """Detect and fix: duplicate keys, contradictions, and noise in PCG preferences.

    Returns a dict with counts of: duplicates_found, noise_pruned, contradictions_flagged.
    """
    if len(existing_prefs) < 3:
        logger.info("Phase 3: Too few prefs to audit")
        return {"duplicates_found": 0, "noise_pruned": 0, "contradictions_flagged": 0}

    # Build a condensed view of all prefs for the LLM (keep tight to stay in context)
    prefs_text = "\n".join(
        f"  {p.get('key','')} [{p.get('category','')}]: {str(p.get('value',''))[:80]}"
        for p in existing_prefs
    )

    # Build recent conversation context (last 3 sessions worth of messages)
    recent_context = ""
    for s in sessions[:3]:
        if s.get("summary"):
            recent_context += f"- {s['summary'][:200]}\n"
        elif s.get("raw_transcript"):
            lines = s["raw_transcript"].split("\n")[:6]
            recent_context += "- " + " | ".join(lines) + "\n"

    prompt = f"""You are auditing a personal AI assistant's memory store for quality issues.

CURRENT MEMORY ({len(existing_prefs)} entries):
{prefs_text}

RECENT CONVERSATIONS (context for contradiction detection):
{recent_context if recent_context else "  (none available)"}

Find:
1. DUPLICATES: Two or more entries that store the same or nearly same fact (redundant keys)
2. CONTRADICTIONS: Entries that contradict each other or contradict recent conversations
3. NOISE: Entries that are too vague, generic, or not useful (e.g., key="that", value="yes")

For each issue, specify the key(s) involved and the recommended action.

Respond ONLY in this JSON format:
{{
  "duplicates": [
    {{"keys": ["key_a", "key_b"], "keep": "key_a", "reason": "key_b is redundant"}}
  ],
  "contradictions": [
    {{"key": "some_key", "current_value": "old value", "correct_value": "new value", "reason": "recent session says otherwise"}}
  ],
  "noise": [
    {{"key": "noise_key", "reason": "too vague to be useful"}}
  ]
}}"""

    logger.info(f"Phase 3: Memory rot audit on {len(existing_prefs)} PCG preferences...")
    raw = await _llm(
        prompt,
        system="You are a memory quality auditor. Output ONLY a raw JSON object with no prose, no markdown, no explanation before or after. Start your response with { and end with }.",
        max_tokens=3000,
    )

    result = {"duplicates_found": 0, "noise_pruned": 0, "contradictions_flagged": 0}
    if not raw:
        return result

    data = _parse_llm_json(raw, "Phase 3")
    if not data:
        return result

    existing_keys = {p.get("key", "") for p in existing_prefs}

    # Log duplicates (flag only — don't auto-delete preferences)
    for dup in data.get("duplicates", [])[:5]:
        keys = dup.get("keys", [])
        keep = dup.get("keep", "")
        reason = dup.get("reason", "")
        if len(keys) < 2:
            continue
        result["duplicates_found"] += 1
        logger.info(f"  ⚠ Duplicate: {keys} — keep '{keep}' ({reason})")
        if not dry_run:
            # Write an insight noting the duplication for Nova to clean up
            await _write_insight(
                datetime.date.today().isoformat(),
                f"Memory audit: duplicate preferences detected for {keys}. Keep '{keep}'. {reason}",
                category="memory_audit",
            )

    # Fix contradictions
    for con in data.get("contradictions", [])[:3]:
        key = con.get("key", "")
        correct = con.get("correct_value", "")
        reason = con.get("reason", "")
        if not key or not correct or key not in existing_keys:
            continue
        result["contradictions_flagged"] += 1
        logger.info(f"  ⚠ Contradiction: '{key}' → correct value: {correct[:60]} ({reason})")
        if dry_run:
            logger.info(f"[DRY-RUN] Would fix contradiction for '{key}'")
        else:
            cat = next((p.get("category", "other") for p in existing_prefs if p.get("key") == key), "other")
            ok = await _upsert_preference(cat, key, correct, context=f"dream-contradiction: {reason}")
            if ok:
                logger.info(f"  ✓ Contradiction fixed: [{cat}] {key} = {correct[:60]}")

    # Log noise (flag only — conservative)
    for noise in data.get("noise", [])[:3]:
        key = noise.get("key", "")
        reason = noise.get("reason", "")
        if not key:
            continue
        result["noise_pruned"] += 1
        logger.info(f"  ⚠ Noise entry: '{key}' ({reason}) — flagged, not auto-deleted")

    return result


# ---------------------------------------------------------------------------
# Phase 3 — Fact promotion (extracted facts → PCG preferences)
# ---------------------------------------------------------------------------

async def _promote_facts(sessions: list[dict], existing_prefs: list[dict], dry_run: bool) -> int:
    """Promote high-confidence extracted facts from compacted sessions to PCG preferences."""
    existing_keys = {p.get("key", "") for p in existing_prefs}
    promoted = 0

    for session in sessions:
        for fact in session.get("facts", []):
            key = fact.get("key", "").strip().lower().replace(" ", "_")
            value = fact.get("value", "").strip()
            category = fact.get("category", "other")
            context = fact.get("context", "")

            if not key or not value or len(value) < 5:
                continue

            # Only promote facts not already in PCG (avoid noise)
            if key in existing_keys:
                continue

            # Skip obvious noise keys
            if key in ("yes", "no", "ok", "sure", "thanks", "that", "this"):
                continue

            if dry_run:
                logger.info(f"[DRY-RUN] Would promote fact [{category}] {key} = {value[:60]}")
                promoted += 1
            else:
                ok = await _upsert_preference(
                    category, key, value,
                    context=f"dream-promoted from: {session['title'][:40]}. {context}"[:200],
                )
                if ok:
                    logger.info(f"  ✓ Fact promoted: [{category}] {key} = {value[:60]}")
                    existing_keys.add(key)
                    promoted += 1
                await asyncio.sleep(0.1)

    return promoted


# ---------------------------------------------------------------------------
# Phase 6 — LLM Behavioral Reinforcement Audit
# ---------------------------------------------------------------------------

async def _behavioral_reinforcement_audit(dry_run: bool) -> dict:
    """Meta-analysis reinforcement: use an LLM to judge whether each learned routing
    decision was approved or rejected by the user, then feed those verdicts back into
    upsert_learned_plan_candidate (reinforce) or penalize_learned_plan_candidate (penalize).

    Only fires on sessions where a candidate_applied event exists — meaning the learned
    routing suggestion was actually used.  Caps at 8 sessions / 6 candidates per cycle.
    Only acts on LLM verdicts with confidence >= 0.70 to avoid acting on ambiguous signals.
    """
    import aiosqlite
    from nova.store import get_recent_learning_events, DB_PATH
    from nova.learning import upsert_learned_plan_candidate, penalize_learned_plan_candidate

    result = {"reinforced": 0, "penalized": 0, "neutral": 0, "skipped": 0}

    # Pull a wide event window — we need multiple sessions worth
    events = await get_recent_learning_events(limit=600)
    if not events:
        logger.info("Phase 6: No learning events found — skipping reinforcement audit")
        return result

    # Sort chronologically (DB returns DESC)
    events.sort(key=lambda e: e.get("timestamp", 0))

    # Group by session_id; keep only sessions with at least one candidate_applied event
    from collections import defaultdict
    by_session: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        sid = e.get("session_id") or ""
        if sid:
            by_session[sid].append(e)

    candidate_sessions = {
        sid: evts for sid, evts in by_session.items()
        if any(e["event_type"] == "candidate_applied" for e in evts)
    }

    if not candidate_sessions:
        logger.info("Phase 6: No candidate_applied events found — skipping reinforcement audit")
        return result

    logger.info(f"Phase 6: Auditing {len(candidate_sessions)} sessions with applied candidates...")

    # Look up candidate record (trigger_text + intent + tools_used) by id
    async def _fetch_candidate(cid: int) -> dict | None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await db.execute_fetchall(
                    "SELECT * FROM learned_plan_candidates WHERE id = ?", (cid,)
                )
                return dict(rows[0]) if rows else None
        except Exception:
            return None

    for session_id, session_events in list(candidate_sessions.items())[:8]:
        # Build context blocks — one per candidate_applied event in this session
        context_blocks = []

        for i, ce in enumerate(
            [e for e in session_events if e["event_type"] == "candidate_applied"][:6]
        ):
            payload = json.loads(ce.get("payload_json") or "{}")
            candidate_id = payload.get("candidate_id")
            intent = payload.get("intent", "unknown")
            applied_ts = ce.get("timestamp", 0)
            trigger_text = (ce.get("canonical_text") or "").strip()

            # Subsequent events (up to 6) to gauge user reaction
            subsequent = [
                e for e in session_events if e.get("timestamp", 0) > applied_ts
            ][:6]

            followup_lines = []
            for se in subsequent:
                etype = se["event_type"]
                if etype == "user_turn_received":
                    txt = (se.get("canonical_text") or "").strip()
                    if txt:
                        followup_lines.append(f'  User follow-up: "{txt[:200]}"')
                elif etype == "tool_call_completed":
                    status = "succeeded" if se.get("success") else "FAILED"
                    followup_lines.append(f"  Tool {se.get('tool_name','?')} {status}")

            if not followup_lines:
                result["skipped"] += 1
                continue

            context_blocks.append({
                "index": i + 1,
                "candidate_id": candidate_id,
                "intent": intent,
                "trigger": trigger_text,
                "followup": "\n".join(followup_lines),
            })

        if not context_blocks:
            continue

        # Build the LLM prompt
        blocks_text = ""
        for b in context_blocks:
            blocks_text += (
                f"\n--- Interaction {b['index']} (candidate_id={b['candidate_id']}) ---\n"
                f'User said: "{b["trigger"]}"\n'
                f"Nova routed to intent: {b['intent']}\n"
                f"What followed:\n{b['followup']}\n"
            )

        prompt = (
            "You are auditing a personal AI assistant's routing decisions to determine "
            "whether each was approved or rejected by the user.\n\n"
            "For each interaction, Nova matched the user's message to a learned routing "
            "pattern and invoked a tool. Analyze the user's follow-up to judge the outcome.\n\n"
            "Verdict rules:\n"
            '- "approve": User continued naturally, confirmed, thanked, or the topic resolved\n'
            '- "penalize": User corrected, expressed frustration, asked to redo, or '
            "immediately redirected to something different\n"
            '- "neutral": Insufficient signal to judge\n\n'
            f"INTERACTIONS FOR SESSION {session_id[:12]}:\n{blocks_text}\n\n"
            "Respond ONLY in this JSON format:\n"
            "{\n"
            '  "evaluations": [\n'
            '    {"candidate_id": 42, "verdict": "approve", "confidence": 0.85, '
            '"reason": "User said \'perfect\' and conversation continued normally"},\n'
            '    {"candidate_id": 17, "verdict": "penalize", "confidence": 0.90, '
            '"reason": "User immediately corrected: \'No, I wanted the weather\'"}\n'
            "  ]\n"
            "}"
        )

        logger.info(
            f"Phase 6: Sending {len(context_blocks)} interactions for session "
            f"{session_id[:12]} to LLM..."
        )
        raw = await _llm(
            prompt,
            system=(
                "You are a behavioral reinforcement auditor for an AI assistant. "
                "Output ONLY a raw JSON object with no prose. Start with { and end with }."
            ),
            max_tokens=800,
        )

        if not raw:
            logger.warning(f"Phase 6: LLM returned empty for session {session_id[:12]}")
            result["skipped"] += len(context_blocks)
            continue

        data = _parse_llm_json(raw, "Phase 6")
        if not data:
            result["skipped"] += len(context_blocks)
            continue

        for ev in data.get("evaluations", []):
            cid = ev.get("candidate_id")
            verdict = ev.get("verdict", "neutral")
            confidence = float(ev.get("confidence", 0.0))
            reason = ev.get("reason", "")

            if not cid:
                result["skipped"] += 1
                continue

            if confidence < 0.70:
                logger.info(
                    f"Phase 6: Skipping low-confidence verdict for candidate {cid} "
                    f"(conf={confidence:.2f}) — {reason}"
                )
                result["neutral"] += 1
                continue

            logger.info(
                f"Phase 6: candidate {cid} → {verdict.upper()} "
                f"(conf={confidence:.2f}) | {reason}"
            )

            if verdict == "approve":
                if dry_run:
                    logger.info(f"[DRY-RUN] Would reinforce candidate {cid}")
                    result["reinforced"] += 1
                else:
                    rec = await _fetch_candidate(cid)
                    if rec:
                        tools = json.loads(rec.get("tools_used_json") or "[]")
                        await upsert_learned_plan_candidate(
                            trigger_text=rec["trigger_text"],
                            intent=rec["intent"],
                            tools_used=tools,
                            source_session_id=session_id,
                        )
                        logger.info(
                            f"  ✓ Reinforced [{rec['intent']}] "
                            f"\"{rec['trigger_text'][:60]}\""
                        )
                        result["reinforced"] += 1

            elif verdict == "penalize":
                if dry_run:
                    logger.info(f"[DRY-RUN] Would penalize candidate {cid}")
                    result["penalized"] += 1
                else:
                    await penalize_learned_plan_candidate(cid)
                    result["penalized"] += 1

            else:
                result["neutral"] += 1

    logger.info(
        f"Phase 6 complete: {result['reinforced']} reinforced, "
        f"{result['penalized']} penalized, {result['neutral']} neutral, "
        f"{result['skipped']} skipped"
    )
    return result


# ---------------------------------------------------------------------------
# Main dream cycle
# ---------------------------------------------------------------------------

async def dream_cycle(user_id: str = _USER_ID, dry_run: bool = False) -> dict[str, Any]:
    """Run the full Nova dreaming cycle. Returns a summary report.

    Phases:
      1. Compact eligible sessions (>7 day old messages via decay)
      2. Cross-session insight extraction (works on ANY recent session, no age gate)
      3. Memory rot audit (dedup, contradiction, noise detection)
      4. Fact promotion (compacted-session facts → PCG preferences)
      5. Temporal decay of stale learned plan candidates
      6. LLM behavioral reinforcement audit (approve/penalize routing decisions)
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"=== Nova Dream Cycle started at {started_at.strftime('%Y-%m-%d %H:%M UTC')} ===")
    if dry_run:
        logger.info("DRY-RUN mode — no writes will be made")

    report: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "user_id": user_id,
        "dry_run": dry_run,
        "phase1_compaction": [],
        "phase2_insights": [],
        "phase3_memory_audit": {},
        "phase4_facts_promoted": 0,
        "phase5_candidates_decayed": 0,
        "phase6_reinforcement": {"reinforced": 0, "penalized": 0, "neutral": 0, "skipped": 0},
        "errors": [],
    }

    # --- Phase 1: Compact unprocessed sessions (age-gated by decay, OK to skip) ---
    try:
        compaction_results = await _run_compaction(user_id, dry_run)
        report["phase1_compaction"] = compaction_results
    except Exception as e:
        logger.error(f"Phase 1 error: {e}")
        report["errors"].append(f"compaction: {e}")

    # --- Load existing PCG preferences (used by phases 2, 3, 4) ---
    import aiohttp
    existing_prefs: list[dict] = []
    try:
        async with aiohttp.ClientSession() as _s:
            async with _s.get(
                f"{_PCG_URL}/api/pic/preferences",
                headers={"X-PIC-Read-Key": _PCG_READ_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    existing_prefs = data.get("preferences", [])
        logger.info(f"Loaded {len(existing_prefs)} existing PCG preferences")
    except Exception as e:
        logger.warning(f"Could not load existing prefs: {e}")

    # --- Load recent sessions (raw transcripts if not yet compacted) ---
    sessions: list[dict] = []
    try:
        sessions = await _load_recent_sessions(user_id, _DREAM_INSIGHT_SESSIONS)
        logger.info(f"Loaded {len(sessions)} recent sessions for dreaming ({sum(1 for s in sessions if s['summary'])} with summaries)")
    except Exception as e:
        logger.error(f"Session load error: {e}")
        report["errors"].append(f"session_load: {e}")

    # --- Phase 2: Cross-session insight extraction (immediate — no age gate) ---
    try:
        insights = await _extract_cross_session_insights(sessions, existing_prefs, dry_run)
        report["phase2_insights"] = insights
    except Exception as e:
        logger.error(f"Phase 2 error: {e}")
        report["errors"].append(f"insights: {e}")

    # --- Phase 3: Memory rot audit (dedup + contradiction + noise) ---
    try:
        audit = await _memory_rot_audit(existing_prefs, sessions, dry_run)
        report["phase3_memory_audit"] = audit
    except Exception as e:
        logger.error(f"Phase 3 error: {e}")
        report["errors"].append(f"memory_audit: {e}")

    # --- Phase 4: Promote facts from compacted sessions to PCG ---
    try:
        promoted = await _promote_facts(sessions, existing_prefs, dry_run)
        report["phase4_facts_promoted"] = promoted
    except Exception as e:
        logger.error(f"Phase 4 error: {e}")
        report["errors"].append(f"fact_promotion: {e}")

    # --- Phase 5: Decay stale learned plan candidates ---
    try:
        from nova.learning import decay_stale_candidates
        decayed_count = 0 if dry_run else await decay_stale_candidates(stale_days=30)
        report["phase5_candidates_decayed"] = decayed_count
        if dry_run:
            logger.info("[DRY-RUN] Skipping learned candidate decay")
    except Exception as e:
        logger.error(f"Phase 5 error: {e}")
        report["errors"].append(f"candidate_decay: {e}")

    # --- Phase 6: LLM Behavioral Reinforcement Audit ---
    try:
        reinforcement = await _behavioral_reinforcement_audit(dry_run)
        report["phase6_reinforcement"] = reinforcement
    except Exception as e:
        logger.error(f"Phase 6 error: {e}")
        report["errors"].append(f"reinforcement_audit: {e}")

    # --- Summary ---
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds()
    compacted_count = sum(1 for r in report["phase1_compaction"] if r.get("status") == "compacted")
    audit = report["phase3_memory_audit"]
    report["elapsed_seconds"] = round(elapsed, 1)
    report["summary"] = (
        f"Compacted {compacted_count} sessions | "
        f"{len(report['phase2_insights'])} insights written | "
        f"{audit.get('duplicates_found',0)} dups / {audit.get('contradictions_flagged',0)} contradictions / {audit.get('noise_pruned',0)} noise flagged | "
        f"{report['phase4_facts_promoted']} facts promoted | "
        f"{report['phase5_candidates_decayed']} candidates decayed | "
        f"{report['phase6_reinforcement']['reinforced']}✓/{report['phase6_reinforcement']['penalized']}✗ behaviors reinforced | "
        f"{elapsed:.1f}s"
    )

    logger.info(f"=== Dream cycle complete: {report['summary']} ===")
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Nova Dreaming — idle-time memory consolidation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to PCG")
    parser.add_argument("--user-id", default=_USER_ID, help="Nova user ID")
    args = parser.parse_args()

    logging_format = "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}"
    logger.remove()
    logger.add(sys.stderr, format=logging_format, level="INFO")

    report = asyncio.run(dream_cycle(user_id=args.user_id, dry_run=args.dry_run))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
