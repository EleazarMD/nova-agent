---
name: web-search
tool_name: web_search
description: >
  DEFAULT TOOL for recent events, factual data, current information, news, prices, reviews,
  sports scores, movie releases, and ANY question that may involve data newer than your training.
  ALWAYS call this FIRST for recent/factual queries — do NOT check internal sources first,
  do NOT ask permission, just search. Returns grounded results with citations. Fast (~2-5s).
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

**DEFAULT BEHAVIOR**: When the user asks about recent events, current facts, or anything
that may have changed since your training cutoff, call `web_search` IMMEDIATELY. Do NOT:
- Check `check_studio` or `search_past_conversations` first
- Ask permission to search
- Say "I don't have internal data" and wait

Just search. That's what this tool is for.

Fast, grounded web search with citations using Perplexity Sonar API via AI Gateway.

## When to Invoke (DEFAULT FIRST)

**Call web_search FIRST (no pre-checks, no permission) for:**
- Recent events, news, or current happenings
- Movie releases, sports results, product launches
- Current prices (stocks, products, flights)
- Reviews, ratings, critic opinions
- Weather, traffic, real-time status
- Fact-checking claims
- Any question involving data that may be newer than training

**The rule**: If the user's question involves "recent", "current", "latest", "today",
"this week", or any temporal indicator suggesting up-to-date info → web_search immediately.

## Instructions

### Step 1: Call web_search immediately
Do not check internal sources first. Do not ask "want me to look that up?". Just call the tool.

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
