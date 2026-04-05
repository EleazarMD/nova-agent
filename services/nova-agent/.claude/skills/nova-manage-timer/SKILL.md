---
name: nova-manage-timer
description: Manage timers and countdowns. Start, stop, pause, and check status of kitchen timers, pomodoros, or any countdown needs.
---

# Manage Timer

Controls timers and countdowns for time-boxed activities. Useful for cooking, Pomodoro technique, or any timed tasks.

## When to Invoke

- User asks to set a timer
- "Set a timer for..."
- Cooking/baking countdowns
- Pomodoro sessions
- Time-boxed activities

## Actions

- **start**: Start a new timer
- **stop**: Stop a running timer
- **pause**: Pause a timer
- **resume**: Resume a paused timer
- **status**: Check timer status
- **list**: List all active timers

## Parameters

- `duration`: Timer duration (e.g., "5 minutes", "30 seconds", "1 hour")
- `name`: Timer name/label
- `timer_id`: Timer identifier for operations

## Examples

User: "Set a timer for 10 minutes"
Assistant: Invoking @nova-manage-timer to start a 10-minute timer...

User: "Start a pomodoro timer"
Assistant: Invoking @nova-manage-timer to start a 25-minute pomodoro...

## References

- Handler: `handle_manage_timer()` in tools.py
