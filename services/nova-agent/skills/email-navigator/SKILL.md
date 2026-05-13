---
name: email-navigator
tool_name: query_cig
description: >
  Navigate emails, threads, contacts, and the communication graph via the
  Communication Intelligence Graph (CIG). Covers: reading recent/urgent emails,
  semantic search, full thread traversal, person/contact lookup, VIP and cooling
  contact lists, live graph stats, inbox action items, and relationship network maps.
  All calls are READ-ONLY — use hub_delegate(agent='hermes') for drafting or sending.
parameters:
  type: object
  properties:
    domain:
      type: string
      enum: [email, search, thread, person, contacts, graph, actions, calendar, briefing]
      description: Which CIG domain to query
    query:
      type: string
      description: Search query or sub-mode (vips, cooling, stats, network, briefing type)
    item_id:
      type: string
      description: >
        Opaque ID for targeted lookups. MUST be the verbatim `id`/`message_id`
        returned by a prior `search` call (for `thread`) or a full email address
        (for `person`). DO NOT pass a subject line, human label, or free-form
        text like 'Wingstop Order Confirmation' — those will 404. If you don't
        yet have an ID, call `query_cig` with `domain='search'` first and copy
        the `id:` field from the result.
  required:
    - domain
---

# Email Navigator

`query_cig` is Nova's primary interface for everything related to email,
contacts, and the communication relationship graph in CIG (Communication
Intelligence Graph). 176k+ emails, 23k+ persons, 3.2k threads, all in Neo4j.

---

## Domain Decision Tree

```
User asks about...
│
├── "my recent emails" / "what's in my inbox" / "any urgent emails"
│     → query_cig(domain='email')
│
├── "emails about X" / "any email from Y" / "find emails mentioning Z"
│     → query_cig(domain='search', query='X or Y or Z')
│
├── "show me that thread" / "what's the full conversation?" / "get the replies"
│     → query_cig(domain='thread', item_id=<thread_id or message_id>)
│     Strategy: first find the email ID via 'search', then fetch the thread.
│
├── "who is [name]?" / "tell me about [person]" / "what's my relationship with X?"
│     → query_cig(domain='person', item_id=<email_address>)
│     Strategy: if you don't have the email, search for it first.
│
├── "who are my VIPs?" / "show me key contacts"
│     → query_cig(domain='contacts', query='vips')
│
├── "who have I not talked to lately?" / "cooling relationships"
│     → query_cig(domain='contacts', query='cooling')
│
├── "relationship health" / "contact scores"
│     → query_cig(domain='contacts')
│
├── "how many emails do I have?" / "graph stats" / "what's indexed?"
│     → query_cig(domain='graph', query='stats')
│
├── "who do I communicate with most?" / "contact network" / "show my network"
│     → query_cig(domain='graph', query='network')
│     Optionally: item_id=<email_address> to center on a specific person
│
├── "what do I need to do?" / "inbox tasks" / "pending action items"
│     → query_cig(domain='actions')
│
├── "what's on my calendar?" / "upcoming meetings"
│     → query_cig(domain='calendar')
│
└── "my morning briefing" / "Hermes briefing" / "what's urgent right now"
      → query_cig(domain='briefing', query='morning')  # or evening/heartbeat/etc.
```

---

## Domain Reference

### `email` — Recent/urgent inbox

**When**: User asks about recent emails, unread messages, or inbox overview.

```
query_cig(domain='email')
```

Returns top-10 recent emails with subject, sender, date.
Tip: if the user named a topic or sender, use `search` instead.

---

### `search` — Semantic email search (176k emails)

**When**: User references a topic, keyword, person, date range, or subject.

```
query_cig(domain='search', query='keynote speaker slides November')
query_cig(domain='search', query='from Dr. Raven budget proposal')
```

Returns ranked emails with subject, sender, date, snippet, and message_id.
Always capture the `id` field — you'll need it for `thread` or detail lookups.

---

### `thread` — Full conversation chain

**When**: User wants to see a full back-and-forth conversation, all replies to
an email, or navigate the thread graph (REPLIES_TO edges, 5,015 chains).

**Requires `item_id`** — an opaque message_id or thread_id, NOT a subject.

```
# ✅ Right — opaque ID from a prior search result
query_cig(domain='thread', item_id='d07cb88a-bcaf-4ad4-8d13-3bc6fc1b101f@ind1s01mta864.xt.local')

# ❌ Wrong — these will return HTTP 404, every time
query_cig(domain='thread', item_id='Wingstop Order Confirmation')   # subject, not an ID
query_cig(domain='thread', item_id='Dr. Smith Q3 budget')           # human label
query_cig(domain='thread', item_id='lunch order')                   # free-form text
```

**Mandatory two-step pattern when you don't yet have the ID:**
1. `query_cig(domain='search', query='<subject or sender>')` — read the
   `id:` line under the matching result (each result has one).
2. `query_cig(domain='thread', item_id=<that exact id, verbatim>)`

Never guess, paraphrase, or reconstruct an ID. If `search` returned no usable
ID, tell the user you couldn't find that thread rather than making one up.

Returns: all messages in chronological order, with sender, date, snippet,
REPLIES_TO pointer, and individual message IDs.

---

### `person` — Contact profile lookup

**When**: User asks about a specific person — their role, org, VIP status,
relationship health, topics discussed, AI summary.

