# Nova Agentic Design Roadmap

## Purpose

This document defines Nova's product roadmap for becoming a durable, high-quality agentic assistant rather than a voice chatbot that improvises each turn. It adapts the six Claude Design-style agentic patterns to Nova's own harness: voice runtime, turn orchestrator, PCG/CIG/workspace grounding, Pi Agent Hub delegation, Scribe, and Pi Workspace.

The goal is not to add an overreaching manager. The goal is to make Nova's harness enforce observable contracts:

- Ground involved tasks in real sources of truth.
- Maintain durable, portable task artifacts.
- Let the user refine concrete outputs.
- Verify created artifacts before claiming success.
- Offer useful variations when the design space is ambiguous.
- Hand off work in standard formats across tools and agents.

## Product Principle

Nova should treat simple turns as simple turns and involved turns as durable work.

Simple turns can remain direct:

```text
user -> deterministic tool or LLM answer -> final response
```

Involved artifact/action turns should follow the agentic design loop:

```text
classify -> ground -> task artifact -> refine/variation -> execute -> self-QA -> handoff -> final response
```

## Current Harness Baseline

Nova already has the raw ingredients for this architecture:

- `bot.py`: Pipecat/WebRTC voice harness, tool loop, RTVI/server messages, persistence.
- `nova/voice_turn_runtime.py`: per-turn truth/finalization contract, status phases, LLM/tool evidence, promise-action invariant.
- `nova/turn_orchestrator.py`: deterministic turn control plane for high-confidence intents.
- `nova/turn_policy.py`: interpretable turn features and policy observations.
- `nova/tools.py`: tool registry, dispatch, cache, local/cloud integrations, Pi Agent Hub delegation.
- `nova/store.py`: SQLite/PostgreSQL conversations, learning events, semantic search, session metadata.
- `nova/warming.py` and cache layer: pre-warmed context for likely needs.
- Pi Agent Hub: specialist/background agents such as Scribe, Hermes, Atlas, Argus, Infra, Tesla.
- Pi Workspace: durable page/block/document destination.

The missing connective tissue is a first-class task artifact and an explicit involved-turn loop.

## Pattern 1: Agentic Context Grounding

### Claude Design Pattern

Before generating, the agent grounds itself in a source of truth: design system, style guide, project files, screenshots, proprietary data, or current canvas state.

### Nova Translation

Nova should ground involved turns in the right private/local sources before generating or acting.

Relevant sources:

- Current conversation state.
- Past conversations, excluding the active conversation by default.
- Workspace pages and blocks.
- PCG/PIC identity, preferences, goals, family facts.
- CIG email/calendar/contact graph.
- LIAM frameworks and knowledge graph.
- Cached/warmed briefings.
- Active workflow/task state.

### Required Contract

For involved turns, Nova should create a grounding record before answering:

```json
{
  "status": "grounded",
  "sources_checked": ["past_conversations", "workspace", "pcg", "liam"],
  "sources_excluded": ["active_conversation_for_recall_search"],
  "best_context": {
    "topic": "exam table paper removal",
    "issue": "managerial overreach and lack of stakeholder input",
    "candidate_workspace_page": "...",
    "frameworks": ["LIAM", "stakeholder governance", "change management"]
  },
  "open_questions": []
}
```

### Implementation Roadmap

1. Add `GroundingResult` schema.
2. Extend `search_past_conversations` to accept and use `exclude_conversation_id` by default.
3. Add orchestrator phase `grounding_context` with status updates.
4. Add grounding telemetry to `VoiceTurnRuntime` trace.
5. Add `/picode/grounding/latest` or fold into task artifact observability.

## Pattern 2: Structured Memory

### Claude Design Pattern

The agent creates and maintains portable structured artifacts such as Markdown, HTML, or JSON as persistent working memory.

### Nova Translation

Nova should not rely only on chat turns and learning events. For involved work, Nova needs a durable task artifact that captures what is being built, what has been grounded, what decisions were made, what remains, and where the handoff lives.

### Proposed Artifact

Create `nova/task_artifacts.py` with a schema like:

```json
{
  "task_id": "workspace-page-managerial-overreach",
  "kind": "workspace_page",
  "status": "grounding|drafting|awaiting_selection|executing|qa|handoff|complete|blocked",
  "goal": "Create/update workspace page for the exam table paper managerial overreach case.",
  "source_context": [],
  "requirements": [],
  "decisions": [],
  "open_questions": [],
  "candidate_outputs": [],
  "selected_output": null,
  "execution": {
    "tools_used": [],
    "delegations": [],
    "workspace_page_id": null
  },
  "qa": {
    "status": "not_run",
    "checks": []
  },
  "handoff": {
    "formats": ["markdown", "workspace_blocks", "json"],
    "links": []
  }
}
```

