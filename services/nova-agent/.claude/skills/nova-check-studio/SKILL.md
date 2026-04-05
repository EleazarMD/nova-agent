---
name: nova-check-studio
description: Check Studio Mac status and information. Query Studio system status, quick reads, and availability.
---

# Check Studio

Checks the status of the Studio Mac and retrieves system information. Provides quick reads on Studio availability and state.

## When to Invoke

- User asks about Studio Mac
- "Is Studio running?"
- Checking Studio availability
- Studio system status inquiries

## Actions

- **status**: Get Studio system status
- **info**: Get detailed Studio information
- **quick_read**: Perform quick read operation

## Parameters

- `studio`: Studio identifier or name
- `metric`: Specific metric to check

## Examples

User: "Is Studio running?"
Assistant: Invoking @nova-check-studio to check Studio status...

User: "What's the status of the Studio Mac?"
Assistant: Invoking @nova-check-studio to retrieve Studio information...

## References

- Handler: `handle_check_studio()` in tools.py
- Dashboard API: Port 8404
