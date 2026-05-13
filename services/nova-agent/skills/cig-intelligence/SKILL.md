---
name: cig-intelligence
description: >
  Read CIG's derivative-order intelligence layers — network topology
  (PageRank/Louvain/bridges), relationship health, sentiment trajectories,
  topic trends, commitments, event outcomes, strategic alerts, and the
  pre-computed morning brief. Use this skill when the user wants insight,
  patterns, or trends — NOT for raw email/calendar lookup.
---

# CIG Intelligence (3rd / 4th / 5th Order)

CIG continually derives higher-order intelligence from raw emails, events,
and contacts and persists it in Neo4j. This skill explains which endpoint
to call for each kind of question, what the data means, and how to combine
layers.

> **CIG base URL**: `http://localhost:8780`
> **Auth header**: `X-API-Key: $CIG_API_KEY` (default: `nova-agent-key-2024`)
> **Service**: `cig.service` (port 8780). If endpoints 404, the service may
> be running an old build — see `cig-tool-troubleshooting` skill.

## Decision Tree — Which Endpoint?

| User question | Endpoint |
|---|---|
| "What's on my plate this morning?" | `GET /v1/intelligence/morning-brief` |
| "Anything I should know about?" | `GET /v1/intelligence/alerts` |
| "Who are my most important contacts?" | `GET /v1/intelligence/topology/centrality` |
| "Who are my key connectors / bridges?" | `GET /v1/intelligence/topology/bridges` |
| "Who is in the same group as X?" | look up X's `cluster_id`, then `GET /v1/intelligence/topology/clusters/{id}` |
| "What groups/communities are in my network?" | `GET /v1/intelligence/topology/clusters` |
| "How's my relationship with X?" | `GET /v1/contacts/{email}/health` + `GET /v1/contacts/{email}/sentiment-arc` |
| "Show me everything about X" | `GET /v1/intelligence/context/person?email=X&days=90` |
| "What's trending in my inbox?" | `GET /v1/topics/trends` (or via `/morning-brief`) |
| "What did we decide in the X meeting?" | `GET /v1/calendar/{event_id}/outcome` |
| "What's coming up about topic Y?" | `GET /v1/intelligence/context/topic?topic=Y&days=30` |
| "What did I promise people?" | `GET /v1/commitments?direction=outbound&status=open` |
| "Is CIG's intelligence current?" | `GET /v1/intelligence/derivations/status` |

## Layer Reference

### 🥇 Always Start Here — Morning Brief

For any "what should I know right now" question, **prefer the morning brief
over multiple separate calls**. It's a single pre-computed graph read that
joins everything: today's events, active alerts, open commitments, trending
topics, cooling VIPs, urgent unread emails.

```
GET /v1/intelligence/morning-brief?days_ahead=1
```

Returns:
```json
{
  "today_events": [...],            // calendar (with has_brief/has_outcome flags)
  "active_alerts": [...],           // strategic alerts (high→low severity)
  "open_commitments": [...],        // outbound commitments only
  "trending_topics": [...],         // delta_pct > 10%
  "cooling_vips": [...],            // VIPs with declining health
  "urgent_unread_emails": [...],    // last 24h
  "summary": {                      // count block, scan-first
    "event_count": 4,
    "prep_needed": 2,
    "high_alerts": 1,
    ...
  }
}
```

