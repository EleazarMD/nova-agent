---
name: pcg-query-context
tool_name: query_context
description: >
  Query unified Personal Context Graph (PCG) for context across personal data, knowledge graph facts, and LIAM frameworks.
  Use when you need a comprehensive overview of everything known about a topic (e.g. 'what is the context for my Starbucks order?').
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Search query for the context graph"
    include_personal:
      type: boolean
      description: "Include PIC personal data (default: true)"
    include_knowledge:
      type: boolean
      description: "Include infrastructure knowledge graph facts (default: true)"
    include_dimensions:
      type: boolean
      description: "Include LIAM frameworks/dimensions (default: true)"
  required:
    - query
---

# PCG Query Context

Unified query interface for the Personal Context Graph (PCG). Orchestrates results across PIC (identity/preferences), KG-API (homelab facts), and LIAM (reasoning frameworks).

## Triggers

- User asks for a summary of what you know about a project or person
- You need deep background context before starting a complex task
- Unified knowledge retrieval is required

## Examples

<example>
User: "What's the context for my meeting with Dr. Coleman?"
Assistant: [call query_context query="Dr. Coleman meeting"]
</example>
