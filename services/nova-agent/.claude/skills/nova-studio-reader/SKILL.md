---
name: nova-studio-reader
description: Read and retrieve information from Studio Mac. Access Studio data, logs, and system information remotely.
---

# Studio Reader

Reads and retrieves information from the Studio Mac system. Provides remote access to Studio data, logs, and system information.

## When to Invoke

- User asks about Studio data
- Reading Studio logs
- "What's on Studio?"
- Studio information retrieval
- Remote Studio access needed

## Actions

- **read**: Read Studio data
- **logs**: Get Studio logs
- **status**: Get Studio status
- **info**: Get Studio information

## Parameters

- `path`: Path to read from
- `type`: Data type to retrieve
- `limit**: Result limit

## Examples

User: "Read the latest logs from Studio"
Assistant: Invoking @nova-studio-reader to retrieve Studio logs...

User: "What's the current status on Studio?"
Assistant: Invoking @nova-studio-reader to check Studio status...

## References

- Script: `services/nova-agent/skills/studio-reader/scripts/studio_reader.py`
- Service: Studio Mac (via Dashboard)
