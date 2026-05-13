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
        - delete_page
        - add_block
        - search
        - list_databases
        - create_database
        - list_rows
        - add_row
        - update_row
        - create_form
        - submit_form
        - get_planner_day
        - create_task
        - update_task
        - delete_task
        - create_event
        - update_planner_notes
        - list_templates
        - create_from_template
        - ai_chat
        - component_registry
      description: "Action to perform"
    title:
      type: string
      description: "Title for pages, databases, tasks, events, forms"
    intent:
      type: string
      description: "Explanation of intent for approval-gated actions like delete_page"
    page_id:
      type: string
      description: "Full page UUID from list_pages/search/create_page. Do not shorten or summarize UUIDs."
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
      description: "Tags for tasks, or keyword tags for Nova-authored pages"
    category:
      type: string
      enum: [article, case_study, research, worksheet, briefing, note, report, template]
      description: >
        Category for Nova-authored pages. Determines which sub-folder the page
        appears under in the PiCode 'Nova Workspace' tree panel.
        Use 'article' for long-form writing, 'case_study' for structured case
        analyses, 'research' for findings/literature, 'worksheet' for exercises,
        'briefing' for summaries/situation reports, 'note' for quick captures,
        'report' for formal reports, 'template' for reusable page layouts.
        Defaults to 'note' when omitted.
    topic:
      type: string
      description: "Short topic label for the page (e.g. 'managerial overreach', 'Q3 budget analysis'). Stored in metadata and displayed as a subtitle in PiCode."
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

## Nova Authorship Metadata

Every page Nova creates is automatically stamped with `properties.metadata` so PiCode can group it under the correct sub-folder:

```json
{
  "source": "nova",
  "agent": "nova-agent",
  "category": "case_study",
  "category_label": "Case Studies",
  "topic": "managerial overreach",
  "tags": ["HR", "leadership"],
  "created_at": "2026-05-12T18:30:00Z"
}
```

**PiCode tree structure** (Nova Workspace panel):
```
📁 Nova Workspace
  ├── 📄 Articles
  ├── 📄 Case Studies
  ├── 📄 Research
  ├── 📄 Worksheets
  ├── 📄 Briefings
  ├── 📄 Notes
  ├── 📄 Reports
  └── 📄 Templates
```

**Always pass `category` when creating a long-form page.** If no category is given, the page lands under "Notes".

**Examples:**
```
manage_workspace(action='create_page_with_blocks', title='Managerial Overreach — Case Study',
  category='case_study', topic='managerial overreach', tags=['HR','leadership','case-study'],
  icon='📋', properties={"blocks": [...]})

manage_workspace(action='create_page_with_blocks', title='Q3 Budget Analysis',
  category='report', topic='Q3 budget', tags=['finance','quarterly'],
  icon='📊', properties={"blocks": [...]})

manage_workspace(action='create_page', title='STAAR Prep Notes',
  category='worksheet', topic='math prep', tags=['STAAR','math'], icon='📐')
```

---

## When to Invoke

- **Proactively store** information the user shares (preferences, decisions, contacts, ideas)
- **Recall context** from previous conversations via search
- **Create notes** for meeting summaries, decisions, reference material
- **Track tasks** in the daily planner with priorities and due dates
- **Manage structured data** in databases (bug tracker, contact list, project board)
- **Collect data** via forms that auto-populate database rows
- **Daily planning** — view tasks, events, and notes for any day
- **AI chat** — ask questions grounded in workspace + CIG/PCG context

## Failure Recovery Examples

Every tool call can fail or return a partial result. The rule is always the same: **read the result, reason about it, recover once — never retry blindly**.

---

### `get_page` — Empty stub (most common failure)

<example>
User: "Tell me what's on the workspace page for the case study."

Action: manage_workspace(action="get_page", page_id="b7c9e2d1-5f3a-4e8b-9c6a-1f2d3e4a5b6c")

Tool returns:
  📄 Untitled
  page_id: b7c9e2d1-5f3a-4e8b-9c6a-1f2d3e4a5b6c
  (Page is empty — no content blocks. Do not call get_page again for this page_id.)

Recovery:
  The page exists as a stub — it was created as a placeholder but never filled in.
  Do NOT call get_page again. Do NOT search for other pages.
  Instead, check if an Active Task Plan is shown in the system prompt.
    → If yes: call manage_task_plan(action="get", plan_id="...") to load session history and next_steps.
    → If no: tell the user the page is empty and ask if they want to start drafting now.