**When NOT to use**: If the user asks a targeted question (e.g. "who's my
top connector?"), call the specific endpoint instead — don't dump the
whole brief.

### 🚨 Strategic Alerts (5th order — P3.1)

Persistent, deduplicated alerts with TTL covering 5 patterns:
`cooling_vip`, `trending_topic`, `overdue_commitment`, `pending_outcome`,
`pcg_insight`.

```
GET  /v1/intelligence/alerts?severity=high&type=cooling_vip&limit=20
POST /v1/intelligence/alerts/generate        # force a refresh cycle
POST /v1/intelligence/alerts/{alert_id}/dismiss
```

Each alert has: `alert_id`, `alert_type`, `subject`, `title`, `body`,
`severity` (high/medium/low), `source`, `generated_at`, `expires_at`.

Alerts auto-regenerate every 6h. If the user says "I already handled
that" → dismiss it so it won't reappear that cycle.

### 🕸️ Network Topology (4th order — P2.1)

Persisted on `:Person` nodes as `network_centrality` (PageRank, 0-1),
`cluster_id` (Louvain community), `is_bridge` (cut-vertex). Recomputed
every 24h via GDS.

```
GET  /v1/intelligence/topology/centrality?limit=20&min_score=0.0
GET  /v1/intelligence/topology/bridges?limit=20
GET  /v1/intelligence/topology/clusters?limit=20&sample_size=5
GET  /v1/intelligence/topology/clusters/{cluster_id}?limit=100
POST /v1/intelligence/topology/compute          # on-demand GDS rerun
```

**How to interpret**:
- **Centrality**: higher = more central to the user's communication graph.
  Top 5–10 are the people most of the network revolves around.
- **Bridge node**: removing this person would split the network into
  disconnected pieces. They are gatekeepers between groups — losing
  them silently is bad.
- **Cluster** (Louvain): a community of densely-connected people.
  Usually maps to a team, org, or project. The cluster has no built-in
  name — infer it from the top members' org/topic patterns.

**Combining**: a person who is **VIP + bridge + declining health** is the
single most strategic relationship in the user's network to repair.
Cross-reference with `/v1/contacts/{email}/health` and `/sentiment-arc`.

### 💬 Sentiment Trajectory (4th order — P2.2)

Two-week sentiment buckets on each `COMMUNICATES_WITH` edge, plus a
trend label.

```
GET /v1/contacts/{email}/sentiment-arc
```

Returns `sentiment_history` (array of `{bucket_start, avg_sentiment, n}`),
`sentiment_trend` (`warming` | `cooling` | `stable`), `sentiment_arc_at`.

Useful when answering "how has my relationship with X been lately?" —
present the trend, not the raw history, unless asked.

### 📅 Event Outcomes (4th order — P2.3)

LLM-extracted post-meeting intelligence: decisions made, action items
captured, commitments created, sentiment, follow-up topics.

```
GET  /v1/calendar/{event_id}/outcome
POST /v1/calendar/{event_id}/outcome          # generate on-demand
```

Auto-generated every 2h for events that ended 1–48h ago. If the user
asks about a recent meeting, call GET first; if empty/missing, POST to
generate then GET again.

### 🔍 Cross-Modal Context (4th order — P2.4)

The "tell me everything about X" endpoint. Joins emails, events,
commitments, topics, thread summaries, health, topology — for a single
person or topic.

```
GET /v1/intelligence/context/person?email=alice@example.com&days=90
GET /v1/intelligence/context/topic?topic=quarterly_review&days=30
GET /v1/intelligence/topics                  # list known topics
```

This is **the single richest endpoint**. Use it when the user wants a
deep dive on someone or something.

### ❤️ Relationship Health (3rd order — P1.1)

```
GET /v1/contacts/{email}/health
GET /v1/contacts/vips
GET /v1/contacts/cooling
POST /v1/contacts/backfill-health-scores
```

Health score is 0–100 (recency + frequency + relationship weight + mutual
initiation). `health_trend` is `warming` / `stable` / `declining`.

### ✅ Commitments (3rd order — P1.2)

Promises extracted from email action items.

```
GET   /v1/commitments?direction=outbound&status=open&limit=20
GET   /v1/commitments/{commitment_id}
PATCH /v1/commitments/{commitment_id}/status   # body: {"status":"fulfilled"}
POST  /v1/commitments/backfill
```

`direction`: `outbound` (user promised to someone) | `inbound` (someone
promised user). `status`: `open` | `fulfilled` | `overdue`.

### 📈 Topic Trends (3rd order — P1.3)

```
GET  /v1/topics/trends
POST /v1/topics/trends/run                   # force re-aggregation
```

Weekly buckets per topic. `delta_pct > 0.3` = notable spike.

### 🧵 Thread Summaries (3rd order — P1.4)

```
GET  /v1/threads/{thread_id}/summary
POST /v1/threads/{thread_id}/summarize
```

Auto-summarizes VIP/urgent threads with ≥5 messages every 4h.

### 🩺 Derivations Status (META)

**Call this first if uncertain whether a layer is populated.** Returns
coverage % and freshness for every derivation:

```
GET /v1/intelligence/derivations/status
```

Example response:
```json
{
  "phase_2_4th_order": {
    "network_topology": {
      "with_centrality": 142, "with_cluster": 142,
      "bridge_nodes": 8, "total_persons": 1834
    },
    "sentiment_arcs": {"covered": 56, "total_edges": 211},
    ...
  },
  "phase_3_5th_order": {
    "strategic_alerts": {"active": 7, "dismissed": 12, "expired": 41, ...}
  }
}
```

If `with_centrality == 0`, GDS hasn't run yet — call
`POST /v1/intelligence/topology/compute`.

## Examples

<example>
User: Who are my most important people right now?
Assistant: [GET /v1/intelligence/topology/centrality?limit=10]
→ Combine with [GET /v1/contacts/vips] to mark which are flagged VIP.
Reply: "Your network's most central contacts are: [name] (centrality 0.18,
VIP, declining), [name] (0.14, stable), …"
</example>

