"""
Tesla Stream Monitor - Real-time vehicle event monitoring via SSE

Monitors Tesla Relay SSE stream for vehicle events and triggers notifications.
"""

import os
import asyncio
import aiohttp
import json
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger

TESLA_RELAY_URL = os.environ.get("TESLA_RELAY_URL", "http://localhost:18810")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8404")

# Active monitors storage
_active_monitors: Dict[str, "MonitorConfig"] = {}
_sse_task: Optional[asyncio.Task] = None
_last_vehicle_states: Dict[str, dict] = {}


@dataclass
class MonitorConfig:
    """Configuration for an active monitor."""
    monitor_id: str
    user_id: str
    event_type: str
    vehicle_vin: Optional[str] = None
    location_name: Optional[str] = None
    location_coords: Optional[tuple] = None  # (lat, lon, radius_miles)
    notify: bool = True
    created_at: datetime = field(default_factory=datetime.now)


async def handle_tesla_stream_monitor(
    user_id: str,
    action: str,
    event_type: str = "all",
    vehicle_identifier: Optional[str] = None,
    location_name: Optional[str] = None,
    notify: bool = True,
) -> str:
    """
    Unified Tesla stream monitor handler.
    
    Args:
        user_id: User identifier
        action: Operation (start, stop, status, list)
        event_type: Event type to monitor
        vehicle_identifier: VIN, model name, or display name
        location_name: Location name for arrival/departure
        notify: Send push notifications
    """
    if action == "start":
        return await _start_monitor(
            user_id=user_id,
            event_type=event_type,
            vehicle_identifier=vehicle_identifier,
            location_name=location_name,
            notify=notify,
        )
    elif action == "stop":
        return await _stop_monitor(
            user_id=user_id,
            event_type=event_type,
            vehicle_identifier=vehicle_identifier,
        )
    elif action == "status":
        return await _get_status(user_id)
    elif action == "list":
        return _list_event_types()
    else:
        return f"Unknown action: {action}. Valid actions: start, stop, status, list"


def _list_event_types() -> str:
    """List available event types."""
    return """Available Tesla event types:
- charging_complete: Notifies when charging finishes (battery > 90%)
- location_change: Notifies when vehicle location changes significantly
- sentry_alert: Notifies when sentry mode is triggered
- arrival: Notifies when vehicle arrives at a named location
- departure: Notifies when vehicle leaves a named location
- all: Monitor all event types"""


async def _start_monitor(
    user_id: str,
    event_type: str,
    vehicle_identifier: Optional[str],
    location_name: Optional[str],
    notify: bool,
) -> str:
    """Start monitoring for events."""
    global _sse_task
    
    # Resolve vehicle identifier to VIN if provided
    vehicle_vin = None
    if vehicle_identifier:
        vehicle_vin = await _resolve_vehicle(user_id, vehicle_identifier)
        if not vehicle_vin:
            return f"Could not find vehicle matching '{vehicle_identifier}'"
    
    # Resolve location name to coordinates if provided
    location_coords = None
    if location_name and event_type in ("arrival", "departure", "all"):
        location_coords = await _resolve_location(location_name)
        if not location_coords:
            return f"Could not resolve location '{location_name}'. Known locations: home, work"
    
    # Create monitor config
    monitor_id = f"{user_id}:{event_type}:{vehicle_vin or 'all'}"
    config = MonitorConfig(
        monitor_id=monitor_id,
        user_id=user_id,
        event_type=event_type,
        vehicle_vin=vehicle_vin,
        location_name=location_name,
        location_coords=location_coords,
        notify=notify,
    )
    
    _active_monitors[monitor_id] = config
    
    # Start SSE listener if not running
    if _sse_task is None or _sse_task.done():
        _sse_task = asyncio.create_task(_sse_listener(user_id))
        logger.info(f"Tesla SSE listener started for user {user_id}")
    
    return f"Started monitoring for {event_type} events{' on ' + vehicle_identifier if vehicle_identifier else ''}. I'll notify you when it triggers."


