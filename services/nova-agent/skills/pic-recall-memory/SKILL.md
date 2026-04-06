---
name: pic-recall-memory
tool_name: recall_memory
description: >
  Search PIC (Personal Identity Core) for stored preferences, facts, or personal details about the user.
  Use when you need to check what you already know before asking the user.
  Port 8765, backed by Neo4j + ChromaDB for semantic search over personal data.
parameters:
  type: object
  properties:
    query:
      type: string
      description: >
        What to look up (e.g. "coffee order", "kids names", "work schedule", "food preferences").
        Use natural language — the search is semantic.
    category:
      type: string
      enum:
        - communication
        - work
        - scheduling
        - learning
        - health
        - social
        - creative
        - finance
        - technology
        - food
        - family
        - other
      description: "Optional: narrow search to a specific category"
  required:
    - query
---

# PIC Recall Memory

Search the Personal Identity Core (PIC) for stored preferences, facts, and personal details about the user.

## When to Invoke

- User asks "do you remember what I told you about X?"
- User asks "what do you know about my Y?"
- You need to check stored preferences before making assumptions
- User references past conversations ("remember that restaurant I mentioned?")
- You want to personalize a response based on known preferences

## Architecture

```
Nova Agent
    │
    └─► PIC (port 8765)
        │
        ├─► Neo4j (graph storage)
        │   └─► :Preference nodes with relationships
        │
        └─► ChromaDB (vector search)
            └─► Semantic search over all stored data
```

## Instructions

### Step 1: Call recall_memory with a search query
Use natural language. The search is semantic, so "coffee" will match "espresso preferences".

### Step 2: Read the results
Returns a list of matching preferences and observations with relevance scores.

### Step 3: Use the information naturally
Weave the recalled information into your response without explicitly saying "I searched my memory".

## Examples

<example>
User: What do you know about my coffee preferences?
Assistant: Let me check—
[call recall_memory query="coffee preferences"]
Result: [{category: food, key: coffee_order, value: "espresso on the rocks"}, ...]
Assistant: You prefer espresso on the rocks — not hot coffee.
</example>

<example>
User: Do you remember my daughter's name?
Assistant: [call recall_memory query="daughter name family"]
Result: [{category: family, key: daughter_name, value: "Sofia"}, ...]
Assistant: Yes, your daughter is named Sofia.
</example>

<example>
User: What's my work setup again?
Assistant: [call recall_memory query="work setup schedule" category=work]
Result: [{category: work, key: work_schedule, value: "WFH on Fridays"}, ...]
Assistant: You work from home on Fridays, and your primary workspace is...
</example>

## Search Tips

- **Broad queries**: "food" or "preferences" will return all matches
- **Specific queries**: "Starbucks order" will match coffee preferences
- **Category filtering**: Use `category` parameter to narrow results
- **Synonyms work**: "kids" will match "children", "daughter", "son"

## Technical Details

- PIC URL: http://localhost:8765
- Endpoint: GET `/api/pic/preferences`
- Backend: Neo4j (graph) + ChromaDB (vectors)
- Cache: Session-scoped, reads from cache if warm
- Timeout: 5 seconds

## References

- Script: `skills/pic-memory/scripts/pic_memory.py`
- Handler: `nova/tools.py` → `handle_recall_memory`
- PIC API: http://localhost:8765/api/pic
