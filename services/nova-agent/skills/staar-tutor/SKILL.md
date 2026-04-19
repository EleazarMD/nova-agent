---
name: staar-tutor
description: >
  Generate TEKS-aligned STAAR practice problems via the STAAR Tutor microservice.
  Supports multiple problem types (multiple choice, fill-in, comparison, number line,
  multipart), reporting categories, and student progress tracking. Use when the user
  asks for STAAR prep, TEKS-aligned problems, or structured practice sessions.
tool_name: staar_tutor
parameters:
  type: object
  properties:
    action:
      type: string
      enum:
        - generate
        - create_session
        - submit_answer
        - get_progress
        - list_teks
        - list_categories
      description: "Action to perform"
    grade:
      type: integer
      description: "Grade level (4 or 5, default 4)"
    count:
      type: integer
      description: "Number of problems to generate (1-32, default 5)"
    categories:
      type: array
      items:
        type: integer
      description: "Filter by reporting categories (1-4). 1=Fractions/Numbers, 2=Computations/Algebra, 3=Geometry/Measurement, 4=Data/Financial"
    teks:
      type: array
      items:
        type: string
      description: "Filter by specific TEKS standards (e.g. ['4.3A', '4.3D'])"
    types:
      type: array
      items:
        type: string
      description: "Problem types: multiple_choice, fill_blank, comparison, number_line, multipart"
    student_name:
      type: string
      description: "Student name for personalization and progress tracking"
    session_id:
      type: string
      description: "Session ID for answer submission"
    problem_id:
      type: string
      description: "Problem ID for answer submission"
    answer:
      type: string
      description: "Student's answer to submit"
    difficulty:
      type: string
      enum:
        - easy
        - medium
        - hard
      description: "Problem difficulty level"
    seed:
      type: integer
      description: "Random seed for reproducible problem sets"
  required:
    - action
---

# STAAR Tutor — TEKS-Aligned Problem Generation

## When to Invoke

- User asks for STAAR practice problems, TEKS-aligned exercises, or test prep
- User wants structured practice sessions with scoring
- User wants to track student progress by TEKS standard
- User mentions specific TEKS standards or reporting categories

## Intent Disambiguation

- "Give me some STAAR problems" / "Generate practice exercises" / "Quiz Sofia on fractions" → **speak the problems inline** — use `staar_tutor` with `action=generate`, then read the problems aloud
- "Create a practice session" / "Start a quiz session for Sofia" → **use `staar_tutor` with `action=create_session`** — creates a trackable session with scoring
- "How is Sofia doing on fractions?" / "Show me progress" → **use `staar_tutor` with `action=get_progress`**

## Progress Narration

When this skill requires a tool call, you MUST speak to the user before and during the task:
- **Before calling the tool**: Say what you're doing — "Let me generate some STAAR problems for Sofia."
- **After the tool returns**: Confirm the result — "Here are 5 fraction problems, STAAR style."
- **If the task takes multiple steps**: Narrate each step — "Creating the session first... now generating the problems."
Never go silent while a tool is running. The user should always hear what's happening.

## STAAR Reporting Categories

| # | Category | Focus |
|---|----------|-------|
| 1 | Numerical Representations & Relationships | Fractions, comparing, equivalence, decimals, place value |
| 2 | Computations & Algebraic Relationships | Operations, multi-step, patterns, input-output tables |
| 3 | Geometry & Measurement | Angles, lines, shapes, perimeter, area, measurement |
| 4 | Data Analysis & Financial Literacy | Data representation, analysis, income, expenses, profit |

## Problem Display Format

When speaking problems inline, use **vertically stacked** answer choices (STAAR standard):

```
Problem 1 — Adding Fractions
Sofia ran 3/8 of a mile in the morning and 2/8 of a mile after school.
How far did she run in total?

A   5/8
B   1/8
C   6/8
D   5/16
```

For fill-in-the-blank: use □ symbol. For comparisons: use `>  <  =` between fractions.

## Using the API

The STAAR Tutor service runs on port 8790. The `staar_tutor` tool handles all API calls.

### Generate Problems
```
staar_tutor(action="generate", count=5, categories=[1], student_name="Sofia")
```

### Create a Practice Session
```
staar_tutor(action="create_session", student_name="Sofia", count=5, categories=[1])
```

### Submit an Answer
```
staar_tutor(action="submit_answer", session_id="sess-abc123", problem_id="prob-xyz789", answer="A")
```

### Get Student Progress
```
staar_tutor(action="get_progress", student_name="Sofia")
```

### List TEKS Standards
```
staar_tutor(action="list_teks", categories=[1])
```

## Answer Key

Always provide a separate Answer Key section at the end, not with each problem.
