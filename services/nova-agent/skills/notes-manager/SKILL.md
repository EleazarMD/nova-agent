---
name: notes-manager
description: >
  Meeting notes, action items, and productivity document management via Dashboard Notes API.
  Use for creating notes, tracking action items, and organizing meeting documentation.
---

# Notes Manager

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