Response to user:
  "The workspace page is there but it's still empty — we created it as a placeholder.
   From my task plan I can see we were working on [topic]. Want me to start drafting the first section?"
</example>

---

### `get_page` — Stale or wrong page_id

<example>
User: "Pull up the managerial overreach article."

Action: manage_workspace(action="get_page", page_id="b7c9e2d1-5f3a-4e8b-9c6a-1f2d3e4a5b6c")

Tool returns:
  Page b7c9e2d1-5f3a-4e8b-9c6a-1f2d3e4a5b6c not found.

Recovery:
  The stored page_id is stale (page was deleted or recreated).
  Call search ONCE to find the current page.
  Do NOT try get_page with other guessed IDs.

Action: manage_workspace(action="search", content="managerial overreach article")

Tool returns:
  🔍 Found 2 results:
    Managerial Overreach — Case Study (page_id: cc4aec4b-1a11-45e2-9f39-e4e70d8184a3)
    Overreach Analysis Draft (page_id: 9f3a1c2d-...)

Recovery continued:
  Pick the most relevant match ("Managerial Overreach — Case Study").
  Call get_page ONCE with that page_id.
  Do NOT search again with different terms.
</example>

---

### `search` — No results found

<example>
User: "What did we write about the hospital staffing case?"

Action: manage_workspace(action="search", content="hospital staffing case study")

Tool returns:
  No results found for 'hospital staffing case study'.

Recovery:
  Do NOT search again with alternate terms.
  Do NOT call get_page with a guessed page_id.
  Instead: check search_past_conversations to see if this topic was discussed in a prior session
  but never saved to the workspace.
  Then tell the user what was found (or not found) and ask if they want to create a page now.

Response to user:
  "I didn't find a workspace page for that topic. We may have discussed it in a previous conversation
   but never saved it. Want me to create a page and start drafting from what I remember of that conversation?"
</example>

---

### `search` → `get_page` — Correct two-step sequence

<example>
User: "What's on the canvas page for the case study we talked about?"

Step 1 — Search:
  Action: manage_workspace(action="search", content="case study")
  Tool returns:
    🔍 Found 3 results:
      Case Study — Managerial Overreach (page_id: cc4aec4b-...)
      Case Study Outline (page_id: 9f3a1c2d-...)
      Case Study Notes (page_id: 4b2e8f1a-...)

Step 2 — Get the best match (ONE call):
  Action: manage_workspace(action="get_page", page_id="cc4aec4b-1a11-45e2-9f39-e4e70d8184a3")
  Tool returns page content with blocks.

STOP. Do not call get_page again for other results.
Do not call search again with a refined query.
Report the content to the user from what was returned.

Anti-pattern (WRONG — causes the 22-call loop):
  ❌ get_page(b7c9e2d1) → empty → search again → get_page(cc4aec4b) → empty → search again → ...
</example>

---

### `create_page_with_blocks` — Schema error

<example>
User: "Create a workspace page for the case study with an intro section."

Action: manage_workspace(action="create_page_with_blocks", title="Case Study", blocks=[...])

Tool returns:
  properties with 'blocks' array is required. Format: {"blocks": [...]}

Recovery:
  This is a schema error — blocks must be nested inside a `properties` dict.
  Retry ONCE with the corrected schema. Do NOT tell the user the page was created.

Retry action:
  manage_workspace(action="create_page_with_blocks", title="Case Study",
    properties={"blocks": [{"type": "heading_2", "content": "Introduction"}, ...]})

If it fails again: tell the user and fall back to create_page + add_block separately.
</example>

---

### `add_block` — page_id not found after create

<example>
User: "Add a section for the methodology to that page."

Action: manage_workspace(action="add_block", page_id="cc4aec4b-...", block_type="heading_2", content="Methodology")

Tool returns:
  Page cc4aec4b-... not found.

Recovery:
  Do NOT guess a new page_id.
  Call manage_workspace(action="list_pages") to see current pages, or search for the page by title.
  Once the correct page_id is confirmed, retry add_block ONCE.
  If page is genuinely gone, tell the user and offer to recreate it.
</example>

---

### `manage_task_plan` — Already exists on create

<example>
Action: manage_task_plan(action="create", topic="Managerial Overreach Case Study")

Tool returns:
  ✅ Task plan already exists: "Managerial Overreach Case Study"
  plan_id: e4dcc9a1-4e0a-423d-8f0a-80c5a5c7a044
  Use this existing plan_id for all subsequent actions. Do NOT create another plan.

