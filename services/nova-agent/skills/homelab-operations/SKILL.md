---
name: homelab-operations
tool_name: service_status
description: >
  Docker container management and infrastructure health checks for AI Homelab.
  Use for container status, logs, health checks, and controlled restart operations.
parameters:
  type: object
  properties:
    container:
      type: string
      description: "Specific container name (e.g. 'cig', 'nim-embeddings'). Leave empty for all."
  required: []
---

# Homelab Operations

Manage Docker containers and infrastructure services with tiered safety controls.

## When to Invoke

- Checking container or service status
- Viewing container logs
- Running health checks on infrastructure
- Restarting containers (requires approval)
- Managing homelab services

## Diagnostic Boundary (JIT Zero-Tolerance)

Nova is a voice assistant — the user expects a 2-second answer, not 30 seconds of silence.
Follow this tiered approach:

| Tier | Tool | Speed | When to use |
|------|------|-------|-------------|
| 0 | `homelab_heartbeat` | ~0ms | "how's the homelab?", "is everything ok?", "quick status" |
| 1 | `service_health_check` | ~5s | "deep check on hermes", "investigate X", "is neo4j connected?" |
| 2 | `hub_delegate(agent='infra', method='diagnose')` | async | "find out WHY dashboard is degraded", "trace the root cause" |
| 3 | `hub_delegate(agent='infra', method='restart')` | async+approval | "fix it", "restart the service" |

**Rule**: Always start with `homelab_heartbeat`. If the user wants more detail on a specific
problem, offer to delegate to the infra agent. Never run deep diagnostics yourself — the
user hears silence while you probe 8 services.

## Actions

### READ-ONLY (No Approval Required)

- status: List all managed containers and their state
- logs: View container logs (params: container, lines)
- health_check: Deep health check with application probes

### MUTATING (Approval Required)

- restart: Restart a container
- start: Start a stopped container
- stop: Stop a running container

## Managed Containers

- CIG/Hermes: cig, hermes-chromadb, hermes-neo4j
- AI Gateway: ai-gateway-postgres, ai-gateway-redis
- AI Inferencing: ai-inferencing
- NIM Embeddings: nim-embeddings

## Monitoring

The homelab-monitor systemd timer runs every 2 minutes and writes:
- `heartbeat-state.json` — machine-readable ecosystem status (read via `homelab_heartbeat`)
- `YYYY-MM-DD.md` — daily log of status changes (alerts only, not every check)

## Examples

User: "How's the homelab?"
Assistant: Let me check the heartbeat. → `homelab_heartbeat` → "14 of 15 services healthy. Dashboard is degraded."

User: "Find out why dashboard is degraded"
Assistant: I'll have the infrastructure agent investigate that. → `hub_delegate(agent='infra', method='diagnose', params={task: 'Investigate why Dashboard reports degraded status'})`

User: "Restart hermes"
Assistant: Invoking @homelab-operations with action=restart. This requires approval.

## Safety Notes

- Protected containers cannot be mutated
- All mutating operations route through the ApprovalService
- Nova never auto-approves any mutating operation
- Deep diagnostics are delegated to pi-agent-hub infra agent (Tier 2+)

## Progress Narration

- Before calling `homelab_heartbeat`: "Checking the homelab status for you."
- After heartbeat returns: Summarize the result immediately (e.g., "14 of 15 services healthy.")
- Before delegating to infra agent: "I'll have the infrastructure agent look into that."
- After delegation: "The infra agent is investigating. I'll let you know what it finds."

