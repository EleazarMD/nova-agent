---
name: nova-set-reminder
description: Set reminders and alerts for future events, tasks, or deadlines. Schedule notifications at specific times or intervals.
---

# Set Reminder

Creates reminders and scheduled alerts for future events, tasks, or deadlines. Supports one-time and recurring reminders.

## When to Invoke

- User asks to set a reminder
- "Remind me to..."
- Scheduling future alerts
- Deadline notifications
- Recurring reminders (daily, weekly, etc.)

## Actions

- **once**: Set one-time reminder
- **recurring**: Set recurring reminder
- **list**: List active reminders
- **cancel**: Cancel a reminder

## Parameters

- `text`: Reminder message/content
- `time`: When to trigger (e.g., "5pm", "tomorrow 9am", "in 30 minutes")
- `recurrence`: For recurring: daily, weekly, monthly
- `reminder_id`: ID for canceling/updating

## Examples

User: "Remind me to call mom at 5pm"
Assistant: Invoking @nova-set-reminder to schedule this reminder...

User: "Remind me to take my medicine every morning"
Assistant: Invoking @nova-set-reminder to set up a daily reminder...

## References

- Handler: `handle_set_reminder()` in tools.py
