---
name: knowledge-query
tool_name: knowledge_query
description: >
  Query across personal knowledge (PIC) and general knowledge (KG-API) through the Context Bridge.
  Use for questions that might need both personal context AND general facts.
  This is the PRIMARY tool for complex knowledge synthesis.
  Port 8764 (Context Bridge), backed by Neo4j + ChromaDB.
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Natural language query. Be specific about what you need."
    include_personal:
      type: boolean
      description: "Include PIC identity, goals, preferences. Default: true"
      default: true
    include_knowledge:
      type: boolean
      description: "Include KG-API entities, facts, documents. Default: true"
      default: true
    include_dimensions:
      type: boolean
      description: "Include LIAM dimension matches and frameworks. Default: true"
      default: true
  required:
    - query
---

# Knowledge Query

Query across personal knowledge (PIC) and general knowledge (KG-API) through the Context Bridge.

## When to Invoke

- Questions that might need both personal context AND general facts
- "What frameworks apply to my clinical workflow goal?"
- "How should I approach the Coleman follow-up?"
- "Find connections between my goals and what I know"
- Complex knowledge synthesis requiring multiple sources
- PRIMARY tool for knowledge synthesis

## Architecture

```
Nova Agent
    │
    └─► Context Bridge (port 8764)
        │
        ├─► PIC (port 8765)
        │   └─► Personal: identity, goals, preferences
        │
        ├─► KG-API (port 8765)
        │   └─► Knowledge: entities, facts, documents
        │
        └─► LIAM
            └─► Dimensions and frameworks
```

## Instructions

### Step 1: Call knowledge_query with a natural language query
Be specific about what you need. The query will be routed to PIC, KG-API, and LIAM.

### Step 2: Read the synthesized response
Returns:
- `personal`: PIC identity, goals, preferences relevant to query
- `knowledge`: KG-API entities, facts, documents
- `applicable_dimensions`: LIAM frameworks detected
- `synthesis`: Pre-formatted summary for LLM consumption

### Step 3: Weave results into response
Use the synthesis field as context for your answer.

## Examples

<example>
User: What frameworks apply to my clinical workflow goal?
Assistant: [call knowledge_query query="clinical workflow goal frameworks"]
Result: synthesis contains goal details + applicable LIAM frameworks
Assistant: For your clinical workflow optimization goal, the relevant frameworks are...
</example>

<example>
User: How should I approach the Coleman follow-up?
Assistant: [call knowledge_query query="Coleman follow-up approach"]
Result: synthesis contains relevant context from PIC and KG
Assistant: Based on your context and the Coleman case details...
</example>

<example>
User: Find connections between my goals and what I know
Assistant: [call knowledge_query query="connections between goals and knowledge"]
Result: synthesis shows goal-entity relationships from knowledge graph
Assistant: Your goal "transition to AI engineering" connects to these knowledge entities...
</example>

## Response Structure

```json
{
  "personal": {
    "identity": {...},
    "goals": [...],
    "preferences": [...]
  },
  "knowledge": {
    "entities": [...],
    "facts": [...],
    "documents": [...]
  },
  "applicable_dimensions": [
    {"dimension_id": "habits", "frameworks": [...]}
  ],
  "synthesis": "Pre-formatted synthesis..."
}
```

## Technical Details

- Context Bridge URL: http://localhost:8764
- Endpoint: `/v1/query`
- Backend: Neo4j (graph) + ChromaDB (vectors)
- Timeout: 10 seconds
- Parallel queries to PIC, KG-API, and LIAM

## References

- Script: `nova/context_bridge.py`
- Handler: `nova/tools.py` → `handle_knowledge_query`
- Context Bridge API: http://localhost:8764
