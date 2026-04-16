---
name: tesla-location-refresh
tool_name: tesla_location_refresh
description: >
  Refresh and retrieve the current GPS location of a Tesla vehicle.
  Returns real-time coordinates, heading, and speed.
  Use when you need the most up-to-date vehicle location data.
parameters:
  type: object
  properties:
    vin:
      type: string
      description: "Vehicle VIN (optional - defaults to first vehicle if not specified)"
  required: []
---

# Tesla Location Refresh

Get real-time GPS location, heading, and speed of a Tesla vehicle.

## When to Invoke

- User asks "Where is my Tesla?"
- Need current vehicle coordinates for navigation or tracking
- Checking if vehicle has moved since last status check
- Verifying vehicle location before sending navigation command
- User wants to find their car in a parking lot

## How It Works

1. Queries Tesla Fleet API for vehicle location data
2. Returns GPS coordinates (latitude/longitude)
3. Includes heading (compass direction) and speed
4. Data is real-time from vehicle's GPS

## Data Returned

- **Latitude/Longitude**: Precise GPS coordinates
- **Heading**: Compass direction (0-360°)
- **Speed**: Current vehicle speed (mph or km/h)
- **Timestamp**: When location was last updated

## Instructions

1. Call `tesla_location_refresh` to get the vehicle's current GPS coordinates.
2. This wakes the vehicle if sleeping to get an accurate location.
3. Returns latitude, longitude, and timestamp.

## References

- Handler: `nova/tools.py` → `handle_tesla_location_refresh`
- Tesla Fleet API: `vehicle_data` endpoint

## Examples

User: "Where is my Tesla?"
Assistant: Invoking @tesla-location-refresh...
Result: "Your Tesla is at 29.7589°N, 95.3677°W, heading 45° (northeast), speed 0 mph"

User: "Is my car still in the driveway?"
Assistant: Invoking @tesla-location-refresh to check current location...

User: "Send navigation to work"
Assistant: First getting your car's location with @tesla-location-refresh, then sending navigation...

## Requirements

- Tesla account must be connected via Dashboard
- Vehicle must have GPS and cellular connectivity
- Vehicle must be awake (use @tesla-wake if offline)

## Response Format

Returns JSON with:
```json
{
  "latitude": 29.7589,
  "longitude": -95.3677,
  "heading": 45,
  "speed": 0,
  "timestamp": "2026-04-11T19:10:00Z"
}
```

## References

- Handler: `scripts/tesla_tools.py::handle_tesla_location_refresh`
- Fleet API: https://developer.tesla.com/docs/fleet-api
- Related: @tesla-control, @tesla-wake
