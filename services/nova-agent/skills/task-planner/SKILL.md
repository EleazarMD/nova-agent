---
name: task-planner
tool_name: manage_task_plan
description: >
  Create and manage structured task plans for long-horizon multi-session work.
  Tracks session history (conversation IDs, timestamps, what was done, sources,
  next steps) and a step checklist so Nova can resume complex projects across
  separate conversations without losing continuity.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - create
        - get
        - list
        - add_session
        - add_step
        - update_step
        - link_page
        - complete
      description: "Action to perform"
    plan_id:
      type: string
      description: "Plan ID — required for get, add_session, add_step, update_step, link_page, complete"
    topic:
      type: string
      description: "Short project name, e.g. 'Managerial Overreach Article'"
    description:
      type: string
      description: "Full goal or objective description"
    summary:
      type: string
      description: "For add_session: what was accomplished this session (1-2 sentences)"
    content:
      type: string
      description: "For add_session: full detailed notes"
    sources:
      type: array
      items:
        type: string
      description: "For add_session: tools, references, or sources used"
    next_steps:
      type: array
      items:
        type: string
      description: "For add_session: what to do next session"
    step_title:
      type: string
      description: "For add_step: checklist step title"
    step_id:
      type: string
      description: "For update_step: ID of the step to update"
    step_status:
      type: string
      enum: [pending, in_progress, done, skipped]
    step_notes:
      type: string
      description: "Notes for a step"
    step_order:
      type: integer
      description: "For add_step: sort position"
    workspace_page_id:
      type: string
      description: "Pi Workspace page_id to anchor this plan to"
  required:
    - action
---

# Nova Task Planner

A structured planning artifact for long-horizon work that spans multiple conversations. Works like PiCode's task planning — Nova creates a plan once, updates it every session, and loads it at the start of the next session to resume exactly where it left off.

## When to Use

- User starts a multi-session project: "Let's work on my article about managerial overreach"
- User returns to previous work: "Let's continue the case study we started"
- Any research, writing, analysis, or project that won't be completed in one conversation

## Session Protocol (CRITICAL)

### Session Start
1. Check the system prompt for `## Active Task Plans`
2. If a plan exists for the topic, call `manage_task_plan(action="get", plan_id="...")` to load full history
3. Brief the user on where you left off: "We have 3 sessions on this. Last time we applied the framework. Next steps were: ..."

### During the Session
- Use the step checklist to stay on track
- Mark steps in_progress / done as you go with `update_step`

### Session End (before closing)
- **ALWAYS** call `manage_task_plan(action="add_session")` with:
  - `summary`: 1-2 sentence description of what was accomplished
  - `content`: detailed notes (key decisions, analysis results, quotes, etc.)
  - `sources`: tools called, websites visited, documents referenced
  - `next_steps`: explicit list of what to do next session
- This is the memory that survives across sessions

### New Project Start
**Decision rule (no looping)**:
- Check `## Active Task Plans` in system prompt → if empty, call `manage_task_plan(action="list")` ONCE.
- Check `## Active Work` in system prompt → if empty, call `manage_workspace(action="search")` ONCE.
- If both return nothing: **stop searching immediately**. The project is NEW. Execute steps 1–5 below in one sequence.

**CRITICAL — anti-retry rules (violations cause runaway loops):**
- `create` returns a `plan_id`. **Capture it immediately. Never call `create` again this turn.**
- If `create` returns "already exists" with a `plan_id`, use that `plan_id`. Do NOT create another.
- Once you have a `plan_id` from any source, proceed to `add_step` — do not re-list or re-search.
- Each tool call in this sequence must use results from the previous call. Read the tool result before calling the next tool.
- If any step fails, report the failure to the user. Do NOT restart the sequence from `create`.

1. Call `manage_task_plan(action="create", topic="...", description="...")` → note the returned `plan_id`
2. Add steps with `add_step` using that `plan_id` for each milestone
3. Create a Pi Workspace page with `manage_workspace(action="create_page")` — create a stub now, fill it in after
4. Link them: `manage_task_plan(action="link_page", plan_id="...", workspace_page_id="...")`
5. Call `set_active_goal(goal="...", workspace_page_id="...")` so it appears in every future session
6. Tell the user what you created and ask one focused question to start filling the page

