---
name: nova-recall-memory
description: Recall information from Personal Integration Core (PIC) memory system. Retrieve stored facts, preferences, and context previously saved.
---

# Recall Memory

Retrieves information previously stored in the Personal Integration Core (PIC) memory system. Accesses saved facts, preferences, and personal context.

## When to Invoke

- User asks about previously saved information
- "What did I tell you about...?"
- Retrieving stored preferences
- Contextual recall for personalization
- "Do you remember...?"

## Actions

- **search**: Search memories by content
- **get**: Get specific memory by ID
- **list**: List recent memories
- **by_category**: Get memories by category
- **by_tag**: Get memories by tag

## Parameters

- `query`: Search query or memory ID
- `category`: Filter by category
- `tag`: Filter by tag
- `limit`: Maximum results to return
- `time_range`: Time range filter (today/week/month/all)

## Examples

User: "What did I tell you about my allergies?"
Assistant: Invoking @nova-recall-memory to find that information...

User: "What are my preferences?"
Assistant: Invoking @nova-recall-memory to retrieve your stored preferences...

## References

- Handler: `handle_recall_memory()` in tools.py
- PIC Memory: Personal Integration Core
- Related: @nova-save-memory
