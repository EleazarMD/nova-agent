---
name: nova-openclaw-delegate
description: Delegate complex tasks to OpenClaw agent. Use for browser automation, file operations, code editing, and multi-step tasks requiring desktop environment access.
---

# OpenClaw Delegate

Delegates complex multi-step tasks to the OpenClaw agent. OpenClaw can perform browser automation, file operations, code editing, and other desktop-based tasks.

## When to Invoke

- Task requires browser interaction
- File system operations needed
- Multi-step desktop automation
- "Can you look up..." (web search)
- Code editing or file modifications
- Email or calendar operations
- Tasks needing shell access

## Actions

- **delegate**: Send task to OpenClaw
- **status**: Check delegation status
- **result**: Get delegation result

## Parameters

- `task`: Description of what needs to be done
- `context`: Additional context or constraints
- `priority`: Task priority (low/normal/high/urgent)

## Examples

User: "Can you look up the latest React docs?"
Assistant: Invoking @nova-openclaw-delegate to search the React documentation...

User: "Edit the config file to add port 8080"
Assistant: Invoking @nova-openclaw-delegate to modify the configuration file...

## Notes

- Shows thinking card during delegation
- Progress updates sent to UI
- Results returned when complete

## References

- Handler: `handle_openclaw_delegate()` in tools.py
- Service: OpenClaw (port 18793)
