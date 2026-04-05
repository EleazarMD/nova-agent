"""
Nova Agent — EV Charging Station & Route Planning.

Integrates the NREL Alternative Fuel Stations API for:
- Finding nearest charging stations (including Tesla Superchargers)
- Finding stations along a driving route
- Listing available EV networks

API docs: https://developer.nrel.gov/docs/transportation/alt-fuel-stations-v1/
API key signup: https://developer.nrel.gov/signup/
"""

import os
from typing import Optional

import aiohttp
from loguru import logger

from nova.tesla_client import get_vehicle_location

NREL_API_KEY = os.environ.get("NREL_API_KEY", "DEMO_KEY")
NREL_BASE_URL = "https://developer.nrel.gov/api/alt-fuel-stations/v1"

# Common EV networks for filtering
EV_NETWORKS = {
    "tesla": "Tesla",
    "chargepoint": "ChargePoint Network",
    "electrify_america": "Electrify America",
    "evgo": "eVgo Network",
    "blink": "Blink Network",
}


async def _geocode(location: str) -> Optional[tuple[float, float]]:
    """Geocode a location string to (lat, lng) using Nominatim."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": location, "format": "json", "limit": 1}
    headers = {"User-Agent": "NovaAgent/1.0 (eleazar@homelab)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.warning(f"Geocoding failed for '{location}': {e}")
    return None


async def _nrel_get(endpoint: str, params: dict) -> dict:
    """Make a GET request to the NREL API."""
    params["api_key"] = NREL_API_KEY
    params["format"] = "JSON"
    url = f"{NREL_BASE_URL}/{endpoint}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"NREL API error {resp.status}: {text[:200]}")
                return {"error": f"NREL API returned HTTP {resp.status}: {text[:200]}"}
            return await resp.json()


def _format_station(s: dict) -> str:
    """Format a single station into a concise readable string."""
    name = s.get("station_name", "Unknown")
    network = s.get("ev_network", "Unknown")
    address = s.get("street_address", "")
    city = s.get("city", "")
    state = s.get("state", "")
    zip_code = s.get("zip", "")
    phone = s.get("station_phone", "")
    access = s.get("access_days_time", "")
    distance = s.get("distance", None)
    distance_str = f" ({distance:.1f} mi)" if distance else ""

    # Connector info
    connectors = s.get("ev_connector_types", [])
    connector_str = ", ".join(connectors) if connectors else "N/A"

    # Power levels
    dc_count = s.get("ev_dc_fast_num", 0) or 0
    l2_count = s.get("ev_level2_evse_num", 0) or 0
    l1_count = s.get("ev_level1_evse_num", 0) or 0
    charger_parts = []
    if dc_count:
        charger_parts.append(f"{dc_count} DC Fast")
    if l2_count:
        charger_parts.append(f"{l2_count} L2")
    if l1_count:
        charger_parts.append(f"{l1_count} L1")
    charger_str = ", ".join(charger_parts) if charger_parts else "N/A"

    # Pricing
    pricing = s.get("ev_pricing", "")
    pricing_str = f"\n  Pricing: {pricing}" if pricing else ""

    lines = [
        f"📍 {name}{distance_str}",
        f"  Network: {network}",
        f"  Address: {address}, {city}, {state} {zip_code}",
        f"  Chargers: {charger_str} | Connectors: {connector_str}",
    ]
    if access:
        lines.append(f"  Hours: {access}")
    if phone:
        lines.append(f"  Phone: {phone}")
    if pricing_str:
        lines.append(pricing_str)

    lat = s.get("latitude")
    lng = s.get("longitude")
    if lat and lng:
        lines.append(f"  Coords: {lat}, {lng}")

    return "\n".join(lines)


async def find_nearest_stations(
    location: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius: float = 25.0,
    network: Optional[str] = None,
    limit: int = 10,
    dc_fast_only: bool = False,
) -> str:
    """Find nearest EV charging stations to a location.
    
    Args:
        location: Address, city/state, or ZIP code.
        latitude/longitude: Coordinates (alternative to location).
        radius: Search radius in miles (max 500).
        network: Filter by network (tesla, chargepoint, electrify_america, evgo, blink).
        limit: Max results (default 10, max 50).
        dc_fast_only: Only show DC fast chargers.
    """
    # Resolve location to coordinates (NREL requires lat/lng)
    if location and (latitude is None or longitude is None):
        coords = await _geocode(location)
        if not coords:
            return f"Could not geocode '{location}'. Try providing latitude/longitude directly."
        latitude, longitude = coords
        logger.info(f"Geocoded '{location}' → {latitude}, {longitude}")

    # If no location or coordinates provided, try to get vehicle location from cache
    if latitude is None or longitude is None:
        vehicle_location = await get_vehicle_location()
        if vehicle_location:
            latitude, longitude = vehicle_location
            logger.info(f"Using cached vehicle location: {latitude}, {longitude}")
        else:
            return "Error: No location provided and no cached vehicle location available. Provide a location or ensure Tesla connection is active."

    params: dict = {
        "fuel_type": "ELEC",
        "latitude": latitude,
        "longitude": longitude,
        "radius": min(radius, 500.0),
        "limit": min(limit, 50),
    }

    if network:
        net_key = network.lower().replace(" ", "_")
        if net_key in EV_NETWORKS:
            params["ev_network"] = EV_NETWORKS[net_key]
        else:
            params["ev_network"] = network
        if net_key == "tesla":
            params["ev_connector_type"] = "TESLA"

    if dc_fast_only:
        params["ev_charging_level"] = "dc_fast"

    data = await _nrel_get("nearest.json", params)
    if "error" in data:
        return data["error"]

    stations = data.get("fuel_stations", [])
    total = data.get("total_results", 0)

    if not stations:
        loc_str = location or f"{latitude}, {longitude}"
        return f"No EV charging stations found within {radius} miles of {loc_str}."

    header = f"Found {total} EV charging station(s) within {radius} mi"
    if network:
        header += f" (network: {network})"
    header += f" — showing {len(stations)}:\n"

    formatted = [_format_station(s) for s in stations]
    return header + "\n\n".join(formatted)


async def find_stations_along_route(
    waypoints: str,
    radius: float = 10.0,
    network: Optional[str] = None,
    limit: int = 20,
    dc_fast_only: bool = False,
) -> str:
    """Find EV charging stations along a driving route.
    
    Args:
        waypoints: Semicolon-separated locations defining the route.
                   E.g. "Houston,TX;San Antonio,TX;Austin,TX"
        radius: Distance in miles from route to search (max 500).
        network: Filter by network name.
        limit: Max results (default 20, max 50).
        dc_fast_only: Only show DC fast chargers.
    """
    # Geocode each waypoint to lat,lng pairs for the route parameter
    wp_list = [w.strip() for w in waypoints.split(";") if w.strip()]
    if len(wp_list) < 2:
        return "Error: 'waypoints' needs at least 2 locations separated by semicolons."

    coord_pairs = []
    for wp in wp_list:
        coords = await _geocode(wp)
        if not coords:
            return f"Could not geocode waypoint '{wp}'. Use 'City, State' format."
        coord_pairs.append((coords[0], coords[1]))
        logger.info(f"Route waypoint '{wp}' → {coords[0]}, {coords[1]}")

    # NREL nearby-route requires WKT LINESTRING format: LINESTRING(lng lat, lng lat, ...)
    wkt_points = ",".join(f"{lng} {lat}" for lat, lng in coord_pairs)
    route_str = f"LINESTRING({wkt_points})"

    params: dict = {
        "fuel_type": "ELEC",
        "route": route_str,
        "radius": min(radius, 500.0),
        "limit": min(limit, 50),
    }

    if network:
        net_key = network.lower().replace(" ", "_")
        if net_key in EV_NETWORKS:
            params["ev_network"] = EV_NETWORKS[net_key]
        else:
            params["ev_network"] = network
        if net_key == "tesla":
            params["ev_connector_type"] = "TESLA"

    if dc_fast_only:
        params["ev_charging_level"] = "dc_fast"

    data = await _nrel_get("nearby-route.json", params)
    if "error" in data:
        return data["error"]

    stations = data.get("fuel_stations", [])
    total = data.get("total_results", 0)

    if not stations:
        return f"No EV charging stations found within {radius} miles of route: {waypoints}"

    header = f"Found {total} EV station(s) along route ({waypoints})"
    if network:
        header += f" [network: {network}]"
    header += f" within {radius} mi — showing {len(stations)}:\n"

    formatted = [_format_station(s) for s in stations]
    return header + "\n\n".join(formatted)


async def list_ev_networks() -> str:
    """List all available EV charging networks from NREL."""
    params: dict = {}
    data = await _nrel_get("networks.json", params)
    if "error" in data:
        return data["error"]

    networks = data.get("ev_network_ids", [])
    if not networks:
        return "No EV networks returned from NREL API."

    lines = [f"Available EV Charging Networks ({len(networks)}):"]
    for net in sorted(networks):
        lines.append(f"  • {net}")
    return "\n".join(lines)


async def handle_ev_route_planner(action: str, **kwargs) -> str:
    """Dispatch EV route planning actions.
    
    Actions:
        nearest — Find nearest charging stations to a location
        route — Find stations along a driving route (waypoints)
        networks — List all available EV charging networks
    """
    action = action.lower().strip()

    if action == "nearest":
        return await find_nearest_stations(
            location=kwargs.get("location"),
            latitude=kwargs.get("latitude"),
            longitude=kwargs.get("longitude"),
            radius=kwargs.get("radius", 25.0),
            network=kwargs.get("network"),
            limit=kwargs.get("limit", 10),
            dc_fast_only=kwargs.get("dc_fast_only", False),
        )
    elif action == "route":
        waypoints = kwargs.get("waypoints", "")
        if not waypoints:
            return "Error: 'waypoints' required for route action (semicolon-separated locations)."
        return await find_stations_along_route(
            waypoints=waypoints,
            radius=kwargs.get("radius", 10.0),
            network=kwargs.get("network"),
            limit=kwargs.get("limit", 20),
            dc_fast_only=kwargs.get("dc_fast_only", False),
        )
    elif action == "networks":
        return await list_ev_networks()
    else:
        return (
            f"Unknown action '{action}'. "
            "Available: nearest, route, networks."
        )
