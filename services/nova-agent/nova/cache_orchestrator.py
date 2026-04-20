"""
Nova Cache Orchestrator.

Manages cache warming, eviction, and TTL optimization:
1. Analyze query patterns and adjust warming schedules
2. Make intelligent eviction decisions based on context
3. Adjust TTLs based on cross-domain reasoning
4. Pre-warm cache based on calendar/email analysis
5. Provide cache health reports and recommendations

This module provides an HTTP API for cache management, plus a periodic
analysis task for pattern-driven optimization.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger

from nova.cache import (
    _tool_cache,
    get_cache_stats,
    invalidate_cache,
    set_cached,
    record_staleness,
)
from nova.warming import (
    WarmingSchedule,
    get_warming_service,
    get_warming_status,
)

# AI Gateway endpoint for LLM-powered cache analysis
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777")
AI_GATEWAY_KEY = os.environ.get("AI_GATEWAY_KEY", "")
USER_TZ = ZoneInfo(os.environ.get("USER_TIMEZONE", "America/Chicago"))

# Analysis interval (how often cache strategy is reviewed)
ANALYSIS_INTERVAL_HOURS = int(os.environ.get("CACHE_ANALYSIS_INTERVAL", "6"))


class CacheOrchestrator:
    """
    LLM-powered cache orchestration via AI Gateway.

    Provides intelligent cache management by delegating complex decisions
    to the AI Gateway while keeping the cache layer fast and simple.
    """

    def __init__(self):
        self._analysis_task: Optional[asyncio.Task] = None
        self._last_analysis: Optional[datetime] = None
        self._cache_recommendations: list[dict] = []
        self._running = False
    
    async def start(self):
        """Start the orchestrator background tasks."""
        if self._running:
            return
        self._running = True
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info("Cache orchestrator started")
    
    async def stop(self):
        """Stop the orchestrator."""
        self._running = False
        if self._analysis_task:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass
        logger.info("Cache orchestrator stopped")
    
    async def _analysis_loop(self):
        """Periodically analyze cache patterns via AI Gateway."""
        # Wait a bit before first analysis
        await asyncio.sleep(300)  # 5 min after startup
        
        while self._running:
            try:
                await self.request_cache_analysis()
                await asyncio.sleep(ANALYSIS_INTERVAL_HOURS * 3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cache analysis loop error: {e}")
                await asyncio.sleep(3600)  # Retry in 1 hour
    
    # =========================================================================
    # Cache Analysis Integration
    # =========================================================================
    
    async def request_cache_analysis(self) -> dict:
        """
        Analyze cache patterns via AI Gateway and provide recommendations.

        The AI Gateway receives:
        - Current cache stats
        - Query patterns from the last 24 hours
        - Warming schedule status
        - Seasonal context

        Returns:
        - Recommended schedule changes
        - TTL adjustments
        - Pre-warming suggestions based on calendar/email
        """
        # Gather context for analysis
        context = await self._build_analysis_context()
        
        prompt = f"""Analyze the Nova voice assistant's cache layer and provide optimization recommendations.

## Current Cache State
{json.dumps(context['cache_stats'], indent=2)}

## Query Patterns (last 24h)
{json.dumps(context['query_patterns'][:20], indent=2)}

## Current Warming Schedules
{json.dumps(context['warming_schedules'][:10], indent=2)}

## Seasonal Context
{json.dumps(context['seasonal'], indent=2)}

## Your Task
Analyze these patterns and provide specific recommendations in JSON format:

```json
{{
  "schedule_changes": [
    {{"action": "add|remove|modify", "name": "schedule_name", "tool": "tool_name", "hours": [7,8], "days": [0,1,2,3,4], "reason": "why"}}
  ],
  "ttl_adjustments": [
    {{"tool": "tool_name", "current_ttl": 600, "recommended_ttl": 900, "reason": "why"}}
  ],
  "prewarm_suggestions": [
    {{"tool": "tool_name", "args": {{}}, "when": "description", "reason": "why"}}
  ],
  "eviction_priorities": [
    {{"tool": "tool_name", "priority": "low|medium|high", "reason": "why"}}
  ],
  "insights": "Brief summary of patterns observed"
}}
```

