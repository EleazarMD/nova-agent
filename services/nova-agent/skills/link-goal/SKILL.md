---
name: link-goal
tool_name: link_goal_to_knowledge
description: >
  Link a known goal to a knowledge entity for the PCG.
parameters:
  type: object
  properties:
    goal_id:
      type: string
    entity_id:
      type: string
    context:
      type: string
  required:
    - goal_id
    - entity_id
---

# Link Goal
PCG internal tool.
