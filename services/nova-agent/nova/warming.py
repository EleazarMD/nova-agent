"""
Nova Agent Proactive Cache Warming Service.

Feeds the cache layer with contextual data based on:
- Time of day (morning briefing, commute, evening wind-down)
- Day of week (weekday vs weekend patterns)
- Seasonality (weather patterns, holidays, recurring events)
- User behavior patterns (learned from query history)
- External triggers (calendar events, geofence, vehicle state)

This service runs in the background and proactively fetches data
that the user is likely to request, enabling true zero-wait responses.
"""

import asyncio
import os
from datetime import datetime, timedelta, time as dt_time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from loguru import logger

# User timezone (should come from user preferences)
USER_TZ = ZoneInfo(os.environ.get("USER_TIMEZONE", "America/Chicago"))
USER_LOCATION = os.environ.get("USER_LOCATION", "Dallas, TX")
USER_WORK_LOCATION = os.environ.get("USER_WORK_LOCATION", "")


class WarmingSchedule:
    """Defines when and what to pre-fetch."""
    
    def __init__(
        self,
        name: str,
        tool_name: str,
        args: dict,
        hours: list[int] = None,  # Hours of day to run (0-23)
        days: list[int] = None,   # Days of week (0=Mon, 6=Sun), None=all
        minutes_before: int = 5,  # Run this many minutes before the hour
        enabled: bool = True,
    ):
        self.name = name
        self.tool_name = tool_name
        self.args = args
        self.hours = hours or list(range(24))
        self.days = days  # None means all days
        self.minutes_before = minutes_before
        self.enabled = enabled
        self.last_run: Optional[datetime] = None
    
    def should_run(self, now: datetime) -> bool:
        """Check if this schedule should run at the given time."""
        if not self.enabled:
            return False
        
        # Check day of week
        if self.days is not None and now.weekday() not in self.days:
            return False
        
        # Check if we're in the right time window
        current_hour = now.hour
        current_minute = now.minute
        
        for target_hour in self.hours:
            # Calculate the target time (minutes_before the hour)
            target_minute = 60 - self.minutes_before
            check_hour = (target_hour - 1) % 24 if self.minutes_before > 0 else target_hour
            
            if self.minutes_before == 0:
                # Run at the start of the hour
                if current_hour == target_hour and current_minute < 5:
                    if self._not_run_recently(now):
                        return True
            else:
                # Run minutes_before the target hour
                if current_hour == check_hour and current_minute >= target_minute:
                    if self._not_run_recently(now):
                        return True
        
        return False
    
    def _not_run_recently(self, now: datetime) -> bool:
        """Ensure we don't run too frequently."""
        if self.last_run is None:
            return True
        return (now - self.last_run).total_seconds() > 3600  # At least 1 hour between runs


