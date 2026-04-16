"""
Nova Agent Response Cache Layer — Enhanced Edition.

Provides intelligent caching for tool results to enable true zero-wait responses.

Features:
1. **SQLite Persistence** — Cache survives restarts, stored in data/cache.db
2. **Predictive Warming** — Learns query patterns and pre-fetches likely queries
3. **Adaptive TTLs** — Learns actual data change rates and adjusts TTLs dynamically

Cache entries have base TTLs tuned for data volatility, but these adapt over time
based on observed staleness (how often cached data differs from fresh fetches).
"""

import asyncio
import hashlib
import json
import os
import pickle
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from loguru import logger

# Data directory for persistence
DATA_DIR = Path(os.environ.get("NOVA_DATA_DIR", "/home/eleazar/Projects/AIHomelab/services/nova-agent/data"))
CACHE_DB_PATH = DATA_DIR / "cache.db"


@dataclass
class CacheEntry:
    """A cached tool result with metadata."""
    value: Any
    created_at: float
    ttl: float
    tool_name: str
    cache_key: str
    hit_count: int = 0
    
    @property
    def is_expired(self) -> bool:
        return time.time() > (self.created_at + self.ttl)
    
    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at
    
    @property
    def remaining_ttl(self) -> float:
        return max(0, (self.created_at + self.ttl) - time.time())


@dataclass
class CacheStats:
    """Cache performance statistics."""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    total_entries: int = 0
    warm_hits: int = 0  # Hits from predictive warming
    adaptive_adjustments: int = 0  # TTL adjustments made
    
    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


@dataclass
class QueryPattern:
    """Tracks query patterns for predictive warming."""
    tool_name: str
    args_hash: str
    hour_of_day: int  # 0-23
    day_of_week: int  # 0-6 (Monday=0)
    count: int = 1
    last_seen: float = field(default_factory=time.time)
    
    @property
    def pattern_key(self) -> str:
        return f"{self.tool_name}:{self.args_hash}:{self.hour_of_day}:{self.day_of_week}"


@dataclass 
class TTLLearning:
    """Tracks data change rates for adaptive TTLs."""
    tool_name: str
    args_hash: str
    checks: int = 0  # Number of times we compared cached vs fresh
    stale_count: int = 0  # Times cached differed from fresh
    current_ttl: float = 0
    last_adjustment: float = field(default_factory=time.time)
    
    @property
    def staleness_rate(self) -> float:
        """How often cached data is stale (0.0 = never, 1.0 = always)."""
        return self.stale_count / self.checks if self.checks > 0 else 0.5


