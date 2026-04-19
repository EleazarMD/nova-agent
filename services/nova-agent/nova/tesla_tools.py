"""
Tesla Fleet API Tools for Nova Agent

Provides voice/text control of Tesla vehicles via Tesla Relay Service.
The Tesla Relay (port 18810) is an independent microservice that manages
OAuth tokens, vehicle polling, and SSE streams.
"""

import os
import json
import asyncio
import aiohttp
from typing import Optional
from loguru import logger

TESLA_RELAY_URL = os.environ.get("TESLA_RELAY_URL", "http://localhost:18810")

# ---------------------------------------------------------------------------
# Helper: Call Tesla Relay Service
# ---------------------------------------------------------------------------

async def _tesla_api(method: str, path: str, body: dict = None, timeout: float = 15.0) -> dict:
    """Call Tesla API via Tesla Relay Service."""
    url = f"{TESLA_RELAY_URL}{path}"
    
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 401:
                return {"error": "Tesla account not connected", "needs_auth": True}
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Tesla API error: {resp.status} {text}")
                # Parse relay error format for structured info
                # Relay returns: {"detail":"Tesla API error: {\"error\":\"vehicle unavailable...\"}"}
                try:
                    err_data = json.loads(text)
                    detail = err_data.get("detail", "")
                except:
                    detail = text
                # Check for vehicle unavailable/asleep/offline in any format
                if "vehicle unavailable" in detail or "vehicle is offline" in detail or "asleep" in detail:
                    return {"error": "vehicle_unavailable", "detail": detail}
                return {"error": f"Tesla API error: {resp.status}"}
            return await resp.json()


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

async def handle_tesla_status(user_id: str) -> str:
    """Check Tesla connection status."""
    result = await _tesla_api("GET", f"/auth/status?user_id={user_id}")
    
    if result.get("error"):
        return result["error"]
    
    if not result.get("connected"):
        return "Tesla account is not connected. Please connect your Tesla account via the Tesla settings page."
    
    vehicles = result.get("vehicles_count", 0)
    scopes = result.get("scopes", [])
    scope_str = ", ".join(scopes) if scopes else "none"
    return f"Tesla account connected with {vehicles} vehicle(s). Scopes: {scope_str}"


async def handle_tesla_vehicles(user_id: str) -> str:
    """List Tesla vehicles."""
    result = await _tesla_api("GET", "/vehicles")
    
    # Handle error dict response
    if isinstance(result, dict) and result.get("error"):
        if result.get("needs_auth"):
            return "Tesla account not connected. Please connect your Tesla account first."
        return result["error"]
    
    # Tesla Relay returns direct array, not wrapped in {"response": [...]}
    vehicles = result if isinstance(result, list) else result.get("response", [])
    if not vehicles:
        return "No Tesla vehicles found on your account."
    
    lines = ["Your Tesla vehicles:"]
    for v in vehicles:
        state = v.get("state", "unknown")
        name = v.get("display_name", v.get("vin", "Unknown"))
        model = v.get("model", "")
        vin = v.get("vin", "")
        model_str = f" ({model})" if model else ""
        lines.append(f"- {name}{model_str}: {state} [VIN: {vin}]")
    
    return "\n".join(lines)


async def handle_tesla_vehicle_status(user_id: str, vehicle_identifier: Optional[str] = None) -> str:
    """
    Get detailed status of a Tesla vehicle.
    
    Args:
        vehicle_identifier: Can be VIN, model name (e.g. "Model 3", "Model X"), 
                          or display name (e.g. "Black Panther", "Ruby")
    """
    # Get all vehicles first
    vehicles_result = await _tesla_api("GET", "/vehicles")
    
    # Handle error dict response
    if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
        return vehicles_result["error"]
    
    # Tesla Relay returns direct array, not wrapped in {"response": [...]}
    vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
    if not vehicles:
        return "No Tesla vehicles found."
    
    # If no identifier provided, get status for ALL vehicles
    if not vehicle_identifier:
        all_status = []
        for vehicle in vehicles:
            vehicle_vin = vehicle.get("vin")
            status = await _get_single_vehicle_status(vehicle_vin)
            all_status.append(status)
        return "\n\n".join(all_status)
    
    # Resolve identifier to VIN
    target_vin = await _resolve_vehicle_identifier(vehicles, vehicle_identifier)
    if not target_vin:
        available = [f"{v.get('display_name', 'Unknown')} ({_get_model_from_vin(v.get('vin', ''))})" for v in vehicles]
        return f"Could not find vehicle matching '{vehicle_identifier}'. Available vehicles: {', '.join(available)}"
    
    return await _get_single_vehicle_status(target_vin)