Recovery:
  This is NOT a failure. Capture the plan_id immediately.
  Proceed directly to add_step or get — do NOT call create again.
  Do NOT call list to verify — the plan_id was just returned.
</example>

---

### General recovery principle

For every tool call, ask: *"What did the tool actually return, and does that tell me I'm done or that I need exactly one more action?"*

| Received | Done? | One more action allowed |
|----------|-------|------------------------|
| Content / success confirmation | ✅ Yes | No |
| Empty / stub | ✅ Yes — stop & tell user | No — do not retry same call |
| Not found | No | Search once |
| Schema / validation error | No | Retry once with corrected args |
| Network / timeout error | No | Retry once after brief pause |
| Second failure of same action | ✅ Escalate to user | No more retries |

**Never chain more than 2 recovery attempts for the same goal in one turn.**

---

### Failure Reporting — when recovery is exhausted

When a tool fails twice or produces an unrecoverable result, **stop and report to the user**. Do not go silent, do not hallucinate a workaround, do not claim success.

Use this structure when escalating:

```
"I ran into a problem I couldn't resolve on my own.
 Here's what happened: [one sentence — what you tried, what failed]
 Current state: [what is/isn't saved, what was/wasn't created]
 What I'd suggest: [one concrete next step the user or an engineer can take]"
```

<example>
manage_workspace(action="get_page", page_id="b7c9e2d1-...") → empty
manage_task_plan(action="get", plan_id="e4dcc9a1-...") → plan not found

Escalation response:
  "I hit a problem — the workspace page for the case study is empty and I can't find
   the task plan either. It's possible the plan was archived or the IDs are stale.
   Current state: no content loaded, nothing was changed.
   I'd suggest checking the workspace page list together — want me to pull up all pages
   so we can find the right one?"
</example>

<example>
manage_workspace(action="create_page_with_blocks", ...) → schema error
Retry with corrected schema → same schema error

Escalation response:
  "I tried to create the workspace page twice but kept hitting a schema error on the blocks.
   Current state: no page was created yet — nothing is lost.
   I can try a simpler approach: create a blank page first and then add each section one at a time.
   Want me to do that?"
</example>

**What to always include in an escalation:**
- What action failed (tool name + action)
- Whether any data was written or lost
- One concrete recovery option the user can choose

**What to never say:**
- "I created the page" if creation failed
- "I'll try again" without telling the user what changed
- Nothing — silence after a failure is the worst outcome

---

## Focus & Collaboration Sessions

When the user is in a collaborative writing, drafting, redacting, or analytical session — **generate directly, don't re-ground**.

### Signs you're in a focus session

- The user says "let's draft section by section", "let's write", "let's redact", "keep going", "next section", "continue from where we left off"
- An Active Task Plan or Active Work item is shown in the system prompt
- The workspace page content was already loaded earlier in this conversation

### Focus session rules

1. **Don't re-search before responding.** If the page content is already in context from this conversation, use it directly. Do not call `search` or `get_page` again before generating the next section.
2. **Generate first, verify after.** Produce the draft/section/redaction immediately. The user can then ask for changes. Don't ask permission to start.
3. **One focused clarification at a time.** If something is unclear, ask one question. Don't ask multiple questions before generating anything.
4. **Persist output to the page.** After generating content, offer to write it to the workspace page using `add_block` or `create_page_with_blocks`. This anchors the work.
5. **Log the session at the end.** When the collaborative session wraps up, call `manage_task_plan(action="add_session", ...)` with a summary of what was drafted and what's next.

---

## Multi-Session Work Protocol

For any substantive topic that spans multiple conversations (articles, case studies, analyses, projects, research):

**Step 1 — Search first.** Always call `manage_workspace(action="search", content="<topic>")` before creating anything.

**Step 2 — Create or resume.**
- If a page exists → call `get_page` to load current content and continue from where you left off.
- If no page exists → immediately call `create_page` or `create_page_with_blocks` to anchor the work. Do not continue the topic verbally without creating the page first.

**Step 3 — Set active goal.** After finding or creating the page, call `set_active_goal` with `workspace_page_id` set to the page's ID. This ensures every future session opens with a pointer to exactly where the work lives.

**Step 4 — Resume from page.** In future sessions, if the system prompt shows an active work item with a `workspace_page_id`, call `get_page` on that ID at the start of the session before asking any clarifying questions.