class ContextualWarmingService:
    """
    Proactively warms the cache with contextual data.
    
    Data sources are organized by temporal patterns:
    - Circadian: Time-of-day patterns (morning, afternoon, evening)
    - Weekly: Day-of-week patterns (weekday vs weekend)
    - Seasonal: Month/season patterns
    - Event-driven: Triggered by external events
    """
    
    def __init__(self, dispatch_fn: Callable[[str, dict], Any]):
        """
        Args:
            dispatch_fn: Async function to dispatch tool calls (from tools.py)
        """
        self._dispatch = dispatch_fn
        self._schedules: list[WarmingSchedule] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # Initialize default schedules
        self._init_default_schedules()
    
    def _init_default_schedules(self):
        """Set up default warming schedules based on common patterns."""
        
        # =====================================================================
        # PCG CONTEXT (Always warm — this is Nova's core memory)
        # =====================================================================
        
        # Pre-warm PCG identity + preferences every 5 min (very stable, cheap)
        self._schedules.extend([
            WarmingSchedule(
                name="pcg_identity",
                tool_name="recall_memory",
                args={"query": "user identity name roles"},
                hours=list(range(24)),  # Every hour
                minutes_before=0,
            ),
            WarmingSchedule(
                name="pcg_preferences",
                tool_name="recall_memory",
                args={"query": "preferences communication work health"},
                hours=list(range(24)),
                minutes_before=0,
            ),
            WarmingSchedule(
                name="pcg_goals",
                tool_name="recall_memory",
                args={"query": "goals current projects"},
                hours=[7, 12, 18],  # 3x daily
                minutes_before=0,
            ),
        ])
        
        # =====================================================================
        # CIRCADIAN PATTERNS (Time of Day)
        # =====================================================================
        
        # Morning briefing: Weather + Calendar + Email
        self._schedules.extend([
            WarmingSchedule(
                name="morning_weather",
                tool_name="get_weather",
                args={"location": USER_LOCATION},
                hours=[6, 7, 8],  # Pre-warm before typical wake times
                minutes_before=10,
            ),
            WarmingSchedule(
                name="morning_calendar",
                tool_name="check_studio",
                args={"studio": "calendar", "action": "briefing"},
                hours=[6, 7, 8],
                minutes_before=10,
            ),
            WarmingSchedule(
                name="morning_email",
                tool_name="check_studio",
                args={"studio": "email", "action": "briefing"},
                hours=[7, 8, 9],
                minutes_before=5,
            ),
        ])
        
        # Lunch time: Weather update (for outdoor plans)
        self._schedules.append(
            WarmingSchedule(
                name="lunch_weather",
                tool_name="get_weather",
                args={"location": USER_LOCATION},
                hours=[11, 12],
                minutes_before=15,
            )
        )
        
        # Evening wind-down: Tomorrow's calendar
        self._schedules.append(
            WarmingSchedule(
                name="evening_tomorrow",
                tool_name="check_studio",
                args={"studio": "calendar", "action": "briefing", "query": "tomorrow"},
                hours=[20, 21],  # 8-9 PM
                minutes_before=0,
            )
        )
        
        # =====================================================================
        # WEEKLY PATTERNS (Day of Week)
        # =====================================================================
        
        # Weekday commute: Traffic/weather before typical departure
        self._schedules.append(
            WarmingSchedule(
                name="weekday_commute_weather",
                tool_name="get_weather",
                args={"location": USER_LOCATION},
                hours=[7, 8],
                days=[0, 1, 2, 3, 4],  # Mon-Fri
                minutes_before=30,
            )
        )
        
        # Weekend morning: Relaxed briefing time
        self._schedules.extend([
            WarmingSchedule(
                name="weekend_weather",
                tool_name="get_weather",
                args={"location": USER_LOCATION},
                hours=[9, 10],  # Later wake time
                days=[5, 6],  # Sat-Sun
                minutes_before=10,
            ),
            WarmingSchedule(
                name="weekend_calendar",
                tool_name="check_studio",
                args={"studio": "calendar", "action": "briefing"},
                hours=[9, 10],
                days=[5, 6],
                minutes_before=10,
            ),
        ])
        
        # Monday morning: Week overview
        self._schedules.append(
            WarmingSchedule(
                name="monday_week_overview",
                tool_name="check_studio",
                args={"studio": "calendar", "action": "briefing", "query": "this week"},
                hours=[7, 8],
                days=[0],  # Monday only
                minutes_before=15,
            )
        )
        
        # Friday afternoon: Weekend weather
        self._schedules.append(
            WarmingSchedule(
                name="friday_weekend_weather",
                tool_name="get_weather",
                args={"location": USER_LOCATION, "forecast": True},
                hours=[15, 16],  # 3-4 PM
                days=[4],  # Friday only
                minutes_before=0,
            )
        )
        
        # =====================================================================
        # INFRASTRUCTURE (Always useful)
        # =====================================================================
        
        # Homelab health: Check periodically
        self._schedules.append(
            WarmingSchedule(
                name="homelab_health",
                tool_name="service_health_check",
                args={"container": "all"},
                hours=[8, 14, 20],  # 3x daily
                minutes_before=0,
            )
        )
        
        logger.info(f"Initialized {len(self._schedules)} warming schedules")
    
    def add_schedule(self, schedule: WarmingSchedule):
        """Add a custom warming schedule."""
        self._schedules.append(schedule)
        logger.info(f"Added warming schedule: {schedule.name}")
    
    def remove_schedule(self, name: str) -> bool:
        """Remove a schedule by name."""
        for i, s in enumerate(self._schedules):
            if s.name == name:
                del self._schedules[i]
                logger.info(f"Removed warming schedule: {name}")
                return True
        return False
    
    async def start(self):
        """Start the warming service."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._warming_loop())
        logger.info("Contextual warming service started")
    
    async def stop(self):
        """Stop the warming service."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Contextual warming service stopped")
    
    async def _warming_loop(self):
        """Main loop that checks schedules and triggers warming."""
        while self._running:
            try:
                now = datetime.now(USER_TZ)
                
                for schedule in self._schedules:
                    if schedule.should_run(now):
                        await self._execute_warming(schedule, now)
                
                # Check every minute
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Warming loop error: {e}")
                await asyncio.sleep(60)
    
    async def _execute_warming(self, schedule: WarmingSchedule, now: datetime):
        """Execute a warming schedule."""
        try:
            logger.info(f"[Warming] Executing {schedule.name}: {schedule.tool_name}")
            
            # Dispatch the tool call
            result = await self._dispatch(schedule.tool_name, schedule.args)
            
            schedule.last_run = now
            logger.info(f"[Warming] {schedule.name} completed, result cached")
            
        except Exception as e:
            logger.error(f"[Warming] {schedule.name} failed: {e}")
    
    # =========================================================================
    # SEASONAL AWARENESS
    # =========================================================================
    
    def get_season(self, date: datetime = None) -> str:
        """Get the current season for the Northern Hemisphere."""
        if date is None:
            date = datetime.now(USER_TZ)
        
        month = date.month
        if month in [12, 1, 2]:
            return "winter"
        elif month in [3, 4, 5]:
            return "spring"
        elif month in [6, 7, 8]:
            return "summer"
        else:
            return "fall"
    
    def get_seasonal_context(self) -> dict:
        """Get seasonal context for cache warming decisions."""
        now = datetime.now(USER_TZ)
        season = self.get_season(now)
        
        context = {
            "season": season,
            "month": now.month,
            "is_dst": now.dst() is not None and now.dst().total_seconds() > 0,
            "daylight_hours": self._estimate_daylight_hours(now),
        }
        
        # Seasonal suggestions
        if season == "winter":
            context["weather_concerns"] = ["ice", "snow", "cold"]
            context["suggested_queries"] = ["heating", "road conditions"]
        elif season == "spring":
            context["weather_concerns"] = ["rain", "allergies", "storms"]
            context["suggested_queries"] = ["pollen count", "rain forecast"]
        elif season == "summer":
            context["weather_concerns"] = ["heat", "uv index", "storms"]
            context["suggested_queries"] = ["heat advisory", "pool weather"]
        else:  # fall
            context["weather_concerns"] = ["rain", "temperature drops"]
            context["suggested_queries"] = ["first freeze", "fall colors"]
        
        return context
    
    def _estimate_daylight_hours(self, date: datetime) -> float:
        """Rough estimate of daylight hours based on month (for Dallas latitude)."""
        # Simplified model for ~32°N latitude
        month_daylight = {
            1: 10.2, 2: 11.0, 3: 12.0, 4: 13.0, 5: 13.8, 6: 14.2,
            7: 14.0, 8: 13.3, 9: 12.3, 10: 11.3, 11: 10.5, 12: 10.0
        }
        return month_daylight.get(date.month, 12.0)
    
    # =========================================================================
    # TREND DETECTION
    # =========================================================================
    
    async def detect_query_trends(self, query_history: list[dict]) -> list[dict]:
        """
        Analyze query history to detect emerging trends.
        
        Args:
            query_history: List of {tool_name, args, timestamp} dicts
        
        Returns:
            List of detected trends with warming recommendations
        """
        trends = []
        
        # Group queries by tool and time window
        from collections import defaultdict
        hourly_counts = defaultdict(lambda: defaultdict(int))
        
        for query in query_history:
            ts = query.get("timestamp", 0)
            dt = datetime.fromtimestamp(ts, tz=USER_TZ)
            hour = dt.hour
            tool = query.get("tool_name", "")
            hourly_counts[tool][hour] += 1
        
        # Find tools with strong hourly patterns
        for tool, hours in hourly_counts.items():
            total = sum(hours.values())
            if total < 5:
                continue
            
            # Find peak hours (>20% of queries)
            peak_hours = [h for h, c in hours.items() if c / total > 0.2]
            
            if peak_hours:
                trends.append({
                    "tool": tool,
                    "peak_hours": peak_hours,
                    "total_queries": total,
                    "recommendation": f"Pre-warm {tool} at hours {peak_hours}",
                })
        
        return trends
    
    # =========================================================================
    # STATUS & MANAGEMENT
    # =========================================================================
    
    def get_status(self) -> dict:
        """Get current warming service status."""
        now = datetime.now(USER_TZ)
        
        upcoming = []
        for schedule in self._schedules:
            if schedule.enabled:
                upcoming.append({
                    "name": schedule.name,
                    "tool": schedule.tool_name,
                    "hours": schedule.hours,
                    "days": schedule.days,
                    "last_run": schedule.last_run.isoformat() if schedule.last_run else None,
                })
        
        return {
            "running": self._running,
            "schedules_count": len(self._schedules),
            "enabled_count": sum(1 for s in self._schedules if s.enabled),
            "current_time": now.isoformat(),
            "season": self.get_season(now),
            "schedules": upcoming[:10],  # First 10
        }
    
    def get_next_warmings(self, hours_ahead: int = 2) -> list[dict]:
        """Get warmings scheduled for the next N hours."""
        now = datetime.now(USER_TZ)
        upcoming = []
        
        for schedule in self._schedules:
            if not schedule.enabled:
                continue
            
            for hour in schedule.hours:
                # Calculate target time (minutes_before the hour)
                # If minutes_before is 0, run at the start of the hour
                if schedule.minutes_before == 0:
                    target_minute = 0
                    target_hour = hour
                else:
                    target_minute = 60 - schedule.minutes_before
                    target_hour = (hour - 1) % 24
                
                target_time = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                if target_time < now:
                    target_time += timedelta(days=1)
                
                if (target_time - now).total_seconds() <= hours_ahead * 3600:
                    # Check day constraint
                    if schedule.days is None or target_time.weekday() in schedule.days:
                        upcoming.append({
                            "name": schedule.name,
                            "tool": schedule.tool_name,
                            "scheduled_for": target_time.isoformat(),
                            "minutes_until": int((target_time - now).total_seconds() / 60),
                        })
        
        upcoming.sort(key=lambda x: x["minutes_until"])
        return upcoming


