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
      description: "Specific container name (e.g. 'cig', 'openclaw'). Leave empty for all."
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

- Hermes Intelligence: cig, hermes-chromadb, hermes-neo4j
- OpenClaw: openclaw-novnc, openclaw-inference
- PIC/PKB: pkb-api, pkb-neo4j, pkb-redis
- AI Inferencing: ai-inferencing
- Dashboard: ecosystem-dashboard

## Examples

User: Check the status of all containers
Assistant: Invoking @homelab-operations with action=status

User: Show me logs from cig
Assistant: Invoking @homelab-operations with action=logs, container=cig

User: Restart the dashboard container
Assistant: Invoking @homelab-operations with action=restart. This requires approval.

## Safety Notes

- Protected containers cannot be mutated
- All mutating operations route through the ApprovalService
- Nova never auto-approves any mutating operation

