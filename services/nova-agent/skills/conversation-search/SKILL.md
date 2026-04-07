---
name: conversation-search
tool_name: search_past_conversations
description: >
  Search Nova's conversation history in PostgreSQL and ChromaDB.
  Use for finding past conversations, retrieving context, or recalling previous discussions.
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Search query string"
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

Search through Nova's conversation history using semantic search and date filtering.

## When to Invoke

- Finding past conversations about a topic
- Retrieving context from previous discussions
- Searching for specific information mentioned in past chats
- Looking up conversations within a date range
- Recalling what was discussed about a subject

## Actions

### search
Search conversations by query with optional date filtering.

**Parameters:**
- `query` (required): Search query string
- `days_back`: How many days back to search (default: 30, max: 365)
- `limit`: Maximum results to return (default: 5, max: 20)
- `from_days`: Start of date range (days ago)
- `to_days`: End of date range (days ago)

## Examples

User: What did we discuss about homelab diagnostics last week?
Assistant: Invoking @conversation-search with query="homelab diagnostics", days_back=7

User: Find conversations about Tesla integration from the past 3 months
Assistant: Invoking @conversation-search with query="Tesla integration", days_back=90

User: Search for OpenClaw discussions between 7 and 3 days ago
Assistant: Invoking @conversation-search with query="OpenClaw", from_days=7, to_days=3

## Technical Details

- Backend: PostgreSQL + ChromaDB via Dashboard API
- Endpoint: `/api/memory/conversations/search`
- Timeout: 10 seconds
- Results include conversation snippets with timestamps

## References

- Script: `scripts/execute.py`
- Dashboard API: `http://localhost:8404`
