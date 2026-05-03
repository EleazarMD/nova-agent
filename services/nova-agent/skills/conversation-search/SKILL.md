---
name: conversation-search
tool_name: search_past_conversations
description: >
  Search Nova's conversation history using semantic vector search (NVIDIA NIM + pgvector).
  Understands meaning, not just keywords — "lunch plans" finds food conversations.
  Use for finding past conversations, retrieving context, or recalling previous discussions.
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Natural language search query — describe what you're looking for"
    days_back:
      type: integer
      description: "How many days back to search (default: 30, max: 365)"
      default: 30
    limit:
      type: integer
      description: "Maximum results to return (default: 5, max: 20)"
      default: 5
    from_days:
      type: integer
      description: "Start of date range (days ago)"
    to_days:
      type: integer
      description: "End of date range (days ago)"
  required:
    - query
---

# Conversation Search

Search through Nova's conversation history using semantic vector search with date filtering.

## When to Invoke

- Finding past conversations about a topic
- Retrieving context from previous discussions
- Searching for specific information mentioned in past chats
- Looking up conversations within a date range
- Recalling what was discussed about a subject

## Search Strategy

1. **Primary**: NVIDIA NIM semantic vector search (llama-3.2-nv-embedqa-1b-v2, 2048-dim) via pgvector cosine similarity
2. **Fallback**: ILIKE keyword search if embeddings unavailable
3. **Local fallback**: SQLite keyword search

## Examples

User: What did we discuss about homelab diagnostics last week?
Assistant: Invoking @conversation-search with query="homelab diagnostics", days_back=7

User: What did we have for lunch yesterday?
Assistant: Invoking @conversation-search with query="lunch food meal yesterday", days_back=2

User: Find conversations about Tesla integration from the past 3 months
Assistant: Invoking @conversation-search with query="Tesla integration", days_back=90

## Technical Details

- Primary: NVIDIA NIM (port 8006) embeddings + pgvector cosine similarity on PostgreSQL
- Fallback: ILIKE keyword search on PostgreSQL
- Local fallback: SQLite keyword search
- All 3023+ messages embedded with 2048-dim vectors
- Results include conversation snippets with relevance scores

## References

- Script: `nova/store.py` → `search_past_conversations`
- PostgreSQL: `workspace.ai_messages.embedding` (vector(2048))