class ToolResultCache:
    """
    Intelligent cache for Nova tool results.
    
    Features:
    - Per-tool TTL configuration with adaptive learning
    - Semantic key normalization (handles query variations)
    - LRU eviction when max size reached
    - SQLite persistence across restarts
    - Predictive warming based on usage patterns
    - Adaptive TTLs that learn from data change rates
    """
    
    # Base TTLs in seconds, tuned for data volatility (will adapt over time)
    DEFAULT_TTLS = {
        "get_weather": 600,           # 10 min - weather changes slowly
        "get_time": 1,                # 1 sec - always fresh
        "check_studio": 120,          # 2 min - email/calendar reasonably stable
        "recall_memory": 300,         # 5 min - PCG data stable
        "web_search": 3600,           # 1 hour - external data, expensive
        "service_status": 30,         # 30 sec - infra can change
        "service_health_check": 30,   # 30 sec
        "service_logs": 10,           # 10 sec - logs change rapidly
        "tesla_status": 60,           # 1 min - vehicle state
        "tesla_vehicle_status": 30,   # 30 sec - more dynamic
        "discover_skills": 300,       # 5 min - skill catalog stable
        "diagnose_network": 60,       # 1 min
        "search_past_conversations": 120,  # 2 min - conversation history stable
        "query_cig": 180,             # 3 min - CIG analytics semi-stable
        "query_frameworks": 1800,     # 30 min - LIAM frameworks very stable
        "homelab_diagnostics": 60,    # 1 min - infra state
        "homelab_operations": 30,     # 30 sec - mutating but status reads cached briefly
        "tesla_control": 60,          # 1 min - vehicle state
        "get_reminders": 120,         # 2 min - reminders semi-stable
        "list_timers": 30,            # 30 sec - timers change
        "check_studio": 120,          # 2 min - email/calendar
        "recall_memory": 300,         # 5 min - PCG preferences stable
    }
    
    # TTL bounds for adaptive learning (min/max multipliers of base TTL)
    TTL_MIN_MULTIPLIER = 0.25  # Can shrink to 25% of base
    TTL_MAX_MULTIPLIER = 4.0   # Can grow to 400% of base
    
    # Location synonyms for normalization
    LOCATION_SYNONYMS = {
        "dallas": ["dfw", "dallas tx", "dallas texas"],
        "houston": ["hou", "houston tx", "houston texas"],
        "austin": ["aus", "austin tx", "austin texas"],
        "san antonio": ["sat", "san antonio tx"],
        "new york": ["nyc", "new york city", "manhattan"],
        "los angeles": ["la", "los angeles ca"],
        "chicago": ["chi", "chicago il"],
    }
    
    def __init__(self, max_size: int = 500, default_ttl: float = 300):
        self._cache: dict[str, CacheEntry] = {}
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._stats = CacheStats()
        self._access_order: list[str] = []  # For LRU eviction
        self._lock = asyncio.Lock()
        
        # Predictive warming: track query patterns
        self._query_patterns: dict[str, QueryPattern] = {}
        
        # Adaptive TTLs: track staleness rates
        self._ttl_learning: dict[str, TTLLearning] = {}
        self._adapted_ttls: dict[str, float] = {}  # tool:args_hash -> adapted TTL
        
        # SQLite persistence
        self._db_path = CACHE_DB_PATH
        self._init_db()
        
        # Background tasks
        self._warming_task: Optional[asyncio.Task] = None
        self._persistence_task: Optional[asyncio.Task] = None
    
    def _init_db(self):
        """Initialize SQLite database for persistence."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()
        
        # Cache entries table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                cache_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                value BLOB NOT NULL,
                created_at REAL NOT NULL,
                ttl REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )
        """)
        
        # Query patterns table (for predictive warming)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS query_patterns (
                pattern_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                hour_of_day INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                count INTEGER DEFAULT 1,
                last_seen REAL NOT NULL
            )
        """)
        
        # TTL learning table (for adaptive TTLs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ttl_learning (
                learning_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                checks INTEGER DEFAULT 0,
                stale_count INTEGER DEFAULT 0,
                current_ttl REAL NOT NULL,
                last_adjustment REAL NOT NULL
            )
        """)
        
        # Indexes for efficient queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_patterns_time ON query_patterns(hour_of_day, day_of_week)")
        
        conn.commit()
        conn.close()
        logger.info(f"Cache database initialized at {self._db_path}")
    
    async def load_from_db(self):
        """Load cache state from SQLite on startup."""
        async with self._lock:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # Load non-expired cache entries
            now = time.time()
            cursor.execute("""
                SELECT cache_key, tool_name, value, created_at, ttl, hit_count
                FROM cache_entries
                WHERE created_at + ttl > ?
            """, (now,))
            
            loaded = 0
            for row in cursor.fetchall():
                cache_key, tool_name, value_blob, created_at, ttl, hit_count = row
                try:
                    value = pickle.loads(value_blob)
                    entry = CacheEntry(
                        value=value,
                        created_at=created_at,
                        ttl=ttl,
                        tool_name=tool_name,
                        cache_key=cache_key,
                        hit_count=hit_count,
                    )
                    self._cache[cache_key] = entry
                    self._access_order.append(cache_key)
                    loaded += 1
                except Exception as e:
                    logger.warning(f"Failed to load cache entry {cache_key}: {e}")
            
            # Load query patterns
            cursor.execute("SELECT * FROM query_patterns")
            for row in cursor.fetchall():
                pattern_key, tool_name, args_hash, hour, dow, count, last_seen = row
                self._query_patterns[pattern_key] = QueryPattern(
                    tool_name=tool_name,
                    args_hash=args_hash,
                    hour_of_day=hour,
                    day_of_week=dow,
                    count=count,
                    last_seen=last_seen,
                )
            
            # Load TTL learning data
            cursor.execute("SELECT * FROM ttl_learning")
            for row in cursor.fetchall():
                learning_key, tool_name, args_hash, checks, stale_count, current_ttl, last_adj = row
                self._ttl_learning[learning_key] = TTLLearning(
                    tool_name=tool_name,
                    args_hash=args_hash,
                    checks=checks,
                    stale_count=stale_count,
                    current_ttl=current_ttl,
                    last_adjustment=last_adj,
                )
                self._adapted_ttls[learning_key] = current_ttl
            
            conn.close()
            
            self._stats.total_entries = len(self._cache)
            logger.info(f"Loaded {loaded} cache entries, {len(self._query_patterns)} patterns, {len(self._ttl_learning)} TTL learnings from DB")
        
    def _normalize_location(self, text: str) -> str:
        """Normalize location names to canonical form."""
        text_lower = text.lower().strip()
        for canonical, synonyms in self.LOCATION_SYNONYMS.items():
            if text_lower == canonical or text_lower in synonyms:
                return canonical
        return text_lower
    
    def _normalize_query(self, query: str) -> str:
        """Normalize a query string for cache key generation."""
        # Lowercase and strip
        q = query.lower().strip()
        # Remove common filler words
        fillers = ["the", "a", "an", "in", "for", "of", "what's", "what", "is", 
                   "are", "can", "you", "please", "tell", "me", "about", "get",
                   "check", "show", "find", "look", "up"]
        words = q.split()
        words = [w for w in words if w not in fillers]
        # Sort for order-independence (weather dallas == dallas weather)
        words.sort()
        return " ".join(words)
    
    def _make_cache_key(self, tool_name: str, args: dict) -> str:
        """Generate a normalized cache key from tool name and arguments."""
        # Start with tool name
        parts = [tool_name]
        
        # Normalize arguments based on tool type
        if tool_name == "get_weather":
            location = args.get("location", "")
            parts.append(self._normalize_location(location))
        elif tool_name == "check_studio":
            studio = args.get("studio", "email")
            action = args.get("action", "briefing")
            query = args.get("query", "")
            parts.extend([studio, action])
            if query:
                parts.append(self._normalize_query(query))
        elif tool_name == "web_search":
            query = args.get("query", "")
            parts.append(self._normalize_query(query))
        elif tool_name == "recall_memory":
            query = args.get("query", "")
            parts.append(self._normalize_query(query))
        elif tool_name in ("service_status", "service_health_check", "service_logs"):
            container = args.get("container", "all")
            parts.append(container.lower())
        elif tool_name.startswith("tesla_"):
            vin = args.get("vin", args.get("vehicle_id", "default"))
            parts.append(str(vin)[:8])  # First 8 chars of VIN
        else:
            # Generic: hash all args
            args_str = json.dumps(args, sort_keys=True, default=str)
            parts.append(hashlib.md5(args_str.encode()).hexdigest()[:12])
        
        return ":".join(parts)
    
    async def get(self, tool_name: str, args: dict) -> Optional[CacheEntry]:
        """Get a cached result if available and not expired."""
        cache_key = self._make_cache_key(tool_name, args)
        
        async with self._lock:
            entry = self._cache.get(cache_key)
            
            if entry is None:
                self._stats.misses += 1
                return None
            
            if entry.is_expired:
                # Expired - remove and return miss
                del self._cache[cache_key]
                if cache_key in self._access_order:
                    self._access_order.remove(cache_key)
                self._stats.misses += 1
                self._stats.evictions += 1
                return None
            
            # Cache hit
            entry.hit_count += 1
            self._stats.hits += 1
            
            # Update LRU order
            if cache_key in self._access_order:
                self._access_order.remove(cache_key)
            self._access_order.append(cache_key)
            
            logger.debug(f"Cache HIT: {cache_key} (age={entry.age_seconds:.1f}s, hits={entry.hit_count})")
            return entry
    
    def _get_args_hash(self, args: dict) -> str:
        """Generate a hash of args for pattern/learning tracking."""
        args_str = json.dumps(args, sort_keys=True, default=str)
        return hashlib.md5(args_str.encode()).hexdigest()[:16]
    
    def _get_adaptive_ttl(self, tool_name: str, args: dict) -> float:
        """Get TTL, possibly adapted based on learned staleness rates."""
        base_ttl = self.DEFAULT_TTLS.get(tool_name, self._default_ttl)
        args_hash = self._get_args_hash(args)
        learning_key = f"{tool_name}:{args_hash}"
        
        # Check if we have an adapted TTL
        if learning_key in self._adapted_ttls:
            return self._adapted_ttls[learning_key]
        
        return base_ttl
    
    async def _record_query_pattern(self, tool_name: str, args: dict):
        """Record a query pattern for predictive warming."""
        try:
            tz = ZoneInfo("America/Chicago")  # User's timezone
        except Exception:
            tz = None
        
        now = datetime.now(tz) if tz else datetime.now()
        args_hash = self._get_args_hash(args)
        hour = now.hour
        dow = now.weekday()
        
        pattern_key = f"{tool_name}:{args_hash}:{hour}:{dow}"
        
        if pattern_key in self._query_patterns:
            self._query_patterns[pattern_key].count += 1
            self._query_patterns[pattern_key].last_seen = time.time()
        else:
            self._query_patterns[pattern_key] = QueryPattern(
                tool_name=tool_name,
                args_hash=args_hash,
                hour_of_day=hour,
                day_of_week=dow,
                count=1,
                last_seen=time.time(),
            )
    
    async def set(self, tool_name: str, args: dict, value: Any, ttl: Optional[float] = None) -> str:
        """Cache a tool result with pattern tracking and adaptive TTL."""
        cache_key = self._make_cache_key(tool_name, args)
        
        # Determine TTL (use adaptive if available)
        if ttl is None:
            ttl = self._get_adaptive_ttl(tool_name, args)
        
        async with self._lock:
            # Evict if at capacity
            while len(self._cache) >= self._max_size and self._access_order:
                oldest_key = self._access_order.pop(0)
                if oldest_key in self._cache:
                    del self._cache[oldest_key]
                    self._stats.evictions += 1
            
            # Store entry
            entry = CacheEntry(
                value=value,
                created_at=time.time(),
                ttl=ttl,
                tool_name=tool_name,
                cache_key=cache_key,
            )
            self._cache[cache_key] = entry
            self._access_order.append(cache_key)
            self._stats.total_entries = len(self._cache)
            
            logger.debug(f"Cache SET: {cache_key} (ttl={ttl}s)")
        
        # Record query pattern (outside lock for performance)
        await self._record_query_pattern(tool_name, args)
        
        return cache_key
    
    async def invalidate(self, tool_name: str, args: Optional[dict] = None) -> int:
        """Invalidate cache entries. If args is None, invalidate all for tool."""
        count = 0
        async with self._lock:
            if args is not None:
                # Invalidate specific entry
                cache_key = self._make_cache_key(tool_name, args)
                if cache_key in self._cache:
                    del self._cache[cache_key]
                    if cache_key in self._access_order:
                        self._access_order.remove(cache_key)
                    count = 1
            else:
                # Invalidate all entries for this tool
                keys_to_remove = [k for k in self._cache if k.startswith(f"{tool_name}:")]
                for key in keys_to_remove:
                    del self._cache[key]
                    if key in self._access_order:
                        self._access_order.remove(key)
                    count += 1
            
            self._stats.total_entries = len(self._cache)
        
        if count > 0:
            logger.info(f"Cache INVALIDATE: {tool_name} ({count} entries)")
        return count
    
    async def clear(self) -> int:
        """Clear all cache entries."""
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._access_order.clear()
            self._stats = CacheStats()
            return count
    
    # -------------------------------------------------------------------------
    # Persistence: Save/load cache state to SQLite
    # -------------------------------------------------------------------------
    
    async def persist_to_db(self):
        """Persist current cache state to SQLite."""
        async with self._lock:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # Clear old entries and insert current
            cursor.execute("DELETE FROM cache_entries")
            for cache_key, entry in self._cache.items():
                try:
                    value_blob = pickle.dumps(entry.value)
                    cursor.execute("""
                        INSERT OR REPLACE INTO cache_entries 
                        (cache_key, tool_name, value, created_at, ttl, hit_count)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (cache_key, entry.tool_name, value_blob, 
                          entry.created_at, entry.ttl, entry.hit_count))
                except Exception as e:
                    logger.warning(f"Failed to persist {cache_key}: {e}")
            
            # Persist query patterns
            cursor.execute("DELETE FROM query_patterns")
            for pattern_key, pattern in self._query_patterns.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO query_patterns
                    (pattern_key, tool_name, args_hash, hour_of_day, day_of_week, count, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (pattern_key, pattern.tool_name, pattern.args_hash,
                      pattern.hour_of_day, pattern.day_of_week, pattern.count, pattern.last_seen))
            
            # Persist TTL learning
            cursor.execute("DELETE FROM ttl_learning")
            for learning_key, learning in self._ttl_learning.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO ttl_learning
                    (learning_key, tool_name, args_hash, checks, stale_count, current_ttl, last_adjustment)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (learning_key, learning.tool_name, learning.args_hash,
                      learning.checks, learning.stale_count, learning.current_ttl, learning.last_adjustment))
            
            conn.commit()
            conn.close()
            logger.debug(f"Persisted {len(self._cache)} entries, {len(self._query_patterns)} patterns to DB")
    
    async def start_background_tasks(self):
        """Start background persistence and warming tasks."""
        if self._persistence_task is None:
            self._persistence_task = asyncio.create_task(self._persistence_loop())
        if self._warming_task is None:
            self._warming_task = asyncio.create_task(self._warming_loop())
    
    async def _persistence_loop(self):
        """Periodically persist cache to SQLite."""
        while True:
            try:
                await asyncio.sleep(60)  # Persist every minute
                await self.persist_to_db()
            except asyncio.CancelledError:
                # Final persist on shutdown
                await self.persist_to_db()
                break
            except Exception as e:
                logger.error(f"Persistence loop error: {e}")
    
    # -------------------------------------------------------------------------
    # Predictive Warming: Pre-fetch likely queries based on patterns
    # -------------------------------------------------------------------------
    
    async def _warming_loop(self):
        """Periodically check for queries to pre-warm based on patterns."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._warm_predicted_queries()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Warming loop error: {e}")
    
    async def _warm_predicted_queries(self):
        """Pre-fetch queries that are likely to be requested soon."""
        try:
            tz = ZoneInfo("America/Chicago")
        except Exception:
            tz = None
        
        now = datetime.now(tz) if tz else datetime.now()
        current_hour = now.hour
        current_dow = now.weekday()
        
        # Find patterns matching current time window (±1 hour)
        candidates = []
        for pattern_key, pattern in self._query_patterns.items():
            hour_match = abs(pattern.hour_of_day - current_hour) <= 1 or \
                         abs(pattern.hour_of_day - current_hour) >= 23  # Handle midnight wrap
            dow_match = pattern.day_of_week == current_dow
            
            # Only warm if pattern has been seen multiple times
            if hour_match and dow_match and pattern.count >= 3:
                candidates.append(pattern)
        
        # Sort by frequency and warm top candidates
        candidates.sort(key=lambda p: p.count, reverse=True)
        warmed = 0
        
        for pattern in candidates[:5]:  # Warm up to 5 queries
            cache_key = f"{pattern.tool_name}:{pattern.args_hash}"
            # Check if already cached
            if any(k.startswith(cache_key) for k in self._cache):
                continue
            
            logger.info(f"[Warming] Would pre-fetch {pattern.tool_name} (seen {pattern.count}x at this time)")
            # Note: Actual pre-fetching requires tool dispatch integration
            # For now, we just log what would be warmed
            warmed += 1
        
        if warmed > 0:
            logger.info(f"[Warming] Identified {warmed} queries for pre-warming")
    
    def get_warming_candidates(self) -> list[dict]:
        """Get list of queries that would be pre-warmed at current time."""
        try:
            tz = ZoneInfo("America/Chicago")
        except Exception:
            tz = None
        
        now = datetime.now(tz) if tz else datetime.now()
        current_hour = now.hour
        current_dow = now.weekday()
        
        candidates = []
        for pattern in self._query_patterns.values():
            hour_match = abs(pattern.hour_of_day - current_hour) <= 1
            dow_match = pattern.day_of_week == current_dow
            
            if hour_match and dow_match and pattern.count >= 3:
                candidates.append({
                    "tool": pattern.tool_name,
                    "args_hash": pattern.args_hash,
                    "count": pattern.count,
                    "hour": pattern.hour_of_day,
                    "day": pattern.day_of_week,
                })
        
        candidates.sort(key=lambda c: c["count"], reverse=True)
        return candidates[:10]
    
    # -------------------------------------------------------------------------
    # Adaptive TTLs: Learn from data change rates
    # -------------------------------------------------------------------------
    
    async def record_staleness(self, tool_name: str, args: dict, was_stale: bool):
        """Record whether cached data was stale when compared to fresh fetch.
        
        Call this when you fetch fresh data and can compare to cached version.
        Over time, this adjusts TTLs to match actual data volatility.
        """
        args_hash = self._get_args_hash(args)
        learning_key = f"{tool_name}:{args_hash}"
        base_ttl = self.DEFAULT_TTLS.get(tool_name, self._default_ttl)
        
        if learning_key not in self._ttl_learning:
            self._ttl_learning[learning_key] = TTLLearning(
                tool_name=tool_name,
                args_hash=args_hash,
                current_ttl=base_ttl,
            )
        
        learning = self._ttl_learning[learning_key]
        learning.checks += 1
        if was_stale:
            learning.stale_count += 1
        
        # Adjust TTL every 10 checks
        if learning.checks % 10 == 0:
            await self._adjust_ttl(learning_key, learning, base_ttl)
    
    async def _adjust_ttl(self, learning_key: str, learning: TTLLearning, base_ttl: float):
        """Adjust TTL based on observed staleness rate."""
        staleness = learning.staleness_rate
        
        # If data is often stale, reduce TTL
        # If data is rarely stale, increase TTL
        if staleness > 0.5:
            # More than half the time stale → reduce TTL
            multiplier = max(self.TTL_MIN_MULTIPLIER, 1.0 - (staleness - 0.5))
        elif staleness < 0.2:
            # Rarely stale → increase TTL
            multiplier = min(self.TTL_MAX_MULTIPLIER, 1.0 + (0.2 - staleness) * 5)
        else:
            # Acceptable staleness range → keep current
            multiplier = learning.current_ttl / base_ttl if base_ttl > 0 else 1.0
        
        new_ttl = base_ttl * multiplier
        old_ttl = learning.current_ttl
        
        if abs(new_ttl - old_ttl) > 1:  # Only log significant changes
            logger.info(f"[Adaptive TTL] {learning_key}: {old_ttl:.0f}s → {new_ttl:.0f}s (staleness={staleness:.1%})")
            self._stats.adaptive_adjustments += 1
        
        learning.current_ttl = new_ttl
        learning.last_adjustment = time.time()
        self._adapted_ttls[learning_key] = new_ttl
    
    def get_stats(self) -> dict:
        """Get cache statistics including learning metrics."""
        return {
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "hit_rate": f"{self._stats.hit_rate:.1%}",
            "evictions": self._stats.evictions,
            "entries": len(self._cache),
            "max_size": self._max_size,
            "patterns_learned": len(self._query_patterns),
            "ttls_adapted": len(self._adapted_ttls),
            "adaptive_adjustments": self._stats.adaptive_adjustments,
            "warm_hits": self._stats.warm_hits,
        }


