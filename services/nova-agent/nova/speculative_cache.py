"""
Nova Agent - Speculative Cache

Pre-warmed grounded data for instant Zero-Wait responses.
Layer 1 of the Ground-Truth Architecture.

Features:
- TTL-based cache entries with automatic expiration
- Scheduled warming (cron-style triggers)
- Event-driven invalidation (calendar change, email arrival)
- Query pattern matching for cache key lookup
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum
import aiohttp

from loguru import logger


class QueryDomain(str, Enum):
    """Query domains for cache classification."""
    PRODUCTIVITY = "productivity"
    NEWS = "news"
    TASKS = "tasks"
    KNOWLEDGE = "knowledge"
    GENERAL = "general"


@dataclass
class CacheEntry:
    """A cached response ready for immediate delivery."""
    cache_key: str
    display_text: str      # Rich formatted text for UI
    speech_text: str       # Natural language for TTS
    domain: QueryDomain
    confidence: float = 1.0
    sources: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    ttl_seconds: int = 1800  # 30 min default
    
    # Context enrichment (v2.0)
    pic_context: dict = field(default_factory=dict)
    liam_frameworks: list[dict] = field(default_factory=list)
    kg_entities: list[dict] = field(default_factory=list)
    recent_turns: list[dict] = field(default_factory=list)
    
    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds
    
    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


@dataclass
class CacheConfig:
    """Configuration for a cache entry."""
    cache_key: str
    domain: QueryDomain
    tool: str              # Tool to call for warming
    tool_args: dict        # Arguments for the tool
    ttl_seconds: int = 1800
    triggers: list[str] = field(default_factory=list)  # calendar_change, email_arrival, etc.
    
    # Context enrichment flags (v2.0)
    enrich_with_pic: bool = False
    enrich_with_liam: bool = False
    enrich_with_kg: list[str] = field(default_factory=list)  # Entity types to fetch
    enrich_with_history: bool = False
    applicable_frameworks: list[str] = field(default_factory=list)  # LIAM frameworks


# Default cache configurations (v2.0 - Enriched)
DEFAULT_CACHE_CONFIGS: list[CacheConfig] = [
    # ── Productivity Domain (Enriched) ──────────────────────────────
    CacheConfig(
        cache_key="productivity.schedule.today",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "what's on my schedule today"},
        ttl_seconds=1800,  # 30 min
        triggers=["calendar_change", "morning_warmup"],
        # v2.0 enrichment
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_kg=["Person"],  # Meeting participants
        enrich_with_history=True,
        applicable_frameworks=["Time Management", "Eisenhower Matrix"],
    ),
    CacheConfig(
        cache_key="productivity.schedule.tomorrow",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "what's on my schedule tomorrow"},
        ttl_seconds=3600,  # 1 hour
        triggers=["calendar_change", "evening_warmup"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_kg=["Person"],
        enrich_with_history=True,
        applicable_frameworks=["Time Management"],
    ),
    CacheConfig(
        cache_key="productivity.email.triage",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "emails needing response"},
        ttl_seconds=600,  # 10 min
        triggers=["email_arrival", "periodic"],
        # EXAMPLE: Full enrichment with Filter Model
        enrich_with_pic=True,  # Cognitive style, peak hours
        enrich_with_liam=True,  # Filter Model, System 1/2
        enrich_with_kg=["Person"],  # Email sender relationships
        enrich_with_history=True,  # Follow-up awareness
        applicable_frameworks=[
            "Filter Model",  # Optimal stopping for email triage
            "System 1/2 Thinking",  # Fast vs deliberate responses
            "Nudge",  # Choice architecture
        ],
    ),
    CacheConfig(
        cache_key="productivity.email.unread",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "any unread emails"},
        ttl_seconds=900,  # 15 min
        triggers=["email_arrival", "periodic"],
        enrich_with_pic=True,
        enrich_with_kg=["Person"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="tasks.pending",
        domain=QueryDomain.TASKS,
        tool="check_studio",
        tool_args={"query": "what tasks do I have pending"},
        ttl_seconds=600,  # 10 min
        triggers=["task_change", "periodic"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_history=True,
        applicable_frameworks=["Atomic Habits", "Time Management"],
    ),
    CacheConfig(
        cache_key="productivity.schedule.next_meeting",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "next meeting"},
        ttl_seconds=900,  # 15 min
        triggers=["calendar_change"],
        enrich_with_pic=True,
        enrich_with_kg=["Person"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="productivity.schedule.week",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "schedule for the week"},
        ttl_seconds=7200,  # 2 hours
        triggers=["calendar_change"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_kg=["Person"],
        applicable_frameworks=["Time Management", "Eisenhower Matrix"],
    ),
    CacheConfig(
        cache_key="productivity.email.important",
        domain=QueryDomain.PRODUCTIVITY,
        tool="check_studio",
        tool_args={"query": "important emails"},
        ttl_seconds=600,  # 10 min
        triggers=["email_arrival"],
        enrich_with_pic=True,
        enrich_with_kg=["Person"],
        enrich_with_history=True,
    ),
    
    # ── Tesla Domain ───────────────────────────────────────────────
    CacheConfig(
        cache_key="tesla.vehicle.status",
        domain=QueryDomain.GENERAL,
        tool="tesla_vehicle_status",
        tool_args={},
        ttl_seconds=120,  # 2 min (fresh data)
        triggers=["vehicle_wake", "periodic"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="tesla.vehicle.charge_status",
        domain=QueryDomain.GENERAL,
        tool="tesla_vehicle_status",
        tool_args={"focus": "charge"},
        ttl_seconds=300,  # 5 min
        triggers=["charge_start", "periodic"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="tesla.vehicle.location",
        domain=QueryDomain.GENERAL,
        tool="tesla_vehicle_status",
        tool_args={"focus": "location"},
        ttl_seconds=180,  # 3 min
        triggers=["location_change"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    
    # ── Homelab Domain ─────────────────────────────────────────────
    CacheConfig(
        cache_key="homelab.services.all_status",
        domain=QueryDomain.GENERAL,
        tool="service_health_check",
        tool_args={},
        ttl_seconds=1800,  # 30 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="homelab.services.hermes_stats",
        domain=QueryDomain.GENERAL,
        tool="service_health_check",
        tool_args={"container": "cig"},
        ttl_seconds=900,  # 15 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="homelab.services.recent_errors",
        domain=QueryDomain.GENERAL,
        tool="service_logs",
        tool_args={"lines": 50, "filter": "ERROR"},
        ttl_seconds=60,  # 1 min (ultra-fresh)
        triggers=["error_detected"],
        enrich_with_history=True,
    ),
    
    # ── Clinical Domain ────────────────────────────────────────────
    CacheConfig(
        cache_key="clinical.patient.next",
        domain=QueryDomain.GENERAL,
        tool="check_studio",
        tool_args={"query": "next patient appointment"},
        ttl_seconds=900,  # 15 min
        triggers=["calendar_change"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_kg=["Patient", "Diagnosis"],
        enrich_with_history=True,
        applicable_frameworks=[
            "System 1/2 Thinking",
            "Bayesian Reasoning",
        ],
    ),
    CacheConfig(
        cache_key="clinical.schedule.today",
        domain=QueryDomain.GENERAL,
        tool="check_studio",
        tool_args={"query": "patient schedule today"},
        ttl_seconds=1800,  # 30 min
        triggers=["calendar_change"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        enrich_with_kg=["Patient"],
        applicable_frameworks=["Time Management"],
    ),
    
    # ── Personal Context Domain ────────────────────────────────────
    CacheConfig(
        cache_key="personal.location.current",
        domain=QueryDomain.GENERAL,
        tool="get_location",
        tool_args={},
        ttl_seconds=180,  # 3 min
        triggers=["location_change"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="personal.health.sleep_last_night",
        domain=QueryDomain.GENERAL,
        tool="check_studio",
        tool_args={"query": "sleep data last night"},
        ttl_seconds=3600,  # 1 hour
        triggers=["morning_warmup"],
        enrich_with_pic=True,
        enrich_with_liam=True,
        applicable_frameworks=["Atomic Habits"],
    ),
    CacheConfig(
        cache_key="personal.health.steps_today",
        domain=QueryDomain.GENERAL,
        tool="check_studio",
        tool_args={"query": "steps today"},
        ttl_seconds=900,  # 15 min
        triggers=["periodic"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    
    # ── Knowledge Domain ──────────────────────────────────────────
    CacheConfig(
        cache_key="knowledge.weather.current",
        domain=QueryDomain.KNOWLEDGE,
        tool="get_weather",
        tool_args={},
        ttl_seconds=1800,  # 30 min
        triggers=["hourly", "location_change"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="knowledge.weather.forecast_3day",
        domain=QueryDomain.KNOWLEDGE,
        tool="get_weather",
        tool_args={"forecast": "3day"},
        ttl_seconds=21600,  # 6 hours
        triggers=["morning_warmup"],
        enrich_with_pic=True,
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="knowledge.time.current",
        domain=QueryDomain.KNOWLEDGE,
        tool="get_time",
        tool_args={},
        ttl_seconds=60,  # 1 min
        triggers=["periodic"],
        enrich_with_pic=True,
    ),
    
    # ── News Domain ────────────────────────────────────────────────
    CacheConfig(
        cache_key="news.headlines.general",
        domain=QueryDomain.NEWS,
        tool="web_search",
        tool_args={"query": "top news headlines today"},
        ttl_seconds=900,  # 15 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="news.headlines.ai",
        domain=QueryDomain.NEWS,
        tool="web_search",
        tool_args={"query": "latest AI news"},
        ttl_seconds=900,  # 15 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="news.headlines.tech",
        domain=QueryDomain.NEWS,
        tool="web_search",
        tool_args={"query": "tech news today"},
        ttl_seconds=900,  # 15 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="news.headlines.spacex",
        domain=QueryDomain.NEWS,
        tool="web_search",
        tool_args={"query": "SpaceX latest news"},
        ttl_seconds=1800,  # 30 min
        triggers=["periodic"],
        enrich_with_history=True,
    ),
    CacheConfig(
        cache_key="news.headlines.local",
        domain=QueryDomain.NEWS,
        tool="web_search",
        tool_args={"query": "Houston news today"},
        ttl_seconds=1800,  # 30 min
        triggers=["periodic"],
        enrich_with_pic=True,  # Location context
        enrich_with_history=True,
    ),
]


class SpeculativeCache:
    """
    Pre-warmed cache for instant ground-truth responses.
    
    Usage:
        cache = SpeculativeCache()
        
        # Check cache before LLM
        result = await cache.lookup("what's on my schedule today")
        if result:
            # Send immediate response (<100ms)
            await send_grounded(result)
        else:
            # Continue to parallel retrieval
            pass
        
        # Warm cache periodically
        await cache.warm_all()
        
        # Invalidate on events
        await cache.invalidate("productivity.schedule.today")
    """
    
    def __init__(
        self,
        tool_dispatcher: Optional[Callable] = None,
        config: Optional[list[CacheConfig]] = None,
        user_id: str = "default",
    ):
        self._cache: dict[str, CacheEntry] = {}
        self._configs: dict[str, CacheConfig] = {}
        self._tool_dispatcher = tool_dispatcher
        self._warming_tasks: list[asyncio.Task] = []
        self._speech_transform = self._default_speech_transform
        self._user_id = user_id
        
        # Service URLs (from environment)
        import os
        self._pic_url = os.environ.get("PIC_URL", "http://localhost:8765")
        self._context_bridge_url = os.environ.get("CONTEXT_BRIDGE_URL", "http://localhost:8764")
        self._kg_api_url = os.environ.get("KG_API_URL", "http://localhost:8766")
        
        # Load configs
        for cfg in (config or DEFAULT_CACHE_CONFIGS):
            self._configs[cfg.cache_key] = cfg
    
    def _default_speech_transform(self, text: str) -> str:
        """Default: strip markdown for natural speech."""
        from nova.text_utils import strip_markdown_for_speech
        return strip_markdown_for_speech(text)
    
    def set_speech_transform(self, fn: Callable[[str], str]):
        """Set custom speech transformation function."""
        self._speech_transform = fn
    
    def set_tool_dispatcher(self, dispatcher: Callable):
        """Set the tool dispatcher for cache warming."""
        self._tool_dispatcher = dispatcher
    
    async def lookup(self, query: str) -> Optional[CacheEntry]:
        """
        Look up a query in the cache.
        
        Args:
            query: User query string
            
        Returns:
            CacheEntry if found and valid, None otherwise
        """
        # Match query to cache key
        cache_key = self._match_query(query)
        if not cache_key:
            return None
        
        entry = self._cache.get(cache_key)
        if not entry:
            logger.debug(f"[Cache] MISS: {cache_key} (not warmed)")
            return None
        
        if entry.is_expired:
            logger.debug(f"[Cache] EXPIRED: {cache_key} (age: {entry.age_seconds:.0f}s)")
            del self._cache[cache_key]
            return None
        
        logger.info(f"[Cache] HIT: {cache_key} (age: {entry.age_seconds:.0f}s, conf: {entry.confidence})")
        return entry
    
    def _match_query(self, query: str) -> Optional[str]:
        """Match a user query to a cache key."""
        query_lower = query.lower()
        
        # Pattern matching rules (v2.0 - expanded)
        patterns = {
            # Productivity
            "productivity.schedule.today": [
                "what's on my schedule", "what do i have today", "my schedule today",
                "appointments today", "meetings today", "what's today", "today's schedule",
            ],
            "productivity.schedule.tomorrow": [
                "tomorrow's schedule", "what do i have tomorrow", "meetings tomorrow",
                "appointments tomorrow", "tomorrow's meetings",
            ],
            "productivity.schedule.next_meeting": [
                "next meeting", "upcoming meeting", "what's next", "next appointment",
            ],
            "productivity.schedule.week": [
                "this week", "week schedule", "weekly schedule", "what's this week",
            ],
            "productivity.email.triage": [
                "email triage", "emails to respond", "emails needing response",
                "which emails should i answer",
            ],
            "productivity.email.unread": [
                "unread emails", "new emails", "any email", "check email",
            ],
            "productivity.email.important": [
                "important emails", "priority emails", "urgent emails",
            ],
            "tasks.pending": [
                "what tasks", "my tasks", "pending tasks", "todo list", "what do i need to do",
            ],
            
            # Tesla
            "tesla.vehicle.status": [
                "tesla status", "car status", "vehicle status", "how's my tesla",
                "check my car", "model 3 status",
            ],
            "tesla.vehicle.charge_status": [
                "charge status", "battery level", "charging", "how charged",
            ],
            "tesla.vehicle.location": [
                "where's my tesla", "car location", "where's my car",
            ],
            
            # Homelab
            "homelab.services.all_status": [
                "homelab status", "service status", "all services", "infrastructure status",
            ],
            "homelab.services.hermes_stats": [
                "hermes status", "email stats", "hermes core", "email service",
            ],
            "homelab.services.recent_errors": [
                "recent errors", "service errors", "what's broken", "any errors",
            ],
            
            # Clinical
            "clinical.patient.next": [
                "next patient", "upcoming patient", "next appointment",
            ],
            "clinical.schedule.today": [
                "patient schedule", "today's patients", "clinic schedule",
            ],
            
            # Personal
            "personal.location.current": [
                "where am i", "current location", "my location",
            ],
            "personal.health.sleep_last_night": [
                "sleep last night", "how did i sleep", "sleep data",
            ],
            "personal.health.steps_today": [
                "steps today", "how many steps", "step count",
            ],
            
            # Knowledge
            "knowledge.weather.current": [
                "weather", "temperature", "how's the weather", "is it going to rain",
            ],
            "knowledge.weather.forecast_3day": [
                "weather forecast", "3 day forecast", "forecast", "weather this week",
            ],
            "knowledge.time.current": [
                "what time", "current time", "time now",
            ],
            
            # News (specific before general)
            "news.headlines.ai": [
                "ai news", "artificial intelligence news", "machine learning news",
            ],
            "news.headlines.tech": [
                "tech news", "technology news",
            ],
            "news.headlines.spacex": [
                "spacex news", "spacex", "starship",
            ],
            "news.headlines.local": [
                "local news", "houston news",
            ],
            "news.headlines.general": [
                "news", "headlines", "what's happening", "what's new",
            ],
        }
        
        for cache_key, keywords in patterns.items():
            if any(kw in query_lower for kw in keywords):
                return cache_key
        
        return None
    
    async def warm(self, cache_key: str) -> bool:
        """
        Warm a specific cache entry with enriched context.
        
        Args:
            cache_key: Key to warm
            
        Returns:
            True if successful, False otherwise
        """
        config = self._configs.get(cache_key)
        if not config:
            logger.warning(f"[Cache] No config for {cache_key}")
            return False
        
        if not self._tool_dispatcher:
            logger.warning(f"[Cache] No tool dispatcher configured")
            return False
        
        try:
            # 1. Get base data from tool
            logger.info(f"[Cache] Warming: {cache_key}")
            
            base_result = await self._tool_dispatcher(
                tool_name=config.tool,
                tool_args=config.tool_args,
            )
            
            if not base_result:
                logger.warning(f"[Cache] Warming returned empty: {cache_key}")
                return False
            
            # 2. Enrich with context (parallel fetching for speed)
            enrichment_tasks = []
            
            if config.enrich_with_pic:
                enrichment_tasks.append(self._fetch_pic_context(cache_key))
            else:
                enrichment_tasks.append(asyncio.sleep(0, result={}))
            
            if config.enrich_with_liam:
                enrichment_tasks.append(
                    self._fetch_liam_context(cache_key, config.applicable_frameworks)
                )
            else:
                enrichment_tasks.append(asyncio.sleep(0, result={"frameworks": [], "active_dimensions": [], "linked_goals": []}))
            
            if config.enrich_with_kg:
                enrichment_tasks.append(
                    self._fetch_kg_entities(base_result, config.enrich_with_kg)
                )
            else:
                enrichment_tasks.append(asyncio.sleep(0, result=[]))
            
            if config.enrich_with_history:
                enrichment_tasks.append(self._fetch_recent_turns(limit=5))
            else:
                enrichment_tasks.append(asyncio.sleep(0, result=[]))
            
            # Fetch all context in parallel
            pic_ctx, liam_ctx, kg_entities, recent_turns = await asyncio.gather(
                *enrichment_tasks,
                return_exceptions=True
            )
            
            # Handle exceptions
            if isinstance(pic_ctx, Exception):
                logger.warning(f"[Cache] PIC enrichment failed: {pic_ctx}")
                pic_ctx = {}
            if isinstance(liam_ctx, Exception):
                logger.warning(f"[Cache] LIAM enrichment failed: {liam_ctx}")
                liam_ctx = {"frameworks": [], "active_dimensions": [], "linked_goals": []}
            if isinstance(kg_entities, Exception):
                logger.warning(f"[Cache] KG enrichment failed: {kg_entities}")
                kg_entities = []
            if isinstance(recent_turns, Exception):
                logger.warning(f"[Cache] History enrichment failed: {recent_turns}")
                recent_turns = []
            
            # 3. Generate enriched response text
            enriched_text = await self._generate_enriched_response(
                base_data=base_result,
                pic_context=pic_ctx,
                liam_context=liam_ctx,
                kg_entities=kg_entities,
                recent_turns=recent_turns,
            )
            
            # 4. Transform for speech
            speech_text = self._speech_transform(enriched_text)
            
            # 5. Create enriched cache entry
            entry = CacheEntry(
                cache_key=cache_key,
                display_text=enriched_text,
                speech_text=speech_text,
                domain=config.domain,
                confidence=0.95,
                sources=[{"source": config.tool, "cached": True}],
                ttl_seconds=config.ttl_seconds,
                # Enriched context
                pic_context=pic_ctx,
                liam_frameworks=liam_ctx.get("frameworks", []),
                kg_entities=kg_entities,
                recent_turns=recent_turns,
            )
            
            self._cache[cache_key] = entry
            
            enrichment_summary = []
            if pic_ctx:
                enrichment_summary.append("PIC")
            if liam_ctx.get("frameworks"):
                enrichment_summary.append(f"LIAM({len(liam_ctx['frameworks'])})")
            if kg_entities:
                enrichment_summary.append(f"KG({len(kg_entities)})")
            if recent_turns:
                enrichment_summary.append(f"History({len(recent_turns)})")
            
            logger.info(
                f"[Cache] Warmed: {cache_key} ({len(enriched_text)} chars) "
                f"[{'+'.join(enrichment_summary) if enrichment_summary else 'base'}]"
            )
            return True
            
        except Exception as e:
            logger.error(f"[Cache] Warming failed: {cache_key} - {e}")
            return False
    
    async def warm_all(self) -> dict[str, bool]:
        """
        Warm all cache entries.
        
        Returns:
            Dict of cache_key -> success
        """
        results = {}
        tasks = []
        
        for cache_key in self._configs.keys():
            if cache_key not in self._cache or self._cache[cache_key].is_expired:
                tasks.append(self._warm_and_track(cache_key, results))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        return results
    
    async def _warm_and_track(self, cache_key: str, results: dict):
        """Warm a cache entry and track result."""
        results[cache_key] = await self.warm(cache_key)
    
    async def invalidate(self, cache_key: str):
        """Invalidate a cache entry."""
        if cache_key in self._cache:
            del self._cache[cache_key]
            logger.info(f"[Cache] Invalidated: {cache_key}")
    
    async def invalidate_by_trigger(self, trigger: str):
        """Invalidate all cache entries triggered by an event."""
        invalidated = []
        for cache_key, config in self._configs.items():
            if trigger in config.triggers:
                await self.invalidate(cache_key)
                invalidated.append(cache_key)
        
        if invalidated:
            logger.info(f"[Cache] Invalidated by trigger '{trigger}': {invalidated}")
    
    def get_status(self) -> dict:
        """Get cache status for debugging."""
        status = {
            "total_configs": len(self._configs),
            "cached_entries": len(self._cache),
            "entries": {},
        }
        
        for cache_key, entry in self._cache.items():
            status["entries"][cache_key] = {
                "age_seconds": entry.age_seconds,
                "is_expired": entry.is_expired,
                "confidence": entry.confidence,
                "domain": entry.domain.value,
            }
        
        return status
    
    async def start_scheduled_warming(self, interval_seconds: int = 300):
        """Start background task for periodic cache warming."""
        async def _warm_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                logger.info("[Cache] Scheduled warming...")
                await self.warm_all()
        
        task = asyncio.create_task(_warm_loop())
        self._warming_tasks.append(task)
        return task
    
    def stop_scheduled_warming(self):
        """Stop all scheduled warming tasks."""
        for task in self._warming_tasks:
            task.cancel()
        self._warming_tasks.clear()
    
    # ── Context Enrichment Methods (v2.0) ──────────────────────────────
    
    async def _fetch_pic_context(self, cache_key: str) -> dict:
        """
        Fetch PIC context for cache enrichment.
        
        Returns:
            {
                "identity": {...},
                "preferences": {...},
                "cognitive_style": {...},
                "peak_hours": "9-11am",
                "current_time": "3pm",
            }
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Get full identity context
                async with session.post(
                    f"{self._context_bridge_url}/v1/context",
                    json={
                        "agent_id": "nova-cache",
                        "user_id": self._user_id,
                        "include_preferences": True,
                        "include_cognitive_profile": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Extract relevant context
                        cognitive_profile = data.get("cognitive_profile") or {}
                        pic_ctx = {
                            "identity": data.get("identity", {}),
                            "preferences": data.get("preferences", {}),
                            "cognitive_style": cognitive_profile.get("decision_style"),
                            "peak_hours": cognitive_profile.get("peak_hours", "9-11am"),
                        }
                        
                        # Add current time context
                        from datetime import datetime
                        import pytz
                        tz = pytz.timezone(data.get("identity", {}).get("timezone", "America/Chicago"))
                        now = datetime.now(tz)
                        pic_ctx["current_time"] = now.strftime("%I%p").lstrip("0").lower()
                        pic_ctx["current_hour"] = now.hour
                        
                        logger.debug(f"[Cache] PIC context fetched for {cache_key}")
                        return pic_ctx
                    else:
                        logger.warning(f"[Cache] PIC context fetch failed: {resp.status}")
                        return {}
        except asyncio.TimeoutError:
            logger.warning(f"[Cache] PIC context timeout for {cache_key}")
            return {}
        except Exception as e:
            logger.error(f"[Cache] PIC context error: {e}")
            return {}
    
    async def _fetch_liam_context(self, cache_key: str, frameworks: list[str]) -> dict:
        """
        Fetch LIAM frameworks and guidance for cache enrichment.
        
        Args:
            cache_key: Cache key being warmed
            frameworks: List of framework names to fetch
            
        Returns:
            {
                "frameworks": [
                    {
                        "name": "Filter Model",
                        "guidance": "Optimal stopping: Review first 37%...",
                        "source": "Algorithms to Live By"
                    }
                ],
                "active_dimensions": ["Communication", "Professional"],
                "linked_goals": [...]
            }
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Get enriched context with LIAM frameworks
                async with session.post(
                    f"{self._context_bridge_url}/v1/context",
                    json={
                        "agent_id": "nova-cache",
                        "user_id": self._user_id,
                        "include_goals": True,
                        "include_dimensions": True,
                        "include_frameworks": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Extract LIAM context
                        liam_ctx = {
                            "frameworks": [],
                            "active_dimensions": data.get("applicable_dimensions", [])[:3],
                            "linked_goals": data.get("goals", [])[:5],
                        }
                        
                        # Filter to requested frameworks
                        all_frameworks = data.get("frameworks", [])
                        for fw_name in frameworks:
                            fw = next((f for f in all_frameworks if f.get("name") == fw_name), None)
                            if fw:
                                liam_ctx["frameworks"].append({
                                    "name": fw.get("name"),
                                    "guidance": fw.get("description", "")[:200],
                                    "source": fw.get("source", ""),
                                })
                        
                        logger.debug(f"[Cache] LIAM context fetched: {len(liam_ctx['frameworks'])} frameworks")
                        return liam_ctx
                    else:
                        logger.warning(f"[Cache] LIAM context fetch failed: {resp.status}")
                        return {"frameworks": [], "active_dimensions": [], "linked_goals": []}
        except asyncio.TimeoutError:
            logger.warning(f"[Cache] LIAM context timeout for {cache_key}")
            return {"frameworks": [], "active_dimensions": [], "linked_goals": []}
        except Exception as e:
            logger.error(f"[Cache] LIAM context error: {e}")
            return {"frameworks": [], "active_dimensions": [], "linked_goals": []}
    
    async def _fetch_kg_entities(self, base_text: str, entity_types: list[str]) -> list[dict]:
        """
        Extract and fetch KG entities from base text.
        
        Args:
            base_text: Base cache content to extract entities from
            entity_types: Types of entities to fetch (Person, Project, etc.)
            
        Returns:
            [
                {
                    "name": "Dr. Coleman",
                    "type": "Person",
                    "relationship": "Colleague - Cardiology",
                    "context": "Last email 2 days ago"
                }
            ]
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Extract entities from text
                async with session.post(
                    f"{self._kg_api_url}/api/kg/extract",
                    json={
                        "text": base_text,
                        "entity_types": entity_types,
                        "user_id": self._user_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        entities = data.get("entities", [])
                        
                        # Enrich each entity with relationship context
                        enriched = []
                        for entity in entities[:5]:  # Limit to 5 entities
                            enriched.append({
                                "name": entity.get("name"),
                                "type": entity.get("type"),
                                "relationship": entity.get("relationship", ""),
                                "context": entity.get("context", "")[:100],
                            })
                        
                        logger.debug(f"[Cache] KG entities fetched: {len(enriched)}")
                        return enriched
                    else:
                        logger.warning(f"[Cache] KG entity fetch failed: {resp.status}")
                        return []
        except asyncio.TimeoutError:
            logger.warning(f"[Cache] KG entity timeout")
            return []
        except Exception as e:
            logger.error(f"[Cache] KG entity error: {e}")
            return []
    
    async def _fetch_recent_turns(self, limit: int = 5) -> list[dict]:
        """
        Fetch recent conversation turns for context.
        
        Args:
            limit: Number of recent turns to fetch
            
        Returns:
            [
                {"role": "user", "content": "...", "timestamp": "..."},
                {"role": "assistant", "content": "...", "timestamp": "..."}
            ]
        """
        try:
            # Import conversation store
            from nova.store import get_recent_turns
            
            turns = await get_recent_turns(
                user_id=self._user_id,
                limit=limit,
            )
            
            logger.debug(f"[Cache] Recent turns fetched: {len(turns)}")
            return turns
        except Exception as e:
            logger.error(f"[Cache] Recent turns error: {e}")
            return []
    
    async def _generate_enriched_response(
        self,
        base_data: str,
        pic_context: dict,
        liam_context: dict,
        kg_entities: list[dict],
        recent_turns: list[dict],
    ) -> str:
        """
        Generate enriched response text combining base data with context.
        
        This method intelligently weaves together:
        - Base tool output (schedule, emails, etc.)
        - PIC context (cognitive style, peak hours, preferences)
        - LIAM frameworks (scientific guidance)
        - KG entities (relationship context)
        - Recent conversation turns (follow-up awareness)
        
        Args:
            base_data: Base tool output
            pic_context: PIC identity/preferences/cognitive context
            liam_context: LIAM frameworks and guidance
            kg_entities: Knowledge graph entities
            recent_turns: Recent conversation history
            
        Returns:
            Enriched response text ready for display/speech
        """
        # Start with base data
        enriched = base_data
        
        # Add LIAM framework guidance if applicable
        if liam_context.get("frameworks"):
            framework_guidance = []
            
            for fw in liam_context["frameworks"]:
                # Extract actionable guidance
                guidance = fw.get("guidance", "")
                if guidance:
                    framework_guidance.append(
                        f"\n\n**{fw['name']}**: {guidance}"
                    )
            
            if framework_guidance:
                enriched += "\n\n---\n**Framework Guidance:**" + "".join(framework_guidance)
        
        # Add PIC-based recommendations
        if pic_context:
            current_hour = pic_context.get("current_hour", 12)
            peak_hours = pic_context.get("peak_hours", "9-11am")
            cognitive_style = pic_context.get("cognitive_style", "")
            
            # Check if current time is outside peak hours
            peak_start, peak_end = 9, 11  # Default
            try:
                if "-" in peak_hours:
                    start_str, end_str = peak_hours.split("-")
                    peak_start = int(start_str.replace("am", "").replace("pm", "").strip())
                    peak_end = int(end_str.replace("am", "").replace("pm", "").strip())
                    if "pm" in end_str and peak_end < 12:
                        peak_end += 12
            except:
                pass
            
            if current_hour < peak_start or current_hour > peak_end:
                enriched += (
                    f"\n\n*Note: It's currently outside your peak cognitive hours "
                    f"({peak_hours}). Complex tasks may be better suited for tomorrow morning.*"
                )
        
        # Add KG entity context
        if kg_entities:
            entity_context = []
            for entity in kg_entities[:3]:  # Top 3 entities
                name = entity.get("name", "")
                relationship = entity.get("relationship", "")
                context = entity.get("context", "")
                
                if name and (relationship or context):
                    entity_info = f"**{name}**"
                    if relationship:
                        entity_info += f" ({relationship})"
                    if context:
                        entity_info += f" — {context}"
                    entity_context.append(entity_info)
            
            if entity_context:
                enriched += "\n\n---\n**Context:**\n- " + "\n- ".join(entity_context)
        
        # Add conversation continuity hints
        if recent_turns:
            # Check if this is a follow-up query
            last_user_turn = next(
                (t for t in reversed(recent_turns) if t.get("role") == "user"),
                None
            )
            if last_user_turn:
                last_content = last_user_turn.get("content", "").lower()
                
                # Detect follow-up patterns
                follow_up_indicators = ["and", "also", "what about", "how about", "tomorrow"]
                if any(indicator in last_content for indicator in follow_up_indicators):
                    # This might be a follow-up - add continuity note
                    enriched += "\n\n*(Continuing from previous context)*"
        
        return enriched


# Singleton instance
_cache_instance: Optional[SpeculativeCache] = None


def get_speculative_cache() -> SpeculativeCache:
    """Get the global speculative cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SpeculativeCache()
    return _cache_instance


def init_speculative_cache(
    tool_dispatcher: Callable,
    speech_transform: Optional[Callable] = None,
) -> SpeculativeCache:
    """Initialize the global speculative cache."""
    global _cache_instance
    _cache_instance = SpeculativeCache(tool_dispatcher=tool_dispatcher)
    if speech_transform:
        _cache_instance.set_speech_transform(speech_transform)
    return _cache_instance
