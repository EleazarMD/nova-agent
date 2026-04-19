---
name: workspace-manager
tool_name: manage_workspace
description: >
  Manage the Pi Workspace — your persistent Notion-like workspace for notes,
  pages, databases, planner, forms, and AI-powered content.
  All data persists across conversations and is context-grounded with CIG/PCG.
  Use for: creating notes, tracking tasks, managing databases, daily planning,
  searching content, and AI-powered conversations with workspace context.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - list_pages
        - create_page
        - create_page_with_blocks
        - get_page
        - add_block
        - search
        - list_databases
        - create_database
        - add_row
        - update_row
        - list_rows
        - create_form
        - submit_form
        - planner
        - create_task
        - update_task
        - delete_task
        - create_event
        - list_templates
        - create_from_template
        - ai_chat
        - component_registry
      description: "Action to perform"
    title:
      type: string
      description: "Title for pages, databases, tasks, events, forms"
    page_id:
      type: string
      description: "Page ID"
    database_id:
      type: string
      description: "Database ID"
    row_id:
      type: string
      description: "Database row ID"
    task_id:
      type: string
      description: "Planner task ID"
    form_id:
      type: string
      description: "Form ID"
    template_id:
      type: string
      description: "Template ID"
    block_type:
      type: string
      description: "Block type: paragraph, heading_1, heading_2, to_do, bulleted_list_item, callout, quote, code, divider, planner_task"
    content:
      type: string
      description: "Text content or search query"
    properties:
      type: object
      description: "Properties for database rows, form submissions, or block properties"
    schema:
      type: array
      description: "Database schema: array of {name, type} objects"
    priority:
      type: string
      enum: [low, medium, high, urgent]
      description: "Task priority"
    status:
      type: string
      description: "Task status: not_started, in_progress, done, cancelled"
    due_date:
      type: string
      description: "Due date (ISO: 2026-04-17)"
    date:
      type: string
      description: "Date for planner view (ISO: 2026-04-17)"
    start_time:
      type: string
      description: "Event start time (ISO datetime)"
    end_time:
      type: string
      description: "Event end time (ISO datetime)"
    location:
      type: string
      description: "Event location"
    source_type:
      type: string
      enum: [manual, cig_calendar, pcg_goal, agent_task]
      description: "Source of task/event"
    icon:
      type: string
      description: "Emoji icon for pages"
    tags:
      type: array
      items:
        type: string
      description: "Tags for tasks"
    fields:
      type: array
      description: "Form field definitions"
    message:
      type: string
      description: "Message for AI chat"
  required:
    - action
---

# Pi Workspace Manager

Your persistent Notion-like workspace for personal knowledge management, note-taking, task tracking, and structured data. All content is context-grounded with CIG (email/calendar intelligence) and PCG (personal context/goals).

## When to Invoke

- **Proactively store** information the user shares (preferences, decisions, contacts, ideas)
- **Recall context** from previous conversations via search
- **Create notes** for meeting summaries, decisions, reference material
- **Track tasks** in the daily planner with priorities and due dates
- **Manage structured data** in databases (bug tracker, contact list, project board)
- **Collect data** via forms that auto-populate database rows
- **Daily planning** — view tasks, events, and notes for any day
- **AI chat** — ask questions grounded in workspace + CIG/PCG context

## Progress Narration

When using this skill, you MUST speak to the user before and during tool calls:
- **Before calling manage_workspace**: Say what you're doing — "Creating that page for you." / "Searching your workspace."
- **After the tool returns**: Confirm the result — "Done — page created." / "Found 3 matching pages."
- **Multi-step tasks**: Narrate each step — "Creating the page first... now adding the content blocks."
Never go silent while a tool is running. The user should always hear what's happening.

## Key Concepts

- **Pages**: Notion-style documents with blocks (paragraphs, headings, to-dos, callouts, code, etc.)
- **Databases**: Tables with typed columns (select, date, person, checkbox, etc.) where each row is also a page
- **Planner**: Daily view with tasks (tracked by status/priority) and events (calendar-sourced or manual)
- **Forms**: Data collection that auto-creates database rows on submission
- **Search**: Hybrid full-text + vector semantic search across all workspace content
- **Context Grounding**: Pages and AI chat are automatically enriched with CIG (email/calendar) and PCG (goals/preferences) context

## Actions

### Pages
- `list_pages` — Browse all pages/notes
- `create_page` — Create new note (title + optional content + icon)
- `get_page` — Read page with all its blocks
- `add_block` — Add content block (paragraph, heading, to-do, etc.)

