---
name: nova-exomind
description: Access and query the Exomind knowledge system. Retrieve information from the knowledge graph and personal knowledge base.
---

# Exomind

Interfaces with the Exomind knowledge system to retrieve information from the knowledge graph and personal knowledge base.

## When to Invoke

- User asks about personal knowledge
- Querying the knowledge graph
- "What do I know about..."
- Knowledge base queries
- Information retrieval from PKB

## Actions

- **query**: Query knowledge base
- **search**: Search knowledge graph
- **facts**: Get facts about a topic
- **related**: Find related concepts

## Parameters

- `query`: Natural language query
- `topic`: Specific topic to query
- `depth`: Query depth/precision
- `limit`: Maximum results

## Examples

User: "What do I know about Python?"
Assistant: Invoking @nova-exomind to query your knowledge base...

User: "Search my notes for machine learning"
Assistant: Invoking @nova-exomind to search your knowledge graph...

## References

- Handler: `handle_exomind()` in tools.py
- Service: Exomind/PKB
