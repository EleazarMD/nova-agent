---
name: analyze-spreadsheet
tool_name: analyze_spreadsheet
description: >
  Analyze a spreadsheet, CSV, or tabular data file attached to an email. Pulls
  the attachment text from CIG (xlsx, csv, pdf tables, etc.) and delegates to
  the Atlas research agent on Pi Agent Hub for structured data analysis.
  Returns a concise report with shape, direct answer, patterns/outliers, and
  data-quality caveats. Slow (30-180s) — ack before calling.
parameters:
  type: object
  properties:
    email_id:
      type: string
      description: The email message_id containing the attachment (from CIG).
    question:
      type: string
      description: >
        What the user wants to know. Be specific — e.g. "top 3 expense
        categories", "any Q3 revenue outliers", "summarize the dataset".
    attachment_index:
      type: integer
      description: Which attachment (0-based). Default 0 = first tabular attachment.
  required:
    - email_id
    - question
---

# Analyze Spreadsheet

Turns a spreadsheet/CSV/table attached to an email into a structured analysis
produced by Atlas (Claude Sonnet 4 via Pi Agent Hub).

## When to Invoke

Call `analyze_spreadsheet` when the user asks to:

- "Analyze the spreadsheet Raven sent me"
- "What's in the survey results attachment?"
- "Summarize the CSV from the Q3 report email"
- "Are there any outliers in the budget attached to that email?"
- "Compute the totals from the expense sheet Gabriel forwarded"

If the user references an attachment but you don't have the email_id yet, first
call `check_studio(studio='email', action='recent')` or `query_cig(domain='search', query='...')`
to locate the email, then call `analyze_spreadsheet` with that `email_id`.

## Supported File Types

- **Preferred (tabular)**: `.xlsx`, `.csv`, `.xls`, `.tsv`, `.ods`
- **Also supported**: `.pdf`, `.docx`, `.html` (tables extracted as text)

The tool auto-prefers tabular attachments when `attachment_index=0`.

## Progress Narration

This is a **slow tool** (30-180s). ALWAYS narrate before and during:

1. **Before calling**: "Let me pull that attachment and send it to Atlas for analysis."
2. **While running**: The bot framework shows a thinking card automatically, but
   you may add a spoken "Still working — Atlas is crunching the numbers" if a
   heartbeat fires.
3. **After it returns**: Deliver the analysis in plain prose (spoken). Never
   read raw tables aloud — summarize the key findings.

## Instructions

### Step 1: Locate the email
If the user names a sender or subject, use CIG search to find the email_id.

### Step 2: Narrate and call
Say what you're doing, then call:
```
analyze_spreadsheet(
  email_id="<message_id>",
  question="<specific question in user's words>",
  attachment_index=0  # omit unless user says "the second attachment"
)
```

### Step 3: Deliver the report
Atlas returns a structured report with:
1. Dataset shape (sheets/columns/rows)
2. Direct answer to the question with cell references
3. Patterns, totals, outliers
4. Data-quality caveats

Weave these into spoken prose. Keep under 400 words.

## Examples

<example>
User: "Can you analyze the spreadsheet in Raven's latest email?"
Action 1: query_cig(domain='search', query='Raven attachment spreadsheet')
Action 2: Say "Pulling the spreadsheet and sending it to Atlas now."
Action 3: analyze_spreadsheet(email_id=<found>, question="Summarize the data and flag anything unusual")
Result: Report the findings conversationally.
</example>

<example>
User: "What are the top expense categories in that budget attachment?"
Action: analyze_spreadsheet(email_id=<known>, question="What are the top 3 expense categories by total?")
Result: Atlas returns ranked categories with totals — speak them plainly.
</example>

## Error Handling

- **No attachments** → "That email doesn't have any attachments."
- **No tabular files** → Report available filenames, ask user which to analyze.
- **Extraction fails** → Report the filename and HTTP status, suggest user open in source app.
- **Hub unavailable** → "Pi Agent Hub is down — I can't delegate analysis right now."
- **Timeout** → "Atlas is taking longer than expected. I'll keep watching."

## References

- Handler: `nova/tools.py::handle_analyze_spreadsheet`
- CIG extraction: `POST /v1/attachments/extract-text/{email_id}/{idx}` (port 8780)
- Atlas RPC: `atlas.analyze-data` on Pi Agent Hub (ws://127.0.0.1:18900)
- Underlying extractor: `services/cig/attachment_extract.py` (openpyxl for xlsx)
- Context budget: 40,000 chars max (truncated with caveat if larger)
