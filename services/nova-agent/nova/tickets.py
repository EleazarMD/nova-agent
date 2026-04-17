"""
Nova Agent ticket management handlers.

Direct PostgreSQL CRUD — no Dashboard dependency.
Tickets are the structured way Nova reports issues discovered
during conversations so they can be triaged, analyzed by OpenClaw, and
fixed by Windsurf or OpenClaw with approval gating.

Ticket lifecycle:
  Nova creates (open) → triage → OpenClaw analyzes → proposed fix →
  approval engine → implement → resolved
"""

import json
import os
from typing import Any, Optional
from loguru import logger

try:
    import asyncpg
    _POOL: Optional[asyncpg.Pool] = None
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://eleazar@localhost/ecosystem_unified")
except ImportError:
    asyncpg = None  # type: ignore
    _POOL = None
    DATABASE_URL = ""    # type: ignore


async def _get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    return _POOL


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
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO tickets
                   (title, description, priority, severity, category, component,
                    tags, source_agent, source_context, assigned_to, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'nova', $8, $9, 'open')
                   RETURNING id""",
                title, description, priority, severity, category,
                component or None, json.dumps(tag_list),
                source_context or None, assigned_to or None,
            )
            tid = str(row["id"])[:8]
            logger.info(f"Ticket created: {tid} — {title}")
            return (
                f"Ticket created (ID: {tid}).\n"
                f"Title: {title}\n"
                f"Priority: {priority}, Category: {category}"
                + (f", Component: {component}" if component else "")
                + (f"\nAssigned to: {assigned_to}" if assigned_to else "")
            )
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
        pool = await _get_pool()
        conditions = []
        params = []
        idx = 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if priority:
            conditions.append(f"priority = ${idx}")
            params.append(priority)
            idx += 1
        if assigned_to:
            conditions.append(f"assigned_to = ${idx}")
            params.append(assigned_to)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(int(limit))

        rows = await pool.fetch(
            f"""SELECT id, title, status, priority, category, component, assigned_to, created_at
                FROM tickets {where}
                ORDER BY created_at DESC LIMIT ${idx}""",
            *params,
        )
        if not rows:
            filter_desc = []
            if status: filter_desc.append(f"status={status}")
            if priority: filter_desc.append(f"priority={priority}")
            return f"No tickets found" + (f" ({', '.join(filter_desc)})" if filter_desc else "") + "."

        lines = [f"{len(rows)} ticket(s) found:"]
        for r in rows:
            tid = str(r["id"])[:8]
            st = r["status"] or "?"
            pri = r["priority"] or "?"
            title = (r["title"] or "Untitled")[:80]
            comp = r["component"] or ""
            assigned = r["assigned_to"] or ""
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
        pool = await _get_pool()
        row = await pool.fetchrow(
            """SELECT * FROM tickets WHERE id = $1::uuid""",
            ticket_id,
        )
        if not row:
            return f"Ticket {ticket_id[:8]} not found."

        t = dict(row)
        parts = [
            f"Ticket {str(t.get('id', '?'))[:8]}",
            f"Title: {t.get('title', '?')}",
            f"Status: {t.get('status', '?')} | Priority: {t.get('priority', '?')} | Severity: {t.get('severity', '?')}",
            f"Category: {t.get('category', '?')} | Component: {t.get('component', '—')}",
            f"Source: {t.get('source_agent', '?')} | Assigned: {t.get('assigned_to', '—')}",
            f"Created: {str(t.get('created_at', '?'))[:19]}",
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
                if isinstance(files, str):
                    files = json.loads(files)
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
        sets = []
        params = []
        idx = 1

        if status:
            sets.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if priority:
            sets.append(f"priority = ${idx}")
            params.append(priority)
            idx += 1
        if assigned_to:
            sets.append(f"assigned_to = ${idx}")
            params.append(assigned_to)
            idx += 1
        if analysis:
            sets.append(f"analysis = ${idx}")
            params.append(analysis)
            idx += 1
            sets.append(f"analysis_agent = ${idx}")
            params.append("nova")
            idx += 1
            sets.append("analysis_at = now()")
        if proposed_fix:
            sets.append(f"proposed_fix = ${idx}")
            params.append(proposed_fix)
            idx += 1
        if resolution:
            sets.append(f"resolution = ${idx}")
            params.append(resolution)
            idx += 1
            sets.append(f"resolution_agent = ${idx}")
            params.append(resolution_agent or "nova")
            idx += 1
        if affected_files:
            file_list = [f.strip() for f in affected_files.split(",") if f.strip()]
            sets.append(f"affected_files = ${idx}::jsonb")
            params.append(json.dumps(file_list))
            idx += 1

        if not sets:
            return "No fields provided to update."

        # If status is resolved/closed, set closed_at
        if status in ("resolved", "closed"):
            sets.append("closed_at = now()")

        params.append(ticket_id)
        row = await (await _get_pool()).fetchrow(
            f"""UPDATE tickets SET {', '.join(sets)} WHERE id = ${idx}::uuid
                RETURNING id, status""",
            *params,
        )
        if not row:
            return f"Ticket {ticket_id[:8]} not found."

        updated_fields = ", ".join(s.split("=")[0].strip() for s in sets)
        logger.info(f"Ticket {ticket_id[:8]} updated: {updated_fields}")
        return f"Ticket {str(row['id'])[:8]} updated ({updated_fields}). Status: {row['status']}."
    except Exception as e:
        logger.error(f"Ticket update error: {e}")
        return f"Error updating ticket: {e}"


async def handle_delegate_ticket(
    ticket_id: str,
    delegate_to: str = "openclaw",
    task_description: str = "",
) -> str:
    """Delegate a ticket to OpenClaw or Windsurf for analysis/fix."""
    try:
        new_status = "analyzing" if delegate_to == "openclaw" else "in_progress"
        pool = await _get_pool()
        row = await pool.fetchrow(
            """UPDATE tickets
               SET assigned_to = $1, delegated_to = $1,
                   delegation_status = 'pending', status = $2
               WHERE id = $3::uuid
               RETURNING id, title""",
            delegate_to, new_status, ticket_id,
        )
        if not row:
            return f"Ticket {ticket_id[:8]} not found."

        tid = str(row["id"])[:8]
        title = (row["title"] or "?")[:60]
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
