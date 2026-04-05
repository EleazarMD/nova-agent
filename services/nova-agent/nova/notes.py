"""
Nova Agent Notes Handler

Manages meeting notes, action items, and productivity documents
via the Dashboard Notes API.
"""

import os
import aiohttp
from typing import Optional, List, Dict, Any
from loguru import logger

ECOSYSTEM_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
ECOSYSTEM_USER_ID = os.environ.get("ECOSYSTEM_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")
INTERNAL_SERVICE_KEY = os.environ.get("INTERNAL_SERVICE_KEY", "")


async def handle_manage_notes(
    action: str,
    note_id: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    note_type: Optional[str] = None,
    tags: Optional[str] = None,
    action_items: Optional[str] = None,
    meeting_date: Optional[str] = None,
    attendees: Optional[str] = None,
    search: Optional[str] = None,
    action_item_id: Optional[str] = None,
    action_item_text: Optional[str] = None,
    action_item_completed: Optional[bool] = None,
    limit: Optional[str] = None,
) -> str:
    """
    Handle notes management operations.
    
    Actions:
    - create: Create a new note
    - list: List notes (with optional filters)
    - get: Get a specific note by ID
    - update: Update a note
    - search: Search notes by content
    - add_action: Add action item to a note
    - complete_action: Mark action item as complete
    - list_actions: List action items for a note
    """
    base = ECOSYSTEM_URL
    headers = {
        "Content-Type": "application/json",
        "X-User-Id": ECOSYSTEM_USER_ID,
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession() as session:
            # CREATE: Create a new note
            if action == "create":
                if not title:
                    return "Error: title is required to create a note."
                
                body: Dict[str, Any] = {
                    "title": title,
                    "content": content or "",
                    "note_type": note_type or "quick",
                }
                
                if tags:
                    body["tags"] = [t.strip() for t in tags.split(",")]
                if meeting_date:
                    body["meeting_date"] = meeting_date
                if attendees:
                    body["attendees"] = [a.strip() for a in attendees.split(",")]
                if action_items:
                    # Parse action items from comma-separated string
                    items = []
                    for item_text in action_items.split(";"):
                        item_text = item_text.strip()
                        if item_text:
                            items.append({
                                "text": item_text,
                                "completed": False,
                            })
                    body["action_items"] = items

                url = f"{base}/api/notes"
                async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        logger.error(f"Notes API create error: {resp.status} - {text}")
                        return f"Failed to create note (HTTP {resp.status})."
                    
                    data = await resp.json()
                    result = f"✅ Note created: \"{data.get('title', title)}\"\n"
                    result += f"ID: {data.get('id')}\n"
                    result += f"Type: {data.get('note_type', 'quick')}"
                    
                    items = data.get("action_items", [])
                    if items:
                        result += f"\nAction items: {len(items)}"
                    
                    return result

            # LIST: List notes with optional filters
            elif action == "list":
                params: Dict[str, str] = {}
                if note_type:
                    params["note_type"] = note_type
                if tags:
                    params["tag"] = tags.split(",")[0].strip()
                if limit:
                    params["limit"] = limit

                url = f"{base}/api/notes"
                async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        return f"Failed to list notes (HTTP {resp.status})."
                    
                    data = await resp.json()
                    notes = data.get("notes", [])
                    total = data.get("pagination", {}).get("total", len(notes))
                    
                    if not notes:
                        return "No notes found."
                    
                    lines = [f"📝 {total} notes found:"]
                    for n in notes[:10]:
                        title_str = n.get("title", "Untitled")[:50]
                        ntype = n.get("note_type", "quick")
                        items = n.get("action_items", [])
                        pending = len([i for i in items if not i.get("completed")])
                        
                        line = f"- [{ntype}] {title_str}"
                        if pending > 0:
                            line += f" ({pending} pending actions)"
                        line += f" (id: {n.get('id')[:8]}...)"
                        lines.append(line)
                    
                    return "\n".join(lines)

            # GET: Get a specific note
            elif action == "get":
                if not note_id:
                    return "Error: note_id is required for get action."
                
                url = f"{base}/api/notes/{note_id}"
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Note {note_id} not found."
                    if resp.status != 200:
                        return f"Failed to get note (HTTP {resp.status})."
                    
                    n = await resp.json()
                    lines = [
                        f"📋 {n.get('title', 'Untitled')}",
                        f"Type: {n.get('note_type', 'quick')}",
                    ]
                    
                    if n.get("meeting_date"):
                        lines.append(f"Meeting date: {n['meeting_date']}")
                    if n.get("attendees"):
                        lines.append(f"Attendees: {', '.join(n['attendees'])}")
                    if n.get("tags"):
                        lines.append(f"Tags: {', '.join(n['tags'])}")
                    
                    content = n.get("content", "")
                    if content:
                        # Truncate long content
                        if len(content) > 500:
                            content = content[:500] + "..."
                        lines.append(f"\n{content}")
                    
                    items = n.get("action_items", [])
                    if items:
                        lines.append(f"\nAction Items ({len(items)}):")
                        for item in items:
                            status = "✅" if item.get("completed") else "☐"
                            line = f"  {status} {item.get('text', '')}"
                            if item.get("assignee"):
                                line += f" (@{item['assignee']})"
                            if item.get("due_date"):
                                line += f" [due: {item['due_date']}]"
                            lines.append(line)
                    
                    return "\n".join(lines)

            # UPDATE: Update a note
            elif action == "update":
                if not note_id:
                    return "Error: note_id is required for update action."
                
                body = {}
                if title:
                    body["title"] = title
                if content:
                    body["content"] = content
                if note_type:
                    body["note_type"] = note_type
                if tags:
                    body["tags"] = [t.strip() for t in tags.split(",")]
                if meeting_date:
                    body["meeting_date"] = meeting_date
                if attendees:
                    body["attendees"] = [a.strip() for a in attendees.split(",")]
                
                if not body:
                    return "Error: No fields to update. Provide title, content, tags, etc."
                
                url = f"{base}/api/notes/{note_id}"
                async with session.patch(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Note {note_id} not found."
                    if resp.status != 200:
                        return f"Failed to update note (HTTP {resp.status})."
                    
                    data = await resp.json()
                    return f"✅ Note updated: \"{data.get('title', 'Untitled')}\""

            # SEARCH: Search notes
            elif action == "search":
                if not search:
                    return "Error: search query is required."
                
                params = {"search": search}
                if limit:
                    params["limit"] = limit
                
                url = f"{base}/api/notes"
                async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        return f"Search failed (HTTP {resp.status})."
                    
                    data = await resp.json()
                    notes = data.get("notes", [])
                    
                    if not notes:
                        return f"No notes found matching '{search}'."
                    
                    lines = [f"🔍 Found {len(notes)} notes matching '{search}':"]
                    for n in notes[:8]:
                        title_str = n.get("title", "Untitled")[:50]
                        ntype = n.get("note_type", "quick")
                        lines.append(f"- [{ntype}] {title_str} (id: {n.get('id')[:8]}...)")
                    
                    return "\n".join(lines)

            # ADD_ACTION: Add action item to a note
            elif action == "add_action":
                if not note_id:
                    return "Error: note_id is required."
                if not action_item_text:
                    return "Error: action_item_text is required."
                
                body = {"text": action_item_text}
                
                url = f"{base}/api/notes/{note_id}/action-items"
                async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Note {note_id} not found."
                    if resp.status not in (200, 201):
                        return f"Failed to add action item (HTTP {resp.status})."
                    
                    data = await resp.json()
                    return f"✅ Action item added: \"{action_item_text}\"\nTotal items: {data.get('total_items', 1)}"

            # COMPLETE_ACTION: Mark action item as complete
            elif action == "complete_action":
                if not note_id:
                    return "Error: note_id is required."
                if not action_item_id:
                    return "Error: action_item_id is required."
                
                completed = action_item_completed if action_item_completed is not None else True
                body = {
                    "action_item_id": action_item_id,
                    "completed": completed,
                }
                
                url = f"{base}/api/notes/{note_id}/action-items"
                async with session.patch(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Note or action item not found."
                    if resp.status != 200:
                        return f"Failed to update action item (HTTP {resp.status})."
                    
                    data = await resp.json()
                    status = "completed" if completed else "reopened"
                    return f"✅ Action item {status}. {data.get('completed_count', 0)}/{data.get('total_items', 0)} complete."

            # LIST_ACTIONS: List action items for a note
            elif action == "list_actions":
                if not note_id:
                    return "Error: note_id is required."
                
                url = f"{base}/api/notes/{note_id}/action-items"
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Note {note_id} not found."
                    if resp.status != 200:
                        return f"Failed to list action items (HTTP {resp.status})."
                    
                    data = await resp.json()
                    items = data.get("action_items", [])
                    summary = data.get("summary", {})
                    
                    if not items:
                        return "No action items in this note."
                    
                    lines = [f"Action Items ({summary.get('completed', 0)}/{summary.get('total', 0)} complete):"]
                    for item in items:
                        status = "✅" if item.get("completed") else "☐"
                        line = f"  {status} {item.get('text', '')}"
                        if item.get("assignee"):
                            line += f" (@{item['assignee']})"
                        if item.get("due_date"):
                            line += f" [due: {item['due_date']}]"
                        line += f" (id: {item.get('id', '')[:12]})"
                        lines.append(line)
                    
                    return "\n".join(lines)

            else:
                return f"Unknown action: {action}. Available: create, list, get, update, search, add_action, complete_action, list_actions"

    except aiohttp.ClientError as e:
        logger.error(f"Notes API connection error: {e}")
        return f"Could not connect to notes service: {str(e)}"
    except Exception as e:
        logger.error(f"Notes handler error: {e}")
        return f"Notes operation failed: {str(e)}"
