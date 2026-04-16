---
name: tesla-navigation
tool_name: tesla_navigation
description: >
  Send navigation destination to a Tesla vehicle.
  Supports both address/place names and GPS coordinates.
  Use when user wants to navigate to a location or set a destination in their Tesla.
parameters:
  type: object
  properties:
    destination:
      type: string
      description: "Address or place name (e.g., '1600 Amphitheatre Parkway, Mountain View, CA')"
    latitude:
      type: number
      description: "GPS latitude for precise navigation (use with longitude)"
    longitude:
      type: number
      description: "GPS longitude for precise navigation (use with latitude)"
    vin:
      type: string
      description: "Vehicle VIN (optional - defaults to first vehicle if not specified)"
  required:
    - destination
---

# Tesla Navigation

Send navigation destination to a Tesla vehicle via the Fleet API.

## When to Invoke

- User asks to navigate to a location
- User wants to send directions to their Tesla
- User requests to set a destination in their car
- User provides an address or place name to navigate to
- User provides GPS coordinates for navigation

## How It Works

1. Accepts either an address/place name OR GPS coordinates
2. Sends destination to Tesla via Fleet API
3. Navigation appears on vehicle's in-car display
4. Vehicle can provide turn-by-turn directions

## Destination Formats

### Address/Place Name
- "1600 Amphitheatre Parkway, Mountain View, CA"
- "Starbucks on Main Street, Houston TX"
- "Work" (if saved in Tesla app)
- "Home" (if saved in Tesla app)

### GPS Coordinates
- Latitude: 37.4224 (North/South)
- Longitude: -122.0842 (East/West)

## Instructions

1. Call `tesla_navigation` with a destination address or lat/long.
2. The destination is sent directly to the vehicle's navigation system.
3. Use full addresses for best results (e.g., "123 Main St, Houston, TX").

## References

- Handler: `nova/tools.py` → `handle_tesla_navigation`
- Tesla Fleet API: `navigation_request` endpoint

## Examples

User: "Navigate to work"
Assistant: Invoking @tesla-navigation destination="work"...

User: "Send directions to 123 Main Street, Houston"
Assistant: Invoking @tesla-navigation destination="123 Main Street, Houston"...

User: "Navigate to coordinates 29.7589, -95.3677"
Assistant: Invoking @tesla-navigation latitude=29.7589, longitude=-95.3677...

User: "Take me to the nearest Starbucks"
Assistant: Invoking @tesla-navigation destination="nearest Starbucks"...

## Requirements

- Tesla account must be connected via Dashboard
- Vehicle must be awake (use @tesla-wake if offline)
- Vehicle must have navigation capability
- Either destination address OR latitude/longitude must be provided

## Response

Returns: "Navigation sent to Tesla: [destination or coordinates]"

## References

- Handler: `scripts/tesla_tools.py::handle_tesla_navigation`
- Fleet API: https://developer.tesla.com/docs/fleet-api
- Related: @tesla-control, @tesla-wake, @tesla-location-refresh
