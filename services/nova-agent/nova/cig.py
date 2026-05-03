"""
CIG (Communication Intelligence Graph) — Client for Nova Agent.

Single entry point for email, calendar, and contact analytics.
Backed by the CIG v2 service on port 8780 (Neo4j + Redis + Google Workspace).

Data flow:
  User asks about email/calendar/contacts → query_cig() → CIG v2 API
  Nova context briefing → get_nova_context() → /v1/nova/context
  Direct Nova queries → handle_cig_query() → this client
"""

import asyncio
import os
import aiohttp
import json
from typing import Any, Optional
from loguru import logger

CIG_URL = os.environ.get("CIG_URL", "http://localhost:8780")
CIG_API_KEY = os.environ.get("CIG_API_KEY", "nova-agent-key-2024")

# Fast read endpoints (cached Neo4j queries). 12s is plenty.
_TIMEOUT = aiohttp.ClientTimeout(total=12)
# Semantic search hits the AI Gateway to embed the query and then
# Chroma for the ANN lookup; cold-path can spike past 12s, and the
# aiohttp TimeoutError stringifies to an empty message which made
# previous failures look silently like "no results". 30s is well
# inside Nova's tool-call budget.
_SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=30)

_HEADERS = {
    "X-API-Key": CIG_API_KEY,
    "Content-Type": "application/json",
}


async def query_email_analytics(
    user_id: str,
    mode: str = "summary",
    hours_back: int = 24,
    limit: int = 10,
) -> dict[str, Any]:
    """Query CIG v2 email analytics.

    Returns urgency-scored emails, action items, and patterns.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/emails/recent",
                params={"limit": limit},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning(f"CIG email analytics failed: {resp.status} {text[:100]}")
                    return {"error": f"CIG returned {resp.status}", "emails": []}
    except Exception as e:
        logger.warning(f"CIG email analytics error: {e}")
        return {"error": str(e), "emails": []}


async def query_calendar_analytics(
    user_id: str,
    period: str = "today",
    detail: str = "summary",
) -> dict[str, Any]:
    """Query CIG v2 calendar analytics.

    Returns upcoming events, conflict detection, meeting patterns.
    """
    try:
        hours = {"today": 12, "week": 168, "tomorrow": 36}.get(period, 24)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/calendar/upcoming",
                params={"hours": hours},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning(f"CIG calendar analytics failed: {resp.status} {text[:100]}")
                    return {"error": f"CIG returned {resp.status}", "events": []}
    except Exception as e:
        logger.warning(f"CIG calendar analytics error: {e}")
        return {"error": str(e), "events": []}


async def query_contact_analytics(
    user_id: str,
    action: str = "outreach",
    filter_type: str = "overdue",
    limit: int = 5,
) -> dict[str, Any]:
    """Query CIG v2 contact/relationship analytics.

    Returns relationship health scores, outreach recommendations, interaction patterns.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/contacts/relationship-scores",
                params={"limit": limit},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning(f"CIG contact analytics failed: {resp.status} {text[:100]}")
                    return {"error": f"CIG returned {resp.status}", "contacts": []}
    except Exception as e:
        logger.warning(f"CIG contact analytics error: {e}")
        return {"error": str(e), "contacts": []}