**Requires `item_id`** — the contact's email address.

```
query_cig(domain='person', item_id='raven.jones@houstonmethodist.org')
```

If you only know their name:
1. `query_cig(domain='search', query='from Raven Jones')` — grab `from_email`
2. `query_cig(domain='person', item_id=<email from step 1>)`

Returns: name, org, VIP flag, ai_importance, relationship health, last contact,
total interactions, AI-extracted topics, AI summary.

---

### `contacts` — Contact lists and health

**When**: User wants VIP list, cooling relationships, or overall relationship scores.

```
query_cig(domain='contacts', query='vips')       # ✅ VIP list (is_vip=true)
query_cig(domain='contacts', query='cooling')    # Silent contacts (30+ days quiet)
query_cig(domain='contacts')                     # Relationship health scores
```

Cooling contacts are VIPs/high-importance people with no activity in 30+ days —
great for proactive outreach suggestions.

---

### `graph` — Live graph stats and contact network

**When**: User asks about scale ("how many emails?"), or wants to see who they
communicate with most.

```
query_cig(domain='graph', query='stats')
# → 205k nodes, 68k edges, 23k persons, 176k emails, 3.2k threads, 5k reply chains

query_cig(domain='graph', query='network')
# → Contact network centered on you (top 20 by email_count)

query_cig(domain='graph', query='network', item_id='colleague@example.com')
# → Contact network centered on that person
```

Graph model in Neo4j:
- `(:Person)-[:SENT]->(:Email)-[:SENT_TO]->(:Person)` — all 176k emails
- `(:Email)-[:PART_OF]->(:Thread)` — 3,264 threads
- `(:Email)-[:REPLIES_TO]->(:Email)` — 5,015 reply chains
- `(:Person)-[:COMMUNICATES_WITH]->(:Person)` — 60,486 bidirectional edges
- `(:Person)-[:WORKS_AT]->(:Organization)` — org rollup

---

### `actions` — Inbox action items

**When**: User asks what they need to do, pending follow-ups, deadlines from email.

```
query_cig(domain='actions')
```

Returns: action type (task/commitment/deadline/follow-up), email subject,
sender, due date, urgency.

---

### `calendar` — Upcoming events

**When**: User asks about upcoming meetings or their schedule.

```
query_cig(domain='calendar')
```

---

### `briefing` — Hermes synthesized briefing

**When**: User asks for morning briefing, EOD summary, urgency scan, or Hermes briefing.

```
query_cig(domain='briefing')                      # most recent of any type
query_cig(domain='briefing', query='morning')     # morning briefing
query_cig(domain='briefing', query='heartbeat')   # Hermes heartbeat scan
```

---

## Multi-Step Navigation Examples

### "Show me the thread for that email from Dr. Smith about the Q3 budget"
```
Step 1: query_cig(domain='search', query='Dr. Smith Q3 budget')
Step 2: query_cig(domain='thread', item_id=<message_id from step 1>)
```

### "Who is Gabriel Morales and how well do we communicate?"
```
Step 1: query_cig(domain='search', query='from Gabriel Morales')  → grab from_email
Step 2: query_cig(domain='person', item_id='gabriel.morales@example.com')
```

### "What's the full conversation in that keynote email chain?"
```
Step 1: query_cig(domain='search', query='keynote speaker slides')
Step 2: query_cig(domain='thread', item_id=<message_id>)
Narrate: "That's a 5-message thread from Oct 14 to Oct 22…"
```

### "Any cooling relationships I should reach out to?"
```
query_cig(domain='contacts', query='cooling')
Narrate: "You have 8 important contacts who've gone quiet…"
```

---

## What this tool does NOT do

- **Draft or send emails** → `hub_delegate(agent='hermes', method='hermes.draft')`
- **Analyze attachments/spreadsheets** → `analyze_spreadsheet`
- **Book meetings** → `hub_delegate(agent='hermes', method='hermes.meeting-prep')`
- **Web search** → `web_search`

---

## Error Handling

| Situation | Response |
|-----------|----------|
| No `item_id` for `thread` or `person` | Ask user for the email ID or address |
| Thread lookup returned 404 / "not found" | You probably passed a subject or label as `item_id`. Do NOT retry the same `thread` call. Run `query_cig(domain='search', query=...)` first and pass the verbatim `id:` from a result. |
| Person not found | Verify it's an email address, not a name. Search first if needed. |
| CIG unavailable (timeout) | "CIG is temporarily unavailable. Try again in a moment." |
| Empty results | "No results found — try a broader search term" |

---

## References

- CIG service: `http://localhost:8780` (port 8780)
- Auth: `X-API-Key: nova-agent-key-2024`
- Neo4j: `bolt://localhost:7689` (graphrag2025)
- Key endpoints:
  - `GET /v1/threads/{id}` — full thread
  - `GET /v1/emails/{id}/thread` — email → thread
  - `GET /v1/contacts/{email}` — person profile
  - `GET /v1/contacts/vips` — VIP list
  - `GET /v1/contacts/cooling` — cooling contacts
  - `GET /v1/graph/stats` — live graph counts
  - `GET /v1/graph/contacts?center_email=X` — contact network
  - `GET /v1/inbox/actions` — action items
  - `POST /v1/search/emails` — semantic search
