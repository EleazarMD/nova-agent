---
name: query-frameworks
description: >
  Query LIAM (Life Intelligence Augmentation Matrix) for scientific frameworks applicable to decisions, problems, or life questions.
  Use for finding mental models, decision-making frameworks, and cognitive tools.
---

# Query Frameworks

Query LIAM for scientific frameworks, mental models, and decision-making tools applicable to a problem or question.

## When to Invoke

- Making important decisions
- Analyzing complex problems
- Finding applicable mental models
- Seeking structured thinking frameworks
- Applying scientific approaches to life questions
- Using Model Thinker methodology (multiple frameworks)

## Actions

### query
Search for applicable frameworks using semantic search.

**Parameters:**
- `problem_description` (required): Natural language problem description
- `dimension_id`: Optional LIAM dimension filter (e.g., "habits", "decision_fatigue")
- `category`: Optional framework category filter
- `limit`: Maximum frameworks to return (default: 5)
- `use_context_bridge`: Try Context Bridge first for semantic search (default: true)

## Framework Categories

- **decision_making**: Decision-making frameworks
- **behavioral**: Behavioral science frameworks
- **cognitive**: Cognitive psychology frameworks
- **probabilistic**: Probabilistic thinking frameworks
- **computational**: Computational frameworks
- **systems**: Systems thinking frameworks

## Search Methods

### Semantic Search (Preferred)
Via Context Bridge - finds frameworks based on meaning and applicability.

### Filtered Search (Fallback)
Direct PIC query with dimension or category filters.

## Response Structure

- **applicable_frameworks**: List of relevant frameworks with:
  - Name and category
  - Authors
  - When to use
  - Key concepts
  - Core thesis
  - Limitations
  - Applicable dimensions
  - Relevance score (semantic search only)
- **dimensions_detected**: LIAM dimensions applicable to the problem
- **synthesis**: Brief explanation of how to apply frameworks

## Examples

User: Should I switch careers?
Assistant: Invoking @query-frameworks problem_description="Should I switch careers?"

User: How do I build better habits?
Assistant: Invoking @query-frameworks problem_description="How to build habits?", dimension_id=habits

User: Why is this project delayed?
Assistant: Invoking @query-frameworks problem_description="Why is this delayed?", category=systems, limit=3

## Model Thinker Approach

When multiple frameworks are returned, apply the **Model Thinker** methodology:
- Use multiple frameworks as different lenses
- Each provides unique insights
- Combine perspectives for robust understanding
- Avoid single-framework bias

## Technical Details

- Primary: Context Bridge semantic search (http://localhost:8764)
- Fallback: Direct PIC/LIAM query (http://localhost:8765)
- Timeout: 10 seconds
- Max results: Configurable (default: 5)

## References

- Script: `scripts/query_frameworks.py`
- Context Bridge: http://localhost:8764/v1/query
- PIC/LIAM: http://localhost:8765/api/pic/liam
- CLI: Supports standalone execution with `--json` output
