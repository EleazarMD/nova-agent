---
name: tesla-control
tool_name: tesla_control
description: >
  Control Tesla vehicles via Tesla Relay Service with approval-gated commands.
  Supports listing vehicles, getting status, controlling climate, charging, locks, trunk, navigation, and more.
  All commands follow tiered approval system (Tier 0-4) for security.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - status
        - vehicles
        - vehicle_status
        - wake
        - climate_on
        - climate_off
        - set_temperature
        - start_charge
        - stop_charge
        - open_frunk
        - open_trunk
        - lock
        - unlock
      description: "Action to perform"
    vehicle_id:
      type: integer
      description: "Vehicle ID from vehicles list (required for vehicle-specific commands)"
    temperature:
      type: number
      description: "Climate temperature in degrees (for set_temperature)"
  required:
    - action
---

# Tesla Control

Control Tesla vehicles through the Fleet API with voice and text commands.

## When to Invoke

- Checking Tesla vehicle status
- Controlling vehicle functions (climate, charging, locks)
- Finding vehicle location
- Starting/stopping charging
- Opening frunk/trunk

## Actions

### Status Operations

- status: Check Tesla account connection
- vehicles: List all Tesla vehicles on account
- vehicle_status: Get detailed vehicle state (battery, location, climate)

### Vehicle Commands

- wake: Wake vehicle from sleep
- climate_on: Turn on climate control
- climate_off: Turn off climate control
- set_temperature: Set climate temperature
- start_charge: Start charging session
- stop_charge: Stop charging session
- open_frunk: Open front trunk
- open_trunk: Open rear trunk
- lock: Lock vehicle
- unlock: Unlock vehicle

## Examples

User: Where is my Tesla?
Assistant: Invoking @tesla-control to get vehicle location...

User: Start charging my car
Assistant: Invoking @tesla-control with action=start_charge...

User: Turn on the AC in my Model 3
Assistant: Invoking @tesla-control with action=climate_on...

## Requirements

- Tesla account must be connected via Dashboard
- OAuth tokens managed by Dashboard API
- Some commands require vehicle to be awake

## References

- Fleet API: https://developer.tesla.com/docs/fleet-api
- Dashboard Proxy: /api/tesla/*

