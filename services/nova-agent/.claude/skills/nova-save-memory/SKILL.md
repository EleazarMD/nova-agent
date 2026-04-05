---
name: nova-save-memory
description: Save information to Personal Integration Core (PIC) memory system. Store facts, preferences, context, and personal data for later recall.
---

# Save Memory

Stores information in the Personal Integration Core (PIC) memory system for persistent recall across conversations. Saves facts, preferences, and personal context.

## When to Invoke

- User shares personal information to remember
- Storing preferences or settings
- Saving important facts about the user
- "Remember that I..."
- "Don't forget that..."
- Context worth preserving for future interactions

## Actions

- **save**: Save a memory entry
- **update**: Update existing memory
- **tag**: Add tags to memory for organization

## Parameters

- `content`: The information to remember
- `category`: Memory category (preference, fact, task, person, etc.)
- `tags`: Comma-separated tags for organization
- `importance`: Priority level (low/normal/high/critical)
- `expires`: Optional expiration date

## Examples

User: "Remember that I'm allergic to peanuts"
Assistant: Invoking @nova-save-memory to store this important information...

User: "My wife's name is Sarah"
Assistant: Invoking @nova-save-memory to remember this...

## References

- Handler: `handle_save_memory()` in tools.py
- PIC Memory: Personal Integration Core
- Related: @nova-recall-memory
