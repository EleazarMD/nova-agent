---
name: nova-conversation-search
description: Search through conversation history and transcripts. Find past conversations, messages, and dialogue context.
---

# Conversation Search

Searches through conversation history and transcripts to find past messages, dialogue context, and previous interactions.

## When to Invoke

- User asks about previous conversations
- "What did we talk about..."
- Searching conversation history
- Finding past messages
- "Earlier you mentioned..."

## Actions

- **search**: Search conversation history
- **recent**: Get recent conversations
- **by_date**: Find conversations by date
- **by_topic**: Find conversations by topic

## Parameters

- `query`: Search query
- `date**: Specific date or range
- `topic**: Topic filter
- `limit`: Maximum results

## Examples

User: "What did we talk about yesterday?"
Assistant: Invoking @nova-conversation-search to find yesterday's conversation...

User: "Search for our discussion about Docker"
Assistant: Invoking @nova-conversation-search for Docker-related conversations...

## References

- Script: `services/nova-agent/skills/conversation-search/scripts/execute.py`
- Handler: `search_conversations()`
