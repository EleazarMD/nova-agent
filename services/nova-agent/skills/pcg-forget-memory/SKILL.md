---
name: pcg-forget-memory
tool_name: forget_memory
description: >
  Record a correction in PIC (Personal Context Graph) when the user says to forget something 
  or when a stored fact is no longer true.
parameters:
  type: object
  properties:
    keyword:
      type: string
      description: "Keyword or topic to forget/correct (e.g. 'morning coffee', 'work schedule')"
  required:
    - keyword
---

# PCG Forget Memory

Correct or "forget" information in the Personal Context Graph (PCG). Since PIC is an observation-based system, this records a 'correction' observation that overrides previous facts.

## Triggers

- User says "forget that"
- User says "actually that's not true anymore"
- User provides a correction to a previously stored fact

## Examples

<example>
User: "Actually, I don't work on Fridays anymore."
Assistant: [call forget_memory keyword="work on Fridays"]
</example>
