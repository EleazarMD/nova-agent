---
name: pcg-save-memory
tool_name: save_memory
description: >
  Save user facts, preferences, or important details to PCG (Personal Context Graph).
  Use when the user explicitly tells you something you should remember.
  Unified PIC + KG-API + LIAM service.
parameters:
  type: object
  properties:
    content:
      type: string
      description: "The fact or preference to save (natural language)"
    category:
      type: string
      enum: [communication, work, scheduling, learning, health, social, creative, finance, technology, food, family, other]
      description: "Category for organizing the memory"
  required:
    - content
---

# PCG Save Memory

Save user facts, preferences, or important details to the Personal Context Graph (PCG) for persistent memory across all homelab agents.

## When to Invoke

- User explicitly states a preference ("I prefer concise responses")
- User corrects you ("Actually, I work from home on Fridays")
- User shares personal info they'd want remembered ("My daughter's birthday is in March")
- User says "remember this" or "don't forget"
- User provides context that should persist across conversations

## Architecture

```
Nova Agent
    │
    └─► PIC (port 8765)
        │
        ├─► Neo4j (graph storage)
        │   └─► :Observation nodes → consolidated into :Preference nodes
        │
        └─► ChromaDB (vector search)
            └─► Semantic search over preferences
```

## Instructions

### Step 1: Confirm with user (REQUIRED)
**Never save_memory without explicit user confirmation.** Always ask first.

Say: "Should I remember that for future conversations?" or "Want me to save that preference?"

### Step 2: Call save_memory with fact and category
Write the fact in third person. Pick the most appropriate category.

### Step 3: Confirm save
Tell the user it's been saved to their personal memory.

## Examples

<example>
User: I prefer espresso on the rocks, not hot coffee
Assistant: Should I remember that for future conversations?
User: Yes
Assistant: [call save_memory fact="User prefers espresso on the rocks, not hot coffee" category=food]
Result: Saved to PIC (food): User prefers espresso on the rocks, not hot coffee
Assistant: Got it — I'll remember you prefer espresso on the rocks.
</example>

<example>
User: My daughter Sofia has soccer practice on Tuesdays at 4pm
Assistant: Want me to save that for future reference?
User: Sure
Assistant: [call save_memory fact="User's daughter Sofia has soccer practice on Tuesdays at 4pm" category=family]
Result: Saved to PIC (family): User's daughter Sofia has soccer practice...
Assistant: Saved — I'll remember Sofia's Tuesday soccer practice.
</example>

<example>
User: Actually, I don't like being called "sir" — just use my name
Assistant: I'll make a note of that. Should I save this preference?
User: Yes please
Assistant: [call save_memory fact="User prefers to be addressed by name, not 'sir'" category=communication]
Result: Saved to PIC (communication): User prefers to be addressed by name...
Assistant: Understood — I'll address you by name going forward.
</example>

## Categories

| Category | Use For |
|----------|---------|
| `communication` | Response style, address preferences, language |
| `work` | Work habits, schedule, tools, preferences |
| `scheduling` | Calendar preferences, meeting habits, reminders |
| `learning` | Learning style, topics of interest, goals |
| `health` | Health preferences, diet restrictions, exercise |
| `social` | Social preferences, family context, relationships |
| `creative` | Creative preferences, tools, style |
| `finance` | Financial preferences, budgeting, priorities |
| `technology` | Tech preferences, devices, platforms |
| `food` | Food preferences, allergies, favorites |
| `family` | Family details, events, relationships |
| `other` | Anything that doesn't fit other categories |

## Technical Details

- PIC URL: http://localhost:8765
- Endpoint: POST `/api/pic/learn` (observation) or `/api/pic/preferences` (direct)
- Backend: Neo4j (graph) + ChromaDB (vectors)
- Cache: Session-scoped, invalidated on write
- Timeout: 5 seconds

## References

- Script: `skills/pic-memory/scripts/pic_memory.py`
- Handler: `nova/tools.py` → `handle_save_memory`
- PIC API: http://localhost:8765/api/pic
