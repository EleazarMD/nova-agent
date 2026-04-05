"""
Tesla SSE Client for Nova Agent

Subscribes to Tesla Relay SSE stream (port 18811) for real-time vehicle updates.
Caches vehicle location data for tools to access without API calls.

Usage:
    from nova.tesla_client import get_vehicle_location, refresh_vehicle_data
    
    # Get cached location (fast, no API call)
    lat, lon = await get_vehicle_location()
    
    # Force on-demand refresh (API call)
    location = await refresh_vehicle_data(vin)
"""

import asyncio
import json
import os
from typing import Optional, Dict, Any
from datetime import datetime
from loguru import logger
import aiohttp

# Tesla Relay configuration
TESLA_RELAY_URL = os.environ.get("TESLA_RELAY_URL", "http://100.108.41.22:18810")
# SSE stream is on the same port as REST API (FastAPI endpoint at /stream)
TESLA_RELAY_SSE_URL = os.environ.get("TESLA_RELAY_SSE_URL", "http://100.108.41.22:18810")

# In-memory vehicle location cache: vin -> {lat, lon, timestamp, full_data}
_vehicle_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = asyncio.Lock()

# SSE background task
_sse_task: Optional[asyncio.Task] = None
_user_id: str = "default"


async def _connect_sse():
    """
    Connect to Tesla Relay SSE stream and listen for vehicle updates.
    Runs as a background task.
    """
    global _vehicle_cache
    
    stream_url = f"{TESLA_RELAY_SSE_URL}/stream?user_id={_user_id}"
    logger.info(f"[TeslaSSE] Connecting to SSE stream: {stream_url}")
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(stream_url, headers={"Accept": "text/event-stream"}) as resp:
                    if resp.status != 200:
                        logger.warning(f"[TeslaSSE] SSE connection failed: {resp.status}")
                        await asyncio.sleep(30)  # Retry in 30 seconds
                        continue
                    
                    logger.info("[TeslaSSE] Connected to vehicle update stream")
                    
                    # Read SSE events
                    buffer = ""
                    async for chunk in resp.content:
                        chunk_str = chunk.decode('utf-8')
                        buffer += chunk_str
                        
                        # Parse SSE format
                        while "\n\n" in buffer:
                            event_text, buffer = buffer.split("\n\n", 1)
                            await _parse_sse_event(event_text)
                            
        except asyncio.CancelledError:
            logger.info("[TeslaSSE] SSE task cancelled")
            raise
        except Exception as e:
            logger.error(f"[TeslaSSE] SSE connection error: {e}")
            await asyncio.sleep(30)  # Retry in 30 seconds


async def _parse_sse_event(event_text: str):
    """Parse an SSE event and update cache if it's a vehicle_data event."""
    global _vehicle_cache
    
    lines = event_text.strip().split("\n")
    event_type = None
    data_json = None
    
    for line in lines:
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_json = line[5:].strip()
    
    if event_type == "vehicle_data" and data_json:
        try:
            data = json.loads(data_json)
            vin = data.get("vin")
            lat = data.get("latitude")
            lon = data.get("longitude")
            
            if vin and lat is not None and lon is not None:
                async with _cache_lock:
                    _vehicle_cache[vin] = {
                        "latitude": lat,
                        "longitude": lon,
                        "timestamp": datetime.utcnow().isoformat(),
                        "display_name": data.get("display_name"),
                        "battery_level": data.get("battery_level"),
                        "battery_range": data.get("battery_range"),
                        "charging_state": data.get("charging_state"),
                        "speed": data.get("speed"),
                        "heading": data.get("heading"),
                        "full_data": data
                    }
                logger.info(f"[TeslaSSE] Updated cache: {vin} -> {lat}, {lon}")
                
        except json.JSONDecodeError:
            logger.warning(f"[TeslaSSE] Failed to parse event data: {data_json}")


