"""
PCG (Personal Context Graph) — Unified client for Nova Agent.

Single entry point for ALL context: personal identity, preferences, goals,
observations, knowledge graph entities, and LIAM frameworks.

Backed by the PCG service on port 8765 (Neo4j + Redis).

Data flow:
  Session start → build_context() → system prompt (cached in-process)
  Mid-session   → get_preferences() / get_identity() (from cache)
  User states   → record_observation() → PCG → cache invalidated
  Knowledge     → query() → PCG /api/kg/search + /api/pcg/preferences
  Hub agent task → POST /api/pcg/learn → PCG (write-through)
"""

import os
import time
import aiohttp
import json
from typing import Any, Optional
from loguru import logger

PCG_URL = os.environ.get("PCG_URL", "http://localhost:8765")
PCG_READ_KEY = os.environ.get("PCG_READ_KEY", "dev-read-key-change-in-prod")
PCG_ADMIN_KEY = os.environ.get("PCG_ADMIN_KEY", "dev-admin-key-change-in-prod")

_TIMEOUT = aiohttp.ClientTimeout(total=5)


# ---------------------------------------------------------------------------
# Session-scoped in-process cache
# ---------------------------------------------------------------------------

class _PCGCache:
    """Simple in-process cache for PCG data within a Nova session."""

    def __init__(self):
        self._identity: Optional[dict] = None
        self._preferences: Optional[list[dict]] = None
        self._goals: Optional[list[dict]] = None
        self._loaded_at: float = 0.0

    def is_warm(self) -> bool:
        return self._loaded_at > 0

    def invalidate(self):
        """Clear cache — called after writes so next read hits PCG."""
        self._identity = None
        self._preferences = None
        self._goals = None
        self._loaded_at = 0.0
        logger.debug("PCG cache invalidated")

    def store(self, identity: Optional[dict], preferences: list[dict], goals: list[dict]):
        self._identity = identity
        self._preferences = preferences
        self._goals = goals
        self._loaded_at = time.monotonic()

    @property
    def identity(self) -> Optional[dict]:
        return self._identity

    @property
    def preferences(self) -> list[dict]:
        return self._preferences or []

    @property
    def goals(self) -> list[dict]:
        return self._goals or []


_cache = _PCGCache()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _read_headers() -> dict[str, str]:
    return {"X-PIC-Read-Key": PCG_READ_KEY}