# Global instance (initialized by bot.py or text_chat.py)
_warming_service: Optional[ContextualWarmingService] = None


async def init_warming_service(dispatch_fn: Callable):
    """Initialize and start the warming service."""
    global _warming_service
    if _warming_service is not None:
        return _warming_service
    
    _warming_service = ContextualWarmingService(dispatch_fn)
    await _warming_service.start()
    
    # Immediately pre-warm critical data on startup
    await prewarm_on_startup(dispatch_fn)
    
    return _warming_service


async def prewarm_on_startup(dispatch_fn: Callable):
    """Pre-warm cache with critical data immediately on service startup.
    
    This ensures Nova can respond instantly to basic queries without
    waiting for the first scheduled warming cycle.
    """
    startup_warms = [
        ("recall_memory", {"query": "user identity name roles"}),
        ("recall_memory", {"query": "preferences communication work health"}),
        ("get_weather", {"location": USER_LOCATION}),
        ("service_health_check", {"container": "all"}),
    ]
    
    logger.info(f"[Startup Pre-warm] Warming {len(startup_warms)} critical cache entries...")
    
    for tool_name, args in startup_warms:
        try:
            result = await dispatch_fn(tool_name, args)
            logger.info(f"[Startup Pre-warm] ✅ {tool_name} cached")
        except Exception as e:
            logger.warning(f"[Startup Pre-warm] ❌ {tool_name} failed: {e}")
    
    logger.info("[Startup Pre-warm] Complete — cache ready for instant responses")


def get_warming_service() -> Optional[ContextualWarmingService]:
    """Get the warming service instance."""
    return _warming_service


def get_warming_status() -> dict:
    """Get warming service status."""
    if _warming_service is None:
        return {"running": False, "message": "Warming service not initialized"}
    return _warming_service.get_status()
