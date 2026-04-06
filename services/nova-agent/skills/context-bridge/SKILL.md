---
name: context-bridge
tool_name: query_context
description: >
  Unified context retrieval across personal memory (PIC), knowledge graphs (KG-API), and LIAM frameworks.
  Use for complex queries requiring multiple context sources. Returns synthesized results from all sources.
  Port 8764 orchestrates PIC (8765) and KG-API (8765) with shared Neo4j + ChromaDB backend.
parameters:
  type: object
  properties:
    query:
      type: string
      description: >
        Natural language query to search across PIC (personal data), KG-API (knowledge graph),
        and LIAM frameworks. Be specific (e.g. "habit building frameworks given my schedule preferences").
    include_personal:
      type: boolean
      description: Include PIC personal data, preferences, goals (default: true)
      default: true
    include_knowledge:
      type: boolean
      description: Include KG-API entities and relationships (default: true)
      default: true
    include_dimensions:
      type: boolean
      description: Include LIAM dimensions matching the query (default: true)
      default: true
  required:
    - query
---

# Context Bridge

Unified context retrieval service that bridges personal memory (PIC), knowledge graphs (KG-API), and LIAM frameworks.

## When to Invoke

- Complex queries requiring multiple context sources
- Questions needing both personal preferences AND knowledge frameworks
- Synthesizing information across PIC, KG-API, and LIAM
- Building comprehensive context for decision-making
- Queries that span personal data and scientific frameworks

## Architecture

```
Nova Agent
    │
    └─► Context Bridge (port 8764)
        │
        ├─► PIC (port 8765)
        │   └─► Personal data: identity, preferences, goals
        │
        └─► KG-API (port 8765)
            └─► Knowledge graph: entities, relationships
            
Both share same Neo4j + ChromaDB backend
```

## Instructions

### Step 1: Call query_context with a natural language query
The query will be routed to PIC, KG-API, and LIAM in parallel.

### Step 2: Read the synthesized response
Returns:
- `personal`: PIC identity, preferences, goals relevant to query
- `knowledge`: KG-API entities and relationships
- `applicable_dimensions`: LIAM frameworks detected
- `synthesis`: Pre-formatted summary for LLM consumption

### Step 3: Weave results into response
Use the synthesis field as context for your answer.

## Examples

<example>
User: How should I approach building a new habit given my preferences?
Assistant: Let me check your preferences and relevant frameworks—
[call query_context query="habit building frameworks given my preferences"]
Result: synthesis contains personal schedule preferences + habit formation frameworks
Assistant: Based on your preference for morning routines and the habit stacking framework...
</example>

<example>
User: What do you know about my career goals and relevant frameworks?
Assistant: Let me pull your goals and matching frameworks—
[call query_context query="career goals and decision frameworks"]
Result: synthesis contains active career goals + decision-making frameworks
Assistant: Your active goal is "transition to AI engineering" and the relevant frameworks are...
</example>

## Use Cases

1. **Personalized Recommendations**: Combine user preferences with frameworks
2. **Decision Support**: Merge personal context with decision-making models
3. **Learning Optimization**: Match learning frameworks to user's learning style
4. **Goal Planning**: Align goals with applicable frameworks and knowledge

## Technical Details

- Endpoint: `/v1/query`
- URL: http://localhost:8764
- Timeout: 10 seconds
- Parallel queries to PIC, KG-API, and LIAM
- Shared backend: Neo4j (graph) + ChromaDB (vectors)

## References

- Script: `nova/context_bridge.py`
- Service: Context Bridge API at http://localhost:8764
- Dependencies: PIC (8765), KG-API (8765), LIAM, Neo4j, ChromaDB
