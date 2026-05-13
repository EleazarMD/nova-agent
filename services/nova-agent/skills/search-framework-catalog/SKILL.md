---
name: search-framework-catalog
tool_name: search_framework_catalog
description: >
  Browse or inventory the LIAM Framework Database catalog by author, source, or keyword.
  Use for existence questions (is Taleb included?), author lookups, or source listing.
  NOT for selecting frameworks to apply to a problem — use query_frameworks for that.
parameters:
  type: object
  properties:
    query:
      type: string
      description: "Free-text catalog search across id, name/label, source, description, key concepts, dimensions, and limitations."
    author:
      type: string
      description: "Author/source-name filter, e.g. 'Taleb', 'Kahneman'."
    source:
      type: string
      description: "Source/work filter, e.g. 'Incerto', 'Thinking, Fast and Slow'."
    category:
      type: string
      description: "Optional framework category filter."
    dimension:
      type: string
      description: "Optional LIAM dimension id filter, e.g. 'metacognition' or 'financial'."
    limit:
      type: integer
      description: "Maximum number of frameworks to return."
      default: 20
  required: []
---

# Search Framework Catalog

Browse or inventory the LIAM Framework Database by author, source, keyword, or category.

## When to Use

- User asks "what frameworks do we have from Kahneman?"
- User asks "is Nassim Taleb included?"
- User wants a catalog listing by topic or source book
- Admin / inventory tasks

## When NOT to Use

- **Do NOT use this to select frameworks for a problem** — use `query_frameworks` instead.
- **Do NOT loop this tool.** Call it **once** per user request. If the result returns frameworks, use them. If it returns none, accept the result and move on.

## Stop Rule (CRITICAL — prevents tool loops)

**Call once. Accept the result. Do not retry.**

- Got results? Use what was returned. Do not call again with a slightly different query.
- Got empty results? That means the catalog has no matching frameworks. Tell the user and proceed without searching further.
- **Never call `search_framework_catalog` more than once in a single turn.** Each call uses the same underlying index — rephrasing the query will not produce meaningfully different results.

## Correct Workflow for Case Study / Article Work

1. Call `query_frameworks` ONCE with the problem description → get applicable frameworks
2. If needed, call `search_framework_catalog` ONCE to check for specific authors/sources
3. **Stop searching. Build the content using what you have.**
4. Call `manage_workspace(action="create_page_with_blocks", ...)` with the frameworks woven in
5. Do not search for more frameworks after you have started building

## Examples

User: "What Kahneman frameworks do we have?"
→ `search_framework_catalog(author="Kahneman", limit=10)`
→ Return the list. Done.

User: "Apply LIAM frameworks to my leadership problem"
→ Use `query_frameworks`, NOT `search_framework_catalog`