async def search_cig_kg(
    user_id: str,
    query: str,
    entity_type: str = "any",
    limit: int = 5,
) -> dict[str, Any]:
    """Search CIG for emails/entities matching a natural-language query.

    Uses `/v1/search/emails` (semantic email search backed by vector embeddings)
    which is the correct NL-search surface. The older `/v1/kg/query` endpoint
    requires admin auth + raw Cypher and is not appropriate for LLM tools.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CIG_URL}/v1/search/emails",
                headers=_HEADERS,
                json={"query": query, "limit": limit},
                timeout=_SEARCH_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Normalize: expose as "results" for the caller.
                    if "results" not in data:
                        data["results"] = data.get("emails", [])
                    return data
                else:
                    text = await resp.text()
                    logger.warning(f"CIG search failed: {resp.status} {text[:100]}")
                    return {"error": f"CIG returned {resp.status}", "results": []}
    except asyncio.TimeoutError:
        # asyncio.TimeoutError stringifies to '' which previously
        # masked real timeouts as "empty response". Report it.
        logger.warning("CIG search timed out (>30s)")
        return {"error": "search timed out (CIG/AI-Gateway slow path)",
                "results": []}
    except Exception as e:
        logger.warning(f"CIG search error: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}", "results": []}


async def get_latest_briefing(briefing_type: Optional[str] = None) -> dict[str, Any]:
    """Fetch the most recent persisted briefing from CIG.

    Briefings are synthesized by Hermes (pi-agent running MiniMax M2.7) and
    POSTed to `/v1/briefings`. They live as :Briefing nodes in Neo4j and
    can be filtered by type (e.g. `morning`, `evening`, `heartbeat`,
    `meeting-prep`, `urgency-scan`). Returns `{"briefing": None}` when no
    briefing of that type exists yet.
    """
    params = {"briefing_type": briefing_type} if briefing_type else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/briefings/latest",
                params=params,
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.warning(f"CIG latest briefing failed: {resp.status} {text[:100]}")
                return {"error": f"CIG returned {resp.status}", "briefing": None}
    except Exception as e:
        logger.warning(f"CIG latest briefing error: {e}")
        return {"error": str(e), "briefing": None}


async def get_nova_context() -> dict[str, Any]:
    """Get Nova-specific briefing context from CIG.

    Returns recent emails, upcoming meetings, VIP contacts, and action items
    in a single call — optimized for Nova's context window.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/nova/context",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning(f"CIG nova context failed: {resp.status} {text[:100]}")
                    return {}
    except Exception as e:
        logger.warning(f"CIG nova context error: {e}")
        return {}


def _format_email_line(e: dict[str, Any]) -> str:
    """Render one email row as a single human-readable line.

    Robust to all the shapes CIG /v1/emails/recent and
    /v1/search/emails return: sender may be a {email,name} dict,
    or split across from_email/from_name, or a plain string. Date
    falls back through several keys."""
    subject = (e.get("subject") or e.get("snippet") or "(no subject)")[:120]

    # Sender resolution — prefer name+email, fall back to whichever exists.
    name = e.get("from_name") or e.get("sender_name")
    addr = e.get("from_email") or e.get("sender_email")
    if not (name or addr):
        s = e.get("sender") or e.get("from")
        if isinstance(s, dict):
            name = name or s.get("name")
            addr = addr or s.get("email")
        elif isinstance(s, str):
            addr = addr or s
    if name and addr:
        sender = f"{name} <{addr}>"
    else:
        sender = name or addr or "Unknown"

    date = (e.get("date") or e.get("sent_date") or e.get("received_at")
            or e.get("timestamp") or "")
    return f"{subject} — from {sender} ({date})"


async def query_thread(item_id: str) -> dict[str, Any]:
    """Fetch a full email thread by thread_id or message_id.

    Tries GET /v1/threads/{item_id} first (canonical thread lookup).
    If that 404s, tries GET /v1/emails/{item_id}/thread (message → thread).
    Returns the thread dict or an error dict.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 1) Direct thread lookup
            async with session.get(
                f"{CIG_URL}/v1/threads/{item_id}",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
            # 2) Email → thread lookup
            async with session.get(
                f"{CIG_URL}/v1/emails/{item_id}/thread",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"Thread not found for '{item_id}' (HTTP {resp.status})"}
    except Exception as e:
        logger.warning(f"CIG thread lookup error: {e}")
        return {"error": str(e)}


async def query_person(email_addr: str) -> dict[str, Any]:
    """Look up a contact/person by email address via GET /v1/contacts/{email}."""
    try:
        import urllib.parse
        encoded = urllib.parse.quote(email_addr, safe="")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/contacts/{encoded}",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                return {"error": f"Contact not found (HTTP {resp.status}): {text[:100]}"}
    except Exception as e:
        logger.warning(f"CIG person lookup error: {e}")
        return {"error": str(e)}


async def query_graph_stats() -> dict[str, Any]:
    """Fetch live graph counts via GET /v1/graph/stats."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/graph/stats",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"CIG graph/stats returned HTTP {resp.status}"}
    except Exception as e:
        logger.warning(f"CIG graph stats error: {e}")
        return {"error": str(e)}


