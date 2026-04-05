"""
Nova Agent ticket management handlers.

Provides CRUD operations against the ecosystem dashboard's /api/tickets
endpoint. Tickets are the structured way Nova reports issues discovered
during conversations so they can be triaged, analyzed by OpenClaw, and
fixed by Windsurf or OpenClaw with approval gating.

Ticket lifecycle:
  Nova creates (open) → triage → OpenClaw analyzes → proposed fix →
  approval engine → implement → resolved
"""

import aiohttp
import os
from typing import Any, Optional
from loguru import logger

ECOSYSTEM_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
ECOSYSTEM_API_KEY = os.environ.get("ECOSYSTEM_API_KEY", "ai-gateway-api-key-2024")

_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _headers() -> dict[str, str]:
    return {
        "X-API-Key": ECOSYSTEM_API_KEY,
        "Content-Type": "application/json",
    }


async def handle_create_ticket(
    title: str,
    description: str = "",
    priority: str = "medium",
    severity: str = "minor",
    category: str = "bug",
    component: str = "",
    tags: str = "",
    source_context: str = "",
    assigned_to: str = "",
) -> str:
    """Create a new ticket in the homelab ticket tracker."""
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
            "severity": severity,
            "category": category,
            "source_agent": "nova",
            "tags": tag_list,
        }
        if component:
            body["component"] = component
        if source_context:
            body["source_context"] = source_context
        if assigned_to:
            body["assigned_to"] = assigned_to

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ECOSYSTEM_URL}/api/tickets",
                headers=_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    ticket = data.get("ticket", {})
                    tid = ticket.get("id", "?")[:8]
                    logger.info(f"Ticket created: {tid} — {title}")
                    return (
                        f"Ticket created (ID: {tid}).\n"
                        f"Title: {title}\n"
                        f"Priority: {priority}, Category: {category}"
                        + (f", Component: {component}" if component else "")
                        + (f"\nAssigned to: {assigned_to}" if assigned_to else "")
                    )
                else:
                    text = await resp.text()
                    logger.warning(f"Ticket create failed: HTTP {resp.status} {text[:200]}")
                    return f"Failed to create ticket (HTTP {resp.status})."
    except Exception as e:
        logger.error(f"Ticket create error: {e}")
        return f"Error creating ticket: {e}"


async def handle_list_tickets(
    status: str = "",
    priority: str = "",
    assigned_to: str = "",
    limit: str = "10",
) -> str:
    """List tickets from the homelab ticket tracker."""
    try:
        params: dict[str, str] = {"per_page": limit}
        if status:
            params["status"] = status
        if priority:
            params["priority"] = priority
        if assigned_to:
            params["assigned_to"] = assigned_to

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ECOSYSTEM_URL}/api/tickets",
                headers=_headers(),
                params=params,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return f"Failed to list tickets (HTTP {resp.status})."
                data = await resp.json()

        tickets = data.get("tickets", [])
        total = data.get("total", 0)
        if not tickets:
            filter_desc = []
            if status:
                filter_desc.append(f"status={status}")
            if priority:
                filter_desc.append(f"priority={priority}")
            return f"No tickets found" + (f" ({', '.join(filter_desc)})" if filter_desc else "") + "."

        lines = [f"{total} ticket(s) found:"]
        for t in tickets:
            tid = t.get("id", "?")[:8]
            st = t.get("status", "?")
            pri = t.get("priority", "?")
            title = t.get("title", "Untitled")[:80]
            comp = t.get("component", "")
            assigned = t.get("assigned_to", "")
            line = f"- [{tid}] {st.upper()} ({pri}) {title}"
            if comp:
                line += f" [{comp}]"
            if assigned:
                line += f" → {assigned}"
            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Ticket list error: {e}")
        return f"Error listing tickets: {e}"