Focus on:
1. Patterns that suggest new warming schedules
2. Tools with high miss rates that should be pre-warmed
3. Tools with low hit rates that might have wrong TTLs
4. Cross-domain opportunities (calendar event → related data)
"""

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": "application/json"}
                if AI_GATEWAY_KEY:
                    headers["Authorization"] = f"Bearer {AI_GATEWAY_KEY}"
                
                # Use AI Gateway's chat completions endpoint
                payload = {
                    "model": "default",
                    "messages": [
                        {"role": "system", "content": "You are a cache optimization analyst. Analyze patterns and provide JSON recommendations."},
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                }
                
                async with session.post(
                    f"{AI_GATEWAY_URL}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Cache analysis request failed: {resp.status}")
                        return {"error": f"HTTP {resp.status}"}
                    
                    result = await resp.json()
                    
                    # Extract content from OpenAI-style response
                    choices = result.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        result = {"result": content}
                    
                    # Parse recommendations from response
                    recommendations = self._parse_recommendations(result)
                    
                    if recommendations:
                        self._cache_recommendations = recommendations
                        self._last_analysis = datetime.now(USER_TZ)
                        await self._apply_recommendations(recommendations)
                        logger.info(f"Cache analysis complete: {len(recommendations)} recommendations")
                    
                    return recommendations
                    
        except Exception as e:
            logger.error(f"Cache analysis error: {e}")
            return {"error": str(e)}
    
    async def _build_analysis_context(self) -> dict:
        """Build context for cache analysis."""
        # Cache stats
        cache_stats = get_cache_stats()
        
        # Query patterns from cache layer
        patterns = []
        for pattern_key, pattern in _tool_cache._query_patterns.items():
            patterns.append({
                "tool": pattern.tool_name,
                "hour": pattern.hour_of_day,
                "day": pattern.day_of_week,
                "count": pattern.count,
                "last_seen": datetime.fromtimestamp(pattern.last_seen).isoformat(),
            })
        patterns.sort(key=lambda p: p["count"], reverse=True)
        
        # Warming schedules
        warming_service = get_warming_service()
        schedules = []
        if warming_service:
            for s in warming_service._schedules:
                schedules.append({
                    "name": s.name,
                    "tool": s.tool_name,
                    "hours": s.hours,
                    "days": s.days,
                    "enabled": s.enabled,
                    "last_run": s.last_run.isoformat() if s.last_run else None,
                })
        
        # Seasonal context
        seasonal = {}
        if warming_service:
            seasonal = warming_service.get_seasonal_context()
        
        return {
            "cache_stats": cache_stats,
            "query_patterns": patterns,
            "warming_schedules": schedules,
            "seasonal": seasonal,
            "analysis_time": datetime.now(USER_TZ).isoformat(),
        }
    
    def _parse_recommendations(self, analysis_result: dict) -> dict:
        """Parse LLM response into actionable recommendations."""
        try:
            # LLM returns result in various formats, try to extract JSON
            content = analysis_result.get("result", "")
            if isinstance(content, str):
                # Try to find JSON block in response
                import re
                json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(1))
                # Try direct JSON parse
                try:
                    return json.loads(content)
                except:
                    pass
            elif isinstance(content, dict):
                return content
        except Exception as e:
            logger.warning(f"Failed to parse cache recommendations: {e}")
        
        return {}
    
    async def _apply_recommendations(self, recommendations: dict):
        """Apply recommendations to the cache/warming systems."""
        warming_service = get_warming_service()
        
        # Apply schedule changes
        for change in recommendations.get("schedule_changes", []):
            action = change.get("action")
            name = change.get("name")
            
            if action == "add" and warming_service:
                schedule = WarmingSchedule(
                    name=name,
                    tool_name=change.get("tool", ""),
                    args=change.get("args", {}),
                    hours=change.get("hours", []),
                    days=change.get("days"),
                )
                warming_service.add_schedule(schedule)
                logger.info(f"[Orchestrator] Added schedule: {name}")
                
            elif action == "remove" and warming_service:
                warming_service.remove_schedule(name)
                logger.info(f"[Orchestrator] Removed schedule: {name}")
                
            elif action == "modify" and warming_service:
                # Remove and re-add with new settings
                warming_service.remove_schedule(name)
                schedule = WarmingSchedule(
                    name=name,
                    tool_name=change.get("tool", ""),
                    args=change.get("args", {}),
                    hours=change.get("hours", []),
                    days=change.get("days"),
                )
                warming_service.add_schedule(schedule)
                logger.info(f"[Orchestrator] Modified schedule: {name}")
        
        # Apply TTL adjustments
        for adj in recommendations.get("ttl_adjustments", []):
            tool = adj.get("tool")
            new_ttl = adj.get("recommended_ttl")
            if tool and new_ttl:
                # Update the default TTL for this tool
                _tool_cache.DEFAULT_TTLS[tool] = new_ttl
                logger.info(f"[Orchestrator] Adjusted TTL for {tool}: {new_ttl}s")
        
        # Log insights
        insights = recommendations.get("insights", "")
        if insights:
            logger.info(f"[Orchestrator] Cache insights: {insights}")
    
    # =========================================================================
    # Event-Driven Cache Decisions
    # =========================================================================
    
    async def on_calendar_event(self, event: dict):
        """
        Called when a calendar event is approaching.
        Ask AI Gateway what data to pre-warm.
        """
        title = event.get("title", "")
        start_time = event.get("start", "")
        attendees = event.get("attendees", [])
        
        prompt = f"""A calendar event is approaching:
- Title: {title}
- Start: {start_time}
- Attendees: {', '.join(attendees[:5])}

