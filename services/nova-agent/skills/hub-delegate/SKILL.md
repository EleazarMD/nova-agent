---
name: hub-delegate
tool_name: hub_delegate
description: >
  Delegate specialized tasks to Pi Agent Hub background agents for long-running, approval-gated, or deep work.
  Use for: deep research → Atlas, analytics → Atlas, email drafting/briefing → Hermes, browser automation → Argus,
  service operations → Infra, code fixes → Coder.
parameters:
  type: object
  properties:
    agent:
      type: string
      enum: ["atlas", "infra", "coder", "tesla", "hermes", "argus", "orchestrator"]
      description: "Which Hub agent to delegate to"
    method:
      type: string
      description: "RPC method name (e.g. atlas.research, hermes.inbox-briefing)"
    params:
      type: object
      description: "Optional JSON parameters for the method"
    context:
      type: string
      description: "Optional conversation context or notes"
  required:
    - agent
    - method
---

# Hub Delegate

Delegate tasks to the AI Homelab's background multi-agent orchestrator.

## When to Invoke

- When a task requires deep web research (Atlas).
- When writing a complex email or checking the inbox for unread mail (Hermes).
- When a task takes a long time and should run asynchronously.
- When making infrastructure changes requiring zero-trust approvals.

## Agent Reference

| Agent | Use for |
|-------|---------|
| `atlas` | Deep research, web analysis, multi-source synthesis |
| `hermes` | Email drafting, inbox briefing, contact outreach |
| `infra` | Service diagnostics, restarts, infrastructure fixes |
| `coder` | Code-level bug fixes, service patches |
| `argus` | Browser automation, web scraping |
| `tesla` | Vehicle control and monitoring |
| `orchestrator` | Multi-agent coordination |

## Instructions

Do not use this for quick synchronous lookups — use direct tools like `web_search`, `homelab_heartbeat`, `query_cig` instead. `hub_delegate` is for long-running, approval-gated, or multi-step tasks.

Always narrate before delegating: "I'll have the [agent] agent handle that."
After delegation: "The [agent] agent is on it — I'll let you know when it responds."

## Examples

User: "Find out why CIG is degraded"
→ hub_delegate(agent="infra", method="diagnose", params={"task": "Investigate why CIG container reports degraded"})

User: "Draft a follow-up email to Dr. Coleman"
→ hub_delegate(agent="hermes", method="draft", params={"to": "Dr. Coleman", "topic": "Follow-up from last meeting"})

User: "Research the latest on physician burnout policy"
→ hub_delegate(agent="atlas", method="research", params={"query": "physician burnout policy 2025 2026"})

## Failure Reporting

When `hub_delegate` fails or the agent doesn't respond, **stop and report immediately**. Do not retry with the same params, do not guess at what the agent would have returned.

**Recovery limit: 1 retry per delegation per turn, only if the failure was a transient connection error.**

<example>
hub_delegate(agent="atlas", method="research", params={...}) → "Agent atlas not available"

Recovery: Do NOT retry. Atlas may be down or pi-agent-hub is unreachable.
Escalation response:
  "I tried to send that research task to the Atlas agent but it didn't respond.
   Current state: the research was not started; nothing was changed.
   You can check if pi-agent-hub is running with: systemctl status pi-agent-hub
   Want me to try a direct web search instead as a fallback?"
</example>

<example>
hub_delegate(agent="infra", method="restart", params={"container": "cig"}) → approval request sent, then timeout waiting for approval

Recovery: Do NOT send a second delegation. The approval request is already queued.
Escalation response:
  "The restart request was sent to the infra agent and is waiting for your approval.
   Current state: CIG has NOT been restarted yet — waiting on your approval in the Hub.
   Check the Pi Agent Hub approval queue to approve or reject it."
</example>

<example>
hub_delegate(agent="hermes", method="draft", ...) → returns error string or empty result

Recovery: Retry ONCE with simplified params. Second failure:
Escalation response:
  "Hermes couldn't complete the email draft — it returned an error.
   Current state: no draft was created or sent.
   Want me to draft something directly here instead, and you can send it manually?"
</example>

**What to always include in an escalation:**
- Which agent + method was called
- Whether any action was taken (approval queued, task started, etc.)
- A fallback option Nova can offer

**What to never say:**
- "The agent handled it" if delegation returned an error
- "The restart completed" if only the approval request was sent
- Nothing — always confirm whether the task was accepted or not
