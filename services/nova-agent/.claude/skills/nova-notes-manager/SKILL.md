---
name: nova-notes-manager
description: Manage meeting notes, action items, and productivity documents via the Dashboard Notes API. Create, read, update, search notes and track action items.
---

# Notes Manager

Manages notes and action items through the Dashboard API. Supports creating meeting notes, tracking action items, and searching existing notes.

## When to Invoke

- User mentions taking notes or meeting notes
- Creating action items or to-do lists
- Searching for existing notes
- Updating or modifying existing notes
- Adding attendees or meeting details
- Managing productivity documents

## Actions

- **create**: Create a new note with title, content, type, tags
- **list**: List all notes with optional filters
- **get**: Retrieve a specific note by ID
- **update**: Update note content or metadata
- **search**: Search notes by content keywords
- **add_action**: Add action item to a note
- **complete_action**: Mark action item as complete
- **list_actions**: List action items for a note

## Parameters

- `action`: Operation to perform (create/list/get/update/search/add_action/complete_action/list_actions)
- `note_id`: Note identifier (for get/update/action operations)
- `title`: Note title
- `content`: Note content/body
- `note_type`: Type of note (meeting, general, action_items)
- `tags`: Comma-separated tags
- `action_items`: Action items to include
- `meeting_date`: Date of meeting
- `attendees`: List of attendees
- `search`: Search query string
- `limit`: Maximum results to return

## Examples

User: "Take notes for my team meeting about Q3 roadmap"
Assistant: Invoking @nova-notes-manager to create meeting notes...

User: "What action items do I have?"
Assistant: Invoking @nova-notes-manager to list your action items...

## References

- Script: `services/nova-agent/skills/notes-manager/scripts/notes_manager.py`
- Handler: `handle_manage_notes()`
- Dashboard API: Port 8404
