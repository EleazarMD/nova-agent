---
name: personal-context-graph
description: >
  Personal Context Graph (PCG) client for persistent user memory, preferences, and identity.
  Unified interface for PIC (Personal Identity Core) and Context Bridge.
  Port 8765 (PIC) / 8764 (Bridge).
---

# Personal Context Graph

Access and manage personal identity data, preferences, and goals through the Personal Context Graph (PCG). This skill provides persistent memory across all conversations and agents.

## Triggers

- User expresses a preference ("I like...", "I prefer...")
- User shares personal facts ("My wife is...", "My kids are...")
- You need to recall something the user told you previously
- You need unified context across personal data and the knowledge graph

## Tools

### save_memory
Save user facts, preferences, or important details. **Requires explicit user confirmation first.**

### recall_memory
Search for stored preferences, facts, or personal details about the user.

### query_context
Get unified context across personal data (PIC) and infrastructure knowledge (KG).

## Instructions

1. **Saving Memory**: Always ask: "Should I remember that for future conversations?" before calling `save_memory`.
2. **Recalling Memory**: Call `recall_memory` whenever you need to check what you already know about the user's preferences or family.
3. **Third Person**: Always store facts in the third person (e.g., "User prefers espresso" not "I prefer espresso").

## Examples

<example>
User: "Remember that my wife's name is Claudia."
Assistant: "I'll make a note of that. Should I save her name to your permanent memory?"
User: "Yes."
Assistant: [call save_memory content="User's wife is named Claudia" category="family"]
</example>

## Technical Details

- PIC Service: http://localhost:8765
- Context Bridge: http://localhost:8764
- Backend: Neo4j (Graph) + ChromaDB (Vectors)
