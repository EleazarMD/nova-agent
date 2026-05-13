---
name: manage-timer
tool_name: manage_timer
description: >
  Start, stop, check, or list timers.
parameters:
  type: object
  properties:
    action:
      type: string
      enum: ["start", "stop", "check", "list"]
      description: "Timer action to perform"
    duration:
      type: string
      description: "Duration for new timer (e.g. '10m', '1h 30m', '45s')"
    name:
      type: string
      description: "Optional name/label for the timer (e.g. 'pasta', 'laundry')"
  required:
    - action
---

# Manage Timer

Create and manage local asynchronous timers for the user.
