---
name: notes-manager
tool_name: manage_notes
description: >
  DEPRECATED: Use manage_workspace instead. Notes are now Pi Workspace pages.
  Backward-compatible wrapper for creating notes as workspace pages.
  Creates notes with to-do blocks for action items, callouts for metadata.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - create
        - list
        - get
        - update
        - search
        - add_action
        - complete_action
        - list_actions
      description: "Action to perform"
    note_id:
      type: string
      description: "Note ID (page ID in workspace)"
    title:
      type: string
      description: "Note title"
    content:
      type: string
      description: "Note content (added as paragraph block)"
    note_type:
      type: string
      description: "Note type sets emoji icon: meeting=📅, quick=📝, project=📁, reference=📚, journal=📔"
    tags:
      type: string
      description: "Comma-separated tags (stored in metadata callout)"
  required:
    - action
---

# Notes Manager (DEPRECATED)

**DEPRECATED**: This skill is maintained for backward compatibility. New code should use `manage_workspace` with `create_page` instead.

Notes are now stored as Pi Workspace pages with blocks:
- Content → paragraph blocks
- Action items → to_do blocks
- Metadata → callout blocks

## Migration Path

Old: `manage_notes(action="create", title="Meeting")`
New: `manage_workspace(action="create_page", title="Meeting", icon="📅")`

Old: `manage_notes(action="list")`
New: `manage_workspace(action="list_pages")`

Manage meeting notes, action items, and productivity documents with full CRUD operations.

## When to Invoke

- Creating meeting notes
- Adding action items to notes
- Searching for notes by content or tags
- Tracking action item completion
- Organizing productivity documents
- Managing meeting attendees and dates

## Actions

### create
Create a new note.

**Parameters:**
- `title` (required): Note title
- `content`: Note content/body
- `note_type`: Type (quick, meeting, project, reference) - default: quick
- `tags`: Comma-separated tags
- `meeting_date`: ISO date for meeting notes
- `attendees`: Comma-separated attendee names
- `action_items`: Semicolon-separated action items

### list
List notes with optional filters.

**Parameters:**
- `note_type`: Filter by type
- `tags`: Filter by tag (first tag only)
- `limit`: Max results

### get
Get a specific note by ID.

**Parameters:**
- `note_id` (required): Note UUID

### update
Update an existing note.

**Parameters:**
- `note_id` (required): Note UUID
- `title`, `content`, `note_type`, `tags`, `meeting_date`, `attendees`: Fields to update

### search
Search notes by content.

**Parameters:**
- `search` (required): Search query
- `limit`: Max results

### add_action
Add action item to a note.

**Parameters:**
- `note_id` (required): Note UUID
- `action_item_text` (required): Action item description

### complete_action
Mark action item as complete or incomplete.

**Parameters:**
- `note_id` (required): Note UUID
- `action_item_id` (required): Action item UUID
- `action_item_completed`: true/false (default: true)

### list_actions
List all action items for a note.

**Parameters:**
- `note_id` (required): Note UUID

## Examples

User: Create a meeting note for today's standup
Assistant: Invoking @notes-manager action=create, title="Daily Standup", note_type=meeting, meeting_date="2026-04-05"

User: Add an action item to follow up on the Tesla integration
Assistant: Invoking @notes-manager action=add_action, note_id="...", action_item_text="Follow up on Tesla integration"

User: Search for notes about homelab
Assistant: Invoking @notes-manager action=search, search="homelab"

User: Mark action item as complete
Assistant: Invoking @notes-manager action=complete_action, note_id="...", action_item_id="..."

## Note Types

- **quick**: Quick notes and reminders
- **meeting**: Meeting notes with attendees and dates
- **project**: Project documentation
- **reference**: Reference materials

## Technical Details

- Backend: Dashboard Notes API
- Endpoint: `/api/notes`
- Authentication: X-User-Id and X-Internal-Service-Key headers
- Timeout: 15 seconds

## References

- Script: `scripts/notes_manager.py`
- Dashboard API: http://localhost:8404
