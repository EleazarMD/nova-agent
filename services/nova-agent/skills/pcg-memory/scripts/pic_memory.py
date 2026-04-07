"""
PIC (Personal Identity Core) client for Nova Agent.

PIC is the **single source of truth** for personal data across all homelab agents.
Architecture: Option B — Nova has direct bidirectional access to PIC. OpenClaw
consumes PIC data (read-only MEMORY.md rendered from PIC) and writes discoveries
back through PIC's observation API.

Data flow:
  Session start → build_pic_context() → system prompt (cached in-process)
  Mid-session   → get_preferences() / get_identity() (from cache)
  User states   → record_observation() → PIC → cache invalidated
  OpenClaw task → POST /api/pic/learn → PIC (write-through)

Backed by: Neo4j (graph) + Redis (cache) at :8765
"""

import os
import time
import aiohttp
import json
from typing import Any, Optional
from loguru import logger

PIC_URL = os.environ.get("PIC_URL", "http://localhost:8765")
PIC_READ_KEY = os.environ.get("PIC_READ_KEY", "dev-read-key-change-in-prod")
PIC_ADMIN_KEY = os.environ.get("PIC_ADMIN_KEY", "dev-admin-key-change-in-prod")

_TIMEOUT = aiohttp.ClientTimeout(total=5)


# ---------------------------------------------------------------------------
# Session-scoped in-process cache
# ---------------------------------------------------------------------------
# Avoids repeated HTTP calls to PIC within a single Pipecat session.
# Invalidated when Nova writes (save_memory), so mid-session reads
# always reflect the latest state.

class _PICCache:
    """Simple in-process cache for PIC data within a Nova session."""

    def __init__(self):
        self._identity: Optional[dict] = None
        self._preferences: Optional[list[dict]] = None
        self._goals: Optional[list[dict]] = None
        self._loaded_at: float = 0.0

    def is_warm(self) -> bool:
        return self._loaded_at > 0

    def invalidate(self):
        """Clear cache — called after writes so next read hits PIC."""
        self._identity = None
        self._preferences = None
        self._goals = None
        self._loaded_at = 0.0
        logger.debug("PIC cache invalidated")

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


_cache = _PICCache()


# ---------------------------------------------------------------------------
# PIC Read (identity, preferences, goals)
# ---------------------------------------------------------------------------

def _read_headers() -> dict[str, str]:
    return {"X-PIC-Read-Key": PIC_READ_KEY}


async def _fetch_identity() -> Optional[dict[str, Any]]:
    """Fetch user identity profile from PIC (HTTP)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PIC_URL}/api/pic/identity",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"PIC identity fetch failed: HTTP {resp.status}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.warning(f"PIC identity unavailable: {e}")
        return None


async def _fetch_preferences() -> list[dict]:
    """Fetch all user preferences from PIC (HTTP)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PIC_URL}/api/pic/preferences",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("preferences", [])
    except Exception as e:
        logger.warning(f"PIC preferences unavailable: {e}")
        return []


async def _fetch_goals(status: str = "active") -> list[dict]:
    """Fetch user goals from PIC (HTTP)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PIC_URL}/api/pic/goals?status={status}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("goals", [])
    except Exception as e:
        logger.warning(f"PIC goals unavailable: {e}")
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
    logger.info(f"PIC cache warmed: {len(preferences)} prefs, {len(goals)} goals ({elapsed:.0f}ms)")


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
# PIC Write (observations + preferences — invalidates cache)
# ---------------------------------------------------------------------------

def _admin_headers() -> dict[str, str]:
    return {
        "X-PIC-Admin-Key": PIC_ADMIN_KEY,
        "Content-Type": "application/json",
    }


async def record_observation(
    observation_type: str,
    category: str,
    key: str,
    value: str,
    context: str = "",
) -> bool:
    """Record a learning observation directly to PIC.
    
    PIC stores it in Neo4j as an :Observation node. Observations are
    consolidated into preferences when enough corroborating data exists.
    Invalidates the local cache so subsequent reads reflect the new data.
    """
    try:
        # PIC API expects observation as a JSON-encoded string
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
                f"{PIC_URL}/api/pic/learn",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    obs_id = data.get("observation", {}).get("id", "?")
                    logger.info(f"PIC observation recorded [{obs_id}]: {category}/{key}={value}")
                    _cache.invalidate()  # next read will fetch fresh data
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"PIC learn rejected: HTTP {resp.status} {text[:100]}")
                    return False
    except Exception as e:
        logger.warning(f"PIC learn failed: {e}")
        return False


async def create_preference(
    category: str,
    key: str,
    value: str,
    context: str = "",
    source: str = "explicit",
) -> bool:
    """Create or update a preference directly in PIC.
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
                f"{PIC_URL}/api/pic/preferences",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    logger.info(f"PIC preference saved: {category}/{key}={value}")
                    _cache.invalidate()
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"PIC preference write failed: HTTP {resp.status} {text[:100]}")
                    return False
    except Exception as e:
        logger.warning(f"PIC preference write failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Build full PIC context for Nova's system prompt
# ---------------------------------------------------------------------------

async def build_pic_context(user_id: str) -> dict[str, Any]:
    """Fetch PIC data and structure it for Nova's system prompt.
    
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
        f"PIC context for {user_id}: {len(prefs)} prefs, "
        f"{len(goals)} goals, {len(result['memory_snippets'])} snippets"
    )
    return result