### Storage Options

- SQLite/PostgreSQL `task_artifacts` table.
- Session metadata for active task pointer.
- Pi Workspace page metadata for durable handoff.
- Markdown/JSON export for portability.

### Implementation Roadmap

1. Add `TaskArtifact` dataclass and persistence helpers.
2. Add active task pointer in session metadata.
3. Add task artifact events to `learning_events` or a dedicated table.
4. Add `/picode/task-artifacts` read-only observability endpoint.
5. Make Scribe accept and return task artifact JSON.

## Pattern 3: Iterative Refinement Loop

### Claude Design Pattern

The agent supports natural refinement through concrete UI controls, multimodal inputs, DOM/canvas selection, and generated components.

### Nova Translation

Nova should let the user refine durable work through concrete choices rather than vague follow-up questions.

For voice, this means Nova should produce choice cards, outline cards, draft previews, framework-selection cards, and approval/refinement controls.

### Example

```json
{
  "type": "choice_card",
  "title": "How should I structure this workspace page?",
  "options": [
    {
      "id": "governance_brief",
      "label": "Governance Issue Brief",
      "description": "Best for concise leadership escalation."
    },
    {
      "id": "case_analysis",
      "label": "Clinical Operations Case Analysis",
      "description": "Best for timeline, stakeholders, frameworks, and root causes."
    },
    {
      "id": "advocacy_plan",
      "label": "Physician Advocacy Plan",
      "description": "Best for scripts, next steps, and escalation strategy."
    }
  ]
}
```

### Implementation Roadmap

1. Define server message card types: `choice_card`, `outline_card`, `artifact_preview`, `approval_card`.
2. Add `awaiting_selection` state to `TaskArtifact`.
3. Let iOS responses select option IDs, not only free text.
4. Support voice refinement commands such as “use option two but add LIAM.”
5. Persist selected decisions into the task artifact.

## Pattern 4: Self-QA Loop

### Claude Design Pattern

The agent renders or inspects its own output, critiques it against user intent, and repairs before presenting the final result.

### Nova Translation

Nova should verify artifacts and side effects before claiming completion.

Examples:

- Workspace page: fetch page after creation, confirm title, blocks, required sections, and handoff link.
- Scribe output: validate Markdown/block JSON before committing.
- Email draft: verify recipient, tone, unresolved asks, and missing attachments.
- Calendar action: verify event exists with expected participants/time.
- Research brief: verify citations and separate sourced vs inferred claims.

### Workspace QA Contract

```json
{
  "qa_status": "passed|failed|repaired",
  "checks": [
    "page_exists",
    "has_nonempty_blocks",
    "includes_problem_summary",
    "includes_frameworks",
    "includes_recommended_actions",
    "has_handoff_link"
  ],
  "repair_actions": []
}
```

### Implementation Roadmap

1. Add `workspace_page_qa` helper.
2. Require QA before finalizing workspace page tasks.
3. Mark tool results as failed when returned text is an error contract, even if the tool returned HTTP 200.
4. Add `qa_status` to task artifact and turn trace.
5. Later: add screenshot/vision QA for rendered workspace pages or web artifacts.

## Pattern 5: Multi-Variation Generation

### Claude Design Pattern

The agent generates multiple versions to surface the hierarchy of decisions and let the user select from concrete examples.

### Nova Translation

For ambiguous artifact/design tasks, Nova should generate 2-3 options before committing.

Good targets:

- Workspace pages.
- Reports and briefs.
- Emails.
- LIAM analyses.
- Tutoring worksheets.
- Travel/family plans.
- Strategy/action plans.

### Variation Contract

```json
{
  "phase": "variation_generation",
  "decision_axis": "page structure",
  "variations": [
    {
      "id": "leadership_memo",
      "title": "Leadership Memo",
      "best_for": "formal escalation"
    },
    {
      "id": "knowledge_page",
      "title": "Workspace Knowledge Page",
      "best_for": "long-term reference"
    },
    {
      "id": "action_plan",
      "title": "Action Plan",
      "best_for": "next steps"
    }
  ]
}
```

### Implementation Roadmap

1. Add `needs_variation()` heuristic based on task kind and ambiguity.
2. Store variations in `TaskArtifact.candidate_outputs`.
3. Render variation cards in iOS/PiCode.
4. Support selection/refinement loop.
5. Allow bypass when the user gives a direct structure.

## Pattern 6: Handoff Pattern

### Claude Design Pattern

Output should not be trapped in proprietary formats. It should be exportable to standard tools and other agents.

