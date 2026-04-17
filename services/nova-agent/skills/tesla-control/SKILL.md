---
name: tesla-control
tool_name: tesla_control
description: >
  Control physical Tesla electric vehicles (Model S, 3, X, Y, Cybertruck) via Tesla Fleet API.
  This tool queries and controls ACTUAL TESLA CARS - not Docker containers, not services.
  Use for: battery status, vehicle location, door locks, climate control, charging, trunk/frunk, navigation.
  Query operations (vehicles, status) are instant. Control operations require user approval.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - vehicles
        - status
        - climate
        - charge
        - lock
        - trunk
        - wake
        - honk_flash
        - navigation
        - waypoints
        - software_update
        - nearby_charging
        - live_camera
        - charging_sessions
        - telemetry
      description: "Vehicle action: 'vehicles' lists cars, 'status' gets battery/location/climate, 'climate' controls AC, 'charge' for charging, 'lock' for doors, 'trunk' for trunk/frunk, 'wake' wakes car, 'honk_flash' to find car, 'navigation' sends destination, 'waypoints' multi-stop route, 'software_update' schedule/cancel update, 'nearby_charging' find Superchargers, 'live_camera' camera snapshots, 'charging_sessions' charge history, 'telemetry' fleet telemetry config"
    vehicle_identifier:
      type: string
      description: "Vehicle identifier: VIN, model name (Model 3, Model X), or display name (e.g., Black Panther)"
    command:
      type: string
      description: "Specific command within action (e.g., start, stop, lock, unlock, honk, flash)"
    value:
      type: number
      description: "Numeric parameter (temperature for climate, charge limit percentage, amps)"
    destination:
      type: string
      description: "Navigation destination address (for navigation action)"
    latitude:
      type: number
      description: "GPS latitude for navigation destination"
    longitude:
      type: number
      description: "GPS longitude for navigation destination"
    vin:
      type: string
      description: "Vehicle VIN (alternative to vehicle_identifier)"
  required:
    - action
---

# Tesla Control

Control Tesla vehicles through the Fleet API with voice and text commands.

## When to Invoke

- Checking Tesla vehicle status or location
- Controlling climate (start/stop/set temperature)
- Managing charging (start/stop/set limit)
- Locking/unlocking doors
- Opening frunk or trunk
- Sending navigation destination or multi-stop waypoints
- Honking horn or flashing lights
- Waking vehicle from sleep
- Scheduling or cancelling software updates
- Finding nearby Superchargers and destination chargers
- Viewing live camera snapshots
- Checking charging session history
- Reviewing fleet telemetry configuration

## Actions

### Query Operations

| Action | Description | Commands |
|--------|-------------|----------|
| `vehicles` | List all Tesla vehicles on account | None needed |
| `status` | Get detailed vehicle state (battery, location, climate) | None needed, or specify vehicle |

### Control Operations

| Action | Commands | Value Parameter |
|--------|----------|-----------------|
| `climate` | `start`, `stop`, `set_temp` | Temperature in °F |
| `charge` | `start`, `stop`, `set_limit`, `set_amps` | Limit % or amps |
| `lock` | `lock`, `unlock` | None |
| `trunk` | `open_frunk`, `open_trunk` | None |
| `wake` | None needed | None |
| `honk_flash` | `honk`, `flash` | None |
| `navigation` | None needed | `destination` or `latitude`/`longitude` |
| `waypoints` | None needed | `value` = list of {lat, lon, name} stops |
| `software_update` | `schedule`, `cancel` | `value` = offset seconds |
| `nearby_charging` | None needed | None |
| `live_camera` | None needed | None |
| `charging_sessions` | None needed | None |
| `telemetry` | None needed | None |

## Vehicle Identification

The `vehicle_identifier` parameter supports:
- **VIN**: Exact match (e.g., "5YJ3E1EA...")
- **Model name**: "Model 3", "Model X", "Model S", "Model Y"
- **Display name**: Custom name set in Tesla app (e.g., "Black Panther", "Ruby")

If not specified, defaults to first available vehicle.

## Examples

User: Where is my Tesla?
Assistant: Invoking @tesla-control action=status...

User: Start charging my Model 3
Assistant: Invoking @tesla-control action=charge, command=start, vehicle_identifier=Model 3...

User: Turn on the AC and set to 72 degrees
Assistant: Invoking @tesla-control action=climate, command=start, then action=climate, command=set_temp, value=72...

User: Lock my car
Assistant: Invoking @tesla-control action=lock, command=lock...

User: Send navigation to 123 Main Street, Houston TX
Assistant: Invoking @tesla-control action=navigation, destination="123 Main Street, Houston TX"...

User: Honk the horn
Assistant: Invoking @tesla-control action=honk_flash, command=honk...

## Requirements

- Tesla account must be connected (OAuth via Tesla Relay)
- Some commands require vehicle to be awake (use `wake` action first)
- Signed commands (lock, unlock, climate, etc.) require virtual key pairing
- Live camera requires key pairing and vehicle_cmds scope
- Charging sessions requires business fleet account

## Architecture

```
Nova Agent
    |
    v
Tesla Relay (:18810)
    |
    v
Tesla Fleet API / Command Proxy
    |
    v
Vehicle
```

## References

- Handler: `scripts/tesla_control.py`
- Fleet API: https://developer.tesla.com/docs/fleet-api
- Tesla Relay: `http://localhost:18810`

