"""
Nova Agent Pi Workspace Client

Provides full access to the Pi Workspace backend (port 8762) for:
- Pages (notes, documents, journals)
- Databases (tables with rows and views)
- Planner (tasks, events, daily planning)
- Forms (data collection with auto-row creation)
- Search (hybrid FTS + vector)
- AI Chat (context-grounded conversations)
- Component Registry (PiCode Agent recognition)
"""

import os
import json
import aiohttp
from typing import Any, Optional
from loguru import logger

PI_WORKSPACE_URL = os.environ.get("PI_WORKSPACE_URL", "http://localhost:8762")
PI_WORKSPACE_API_KEY = os.environ.get("PI_WORKSPACE_API_KEY", "")
_timeout = aiohttp.ClientTimeout(total=15)


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if PI_WORKSPACE_API_KEY:
        headers["X-API-Key"] = PI_WORKSPACE_API_KEY
    return headers


async def _ws_get(path: str, params: dict | None = None) -> dict | list | None:
    async with aiohttp.ClientSession(timeout=_timeout) as s:
        async with s.get(f"{PI_WORKSPACE_URL}{path}", params=params, headers=_headers()) as r:
            if r.status == 200:
                return await r.json()
            text = await r.text()
            logger.warning(f"Workspace GET {path} failed: {r.status} {text[:200]}")
            return None


async def _ws_post(path: str, body: dict) -> dict | None:
    async with aiohttp.ClientSession(timeout=_timeout) as s:
        async with s.post(f"{PI_WORKSPACE_URL}{path}", json=body, headers=_headers()) as r:
            if r.status in (200, 201):
                return await r.json()
            text = await r.text()
            logger.warning(f"Workspace POST {path} failed: {r.status} {text[:200]}")
            return None


async def _ws_put(path: str, body: dict) -> dict | None:
    async with aiohttp.ClientSession(timeout=_timeout) as s:
        async with s.put(f"{PI_WORKSPACE_URL}{path}", json=body, headers=_headers()) as r:
            if r.status == 200:
                return await r.json()
            text = await r.text()
            logger.warning(f"Workspace PUT {path} failed: {r.status} {text[:200]}")
            return None


async def _ws_delete(path: str) -> bool:
    async with aiohttp.ClientSession(timeout=_timeout) as s:
        async with s.delete(f"{PI_WORKSPACE_URL}{path}", headers=_headers()) as r:
            return r.status == 200


async def _get_workspace_id() -> str:
    """Get the default workspace ID."""
    workspaces = await _ws_get("/api/workspaces")
    if workspaces and isinstance(workspaces, list) and len(workspaces) > 0:
        return workspaces[0]["id"]
    return ""


def _plain_title(title_field: Any) -> str:
    """Extract plain text from a RichText title field."""
    if isinstance(title_field, str):
        return title_field
    if isinstance(title_field, list):
        parts = []
        for seg in title_field:
            if isinstance(seg, dict):
                parts.append(seg.get("plainText", seg.get("text", {}).get("content", "")))
            elif isinstance(seg, str):
                parts.append(seg)
        return "".join(parts)
    return str(title_field)


# ---------------------------------------------------------------------------
# Pages (Notes)
# ---------------------------------------------------------------------------

async def create_page(title: str, parent_id: str = "", icon: str = "") -> dict:
    ws_id = await _get_workspace_id()
    body: dict = {"title": title}
    if parent_id:
        body["parentId"] = parent_id
    if icon:
        body["icon"] = {"type": "emoji", "emoji": icon}
    return await _ws_post(f"/api/workspaces/{ws_id}/pages", body) or {}


async def create_page_with_blocks(title: str, blocks: list[dict], icon: str = "", parent_id: str = "") -> dict:
    """Create a page and populate it with multiple blocks in one call.

    Args:
        title: Page title
        blocks: List of {type, content?, properties?} dicts.
                type: paragraph, heading_1, heading_2, heading_3, to_do,
                      bulleted_list_item, numbered_list_item, callout, quote,
                      code, divider, planner_task
                content: Text content (auto-wrapped into richText)
                properties: Override block properties (merged with content)
        icon: Emoji icon for the page
        parent_id: Parent page ID

    Returns:
        Created page dict with block_ids list
    """
    page = await create_page(title, parent_id=parent_id, icon=icon)
    if not page or "id" not in page:
        return {}
    page_id = page["id"]
    root_block_id = page.get("rootBlockId", "")
    block_ids = []

    for i, blk in enumerate(blocks):
        block_type = blk.get("type", "paragraph")
        content = blk.get("content", "")
        props = blk.get("properties", {})

        # Auto-wrap plain content into richText if not already provided
        if content and "richText" not in props and "title" not in props:
            props["richText"] = [{"type": "text", "text": {"content": content}, "plainText": content}]

        block = await create_block(page_id, block_type, props, parent_id=root_block_id)
        if block and "id" in block:
            block_ids.append(block["id"])

    page["block_ids"] = block_ids
    page["block_count"] = len(block_ids)
    return page


async def list_pages() -> list:
    ws_id = await _get_workspace_id()
    result = await _ws_get(f"/api/workspaces/{ws_id}/pages")
    return result if isinstance(result, list) else []


async def get_page(page_id: str) -> dict | None:
    return await _ws_get(f"/api/pages/{page_id}")


async def get_page_blocks(page_id: str) -> list:
    result = await _ws_get(f"/api/pages/{page_id}/blocks")
    return result if isinstance(result, list) else []


