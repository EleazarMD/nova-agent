---
name: workstation-status
tool_name: get_workstation_status
description: >
  Get RTX Workstation status: GPU temps, VRAM, running models.
parameters:
  type: object
  properties:
    detail:
      type: string
      enum: ["summary", "full", "alerts"]
      description: "Level of detail (default summary)"
---

# Workstation Status

Check the health and utilization of the local NVIDIA RTX compute node.