async def handle_get_ticket(ticket_id: str) -> str:
    """Get full details of a specific ticket."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ECOSYSTEM_URL}/api/tickets/{ticket_id}",
                headers=_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 404:
                    return f"Ticket {ticket_id[:8]} not found."
                if resp.status != 200:
                    return f"Failed to get ticket (HTTP {resp.status})."
                data = await resp.json()

        t = data.get("ticket", {})
        parts = [
            f"Ticket {t.get('id', '?')[:8]}",
            f"Title: {t.get('title', '?')}",
            f"Status: {t.get('status', '?')} | Priority: {t.get('priority', '?')} | Severity: {t.get('severity', '?')}",
            f"Category: {t.get('category', '?')} | Component: {t.get('component', '—')}",
            f"Source: {t.get('source_agent', '?')} | Assigned: {t.get('assigned_to', '—')}",
            f"Created: {t.get('created_at', '?')[:19]}",
        ]
        if t.get("description"):
            parts.append(f"\nDescription:\n{t['description'][:500]}")
        if t.get("source_context"):
            parts.append(f"\nContext:\n{t['source_context'][:300]}")
        if t.get("analysis"):
            parts.append(f"\nAnalysis ({t.get('analysis_agent', '?')}):\n{t['analysis'][:500]}")
        if t.get("proposed_fix"):
            parts.append(f"\nProposed Fix:\n{t['proposed_fix'][:500]}")
        if t.get("affected_files"):
            files = t["affected_files"]
            if files:
                parts.append(f"\nAffected Files: {', '.join(str(f) for f in files[:10])}")
        if t.get("resolution"):
            parts.append(f"\nResolution ({t.get('resolution_agent', '?')}):\n{t['resolution'][:500]}")

        return "\n".join(parts)
    except Exception as e:
        logger.error(f"Ticket get error: {e}")
        return f"Error getting ticket: {e}"


async def handle_update_ticket(
    ticket_id: str,
    status: str = "",
    priority: str = "",
    assigned_to: str = "",
    analysis: str = "",
    proposed_fix: str = "",
    resolution: str = "",
    resolution_agent: str = "",
    affected_files: str = "",
) -> str:
    """Update fields on an existing ticket."""
    try:
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if priority:
            body["priority"] = priority
        if assigned_to:
            body["assigned_to"] = assigned_to
        if analysis:
            body["analysis"] = analysis
            body["analysis_agent"] = "nova"
        if proposed_fix:
            body["proposed_fix"] = proposed_fix
        if resolution:
            body["resolution"] = resolution
            body["resolution_agent"] = resolution_agent or "nova"
        if affected_files:
            body["affected_files"] = [f.strip() for f in affected_files.split(",") if f.strip()]

        if not body:
            return "No fields provided to update."

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{ECOSYSTEM_URL}/api/tickets/{ticket_id}",
                headers=_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 404:
                    return f"Ticket {ticket_id[:8]} not found."
                if resp.status != 200:
                    text = await resp.text()
                    return f"Failed to update ticket (HTTP {resp.status}): {text[:200]}"
                data = await resp.json()

        t = data.get("ticket", {})
        updated_fields = ", ".join(body.keys())
        logger.info(f"Ticket {ticket_id[:8]} updated: {updated_fields}")
        return f"Ticket {t.get('id', '?')[:8]} updated ({updated_fields}). Status: {t.get('status', '?')}."
    except Exception as e:
        logger.error(f"Ticket update error: {e}")
        return f"Error updating ticket: {e}"


async def handle_delegate_ticket(
    ticket_id: str,
    delegate_to: str = "openclaw",
    task_description: str = "",
) -> str:
    """Delegate a ticket to OpenClaw or Windsurf for analysis/fix.

    For OpenClaw: updates the ticket status to 'analyzing' and sets
    assigned_to. The OpenClaw coding agent will pick it up, analyze the
    codebase, and write back its analysis + proposed fix. Any actual
    code changes require approval via the homelab approval engine.

    For Windsurf: marks as assigned so IDE-based work can begin.
    """
    try:
        body: dict[str, Any] = {
            "assigned_to": delegate_to,
            "delegated_to": delegate_to,
            "delegation_status": "pending",
        }
        if delegate_to == "openclaw":
            body["status"] = "analyzing"
        elif delegate_to == "windsurf":
            body["status"] = "in_progress"

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{ECOSYSTEM_URL}/api/tickets/{ticket_id}",
                headers=_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"Failed to delegate ticket (HTTP {resp.status}): {text[:200]}"
                data = await resp.json()

        t = data.get("ticket", {})
        tid = t.get("id", "?")[:8]
        title = t.get("title", "?")[:60]
        logger.info(f"Ticket {tid} delegated to {delegate_to}")

        if delegate_to == "openclaw":
            return (
                f"Ticket {tid} delegated to OpenClaw for analysis.\n"
                f"Title: {title}\n"
                "OpenClaw will analyze the codebase, identify root cause, "
                "and propose a fix. Any code changes will require approval."
            )
        elif delegate_to == "windsurf":
            return (
                f"Ticket {tid} assigned to Windsurf IDE.\n"
                f"Title: {title}\n"
                "This ticket is now queued for structural/architectural work in the IDE."
            )
        else:
            return f"Ticket {tid} delegated to {delegate_to}."
    except Exception as e:
        logger.error(f"Ticket delegate error: {e}")
        return f"Error delegating ticket: {e}"