<example>
User: Who connects my work and personal networks?
Assistant: [GET /v1/intelligence/topology/bridges]
Reply: List bridge nodes ordered by centrality. Highlight any with
declining health — those are silent risks.
</example>

<example>
User: How was my morning's meeting with Sarah?
Assistant: First find the event_id (`/v1/calendar/events?...` or
`/calendar/today`), then:
  [GET /v1/calendar/{event_id}/outcome]
If outcome is missing → [POST /v1/calendar/{event_id}/outcome] then re-GET.
</example>

<example>
User: Give me my morning briefing.
Assistant: [GET /v1/intelligence/morning-brief]
Render the summary block first ("4 events, 2 need prep, 1 high alert"),
then narrate the high alerts and prep-needed events. Don't dump every
field — surface what's actionable.
</example>

<example>
User: Tell me about Bob.
Assistant: [GET /v1/intelligence/context/person?email=bob@example.com&days=90]
This single call returns interactions, topics, events, commitments,
health, and topology position. Compose a narrative summary.
</example>

<example>
User: Is CIG's intelligence layer current?
Assistant: [GET /v1/intelligence/derivations/status]
Report coverage % for each layer and latest-generation timestamps.
Recommend `POST /…/compute` or `POST /…/backfill` for any layer at 0%.
</example>

## Anti-Patterns — Don't Do This

- ❌ Don't call `/v1/emails/recent` then build relationship insights
  yourself. The 4th-order layer already did it — use it.
- ❌ Don't loop through `/v1/contacts/{email}/health` for everyone in a
  cluster. Use `/v1/intelligence/topology/clusters/{cluster_id}` which
  returns health inline.
- ❌ Don't call `topology/compute` on every request. It's heavy (GDS).
  The 24h scheduled loop is enough; only POST if user explicitly asks
  to refresh or `derivations/status` shows 0% coverage.
- ❌ Don't generate outcomes for every event. Auto-loop handles
  high/critical importance events every 2h. Only POST `/outcome` if
  the user asks about a specific recent meeting.
- ❌ Don't expose raw `cluster_id` integers to the user. Either name
  the cluster from its top members ("your Houston Methodist cluster")
  or describe membership.

## Combining Layers — Common Patterns

**"Who needs my attention this week?"**
1. `/v1/intelligence/alerts?severity=high` → high-priority alerts
2. `/v1/intelligence/topology/bridges` → cross-ref any bridges with declining health
3. `/v1/commitments?direction=outbound&status=open` → upcoming promises

**"Strategic relationship review of contact X"**
1. `/v1/intelligence/context/person?email=X&days=180` → narrative base
2. `/v1/contacts/{X}/sentiment-arc` → emotional trajectory
3. From the context payload's `cluster_id` →
   `/v1/intelligence/topology/clusters/{id}?limit=20` → who else is in their world

**"Did the network change this quarter?"**
1. `/v1/intelligence/derivations/status` → confirm topology was recomputed recently
2. `/v1/intelligence/topology/clusters?limit=10` → community sizes
3. `/v1/intelligence/topology/bridges` → who currently holds the network together

## Technical Details

- **Service**: `cig.service` (systemd) on `http://localhost:8780`
- **All endpoints require**: `X-API-Key: $CIG_API_KEY`
- **Timeouts**: 12s for reads, 30s for searches, 60s+ for `topology/compute`
- **Pagination**: `limit` query param (defaults: 10–50; max usually 200)
- **Idempotent POSTs**: alert generation, topology compute, outcome
  generation — safe to retry
- **Auto-loops** keep most layers fresh; manual POST is for on-demand
  refresh, not normal operation

## References

- CIG roadmap & full API surface:
  `/home/eleazar/Projects/AIHomelab/services/cig/docs/KNOWLEDGE_ROADMAP.md`
- CIG client (Nova-side helpers):
  `/home/eleazar/Projects/AIHomelab/services/nova-agent/services/nova-agent/nova/cig.py`
- Convergence module (alerts):
  `/home/eleazar/Projects/AIHomelab/services/cig/convergence.py`
- Cross-modal + morning brief + topology read:
  `/home/eleazar/Projects/AIHomelab/services/cig/intelligence.py`
- Diagnostics & restart procedure: see `cig-tool-troubleshooting` skill