async def query_graph_network(center_email: str, limit: int = 30) -> dict[str, Any]:
    """Fetch the contact communication network via GET /v1/graph/contacts."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/graph/contacts",
                params={"center_email": center_email, "limit": limit},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"CIG graph/contacts returned HTTP {resp.status}"}
    except Exception as e:
        logger.warning(f"CIG contact network error: {e}")
        return {"error": str(e)}


async def query_action_items(days: int = 30) -> dict[str, Any]:
    """Fetch inbox action items via GET /v1/inbox/actions."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/v1/inbox/actions",
                params={"days": days},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                return {"error": f"CIG inbox/actions returned HTTP {resp.status}: {text[:100]}"}
    except Exception as e:
        logger.warning(f"CIG action items error: {e}")
        return {"error": str(e)}


async def query_cig(
    user_id: str,
    domain: str = "email",
    query: str = "",
    item_id: str = "",
    **kwargs,
) -> str:
    """Unified CIG query interface for Nova tools.

    Dispatches to the appropriate CIG v2 endpoint based on domain.
    Returns a formatted string for the LLM.
    """
    domain = domain.lower().strip()

    # ------------------------------------------------------------------
    # Thread navigation — fetch a full conversation chain
    # ------------------------------------------------------------------
    if domain == "thread":
        if not item_id:
            return "Provide item_id=<thread_id or email_message_id> to fetch a thread."
        data = await query_thread(item_id)
        if "error" in data:
            return f"Thread lookup failed: {data['error']}"
        messages = data.get("messages") or data.get("emails") or []
        if not messages:
            return f"Thread '{item_id}' found but has no messages."
        thread_id = data.get("thread_id") or data.get("id") or item_id
        subject = data.get("subject") or (messages[0].get("subject") if messages else "")
        participants = data.get("participants") or []
        lines = [
            f"Thread: {subject}",
            f"ID: {thread_id}",
            f"Messages: {len(messages)}",
        ]
        if participants:
            lines.append(f"Participants: {', '.join(str(p) for p in participants[:8])}")
        lines.append("")
        for i, m in enumerate(messages, 1):
            subj = m.get("subject", "")
            sender = m.get("sender_email") or m.get("from_email") or m.get("sender", {})
            if isinstance(sender, dict):
                sender = sender.get("email") or sender.get("name") or "?"
            date = m.get("sent_date") or m.get("date") or ""
            snippet = (m.get("body_preview") or m.get("snippet") or "")[:120]
            mid = m.get("message_id") or m.get("id") or ""
            reply_to = m.get("replies_to") or m.get("parent_id") or ""
            line = f"[{i}] {sender} ({str(date)[:16]})"
            if subj:
                line += f" — {subj}"
            if reply_to:
                line += f" ↩ reply to {str(reply_to)[:40]}"
            lines.append(line)
            if snippet:
                lines.append(f"    {snippet}")
            if mid:
                lines.append(f"    id: {mid}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Person / contact lookup
    # ------------------------------------------------------------------
    if domain == "person":
        if not item_id:
            return "Provide item_id=<email_address> to look up a contact."
        data = await query_person(item_id)
        if "error" in data:
            return f"Contact lookup failed: {data['error']}"
        # Normalize — CIG returns the full person node dict
        p = data.get("person") or data
        name = p.get("name") or p.get("display_name") or item_id
        org = p.get("organization") or p.get("org") or ""
        vip = "✅ VIP" if p.get("is_vip") else ""
        importance = p.get("ai_importance") or p.get("importance") or ""
        health = p.get("relationship_health") or p.get("health_score") or ""
        last = p.get("last_contact") or p.get("last_interaction") or ""
        total = p.get("total_interactions") or p.get("email_count") or ""
        topics = p.get("ai_topics") or p.get("topics") or []
        summary = p.get("ai_summary") or p.get("summary") or ""
        lines = [f"Contact: {name} <{item_id}>"]
        if org:
            lines.append(f"Organization: {org}")
        if vip:
            lines.append(vip)
        if importance:
            lines.append(f"Importance: {importance}")
        if health:
            lines.append(f"Relationship health: {health}")
        if last:
            lines.append(f"Last contact: {str(last)[:16]}")
        if total:
            lines.append(f"Total interactions: {total}")
        if topics:
            lines.append(f"Topics: {', '.join(str(t) for t in topics[:8])}")
        if summary:
            lines.append(f"AI summary: {summary[:300]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Graph — live stats or contact network
    # ------------------------------------------------------------------
    if domain == "graph":
        sub = (query or "stats").lower().strip()
        if sub in ("network", "map", "neighbors", "connections"):
            center = item_id or "eflores2@houstonmethodist.org"
            data = await query_graph_network(center)
            if "error" in data:
                return f"Contact network unavailable: {data['error']}"
            nodes = data.get("nodes", [])
            edges = data.get("edges", [])
            stats = data.get("stats", {})
            lines = [
                f"Contact network centered on {center}:",
                f"  {stats.get('total_contacts', len(nodes)-1)} contacts, "
                f"{stats.get('key_contacts', 0)} key contacts, "
                f"{len(edges)} communication edges",
                "",
            ]
            for n in nodes[1:21]:  # skip center, show top 20
                label = n.get("label") or n.get("id", "?")
                ntype = "★" if n.get("type") == "key_contact" else " "
                interactions = n.get("interactions", 0)
                org = n.get("group") or ""
                line = f"  {ntype} {label}"
                if org and org != "unknown":
                    line += f" ({org})"
                if interactions:
                    line += f" — {interactions} interactions"
                lines.append(line)
            return "\n".join(lines)
        else:
            # Default: live stats
            data = await query_graph_stats()
            if "error" in data:
                return f"Graph stats unavailable: {data['error']}"
            return (
                f"CIG Knowledge Graph — live counts:\n"
                f"  Nodes: {data.get('nodes', 0):,} total\n"
                f"    • Persons:  {data.get('persons', 0):,}\n"
                f"    • Emails:   {data.get('emails', 0):,}\n"
                f"    • Threads:  {data.get('threads', 0):,}\n"
                f"    • Events:   {data.get('events', 0):,}\n"
                f"  Relationships: {data.get('relationships', 0):,} total\n"
                f"    • COMMUNICATES_WITH: {data.get('communicates_with', 0):,}\n"
                f"    • REPLIES_TO:        {data.get('replies_to', 0):,}\n"
                f"    • PART_OF (threads): {data.get('part_of', 0):,}"
            )

    # ------------------------------------------------------------------
    # Action items — inbox tasks, commitments, deadlines
    # ------------------------------------------------------------------
    if domain in ("actions", "action_items", "tasks"):
        data = await query_action_items(days=kwargs.get("days", 30))
        if "error" in data:
            return f"Inbox actions unavailable: {data['error']}"
        items = data.get("action_items") or data.get("actions") or data.get("items") or []
        if not items:
            return "No inbox action items found in the past 30 days."
        lines = [f"Inbox action items ({len(items)}):"]
        for it in items[:15]:
            subject = (it.get("subject") or it.get("email_subject") or "")[:80]
            sender = it.get("sender") or it.get("from_email") or ""
            atype = it.get("action_type") or it.get("type") or it.get("category") or "task"
            deadline = it.get("deadline") or it.get("due_date") or ""
            urgency = it.get("urgency") or it.get("priority") or ""
            line = f"  [{atype}] {subject}"
            if sender:
                line += f" — from {sender}"
            if deadline:
                line += f" (due: {str(deadline)[:16]})"
            if urgency:
                line += f" [{urgency}]"
            lines.append(line)
        return "\n".join(lines)

    if domain in ("email", "inbox", "emails"):
        # If the caller provided a search query, route to the
        # semantic email search endpoint rather than dumping the
        # 10 most-recent inbox items (which would ignore the query
        # and confuse the LLM into thinking nothing matched).
        q = (query or "").strip()
        if q:
            data = await search_cig_kg(user_id, q, limit=kwargs.get("limit", 10))
            if "error" in data:
                return f"Email search unavailable: {data['error']}"
            emails = data.get("results") or data.get("emails") or []
            if not emails:
                return f"No emails found matching '{q}'."
            lines = [f"Email search results for '{q}':"]
            for e in emails[:10]:
                lines.append("  - " + _format_email_line(e))
            return "\n".join(lines)

        data = await query_email_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Email analytics unavailable: {data['error']}"
        emails = data.get("emails") or data.get("recent_emails") or []
        if not emails:
            return "No recent emails found."
        lines = ["Recent emails:"]
        for e in emails[:10]:
            lines.append("  - " + _format_email_line(e))
        return "\n".join(lines)

    elif domain in ("calendar", "schedule", "meetings"):
        data = await query_calendar_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Calendar analytics unavailable: {data['error']}"
        events = data.get("events", [])
        if not events:
            return "No upcoming calendar events."
        lines = ["Upcoming events:"]
        for ev in events[:10]:
            title = ev.get("title", ev.get("summary", "Untitled"))
            start = ev.get("start", ev.get("start_time", ""))
            attendees = ev.get("attendees", [])
            lines.append(f"  - {start} — {title}" + (f" ({len(attendees)} attendees)" if attendees else ""))
        return "\n".join(lines)

    elif domain in ("contacts", "relationships", "people"):
        sub = (query or "").lower().strip()
        if sub == "vips":
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{CIG_URL}/v1/contacts/vips", headers=_HEADERS, timeout=_TIMEOUT) as resp:
                        data = await resp.json() if resp.status == 200 else {"error": f"HTTP {resp.status}"}
            except Exception as e:
                data = {"error": str(e)}
            if "error" in data:
                return f"VIP contacts unavailable: {data['error']}"
            vips = data.get("vips", [])
            if not vips:
                return "No VIP contacts found."
            lines = [f"VIP contacts ({len(vips)}):"]
            for c in vips[:20]:
                name = c.get("name") or c.get("email", "?")
                email = c.get("email", "")
                org = c.get("organization") or ""
                last = c.get("last_contact") or ""
                line = f"  ★ {name}"
                if email and email != name:
                    line += f" <{email}>"
                if org:
                    line += f" ({org})"
                if last:
                    line += f" — last: {str(last)[:16]}"
                lines.append(line)
            return "\n".join(lines)
        elif sub == "cooling":
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{CIG_URL}/v1/contacts/cooling", headers=_HEADERS, timeout=_TIMEOUT) as resp:
                        data = await resp.json() if resp.status == 200 else {"error": f"HTTP {resp.status}"}
            except Exception as e:
                data = {"error": str(e)}
            if "error" in data:
                return f"Cooling contacts unavailable: {data['error']}"
            cooling = data.get("contacts", [])
            days_quiet = data.get("days_quiet", 30)
            if not cooling:
                return f"No cooling contacts (quiet for {days_quiet}+ days)."
            lines = [f"Cooling contacts (no activity in {days_quiet}+ days) — {len(cooling)} found:"]
            for c in cooling[:15]:
                name = c.get("name") or c.get("email", "?")
                email = c.get("email", "")
                last = c.get("last_contact") or ""
                vip = "★ " if c.get("is_vip") else ""
                line = f"  {vip}{name}"
                if email and email != name:
                    line += f" <{email}>"
                if last:
                    line += f" — last contact: {str(last)[:16]}"
                lines.append(line)
            return "\n".join(lines)
        elif sub:
            try:
                import urllib.parse
                safe_query = urllib.parse.quote(sub)
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{CIG_URL}/v1/contacts/search?q={safe_query}", headers=_HEADERS, timeout=_TIMEOUT) as resp:
                        data = await resp.json() if resp.status == 200 else {"error": f"HTTP {resp.status}"}
            except Exception as e:
                data = {"error": str(e)}
            if "error" in data:
                return f"Contact search unavailable: {data['error']}"
            contacts = data.get("contacts", [])
            if not contacts:
                return f"No contacts found matching '{sub}'."
            lines = [f"Contact search results for '{sub}':"]
            for c in contacts[:10]:
                name = c.get("name") or c.get("email", "?")
                email = c.get("email", "")
                org = c.get("organization") or ""
                last = c.get("last_contact") or "Never"
                score = c.get("health_score", c.get("score", 0))
                line = f"  - {name}"
                if email and email != name:
                    line += f" <{email}>"
                if org:
                    line += f" ({org})"
                line += f" — score: {score}, last contact: {str(last)[:16]}"
                lines.append(line)
            return "\n".join(lines)
        else:
            # Default: relationship health scores
            data = await query_contact_analytics(user_id, **kwargs)
            if "error" in data:
                return f"Contact analytics unavailable: {data['error']}"
            contacts = data.get("contacts", [])
            if not contacts:
                return "No contact data available."
            lines = ["Contact relationship scores:"]
            for c in contacts[:10]:
                name = c.get("name", c.get("email", "Unknown"))
                score = c.get("score", c.get("health_score", 0))
                last = c.get("last_contact", c.get("last_interaction", "Never"))
                lines.append(f"  - {name} — score: {score}, last contact: {last}")
            return "\n".join(lines)

    elif domain in ("search", "kg", "knowledge"):
        if not query:
            return "CIG knowledge search requires a query string."
        data = await search_cig_kg(user_id, query, **kwargs)
        if "error" in data:
            return f"CIG knowledge search unavailable: {data['error']}"
        results = data.get("results", data.get("entities", []))
        if not results:
            return f"No CIG knowledge graph results for '{query}'."
        lines = [f"CIG knowledge graph results for '{query}':"]
        for r in results[:10]:
            name = r.get("name", "Unknown")
            etype = r.get("type", r.get("entity_type", "entity"))
            context = r.get("context", r.get("description", ""))[:100]
            lines.append(f"  - {name} ({etype}) — {context}")
        return "\n".join(lines)

    elif domain in ("briefing", "briefings"):
        # `query` doubles as the optional briefing_type filter
        # (morning | evening | heartbeat | meeting-prep | urgency-scan).
        briefing_type = (query or "").strip() or None
        data = await get_latest_briefing(briefing_type)
        if "error" in data:
            return f"Briefing unavailable: {data['error']}"
        b = data.get("briefing")
        if not b:
            filt = f" of type '{briefing_type}'" if briefing_type else ""
            return f"No persisted briefing{filt} found yet."
        header = (
            f"Latest {b.get('briefing_type', 'briefing')} briefing "
            f"(by {b.get('source_agent', 'hermes')}, "
            f"generated {b.get('generated_at', '?')}):"
        )
        return header + "\n\n" + (b.get("content") or "").strip()

    else:
        return (
            f"Unknown CIG domain '{domain}'. "
            "Use: email, calendar, contacts, search, briefing."
        )
