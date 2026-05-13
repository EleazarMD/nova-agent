---
name: service-restart
tool_name: service_restart
description: Restart a container.
parameters:
  type: object
  properties:
    container:
      type: string
  required:
    - container
---
