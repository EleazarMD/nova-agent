---
name: enriched-context
tool_name: get_enriched_context
description: >
  Get enriched personal context for the current conversation from the Context Bridge.
  Returns identity, goals with applicable frameworks, and relevant knowledge entities.
  Use at conversation start or when you need comprehensive personal context.
  Port 8764 (Context Bridge), backed by PCG (8765) with Neo4j + ChromaDB.
parameters:
  type: object
  properties:
    include_goals:
      type: boolean
      description: "Include active goals with applicable LIAM frameworks (default: true)"
      default: true
    include_relationships:
      type: boolean
      description: "Include knowledge graph relationships (default: false)"
      default: false
---

# Enriched Context

Get comprehensive personal context for the current conversation from the Context Bridge.

## When to Invoke

- Starting a new conversation session
- User asks "what do you know about me?"
- Need comprehensive context including identity, goals, and knowledge
- Building personalized responses with full context
- User wants to see their active goals and related frameworks

## Architecture

```
Nova Agent
    │
    └─► Context Bridge (port 8764)
        │
        ├─► PIC (port 8765)
        │   └─► Identity, preferences, goals
        │
        └─► KG-API (port 8765)
            └─► Knowledge entities, relationships
            
Both share same Neo4j + ChromaDB backend
```

## Instructions

### Step 1: Call get_enriched_context
No parameters required - returns all enriched context for the current user.

### Step 2: Read the response
Returns:
- `identity`: User name, timezone, bio, roles
- `goals`: Active goals with applicable LIAM frameworks
- `relevant_entities`: Knowledge graph entities related to goals/context
- `synthesis`: Pre-formatted summary for LLM consumption

### Step 3: Use context naturally
Weave the enriched context into your response without explicitly saying "I retrieved context".

## Examples

<example>
User: Tell me what you know about me
Assistant: [call get_enriched_context]
Result: {identity: {...}, goals: [...], relevant_entities: [...]}
Assistant: You're working on transitioning to AI engineering, and I know your preferences for morning routines...
</example>

<example>
User: What are my current goals?
Assistant: [call get_enriched_context include_goals=true]
Result: {goals: [{title: "Transition to AI engineering", frameworks: [...]}]}
Assistant: Your active goals are: 1) Transition to AI engineering (with habit stacking and decision frameworks applicable)...
</example>

## Response Structure

```json
{
  "identity": {
    "name": "User",
    "timezone": "America/Chicago",
    "bio": "...",
    "roles": ["..."]
  },
  "goals": [
    {
      "title": "Goal title",
      "status": "active",
      "applicable_frameworks": ["habit_stacking", "progress_principle"]
    }
  ],
  "relevant_entities": [
    {"name": "Entity", "type": "Service", "relevance": 0.85}
  ],
  "synthesis": "Pre-formatted context summary..."
}
```

## Technical Details

- Context Bridge URL: http://localhost:8764
- Endpoint: `/v1/enriched-context`
- Backend: Neo4j (graph) + ChromaDB (vectors)
- Timeout: 10 seconds
- Agent ID: nova-agent

## References

- Script: `nova/context_bridge.py`
- Handler: `nova/tools.py` → `handle_get_enriched_context`
- Context Bridge API: http://localhost:8764
