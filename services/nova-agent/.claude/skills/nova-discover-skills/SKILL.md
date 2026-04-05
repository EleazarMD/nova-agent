---
name: nova-discover-skills
description: Discover available skills from the skill registry. Query what skills are available and get information about skill capabilities.
---

# Discover Skills

Queries the skill registry to discover available skills and their capabilities. Helps users understand what actions Nova can perform.

## When to Invoke

- User asks "What can you do?"
- "What skills do you have?"
- Discovering available capabilities
- Listing all available functions
- Understanding Nova's abilities

## Actions

- **list**: List all available skills
- **search**: Search skills by name or description
- **info**: Get detailed info about a specific skill
- **categories**: List skill categories

## Parameters

- `query`: Search query for skill discovery
- `category**: Filter by skill category
- `skill_id**: Specific skill to get info about

## Examples

User: "What can you do?"
Assistant: Invoking @nova-discover-skills to show available capabilities...

User: "List all your skills"
Assistant: Invoking @nova-discover-skills to enumerate all available skills...

## References

- Handler: `handle_discover_skills()` in tools.py
- Service: Skill Discovery (port 18791)
