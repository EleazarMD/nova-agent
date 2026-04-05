---
name: web-search
description: >
  Web search using Perplexity Sonar (fast mode) or Sonar Pro (deep mode) based on agent operational mode.
  Use for real-time information, current events, fact-checking, and research queries.
---

# Web Search

Fast, grounded web search with citations using Perplexity Sonar API via AI Gateway.

## When to Invoke

- Looking up current information or recent events
- Fact-checking claims or statements
- Research queries requiring web sources
- Finding real-time data (weather, stocks, news)
- Questions outside Nova's knowledge cutoff
- Queries requiring authoritative citations

## Model Selection

Search model is automatically selected based on Nova's operational mode:

- **Fast Mode**: `sonar` - Quick searches (2-5 seconds)
- **Deep Mode**: `sonar-pro` - Comprehensive research with deeper analysis

## Features

- **Grounded Results**: No hallucination, all answers cite sources
- **Structured Citations**: URLs provided with each result
- **iOS Integration**: Citations displayed in user interface
- **Fast Response**: Typical 2-5 seconds for sonar, 5-10 seconds for sonar-pro

## Citation Flow

1. Query sent to AI Gateway with model selection
2. AI Gateway routes to Perplexity API
3. Perplexity returns content + citations array
4. Nova extracts content and citations
5. Citations sent to iOS via server message (type: "sources")
6. iOS displays citations in UI

## Parameters

- `query` (required): Search query string

## Examples

User: What's the weather in Houston today?
Assistant: Invoking @web-search query="current weather Houston Texas"

User: Who won the latest Formula 1 race?
Assistant: Invoking @web-search query="latest Formula 1 race winner"

User: What are the current Tesla stock prices?
Assistant: Invoking @web-search query="Tesla stock price today"

## Response Format

Search results include:
- Concise, factual answer with specific data points
- Source attribution
- Citation count appended to response
- Structured citation URLs sent to iOS

## Technical Details

- API: Perplexity Sonar via AI Gateway
- Models: `sonar` (fast), `sonar-pro` (deep)
- Endpoint: `/api/v1/chat/completions`
- Timeout: 15 seconds
- Max tokens: 1024
- Citation limit: 5 URLs displayed in UI

## Error Handling

- Connection failures: Suggest using openclaw_delegate
- Timeouts: Recommend simpler query or openclaw_delegate
- No results: Returns "Search returned no results"

## References

- Script: `scripts/web_search.py`
- AI Gateway: http://127.0.0.1:8777/api/v1
- Perplexity Docs: https://docs.perplexity.ai/