## Actions Reference

| Action | Purpose |
|--------|---------|
| `create` | Start a new plan for a project |
| `get` | Load full plan + session history + steps |
| `list` | List all active plans |
| `add_session` | Log what happened this session (call at session end) |
| `add_step` | Add a checklist milestone |
| `update_step` | Mark a step done/in_progress/skipped |
| `link_page` | Attach a Pi Workspace page to the plan |
| `complete` | Archive plan when project is finished |

## Example: Starting the Managerial Overreach Article

```
Session 1:
  manage_task_plan(action="create", topic="Managerial Overreach Article",
    description="Analyze managerial overreach and lack of physician/stakeholder input in healthcare decisions")
  manage_task_plan(action="add_step", plan_id="...", step_title="Search for existing workspace page", step_order=1)
  manage_task_plan(action="add_step", plan_id="...", step_title="Apply analytical framework", step_order=2)
  manage_task_plan(action="add_step", plan_id="...", step_title="Draft introduction section", step_order=3)
  manage_task_plan(action="add_step", plan_id="...", step_title="Draft main argument sections", step_order=4)
  manage_task_plan(action="add_step", plan_id="...", step_title="Review and finalize", step_order=5)
  ... (do work) ...
  manage_task_plan(action="add_session", plan_id="...",
    summary="Created plan, searched workspace (no existing page), applied Porter's 5 Forces framework to the case",
    sources=["search_past_conversations", "query_frameworks"],
    next_steps=["Create workspace page", "Draft introduction with framework results"])

Session 2:
  [System prompt shows: Active Task Plans → Managerial Overreach Article, last session 2026-05-11 12:38]
  manage_task_plan(action="get", plan_id="...")  ← load full context
  "We left off applying the framework. Next steps are to create the workspace page and draft the intro."
  ... (continue work) ...
  manage_task_plan(action="add_session", plan_id="...", ...)
```

## Failure Reporting

When a `manage_task_plan` call fails or returns an unexpected result, **stop after one recovery attempt and report to the user**. Never loop, never silently skip logging a session.

**Recovery limit: 1 retry per action per turn.**

<example>
manage_task_plan(action="create", topic="Case Study") → "already exists"
  plan_id: e4dcc9a1-...

Recovery: This is NOT a failure. Capture the plan_id. Proceed to add_step immediately.
Do NOT call create again. Do NOT call list to verify.
</example>

<example>
manage_task_plan(action="get", plan_id="e4dcc9a1-...") → "Plan not found"

Recovery: The plan_id is stale (archived or deleted).
Call manage_task_plan(action="list") ONCE to find the current active plan.
If list returns nothing, tell the user: "I couldn't find an active plan for this project — it may have been archived.
Want me to start a fresh one?"
Do NOT call get again with the same plan_id.
</example>

<example>
manage_task_plan(action="add_session", plan_id="...") → network or timeout error
Retry once. Second failure:

Escalation response:
  "I wasn't able to save the session log — there may be a database issue.
   Current state: your work this session was NOT saved to the plan yet.
   I'd suggest we try again in a moment, or I can summarize what we did so you can paste it manually."
</example>

**What to always include in an escalation:**
- Which action failed (`create` / `get` / `add_session` / etc.)
- Whether session data was saved or lost
- One concrete next step

**What to never say:**
- "Session saved" if `add_session` failed
- "Plan created" if `create` returned an error
- Nothing — always acknowledge the failure explicitly

## Technical Details

- Storage: SQLite (`nova_task_plans`, `nova_task_plan_sessions`, `nova_task_plan_steps`)
- Context injection: Active plans appear in `## Active Task Plans` section of system prompt at every session start
- Session tracking: `conversation_id` and timestamp recorded per entry
- 30-day window: Plans updated within 30 days appear in context automatically
