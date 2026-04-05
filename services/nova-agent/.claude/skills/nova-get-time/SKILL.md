---
name: nova-get-time
description: Get current time and date with timezone support. Returns human-readable time for any timezone.
---

# Get Time

Provides current date and time with timezone support. Essential for time-sensitive operations and scheduling.

## When to Invoke

- User asks "What time is it?"
- Current date inquiries
- Timezone conversions
- Scheduling references
- "What's today's date?"

## Actions

- **now**: Get current time
- **date**: Get current date
- **timezone**: Get time for specific timezone

## Parameters

- `timezone`: Target timezone (e.g., "America/New_York", "UTC")
- `format**: Output format preference

## Examples

User: "What time is it?"
Assistant: Invoking @nova-get-time...

User: "What time is it in Tokyo?"
Assistant: Invoking @nova-get-time for Tokyo timezone...

## References

- Handler: `handle_get_time()` in tools.py
