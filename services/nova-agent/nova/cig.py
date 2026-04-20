"""
CIG (Communication Intelligence Graph) — Client for Nova Agent.

Single entry point for email, calendar, and contact analytics.
Backed by the CIG v2 service on port 8780 (Neo4j + Redis + Google Workspace).

Data flow:
  User asks about email/calendar/contacts → query_cig() → CIG v2 API
  Nova context briefing → get_nova_context() → /v1/nova/context
  Direct Nova queries → handle_cig_query() → this client
"""

import os
import aiohttp
import json
from typing import Any, Optional
from loguru import logger

CIG_URL = os.environ.get("CIG_URL", "http://localhost:8780")
CIG_API_KEY = os.environ.get("CIG_API_KEY", "nova-agent-key-2024")

_TIMEOUT = aiohttp.ClientTimeout(total=12)

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
                timeout=_TIMEOUT,
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
    except Exception as e:
        logger.warning(f"CIG search error: {e}")
        return {"error": str(e), "results": []}


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
        data = await query_email_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Email analytics unavailable: {data['error']}"
        emails = data.get("emails", [])
        if not emails:
            return "No recent emails found."
        lines = ["Recent emails:"]
        for e in emails[:10]:
            subject = e.get("subject", e.get("snippet", "No subject")[:60])
            sender = e.get("from", e.get("sender", "Unknown"))
            date = e.get("date", e.get("received_at", ""))
            lines.append(f"  - {subject} — from {sender} ({date})")
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

    else:
        return f"Unknown CIG domain '{domain}'. Use: email, calendar, contacts, search."
