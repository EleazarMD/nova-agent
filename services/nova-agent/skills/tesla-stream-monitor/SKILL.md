---
name: tesla-stream-monitor
tool_name: tesla_stream_monitor
description: >
  Monitor Tesla vehicles for real-time events via SSE streaming.
  Supports charging completion, location changes, sentry mode alerts, and arrival notifications.
  Integrates with push notifications for proactive alerts.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - start
        - stop
        - status
        - list
      description: "Monitor action to perform"
    event_type:
      type: string
      enum:
        - charging_complete
        - location_change
        - sentry_alert
        - arrival
        - departure
        - all
      description: "Event type to monitor (default: all)"
    vehicle_identifier:
      type: string
      description: "Vehicle identifier: VIN, model name, or display name"
    location_name:
      type: string
      description: "Location name for arrival/departure monitoring (e.g., 'home', 'work')"
    notify:
      type: boolean
      description: "Send push notification when event triggers (default: true)"
      default: true
  required:
    - action
---

# Tesla Stream Monitor

Monitor Tesla vehicles for real-time events via SSE streaming from Tesla Relay. Enables proactive notifications for charging completion, location changes, sentry alerts, and arrivals.

## When to Invoke

- User wants to be notified when charging completes
- User wants to know when their car arrives at a location
- User wants alerts for sentry mode triggers
- User wants to track location changes in real-time
- User wants to stop monitoring for specific events

## Actions

### start
Start monitoring for vehicle events.

**Parameters:**
- `event_type`: Type of event to monitor (default: `all`)
- `vehicle_identifier`: Specific vehicle (optional, monitors all if not specified)
- `location_name`: Required for `arrival`/`departure` events
- `notify`: Send push notification (default: true)

### stop
Stop monitoring for vehicle events.

**Parameters:**
- `event_type`: Stop specific event type (optional, stops all if not specified)
- `vehicle_identifier`: Stop for specific vehicle (optional)

### status
Check current monitoring status.

**Returns:** List of active monitors with event types and vehicles.

### list
List available event types and their descriptions.

## Event Types

| Event | Description | Trigger Condition |
|-------|-------------|-------------------|
| `charging_complete` | Charging finished | `charging_state` changes to `Stopped` and battery > 90% |
| `location_change` | Vehicle moved | Latitude/longitude changes by > 0.001 |
| `sentry_alert` | Sentry mode triggered | `sentry_mode` changes to true while locked |
| `arrival` | Arrived at location | Vehicle enters geofence radius |
| `departure` | Left location | Vehicle exits geofence radius |
| `all` | All events | Any of the above |

## Examples

User: Let me know when my Tesla finishes charging
Assistant: Invoking @tesla-stream-monitor action=start, event_type=charging_complete...

User: Tell me when my car gets home
Assistant: Invoking @tesla-stream-monitor action=start, event_type=arrival, location_name=home...

User: Alert me if sentry mode goes off
Assistant: Invoking @tesla-stream-monitor action=start, event_type=sentry_alert...

User: Stop monitoring my Tesla
Assistant: Invoking @tesla-stream-monitor action=stop...

User: What am I monitoring?
Assistant: Invoking @tesla-stream-monitor action=status...

## Architecture

```
Nova Agent (tesla_stream_monitor tool)
    |
    v
Tesla Relay SSE Stream (:18810/stream)
    |
    v
Event Detection Engine
    |
    v
Push Notification Service
    |
    v
iOS App (User Notification)
```

## Integration

- **Tesla Relay**: SSE endpoint at `/stream?user_id={user_id}`
- **Push Notifications**: Uses existing APNs integration
- **Event Bus**: Publishes to Nova EventBus for logging

## Requirements

- Tesla account connected via Dashboard
- Tesla Relay service running on port 18810
- Valid OAuth tokens (auto-refreshed by Tesla Relay)

## References

- Handler: `scripts/tesla_stream_monitor.py`
- Tesla Relay: `/stream` SSE endpoint
- Push Notifications: iOS APNs integration