async def _stop_monitor(
    user_id: str,
    event_type: Optional[str],
    vehicle_identifier: Optional[str],
) -> str:
    """Stop monitoring for events."""
    global _sse_task
    
    # Build monitor ID pattern
    vehicle_vin = None
    if vehicle_identifier:
        vehicle_vin = await _resolve_vehicle(user_id, vehicle_identifier)
    
    # Find and remove matching monitors
    stopped = []
    keys_to_remove = []
    
    for key, config in _active_monitors.items():
        if config.user_id != user_id:
            continue
        if event_type and config.event_type != event_type and config.event_type != "all":
            continue
        if vehicle_vin and config.vehicle_vin != vehicle_vin:
            continue
        
        keys_to_remove.append(key)
        stopped.append(config.event_type)
    
    for key in keys_to_remove:
        del _active_monitors[key]
    
    if not stopped:
        return "No active monitors found matching your criteria."
    
    # Stop SSE task if no monitors remain
    if not _active_monitors:
        if _sse_task and not _sse_task.done():
            _sse_task.cancel()
            _sse_task = None
            logger.info("Tesla SSE listener stopped (no active monitors)")
    
    return f"Stopped monitoring for: {', '.join(set(stopped))}"


async def _get_status(user_id: str) -> str:
    """Get current monitoring status."""
    user_monitors = [c for c in _active_monitors.values() if c.user_id == user_id]
    
    if not user_monitors:
        return "No active Tesla monitors. Say 'monitor my Tesla for charging' to start."
    
    lines = ["Active Tesla monitors:"]
    for config in user_monitors:
        vehicle = config.vehicle_vin or "all vehicles"
        location = f" at {config.location_name}" if config.location_name else ""
        notify_status = "with notifications" if config.notify else "silent"
        lines.append(f"- {config.event_type} on {vehicle}{location} ({notify_status})")
    
    return "\n".join(lines)


