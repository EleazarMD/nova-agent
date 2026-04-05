---
name: nova-ev-route-planner
description: Plan EV charging routes using NREL Alternative Fuel Stations API. Find charging stations along routes, near locations, or for specific EV networks.
---

# EV Route Planner

Plans electric vehicle charging routes using the NREL Alternative Fuel Stations API. Finds charging stations along driving routes, near current locations, or filtered by network (Tesla, ChargePoint, etc.).

## When to Invoke

- User asks about EV charging stations
- Planning a road trip with an electric vehicle
- Finding nearest charging station
- Looking for specific charging networks (Tesla, ChargePoint)
- Getting charging station details

## Actions

- **nearest**: Find nearest charging stations to a location
- **route**: Find stations along a driving route
- **networks**: List available EV charging networks
- **station_details**: Get detailed info about a specific station

## Parameters

- `action`: Operation type (nearest/route/networks/station_details)
- `location`: Address or coordinates
- `destination`: Route destination (for route planning)
- `vehicle_range`: Vehicle range in miles
- `network`: Filter by network (tesla, chargepoint, electrify_america, evgo, blink)
- `connector_type`: Filter by connector (J1772, CHAdeMO, CCS, Tesla)
- `limit`: Maximum results

## Examples

User: "Find Tesla chargers near me"
Assistant: Invoking @nova-ev-route-planner to find charging stations...

User: "Plan a route from Houston to Dallas with charging stops"
Assistant: Invoking @nova-ev-route-planner for route planning...

## References

- Script: `services/nova-agent/skills/ev-route-planner/scripts/ev_route_planner.py`
- Handler: `handle_ev_route_planner()`
- API: NREL Alternative Fuel Stations API
