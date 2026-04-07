---
name: ev-route-planner
tool_name: ev_route_planner
description: >
  Find EV charging stations and plan charging stops for road trips.
  Uses NREL Alternative Fuel Stations API. Supports Tesla Superchargers and all major networks.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - nearest
        - route
        - networks
      description: "Action to perform"
    location:
      type: string
      description: "Address, city/state, or ZIP (for nearest). E.g. 'Humble, TX' or '77346'"
    latitude:
      type: number
      description: "Latitude (alternative to location, for nearest)"
    longitude:
      type: number
      description: "Longitude (alternative to location, for nearest)"
    waypoints:
      type: string
      description: "Semicolon-separated route locations (for route). E.g. 'Houston,TX;San Antonio,TX;Austin,TX'"
    radius:
      type: number
      description: "Search radius in miles (default 25 for nearest, 10 for route; max 500)"
    network:
      type: string
      description: "Filter by network: tesla, chargepoint, electrify_america, evgo, blink"
    limit:
      type: integer
      description: "Max results (default 10 for nearest, 20 for route; max 50)"
      default: 10
    dc_fast_only:
      type: boolean
      description: "Only show DC fast chargers"
      default: false
  required:
    - action
---

# EV Route Planner

Find EV charging stations and plan routes with charging stops using the NREL Alternative Fuel Stations API.

## When to Invoke

- Finding nearest charging stations
- Planning EV road trips with charging stops
- Locating Tesla Superchargers
- Finding stations along a route
- Checking charging networks (ChargePoint, Electrify America, EVgo, etc.)

## Actions

### nearest
Find nearest charging stations to a location.

**Parameters:**
- `location`: Address, city/state, or ZIP code (optional if Tesla location cached)
- `latitude`/`longitude`: Coordinates (alternative to location)
- `radius`: Search radius in miles (default: 25, max: 500)
- `network`: Filter by network (tesla, chargepoint, electrify_america, evgo, blink)
- `limit`: Max results (default: 10, max: 50)
- `dc_fast_only`: Only show DC fast chargers (default: false)

### route
Find charging stations along a driving route.

**Parameters:**
- `waypoints` (required): Semicolon-separated locations (e.g., "Houston,TX;Austin,TX")
- `radius`: Distance from route in miles (default: 10, max: 500)
- `network`: Filter by network name
- `limit`: Max results (default: 20, max: 50)
- `dc_fast_only`: Only DC fast chargers

### networks
List all available EV charging networks.

## Examples

User: Find Tesla Superchargers near me
Assistant: Invoking @ev-route-planner action=nearest, network=tesla

User: Plan charging stops from Houston to Austin
Assistant: Invoking @ev-route-planner action=route, waypoints="Houston,TX;Austin,TX"

User: What charging networks are available?
Assistant: Invoking @ev-route-planner action=networks

## Technical Details

- API: NREL Alternative Fuel Stations API
- Geocoding: OpenStreetMap Nominatim
- Fallback: Uses cached Tesla vehicle location if available
- Results include: station name, network, address, charger types, pricing, hours

## References

- Script: `scripts/ev_route_planner.py`
- NREL API: https://developer.nrel.gov/docs/transportation/alt-fuel-stations-v1/
- API Key: Set `NREL_API_KEY` environment variable
