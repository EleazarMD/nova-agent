---
name: nova-ticket-manager
description: Manage support tickets and task tracking. Create, update, search, and organize tickets for project management and issue tracking.
---

# Ticket Manager

Manages support tickets and task tracking through the Dashboard API. Supports creating, updating, searching, and organizing tickets.

## When to Invoke

- User mentions creating a ticket
- Task tracking and management
- "Create a ticket for..."
- Updating ticket status
- Searching existing tickets
- Project management tasks

## Actions

- **create**: Create a new ticket
- **update**: Update existing ticket
- **search**: Search tickets
- **list**: List tickets (with filters)
- **get**: Get specific ticket details
- **close**: Close a ticket

## Parameters

- `action`: Operation type
- `ticket_id`: Ticket identifier
- `title`: Ticket title
- `description`: Ticket description
- `status`: Ticket status
- `priority`: Priority level
- `assignee`: Assigned person
- `tags`: Ticket tags

## Examples

User: "Create a ticket to fix the auth bug"
Assistant: Invoking @nova-ticket-manager to create a ticket...

User: "Show me my open tickets"
Assistant: Invoking @nova-ticket-manager to list your tickets...

## References

- Script: `services/nova-agent/skills/ticket-manager/scripts/ticket_manager.py`
- Dashboard API: Port 8404
