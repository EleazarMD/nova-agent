---
name: nova-control-lights
description: Control smart home lights and lighting scenes. Turn lights on/off, adjust brightness, set colors, and activate lighting scenes.
---

# Control Lights

Manages smart home lighting through connected lighting systems. Supports on/off control, brightness adjustment, color changes, and scene activation.

## When to Invoke

- User asks to turn lights on or off
- Adjusting brightness
- Changing light colors
- Activating lighting scenes
- "Dim the lights"
- "Turn on the living room lights"

## Actions

- **on**: Turn lights on
- **off**: Turn lights off
- **toggle**: Toggle light state
- **brightness**: Set brightness level (0-100)
- **color**: Set light color
- **scene**: Activate a lighting scene

## Parameters

- `action`: Operation type (on/off/toggle/brightness/color/scene)
- `room**: Room or area identifier
- `light_id**: Specific light identifier
- `brightness`: Brightness percentage (0-100)
- `color`: Color name or hex code
- `scene**: Scene name to activate

## Examples

User: "Turn on the living room lights"
Assistant: Invoking @nova-control-lights to turn on lights...

User: "Dim the bedroom lights to 30%"
Assistant: Invoking @nova-control-lights with brightness=30...

## References

- Handler: `handle_control_lights()` in tools.py