### Search
- `search` — Hybrid FTS + vector search across all content

### Databases
- `list_databases` — Browse all databases
- `create_database` — Create table with typed schema
- `list_rows` — View database rows with properties
- `add_row` — Add row with properties
- `update_row` — Edit row properties

### Forms
- `create_form` — Build form with fields attached to a database
- `submit_form` — Submit form data (auto-creates row if mapped)

### Planner
- `planner` — View daily planner (tasks + events + notes)
- `create_task` — Add task with priority, due date, source tracking
- `update_task` — Change task status/priority
- `delete_task` — Remove task
- `create_event` — Add calendar event

### Templates & AI
- `list_templates` — Browse page templates
- `create_from_template` — New page from template
- `ai_chat` — Ask AI with full workspace context
- `component_registry` — List all workspace component types (for PiCode Agent)

## Source Tracking

Tasks and events track their origin:
- `manual` — User-created
- `cig_calendar` — From CIG calendar intelligence
- `pcg_goal` — Derived from a PCG goal
- `agent_task` — Created by Nova or another agent

## Examples

User: "Take a note: meeting with Dr. Coleman on Friday"
→ `manage_workspace(action="create_page", title="Dr. Coleman Meeting", content="Meeting with Dr. Coleman on Friday", icon="🩺")`

User: "What's on my planner today?"
→ `manage_workspace(action="planner")`

User: "Add a task to review the API docs by tomorrow"
→ `manage_workspace(action="create_task", title="Review API docs", priority="high", due_date="2026-04-18")`

User: "Mark the review task as done"
→ `manage_workspace(action="update_task", task_id="...", status="done")`

User: "Search for notes about Tesla"
→ `manage_workspace(action="search", content="Tesla")`

User: "Create a bug tracker database"
→ `manage_workspace(action="create_database", title="Bug Tracker", schema=[{"name":"Name","type":"title"},{"name":"Severity","type":"select"},{"name":"Status","type":"select"},{"name":"Assignee","type":"person"}])`

## Technical Details

- Backend: Pi Workspace Server (port 8762)
- Client: `nova/pi_workspace.py`
- Database: PostgreSQL with pgvector for semantic search
- Context: Auto-grounded with CIG (Neo4j) and PCG (Postgres)

## create_page_with_blocks

Create a page with multiple content blocks in a single call — ideal for worksheets, quizzes, and structured documents.

**Parameters (via `properties.blocks`):**
- `properties.blocks`: Array of `{type, content?, properties?}` objects
  - `type`: paragraph, heading_1, heading_2, heading_3, to_do, bulleted_list_item, numbered_list_item, callout, quote, code, divider, planner_task
  - `content`: Text content (auto-wrapped into richText)
  - `properties`: Override block properties

**Example:**
```
manage_workspace(
  action="create_page_with_blocks",
  title="Geometry Quiz",
  icon="📐",
  properties={"blocks": [
    {"type": "heading_2", "content": "Problem 1 — Angles"},
    {"type": "paragraph", "content": "An angle measures 45 degrees. What type is it?"},
    {"type": "paragraph", "content": "Answer: ___________"},
    {"type": "heading_2", "content": "Problem 2 — Shapes"},
    {"type": "paragraph", "content": "A shape has 4 equal sides and 4 right angles. What is it?"},
    {"type": "paragraph", "content": "Answer: ___________"},
    {"type": "divider"},
    {"type": "callout", "properties": {"icon": {"type": "emoji", "emoji": "✅"}, "calloutColor": "green", "title": [{"type": "text", "text": {"content": "Score: ___ / 2"}, "plainText": "Score: ___ / 2"}]}}
  ]}
)
```

## Templates

Available page templates for one-shot creation:
- `tpl-math-worksheet` — Practice worksheet with problems, answer spaces, and difficulty tracking
- `tpl-meeting-notes` — Auto-populated with CIG calendar data
- `tpl-daily-briefing` — Morning briefing with email triage, calendar, and goals
- `tpl-email-draft` — Draft emails in your voice
- `tpl-goal-tracker` — Track goals with PCG-linked progress
- `tpl-research-notes` — Research a topic with AI-assisted findings
- `tpl-weekly-planner` — Weekly plan with calendar integration
- `tpl-blank` — Start from scratch

Use `create_from_template` with a `template_id` to create a page from any template.
