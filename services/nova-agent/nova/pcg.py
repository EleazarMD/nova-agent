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
  OpenClaw task → POST /api/pcg/learn → PCG (write-through)
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
# Knowledge graph queries (formerly KG-API)
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
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{PCG_URL}/api/liam/query/dimensions",
                    headers=_admin_headers(),
                    json={"query": query},
                    timeout=_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results["frameworks"] = data.get("dimensions", [])[:5]
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
            parts.append(f"  - {f.get('name', f.get('id', '?'))}")
    results["synthesis"] = "\n".join(parts)

    return results


# ---------------------------------------------------------------------------
# Build full PCG context for Nova's system prompt
# ---------------------------------------------------------------------------

async def build_context(user_id: str) -> dict[str, Any]:
    """Fetch PCG data and structure it for Nova's system prompt.

    Returns a rich dict with:
      - user_name, user_timezone (for prompt header)
      - preferences_by_category: dict[str, list[dict]] (for personality shaping)
      - memory_snippets: list[str] (for ## Memory section)
      - identity: raw identity dict
    """
    _cache.invalidate()  # fresh start for new session

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
    }

    # Identity
    if identity_data:
        ident = identity_data.get("identity", {})
        result["identity"] = ident
        result["user_name"] = ident.get("preferred_name") or ident.get("name")
        result["user_timezone"] = ident.get("timezone", "America/Chicago")

        bio = ident.get("bio", "")
        if bio:
            result["memory_snippets"].append(f"User profile: {bio}")

        roles = ident.get("roles", [])
        if roles:
            result["memory_snippets"].append(f"User roles: {', '.join(roles)}")

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
        f"{len(goals)} goals, {len(result['memory_snippets'])} snippets"
    )
    return result

