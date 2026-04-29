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


async def query_cig(
    user_id: str,
    domain: str = "email",
    query: str = "",
    **kwargs,
) -> str:
    """Unified CIG query interface for Nova tools.

    Dispatches to the appropriate CIG v2 endpoint based on domain.
    Returns a formatted string for the LLM.
    """
    domain = domain.lower().strip()

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
