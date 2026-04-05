---
name: nova-query-frameworks
description: Query frameworks and technologies via Context Bridge. Access information about available frameworks, libraries, and technical documentation.
---

# Query Frameworks

Queries frameworks and technologies through the Context Bridge service. Provides information about available frameworks, libraries, and technical documentation.

## When to Invoke

- User asks about technology stacks
- Framework information needed
- "What frameworks are available?"
- Technical documentation queries
- Library information requests

## Actions

- **list**: List available frameworks
- **info**: Get framework information
- **docs**: Query documentation
- **compare**: Compare frameworks

## Parameters

- `framework`: Framework name to query
- `technology`: Technology category
- `query`: Specific query string

## Examples

User: "What Python web frameworks are available?"
Assistant: Invoking @nova-query-frameworks to find available frameworks...

User: "Tell me about React"
Assistant: Invoking @nova-query-frameworks for React information...

## References

- Script: `services/nova-agent/skills/query-frameworks/scripts/query_frameworks.py`
- Service: Context Bridge (port 8764)