async def _resolve_vehicle_identifier(vehicles: list, identifier: str) -> Optional[str]:
    """
    Resolve a vehicle identifier (model, name, or VIN) to a VIN.
    
    Supports:
    - VIN (exact match)
    - Model name: "Model 3", "Model X", "Model S", "Model Y"
    - Display name: "Black Panther", "Ruby", etc.
    """
    identifier_lower = identifier.lower().strip()
    
    for vehicle in vehicles:
        vin = vehicle.get("vin", "")
        display_name = vehicle.get("display_name", "").lower()
        # Use model from relay response (reliable), fallback to VIN-based guess
        model = (vehicle.get("model") or _get_model_from_vin(vin)).lower()
        # Populate model cache for _get_model_from_vin
        if vehicle.get("model"):
            _vehicle_model_cache[vin] = vehicle["model"]
        
        # Check VIN exact match
        if vin.lower() == identifier_lower:
            return vin
        
        # Check display name (case-insensitive, partial match)
        if identifier_lower in display_name or display_name in identifier_lower:
            return vin
        
        # Check model name (e.g. "model 3", "model x")
        if identifier_lower in model or model in identifier_lower:
            return vin
    
    return None


def _get_model_from_vin(vin: str) -> str:
    """Get model name from vehicle data cache or VIN fallback.
    
    The relay now returns 'model' in /vehicles response.
    VIN position 4 is NOT reliable for model detection.
    """
    # Check cache first (populated from /vehicles which includes model)
    if vin in _vehicle_model_cache:
        return _vehicle_model_cache[vin]
    return "Unknown"


# Cache for vehicle model names (populated from /vehicles API)
_vehicle_model_cache: dict = {}


async def _get_single_vehicle_status(vin: str) -> str:
    """Get status for a single vehicle by VIN."""
    result = await _tesla_api("GET", f"/vehicles/{vin}/data")
    
    # If vehicle is unavailable/asleep/offline, try to wake it and retry
    if result.get("error") == "vehicle_unavailable" or \
       (result.get("error") and "412" in str(result.get("error"))):
        wake_result = await _tesla_api("POST", f"/vehicles/{vin}/wake_up")
        if wake_result.get("error"):
            return f"Vehicle is asleep/offline. Tried to wake it but got error: {wake_result.get('error', 'unknown')}"
        
        # Wait for vehicle to wake
        import asyncio
        await asyncio.sleep(5)
        
        # Retry data fetch
        result = await _tesla_api("GET", f"/vehicles/{vin}/data")
        if result.get("error"):
            if result.get("error") == "vehicle_unavailable":
                return "Vehicle is still waking up. It may take 10-20 seconds to come online. Try asking again in a moment."
            return f"Vehicle woke up but couldn't get data: {result.get('error', 'unknown')}"
    elif result.get("error"):
        return result.get("error", str(result))
    
    # Tesla Relay returns flattened format (all fields at top level)
    # Not nested like Fleet API (charge_state, climate_state, etc.)
    data = result if isinstance(result, dict) else result.get("response", {})
    
    model = data.get("model", _get_model_from_vin(vin))
    model_str = f" ({model})" if model and model != "Unknown" else ""
    header = f"**{data.get('display_name', vin)}{model_str}** [VIN: {vin}]"

    # Build table rows
    battery = data.get("battery_level", "?")
    range_mi = data.get("battery_range", "?")
    charging = data.get("charging_state", "Unknown")
    charging_str = charging
    if charging == "Charging":
        rate = data.get("charge_rate", 0)
        time_left = data.get("minutes_to_full_charge", 0)
        charging_str = f"Charging — {rate} mi/hr, {time_left} min to full"

    inside_temp = data.get("inside_temp")
    inside_str = f"{round(inside_temp * 9/5 + 32, 1)}°F" if inside_temp else "N/A"
    hvac_on = data.get("is_climate_on", False)
    locked = data.get("locked", None)
    sentry = data.get("sentry_mode", False)

    rows = [
        ("🔋 Battery", f"{battery}% / {range_mi} mi"),
        ("⚡ Charging", charging_str),
        ("🌡️ Interior", inside_str),
        ("❄️ Climate", "On" if hvac_on else "Off"),
        ("🔒 Locked", "Yes" if locked else "No"),
        ("👁️ Sentry", "On" if sentry else "Off"),
    ]

    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat and lon:
        rows.append(("📍 Location", f"{lat:.4f}, {lon:.4f}"))

    table = f"{header}\n| Stat | Value |\n|------|-------|\n"
    for label, value in rows:
        table += f"| {label} | {value} |\n"
    return table.rstrip()


