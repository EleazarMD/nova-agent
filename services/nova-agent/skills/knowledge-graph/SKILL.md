---
name: knowledge-graph
tool_name: kg_query
description: >
  Query the AI Homelab Knowledge Graph (KG-API) for infrastructure context, service dependencies, and entity relationships.
  Part of PCG (Personal Context Graph) on port 8765.
  Use for questions about homelab services, their relationships, and infrastructure knowledge.
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Natural language query about homelab infrastructure or services"
    entity_type:
      type: string
      description: "Filter by entity type (Service, Component, Integration, DataStore)"
      default: "Service"
  required:
    - query
---

# Knowledge Graph

Query the AI Homelab Knowledge Graph (KG-API) for infrastructure context, service dependencies, and entity relationships.

## When to Invoke

- User asks about homelab infrastructure ("what services are running?")
- User asks about dependencies ("what does Nova depend on?")
- User asks about integrations ("what connects to Hermes?")
- User needs context about service relationships
- Debugging infrastructure issues ("why is X not working?")

## Architecture

```
Nova Agent
    │
    └─► Context Bridge (port 8764)
        │
        └─► KG-API (port 8765)
            │
            ├─► Neo4j (graph storage)
            │   └─► :Service, :Component nodes with :DEPENDS_ON, :USES relationships
            │
            └─► ChromaDB (vector search)
                └─► Semantic search over entities
```

## Instructions

### Step 1: Call kg_query with a natural language query
Use specific terms like service names or "dependencies" for better results.

### Step 2: Read the context
Returns:
- `entity`: Matched entity with properties
- `dependencies`: Services this entity depends on
- `integrations`: Services that use this entity
- `status`: Current status if available

### Step 3: Use the context in your response
Weave the graph context into your answer about infrastructure.

## Examples

<example>
User: What services depend on AI Gateway?
Assistant: [call kg_query query="AI Gateway dependencies"]
Result: Nova Agent, Hermes Core, OpenClaw all depend on AI Gateway
Assistant: AI Gateway is used by Nova Agent, Hermes Core, and OpenClaw for LLM routing.
</example>

<example>
User: What does Nova integrate with?
Assistant: [call kg_query query="Nova Agent integrations"]
Result: AI Gateway (LLM routing), Hermes Core (email/calendar), Tesla Relay, OpenClaw
Assistant: Nova integrates with AI Gateway for LLM calls, Hermes Core for email and calendar,
Tesla Relay for vehicle control, and delegates complex tasks to OpenClaw.
</example>

<example>
User: Why is my email not working?
Assistant: Let me check the email service dependencies—
[call kg_query query="Hermes Core dependencies"]
Result: Hermes Core depends on Neo4j, ChromaDB, and AI Gateway
Assistant: Hermes Core (email service) depends on Neo4j, ChromaDB, and AI Gateway.
If any of those are down, email won't work. Want me to check their status?
</example>

## Entity Types

| Type | Description |
|------|-------------|
| `Service` | Running services (Nova, Hermes, AI Gateway) |
| `Component` | Infrastructure components (databases, queues) |
| `Database` | Data stores (Neo4j, ChromaDB, PostgreSQL) |
| `Integration` | External integrations (Tesla, email providers) |
| `Agent` | AI agents (Nova, OpenClaw worker) |

## Technical Details

- KG-API URL: http://localhost:8765
- Backend: Neo4j (graph) + ChromaDB (vectors)
- Entity types: Service, Component, Database, Integration, Agent
- Relationship types: DEPENDS_ON, USES, INTEGRATES_WITH
- Timeout: 10 seconds

## References

- Script: `nova/knowledge_graph.py`
- Handler: `nova/tools.py` → `handle_kg_query`
- KG-API: http://localhost:8765/api/kg
