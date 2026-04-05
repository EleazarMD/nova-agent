---
name: nova-tesla-control
description: Control Tesla vehicles via Fleet API through Tesla Relay Service. Supports vehicle status, climate control, charging, locks, trunk, and navigation with approval-gated commands.
---

# Tesla Control

Controls Tesla vehicles through the Fleet API with voice and text commands. All commands follow a tiered approval system for security.

## When to Invoke

- Checking Tesla vehicle status
- Controlling vehicle functions (climate, charging, locks)
- Finding vehicle location
- Starting/stopping charging
- Opening frunk/trunk
- Setting navigation destinations

## Actions

### Status Operations (No Approval)
- **status**: Check Tesla account connection
- **vehicles**: List all Tesla vehicles on account
- **vehicle_status**: Get detailed vehicle state (battery, location, climate)

### Vehicle Commands (Tiered Approval)
- **wake**: Wake vehicle from sleep
- **climate_on**: Turn on climate control
- **climate_off**: Turn off climate control
- **set_temperature**: Set climate temperature
- **start_charge**: Start charging session
- **stop_charge**: Stop charging session
- **open_frunk**: Open front trunk
- **open_trunk**: Open rear trunk
- **lock**: Lock vehicle
- **unlock**: Unlock vehicle
- **honk_flash**: Honk horn and flash lights
- **navigation**: Send navigation destination

## Parameters

- `action`: Operation to perform
- `vehicle_identifier`: VIN, model name, or display name
- `command`: Specific command for control actions
- `value`: Command parameter (temperature, charge limit, etc.)
- `destination`: Address for navigation
- `vin`: Vehicle VIN

## Examples

User: "Where is my Tesla?"
Assistant: Invoking @nova-tesla-control to get vehicle location...

User: "Start charging my car"
Assistant: Invoking @nova-tesla-control with action=start_charge...

## Requirements

- Tesla account must be connected via Dashboard
- OAuth tokens managed by Dashboard API
- Some commands require vehicle to be awake

## References

- Script: `services/nova-agent/skills/tesla-control/scripts/tesla_control.py`
- Handler: `execute_tesla_control()`
- Dashboard Proxy: /api/tesla/*
