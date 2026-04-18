"""
Nova Agent Notes Handler (Pi Workspace Integration)

Manages notes as Pi Workspace pages with blocks.
Uses the Pi Workspace API (port 8762).
"""

import os
from typing import Optional, List, Dict, Any
from loguru import logger

PI_WORKSPACE_URL = os.environ.get("PI_WORKSPACE_URL", "http://localhost:8762")


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
    **kwargs,
) -> str:
    """
    Handle notes management via Pi Workspace pages.
    
    Actions:
    - create: Create a new note as a workspace page
    - list: List notes (pages)
    - get: Get a specific note
    - update: Update a note
    - search: Search notes via workspace search
    - add_action: Add to-do item to a note (as block)
    - complete_action: Mark to-do as complete
    - list_actions: List to-do blocks in a page
    
    DEPRECATED: This handler is maintained for backward compatibility.
    New code should use manage_workspace with create_page instead.
    """
    from nova.pi_workspace import (
        create_page, list_pages, get_page, get_page_blocks, create_block,
        search_workspace, _plain_title
    )

    try:
        icon_map = {"meeting": "📅", "quick": "📝", "project": "📁", "reference": "📚", "journal": "📔"}

        # CREATE: Create a new note as workspace page
        if action == "create":
            if not title:
                return "Error: title is required to create a note."
            
            icon = icon_map.get(note_type or "quick", "📝")
            page = await create_page(title, icon=icon)
            if not page:
                return "Failed to create note (workspace page creation failed)."
            
            # Add content as paragraph block if provided
            if content:
                await create_block(
                    page["id"], "paragraph",
                    {"richText": [{"type": "text", "text": {"content": content}, "plainText": content}]},
                    parent_id=page.get("rootBlockId", "")
                )
            
            # Add to-do blocks for action items
            if action_items:
                for item_text in action_items.split(";"):
                    item_text = item_text.strip()
                    if item_text:
                        await create_block(
                            page["id"], "to_do",
                            {"richText": [{"type": "text", "text": {"content": item_text}, "plainText": item_text}], "checked": False},
                            parent_id=page.get("rootBlockId", "")
                        )
            
            # Add metadata callout
            meta_lines = []
            if note_type and note_type != "quick":
                meta_lines.append(f"Type: {note_type}")
            if meeting_date:
                meta_lines.append(f"Meeting: {meeting_date}")
            if attendees:
                meta_lines.append(f"Attendees: {attendees}")
            if tags:
                meta_lines.append(f"Tags: {tags}")
            
            if meta_lines:
                await create_block(
                    page["id"], "callout",
                    {"richText": [{"type": "text", "text": {"content": "\n".join(meta_lines)}, "plainText": "\n".join(meta_lines)}]},
                    parent_id=page.get("rootBlockId", "")
                )
            
            return f"✅ Note created: \"{title}\" (id: {page['id'][:8]}...)"

        # LIST: List notes (pages)
        elif action == "list":
            pages = await list_pages()
            if not pages:
                return "No notes found."
            
            lines = [f"📝 {len(pages)} notes:"]
            for p in pages[:10]:
                t = _plain_title(p.get("title", "Untitled"))[:50]
                emoji = p.get("icon", {}).get("emoji", "📝")
                lines.append(f"  {emoji} {t} (id: {p['id'][:8]}...)")
            return "\n".join(lines)

        # GET: Get a specific note with blocks
        elif action == "get":
            if not note_id:
                return "Error: note_id is required for get action."
            
            page = await get_page(note_id)
            if not page:
                return f"Note {note_id} not found."
            
            t = _plain_title(page.get("title", "Untitled"))
            lines = [f"📋 {t}"]
            
            blocks = await get_page_blocks(note_id)
            for b in (blocks or [])[:20]:
                bt = b.get("type", "")
                props = b.get("properties", {})
                
                if bt == "paragraph" and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"][:3])
                    if text.strip():
                        lines.append(f"  {text[:100]}")
                elif bt in ("heading_1", "heading_2") and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"])
                    lines.append(f"  # {text}")
                elif bt == "to_do" and props.get("richText"):
                    checked = "✅" if props.get("checked") else "☐"
                    text = props.get("richText", [{}])[0].get("plainText", "")
                    lines.append(f"  {checked} {text}")
                elif bt == "callout" and props.get("richText"):
                    text = props.get("richText", [{}])[0].get("plainText", "")
                    if "Type:" in text or "Meeting:" in text:
                        lines.append(f"  ℹ️ {text[:80]}")
            
            return "\n".join(lines)

        # UPDATE: Update a note (not fully implemented - would need block diffing)
        elif action == "update":
            if not note_id:
                return "Error: note_id is required for update action."
            # For simplicity, we add a callout with update info
            if content:
                await create_block(
                    note_id, "callout",
                    {"richText": [{"type": "text", "text": {"content": f"Updated: {content}"}, "plainText": f"Updated: {content}"}]}
                )
            return f"✅ Note updated (appended to page {note_id[:8]}...)"

        # SEARCH: Search notes via workspace search
        elif action == "search":
            if not search:
                return "Error: search query is required."
            
            results = await search_workspace(search)
            items = results.get("results", [])
            if not items:
                return f"No notes found matching '{search}'."
            
            lines = [f"🔍 Found {len(items)} results for '{search}':"]
            for r in items[:8]:
                if r.get("type") == "page":
                    lines.append(f"  📄 {r.get('title', 'Untitled')} (id: {r.get('id', '')[:8]}...)")
            return "\n".join(lines)

        # ADD_ACTION: Add to-do block to note
        elif action == "add_action":
            if not note_id:
                return "Error: note_id is required."
            if not action_item_text:
                return "Error: action_item_text is required."
            
            block = await create_block(
                note_id, "to_do",
                {"richText": [{"type": "text", "text": {"content": action_item_text}, "plainText": action_item_text}], "checked": False}
            )
            return f"✅ Action item added: \"{action_item_text}\"" if block else "Failed to add action item."

        # COMPLETE_ACTION: Mark to-do as complete (via update row not implemented yet)
        elif action == "complete_action":
            # Note: This would need block update capability in pi_workspace.py
            # For now, we return a message directing to use manage_workspace
            return "Action item completion: Please use manage_workspace(action='update_task') instead."

        # LIST_ACTIONS: List to-do blocks
        elif action == "list_actions":
            if not note_id:
                return "Error: note_id is required."
            
            blocks = await get_page_blocks(note_id)
            todos = [b for b in (blocks or []) if b.get("type") == "to_do"]
            
            if not todos:
                return "No action items in this note."
            
            lines = [f"Action Items ({len(todos)})"]
            for t in todos:
                props = t.get("properties", {})
                checked = "✅" if props.get("checked") else "☐"
                text = props.get("richText", [{}])[0].get("plainText", "") if props.get("richText") else ""
                lines.append(f"  {checked} {text}")
            
            return "\n".join(lines)

        else:
            return f"Unknown action: {action}. Available: create, list, get, update, search, add_action, complete_action, list_actions"

    except Exception as e:
        logger.error(f"Notes handler error: {e}", exc_info=True)
        return f"Notes operation failed: {str(e)}"
