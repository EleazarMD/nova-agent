---
name: studio-reader
description: >
  Read and analyze content from Studio (Apple Notes-like knowledge base).
  Use for accessing stored notes, documents, and knowledge base content.
---

# Studio Reader

Access and read content from Studio, the homelab's knowledge base and note storage system.

## When to Invoke

- Reading stored notes from Studio
- Accessing knowledge base articles
- Retrieving saved documents
- Searching Studio content
- Pulling reference materials from personal knowledge base

## Features

- Read notes and documents from Studio
- Search Studio content by title or tags
- Access knowledge base articles
- Retrieve structured information

## Status

**Note**: This skill is currently a placeholder. Full implementation pending Studio API integration.

## Planned Actions

### read
Read a specific note or document from Studio.

**Parameters:**
- `note_id`: Studio note identifier
- `title`: Note title for search

### search
Search Studio content.

**Parameters:**
- `query`: Search query
- `tags`: Filter by tags
- `limit`: Max results

### list
List recent or tagged notes.

**Parameters:**
- `tags`: Filter by tags
- `limit`: Max results
- `sort`: Sort order (recent, title, modified)

## Examples

User: Read my note about homelab architecture
Assistant: Invoking @studio-reader action=search, query="homelab architecture"

User: Show me my recent Studio notes
Assistant: Invoking @studio-reader action=list, limit=10, sort=recent

## Technical Details

- Backend: Studio API (pending integration)
- Storage: Local or cloud-based note storage
- Format: Markdown, rich text, attachments

## References

- Script: `scripts/` (implementation pending)
- Integration: Awaiting Studio API specification
