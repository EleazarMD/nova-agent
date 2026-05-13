---
name: reminder-manager
tool_name: set_reminder
description: >
  Set a reminder. Will notify via push notification.
parameters:
  type: object
  properties:
    message:
      type: string
      description: "The reminder message"
    when:
      type: string
      description: "When to remind (e.g. 'in 30 minutes', 'at 3pm')"
  required:
    - message
    - when
---

# Set Reminder

Schedules a push notification reminder for the user.