What data should Nova pre-warm to be ready for questions about this event?
Return JSON: {{"prewarm": [{{"tool": "...", "args": {{...}}, "reason": "..."}}]}}
"""
        
        # Quick AI Gateway call for event-specific warming
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": "application/json"}
                if AI_GATEWAY_KEY:
                    headers["Authorization"] = f"Bearer {AI_GATEWAY_KEY}"
                
                payload = {
                    "model": "default",
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                
                async with session.post(
                    f"{AI_GATEWAY_URL}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        choices = result.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content", "")
                            result = {"result": content}
                        recs = self._parse_recommendations(result)
                        for pw in recs.get("prewarm", []):
                            tool = pw.get("tool")
                            args = pw.get("args", {})
                            if tool:
                                # Dispatch the tool to warm cache
                                from nova.tools import dispatch_tool
                                await dispatch_tool(tool, args)
                                logger.info(f"[Orchestrator] Pre-warmed {tool} for event: {title}")
        except Exception as e:
            logger.warning(f"Event-driven warming failed: {e}")
    
    async def on_email_received(self, email: dict):
        """
        Called when an important email is received.
        Ask AI Gateway if any data should be pre-warmed.
        """
        subject = email.get("subject", "")
        sender = email.get("from", "")
        snippet = email.get("snippet", "")[:200]
        
        # Only process emails that might need action
        keywords = ["meeting", "flight", "reservation", "appointment", "reminder", "urgent"]
        if not any(kw in subject.lower() or kw in snippet.lower() for kw in keywords):
            return
        
        prompt = f"""An email was received that might need preparation:
- Subject: {subject}
- From: {sender}
- Preview: {snippet}

Should Nova pre-warm any data? Return JSON: {{"prewarm": [{{"tool": "...", "args": {{...}}}}]}} or {{"prewarm": []}} if none needed.
"""
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": "application/json"}
                if AI_GATEWAY_KEY:
                    headers["Authorization"] = f"Bearer {AI_GATEWAY_KEY}"
                
                payload = {
                    "model": "default",
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                
                async with session.post(
                    f"{AI_GATEWAY_URL}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        choices = result.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content", "")
                            result = {"result": content}
                        recs = self._parse_recommendations(result)
                        for pw in recs.get("prewarm", []):
                            tool = pw.get("tool")
                            args = pw.get("args", {})
                            if tool:
                                from nova.tools import dispatch_tool
                                await dispatch_tool(tool, args)
                                logger.info(f"[Orchestrator] Pre-warmed {tool} for email: {subject[:30]}")
        except Exception as e:
            logger.warning(f"Email-driven warming failed: {e}")
    
    # =========================================================================
    # Eviction Decisions
    # =========================================================================
    
    async def get_eviction_priority(self, cache_key: str) -> str:
        """
        Ask AI Gateway whether a cache entry should be evicted.
        Returns: "keep", "evict", or "low_priority"
        """
        entry = _tool_cache._cache.get(cache_key)
        if not entry:
            return "evict"
        
        # For simple cases, use rules
        if entry.hit_count == 0 and entry.age_seconds > 300:
            return "evict"
        if entry.hit_count > 5:
            return "keep"
        
        # For complex cases, could ask AI Gateway (but this is expensive)
        # In practice, use the rule-based approach for eviction
        return "low_priority"
    
    # =========================================================================
    # Status & Management
    # =========================================================================
    
    def get_status(self) -> dict:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "last_analysis": self._last_analysis.isoformat() if self._last_analysis else None,
            "recommendations_count": len(self._cache_recommendations),
            "analysis_interval_hours": ANALYSIS_INTERVAL_HOURS,
            "next_analysis_in": self._time_until_next_analysis(),
        }
    
    def _time_until_next_analysis(self) -> str:
        if not self._last_analysis:
            return "pending"
        next_time = self._last_analysis + timedelta(hours=ANALYSIS_INTERVAL_HOURS)
        remaining = next_time - datetime.now(USER_TZ)
        if remaining.total_seconds() < 0:
            return "due"
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m"
    
    def get_recommendations(self) -> dict:
        """Get the latest cache recommendations."""
        return {
            "last_analysis": self._last_analysis.isoformat() if self._last_analysis else None,
            "recommendations": self._cache_recommendations,
        }


# Global instance
_orchestrator: Optional[CacheOrchestrator] = None


async def init_orchestrator() -> CacheOrchestrator:
    """Initialize and start the cache orchestrator."""
    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator
    
    _orchestrator = CacheOrchestrator()
    await _orchestrator.start()
    return _orchestrator


def get_orchestrator() -> Optional[CacheOrchestrator]:
    """Get the orchestrator instance."""
    return _orchestrator


async def trigger_analysis() -> dict:
    """Manually trigger a cache analysis."""
    if _orchestrator is None:
        return {"error": "Orchestrator not initialized"}
    return await _orchestrator.request_cache_analysis()


def get_orchestrator_status() -> dict:
    """Get orchestrator status."""
    if _orchestrator is None:
        return {"running": False, "message": "Orchestrator not initialized"}
    return _orchestrator.get_status()
