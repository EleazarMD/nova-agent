"""
CIG (Communication Intelligence Graph) — Client for Nova Agent.

Single entry point for email, calendar, and contact analytics.
Backed by the CIG service on port 8766 (Neo4j + Redis).

Data flow:
  User asks about email/calendar/contacts → query_cig() → CIG analytics API
  Hermes agent delegation → hub_delegate(agent='hermes', ...) → CIG via Hermes
  Direct Nova queries → handle_cig_query() → this client
"""

import os
import aiohttp
import json
from typing import Any, Optional
from loguru import logger

CIG_URL = os.environ.get("CIG_URL", "http://localhost:8766")
CIG_API_KEY = os.environ.get("CIG_API_KEY", "dev-cig-key-change-in-prod")

_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def query_email_analytics(
    user_id: str,
    mode: str = "summary",
    hours_back: int = 24,
    limit: int = 10,
) -> dict[str, Any]:
    """Query CIG email analytics.

    Returns urgency-scored emails, action items, and patterns.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/api/analytics/email",
                params={
                    "user_id": user_id,
                    "mode": mode,
                    "hours_back": hours_back,
                    "limit": limit,
                },
                headers={
                    "X-API-Key": CIG_API_KEY,
                    "X-User-Id": user_id,
                },
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
    """Query CIG calendar analytics.

    Returns upcoming events, conflict detection, meeting patterns.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/api/analytics/calendar",
                params={
                    "user_id": user_id,
                    "period": period,
                    "detail": detail,
                },
                headers={
                    "X-API-Key": CIG_API_KEY,
                    "X-User-Id": user_id,
                },
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
    """Query CIG contact/relationship analytics.

    Returns relationship health scores, outreach recommendations, interaction patterns.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CIG_URL}/api/analytics/contacts",
                params={
                    "user_id": user_id,
                    "action": action,
                    "filter": filter_type,
                    "limit": limit,
                },
                headers={
                    "X-API-Key": CIG_API_KEY,
                    "X-User-Id": user_id,
                },
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
    """Search CIG knowledge graph for entities and relationships.

    Returns matched entities (people, organizations, topics) with relationship context.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CIG_URL}/api/kg/search",
                headers={
                    "X-API-Key": CIG_API_KEY,
                    "X-User-Id": user_id,
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "entity_type": entity_type,
                    "limit": limit,
                },
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.warning(f"CIG KG search failed: {resp.status} {text[:100]}")
                    return {"error": f"CIG returned {resp.status}", "results": []}
    except Exception as e:
        logger.warning(f"CIG KG search error: {e}")
        return {"error": str(e), "results": []}


async def query_cig(
    user_id: str,
    domain: str = "email",
    query: str = "",
    **kwargs,
) -> str:
    """Unified CIG query interface for Nova tools.

    Dispatches to the appropriate CIG analytics endpoint based on domain.
    Returns a formatted string for the LLM.
    """
    domain = domain.lower().strip()

    if domain in ("email", "inbox", "emails"):
        data = await query_email_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Email analytics unavailable: {data['error']}"
        emails = data.get("emails", [])
        if not emails:
            return "No email analytics data available."
        lines = ["Email analytics:"]
        for e in emails[:10]:
            urgency = e.get("urgency_score", 0)
            subject = e.get("subject", "No subject")
            sender = e.get("sender", "Unknown")
            lines.append(f"  [{urgency:.1f}] {subject} — from {sender}")
        return "\n".join(lines)

    elif domain in ("calendar", "schedule", "meetings"):
        data = await query_calendar_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Calendar analytics unavailable: {data['error']}"
        events = data.get("events", [])
        if not events:
            return "No calendar analytics data available."
        lines = ["Calendar analytics:"]
        for ev in events[:10]:
            title = ev.get("title", "Untitled")
            time_str = ev.get("start_time", "")
            attendees = ev.get("attendees", [])
            lines.append(f"  {time_str} — {title}" + (f" ({len(attendees)} attendees)" if attendees else ""))
        return "\n".join(lines)

    elif domain in ("contacts", "relationships", "people"):
        data = await query_contact_analytics(user_id, **kwargs)
        if "error" in data:
            return f"Contact analytics unavailable: {data['error']}"
        contacts = data.get("contacts", [])
        if not contacts:
            return "No contact analytics data available."
        lines = ["Contact analytics:"]
        for c in contacts[:10]:
            name = c.get("name", "Unknown")
            health = c.get("health_score", 0)
            last = c.get("last_contact", "Never")
            lines.append(f"  {name} — health: {health}, last contact: {last}")
        return "\n".join(lines)

    elif domain in ("search", "kg", "knowledge"):
        if not query:
            return "CIG knowledge search requires a query string."
        data = await search_cig_kg(user_id, query, **kwargs)
        if "error" in data:
            return f"CIG knowledge search unavailable: {data['error']}"
        results = data.get("results", [])
        if not results:
            return f"No CIG knowledge graph results for '{query}'."
        lines = [f"CIG knowledge graph results for '{query}':"]
        for r in results[:10]:
            name = r.get("name", "Unknown")
            etype = r.get("type", "entity")
            context = r.get("context", "")[:100]
            lines.append(f"  {name} ({etype}) — {context}")
        return "\n".join(lines)

    else:
        return f"Unknown CIG domain '{domain}'. Use: email, calendar, contacts, search."