**Never hold multi-session work only in conversation memory.** Conversations expire and are hard to search. The workspace page is the source of truth.

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
- `delete_page` — Delete a page. **Requires providing an `intent` parameter** explaining why the page is being deleted for JIT approval.
- `add_block` — Add content block (paragraph, heading, to-do, etc.)

Use the complete `page_id` value returned by `list_pages`, `search`, or page creation. Never pass a shortened display form like `6c8a188e...` when a full UUID is available.

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

**CRITICAL REQUIREMENT — SCHEMA ENFORCEMENT:**
- The `properties` parameter MUST be a dict with a `blocks` key: `{"properties": {"blocks": [...]}}`
- **WRONG** (causes failure): `{"action": "create_page_with_blocks", "blocks": [...]}` — `blocks` at top level is rejected
- **CORRECT**: `{"action": "create_page_with_blocks", "title": "...", "properties": {"blocks": [...]}}`
- If the API returns `"properties with 'blocks' array is required"`, the page was NOT created. Do NOT say it was created. Retry immediately with the corrected schema.

**Supported Block Types:**
*Standard Types:* `paragraph`, `heading_1`, `heading_2`, `heading_3`, `quote`, `callout`, `divider`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `toggle`, `synced_block`, `bulleted_list`, `numbered_list`, `image`, `video`, `file`, `pdf`, `embed`, `bookmark`, `link_preview`, `code`, `mermaid`, `diagram`, `math_block`, `column_list`, `column`, `tab_list`, `tab`, `grid_container`, `grid_item`, `table`, `table_row`, `table_cell`, `page`, `child_page`, `page_embed`

*Advanced/Agent Types:* `database_inline`, `database_full_page`, `database_row`, `data_view`, `run_embed`, `research_embed`, `research_intake`, `source_citation`, `cig_email_card`, `cig_contact_card`, `cig_briefing`, `ai_generated`, `ai_summary`, `ai_insight`, `ai_action_item`, `agent_task`, `approval_request`

**Parameters:**
- `action`: "create_page_with_blocks"
- `title`: The title of the page
- `icon`: An emoji icon
- `properties`: **REQUIRED** dictionary containing a `blocks` array.
  - `properties.blocks`: Array of `{type, content?, properties?}` objects
  - `type`: Any valid block type from the lists above
  - `content`: Text content (auto-wrapped into richText)
  - `properties`: Override block properties (e.g., callout icons, language for code blocks)

