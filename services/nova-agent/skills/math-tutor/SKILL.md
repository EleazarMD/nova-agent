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

## STAAR Test Format (4th Grade Math)

Based on official TEA STAAR blueprint: 32 questions, 40 points.
- 24 one-point questions (multiple-choice + non-multiple-choice)
- 8 two-point questions (multipart, constructed response)
- Answer choices are presented **vertically** — one per line, stacked

### Reporting Categories

| Category | TEKS Focus | Questions | Points |
|----------|-----------|-----------|--------|
| 1: Numerical Representations & Relationships | Fractions, comparing, equivalence, decimals | 7-9 | 8-12 |
| 2: Computations & Algebraic Relationships | Operations, multi-step, patterns, input-output | 10-12 | 12-16 |
| 3: Geometry & Measurement | Angles, perimeter, area, measurement | 8-10 | 9-13 |
| 4: Data Analysis & Financial Literacy | Graphs, tables, income, expenses | 3-5 | 3-6 |

## Problem Format Variety

Use a mix of problem types — STAAR uses multiple formats. Variety keeps students engaged and prepares them for the real test.

### Type 1: Multiple Choice (vertical — STAAR standard)
4 choices labeled A, B, C, D — **stacked vertically**, one per line.

```
Problem 1 — Adding Fractions
Sofia ran 3/8 of a mile in the morning and 2/8 of a mile after school.
How far did she run in total?

A   5/8
B   1/8
C   6/8
D   5/16
```

### Type 2: Fill in the Blank (□)
Student writes the answer in the box. Use □ as the answer blank.

```
Problem 2 — Fraction of a Whole
Luca has 12 baseball cards. He gave 1/3 of them to his friend.
How many cards did Luca give away?  □
```

### Type 3: Compare with >, <, or =
Student picks the correct comparison symbol.

```
Problem 3 — Comparing Fractions
Fill in the blank with >, <, or =:

3/4  ___  2/3
```

### Type 4: Equivalent Fractions (find the missing number)
Student fills in the missing numerator or denominator.

```
Problem 4 — Equivalent Fractions
2/4 = □/8
```

### Type 5: Number Line
Show a number line with a point and ask the student to identify the fraction.

```
Problem 5 — Fractions on a Number Line
What fraction does point P represent?

|----|----|----|----|
0         P         1
          ↑
     (point at 3/4)

A   1/4
B   2/4
C   3/4
D   4/4
```

### Type 6: Multipart (2 points — STAAR format)
Part A is multiple choice, Part B requires explanation or another answer.

```
Problem 6 — Multi-Step Fractions (2 points)

Part A
Sofia made 2/3 of a batch of cookies. She gave 1/3 to Luca.
What fraction of the batch does she have left?

A   1/3
B   1/6
C   3/3
D   2/6

Part B
Explain how you found your answer to Part A. □
```

### Distractor Rules
- Distractors should reflect common misconceptions or calculation errors
  - e.g., for 3/8 + 2/8: distractors include 1/8 (subtracted instead), 6/8 (added denominators), 5/16 (added both num+denom)
- Offer the correct answers as a **separate Answer Key** section at the end

### Answer Key Format

After all problems:

```
---
Answer Key
1. A   2. 4   3. >   4. 4   5. C   6. A + explanation
```

## STAAR-Aligned Problem Structure

1. **Real-world context** — Use relatable scenarios (Sofia, Luca, cooking, sports, school, pets)
2. **Clear question stem** — One question per problem
3. **Mix of problem types** — Multiple choice, fill-in (□), comparison (>/< /=), number lines
4. **Answer Key** — Separate section at the end

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
- Fill-in blanks: `□` — TTS reads as "blank"
- Comparison: `>  <  =` — TTS reads as "greater than, less than, equals"
- Number lines: describe the position verbally too — "point P is three marks from zero"
- Avoid raw LaTeX — keep it plain text with unicode symbols
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
    {"type": "heading_2", "content": "Problem 2 — Fraction of a Whole"},
    {"type": "paragraph", "content": "Luca has 12 baseball cards. He gave 1/3 of them to his friend. How many cards did Luca give away?  □"},
    {"type": "heading_2", "content": "Problem 3 — Comparing Fractions"},
    {"type": "paragraph", "content": "Fill in the blank with >, <, or =:"},
    {"type": "paragraph", "content": "3/4  ___  2/3"},
    {"type": "divider"},
    {"type": "heading_2", "content": "Answer Key"},
    {"type": "paragraph", "content": "1. A   2. 4   3. >"},
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
