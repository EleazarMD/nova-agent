---
name: service-logs
tool_name: service_logs
description: Get container logs.
parameters:
  type: object
  properties:
    container:
      type: string
    lines:
      type: integer
  required:
    - container
---
