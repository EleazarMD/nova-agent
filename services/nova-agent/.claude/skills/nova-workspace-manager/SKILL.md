---
name: nova-workspace-manager
description: Manage development workspaces and projects. Create, switch, and organize coding workspaces and project environments.
---

# Workspace Manager

Manages development workspaces and project environments. Supports creating new workspaces, switching between projects, and organizing development environments.

## When to Invoke

- User asks about workspaces
- Creating new project workspaces
- Switching between projects
- "Open the dashboard workspace"
- Workspace organization tasks
- Project environment setup

## Actions

- **create**: Create new workspace
- **switch**: Switch to workspace
- **list**: List available workspaces
- **info**: Get workspace info
- **close**: Close workspace

## Parameters

- `workspace`: Workspace name or ID
- `project`: Project to open
- `path**: Workspace path

## Examples

User: "Switch to the dashboard project"
Assistant: Invoking @nova-workspace-manager to switch workspaces...

User: "List my workspaces"
Assistant: Invoking @nova-workspace-manager to list available workspaces...

## References

- Script: `services/nova-agent/skills/workspace-manager/scripts/workspace_manager.py`
