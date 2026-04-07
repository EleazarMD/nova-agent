---
name: workspace-manager
tool_name: manage_workspace
description: >
  Manage development workspaces, projects, and coding environments.
  Use for workspace setup, project management, and development environment control.
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - create
        - list
        - activate
        - configure
        - install_dependencies
      description: "Action to perform"
    name:
      type: string
      description: "Workspace name"
    type:
      type: string
      description: "Project type (python, node, rust, etc.)"
    template:
      type: string
      description: "Template to use"
    path:
      type: string
      description: "Workspace path"
    workspace_id:
      type: string
      description: "Workspace identifier"
    settings:
      type: object
      description: "Configuration object"
    package_manager:
      type: string
      description: "Package manager to use (pip, npm, cargo, etc.)"
    status:
      type: string
      enum:
        - active
        - archived
      description: "Filter by status"
  required:
    - action
---

# Workspace Manager

Manage development workspaces, projects, and coding environments in the homelab.

## When to Invoke

- Setting up new development workspaces
- Managing project configurations
- Switching between projects
- Organizing development environments
- Managing workspace dependencies
- Configuring project settings

## Status

**Note**: This skill is currently a placeholder. Full implementation pending workspace management system integration.

## Planned Actions

### create
Create a new workspace or project.

**Parameters:**
- `name` (required): Workspace name
- `type`: Project type (python, node, rust, etc.)
- `template`: Template to use
- `path`: Workspace path

### list
List available workspaces.

**Parameters:**
- `type`: Filter by project type
- `status`: Filter by status (active, archived)

### activate
Activate a workspace.

**Parameters:**
- `workspace_id` (required): Workspace identifier

### configure
Configure workspace settings.

**Parameters:**
- `workspace_id` (required): Workspace identifier
- `settings`: Configuration object

### install_dependencies
Install workspace dependencies.

**Parameters:**
- `workspace_id` (required): Workspace identifier
- `package_manager`: Package manager to use (pip, npm, cargo, etc.)

## Examples

User: Create a new Python workspace for the API project
Assistant: Invoking @workspace-manager action=create, name="api-project", type=python

User: List my active workspaces
Assistant: Invoking @workspace-manager action=list, status=active

User: Install dependencies for the current workspace
Assistant: Invoking @workspace-manager action=install_dependencies, workspace_id="current"

## Technical Details

- Backend: Workspace management API (pending integration)
- Possible integrations: VS Code workspaces, tmux sessions, Docker containers
- Configuration: JSON or YAML workspace definitions

## References

- Script: `scripts/` (implementation pending)
- Integration: Awaiting workspace management system specification