async def start_tesla_client(user_id: str = "default"):
    """
    Start the Tesla SSE client as a background task.
    Call this once when Nova starts.
    """
    global _sse_task, _user_id
    
    _user_id = user_id
    
    if _sse_task is None or _sse_task.done():
        _sse_task = asyncio.create_task(_connect_sse())
        logger.info("[TeslaSSE] Tesla SSE client started")
    else:
        logger.debug("[TeslaSSE] Tesla SSE client already running")


async def stop_tesla_client():
    """Stop the Tesla SSE client."""
    global _sse_task
    
    if _sse_task and not _sse_task.done():
        _sse_task.cancel()
        try:
            await _sse_task
        except asyncio.CancelledError:
            pass
        logger.info("[TeslaSSE] Tesla SSE client stopped")


async def get_vehicle_location(vin: Optional[str] = None) -> Optional[tuple[float, float]]:
    """
    Get cached vehicle location.
    
    Args:
        vin: Vehicle VIN. If None, returns location of first cached vehicle.
        
    Returns:
        (latitude, longitude) tuple or None if not cached
    """
    async with _cache_lock:
        if vin:
            vehicle = _vehicle_cache.get(vin)
            if vehicle:
                return (vehicle["latitude"], vehicle["longitude"])
            return None
        
        # Return first vehicle's location if no VIN specified
        for v in _vehicle_cache.values():
            return (v["latitude"], v["longitude"])
        
    return None


async def get_cached_vehicle_data(vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get full cached vehicle data.
    
    Args:
        vin: Vehicle VIN. If None, returns first cached vehicle.
        
    Returns:
        Dict with vehicle data or None if not cached
    """
    async with _cache_lock:
        if vin:
            return _vehicle_cache.get(vin)
        
        # Return first vehicle if no VIN specified
        for v in _vehicle_cache.values():
            return v.copy()
    
    return None


async def refresh_vehicle_data(vin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    On-demand refresh of vehicle data from Tesla API.
    This triggers an API call and updates the cache.
    
    Args:
        vin: Vehicle VIN. If None, uses first available vehicle.
        
    Returns:
        Dict with location data or None if refresh failed
    """
    try:
        # If no VIN provided, get list of vehicles first
        if not vin:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{TESLA_RELAY_URL}/vehicles?user_id={_user_id}") as resp:
                    if resp.status == 200:
                        vehicles = await resp.json()
                        if vehicles:
                            vin = vehicles[0].get("vin")
                        else:
                            logger.warning("[TeslaClient] No vehicles found for refresh")
                            return None
                    else:
                        logger.error(f"[TeslaClient] Failed to get vehicles: {resp.status}")
                        return None
        
        if not vin:
            return None
        
        # Call refresh endpoint
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TESLA_RELAY_URL}/vehicles/{vin}/refresh?user_id={_user_id}"
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    location = result.get("location", {})
                    lat = location.get("latitude")
                    lon = location.get("longitude")
                    
                    if lat is not None and lon is not None:
                        # Update cache
                        async with _cache_lock:
                            if vin not in _vehicle_cache:
                                _vehicle_cache[vin] = {}
                            _vehicle_cache[vin]["latitude"] = lat
                            _vehicle_cache[vin]["longitude"] = lon
                            _vehicle_cache[vin]["timestamp"] = datetime.utcnow().isoformat()
                        
                        logger.info(f"[TeslaClient] On-demand refresh: {vin} -> {lat}, {lon}")
                        return {"vin": vin, "latitude": lat, "longitude": lon}
                else:
                    text = await resp.text()
                    logger.error(f"[TeslaClient] Refresh failed: {resp.status} {text}")
                    return None
                    
    except Exception as e:
        logger.error(f"[TeslaClient] Error during refresh: {e}")
        return None


async def list_cached_vehicles() -> list:
    """List all vehicles in the cache."""
    async with _cache_lock:
        return [
            {
                "vin": vin,
                "display_name": data.get("display_name"),
                "latitude": data.get("latitude"),
                "longitude": data.get("longitude"),
                "timestamp": data.get("timestamp"),
                "battery_level": data.get("battery_level"),
            }
            for vin, data in _vehicle_cache.items()
        ]
