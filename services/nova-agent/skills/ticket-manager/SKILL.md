---
name: ticket-manager
description: >
  Manage tickets, issues, and tasks in the homelab project management system.
  Use for creating tickets, tracking issues, and managing project tasks.
---

# Ticket Manager

Create and manage tickets, issues, and tasks in the homelab's project management system.

## When to Invoke

- Creating new tickets or issues
- Tracking bugs and feature requests
- Managing project tasks
- Updating ticket status
- Searching for existing tickets
- Assigning tasks to team members

## Status

**Note**: This skill is currently a placeholder. Full implementation pending ticket system integration.

## Planned Actions

### create
Create a new ticket or issue.

**Parameters:**
- `title` (required): Ticket title
- `description`: Detailed description
- `type`: Ticket type (bug, feature, task, improvement)
- `priority`: Priority level (low, medium, high, critical)
- `assignee`: Assigned user
- `labels`: Comma-separated labels

### list
List tickets with optional filters.

**Parameters:**
- `status`: Filter by status (open, in_progress, closed)
- `type`: Filter by type
- `assignee`: Filter by assignee
- `labels`: Filter by labels
- `limit`: Max results

### get
Get details of a specific ticket.

**Parameters:**
- `ticket_id` (required): Ticket identifier

### update
Update an existing ticket.

**Parameters:**
- `ticket_id` (required): Ticket identifier
- `status`, `priority`, `assignee`, `labels`: Fields to update

### comment
Add a comment to a ticket.

**Parameters:**
- `ticket_id` (required): Ticket identifier
- `comment` (required): Comment text

## Examples

User: Create a ticket for the Tesla integration bug
Assistant: Invoking @ticket-manager action=create, title="Tesla integration bug", type=bug, priority=high

User: Show me my open tickets
Assistant: Invoking @ticket-manager action=list, status=open, assignee=me

User: Update ticket status to in progress
Assistant: Invoking @ticket-manager action=update, ticket_id="...", status=in_progress

## Technical Details

- Backend: Project management API (pending integration)
- Possible integrations: GitHub Issues, Linear, Jira, or custom system
- Authentication: API key or OAuth

## References

- Script: `scripts/` (implementation pending)
- Integration: Awaiting ticket system selection and API specification
