---
name: nova-context-bridge
description: Bridge and share context between different agents and services. Transfer context and state across the AI Homelab ecosystem.
---

# Context Bridge

Bridges context and shares state between different agents and services in the AI Homelab ecosystem. Enables seamless context transfer across components.

## When to Invoke

- Transferring context between agents
- Sharing state across services
- "Pass this to OpenClaw"
- Context synchronization needs
- Agent-to-agent communication

## Actions

- **send**: Send context to service
- **receive**: Receive context from service
- **sync**: Synchronize context
- **bridge**: Bridge between agents

## Parameters

- `target`: Target service/agent
- `context`: Context data to transfer
- `operation`: Bridge operation type

## Examples

User: "Send this to the dashboard"
Assistant: Invoking @nova-context-bridge to transfer context...

User: "Sync my context with OpenClaw"
Assistant: Invoking @nova-context-bridge to synchronize context...

## References

- Script: `services/nova-agent/skills/context-bridge/scripts/`
- Service: Context Bridge (port 8764)