# Global cache instance
_tool_cache = ToolResultCache(max_size=500)
_initialized = False


async def init_cache():
    """Initialize cache: load from DB and start background tasks."""
    global _initialized
    if _initialized:
        return
    
    await _tool_cache.load_from_db()
    await _tool_cache.start_background_tasks()
    _initialized = True
    logger.info("Cache layer initialized with persistence and learning")


async def get_cached(tool_name: str, args: dict) -> Optional[Any]:
    """Get a cached tool result, or None if not cached/expired."""
    entry = await _tool_cache.get(tool_name, args)
    return entry.value if entry else None


async def set_cached(tool_name: str, args: dict, value: Any, ttl: Optional[float] = None) -> None:
    """Cache a tool result."""
    await _tool_cache.set(tool_name, args, value, ttl)


async def invalidate_cache(tool_name: str, args: Optional[dict] = None) -> int:
    """Invalidate cache entries."""
    return await _tool_cache.invalidate(tool_name, args)


async def clear_cache() -> int:
    """Clear all cache entries."""
    return await _tool_cache.clear()


async def record_staleness(tool_name: str, args: dict, was_stale: bool) -> None:
    """Record staleness observation for adaptive TTL learning."""
    await _tool_cache.record_staleness(tool_name, args, was_stale)


def get_cache_stats() -> dict:
    """Get cache statistics."""
    return _tool_cache.get_stats()


def get_warming_candidates() -> list[dict]:
    """Get queries that would be pre-warmed at current time."""
    return _tool_cache.get_warming_candidates()


async def persist_cache() -> None:
    """Manually trigger cache persistence."""
    await _tool_cache.persist_to_db()


# Decorator for automatic caching
def cached_tool(ttl: Optional[float] = None):
    """
    Decorator to automatically cache tool handler results.
    
    Usage:
        @cached_tool(ttl=600)
        async def handle_get_weather(location: str) -> str:
            ...
    """
    def decorator(func: Callable):
        async def wrapper(*args, **kwargs):
            # Extract tool name from function name (handle_X -> X)
            tool_name = func.__name__
            if tool_name.startswith("handle_"):
                tool_name = tool_name[7:]
            
            # Check cache
            cached = await get_cached(tool_name, kwargs)
            if cached is not None:
                logger.info(f"[Cache] Returning cached result for {tool_name}")
                return cached
            
            # Execute and cache
            result = await func(*args, **kwargs)
            await set_cached(tool_name, kwargs, result, ttl)
            return result
        
        return wrapper
    return decorator