### Nova Translation

Nova artifacts should be portable across Nova, Scribe, Pi Workspace, PiCode, and external formats.

Every durable artifact should include:

```json
{
  "artifact_format": "markdown+workspace_blocks+json",
  "source_context": [],
  "workspace_page_id": "...",
  "export_targets": ["pi_workspace", "markdown", "json", "pdf_later"],
  "handoff_summary": "..."
}
```

### Implementation Roadmap

1. Standardize artifact exports as Markdown + block JSON + metadata JSON.
2. Make Scribe consume and produce this handoff contract.
3. Add workspace page IDs and URLs to final responses.
4. Add downloadable/exportable artifacts via PiCode or workspace endpoint.
5. Make task artifacts reusable by other Hub agents.

## End-to-End Involved Turn Flow

For a request like:

```text
Let's talk about the workspace page for the managerial overreach case and the frameworks we need to include.
```

Nova should do:

1. `heard_user`: acknowledge immediately.
2. `classifying_task`: detect `workspace_context_continuation`.
3. `grounding_context`: search prior conversations excluding current thread, check workspace, recall LIAM/PCG context.
4. `task_artifact_created`: write/update `TaskArtifact`.
5. `variation_generation`: offer page structure choices if ambiguous.
6. `executing`: delegate to Scribe or call workspace tools.
7. `self_qa`: fetch/inspect resulting page.
8. `handoff`: return workspace link, Markdown/block JSON status, and next refinement options.

## Turn Categories

### Simple Turn

Examples:

- Weather.
- Time.
- Basic factual answer.
- Quick reminder.

Path:

```text
direct deterministic/tool answer
```

### Conversational Turn

Examples:

- “That was not for you.”
- “No, I meant Luca.”
- Personal discussion.

Path:

```text
acknowledge, correct conversation state, optionally store only with confirmation
```

### Involved Artifact/Action Turn

Examples:

- “Turn this conversation into a workspace page.”
- “Build the LIAM framework page.”
- “Create a report from these emails.”
- “Research this and make a brief.”

Path:

```text
ground -> task artifact -> variation/refinement -> execute -> QA -> handoff
```

## Avoiding Harness Overreach

The harness must enforce contracts, not preferences.

Good harness behavior:

- Verify that a promised action actually happened.
- Ground involved tasks before generating.
- Store durable task state.
- QA artifacts before claiming success.
- Offer options when ambiguity is real.
- Keep simple turns simple.

Bad harness behavior:

- Force every turn into a workflow.
- Add approvals for non-risky read/write tasks.
- Block direct answers when the task is simple.
- Override the user's framing without evidence.
- Create bureaucratic “manager” layers that delay action.

## Near-Term Build Plan

### Phase 0: Turn Contract and Action Ledger

Status: Core Action Ledger and Tesla side-effect safety slice completed on May 8, 2026.

The May 8 afternoon Tesla/navigation incident exposed a deeper architecture failure: Nova had multiple partial decision layers, but no single durable contract for who owns a turn, what action is pending, what evidence is required, and what may be claimed to the user.

Nova must add a foundation below routing and learning:

```text
user utterance
  -> TurnContract
  -> ActionLedger
  -> executor/delegation
  -> evidence validation
  -> ResponseGovernor
  -> exactly one final answer
```

Required guarantees:

- A cached, learned, or LLM-generated answer cannot claim a side effect unless the active action ledger has successful tool evidence.
- Follow-ups such as “yes please,” “go ahead,” “what’s taking long,” “try again,” and “it didn’t show up” must attach to the active action instead of falling through to the model.
- Every nontrivial side-effect action must have durable state: `planned`, `awaiting_confirmation`, `running`, `tool_failed`, `completed`, `evidence_missing`, `cancelled`, or `needs_clarification`.
- Tool/delegation results must be normalized into action evidence before Nova speaks.
- The response layer must enforce one final response per turn; progress messages are allowed, but final claims require the contract’s evidence.

Initial implementation targets:

1. Completed: Add `nova_action_ledger` persistence and read APIs in `nova/store.py`.
2. Completed: Add deterministic active-action intents for status, retry, confirmation, and failure report.
3. Completed: Introduce navigation/Tesla action ledger entries before allowing destination-send claims.
4. Completed: Gate Tesla navigation execution behind explicit confirmation.
5. Completed: Resolve Tesla vehicle hints such as `Model 3` and `Black Panther` to a unique VIN before sending; refuse unsafe fallbacks.
6. Completed: Append tool evidence for success/failure and claim completion only after successful `tesla_navigation` evidence.
7. Completed: Retry failed Tesla navigation actions through the same ledger/evidence path.
8. Completed: Add incident replay tests for the May 8 failed conversation sequence.
9. Completed: Add read-only PiCode Action Ledger observability endpoints.
10. Completed: Refactor learned router and plan cache into proposal-only inputs to the contract compiler.

