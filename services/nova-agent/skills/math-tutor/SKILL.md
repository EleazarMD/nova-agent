---
name: math-tutor
description: >
  Generate STAAR-aligned math problems, worksheets, and practice exercises
  for students (especially Sofia, 4th grade). Provides formatting rules for
  multiple-choice answer options, problem structure, and TTS-friendly output.
  Invoke when the user asks for math problems, quizzes, worksheets, homework
  help, or STAAR prep.
---

# Math Tutor — STAAR-Aligned Problem Generation

## When to Invoke

- User asks for math problems, practice exercises, or quiz questions
- User mentions STAAR prep, homework help, or tutoring
- User mentions Sofia and math/fractions/numbers in the same request

## Intent Disambiguation

- "Give me some problems" / "Generate exercises" / "Quiz Sofia" → **speak the problems inline** — no tool call, just respond with the problems
- "Create a worksheet page" / "Make a math page in my workspace" / "Build a quiz I can print" → **use manage_workspace** to create a page

## Progress Narration

When this skill requires a tool call (e.g., creating a workspace page), you MUST speak to the user before and during the task:
- **Before calling the tool**: Say what you're doing — "Let me build that worksheet page for you."
- **After the tool returns**: Confirm the result — "Done — I created 'Fraction Practice' in your workspace."
- **If the task takes multiple steps**: Narrate each step — "Adding the problems now... and the answer key at the bottom."
Never go silent while a tool is running. The user should always hear what's happening.

## STAAR Multiple-Choice Format (MANDATORY)

Every math problem MUST include 4 answer choices in STAAR format:

- **4 choices labeled A, B, C, D**
- **One correct answer** and **three plausible distractors**
- Distractors should reflect common misconceptions or calculation errors
  - e.g., for 3/8 + 2/8: distractors include 1/8 (subtracted instead), 6/8 (added denominators), 5/16 (added both num+denom)
- Offer the correct answers as a **separate Answer Key** section at the end, so the student can work through problems first

### Format

```
Problem N — [Skill Name]
[Real-world context and question]

A) [choice]  B) [choice]  C) [choice]  D) [choice]
```

...after all problems...

```
---
Answer Key
1. A   2. B   3. C   4. D   5. A
```

### Example

```
Problem 1 — Adding Fractions
Sophia ran 3/8 of a mile in the morning and 2/8 of a mile after school.
How far did she run in total?

A) 5/8  B) 1/8  C) 6/8  D) 5/16

Problem 2 — Subtracting Fractions
A pizza had 7/10 left. Sofia's family ate 4/10. How much is left?

A) 3/10  B) 11/10  C) 3/20  D) 4/10

---
Answer Key
1. A   2. A
```

## STAAR-Aligned Problem Structure

1. **Real-world context** — Use relatable scenarios (Sofia, Luca, cooking, sports, school, pets)
2. **Clear question stem** — One question per problem
3. **Four answer choices** — A through D
4. **Answer Key** — Offer as a separate section at the end so students can check after attempting all problems

## Grade-Level Skills (4th Grade TEKS)

| Topic | TEKS | Example Problems |
|-------|------|-----------------|
| Adding fractions (same denom) | 4.3A | 3/8 + 2/8 |
| Subtracting fractions | 4.3A | 7/10 - 4/10 |
| Fraction of a whole | 4.3A | 2/3 of 24 students |
| Comparing fractions | 4.3D | 5/6 vs 3/4 |
| Equivalent fractions | 4.3C | 2/4 = ?/8 |
| Mixed operations | 4.3A | 2/3 - 1/4 flour/sugar |
| Multi-step word problems | 4.4 | Combine operations |
| Decimals to fractions | 4.2G | 0.25 = 1/4 |
| Multiplying fractions by whole | 4.3E | 3 × 2/5 |

## TTS Formatting for Math

- Fractions: write as `3/8` (not "three-eighths") — TTS reads it naturally
- Answer choices: `A) 5/8` — speak as "A, five eighths"
- Avoid raw LaTeX or special symbols — keep it plain text
- Percent: write `25%` — TTS says "twenty-five percent"

## Creating Worksheet Pages

When the user asks for a workspace page, use `manage_workspace` with:
- `action="create_page_with_blocks"` — builds a structured page with all problems in one call
- `action="create_from_template"` with `template_id="tpl-math-worksheet"` — uses the Math Worksheet template

### Example: create_page_with_blocks

```
manage_workspace(
  action="create_page_with_blocks",
  title="Sofia — Fraction Addition Practice",
  icon="📐",
  properties={"blocks": [
    {"type": "heading_2", "content": "Problem 1 — Adding Fractions"},
    {"type": "paragraph", "content": "Sophia ran 3/8 of a mile in the morning and 2/8 of a mile after school. How far did she run in total?"},
    {"type": "paragraph", "content": "A) 5/8   B) 1/8   C) 6/8   D) 5/16"},
    {"type": "heading_2", "content": "Problem 2 — Subtracting Fractions"},
    {"type": "paragraph", "content": "A pizza had 7/10 left. Sofia's family ate 4/10. How much is left?"},
    {"type": "paragraph", "content": "A) 3/10   B) 11/10   C) 3/20   D) 4/10"},
    {"type": "divider"},
    {"type": "heading_2", "content": "Answer Key"},
    {"type": "paragraph", "content": "1. A   2. A"},
    {"type": "callout", "properties": {"icon": {"type": "emoji", "emoji": "✅"}, "calloutColor": "green", "title": [{"type": "text", "text": {"content": "Score: ___ / 2"}, "plainText": "Score: ___ / 2"}]}}
  ]}
)
```

## Distractor Strategy

Good distractors make wrong answers tempting by reflecting common errors:

| Error Type | Example | Distractor |
|-----------|---------|-----------|
| Add denominators | 3/8 + 2/8 → 5/16 | D) 5/16 |
| Subtract instead of add | 3/8 + 2/8 → 1/8 | B) 1/8 |
| Add both num+denom | 3/8 + 2/8 → 6/8 | C) 6/8 |
| Wrong common denominator | 5/6 vs 3/4 → compare 5/12 vs 3/12 | wrong comparison |
| Forget to simplify | 4/8 instead of 1/2 | D) 4/8 |

## References

- TEKS Mathematics Standards: https://tea.texas.gov/curriculum/teks
- STAAR Released Tests: https://tea.texas.gov/student-assessment/testing/staar-released-test-questions
- Workspace template: `tpl-math-worksheet`
