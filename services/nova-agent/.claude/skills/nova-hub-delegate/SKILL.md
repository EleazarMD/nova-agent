---
name: nova-hub-delegate
description: Delegate complex tasks to Pi Agent Hub. Use for browser automation (Argus), research (Atlas), infrastructure diagnostics (Infra), and multi-step tasks.
---

# Hub Delegate

Delegates complex multi-step tasks to the Pi Agent Hub. Hub agents include Argus (browser), Atlas (research), and Infra (diagnostics).

## When to Invoke

- Task requires browser interaction
- File system operations needed
- Multi-step desktop automation
- "Can you look up..." (web search)
- Code editing or file modifications
- Email or calendar operations
- Tasks needing shell access

## Actions

- **delegate**: Send task to Pi Agent Hub
- **status**: Check delegation status
- **result**: Get delegation result

## Parameters

- `task`: Description of what needs to be done
- `context`: Additional context or constraints
- `priority`: Task priority (low/normal/high/urgent)

## Examples

User: "Can you look up the latest React docs?"
Assistant: Invoking @nova-hub-delegate to search the React documentation...

User: "Edit the config file to add port 8080"
Assistant: Invoking @nova-hub-delegate to modify the configuration file...

## Notes

- Shows thinking card during delegation
- Progress updates sent to UI
- Results returned when complete

## References

- Handler: `handle_hub_delegate()` in tools.py
- Service: Pi Agent Hub (port 18793)
