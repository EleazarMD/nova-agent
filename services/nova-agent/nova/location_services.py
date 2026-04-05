"""
Location Services Integration for Nova Agent.

Provides:
- GPS location tracking from iOS client via RTVI data channel
- Location-aware cache warming (weather, nearby places)
- Proximity-based reminders (geofencing)
- Location metadata in conversation context

Location data flows:
iOS GPS → WebRTC data channel → Nova location store → 
  ├→ Cache warming (weather for current location)
  ├→ Proximity trigger checks
  ├→ Tesla mirror (for trip cards)
  └→ Conversation context ("nearby" queries)
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Callable, Any
from loguru import logger

from nova.events import Event, event_bus


@dataclass
class GeoLocation:
    """Represents a geographic location."""
    latitude: float
    longitude: float
    accuracy: float = 0.0  # meters
    altitude: Optional[float] = None
    timestamp: Optional[datetime] = None
    source: str = "unknown"  # gps, network, ip, etc.
    
    def is_valid(self) -> bool:
        """Check if coordinates are valid."""
        return -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180
    
    def is_fresh(self, max_age_seconds: int = 300) -> bool:
        """Check if location is fresh (default 5 minutes)."""
        if not self.timestamp:
            return False
        age = (datetime.now() - self.timestamp).total_seconds()
        return age <= max_age_seconds


@dataclass
class ProximityTrigger:
    """A geofence trigger for location-based reminders."""
    id: str
    name: str
    center: GeoLocation
    radius_meters: float
    trigger_on: str = "enter"  # enter, exit, or both
    callback: Optional[Callable] = None
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    last_triggered: Optional[datetime] = None
    cooldown_minutes: int = 30  # Don't re-trigger within this time


class LocationService:
    """
    Manages user location and location-based features.
    
    Features:
    - Store latest location from iOS
    - Location-aware speculative cache warming
    - Proximity triggers (geofencing)
    - Distance calculations
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
        self._current_location: Optional[GeoLocation] = None
        self._location_history: List[GeoLocation] = []  # Last 100 locations
        self._proximity_triggers: Dict[str, ProximityTrigger] = {}
        self._cache_warming_fn: Optional[Callable] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._is_monitoring = False
    
    def set_cache_warming_fn(self, fn: Callable[[str, Dict], None]):
        """Set function for cache warming based on location."""
        self._cache_warming_fn = fn
    
    # -------------------------------------------------------------------------
    # Location Updates
    # -------------------------------------------------------------------------
    
    async def update_location(
        self,
        latitude: float,
        longitude: float,
        accuracy: float = 0.0,
        altitude: Optional[float] = None,
        source: str = "gps",
    ) -> GeoLocation:
        """
        Update user's current location.
        
        Called when iOS sends location via RTVI data channel.
        """
        location = GeoLocation(
            latitude=latitude,
            longitude=longitude,
            accuracy=accuracy,
            altitude=altitude,
            timestamp=datetime.now(),
            source=source,
        )
        
        if not location.is_valid():
            logger.warning(f"Invalid location received: {latitude}, {longitude}")
            return self._current_location
        
        # Store location
        previous_location = self._current_location
        self._current_location = location
        self._location_history.append(location)
        
        # Trim history
        if len(self._location_history) > 100:
            self._location_history = self._location_history[-100:]
        
        logger.info(
            f"Location updated for {self.user_id}: "
            f"{latitude:.4f}, {longitude:.4f} (±{accuracy:.0f}m) via {source}"
        )
        
        # Check if location changed significantly (>100m)
        if previous_location:
            distance = self._calculate_distance(previous_location, location)
            if distance > 100:
                logger.info(f"Significant location change: {distance:.0f}m")
                await self._on_significant_location_change(location)
        
        # Check proximity triggers
        await self._check_proximity_triggers(location)
        
        return location
    
    async def _on_significant_location_change(self, location: GeoLocation):
        """Handle significant location changes (>100m)."""
        # Warm cache for new location
        if self._cache_warming_fn:
            await self._warm_location_cache(location)
        
        # Emit event for other systems
        await event_bus.emit(Event(
            type="location.significant_change",
            user_id=self.user_id,
            data={
                "latitude": location.latitude,
                "longitude": location.longitude,
                "accuracy": location.accuracy,
            },
        ))
    
    async def _warm_location_cache(self, location: GeoLocation):
        """Warm speculative cache for current location."""
        if not self._cache_warming_fn:
            return
        
        # Weather for current location
        await self._cache_warming_fn(
            "knowledge.weather.current_location",
            {"lat": location.latitude, "lon": location.longitude},
        )
        
        logger.debug(f"Warmed location-aware cache for {self.user_id}")
    
    def get_current_location(self) -> Optional[GeoLocation]:
        """Get user's current location if fresh."""
        if self._current_location and self._current_location.is_fresh():
            return self._current_location
        return None
    
    def get_location_context(self) -> str:
        """Get location context for LLM prompt enrichment."""
        loc = self.get_current_location()
        if not loc:
            return ""
        
        return f"[User location: {loc.latitude:.4f}, {loc.longitude:.4f}]"
    
    # -------------------------------------------------------------------------
    # Proximity Triggers (Geofencing)
    # -------------------------------------------------------------------------
    
    def add_proximity_trigger(
        self,
        name: str,
        latitude: float,
        longitude: float,
        radius_meters: float = 100,
        trigger_on: str = "enter",
        callback: Optional[Callable] = None,
        data: Optional[Dict] = None,
    ) -> str:
        """
        Add a proximity trigger.
        
        Example:
            trigger_id = location_service.add_proximity_trigger(
                name="Grocery Store",
                latitude=32.7767,
                longitude=-96.7970,
                radius_meters=200,
                trigger_on="enter",
                callback=on_grocery_store_enter,
                data={"reminder": "Buy milk"},
            )
        """
        import uuid
        trigger_id = str(uuid.uuid4())[:8]
        
        trigger = ProximityTrigger(
            id=trigger_id,
            name=name,
            center=GeoLocation(latitude=latitude, longitude=longitude),
            radius_meters=radius_meters,
            trigger_on=trigger_on,
            callback=callback,
            data=data or {},
        )
        
        self._proximity_triggers[trigger_id] = trigger
        logger.info(f"Added proximity trigger '{name}' ({radius_meters}m radius)")
        
        return trigger_id
    
    def remove_proximity_trigger(self, trigger_id: str):
        """Remove a proximity trigger."""
        if trigger_id in self._proximity_triggers:
            trigger = self._proximity_triggers.pop(trigger_id)
            logger.info(f"Removed proximity trigger '{trigger.name}'")
    
    async def _check_proximity_triggers(self, location: GeoLocation):
        """Check all proximity triggers against current location."""
        for trigger in self._proximity_triggers.values():
            distance = self._calculate_distance(location, trigger.center)
            is_inside = distance <= trigger.radius_meters
            
            # Check cooldown
            if trigger.last_triggered:
                cooldown_elapsed = datetime.now() - trigger.last_triggered
                if cooldown_elapsed < timedelta(minutes=trigger.cooldown_minutes):
                    continue
            
            # Determine if trigger should fire
            should_trigger = False
            if trigger.trigger_on in ("enter", "both") and is_inside:
                should_trigger = True
            elif trigger.trigger_on in ("exit", "both") and not is_inside:
                should_trigger = True
            
            if should_trigger:
                await self._fire_proximity_trigger(trigger, location, distance)
    
    async def _fire_proximity_trigger(
        self,
        trigger: ProximityTrigger,
        location: GeoLocation,
        distance: float,
    ):
        """Fire a proximity trigger."""
        trigger.last_triggered = datetime.now()
        
        logger.info(
            f"Proximity trigger '{trigger.name}' fired: "
            f"{distance:.0f}m away (trigger: {trigger.trigger_on})"
        )
        
        # Call callback if provided
        if trigger.callback:
            try:
                await trigger.callback(trigger, location, distance)
            except Exception as e:
                logger.error(f"Proximity trigger callback error: {e}")
        
        # Emit event
        await event_bus.emit(Event(
            type="location.proximity_trigger",
            user_id=self.user_id,
            data={
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "distance_meters": distance,
                "trigger_on": trigger.trigger_on,
                "data": trigger.data,
            },
        ))
    
    # -------------------------------------------------------------------------
    # Distance Calculations
    # -------------------------------------------------------------------------
    
    @staticmethod
    def _calculate_distance(loc1: GeoLocation, loc2: GeoLocation) -> float:
        """
        Calculate distance between two points using Haversine formula.
        Returns distance in meters.
        """
        R = 6371000  # Earth's radius in meters
        
        lat1_rad = math.radians(loc1.latitude)
        lat2_rad = math.radians(loc2.latitude)
        delta_lat = math.radians(loc2.latitude - loc1.latitude)
        delta_lon = math.radians(loc2.longitude - loc1.longitude)
        
        a = (
            math.sin(delta_lat / 2) ** 2 +
            math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
    
    def distance_to(self, latitude: float, longitude: float) -> Optional[float]:
        """Calculate distance from current location to a point."""
        if not self._current_location:
            return None
        
        target = GeoLocation(latitude=latitude, longitude=longitude)
        return self._calculate_distance(self._current_location, target)
    
    # -------------------------------------------------------------------------
    # Monitoring
    # -------------------------------------------------------------------------
    
    async def start_monitoring(self, interval_seconds: int = 60):
        """Start periodic location monitoring."""
        if self._is_monitoring:
            return
        
        self._is_monitoring = True
        
        async def _monitor_loop():
            while self._is_monitoring:
                await asyncio.sleep(interval_seconds)
                
                # Check if location is stale
                if self._current_location and not self._current_location.is_fresh(600):
                    logger.warning(f"Location stale for {self.user_id}")
                    await event_bus.emit(Event(
                        type="location.stale",
                        user_id=self.user_id,
                        data={"last_updated": self._current_location.timestamp.isoformat()},
                    ))
        
        self._monitoring_task = asyncio.create_task(_monitor_loop())
        logger.info(f"Started location monitoring for {self.user_id}")
    
    def stop_monitoring(self):
        """Stop location monitoring."""
        self._is_monitoring = False
        if self._monitoring_task:
            self._monitoring_task.cancel()
            self._monitoring_task = None
        logger.info(f"Stopped location monitoring for {self.user_id}")


# Registry of location services per user
_location_services: Dict[str, LocationService] = {}


def get_location_service(user_id: str) -> LocationService:
    """Get or create location service for a user."""
    if user_id not in _location_services:
        _location_services[user_id] = LocationService(user_id)
    return _location_services[user_id]


async def handle_location_update(
    user_id: str,
    latitude: float,
    longitude: float,
    accuracy: float = 0.0,
    altitude: Optional[float] = None,
) -> GeoLocation:
    """
    Handle location update from iOS client.
    
    Called by bot.py when location message received via RTVI data channel.
    """
    service = get_location_service(user_id)
    return await service.update_location(
        latitude=latitude,
        longitude=longitude,
        accuracy=accuracy,
        altitude=altitude,
        source="ios_gps",
    )