async def handle_tesla_charge_control(
    user_id: str, 
    action: str,
    vin: Optional[str] = None,
    limit: Optional[int] = None,
    amps: Optional[int] = None,
) -> str:
    """Control Tesla charging."""
    # Get VIN if not provided
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    if action == "start":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "charge_start"
        })
    elif action == "stop":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "charge_stop"
        })
    elif action == "set_limit" and limit:
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "set_charge_limit",
            "params": {"percent": limit}
        })
    elif action == "set_amps" and amps:
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "set_charging_amps",
            "params": {"charging_amps": amps}
        })
    else:
        return f"Invalid action: {action}. Use start, stop, set_limit, or set_amps."
    
    if result.get("error"):
        return result["error"]
    
    return f"Charging command '{action}' executed successfully."


async def handle_tesla_climate_control(
    user_id: str,
    action: str,
    vin: Optional[str] = None,
    temp: Optional[float] = None,
) -> str:
    """Control Tesla climate."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    if action == "start":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "auto_conditioning_start"
        })
    elif action == "stop":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "auto_conditioning_stop"
        })
    elif action == "set_temp" and temp:
        # Convert F to C for API
        temp_c = (temp - 32) * 5/9
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "set_temps",
            "params": {"driver_temp": temp_c, "passenger_temp": temp_c}
        })
    else:
        return f"Invalid action: {action}. Use start, stop, or set_temp."
    
    if result.get("error"):
        return result["error"]
    
    return f"Climate command '{action}' executed successfully."


async def handle_tesla_lock_control(
    user_id: str,
    action: str,
    vin: Optional[str] = None,
) -> str:
    """Control Tesla door locks."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    if action == "lock":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "door_lock"
        })
    elif action == "unlock":
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "door_unlock"
        })
    else:
        return f"Invalid action: {action}. Use lock or unlock."
    
    if result.get("error"):
        return result["error"]
    
    return f"Door {action} command executed successfully."


async def handle_tesla_trunk_control(
    user_id: str,
    which: str,  # "front" or "rear"
    vin: Optional[str] = None,
) -> str:
    """Open Tesla trunk or frunk."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
        "command": "actuate_trunk",
        "params": {"which_trunk": which}
    })
    
    if result.get("error"):
        return result["error"]
    
    trunk_name = "frunk" if which == "front" else "trunk"
    return f"{trunk_name.capitalize()} opened successfully."


async def handle_tesla_wake(user_id: str, vin: Optional[str] = None) -> str:
    """Wake up Tesla vehicle and poll until online (up to 3 attempts)."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")

    result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
        "command": "wake_up"
    }, timeout=20.0)

    if result.get("error"):
        return result["error"]

    # Poll for vehicle to come online (3 attempts, 5s apart)
    for attempt in range(3):
        await asyncio.sleep(5)
        status = await _tesla_api("GET", f"/vehicles/{vin}/data", timeout=10.0)
        if not status.get("error"):
            return "Vehicle is now online and ready."
        if status.get("error") == "vehicle_unavailable":
            logger.info(f"Wake poll attempt {attempt + 1}/3: vehicle still offline")
            continue
        # Some other error — stop polling
        break

    return "Wake command sent, but the vehicle isn't responding yet. It may be in a deep sleep cycle — try again in a few minutes or use the Tesla app when you're near the car."


