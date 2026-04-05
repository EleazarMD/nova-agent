---
name: nova-pic-memory
description: Advanced Personal Integration Core memory operations. Query, manage, and organize PIC memory entries with advanced filtering and retrieval.
---

# PIC Memory

Advanced operations for the Personal Integration Core (PIC) memory system. Provides comprehensive memory management including querying, filtering, and organization.

## When to Invoke

- Advanced memory operations needed
- Complex memory queries
- Memory management tasks
- Bulk memory operations
- Memory organization and cleanup

## Actions

- **query**: Advanced memory query
- **filter**: Filter memories by criteria
- **organize**: Organize memory entries
- **cleanup**: Clean up old/expired memories
- **stats**: Get memory statistics

## Parameters

- `query`: Query string
- `filters`: Filter criteria
- `operation`: Operation type
- `limit**: Maximum results

## Examples

User: "Show me all my work-related memories"
Assistant: Invoking @nova-pic-memory to query your work memories...

User: "Clean up old memories from last year"
Assistant: Invoking @nova-pic-memory to organize and clean up old entries...

## References

- Script: `services/nova-agent/skills/pic-memory/scripts/pic_memory.py`
- Handler: Various PIC memory functions
- Related: @nova-save-memory, @nova-recall-memory
