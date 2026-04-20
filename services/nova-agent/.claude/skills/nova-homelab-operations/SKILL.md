---
name: nova-homelab-operations
description: Docker container management and infrastructure health checks for AI Homelab. Manage containers, view logs, check health, and perform controlled restart operations.
---

# Homelab Operations

Manage Docker containers and infrastructure services with tiered safety controls. Read-only operations require no approval, while mutating operations require explicit approval.

## When to Invoke

- Checking container or service status
- Viewing container logs
- Running health checks on infrastructure
- Restarting containers (requires approval)
- Managing homelab services
- Investigating service issues

## Actions

### READ-ONLY (No Approval Required)
- **status**: List all managed containers and their state
- **logs**: View container logs (params: container, lines)
- **health_check**: Deep health check with application probes

### MUTATING (Approval Required)
- **restart**: Restart a container
- **start**: Start a stopped container
- **stop**: Stop a running container

## Managed Containers

- Hermes Intelligence: cig, hermes-chromadb, hermes-neo4j
- Pi Agent Hub: pi-agent-hub
- PIC/PKB: pkb-api, pkb-neo4j, pkb-redis
- AI Inferencing: ai-inferencing
- Dashboard: ecosystem-dashboard

## Parameters

- `action`: Operation type (status/logs/health_check/restart/start/stop)
- `container`: Container name for targeted operations
- `service`: Service identifier
- `lines`: Number of log lines to retrieve (default: 50)

## Examples

User: "Check the status of all containers"
Assistant: Invoking @nova-homelab-operations with action=status

User: "Show me logs from cig"
Assistant: Invoking @nova-homelab-operations with action=logs, container=cig

User: "Restart the dashboard container"
Assistant: Invoking @nova-homelab-operations with action=restart. This requires approval.

## Safety Notes

- Protected containers cannot be mutated
- All mutating operations route through the ApprovalService
- Nova never auto-approves any mutating operation

## References

- Script: `services/nova-agent/skills/homelab-operations/scripts/operations.py`
- Handler: `homelab_operations()`
