---
name: tesla-wake
tool_name: tesla_wake
description: >
  Wake up a sleeping Tesla vehicle to enable remote control and data access.
  Use when a vehicle is offline/asleep and you need to interact with it.
  Automatically called before status checks if vehicle is detected as offline.
parameters:
  type: object
  properties:
    vin:
      type: string
      description: "Vehicle VIN (optional - defaults to first vehicle if not specified)"
  required: []
---

# Tesla Wake

Wake up a sleeping Tesla vehicle to enable remote operations.

## When to Invoke

- Vehicle is offline or asleep and you need to check its status
- Vehicle is asleep and you need to control it (climate, charging, locks, etc.)
- Before attempting any control operations on a vehicle that's been idle
- Automatically triggered when status check detects offline vehicle

## How It Works

1. Sends wake command to Tesla Fleet API
2. Vehicle receives wake signal and comes online
3. Takes 3-10 seconds for vehicle to fully respond
4. Once awake, all control operations become available

## Instructions

1. Call `tesla_wake` to wake a sleeping vehicle.
2. Waking takes 10-30 seconds — inform the user to wait.
3. Vehicle must be awake for most other Tesla commands to work.
4. If wake fails, the vehicle may be in a low-power state or offline.

## References

- Handler: `nova/tools.py` → `handle_tesla_wake`
- Tesla Fleet API: `wake_up` endpoint

## Examples

User: "Wake up my Tesla"
Assistant: Invoking @tesla-wake...

User: "My car is asleep, can you turn on the AC?"
Assistant: First invoking @tesla-wake to wake the vehicle, then @tesla-control action=climate, command=start...

## Requirements

- Tesla account must be connected via Dashboard
- Vehicle must have cellular connectivity (LTE/5G)
- Vehicle must be a Tesla with remote access enabled

## Response

Returns: "Wake command sent. Vehicle should be online shortly."

## References

- Handler: `scripts/tesla_tools.py::handle_tesla_wake`
- Fleet API: https://developer.tesla.com/docs/fleet-api
- Related: @tesla-control, @tesla-location-refresh