async def delete_page(page_id: str) -> bool:
    """Delete a page in Pi Workspace."""
    return await _ws_delete(f"/api/pages/{page_id}")


async def create_block(page_id: str, block_type: str, properties: dict, parent_id: str = "") -> dict:
    body = {"type": block_type, "properties": properties}
    if parent_id:
        body["parentId"] = parent_id
    return await _ws_post(f"/api/pages/{page_id}/blocks", body) or {}


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

async def create_database(title: str, schema_props: list[dict]) -> dict:
    ws_id = await _get_workspace_id()
    body = {"title": title, "schema": schema_props}
    return await _ws_post(f"/api/workspaces/{ws_id}/databases", body) or {}


async def list_databases() -> list:
    ws_id = await _get_workspace_id()
    result = await _ws_get(f"/api/workspaces/{ws_id}/databases")
    return result if isinstance(result, list) else []


async def get_database(db_id: str) -> dict | None:
    return await _ws_get(f"/api/databases/{db_id}")


async def list_database_rows(db_id: str) -> list:
    result = await _ws_get(f"/api/databases/{db_id}/rows")
    return result if isinstance(result, list) else []


async def create_database_row(db_id: str, title: str, properties: dict = {}) -> dict:
    return await _ws_post(f"/api/databases/{db_id}/rows", {"title": title, "properties": properties}) or {}


async def update_database_row(row_id: str, properties: dict) -> dict:
    return await _ws_put(f"/api/database-rows/{row_id}", {"properties": properties}) or {}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

async def get_planner_day(date: str = "") -> dict:
    ws_id = await _get_workspace_id()
    params = {}
    if date:
        params["date"] = date
    return await _ws_get(f"/api/workspaces/{ws_id}/planner", params) or {}


async def create_task(title: str, priority: str = "medium", due_date: str = "",
                      due_time: str = "", source_type: str = "manual",
                      source_id: str = "", assignee: str = "", tags: list = []) -> dict:
    ws_id = await _get_workspace_id()
    body: dict = {"title": title, "priority": priority}
    if due_date:
        body["dueDate"] = due_date
    if due_time:
        body["dueTime"] = due_time
    if source_type:
        body["sourceType"] = source_type
    if source_id:
        body["sourceId"] = source_id
    if assignee:
        body["assignee"] = assignee
    if tags:
        body["tags"] = tags
    return await _ws_post(f"/api/workspaces/{ws_id}/planner/tasks", body) or {}


async def update_task(task_id: str, patch: dict) -> dict:
    return await _ws_put(f"/api/planner/tasks/{task_id}", patch) or {}


async def delete_task(task_id: str) -> bool:
    return await _ws_delete(f"/api/planner/tasks/{task_id}")


async def create_event(title: str, start_time: str, end_time: str,
                       location: str = "", source_type: str = "manual",
                       source_id: str = "", is_all_day: bool = False) -> dict:
    ws_id = await _get_workspace_id()
    body: dict = {"title": title, "startTime": start_time, "endTime": end_time}
    if location:
        body["location"] = location
    if source_type:
        body["sourceType"] = source_type
    if source_id:
        body["sourceId"] = source_id
    if is_all_day:
        body["isAllDay"] = True
    return await _ws_post(f"/api/workspaces/{ws_id}/planner/events", body) or {}


async def update_planner_notes(date: str, notes: list) -> bool:
    ws_id = await _get_workspace_id()
    result = await _ws_put(f"/api/workspaces/{ws_id}/planner/notes?date={date}", {"notes": notes})
    return result is not None


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------

async def create_form(db_id: str, title: str, fields: list[dict],
                      target_database_id: str = "", property_mapping: dict = {}) -> dict:
    body: dict = {"title": title, "fields": fields}
    if target_database_id:
        body["targetDatabaseId"] = target_database_id
    if property_mapping:
        body["propertyMapping"] = property_mapping
    return await _ws_post(f"/api/databases/{db_id}/forms", body) or {}


async def get_form(form_id: str) -> dict | None:
    return await _ws_get(f"/api/forms/{form_id}")


async def submit_form(form_id: str, values: dict, submitted_by: str = "nova-agent") -> dict:
    body = {**values, "submittedBy": submitted_by}
    return await _ws_post(f"/api/forms/{form_id}/submit", body) or {}


async def list_form_submissions(form_id: str) -> list:
    result = await _ws_get(f"/api/forms/{form_id}/submissions")
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search_workspace(query: str) -> dict:
    return await _ws_get("/api/search", {"q": query}) or {}


# ---------------------------------------------------------------------------
# AI Chat
# ---------------------------------------------------------------------------

async def ai_chat(message: str, conversation_id: str = "") -> dict:
    body: dict = {"message": message}
    if conversation_id:
        body["conversationId"] = conversation_id
    return await _ws_post("/api/ai/chat", body) or {}


# ---------------------------------------------------------------------------
# Component Registry (PiCode Agent)
# ---------------------------------------------------------------------------

async def get_component_registry() -> list:
    result = await _ws_get("/api/agent/components")
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

async def list_templates() -> list:
    result = await _ws_get("/api/templates")
    return result if isinstance(result, list) else []


async def create_from_template(workspace_id: str, template_id: str, title: str = "") -> dict:
    body = {"templateId": template_id}
    if title:
        body["title"] = title
    return await _ws_post(f"/api/workspaces/{workspace_id}/pages/from-template", body) or {}
