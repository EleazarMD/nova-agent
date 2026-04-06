---
name: web-search
tool_name: web_search
description: >
  Search the web for current information using Perplexity Sonar. Use for news, facts, current events,
  prices, reviews, sports scores, stock prices, flight status, movie releases, and any general knowledge
  question that may need up-to-date information. Returns grounded results with citations. Fast (~2-5s).
  Do NOT use openclaw_delegate for simple searches — use this tool instead.
parameters:
  type: object
  properties:
    query:
      type: string
      description: >
        The search query. Be specific and include dates or context when relevant
        (e.g. "Super Mario Bros Movie 2025 release date", "Tesla stock price today").
  required:
    - query
---

# Web Search

Fast, grounded web search with citations using Perplexity Sonar API via AI Gateway.

## When to Invoke

- User asks about current events, news, or recent happenings
- Fact-checking claims or verifying statements
- Any question that may need information newer than training data
- Finding real-time data (prices, scores, weather, flight status)
- Movie releases, sports results, product launches
- Queries requiring authoritative citations
- Any web search task — Nova handles ALL web searches directly

**Important**: Nova ALWAYS handles web search itself. Only long-horizon deep research tasks
(multi-step analysis, comprehensive reports) go to OpenClaw's Deep Research Studio.

## Instructions

### Step 1: Call web_search with a specific query
Include relevant context in the query — dates, full names, specifics.

### Step 2: Read the results
The tool returns grounded text with citation count. Citations are automatically
sent to iOS for display.

### Step 3: Speak the results naturally
Weave the search results into your spoken response. Never output raw tool syntax.

## Model Selection

Automatic based on Nova's operational mode (set at session start):
- **Fast Mode** → `sonar` (2-5 seconds)
- **Deep Mode** → `sonar-pro` (5-10 seconds, more comprehensive)

## Examples

<example>
User: "What's the new Super Mario movie?"
Action: Call web_search(query="new Super Mario movie 2025 2026 release")
Result: The tool returns search results with citations about the latest release.
</example>

<example>
User: "Tesla stock price?"
Action: Call web_search(query="Tesla TSLA stock price today")
Result: Current price and market data with source URLs.
</example>

<example>
User: "Who won the F1 race this weekend?"
Action: Call web_search(query="Formula 1 race results this weekend")
Result: Race winner and standings with citations.
</example>

<example>
User: "Look up the best Italian restaurants near me"
Action: Call web_search(query="best Italian restaurants Humble TX Houston area")
Result: Restaurant recommendations with ratings and sources.
</example>

## Response Format

- Concise, factual answer with specific data points
- Source attribution (citation count appended)
- Structured citation URLs sent to iOS automatically

## Error Handling

- Connection failures → inform user, suggest retry
- Timeouts → recommend simpler/more specific query
- No results → "Search returned no results"
- Errors do NOT trigger OpenClaw delegation

## References

- Script: `scripts/web_search.py`
- AI Gateway: `http://127.0.0.1:8777/api/v1`
- Perplexity Docs: https://docs.perplexity.ai/