async def _resolve_vehicle(user_id: str, identifier: str) -> Optional[str]:
    """Resolve vehicle identifier to VIN."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DASHBOARD_URL}/api/tesla/vehicles") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                vehicles = data.get("response", [])
                
        identifier_lower = identifier.lower()
        
        for v in vehicles:
            vin = v.get("vin", "")
            display_name = v.get("display_name", "").lower()
            model = _get_model_from_vin(vin).lower()
            
            if vin.lower() == identifier_lower:
                return vin
            if identifier_lower in display_name or display_name in identifier_lower:
                return vin
            if identifier_lower in model or model in identifier_lower:
                return vin
        
        return None
    except Exception as e:
        logger.error(f"Failed to resolve vehicle: {e}")
        return None


def _get_model_from_vin(vin: str) -> str:
    """Extract model from VIN."""
    if not vin or len(vin) < 4:
        return "Unknown"
    model_char = vin[3]
    return {"3": "Model 3", "S": "Model S", "X": "Model X", "Y": "Model Y"}.get(model_char.upper(), f"Model {model_char}")


async def _resolve_location(location_name: str) -> Optional[tuple]:
    """Resolve location name to coordinates."""
    # Known locations - could be fetched from user preferences
    known_locations = {
        "home": (29.9988, -95.1694, 0.5),  # Humble, TX with 0.5 mile radius
        "work": (29.7604, -95.3698, 0.5),  # Houston downtown
    }
    
    return known_locations.get(location_name.lower())


async def _sse_listener(user_id: str):
    """Listen to Tesla Relay SSE stream and detect events."""
    global _last_vehicle_states
    
    url = f"{TESLA_RELAY_URL}/stream?user_id={user_id}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Tesla SSE connection failed: {resp.status}")
                    return
                
                logger.info(f"Connected to Tesla SSE stream for user {user_id}")
                
                async for line in resp.content:
                    if not line:
                        continue
                    
                    try:
                        line_str = line.decode("utf-8").strip()
                        if not line_str or line_str.startswith(":"):
                            continue
                        
                        if line_str.startswith("data:"):
                            data_str = line_str[5:].strip()
                            event_data = json.loads(data_str)
                            await _process_sse_event(user_id, event_data)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"SSE event processing error: {e}")
                        
    except asyncio.CancelledError:
        logger.info("Tesla SSE listener cancelled")
    except Exception as e:
        logger.error(f"Tesla SSE listener error: {e}")


async def _process_sse_event(user_id: str, event_data: dict):
    """Process SSE event and check for monitored conditions."""
    vin = event_data.get("vin")
    if not vin:
        return
    
    # Get previous state
    prev_state = _last_vehicle_states.get(vin, {})
    current_state = event_data
    
    # Check each active monitor
    for config in list(_active_monitors.values()):
        if config.user_id != user_id:
            continue
        if config.vehicle_vin and config.vehicle_vin != vin:
            continue
        
        event_triggered = await _check_event(
            config.event_type,
            prev_state,
            current_state,
            config.location_coords,
        )
        
        if event_triggered:
            await _trigger_notification(config, event_triggered, current_state)
    
    # Update state cache
    _last_vehicle_states[vin] = current_state


async def _check_event(
    event_type: str,
    prev_state: dict,
    current_state: dict,
    location_coords: Optional[tuple],
) -> Optional[str]:
    """Check if an event triggered. Returns event description or None."""
    
    # Charging complete
    if event_type in ("charging_complete", "all"):
        prev_charging = prev_state.get("charge_state", {}).get("charging_state")
        curr_charging = current_state.get("charge_state", {}).get("charging_state")
        battery = current_state.get("charge_state", {}).get("battery_level", 0)
        
        if prev_charging == "Charging" and curr_charging == "Stopped" and battery > 90:
            return f"Charging complete at {battery}%"
    
    # Location change
    if event_type in ("location_change", "all"):
        prev_lat = prev_state.get("drive_state", {}).get("latitude")
        prev_lon = prev_state.get("drive_state", {}).get("longitude")
        curr_lat = current_state.get("drive_state", {}).get("latitude")
        curr_lon = current_state.get("drive_state", {}).get("longitude")
        
        if prev_lat and prev_lon and curr_lat and curr_lon:
            # Check if moved > 0.001 degrees (~100m)
            if abs(curr_lat - prev_lat) > 0.001 or abs(curr_lon - prev_lon) > 0.001:
                return f"Vehicle moved to {curr_lat:.4f}, {curr_lon:.4f}"
    
    # Sentry alert
    if event_type in ("sentry_alert", "all"):
        prev_sentry = prev_state.get("vehicle_state", {}).get("sentry_mode")
        curr_sentry = current_state.get("vehicle_state", {}).get("sentry_mode")
        locked = current_state.get("vehicle_state", {}).get("locked", False)
        
        if not prev_sentry and curr_sentry and locked:
            return "Sentry mode triggered"
    
    # Arrival/Departure
    if event_type in ("arrival", "departure", "all") and location_coords:
        target_lat, target_lon, radius = location_coords
        curr_lat = current_state.get("drive_state", {}).get("latitude")
        curr_lon = current_state.get("drive_state", {}).get("longitude")
        prev_lat = prev_state.get("drive_state", {}).get("latitude")
        prev_lon = prev_state.get("drive_state", {}).get("longitude")
        
        if curr_lat and curr_lon:
            # Calculate distance (simplified)
            curr_in_range = _in_geofence(curr_lat, curr_lon, target_lat, target_lon, radius)
            prev_in_range = prev_lat and prev_lon and _in_geofence(prev_lat, prev_lon, target_lat, target_lon, radius)
            
            if event_type in ("arrival", "all") and not prev_in_range and curr_in_range:
                return "Vehicle arrived at destination"
            
            if event_type in ("departure", "all") and prev_in_range and not curr_in_range:
                return "Vehicle departed from location"
    
    return None


def _in_geofence(lat1: float, lon1: float, lat2: float, lon2: float, radius_miles: float) -> bool:
    """Check if point is within geofence radius."""
    # Simplified distance calculation (works for small distances)
    import math
    lat_diff = abs(lat1 - lat2)
    lon_diff = abs(lon1 - lon2)
    # Approximate: 1 degree ~ 69 miles
    distance = math.sqrt(lat_diff**2 + lon_diff**2) * 69
    return distance <= radius_miles


async def _trigger_notification(config: MonitorConfig, event_desc: str, vehicle_state: dict):
    """Trigger notification for detected event."""
    logger.info(f"Tesla event detected: {event_desc} (monitor: {config.monitor_id})")
    
    if not config.notify:
        return
    
    # Send push notification via Dashboard API
    try:
        display_name = vehicle_state.get("display_name", "Tesla")
        
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{DASHBOARD_URL}/api/notifications/send",
                json={
                    "user_id": config.user_id,
                    "title": f"Tesla Alert: {display_name}",
                    "body": event_desc,
                    "category": "TESLA_EVENT",
                    "data": {
                        "event_type": config.event_type,
                        "vin": config.vehicle_vin,
                    }
                }
            )
            logger.info(f"Push notification sent: {event_desc}")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


# ---------------------------------------------------------------------------
# Exports for tools.py
# ---------------------------------------------------------------------------

__all__ = ["handle_tesla_stream_monitor"]