Implemented artifacts:

- `nova/action_ledger.py`: Action Ledger data models, statuses, evidence helpers, and active-action predicates.
- `nova/store.py`: `nova_action_ledger` SQLite persistence, active lookup, status updates, evidence append, recent-list, and summary read APIs.
- `nova/turn_orchestrator.py`: active-action follow-up routing, Tesla navigation planning, confirmation execution, VIN resolution, retry, and failure/status handling.
- `nova/text_chat.py`: read-only PiCode endpoints for Action Ledger recent entries, summary, and individual action lookup.
- `tests/test_action_ledger.py`: persistence, evidence, active lookup, recent-list, and summary tests.
- `tests/test_turn_orchestrator_active_action.py`: active-action status/confirmation/retry/failure routing tests.
- `tests/test_turn_orchestrator_tesla_navigation.py`: Tesla planning, confirmation, vehicle resolution, failure, and retry tests.
- `tests/test_turn_orchestrator_tesla_incident_replay.py`: May 8 Tesla incident replay coverage.
- `tests/test_learned_router.py`: proposal-only learned-router coverage for active actions, Tesla planning, side-effect blocking, and legacy shadow-plan non-promotion.

Validation baseline:

```bash
PYTHONPATH=/home/eleazar/Projects/AIHomelab/services/nova-agent/services/nova-agent ./venv/bin/python -m unittest tests.test_learned_router tests.test_action_ledger tests.test_turn_orchestrator_active_action tests.test_turn_orchestrator_tesla_navigation tests.test_turn_orchestrator_tesla_incident_replay -v
```

Expected result:

```text
Ran 34 tests

OK
```

### Phase A: Grounding Reliability

- Exclude active conversation from recall search by default.
- Add `exclude_conversation_id` propagation from voice runtime/tool loop.
- Add `grounding_context` status events.
- Add `GroundingResult` trace data.

### Phase B: Task Artifacts

- Add `nova/task_artifacts.py`.
- Add persistence table or session metadata storage.
- Add active task pointer.
- Add `/picode/task-artifacts` read-only endpoint.

### Phase C: Workspace Continuation Workflow

- Add `TurnIntent.WORKSPACE_CONTEXT_CONTINUATION`.
- Ground prior thread + workspace page.
- Create/update `TaskArtifact`.
- Delegate to Scribe with structured work order.

### Phase D: Self-QA and Handoff

- Fetch workspace page after write.
- Validate blocks and required sections.
- Return page ID/link and export format status.
- Mark false successes as failures.

### Phase E: Iterative UI and Variations

- Add `choice_card` and `artifact_preview` server messages.
- Let iOS/PiCode select options.
- Store decisions in task artifact.

## Success Metrics

Nova is improving when:

- Involved turns emit visible task-specific statuses.
- “Let me pull that up” never finalizes without evidence.
- Prior context lookup excludes current-thread pollution.
- Workspace/document tasks produce durable task artifacts.
- Created pages pass self-QA before Nova claims success.
- User can refine concrete options instead of repeating context.
- Agents can hand off Markdown/block JSON between Nova, Scribe, and Pi Workspace.
- Side-effect claims always correspond to successful action-ledger evidence.
- Follow-up/status/retry utterances bind to active actions rather than raw pass-through.
- Incident replay conversations fail CI if Nova emits empty fallbacks, fake completions, or duplicate finals.

## Key Files To Modify Over Time

- `services/nova-agent/bot.py`
- `services/nova-agent/nova/voice_turn_runtime.py`
- `services/nova-agent/nova/turn_orchestrator.py`
- `services/nova-agent/nova/turn_policy.py`
- `services/nova-agent/nova/tools.py`
- `services/nova-agent/nova/store.py`
- `services/nova-agent/nova/text_chat.py`
- `services/nova-agent/nova/pi_workspace.py`
- `services/nova-agent/nova/task_artifacts.py` (new)
- `services/nova-agent/nova/action_ledger.py` (new)
- Pi Agent Hub Scribe workflow/assets
- Pi Workspace page/block APIs

## Design North Star

Nova should feel like a present, capable collaborator:

- She grounds herself before acting.
- She remembers through durable artifacts, not vague transcript fragments.
- She shows concrete options when the task is ambiguous.
- She verifies her work before claiming completion.
- She hands off outputs in standard formats.
- She keeps the user in control without forcing every interaction through bureaucracy.