async def handle_tesla_honk_flash(
    user_id: str,
    action: str,  # "honk" or "flash"
    vin: Optional[str] = None,
) -> str:
    """Honk horn or flash lights."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    command = "honk_horn" if action == "honk" else "flash_lights"
    result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
        "command": command
    })
    
    if result.get("error"):
        return result["error"]
    
    return f"{'Horn honked' if action == 'honk' else 'Lights flashed'} successfully."


async def handle_tesla_location_refresh(user_id: str, vin: Optional[str] = None) -> str:
    """
    Refresh Tesla vehicle location on-demand.
    
    This triggers an immediate API call to get the latest vehicle location,
    bypassing the 30-minute polling cycle. Use when you need current location
    for navigation, charging station lookup, or trip planning.
    
    Args:
        vin: Vehicle VIN. If None, refreshes the first available vehicle.
    """
    from nova.tesla_client import refresh_vehicle_data
    
    # Refresh vehicle data
    result = await refresh_vehicle_data(vin)
    
    if not result:
        if not vin:
            return "Could not refresh vehicle location. Ensure Tesla account is connected and a vehicle is available."
        return f"Could not refresh location for vehicle {vin}. Ensure the vehicle is online and accessible."
    
    lat = result.get("latitude")
    lon = result.get("longitude")
    vin_used = result.get("vin", vin or "unknown")
    
    return f"Vehicle location refreshed: {vin_used} is at {lat:.5f}, {lon:.5f}"


async def handle_tesla_navigation(
    user_id: str,
    destination: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    vin: Optional[str] = None,
) -> str:
    """
    Send navigation destination to Tesla.
    
    Args:
        destination: Address or place name (e.g., "1600 Amphitheatre Parkway, Mountain View, CA")
        latitude: Optional GPS latitude for precise navigation
        longitude: Optional GPS longitude for precise navigation
        vin: Vehicle VIN. If None, sends to first available vehicle.
    """
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    # Use GPS coordinates if provided, otherwise use address
    if latitude is not None and longitude is not None:
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "navigation_gps_request",
            "params": {
                "lat": latitude,
                "lon": longitude,
                "order": 1
            }
        })
    else:
        result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
            "command": "navigation_request",
            "params": {
                "type": "share_ext_content_raw",
                "value": {
                    "android.intent.extra.TEXT": destination
                },
                "locale": "en-US",
                "timestamp_ms": int(__import__('time').time() * 1000)
            }
        })
    
    if result.get("error"):
        return result["error"]
    
    location_str = f"{latitude}, {longitude}" if latitude and longitude else destination
    return f"Navigation sent to Tesla: {location_str}"


# ---------------------------------------------------------------------------
# Unified Tesla Control Handler (Claude Skills Format)
# ---------------------------------------------------------------------------

async def handle_tesla_control(
    user_id: str,
    action: str,
    vehicle_identifier: Optional[str] = None,
    destination: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    command: Optional[str] = None,
    value: Optional[float] = None,
    vin: Optional[str] = None,
) -> str:
    """
    Unified Tesla control handler following Claude Skills pattern.
    
    Args:
        action: Operation to perform (vehicles, status, climate, charge, lock, trunk, wake, honk_flash, navigation)
        vehicle_identifier: Model name, display name, or VIN
        destination: Address for navigation
        latitude: GPS latitude for navigation
        longitude: GPS longitude for navigation
        command: Specific command (e.g., 'start', 'stop', 'lock', 'unlock')
        value: Command parameter (temperature, charge limit, etc.)
        vin: Vehicle VIN (alternative to vehicle_identifier)
    """
    # Query operations
    if action == "vehicles":
        return await handle_tesla_vehicles(user_id)
    
    elif action == "status":
        return await handle_tesla_vehicle_status(user_id, vehicle_identifier or vin)
    
    # Control operations
    elif action == "climate":
        if not command:
            return "Climate control requires a command: start, stop, or set_temp"
        return await handle_tesla_climate_control(
            user_id=user_id,
            action=command,
            vin=vin,
            temp=value,
        )
    
    elif action == "charge":
        if not command:
            return "Charge control requires a command: start, stop, set_limit, or set_amps"
        limit = int(value) if value and command == "set_limit" else None
        amps = int(value) if value and command == "set_amps" else None
        return await handle_tesla_charge_control(
            user_id=user_id,
            action=command,
            vin=vin,
            limit=limit,
            amps=amps,
        )
    
    elif action == "lock":
        if not command:
            return "Lock control requires a command: lock or unlock"
        return await handle_tesla_lock_control(
            user_id=user_id,
            action=command,
            vin=vin,
        )
    
    elif action == "trunk":
        which = "front" if command == "open_frunk" else "rear"
        return await handle_tesla_trunk_control(
            user_id=user_id,
            which=which,
            vin=vin,
        )
    
    elif action == "wake":
        return await handle_tesla_wake(user_id, vin)
    
    elif action == "honk_flash":
        if not command:
            return "Honk/flash requires a command: honk or flash"
        return await handle_tesla_honk_flash(
            user_id=user_id,
            action=command,
            vin=vin,
        )
    
    elif action == "navigation":
        if not destination and (latitude is None or longitude is None):
            return "Navigation requires either a destination address or GPS coordinates"
        return await handle_tesla_navigation(
            user_id=user_id,
            destination=destination or "",
            latitude=latitude,
            longitude=longitude,
            vin=vin,
        )
    
    else:
        return f"Unknown Tesla action: {action}. Valid actions: vehicles, status, climate, charge, lock, trunk, wake, honk_flash, navigation"
