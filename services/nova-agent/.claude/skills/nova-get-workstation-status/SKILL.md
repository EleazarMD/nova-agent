---
name: nova-get-workstation-status
description: Check workstation/computer status including CPU, memory, disk usage, and running processes. Monitor system health and resource utilization.
---

# Get Workstation Status

Monitors workstation health and resource utilization. Provides real-time data on CPU usage, memory consumption, disk space, and system processes.

## When to Invoke

- User asks about computer performance
- Checking system resources
- "Is my computer running slow?"
- Disk space inquiries
- Memory usage questions
- CPU load monitoring

## Actions

- **summary**: Get system overview (CPU, memory, disk)
- **cpu**: Get detailed CPU information
- **memory**: Get memory usage details
- **disk**: Get disk space information
- **processes**: Get top resource-consuming processes
- **uptime**: Get system uptime

## Parameters

- `metric`: Specific metric to check (cpu/memory/disk/processes/uptime)
- `top_n`: Number of top processes to show (default: 10)

## Examples

User: "How's my computer doing?"
Assistant: Invoking @nova-get-workstation-status for system overview...

User: "Why is my computer slow?"
Assistant: Invoking @nova-get-workstation-status to check resource usage...

## References

- Handler: `handle_get_workstation_status()` in tools.py
- Service: Workstation Monitor (port 8404)