def _admin_headers() -> dict[str, str]:
    return {
        "X-PIC-Admin-Key": PCG_ADMIN_KEY,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Personal context (identity, preferences, goals)
# ---------------------------------------------------------------------------

async def _fetch_identity() -> Optional[dict[str, Any]]:
    """Fetch user identity profile from PCG."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pic/identity",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"PCG identity fetch failed: HTTP {resp.status}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.warning(f"PCG identity unavailable: {e}")
        return None


async def _fetch_preferences() -> list[dict]:
    """Fetch all user preferences from PCG."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pic/preferences",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("preferences", [])
    except Exception as e:
        logger.warning(f"PCG preferences unavailable: {e}")
        return []


async def _fetch_goals(status: str = "active") -> list[dict]:
    """Fetch user goals from PCG."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pic/goals?status={status}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("goals", [])
    except Exception as e:
        logger.warning(f"PCG goals unavailable: {e}")
        return []


async def _ensure_cache():
    """Warm the cache if cold. Called before any read."""
    if _cache.is_warm():
        return
    t0 = time.monotonic()
    identity = await _fetch_identity()
    preferences = await _fetch_preferences()
    goals = await _fetch_goals()
    _cache.store(identity, preferences, goals)
    elapsed = (time.monotonic() - t0) * 1000
    logger.info(f"PCG cache warmed: {len(preferences)} prefs, {len(goals)} goals ({elapsed:.0f}ms)")


async def get_identity() -> Optional[dict[str, Any]]:
    """Get user identity (cached)."""
    await _ensure_cache()
    return _cache.identity


async def get_preferences(categories: Optional[list[str]] = None) -> list[dict]:
    """Get user preferences (cached), optionally filtered by category."""
    await _ensure_cache()
    prefs = _cache.preferences
    if categories:
        prefs = [p for p in prefs if p.get("category") in categories]
    return prefs


async def get_goals(status: str = "active") -> list[dict]:
    """Get user goals (cached)."""
    await _ensure_cache()
    return _cache.goals


# ---------------------------------------------------------------------------
# Personal writes (observations + preferences — invalidates cache)
# ---------------------------------------------------------------------------

async def record_observation(
    observation_type: str,
    category: str,
    key: str,
    value: str,
    context: str = "",
) -> bool:
    """Record a learning observation directly to PCG.

    PCG stores it in Neo4j as an :Observation node. Observations are
    consolidated into preferences when enough corroborating data exists.
    Invalidates the local cache so subsequent reads reflect the new data.
    """
    try:
        observation_data = {
            "observation_type": observation_type,
            "category": category,
            "key": key,
            "value": value,
            "context": context,
        }
        body = {
            "observation": json.dumps(observation_data),
            "source_agent": "nova-agent",
            "source_action": "voice_conversation",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/pic/learn",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    obs_id = data.get("observation", {}).get("id", "?")
                    logger.info(f"PCG observation recorded [{obs_id}]: {category}/{key}={value}")
                    _cache.invalidate()
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"PCG learn rejected: HTTP {resp.status} {text[:100]}")
                    return False
    except Exception as e:
        logger.warning(f"PCG learn failed: {e}")
        return False


async def create_preference(
    category: str,
    key: str,
    value: str,
    context: str = "",
    source: str = "explicit",
) -> bool:
    """Create or update a preference directly in PCG.
    Invalidates the local cache on success.
    """
    try:
        body = {
            "category": category,
            "key": key,
            "value": value,
            "context": context,
            "source": source,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/pic/preferences",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"PCG preference saved: {category}/{key}={value}")
                    _cache.invalidate()
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"PCG preference write failed: HTTP {resp.status} {text[:100]}")
                    return False
    except Exception as e:
        logger.warning(f"PCG preference write failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Knowledge graph queries
# ---------------------------------------------------------------------------

async def search_knowledge(query: str, entity_types: Optional[list[str]] = None) -> list[dict]:
    """Search the knowledge graph for entities matching a query."""
    try:
        body = {"query": query}
        if entity_types:
            body["entity_types"] = entity_types
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/kg/search",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("results", [])
    except Exception as e:
        logger.warning(f"PCG knowledge search failed: {e}")
        return []


async def query_knowledge_graph(query: str) -> str:
    """Natural language query against the knowledge graph."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/kg/nl-query",
                headers=_admin_headers(),
                json={"query": query},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                return data.get("answer", data.get("response", ""))
    except Exception as e:
        logger.warning(f"PCG kg query failed: {e}")
        return ""


async def get_entity(entity_id: str) -> Optional[dict]:
    """Get a specific entity from the knowledge graph."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/kg/entities/{entity_id}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:
        logger.warning(f"PCG entity fetch failed: {e}")
        return None


async def get_neighbors(entity_id: str) -> list[dict]:
    """Get neighboring entities in the knowledge graph."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/kg/neighbors/{entity_id}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("neighbors", [])
    except Exception as e:
        logger.warning(f"PCG neighbors fetch failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Unified query (personal + knowledge + frameworks)
# ---------------------------------------------------------------------------

async def query(
    query: str,
    include_personal: bool = True,
    include_knowledge: bool = True,
    include_frameworks: bool = True,
) -> dict[str, Any]:
    """
    Unified query across all PCG data: personal, knowledge graph, and LIAM frameworks.

    """
    results: dict[str, Any] = {
        "query": query,
        "personal": [],
        "knowledge": [],
        "frameworks": [],
        "synthesis": "",
    }

    # Personal context
    if include_personal:
        prefs = await get_preferences()
        query_lower = query.lower()
        query_words = set(query_lower.split())

        # Synonym expansion
        _SYNONYMS = {
            "kids": ["family", "son", "daughter", "child"],
            "children": ["family", "son", "daughter", "child"],
            "family": ["son", "daughter", "wife", "child", "kids"],
            "coffee": ["starbucks", "espresso", "latte", "roast"],
            "food": ["burger", "starbucks", "restaurant", "meal", "order"],
            "home": ["location", "address", "zip", "77346"],
            "work": ["meeting", "schedule", "office", "job"],
        }
        expanded_words = set(query_words)
        for word in query_words:
            if word in _SYNONYMS:
                expanded_words.update(_SYNONYMS[word])

        for p in prefs:
            val = p.get("value", "")
            key = p.get("key", "")
            cat = p.get("category", "")
            searchable = f"{key} {val} {cat}".lower()
            if any(word in searchable for word in expanded_words):
                results["personal"].append(p)

        # Identity terms
        identity_terms = {"name", "role", "roles", "who", "timezone", "bio", "about", "me"}
        if identity_terms & expanded_words:
            identity = await get_identity()
            if identity:
                ident = identity.get("identity", {})
                results["personal"].append({
                    "category": "identity",
                    "key": "name",
                    "value": ident.get("preferred_name", "unknown"),
                })
                if ident.get("roles"):
                    results["personal"].append({
                        "category": "identity",
                        "key": "roles",
                        "value": ", ".join(ident["roles"]),
                    })

    # Knowledge graph
    if include_knowledge:
        kg_results = await search_knowledge(query)
        results["knowledge"] = kg_results[:10]

    # LIAM frameworks
    if include_frameworks:
        try:
            from nova.liam import query_frameworks as liam_query_frameworks
            fw_result = await liam_query_frameworks(query, limit=3)
            fw_list = fw_result.get("frameworks", [])
            # Extract just the framework content for synthesis
            results["frameworks"] = [
                {
                    "name": rec.get("framework", {}).get("name", rec.get("framework_name", "")),
                    "description": rec.get("framework", {}).get("description", ""),
                    "when_to_use": rec.get("framework", {}).get("when_to_use", ""),
                    "key_concepts": rec.get("framework", {}).get("key_concepts", []),
                }
                for rec in fw_list
                if rec.get("framework")
            ]
        except Exception as e:
            logger.debug(f"PCG LIAM query skipped: {e}")

    # Build synthesis
    parts = []
    if results["personal"]:
        parts.append("Personal context:")
        for p in results["personal"][:5]:
            parts.append(f"  - {p.get('key', '?')}: {p.get('value', '')[:100]}")
    if results["knowledge"]:
        parts.append("Knowledge graph:")
        for k in results["knowledge"][:5]:
            parts.append(f"  - {k.get('name', k.get('id', '?'))}: {k.get('type', '')}")
    if results["frameworks"]:
        parts.append("Applicable frameworks:")
        for f in results["frameworks"][:3]:
            parts.append(f"  - {f.get('name', '?')}: {f.get('when_to_use', f.get('description', ''))[:80]}")
    results["synthesis"] = "\n".join(parts)

    return results


# ---------------------------------------------------------------------------
# Build full PCG context for Nova's system prompt
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PCG v2 methods — /api/pcg/* namespace (Phase A6)
# ---------------------------------------------------------------------------
# The four endpoints added in services/personal-kg Phase A5 that power email
# personalization + workflow audit + daily-insight retrieval. See
# services/personal-kg/AGENTIFICATION_PLAN.md.


async def get_contact_context(
    email: str,
    email_limit: int = 10,
    observation_limit: int = 10,
    topic_limit: int = 5,
) -> Optional[dict[str, Any]]:
    """Fetch single-traversal contact context (Identity + scoped prefs +
    communication styles + recent emails/topics from CIG) for email
    personalization. Powers the Hermes draft prompt in Phase B2.

    Returns None on failure. Caller should treat a None/empty response as
    "no PCG context available" and fall back to generic prompting.
    """
    try:
        params = {
            "email": email,
            "email_limit": email_limit,
            "observation_limit": observation_limit,
            "topic_limit": topic_limit,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pcg/contact-context",
                headers=_read_headers(),
                params=params,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"PCG contact-context failed: HTTP {resp.status}"
                    )
                    return None
                return await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PCG contact-context unavailable: {e}")
        return None


async def record_workflow_run(
    run_id: str,
    workflow_name: str,
    status: str = "running",
    triggered_by: Optional[str] = None,
    dry_run: bool = False,
    inputs_summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Register a Hatchet workflow run in PCG's audit graph (:WorkflowRun
    node). Called at workflow start. Idempotent on run_id."""
    try:
        body = {
            "run_id": run_id,
            "workflow_name": workflow_name,
            "status": status,
            "triggered_by": triggered_by,
            "dry_run": dry_run,
            "inputs_summary": inputs_summary,
            "metadata": metadata or {},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/pcg/workflow-run",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                return resp.status in (200, 201)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PCG workflow-run create failed: {e}")
        return False


async def update_workflow_run(
    run_id: str,
    status: str,
    outputs_summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Terminal update (succeeded/failed/cancelled) on an existing run.
    Called at workflow completion."""
    try:
        body = {
            "status": status,
            "outputs_summary": outputs_summary,
            "error": error,
            "metadata": metadata or {},
        }
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{PCG_URL}/api/pcg/workflow-run/{run_id}",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                return resp.status == 200
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PCG workflow-run update failed: {e}")
        return False


async def upsert_communication_style(
    scope: str,
    tone: Optional[str] = None,
    length: Optional[str] = None,
    greeting: Optional[str] = None,
    signoff: Optional[str] = None,
    description: Optional[str] = None,
    confidence: float = 1.0,
    source: str = "explicit",
) -> Optional[dict]:
    """Create/update a communication style (per-scope tone/voice). `scope`
    must be 'global', 'contact:<email>', or 'topic:<name>'."""
    try:
        body = {
            "scope": scope,
            "tone": tone,
            "length": length,
            "greeting": greeting,
            "signoff": signoff,
            "description": description,
            "confidence": confidence,
            "source": source,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/pcg/communication-style",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    return None
                return await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PCG communication-style upsert failed: {e}")
        return None


async def get_recent_insights(limit: int = 10) -> list[dict]:
    """Fetch recent daily-consolidation insights (Phase C1 output). Used by
    the Nova skill `pcg_insights` (Phase C3) to answer 'what did PCG learn
    about me yesterday?'."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/pcg/insights/recent?limit={limit}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("insights", [])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PCG insights unavailable: {e}")
        return []


async def build_daily_snapshot(home_address: str = "", user_tz: str = "America/Chicago") -> dict[str, Any]:
    """Fetch real-time context for the Daily Snapshot block in the system prompt.

    Runs all fetches concurrently with tight timeouts so session start is not
    materially delayed.  Each sub-fetch swallows its own errors gracefully.

    Returns a dict with optional keys:
      weather, calendar_briefing, tesla_charge, tesla_location,
      current_day, family_schedule, gps_city
    """
    import asyncio
    from datetime import datetime, timezone
    import zoneinfo

    now_utc = datetime.now(timezone.utc)
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
        now_local = now_utc.astimezone(tz)
    except Exception:
        now_local = now_utc
    weekday = now_local.strftime("%A")
    date_str = now_local.strftime("%Y-%m-%d")

    snapshot: dict[str, Any] = {
        "current_day": weekday,
        "current_date": date_str,
        "current_time": now_local.strftime("%H:%M"),
    }

    # ── Family schedule (static, but day-dependent) ───────────────────────
    FAMILY_SCHEDULE = {
        "Tuesday":  ["Sofia: karate after school"],
        "Thursday": ["Sofia: karate after school"],
    }
    if weekday in FAMILY_SCHEDULE:
        snapshot["family_schedule"] = FAMILY_SCHEDULE[weekday]

    async def _fetch_weather():
        if not home_address:
            return
        try:
            from nova.tools import handle_get_weather
            result = await asyncio.wait_for(handle_get_weather(home_address), timeout=6)
            if isinstance(result, dict):
                speakable = result.get("speakable", "")
            else:
                speakable = str(result)
            if speakable:
                snapshot["weather"] = speakable
        except Exception as e:
            logger.debug(f"Daily snapshot weather fetch failed: {e}")

    async def _fetch_calendar():
        try:
            from nova.tools import handle_check_studio
            result = await asyncio.wait_for(
                handle_check_studio(studio="calendar", action="briefing", query=date_str),
                timeout=10,
            )
            if result and "error" not in str(result).lower()[:30]:
                snapshot["calendar_briefing"] = str(result)[:600]
        except Exception as e:
            logger.debug(f"Daily snapshot calendar fetch failed: {e}")

    async def _fetch_tesla():
        try:
            from nova.tools import handle_tesla_control
            result = await asyncio.wait_for(
                handle_tesla_control(action="state"),
                timeout=8,
            )
            if isinstance(result, dict):
                charge = result.get("charge_state", {})
                if charge:
                    pct = charge.get("battery_level")
                    range_mi = charge.get("est_battery_range")
                    if pct is not None:
                        snapshot["tesla_charge"] = f"{pct}% battery ({range_mi:.0f} mi range)" if range_mi else f"{pct}% battery"
                drive = result.get("drive_state", {})
                if drive:
                    lat = drive.get("latitude")
                    lon = drive.get("longitude")
                    if lat and lon:
                        snapshot["tesla_location"] = f"{lat:.4f},{lon:.4f}"
            elif result:
                snapshot["tesla_state_raw"] = str(result)[:200]
        except Exception as e:
            logger.debug(f"Daily snapshot Tesla fetch failed: {e}")

    await asyncio.gather(
        _fetch_weather(),
        _fetch_calendar(),
        _fetch_tesla(),
        return_exceptions=True,
    )

    logger.info(f"Daily snapshot built: {list(snapshot.keys())}")
    return snapshot


async def build_context(user_id: str) -> dict[str, Any]:
    """Fetch PCG data and structure it for Nova's system prompt.

    Returns a rich dict with:
      - user_name, user_timezone (for prompt header)
      - preferences_by_category: dict[str, list[dict]] (for personality shaping)
      - memory_snippets: list[str] (for ## Memory section)
      - identity: raw identity dict
    """
    import asyncio as _asyncio

    _cache.invalidate()  # fresh start for new session

    # Run cache warm + daily snapshot concurrently to minimise session-start latency
    await _ensure_cache()

    identity_data = _cache.identity
    prefs = _cache.preferences
    goals = _cache.goals

    result: dict[str, Any] = {
        "user_name": None,
        "user_timezone": "America/Chicago",
        "identity": {},
        "preferences_by_category": {},
        "memory_snippets": [],
        "daily_snapshot": {},
    }

    # Identity — PIC /api/pic/identity returns fields at the TOP LEVEL
    # (name, bio, timezone, roles, metadata). Do NOT wrap in .get("identity", {}).
    home_address = ""
    user_tz = "America/Chicago"
    if identity_data and not identity_data.get("message"):
        ident = identity_data
        metadata = ident.get("metadata") or {}
        result["identity"] = ident
        result["user_name"] = metadata.get("preferred_name") or ident.get("name")
        user_tz = ident.get("timezone", "America/Chicago")
        result["user_timezone"] = user_tz

        # Pull high-value profile data into memory snippets so Nova
        # baseline-knows who you are, where you live, and what you do —
        # without needing to call recall_memory for it.
        bio = ident.get("bio", "")
        if bio:
            result["memory_snippets"].append(f"User profile: {bio}")

        roles = ident.get("roles", [])
        if roles:
            result["memory_snippets"].append(f"User roles: {', '.join(roles)}")

        home_address = metadata.get("home_address", "")
        if home_address:
            result["memory_snippets"].append(f"User home address: {home_address}")

        full_name = metadata.get("full_name") or ident.get("name")
        if full_name:
            result["memory_snippets"].append(f"User full name: {full_name}")

    # Daily snapshot — runs after identity so we have home_address + timezone
    try:
        snapshot = await _asyncio.wait_for(
            build_daily_snapshot(home_address=home_address, user_tz=user_tz),
            timeout=12,
        )
        result["daily_snapshot"] = snapshot
    except Exception as _e:
        logger.warning(f"Daily snapshot timed out or failed: {_e}")

    # Preferences — indexed by category for prompt builder to use
    by_cat: dict[str, list[dict]] = {}
    for p in prefs:
        cat = p.get("category", "other")
        by_cat.setdefault(cat, []).append(p)
        val = p.get("value", "")
        key = p.get("key", "")
        if val:
            result["memory_snippets"].append(f"Preference ({cat}/{key}): {val[:150]}")
    result["preferences_by_category"] = by_cat

    # Goals
    for g in goals[:5]:
        title = g.get("title", "")
        desc = g.get("description", "")
        if title:
            snippet = f"Active goal: {title}"
            if desc:
                snippet += f" — {desc[:100]}"
            result["memory_snippets"].append(snippet)

    logger.info(
        f"PCG context for {user_id}: {len(prefs)} prefs, "
        f"{len(goals)} goals, {len(result['memory_snippets'])} snippets, "
        f"snapshot_keys={list(result['daily_snapshot'].keys())}"
    )
    return result

