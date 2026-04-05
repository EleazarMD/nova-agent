---
name: pic-memory
description: >
  Personal Identity Core (PIC) client for reading and writing personal data, preferences, and goals.
  Single source of truth for user identity across all homelab agents.
---

# PIC Memory

Access and manage personal identity data, preferences, and goals through the Personal Identity Core (PIC).

## When to Invoke

- Reading user preferences or identity
- Recording new observations about the user
- Saving explicit preferences
- Accessing user goals
- Building personalized context for conversations
- Learning from user statements

## Architecture

PIC is the **single source of truth** for personal data across all homelab agents.

**Data Flow:**
- Session start → `build_pic_context()` → system prompt (cached)
- Mid-session → `get_preferences()` / `get_identity()` (from cache)
- User states preference → `record_observation()` → PIC → cache invalidated
- OpenClaw discoveries → POST `/api/pic/learn` → PIC (write-through)

**Backend:** Neo4j (graph) + Redis (cache)

## Read Operations

### get_identity
Get user identity profile (name, timezone, bio, roles).

### get_preferences
Get user preferences, optionally filtered by category.

**Parameters:**
- `categories`: Optional list of categories to filter

### get_goals
Get user goals filtered by status.

**Parameters:**
- `status`: Goal status (default: "active")

### build_pic_context
Build complete PIC context for session initialization.

**Returns:**
- User name and timezone
- Preferences by category
- Active goals
- Memory snippets for system prompt

## Write Operations

### record_observation
Record a learning observation about the user.

**Parameters:**
- `observation_type` (required): Type of observation
- `category` (required): Preference category
- `key` (required): Preference key
- `value` (required): Observed value
- `context`: Additional context

**Note:** Invalidates cache, observations consolidated into preferences over time.

### create_preference
Create or update a preference directly.

**Parameters:**
- `category` (required): Preference category
- `key` (required): Preference key
- `value` (required): Preference value
- `context`: Additional context
- `source`: Source type (default: "explicit")

**Note:** Invalidates cache on success.

## Preference Categories

- `communication`: Communication style preferences
- `productivity`: Work and productivity preferences
- `lifestyle`: Lifestyle and habits
- `technology`: Technology preferences
- `other`: Uncategorized preferences

## Examples

User: I prefer concise responses
Assistant: Recording preference... @pic-memory record_observation, category=communication, key=response_style, value=concise

User: What are my active goals?
Assistant: Invoking @pic-memory get_goals, status=active

User: Remember that I like dark mode
Assistant: Invoking @pic-memory create_preference, category=technology, key=theme, value=dark_mode, source=explicit

## Caching Strategy

- **Session-scoped in-process cache**: Avoids repeated HTTP calls within a session
- **Cache invalidation**: Automatic on writes, next read fetches fresh data
- **Warm-up**: First read loads all data (identity, preferences, goals)

## Technical Details

- PIC URL: http://localhost:8765
- Authentication: X-PIC-Read-Key (read), X-PIC-Admin-Key (write)
- Timeout: 5 seconds
- Cache: In-process, session-scoped

## References

- Script: `scripts/pic_memory.py`
- PIC API: http://localhost:8765/api/pic
- Backend: Neo4j + Redis