**Example:**
```json
{
  "action": "create_page_with_blocks",
  "title": "Geometry Quiz",
  "icon": "📐",
  "properties": {
    "blocks": [
      {"type": "heading_2", "content": "Problem 1 — Angles"},
      {"type": "paragraph", "content": "An angle measures 45 degrees. What type is it?"},
      {"type": "paragraph", "content": "Answer: ___________"},
      {"type": "divider"}
    ]
  }
}
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


---

## Advanced Canvas Page Composition (PiCode Parity)

This section documents all advanced block types and layout patterns that Nova must use to produce workspace pages that render correctly in both Pi Workspace and the PiCode IDE Canvas. All of these types round-trip bi-directionally through `PiWorkspaceSyncService`.

### New PiCode Canvas Extensions

#### `ast_link` — Live Code Snippet Block
A read-only block that links to a file or named symbol in the active PiCode workspace. When synced to PiCode, the IDE renders the live file content inline. Use when documenting code architecture or referencing a specific function in a technical page.

Properties: `astQuery.filePath`, `astQuery.symbolName` (optional), `language`

#### `smart_block` — Agentic Self-Updating Block
A block that embeds an agent prompt. When activated in PiCode, the IDE agent runs the prompt and appends child blocks with the generated results. In Pi Workspace it renders as an expandable callout showing the stored prompt. Use when a page section should auto-populate with agent-generated content.

Properties: `prompt` (string), `status` (idle|running|done), `lastUpdated` (timestamp ms)

### Layout Blocks

#### Column Layout
`column_list` is the container. Each `column` child has a `ratio` property (0–1, values must sum to 1.0). Nest content blocks inside each column using `parentId`.

#### Tab Layout
`tab_list` is the container. Each `tab` child has a `label` property. Nest content blocks inside each tab using `parentId`.

#### Grid Layout
`grid_container` with `columns` count. Each `grid_item` child is a card slot — use `parentId` to assign.

### AI & Intelligence Blocks

- `ai_generated` — AI-authored content; properties: `model`, `generatedAt`
- `ai_summary` — Condensed summary of a longer document or thread
- `ai_insight` — A highlighted AI-derived observation or recommendation
- `ai_action_item` — Agent-derived action; properties: `status`, `priority`, `dueDate`

### Integration Blocks

- `cig_email_card` — Email from CIG; properties: `emailId`, `subject`, `from`, `preview`
- `cig_contact_card` — Contact from CIG; properties: `contactId`, `name`, `title`, `email`
- `cig_briefing` — Full morning briefing with email triage, calendar, and relationship alerts
- `pcg_goal_card` — PCG goal card; properties: `goalId`, `title`, `progress` (0–1), `status`
- `agent_task` — Agentic task block; properties: `agentId`, `task`, `status`, `assignedAt`
- `approval_request` — JIT approval gate; properties: `approvalId`, `tier` (1–3), `action`

### Research & Data Blocks

- `run_embed` — PiCode run output; properties: `runId`, `command`, `status`
- `research_embed` — Deep Research session result; properties: `researchId`, `title`, `preview`
- `research_intake` — Deep Research intake form (PiCode-only interaction; syncs as callout in Pi Workspace)
- `source_citation` — Citation; properties: `url`, `title`, `author`, `publishedAt`, `excerpt`
- `data_view` — Database view; properties: `databaseId`, `viewType` (table|board|gallery|list|calendar)

### Planner Blocks (inline in pages)

- `planner_day` — Day view; properties: `date`, `tasks: []`, `events: []`
- `planner_week` — Week summary
- `planner_task` — Inline task; properties: `title`, `status`, `priority`, `dueDate`
- `planner_event` — Inline event; properties: `title`, `startTime`, `endTime`, `location`

### Full Advanced Canvas Example

```json
{
  "action": "create_page_with_blocks",
  "title": "Weekly Intelligence Briefing",
  "icon": "🧠",
  "properties": {
    "blocks": [
      {"type": "heading_1", "content": "Weekly Intelligence Briefing"},
      {"type": "callout", "content": "Auto-generated by Nova · Synced with PiCode", "properties": {"icon": {"type": "emoji", "emoji": "⚡"}, "calloutColor": "blue"}},
      {"type": "divider"},
      {"type": "heading_2", "content": "Email Highlights"},
      {"type": "cig_email_card", "properties": {"emailId": "<id>", "subject": "...", "from": "..."}},
      {"type": "heading_2", "content": "Goals"},
      {"type": "pcg_goal_card", "properties": {"goalId": "<id>", "title": "...", "progress": 0.6, "status": "active"}},
      {"type": "heading_2", "content": "AI Insight"},
      {"type": "ai_insight", "content": "Top priority: follow up with Dr. Coleman."},
      {"type": "heading_2", "content": "Research"},
      {"type": "research_embed", "properties": {"researchId": "<id>", "title": "Houston World Cup Public Health"}},
      {"type": "heading_2", "content": "Action Items"},
      {"type": "ai_action_item", "content": "Follow up with Dr. Coleman", "properties": {"status": "open", "priority": "high", "dueDate": "2026-05-15"}},
      {"type": "smart_block", "properties": {"prompt": "What are the three most important action items from this week?", "status": "idle"}}
    ]
  }
}
```

### PiCode Sync Compatibility Rules

1. All block types in this SKILL are supported bi-directionally by `PiWorkspaceSyncService` in PiCode.
2. Use `parentId` when creating nested blocks (columns, tabs, grid items).
3. `ast_link` blocks sync as read-only callouts in Pi Workspace but render as live code in PiCode.
4. `smart_block` blocks sync with their stored `prompt` — they appear as a callout in Pi Workspace and are executable in PiCode.
5. Never use `database_full` as an inline block — use `database_inline` or `data_view` inside a page body.
6. `research_intake` is a PiCode-only interaction block; it syncs as a callout in Pi Workspace.

### Quick Reference: Use Case → Block Type

| Use Case | Block Type |
|----------|-----------|
| Summarize emails | `cig_email_card` + `ai_summary` |
| Track a goal | `pcg_goal_card` |
| Live code reference | `ast_link` |
| Auto-generate a section | `smart_block` |
| Research results | `research_embed` |
| Cite a source | `source_citation` |
| Side-by-side layout | `column_list` + `column` |
| Multi-section tabs | `tab_list` + `tab` |
| Dashboard cards | `grid_container` + `grid_item` |
| Agent action tracker | `agent_task` |
| Require JIT approval | `approval_request` |
