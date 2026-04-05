---
name: context-bridge
description: >
  Unified context retrieval across personal memory (PIC), knowledge graphs (LIAM), and conversation history.
  Use for complex queries requiring multiple context sources.
---

# Context Bridge

Unified context retrieval service that bridges personal memory, knowledge frameworks, and conversation history.

## When to Invoke

- Complex queries requiring multiple context sources
- Questions needing both personal preferences and knowledge frameworks
- Synthesizing information across PIC, LIAM, and conversation history
- Building comprehensive context for decision-making
- Queries that span personal data and scientific frameworks

## Architecture

Context Bridge provides a unified API that:
- Queries PIC for personal preferences and identity
- Searches LIAM for applicable frameworks and dimensions
- Retrieves relevant conversation history
- Synthesizes results into coherent context

## Query Types

### Unified Query
Single query across all context sources with intelligent synthesis.

**Parameters:**
- `query` (required): Natural language query
- `include_personal`: Include PIC personal data (default: true)
- `include_knowledge`: Include LIAM frameworks (default: true)
- `include_dimensions`: Include LIAM dimensions (default: true)
- `max_results`: Maximum results per source (default: 5)

## Response Structure

- **personal**: Personal preferences and identity data from PIC
- **knowledge**: Applicable frameworks from LIAM
- **applicable_dimensions**: LIAM dimensions detected
- **synthesis**: Unified synthesis of all context sources

## Examples

User: How should I approach building a new habit given my preferences?
Assistant: Invoking @context-bridge query="building new habit", include_personal=true, include_knowledge=true

User: What frameworks apply to my career decision?
Assistant: Invoking @context-bridge query="career decision frameworks"

## Use Cases

1. **Personalized Recommendations**: Combine user preferences with frameworks
2. **Decision Support**: Merge personal context with decision-making models
3. **Learning Optimization**: Match learning frameworks to user's learning style
4. **Goal Planning**: Align goals with applicable frameworks and past patterns

## Technical Details

- Endpoint: `/v1/query`
- URL: http://localhost:8764
- Timeout: 10 seconds
- Parallel queries to PIC, LIAM, and conversation DB
- Intelligent result synthesis

## Integration Points

- **PIC**: Personal preferences, identity, goals
- **LIAM**: Scientific frameworks, mental models, dimensions
- **Conversation History**: Past discussions and context
- **Neo4j**: Graph-based relationship traversal

## References

- Service: Context Bridge API
- URL: http://localhost:8764
- Dependencies: PIC (8765), LIAM, PostgreSQL, ChromaDB
