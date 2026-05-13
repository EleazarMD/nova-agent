---
name: control-lights
tool_name: control_lights
description: >
  Control Philips Hue lights via openhue CLI. Use for 'turn on lights', 'dim bedroom', 'set to blue'.
parameters:
  type: object
  properties:
    action:
      type: string
      enum: ["on", "off", "brightness", "color", "scene", "status"]
      description: "Action to perform"
    target:
      type: string
      description: "Light name, room name, or scene name"
    value:
      type: string
      description: "Brightness (0-100) or color hex (#FF0000)"
  required:
    - action
---

# Control Lights

Manage the local Philips Hue smart lighting system.
