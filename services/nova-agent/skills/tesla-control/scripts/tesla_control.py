"""
Tesla Fleet API Tools for Nova Agent

Provides voice/text control of Tesla vehicles via Tesla Relay Service.
The Tesla Relay (port 18810) is an independent microservice that manages
OAuth tokens, vehicle polling, and SSE streams.
"""

import os
import aiohttp
from typing import Optional
from loguru import logger

TESLA_RELAY_URL = os.environ.get("TESLA_RELAY_URL", "http://localhost:18810")

# ---------------------------------------------------------------------------
# Helper: Call Tesla Relay Service directly
# ---------------------------------------------------------------------------

async def _tesla_api(method: str, path: str, body: dict = None) -> dict:
    """Call Tesla API via Tesla Relay Service (independent microservice)."""
    url = f"{TESLA_RELAY_URL}{path}"
    
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=body) as resp:
            if resp.status == 401:
                return {"error": "Tesla account not connected", "needs_auth": True}
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Tesla API error: {resp.status} {text}")
                return {"error": f"Tesla API error: {resp.status}"}
            return await resp.json()


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

async def handle_tesla_status(user_id: str) -> str:
    """Check Tesla connection status."""
    result = await _tesla_api("GET", "/auth/status?user_id={user_id}")
    
    if result.get("error"):
        return result["error"]
    
    if not result.get("connected"):
        return "Tesla account is not connected. Please connect your Tesla account in the dashboard settings."
    
    scopes = result.get("scopes", [])
    return f"Tesla account connected. Available scopes: {', '.join(scopes)}"


async def handle_tesla_vehicles(user_id: str) -> str:
    """List Tesla vehicles."""
    result = await _tesla_api("GET", "/vehicles")
    
    if result.get("error"):
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
        lines.append(f"- {name}: {state}")
    
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
    if isinstance(vehicles_result, dict) and vehicles_result.get("error"):
        return vehicles_result["error"]
    # Tesla Relay returns direct array
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
        return f"VEHICLE NOT FOUND: No vehicle matching '{vehicle_identifier}'. You have these vehicles: {', '.join(available)}. Please ask about one of these specific vehicles."
    
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
        model = _get_model_from_vin(vin).lower()
        
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
    """Extract model type from VIN."""
    if not vin or len(vin) < 4:
        return "Unknown"
    
    # Tesla VIN format: position 4 indicates model
    # 3 = Model 3, S = Model S, X = Model X, Y = Model Y
    model_char = vin[3] if len(vin) > 3 else ""
    
    model_map = {
        "3": "Model 3",
        "S": "Model S", 
        "X": "Model X",
        "Y": "Model Y",
    }
    
    return model_map.get(model_char.upper(), f"Model {model_char}")


async def _get_single_vehicle_status(vin: str) -> str:
    """Get status for a single vehicle by VIN."""
    result = await _tesla_api("GET", f"/vehicles/{vin}/data")
    
    # If 412 error (vehicle asleep), wake it and retry
    if result.get("error") and "412" in str(result.get("error")):
        wake_result = await _tesla_api("POST", f"/vehicles/{vin}/wake_up")
        if wake_result.get("error"):
            return f"Vehicle is asleep. Tried to wake it but got error: {wake_result['error']}"
        
        # Wait a moment for vehicle to wake
        import asyncio
        await asyncio.sleep(3)
        
        # Retry data fetch
        result = await _tesla_api("GET", f"/vehicles/{vin}/data")
        if result.get("error"):
            return f"Vehicle woke up but couldn't get data: {result['error']}"
    elif result.get("error"):
        return result["error"]
    
    # Tesla Relay returns flattened format (all fields at top level)
    # Not nested like Fleet API (charge_state, climate_state, etc.)
    data = result if isinstance(result, dict) else result.get("response", {})
    
    lines = [f"Tesla Status ({data.get('display_name', vin)}):"]
    
    # Battery & Charging (flattened format)
    battery = data.get("battery_level", "?")
    range_mi = data.get("battery_range", "?")
    charging = data.get("charging_state", "Unknown")
    lines.append(f"🔋 Battery: {battery}% ({range_mi} miles)")
    lines.append(f"⚡ Charging: {charging}")
    
    if charging == "Charging":
        rate = data.get("charge_rate", 0)
        time_left = data.get("minutes_to_full_charge", 0)
        lines.append(f"   Rate: {rate} mi/hr, {time_left} min to full")
    
    # Climate (flattened format)
    inside_temp = data.get("inside_temp")
    if inside_temp:
        inside_f = round(inside_temp * 9/5 + 32, 1)
        lines.append(f"🌡️ Interior: {inside_f}°F")
    
    hvac_on = data.get("is_climate_on", False)
    lines.append(f"❄️ Climate: {'On' if hvac_on else 'Off'}")
    
    # Security (flattened format)
    locked = data.get("locked", None)
    sentry = data.get("sentry_mode", False)
    lines.append(f"🔒 Locked: {'Yes' if locked else 'No'}")
    lines.append(f"👁️ Sentry Mode: {'On' if sentry else 'Off'}")
    
    # Location (flattened format)
    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat and lon:
        lines.append(f"📍 Location: {lat:.4f}, {lon:.4f}")
    
    return "\n".join(lines)


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
        if vehicles_result.get("error"):
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
        if vehicles_result.get("error"):
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
        if vehicles_result.get("error"):
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
        if vehicles_result.get("error"):
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
    """Wake up Tesla vehicle."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if vehicles_result.get("error"):
            return vehicles_result["error"]
        vehicles = vehicles_result if isinstance(vehicles_result, list) else vehicles_result.get("response", [])
        if not vehicles:
            return "No Tesla vehicles found."
        vin = vehicles[0].get("vin")
    
    result = await _tesla_api("POST", f"/vehicles/{vin}/command", {
        "command": "wake_up"
    })
    
    if result.get("error"):
        return result["error"]
    
    return "Wake command sent. Vehicle should be online shortly."


async def handle_tesla_honk_flash(
    user_id: str,
    action: str,  # "honk" or "flash"
    vin: Optional[str] = None,
) -> str:
    """Honk horn or flash lights."""
    if not vin:
        vehicles_result = await _tesla_api("GET", "/vehicles")
        if vehicles_result.get("error"):
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
        if vehicles_result.get("error"):
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
