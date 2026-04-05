---
name: nova-web-search
description: Search the web using Perplexity Sonar via AI Gateway. Returns grounded results with citations for current information, news, facts, and general knowledge queries.
---

# Web Search

Performs fast web searches using Perplexity Sonar through the AI Gateway. Provides grounded, cited results suitable for current information needs.

## When to Invoke

- User asks for current news, prices, or recent events
- General knowledge questions requiring up-to-date information
- Fact-checking or verifying current data
- Research on topics that may have changed recently
- Sports scores, stock prices, weather forecasts
- Product reviews or comparisons

## Actions

- **search**: Execute web search with query
  - Parameters: `query` (string) - The search query
  - Returns: Search results with citation count
  - Citations displayed in iOS UI automatically

## Examples

User: "What's the latest news on AI regulation?"
Assistant: Invoking @nova-web-search to find current information...

User: "How much does the new iPhone cost?"
Assistant: Invoking @nova-web-search for current pricing...

## Usage Notes

- Search results include source citations
- iOS client displays citations in dedicated UI panel
- Typical response time: 2-5 seconds
- Perplexity Sonar is the only search engine used
- No hallucination - all results are grounded

## References

- Script: `services/nova-agent/skills/web-search/scripts/web_search.py`
- Handler: `handle_web_search()`
- AI Gateway: Port 8777
