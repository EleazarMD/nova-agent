"""
Nova Agent tool definitions and handlers.

Tools are registered with Pipecat's LLM function calling system.
Each tool is an OpenAI-format function definition + an async handler.

Native tools: casual/fast (weather, lights, workstation, reminders)
Delegated tools: complex tasks via OpenClaw with SSE streaming for progress
"""

import asyncio
import json
import os
import aiohttp
import jwt
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo
from loguru import logger

# Skill-based imports (refactored to Claude Skills architecture)
import sys
from pathlib import Path
SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Import from skill scripts using direct file loading
import importlib.util

def _load_skill_module(skill_name: str, script_name: str):
    """Load a skill module from the skills directory."""
    script_path = SKILLS_DIR / skill_name / "scripts" / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(f"skills.{skill_name}.{script_name}", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# Load skill modules
_homelab_ops = _load_skill_module("homelab-operations", "operations")
handle_service_status = _homelab_ops.handle_service_status
handle_service_logs = _homelab_ops.handle_service_logs
handle_service_health_check = _homelab_ops.handle_service_health_check
# Unified homelab_operations handler (per /claude-skills spec)
_homelab_operations = _homelab_ops.homelab_operations

async def handle_homelab_operations(action: str, container: str = "", service: str = "", lines: int = 50) -> dict:
    """
    Unified homelab operations handler (per /claude-skills spec).
    Dispatches to appropriate handler based on action parameter.
    """
    return await _homelab_operations(action=action, container=container, service=service, lines=lines)

from nova.homelab_mutate import (
    handle_service_restart,
    handle_service_start,
    handle_service_stop,
)

_ev_routing = _load_skill_module("ev-route-planner", "ev_route_planner")
handle_ev_route_planner = _ev_routing.handle_ev_route_planner

_notes = _load_skill_module("notes-manager", "notes_manager")
handle_manage_notes = _notes.handle_manage_notes

from nova.exomind import handle_exomind

# Tesla control (skill-based)
_tesla = _load_skill_module("tesla-control", "tesla_control")
handle_tesla_location_refresh = _tesla.handle_tesla_location_refresh
handle_tesla_control = _tesla.handle_tesla_control

# ---------------------------------------------------------------------------
# AI Inferencing Service key fetcher (centralized API key vault, port 9000)
# Constitutional rule: ALL API keys fetched via AI Inferencing, never hardcoded.
# ---------------------------------------------------------------------------

AI_INFERENCING_URL = os.environ.get("AI_INFERENCING_URL", "http://localhost:9000")
_key_cache: dict[str, tuple[str, float]] = {}  # provider -> (key, expiry_ts)
_KEY_CACHE_TTL = 300  # 5 minutes

# Import JWT generator from dedicated auth module
from nova.hermes_auth import generate_hermes_jwt


async def _fetch_provider_key(provider: str) -> str | None:
    """Fetch an API key from AI Inferencing Service for nova-agent/{provider}.
    Caches keys in-memory for 5 minutes to avoid per-request HTTP calls."""
    import time
    now = time.time()
    cached = _key_cache.get(provider)
    if cached and cached[1] > now:
        return cached[0]

    url = f"{AI_INFERENCING_URL}/api/v1/keys/nova-agent/{provider}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    key = data.get("apiKey")
                    if key:
                        _key_cache[provider] = (key, now + _KEY_CACHE_TTL)
                        logger.debug(f"Fetched {provider} key from AI Inferencing (cached={data.get('cached')})")
                        return key
                logger.warning(f"AI Inferencing returned {resp.status} for {provider}")
    except Exception as e:
        logger.warning(f"Failed to fetch {provider} key from AI Inferencing: {e}")
    return None

# Environment configuration
OPENCLAW_URL = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18793")
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "")
CONTEXT_BRIDGE_URL = os.environ.get("CONTEXT_BRIDGE_URL", "http://localhost:8764")
AI_GATEWAY_BUDGET_OVERRIDE = os.environ.get("AI_GATEWAY_BUDGET_OVERRIDE", "")
WORKSTATION_MONITOR_URL = os.environ.get("WORKSTATION_MONITOR_URL", "http://localhost:8404")
NETDIAG_URL = os.environ.get("NETDIAG_URL", "http://localhost:8405")
ECOSYSTEM_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
HERMES_CORE_URL = os.environ.get("HERMES_CORE_URL", "http://localhost:8780")
HERMES_JWT_TOKEN = os.environ.get("HERMES_JWT_TOKEN", "")
ECOSYSTEM_API_KEY = os.environ.get("ECOSYSTEM_API_KEY", "ai-gateway-api-key-2024")
ECOSYSTEM_USER_ID = os.environ.get("ECOSYSTEM_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")
INTERNAL_SERVICE_KEY = os.environ.get("INTERNAL_SERVICE_KEY", "")
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
SKILL_DISCOVERY_URL = os.environ.get("SKILL_DISCOVERY_URL", "http://127.0.0.1:18791")

# Progress callback type: called with (status_type, message) during delegation
ProgressCallback = Callable[[str, str], None]
_current_progress_callback: Optional[ProgressCallback] = None
_current_user_id: Optional[str] = None
_current_user_location: Optional[Any] = None  # Set from location service for timezone

# Server message callback — set by bot.py after RTVI is ready
_server_msg_fn: Optional[Callable] = None


def set_server_msg_fn(fn: Callable):
    """Set the async function used to send server messages to iOS."""
    global _server_msg_fn
    _server_msg_fn = fn
    # Wire server_msg_fn into skill modules that need it
    _init_web_search_skill()


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    # -------------------------------------------------------------------------
    # Native tools (casual, fast)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and forecast. Use for weather queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or coordinates (e.g. 'Houston', 'Humble TX')",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_lights",
            "description": "Control Philips Hue lights. Use for 'turn on lights', 'dim bedroom', 'set to blue'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["on", "off", "brightness", "color", "scene", "status"],
                        "description": "Action to perform",
                    },
                    "target": {
                        "type": "string",
                        "description": "Light name, room name, or scene name",
                    },
                    "value": {
                        "type": "string",
                        "description": "Brightness (0-100) or color hex (#FF0000)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workstation_status",
            "description": "Get RTX Workstation status: GPU temps, VRAM, running models.",
            "parameters": {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string",
                        "enum": ["summary", "full", "alerts"],
                        "description": "Level of detail (default summary)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder. Will notify via push notification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder message",
                    },
                    "when": {
                        "type": "string",
                        "description": "When to remind (e.g. 'in 30 minutes', 'at 3pm')",
                    },
                },
                "required": ["message", "when"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Search tool — definition loaded from skills/web-search/SKILL.md at bottom of file
    # (see _merge_skill_definitions)
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # Delegated tools (browser, email, calendar, shell — use for actions, not searches)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "openclaw_delegate",
            "description": (
                "Execute complex, long-horizon tasks via OpenClaw. Use for ACTIONS: "
                "buying tickets, making reservations, placing orders, booking appointments, "
                "searching/sending emails, creating calendar events, "
                "running shell commands, editing files, filling out web forms. "
                "Also use for STUDIO JOBS that create or generate content: "
                "'start a deep research on X' → Deep Research Studio (multi-step analysis, comprehensive reports), "
                "'create a podcast about X' → Podcast Studio, "
                "'generate an image of X' → Image Studio, "
                "'write a news story about X' → News Studio, "
                "'draft an email to X' → Email (via browser). "
                "IMPORTANT: Nova handles ALL web searches directly via web_search (both fast and deep modes). "
                "Only delegate to OpenClaw for long-horizon deep research requiring multi-step analysis, "
                "synthesis, and comprehensive reporting beyond simple web queries. "
                "For READING results (status, summaries, calendar), use check_studio — it's instant. "
                "Describe the task clearly — OpenClaw will execute and stream progress back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Specific task to execute, including all relevant details (dates, names, locations, preferences)",
                    },
                    "context": {
                        "type": "string",
                        "description": "Background context from the conversation that helps OpenClaw understand the request",
                    },
                },
                "required": ["task"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Memory tools (PIC — Personal Integration Core)
    # PIC is the centralized personal data service shared by all homelab agents.
    # It stores identity, preferences, goals, and observations in a knowledge graph.
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a user fact, preference, or important detail to PIC (Personal Integration Core) — "
                "the user's persistent memory system shared across all AI agents in the homelab. "
                "Use when the user states a preference, corrects you, or shares personal info "
                "they'd want remembered across conversations. "
                "Examples: food preferences, family details, work habits, communication style."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The fact or preference to remember, written in third person (e.g. 'User prefers espresso on the rocks')",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["communication", "work", "scheduling", "learning", "health", "social", "creative", "finance", "technology", "food", "family", "other"],
                        "description": "Category for the preference (pick the best fit)",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Search PIC for stored preferences, facts, or personal details about the user. "
                "Use when you need to check what you already know before asking the user. "
                "Example queries: 'coffee order', 'kids names', 'work schedule', 'food preferences'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up (e.g. 'Starbucks order', 'family members', 'meeting preferences')",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["communication", "work", "scheduling", "learning", "health", "social", "creative", "finance", "technology", "food", "family", "other"],
                        "description": "Optional: narrow search to a specific category",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_past_conversations",
            "description": (
                "Search past conversations with the user to recall what was discussed previously. "
                "Use when user asks 'what did we talk about yesterday', 'remember when I mentioned X', "
                "'what was that thing we discussed last week', or when you need historical context. "
                "Returns conversation snippets ranked by relevance. "
                "Use a broad query like 'recent' to browse recent conversations. "
                "Time intervals: use days_back for simple lookups, or from_days+to_days for windows "
                "(e.g. from_days=90, to_days=7 means 'between 3 months ago and 1 week ago'). "
                "ALWAYS use days_back=30 unless the user specifies a narrower range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for. Use specific terms like 'road trip', 'email sync', 'hamburger'. Use 'recent' to browse all recent conversations.",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "How many days back from now to search. Default 30. Use this for simple 'last N days' lookups.",
                        "default": 30,
                    },
                    "from_days": {
                        "type": "integer",
                        "description": "Far boundary in days ago (e.g. 90 = 3 months ago). Use with to_days for time windows.",
                    },
                    "to_days": {
                        "type": "integer",
                        "description": "Near boundary in days ago (e.g. 7 = 1 week ago). Use with from_days. Set to 0 for 'up to now'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Record a correction in PIC when the user says to forget something "
                "or when a stored fact is no longer true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to match against stored memories (e.g. 'Sonos', 'morning')",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Studio quick-reads (direct dashboard API, no OpenClaw needed)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "check_studio",
            "description": (
                "Quickly read status or results from homelab studios WITHOUT going through OpenClaw. "
                "Use for fast lookups: 'what's on my calendar', 'check research status', "
                "'how's the podcast coming', 'any new news stories', 'show recent research', "
                "'give me my daily briefing', 'any emails need attention'. "
                "Calendar supports: events (today/tomorrow/this_week), briefing, intelligence. "
                "Email supports: briefing with action items, contact highlights, metrics, attachment reading. "
                "For CREATING or GENERATING content, use openclaw_delegate instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "studio": {
                        "type": "string",
                        "enum": ["calendar", "email", "research", "podcast", "news", "image", "workspace"],
                        "description": "Which studio to query",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["status", "recent", "list", "detail", "briefing", "today", "tomorrow", "this_week", "attachment"],
                        "description": "status=overview, recent=latest items, list=all, detail=specific item, briefing=AI summary, today/tomorrow/this_week=calendar date filter, attachment=read email attachment text (requires item_id=email_id)",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "Specific item ID for detail action",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search/filter query (e.g. keyword for calendar search, topic for research)",
                    },
                },
                "required": ["studio"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Skill Discovery (dynamic skill catalog from OpenClaw)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "discover_skills",
            "description": (
                "Discover what skills, studios, and capabilities are available in the homelab. "
                "Call this BEFORE delegating any generative or complex task to learn what's possible "
                "and what inputs each skill requires. Returns the skill catalog with descriptions, "
                "required inputs, and trigger patterns. "
                "Use for: 'what can you do', 'what studios are available', or before creating "
                "workspace pages, podcasts, research, images, or any content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Optional: get details for a specific skill by name (e.g. 'workspace_pages', 'deep_research')",
                    },
                },
                "required": [],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Network diagnostics (homelab-netdiag API)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "diagnose_network",
            "description": (
                "Diagnose homelab network health: gateway, internet, DNS, Tailscale VPN, "
                "Docker containers, homelab services, and UniFi router. "
                "Use for: 'is the network working', 'check connectivity', 'why can't I connect', "
                "'what services are down', 'tailscale status', 'docker status'. "
                "Can also ping a specific host or check a specific port."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check": {
                        "type": "string",
                        "enum": ["full", "ping", "dns", "port", "tailscale", "docker", "services"],
                        "description": "Type of check (default 'full' runs everything)",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target host/IP for ping, DNS lookup, or port check",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port number for port check",
                    },
                },
                "required": [],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Time (grounded current date/time)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": (
                "Get the current date and time in the user's timezone (America/Chicago). "
                "Use for: 'what time is it', 'what day is it', 'what's the date', "
                "or when you need to know the current time for scheduling or context."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Timers & alarms (in-memory, survive until Nova restarts)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "manage_timer",
            "description": (
                "Set, list, or cancel timers and alarms. Timers fire a push notification when done. "
                "Use for: 'set a 5 minute timer', 'timer for 30 minutes', 'remind me in 1 hour', "
                "'cancel my timer', 'what timers are running'. "
                "For calendar-based reminders at specific times, use openclaw_delegate instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "list", "cancel"],
                        "description": "Action to perform",
                    },
                    "duration_minutes": {
                        "type": "number",
                        "description": "Timer duration in minutes (for 'set' action)",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional label for the timer (e.g. 'laundry', 'pasta')",
                    },
                    "timer_id": {
                        "type": "string",
                        "description": "Timer ID to cancel (from 'list' output)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Homelab infrastructure operations (Docker container management)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "service_status",
            "description": (
                "Get status of homelab Docker containers. Use for: "
                "'is hermes running', 'what containers are up', 'docker status', "
                "'check if openclaw is healthy'. Read-only, no approval needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Specific container name (e.g. 'hermes-core', 'openclaw'). Leave empty for all.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_logs",
            "description": (
                "Get recent logs from a homelab Docker container. Use for: "
                "'show hermes logs', 'what errors in openclaw', 'why did the container crash'. "
                "Read-only, no approval needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Container name (e.g. 'hermes-core', 'openclaw')",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to show (default 50, max 200)",
                    },
                },
                "required": ["container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_restart",
            "description": (
                "Restart a homelab Docker container. Use for: "
                "'restart hermes', 'hermes-core is unhealthy restart it', "
                "'openclaw seems stuck'. "
                "Auto-approved for allowlisted containers. Logged and audited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Container name to restart (e.g. 'hermes-core')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Brief explanation of why this restart is needed",
                    },
                },
                "required": ["container", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_start",
            "description": (
                "Start a stopped homelab Docker container. Use for: "
                "'start hermes-core', 'bring up openclaw'. "
                "Auto-approved for allowlisted containers. Logged and audited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Container name to start (e.g. 'hermes-core')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Brief explanation of why this container needs to be started",
                    },
                },
                "required": ["container", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_stop",
            "description": (
                "Stop a running homelab Docker container. DESTRUCTIVE — requires explicit "
                "user approval via push notification before execution. The user MUST approve "
                "on their device before the container is stopped. Use only when truly necessary. "
                "You MUST provide a clear 'intent' explaining why the stop is needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Container name to stop (e.g. 'hermes-core')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "REQUIRED: Clear explanation of why this container needs to be stopped",
                    },
                },
                "required": ["container", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_health_check",
            "description": (
                "Deep health check on homelab infrastructure — container state, ports, image, "
                "PLUS application-level probes for Hermes Core (email counts, calendar stats, "
                "Neo4j/ChromaDB/LLM Gateway database status). "
                "Use for: 'health check all services', 'homelab status', 'is hermes healthy', "
                "'how are the email and calendar databases', 'deep check on hermes'. Read-only. "
                "This is the BEST tool for a comprehensive homelab status report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "Specific container to check. Leave empty for all managed containers.",
                    },
                },
                "required": [],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Homelab Operations - Unified skill (per /claude-skills spec)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "homelab_operations",
            "description": (
                "Manage homelab Docker containers and systemd services with approval gating. "
                "Unified tool for all infrastructure operations — read-only actions need no approval, "
                "mutating actions (restart/start/stop) require user approval via Dashboard or iOS. "
                "Use for: 'restart hermes', 'check status of openclaw', 'get logs from container', "
                "'health check all services', 'start/stop containers'. "
                "Read-only actions: status, logs, health_check. Mutating actions: restart, start, stop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["restart", "start", "stop", "status", "logs", "health_check"],
                        "description": "Operation to perform: restart/start/stop (approval required), status/logs/health_check (read-only)",
                    },
                    "container": {
                        "type": "string",
                        "description": "Docker container name (e.g. 'hermes-core', 'openclaw-inference'). Required for restart/start/stop/logs.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Systemd service name (e.g. 'openclaw-gateway.service'). Alternative to container for restart/start/stop.",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines for 'logs' action (default: 50, max: 200)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Ticket tracker (homelab issue tracking — Nova creates, OpenClaw/Windsurf fix)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "manage_ticket",
            "description": (
                "Create, list, read, update, or delegate tickets in the homelab ticket tracker. "
                "Use this when you encounter bugs, issues, or feature gaps during conversations. "
                "Actions: create (new ticket), list (browse tickets), get (full detail), "
                "update (change status/priority/analysis), delegate (assign to openclaw or windsurf). "
                "OpenClaw handles minor code fixes (with approval). Windsurf handles structural work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "get", "update", "delegate"],
                        "description": "Action to perform",
                    },
                    "ticket_id": {
                        "type": "string",
                        "description": "Ticket ID (for get/update/delegate actions)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Ticket title (for create)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of the issue (for create)",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "description": "Ticket priority (for create/update)",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "major", "minor", "trivial"],
                        "description": "Issue severity (for create)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["bug", "feature", "improvement", "investigation", "maintenance"],
                        "description": "Ticket category (for create)",
                    },
                    "component": {
                        "type": "string",
                        "description": "Affected component (e.g. 'nova-agent', 'openclaw', 'hermes-core', 'dashboard')",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (for create)",
                    },
                    "source_context": {
                        "type": "string",
                        "description": "Conversation context that led to this ticket (for create)",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "triaged", "analyzing", "in_progress", "awaiting_approval", "resolved", "closed", "wont_fix"],
                        "description": "New status (for update)",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Assign to agent: 'openclaw', 'windsurf', or user (for update/create)",
                    },
                    "delegate_to": {
                        "type": "string",
                        "enum": ["openclaw", "windsurf"],
                        "description": "Delegate ticket to OpenClaw (minor fixes) or Windsurf (structural) (for delegate)",
                    },
                    "analysis": {
                        "type": "string",
                        "description": "Root cause analysis text (for update)",
                    },
                    "proposed_fix": {
                        "type": "string",
                        "description": "Proposed fix description (for update)",
                    },
                    "resolution": {
                        "type": "string",
                        "description": "Resolution summary (for update when closing)",
                    },
                    "affected_files": {
                        "type": "string",
                        "description": "Comma-separated file paths affected (for update)",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Max tickets to return (for list, default 10)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Workspace management (fast direct API calls, no OpenClaw needed)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "manage_workspace",
            "description": (
                "Manage workspace pages, permissions, share links, and AI templates. "
                "Fast operations (~10-50ms) that don't need OpenClaw. "
                "Actions: "
                "generate_template (create page structure from description), "
                "infer_schema (suggest database columns), "
                "grant_permission (give user page access), "
                "revoke_permission (remove access), "
                "create_share_link (public link), "
                "list_permissions, list_share_links. "
                "For creating/editing actual page CONTENT, use openclaw_delegate instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["generate_template", "infer_schema", "grant_permission", "revoke_permission", "create_share_link", "list_permissions", "list_share_links"],
                        "description": "Action to perform",
                    },
                    "page_id": {
                        "type": "string",
                        "description": "Page ID (for permission/share actions)",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Template purpose (for generate_template): meeting, project, docs, bug, review, decision, journal, database",
                    },
                    "description": {
                        "type": "string",
                        "description": "Database description (for infer_schema): e.g. 'bug tracker', 'inventory list'",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Target user ID (for grant/revoke permission)",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["owner", "editor", "commenter", "viewer"],
                        "description": "Permission role (for grant_permission, create_share_link)",
                    },
                    "permission_id": {
                        "type": "string",
                        "description": "Permission ID to revoke",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # EV Charging & Route Planning (NREL AFDC API)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "ev_route_planner",
            "description": (
                "Find EV charging stations and plan charging stops for road trips. "
                "Uses NREL Alternative Fuel Stations API. Supports Tesla Superchargers "
                "and all major networks. "
                "Actions: "
                "nearest (find stations near a location — address, city, or coordinates), "
                "route (find stations along a driving route — pass waypoints like 'Houston,TX;Austin,TX;Dallas,TX'), "
                "networks (list all available EV charging networks). "
                "Filter by network (tesla, chargepoint, electrify_america, evgo, blink) "
                "and DC fast chargers only. "
                "NOTE: If no location is provided, uses your Tesla's current cached location."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["nearest", "route", "networks"],
                        "description": "Action to perform",
                    },
                    "location": {
                        "type": "string",
                        "description": "Address, city/state, or ZIP (for 'nearest'). E.g. 'Humble, TX' or '77346'",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "Latitude (alternative to location, for 'nearest')",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Longitude (alternative to location, for 'nearest')",
                    },
                    "waypoints": {
                        "type": "string",
                        "description": "Semicolon-separated route locations (for 'route'). E.g. 'Houston,TX;San Antonio,TX;Austin,TX'",
                    },
                    "radius": {
                        "type": "number",
                        "description": "Search radius in miles (default 25 for nearest, 10 for route; max 500)",
                    },
                    "network": {
                        "type": "string",
                        "description": "Filter by network: tesla, chargepoint, electrify_america, evgo, blink",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10 for nearest, 20 for route; max 50)",
                    },
                    "dc_fast_only": {
                        "type": "boolean",
                        "description": "Only show DC fast chargers (default false)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Tesla Vehicle Location
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "tesla_location_refresh",
            "description": (
                "Refresh Tesla vehicle location on-demand. "
                "Triggers an immediate API call to get the latest vehicle location, "
                "bypassing the normal 30-minute polling cycle. "
                "Use this when you need current location for navigation, "
                "charging station lookup, or trip planning. "
                "The location is automatically cached and used by ev_route_planner "
                "when no explicit location is provided."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, refreshes the first available vehicle.",
                    },
                },
                "required": [],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Notes & Productivity (meeting notes, action items, quick capture)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": (
                "Create, edit, search, and manage notes — meeting notes, quick captures, "
                "project notes, and journals. Supports action items with assignees and due dates. "
                "Actions: "
                "create (new note with optional action items), "
                "list (browse notes by type/tag), "
                "get (read a specific note), "
                "update (edit note content/title/tags), "
                "search (find notes by content), "
                "add_action (add action item to note), "
                "complete_action (mark action item done), "
                "list_actions (show action items for a note). "
                "Use for: 'take meeting notes', 'create a note about X', 'add action item', "
                "'what are my pending action items', 'find my notes about Y'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "get", "update", "search", "add_action", "complete_action", "list_actions"],
                        "description": "Action to perform",
                    },
                    "note_id": {
                        "type": "string",
                        "description": "Note ID (for get, update, add_action, complete_action, list_actions)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Note title (for create, update)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Note content/body text (for create, update). Supports markdown.",
                    },
                    "note_type": {
                        "type": "string",
                        "enum": ["meeting", "quick", "project", "journal", "reference"],
                        "description": "Type of note (for create, list filter). Default: quick",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (for create, update, list filter). E.g. 'work,urgent,dr-coleman'",
                    },
                    "action_items": {
                        "type": "string",
                        "description": "Semicolon-separated action items (for create). E.g. 'Follow up with Dr. Coleman;Send proposal by Friday;Schedule next meeting'",
                    },
                    "meeting_date": {
                        "type": "string",
                        "description": "Meeting date in YYYY-MM-DD format (for create, update)",
                    },
                    "attendees": {
                        "type": "string",
                        "description": "Comma-separated attendee names (for create, update). E.g. 'Dr. Coleman,Sarah,Mike'",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search query (for search action)",
                    },
                    "action_item_id": {
                        "type": "string",
                        "description": "Action item ID (for complete_action)",
                    },
                    "action_item_text": {
                        "type": "string",
                        "description": "Action item text (for add_action)",
                    },
                    "action_item_completed": {
                        "type": "boolean",
                        "description": "Mark action item complete/incomplete (for complete_action). Default: true",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Max results (for list, search). Default: 20",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # ExoMind - Long-running tasks, reminders, and background job orchestration
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "exomind",
            "description": (
                "Delegate long-running tasks to ExoMind for background processing, "
                "reminders, and follow-up tracking. ExoMind monitors jobs, sends "
                "notifications when action is needed, and keeps you updated. "
                "Use for tasks that: take longer than a conversation, need reminders, "
                "require follow-up, or should run in the background. "
                "Actions: "
                "create (new background job/task), "
                "list (show active jobs), "
                "get (job details), "
                "update (change status/progress), "
                "complete (mark done), "
                "cancel (stop a job), "
                "remind (set a reminder/follow-up). "
                "Examples: 'remind me to follow up with Dr. Coleman in 2 days', "
                "'create a task to research X by Friday', 'what jobs are pending', "
                "'mark the Coleman follow-up complete'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "get", "update", "complete", "cancel", "remind"],
                        "description": "Action to perform",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job ID (for get, update, complete, cancel)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Job/reminder title (for create, remind)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of the task",
                    },
                    "job_type": {
                        "type": "string",
                        "enum": ["task", "research", "monitor", "reminder", "followup"],
                        "description": "Type of job. Default: task",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                        "description": "Priority level. Default: medium",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in YYYY-MM-DD or ISO format",
                    },
                    "due_in": {
                        "type": "string",
                        "description": "Relative due time: '2 hours', '3 days', 'tomorrow', 'end of week'",
                    },
                    "reminder_at": {
                        "type": "string",
                        "description": "Reminder time in ISO format",
                    },
                    "remind_in": {
                        "type": "string",
                        "description": "Relative reminder time: '30 minutes', '2 days', 'tomorrow'",
                    },
                    "recurrence": {
                        "type": "string",
                        "description": "Recurrence pattern: daily, weekly, monthly, or number of days",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "waiting_input", "completed", "failed"],
                        "description": "Job status (for update)",
                    },
                    "progress": {
                        "type": "integer",
                        "description": "Progress percentage 0-100 (for update)",
                    },
                    "status_message": {
                        "type": "string",
                        "description": "Status message/note (for update, complete)",
                    },
                    "source_note_id": {
                        "type": "string",
                        "description": "Link to source note (if job originated from a note's action item)",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Max results for list. Default: 20",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # YouTube - Search and play videos on Tesla dashboard
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "youtube",
            "description": (
                "Search YouTube and play videos on the Tesla dashboard Learn page. "
                "Use for: 'play a video about X', 'find YouTube videos on Y', "
                "'search for Z on YouTube', 'play some music videos'. "
                "Actions: "
                "search (find videos matching a query), "
                "play (open a video on Tesla dashboard). "
                "The video will start playing on the Tesla browser's Learn page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "play"],
                        "description": "Action to perform",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for search action)",
                    },
                    "video_id": {
                        "type": "string",
                        "description": "YouTube video ID to play (for play action, from search results)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # -------------------------------------------------------------------------
    # Context Bridge — Unified knowledge orchestration (PIC + KG-API)
    # -------------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "knowledge_query",
            "description": (
                "Query across personal knowledge (PIC) and general knowledge (KG-API) through the Context Bridge. "
                "Use for questions that might need both personal context AND general facts: "
                "'What frameworks apply to my clinical workflow goal?', "
                "'How should I approach the Coleman follow-up?', "
                "'Find connections between my goals and what I know'. "
                "This is the PRIMARY tool for complex knowledge synthesis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query. Be specific about what you need.",
                    },
                    "include_personal": {
                        "type": "boolean",
                        "description": "Include PIC identity, goals, preferences. Default: true",
                    },
                    "include_knowledge": {
                        "type": "boolean",
                        "description": "Include KG-API entities, facts, documents. Default: true",
                    },
                    "include_dimensions": {
                        "type": "boolean",
                        "description": "Include LIAM dimension matches and frameworks. Default: true",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_enriched_context",
            "description": (
                "Get enriched personal context for this conversation from the Context Bridge. "
                "Returns: identity, goals with applicable frameworks, relevant knowledge entities, "
                "and a pre-formatted context prompt. Use at conversation start for grounding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_goals": {
                        "type": "boolean",
                        "description": "Include active goals. Default: true",
                    },
                    "include_relationships": {
                        "type": "boolean",
                        "description": "Include relationship data. Default: false",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_goal_to_knowledge",
            "description": (
                "Create a bi-directional link between a PIC goal and a KG-API knowledge entity. "
                "Use when the user explicitly wants to connect a goal to relevant knowledge: "
                "'Link my email workflow goal to the Filter Model framework'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "The PIC goal ID to link",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "The KG-API entity ID to link",
                    },
                    "relevance": {
                        "type": "number",
                        "description": "Relevance score 0.0-1.0. Default: 0.5",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context for why this link matters",
                    },
                },
                "required": ["goal_id", "entity_id"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Merge skill-based tool definitions from SKILL.md frontmatter
# Skills with `tool_name` + `parameters` in their YAML frontmatter get their
# tool definition generated programmatically — no hardcoding needed.
# ---------------------------------------------------------------------------
from nova.skill_loader import load_skill_tool_definitions as _load_skill_defs

_skill_defs = _load_skill_defs()
# Only add skills whose tool_name isn't already in TOOL_DEFINITIONS
_existing_names = {d["function"]["name"] for d in TOOL_DEFINITIONS if "function" in d}
for _sd in _skill_defs:
    if _sd["function"]["name"] not in _existing_names:
        TOOL_DEFINITIONS.append(_sd)
        logger.info(f"Registered skill-based tool: {_sd['function']['name']}")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def handle_get_weather(location: str) -> str:
    """Get weather using OpenWeatherMap API (industry standard, reliable geocoding).
    API key fetched from AI Inferencing Service (centralized key vault)."""
    api_key = await _fetch_provider_key("openweathermap")
    if not api_key:
        logger.error("OpenWeatherMap key unavailable from AI Inferencing, falling back to wttr.in")
        return await _wttr_fallback(location)
    
    try:
        async with aiohttp.ClientSession() as session:
            # Normalize location for OpenWeatherMap geocoding:
            # Accepts "City", "City,US", "City,StateCode,US" but NOT "City ST" or "City, TX, 77346"
            import re
            loc = location.strip()
            # Strip zip codes (5-digit or 5+4)
            loc = re.sub(r',?\s*\d{5}(-\d{4})?', '', loc).strip().rstrip(',').strip()
            # Strip 2-letter US state abbreviations ("Humble TX" → "Humble", "Humble, TX" → "Humble")
            loc = re.sub(r',?\s+[A-Z]{2}$', '', loc).strip().rstrip(',').strip()
            # If empty after stripping, use original
            if not loc:
                loc = location.strip()
            # Append ,US for better US city disambiguation
            if ',' not in loc:
                loc = f"{loc},US"
            logger.debug(f"OpenWeatherMap query: '{location}' → '{loc}'")
            
            wx_url = (
                f"https://api.openweathermap.org/data/2.5/weather?"
                f"q={loc.replace(' ', '+')}"
                f"&appid={api_key}"
                f"&units=imperial"
            )
            async with session.get(wx_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 404:
                    return f"Could not find location: {location}"
                if resp.status != 200:
                    logger.warning(f"OpenWeatherMap API error: {resp.status}")
                    return await _wttr_fallback(location)
                
                data = await resp.json()
                name = data.get("name", location)
                country = data.get("sys", {}).get("country", "")
                main = data.get("main", {})
                weather = data.get("weather", [{}])[0]
                wind = data.get("wind", {})
                
                temp = main.get("temp", "?")
                humidity = main.get("humidity", "?")
                wind_speed = wind.get("speed", "?")
                desc = weather.get("description", "Unknown conditions").capitalize()
                
                place = f"{name}, {country}" if country else name
                return f"{place}: {desc}, {temp}°F, {humidity}% humidity, wind {wind_speed} mph"
    except Exception as e:
        logger.error(f"OpenWeatherMap error: {e}")
        return await _wttr_fallback(location)


def _wmo_code(code: int) -> str:
    """Convert WMO weather code to short description."""
    codes = {
        0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow",
        80: "Light showers", 81: "Showers", 82: "Heavy showers",
        95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
    }
    return codes.get(code, "Unknown conditions")


async def _wttr_fallback(location: str) -> str:
    """Fallback to wttr.in if Open-Meteo fails."""
    loc = location.replace(" ", "+")
    url = f"https://wttr.in/{loc}?format=%l:+%c+%t+%h+%w"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return f"Weather lookup failed for {location}."
                text = await resp.text()
                return text.strip()
    except Exception as e:
        logger.error(f"wttr.in fallback also failed: {e}")
        return f"Could not get weather for {location}."


async def handle_control_lights(action: str, target: str = "", value: str = "") -> str:
    """Control Philips Hue lights via openhue CLI."""
    try:
        if action == "status":
            result = await asyncio.create_subprocess_exec(
                "openhue", "get", "light", "--json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            data = json.loads(stdout.decode())
            on_count = sum(1 for l in data if l.get("on", {}).get("on", False))
            return f"{on_count} of {len(data)} lights are on."
        
        cmd = ["openhue", "set", "light"]
        if target:
            cmd.append(target)
        
        if action == "on":
            cmd.append("--on")
        elif action == "off":
            cmd.append("--off")
        elif action == "brightness" and value:
            cmd.extend(["--on", "--brightness", value])
        elif action == "color" and value:
            cmd.extend(["--on", "--rgb", value])
        elif action == "scene":
            cmd = ["openhue", "set", "scene", target]
        else:
            return f"Unknown light action: {action}"
        
        result = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await result.communicate()
        
        if action == "scene":
            return f"Scene '{target}' activated."
        return f"Lights {action}: {target or 'all'}"
    except FileNotFoundError:
        return "OpenHue CLI not installed. Cannot control lights."
    except Exception as e:
        logger.error(f"Light control error: {e}")
        return f"Could not control lights: {str(e)}"




async def handle_get_workstation_status(detail: str = "summary") -> str:
    """Get RTX Workstation status from monitoring API."""
    url = f"{WORKSTATION_MONITOR_URL}/api/monitoring/gpu-stats"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return "Could not get workstation status."
                data = await resp.json()
                
                gpus = data.get("gpus", [])
                cpu = data.get("cpu", {})
                
                if detail == "alerts":
                    alerts = []
                    for g in gpus:
                        if g.get("temperature", 0) >= 85:
                            alerts.append(f"GPU {g['id']} hot: {g['temperature']}°C")
                        mem_pct = g.get("memoryUsedMB", 0) / max(g.get("memoryTotalMB", 1), 1)
                        if mem_pct > 0.95:
                            alerts.append(f"GPU {g['id']} VRAM nearly full")
                    if cpu.get("temperature", 0) >= 80:
                        alerts.append(f"CPU hot: {cpu['temperature']}°C")
                    return "All systems normal." if not alerts else " ".join(alerts)
                
                elif detail == "full":
                    lines = []
                    for g in gpus:
                        vram_gb = g.get("memoryUsedMB", 0) // 1024
                        vram_total = g.get("memoryTotalMB", 0) // 1024
                        lines.append(
                            f"GPU {g['id']}: {g.get('utilization', 0)}% util, "
                            f"{g.get('temperature', 0)}°C, {vram_gb}/{vram_total}GB VRAM, "
                            f"{int(g.get('powerDraw', 0))}W"
                        )
                    lines.append(f"CPU: {cpu.get('utilization', 0):.1f}% util, {cpu.get('temperature', 0)}°C")
                    return "\n".join(lines)
                
                else:  # summary
                    gpu_temps = [g.get("temperature", 0) for g in gpus]
                    gpu_utils = [g.get("utilization", 0) for g in gpus]
                    avg_temp = sum(gpu_temps) / len(gpu_temps) if gpu_temps else 0
                    avg_util = sum(gpu_utils) / len(gpu_utils) if gpu_utils else 0
                    
                    # Get running processes
                    processes = []
                    for g in gpus:
                        for p in g.get("processes", []):
                            processes.append(p.get("name", "unknown"))
                    
                    result = f"GPUs: {int(avg_util)}% average utilization, {int(avg_temp)}°C average temp."
                    if processes:
                        result += f" Running: {', '.join(list(set(processes))[:3])}"
                    return result
    except Exception as e:
        logger.error(f"Workstation status error: {e}")
        return f"Could not get workstation status: {str(e)}"


async def handle_set_reminder(message: str, when: str) -> str:
    """Set a reminder via push notification."""
    logger.info(f"Reminder set: '{message}' at '{when}'")
    # TODO: Integrate with scheduler to actually fire at the right time
    return f"Reminder set: '{message}' for {when}. I'll notify you when it's time."


# ---------------------------------------------------------------------------
# Web Search via Perplexity Sonar (Claude Skill: web-search)
# ---------------------------------------------------------------------------
# Delegates to skills/web-search/scripts/web_search.py for mode-aware model
# selection (sonar in fast mode, sonar-pro in deep mode).

import importlib as _importlib
_ws_mod = _importlib.import_module("skills.web-search.scripts.web_search")
handle_web_search = _ws_mod.handle_web_search
_ws_set_config = _ws_mod.set_config
_ws_set_server_msg_fn = _ws_mod.set_server_message_fn
_ws_set_agent_mode = _ws_mod.set_agent_mode

# Configure the skill module with gateway credentials
_ws_set_config(AI_GATEWAY_URL, AI_GATEWAY_API_KEY)

def _init_web_search_skill():
    """Deferred init: wire server_msg_fn after bot.py sets it, and set agent mode."""
    if _server_msg_fn:
        _ws_set_server_msg_fn(_server_msg_fn)

def set_web_search_agent_mode(mode: str):
    """Set web search agent mode. Called when orchestration mode is determined.
    
    Args:
        mode: "fast" for sonar, "deep" for sonar-pro
    """
    _ws_set_agent_mode(mode)
    logger.info(f"Web search skill agent mode set to: {mode}")


# ---------------------------------------------------------------------------
# SSE Streaming OpenClaw Delegation with Progress Updates
# ---------------------------------------------------------------------------

async def handle_openclaw_delegate(
    task: str,
    context: str = "",
    progress_callback: Optional[Callable[[str, str], None]] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    Delegate a task to OpenClaw with SSE streaming for progress updates.
    
    - Streams SSE events from OpenClaw
    - Calls progress_callback with (status_type, message) for each update
    - If user is inactive (phone locked), sends push notifications instead
    """
    from nova.push import is_user_active, send_push
    
    if not OPENCLAW_TOKEN:
        return "OpenClaw delegation not configured."

    # ── Skill enrichment gate ─────────────────────────────────────────────
    # Auto-fetch skill catalog and match task to a known skill so OpenClaw
    # always receives structured execution instructions, even if the LLM
    # skipped discover_skills.
    skill_context = ""
    try:
        import time as _time
        now = _time.time()
        if not _skill_cache.get("data") or (now - _skill_cache.get("fetched_at", 0)) >= _SKILL_CACHE_TTL:
            async with aiohttp.ClientSession() as _sess:
                async with _sess.get(
                    f"{SKILL_DISCOVERY_URL}/api/v1/skills",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as _resp:
                    _skill_cache["data"] = await _resp.json()
                    _skill_cache["fetched_at"] = now

        catalog = _skill_cache.get("data", {})
        task_lower = task.lower()
        matched_skill = None
        for skill in catalog.get("active", []) + catalog.get("available", []):
            triggers = skill.get("triggers", [])
            name = skill.get("name", "")
            desc = (skill.get("description") or "").lower()
            if any(t.lower() in task_lower for t in triggers if t):
                matched_skill = skill
                break
            if name.replace("_", " ").replace("-", " ") in task_lower:
                matched_skill = skill
                break
            # keyword match on description fragments
            keywords = [w for w in desc.split() if len(w) > 4][:6]
            if sum(1 for kw in keywords if kw in task_lower) >= 2:
                matched_skill = skill
                break

        if matched_skill:
            parts = [f"\n\n--- Matched Skill: {matched_skill['name']} ---"]
            if matched_skill.get("description"):
                parts.append(f"Description: {matched_skill['description']}")
            ri = matched_skill.get("required_inputs")
            if ri and isinstance(ri, dict):
                for action, fields in ri.items():
                    if isinstance(fields, dict):
                        field_list = ", ".join(f"{k}: {v}" for k, v in fields.items())
                    elif isinstance(fields, list):
                        field_list = ", ".join(str(f) for f in fields)
                    else:
                        field_list = str(fields)
                    parts.append(f"Required inputs for '{action}': {field_list}")
            if matched_skill.get("gather_requirements"):
                parts.append(f"Pre-requisites: {matched_skill['gather_requirements'][:300]}")
            skill_context = "\n".join(parts)
            logger.info(f"openclaw_delegate: auto-matched skill '{matched_skill['name']}' for task")
    except Exception as e:
        logger.debug(f"Skill enrichment lookup failed (non-fatal): {e}")

    url = f"{OPENCLAW_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENCLAW_TOKEN}",
        "Content-Type": "application/json",
    }
    if AI_GATEWAY_BUDGET_OVERRIDE:
        headers["X-Budget-Override"] = AI_GATEWAY_BUDGET_OVERRIDE

    system_msg = (
        "Execute this task efficiently. Narrate your progress naturally as you work. "
        "Keep status updates brief (one sentence). Report final results concisely."
    )
    # Inject caller identity so OpenClaw can authenticate to downstream APIs
    # Resolve real user ID: iOS sends "default" but workspace ops need the actual owner
    resolved_user_id = user_id
    if not user_id or user_id == "default":
        resolved_user_id = ECOSYSTEM_USER_ID
    if resolved_user_id:
        system_msg += (
            f"\n\n--- Caller Identity ---\n"
            f"User ID: {resolved_user_id}\n"
            f"When making API calls that require authentication headers, use:\n"
            f"  X-User-Id: {resolved_user_id}\n"
            f"  X-Internal-Service-Key: $INTERNAL_SERVICE_KEY (from environment)\n"
            f"All workspace operations MUST use this user_id. Do NOT use hardcoded or default user IDs.\n"
            f"The user's primary workspace is 'Dr. Eleazar\\'s Workspace' (ID: 36e84af0-e52b-4bed-9a8f-01797e20792a)."
        )
    if context:
        system_msg += f"\n\nContext from conversation:\n{context}"
    if skill_context:
        system_msg += skill_context

    body = {
        "model": "openclaw",
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": task},
        ],
        "stream": True,
        "stream_options": {
            "channels": ["spoken"],
            "format": "openclaw.v1",
        },
    }

    final_result = ""
    spoken_result = ""  # Track spoken channel separately to avoid doubling
    last_status = ""
    has_spoken_channel = False  # If OpenClaw sends spoken deltas, prefer those over choices
    last_activity = asyncio.get_event_loop().time()
    heartbeat_task = None

    async def _heartbeat():
        """Send periodic 'still working' messages during long silences."""
        nonlocal last_activity
        _msgs = [
            "Still working on it...",
            "Almost there...",
            "Still searching...",
            "Hang tight, still processing...",
        ]
        idx = 0
        while True:
            await asyncio.sleep(8)
            elapsed = asyncio.get_event_loop().time() - last_activity
            if elapsed >= 7 and progress_callback:
                msg = _msgs[idx % len(_msgs)]
                idx += 1
                try:
                    await progress_callback("status", msg)
                except Exception:
                    pass
    
    try:
        heartbeat_task = asyncio.create_task(_heartbeat())
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"OpenClaw returned HTTP {resp.status}: {text[:200]}"

                # Parse SSE stream line-by-line
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str or line_str.startswith(":"):
                        continue
                    if not line_str.startswith("data: "):
                        continue
                    
                    data_str = line_str[6:]  # Remove "data: " prefix
                    if data_str == "[DONE]":
                        break
                    
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    
                    event_type = data.get("type", "")
                    last_activity = asyncio.get_event_loop().time()
                    
                    # Handle OpenClaw status events → ThinkingCard + phase label
                    if event_type == "openclaw.status":
                        phase = data.get("phase", "")
                        tool_name = data.get("tool", "")
                        phase_label = None  # If set, also send {phase: ...}
                        
                        if phase == "tool_start" and tool_name:
                            thinking_msg = f"🔧 Using {tool_name}"
                            phase_label = "tool_call"
                        elif phase == "tool_complete" and tool_name:
                            thinking_msg = f"✅ Finished {tool_name}"
                            phase_label = "thinking"
                        elif phase == "thinking":
                            thinking_msg = "💭 Analyzing results..."
                            phase_label = "thinking"
                        elif phase == "delegating":
                            thinking_msg = "🔄 Working on it..."
                            phase_label = "delegating"
                        else:
                            continue
                        
                        # Avoid duplicate status messages
                        if thinking_msg == last_status:
                            continue
                        last_status = thinking_msg
                        logger.info(f"OpenClaw progress: {thinking_msg}")
                        
                        if progress_callback:
                            # Update phase label on ThinkingCard
                            if phase_label:
                                await progress_callback("phase", phase_label)
                            # Append thinking content to ThinkingCard
                            await progress_callback("thinking", thinking_msg)
                    
                    # Handle spoken channel deltas (narration from OpenClaw)
                    elif event_type == "openclaw.channel.delta":
                        channel = data.get("channel", "")
                        delta = data.get("delta", "")
                        
                        if channel == "spoken" and delta:
                            has_spoken_channel = True
                            spoken_result += delta
                            
                            # Stream narration to user if active
                            if progress_callback:
                                await progress_callback("narration", delta)
                    
                    # Handle standard OpenAI-style content deltas
                    # Only use if we haven't received spoken channel data (avoid doubling)
                    elif "choices" in data and not has_spoken_channel:
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                final_result += content
                    
                    # Handle approval requests
                    if event_type == "openclaw.approval_required":
                        desc = data.get("description", "Action needs approval")
                        if user_id:
                            await send_push(
                                user_id=user_id,
                                title="Approval Needed",
                                body=desc,
                                data={"type": "approval_request", "action_id": data.get("action_id")},
                            )
                        return f"I need your approval: {desc}. Check your notifications."

        # Prefer spoken channel result if available (avoids doubling)
        result = (spoken_result if has_spoken_channel else final_result).strip()
        
        # Task complete - send push if user inactive
        if user_id and not is_user_active(user_id) and result:
            summary = result[:100] + "..." if len(result) > 100 else result
            await send_push(
                user_id=user_id,
                title="Task Complete",
                body=summary,
                data={"type": "task_complete", "task": task[:50]},
            )
        
        return result or "Task completed."
        
    except asyncio.TimeoutError:
        return "Task timed out after 10 minutes."
    except Exception as e:
        logger.error(f"OpenClaw delegation error: {e}")
        return f"Delegation error: {str(e)}"
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()


# ---------------------------------------------------------------------------
# Memory tool handlers (PIC-backed)
# ---------------------------------------------------------------------------

async def handle_save_memory(fact: str, category: str = "other") -> str:
    """Save a user preference/fact to PIC as an observation."""
    from nova.pic import record_observation
    
    # Derive a short key from the fact
    key = fact.split()[:4]  # first few words
    key_str = "_".join(w.lower().strip(".,!?'\"") for w in key if w.isalpha())[:40] or "user_stated"
    
    success = await record_observation(
        observation_type="preference",
        category=category,
        key=key_str,
        value=fact,
        context="User explicitly stated this during voice conversation with Nova",
    )
    if success:
        logger.info(f"PIC save_memory OK: [{category}] {fact[:80]}")
        return f"Saved to PIC ({category}): {fact}"
    return f"I'll remember that for this conversation, but couldn't save to long-term memory."


async def handle_recall_memory(query: str, category: str = "") -> str:
    """Search PIC for stored preferences and observations matching a query."""
    _pic = _load_skill_module("pic-memory", "pic_memory")
    get_preferences = _pic.get_preferences
    get_identity = _pic.get_identity

    # Synonym expansion — common words that map to PIC categories or keys
    _SYNONYMS: dict[str, list[str]] = {
        "kids": ["family", "son", "daughter", "child"],
        "children": ["family", "son", "daughter", "child"],
        "family": ["son", "daughter", "wife", "child", "kids"],
        "coffee": ["starbucks", "espresso", "latte", "roast"],
        "food": ["burger", "starbucks", "restaurant", "meal", "order"],
        "home": ["location", "address", "zip", "77346"],
        "work": ["meeting", "schedule", "office", "job"],
    }

    results = []

    prefs = await get_preferences(categories=[category] if category else None)
    query_lower = query.lower()
    query_words = set(query_lower.split())

    # Expand query with synonyms
    expanded_words = set(query_words)
    for word in query_words:
        if word in _SYNONYMS:
            expanded_words.update(_SYNONYMS[word])

    for p in prefs:
        val = p.get("value", "")
        key = p.get("key", "")
        cat = p.get("category", "")
        searchable = f"{key} {val} {cat}".lower()
        if any(word in searchable for word in expanded_words):
            results.append(f"[{cat}/{key}]: {val}")

    # If query mentions identity-related terms, include identity
    identity_terms = {"name", "role", "roles", "who", "timezone", "bio", "about", "me"}
    if identity_terms & expanded_words:
        identity = await get_identity()
        if identity:
            ident = identity.get("identity", {})
            results.append(f"Name: {ident.get('preferred_name', 'unknown')}")
            results.append(f"Roles: {', '.join(ident.get('roles', []))}")
            if ident.get('bio'):
                results.append(f"Bio: {ident['bio'][:150]}")

    if results:
        logger.info(f"PIC recall_memory: {len(results)} matches for '{query}'")
        return "Found in PIC:\n" + "\n".join(results[:10])

    logger.info(f"PIC recall_memory: no matches for '{query}'")
    return f"Nothing found in PIC for '{query}'. The user may not have told you this yet."


async def handle_forget_memory(keyword: str) -> str:
    """Forget is not directly supported — record a correction observation instead."""
    from skills.pic_memory.scripts.pic_memory import record_observation
    
    success = await record_observation(
        observation_type="correction",
        category="other",
        key="user_correction",
        value=f"User wants to forget/correct: {keyword}",
        context="User explicitly asked to forget or correct this",
    )
    if success:
        return f"Noted — I've recorded that correction about '{keyword}'."
    return f"I'll note that correction for this conversation."


_conversation_search_count: dict[str, int] = {}

async def handle_search_past_conversations(
    query: str, days_back: int = 30, limit: int = 5, user_id: str = "default",
    from_days: int = None, to_days: int = None,
) -> str:
    """Search past conversations for historical context."""
    from nova.store import search_past_conversations
    
    # Guard against looping — max 2 searches per user per conversation turn
    call_count = _conversation_search_count.get(user_id, 0) + 1
    _conversation_search_count[user_id] = call_count
    
    if call_count > 2:
        logger.warning(f"Conversation search: rate-limited for user {user_id} (call #{call_count})")
        return "You've already searched conversation history this turn. Summarize what you found and respond to the user. Do NOT search again."
    
    days_back = min(max(1, days_back), 180)
    limit = min(max(1, limit), 10)
    
    # Time window: from_days/to_days take precedence over days_back
    if from_days is not None and to_days is not None:
        from_days = min(max(0, from_days), 365)
        to_days = min(max(0, to_days), 365)
        results = await search_past_conversations(
            user_id, query, limit=limit, from_days=from_days, to_days=to_days
        )
    else:
        results = await search_past_conversations(user_id, query, days_back, limit)
    
    if not results:
        logger.info(f"Conversation search: no matches for '{query}' (last {days_back} days)")
        return (
            f"No past conversations found matching '{query}' in the last {days_back} days. "
            "Do NOT retry with a different query — tell the user you couldn't find a match."
        )
    
    logger.info(f"Conversation search: {len(results)} matches for '{query}'")
    
    output = [f"Found {len(results)} relevant past conversation(s):\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        snippet = r.get("snippet", "")[:200]
        msg_count = r.get("message_count", "")
        output.append(f"{i}. {title}" + (f" ({msg_count} messages)" if msg_count else ""))
        if snippet:
            output.append(f"   \"{snippet}\"")
    
    output.append("\nSummarize the relevant findings for the user. These are real past conversations.")
    return "\n".join(output)


def reset_conversation_search_count(user_id: str = ""):
    """Reset search counter — call at start of each new user turn."""
    if user_id:
        _conversation_search_count.pop(user_id, None)
    else:
        _conversation_search_count.clear()


# ---------------------------------------------------------------------------
# Studio quick-read handler (direct ecosystem dashboard API)
# ---------------------------------------------------------------------------

async def handle_check_studio(
    studio: str, action: str = "recent", item_id: str = "", query: str = ""
) -> str:
    """Read status/results from homelab studios via ecosystem dashboard API."""
    base = ECOSYSTEM_URL
    headers = {"X-API-Key": ECOSYSTEM_API_KEY}
    hermes = HERMES_CORE_URL
    # Generate JWT token dynamically for Hermes Core authentication
    hermes_token = generate_hermes_jwt()
    hermes_headers = {"Authorization": f"Bearer {hermes_token}"}
    try:
        async with aiohttp.ClientSession() as session:

            # --- Calendar (via Hermes Core :8780) ---
            if studio == "calendar":
                timeout = aiohttp.ClientTimeout(total=12)

                # Briefing — AI-generated daily summary with email context
                if action == "briefing":
                    url = f"{hermes}/v1/calendar-intelligence/briefing"
                    params: dict[str, Any] = {}
                    if query:
                        params["date"] = query  # YYYY-MM-DD
                    async with session.get(url, params=params, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Calendar briefing API returned HTTP {resp.status}."
                        data = await resp.json()
                        headline = data.get("headline", "")
                        summary = data.get("executive_summary", "")
                        meetings = data.get("total_meetings", 0)
                        focus = data.get("focus_time_minutes", 0)
                        recs = data.get("preparation_recommendations", [])
                        briefs = data.get("meeting_briefs", [])
                        parts = []
                        if headline:
                            parts.append(headline)
                        if summary:
                            parts.append(summary)
                        parts.append(f"{meetings} meetings, {focus} minutes of focus time.")
                        for b in briefs[:5]:
                            t = b.get("title", "")
                            st = b.get("start_time", "")
                            loc = b.get("location", "")
                            if "T" in str(st):
                                st = str(st).split("T")[1][:5]
                            line = f"- {st} {t}"
                            if loc:
                                line += f" at {loc}"
                            parts.append(line)
                        if recs:
                            parts.append("Tips: " + "; ".join(recs[:3]))
                        return "\n".join(parts)

                # Today / Tomorrow / This Week shortcuts
                elif action in ("today", "tomorrow", "this_week"):
                    slug = action.replace("_", "-")
                    url = f"{hermes}/v1/calendar/search/{slug}"
                    params = {}
                    if query:
                        params["query"] = query
                    async with session.get(url, params=params, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Calendar search API returned HTTP {resp.status}."
                        data = await resp.json()
                        results = data.get("results", [])
                        if not results:
                            return f"No events {action.replace('_', ' ')}."
                        lines = []
                        for r in results[:8]:
                            title = r.get("title", "Untitled")
                            st = r.get("start_time", "")
                            loc = r.get("location", "")
                            cal = r.get("calendar_name", "")
                            if "T" in str(st):
                                st = str(st).split("T")[1][:5]
                            line = f"- {st} {title}"
                            if loc:
                                line += f" ({loc})"
                            if cal:
                                line += f" [{cal}]"
                            lines.append(line)
                        return f"{len(results)} events {action.replace('_', ' ')}:\n" + "\n".join(lines)

                # Stats / Count — how many calendars are connected
                elif action in ("stats", "count", "status"):
                    url = f"{hermes}/v1/calendar/neo4j/stats"
                    async with session.get(url, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Calendar stats API returned HTTP {resp.status}."
                        data = await resp.json()
                        calendars = data.get("calendars", 0)
                        events = data.get("events", 0)
                        last_sync = data.get("last_sync", "unknown")
                        # Get calendar breakdown
                        cal_url = f"{hermes}/v1/calendar/neo4j/calendars"
                        async with session.get(cal_url, headers=hermes_headers, timeout=timeout) as cal_resp:
                            if cal_resp.status == 200:
                                cal_data = await cal_resp.json()
                                cal_list = cal_data.get("calendars", [])
                                # Group by account
                                by_account: dict[str, list[str]] = {}
                                for c in cal_list:
                                    acct = c.get("account_name", "Unknown")
                                    if acct not in by_account:
                                        by_account[acct] = []
                                    by_account[acct].append(c.get("name", "?"))
                                breakdown = ", ".join(f"{k}: {len(v)}" for k, v in by_account.items())
                                return f"Connected: {calendars} calendars, {events} events. By account: {breakdown}. Last sync: {last_sync}."
                        return f"Connected: {calendars} calendars, {events} events. Last sync: {last_sync}."

                # Default: upcoming events
                else:
                    url = f"{hermes}/v1/calendar/events"
                    params = {"days_forward": 7}
                    if query:
                        # Use search endpoint for keyword queries
                        url = f"{hermes}/v1/calendar/search"
                        async with session.post(url, json={"query": query, "date_relative": "next_7_days", "top_k": 10}, headers=hermes_headers, timeout=timeout) as resp:
                            if resp.status != 200:
                                return f"Calendar search returned HTTP {resp.status}."
                            data = await resp.json()
                            results = data.get("results", [])
                            if not results:
                                return f"No calendar events matching '{query}'."
                            lines = []
                            for r in results[:7]:
                                title = r.get("title", "Untitled")
                                st = r.get("start_time", "")
                                if "T" in str(st):
                                    st = str(st).split("T")[0] + " " + str(st).split("T")[1][:5]
                                lines.append(f"- {title} ({st})")
                            return f"{len(results)} matching events:\n" + "\n".join(lines)

                    async with session.get(url, params=params, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Calendar API returned HTTP {resp.status}."
                        data = await resp.json()
                        events = data.get("events", [])
                        if not events:
                            return "No upcoming calendar events. Mac Agent may need to sync."
                        lines = []
                        for ev in events[:8]:
                            title = ev.get("title", "Untitled")
                            start = ev.get("start_time", "")
                            if "T" in str(start):
                                start = str(start).split("T")[0] + " " + str(start).split("T")[1][:5]
                            lines.append(f"- {title} ({start})")
                        return f"{len(events)} upcoming events:\n" + "\n".join(lines)

            # --- Email Intelligence (via Hermes Core :8780) ---
            elif studio == "email":
                timeout = aiohttp.ClientTimeout(total=12)

                # Helper: get email health data (reused)
                async def _email_health():
                    url = f"{hermes}/health"
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            return None, f"Hermes Core returned HTTP {resp.status}."
                        return await resp.json(), None

                if action == "briefing":
                    health, err = await _email_health()
                    if err:
                        return err
                    total = health.get("indexed_emails", {}).get("total", 0)
                    sent = health.get("indexed_emails", {}).get("sent", 0)
                    inbox = health.get("indexed_emails", {}).get("inbox", 0)
                    components = health.get("components", {})

                    # Now read latest briefing for action items
                    briefing_url = f"{hermes}/v1/calendar-email/briefings"
                    async with session.get(briefing_url, headers=hermes_headers, timeout=timeout) as resp2:
                        briefings_data = {}
                        if resp2.status == 200:
                            briefings_data = await resp2.json()

                    pending = briefings_data.get("count", 0)
                    parts = [
                        f"Email Intelligence: {total:,} indexed emails ({inbox:,} inbox, {sent:,} sent).",
                        f"Neo4j: {components.get('neo4j', '?')}, ChromaDB: {components.get('chromadb', '?')}, LLM: {components.get('llm_gateway', '?')}.",
                        f"Pending meeting briefings with email context: {pending}.",
                    ]
                    return "\n".join(parts)

                # Recent: hybrid search (keyword + vector + date filtering)
                elif action == "recent":
                    search_url = f"{hermes}/v1/emails/search/hybrid"
                    search_query = query or "recent important emails"
                    
                    # Intelligent date filtering based on query
                    date_relative = "last_7_days"  # Default
                    query_lower = search_query.lower()
                    if any(x in query_lower for x in ["today", "this morning", "this afternoon"]):
                        date_relative = "today"
                    elif any(x in query_lower for x in ["yesterday", "last night"]):
                        date_relative = "yesterday"
                    elif any(x in query_lower for x in ["last 24 hours", "past day", "past 24"]):
                        date_relative = "last_24_hours"
                    elif any(x in query_lower for x in ["this week", "past week", "last week"]):
                        date_relative = "last_7_days"
                    elif any(x in query_lower for x in ["this month", "past month", "last 30"]):
                        date_relative = "last_30_days"
                    
                    async with session.post(
                        search_url,
                        json={
                            "query": search_query,
                            "top_k": 10,
                            "include_sent": True,
                            "include_inbox": True,
                            "date_relative": date_relative,
                        },
                        headers=hermes_headers, timeout=timeout,
                    ) as resp:
                        if resp.status != 200:
                            return f"Email search returned HTTP {resp.status}."
                        data = await resp.json()
                        results = data.get("results", [])
                        if not results:
                            return f"No emails matching '{search_query}'."
                        lines = []
                        for r in results[:8]:
                            subj = r.get("subject", "No subject")[:80]
                            sender = r.get("from_addr", r.get("from_email", r.get("from", "?")))
                            date = str(r.get("date", ""))[:16]
                            snippet = (r.get("snippet", "") or r.get("ai_summary", ""))[:80]
                            eid = r.get("email_id", r.get("id", ""))
                            line = f"- {subj} (from {sender}, {date})"
                            if snippet:
                                line += f"\n  {snippet}"
                            if eid:
                                line += f"\n  [id: {eid[:60]}]"
                            lines.append(line)
                        return f"{len(results)} emails found:\n" + "\n".join(lines)

                # List: inbox summary with counts
                elif action == "list":
                    health, err = await _email_health()
                    if err:
                        return err
                    total = health.get("indexed_emails", {}).get("total", 0)
                    sent = health.get("indexed_emails", {}).get("sent", 0)
                    inbox = health.get("indexed_emails", {}).get("inbox", 0)
                    components = health.get("components", {})
                    status_val = health.get("status", "unknown")
                    parts = [
                        f"Email service: {status_val}",
                        f"Total indexed: {total:,} ({inbox:,} inbox, {sent:,} sent)",
                        f"Neo4j: {components.get('neo4j', '?')}, ChromaDB: {components.get('chromadb', '?')}",
                    ]
                    return "\n".join(parts)

                # Attachment: retrieve and summarize email attachment text
                elif action == "attachment":
                    if not item_id:
                        return "Provide an email ID via item_id to retrieve attachment content."
                    # First get attachment list
                    att_url = f"{hermes}/v1/attachments/by-email/{item_id}"
                    async with session.get(att_url, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Could not fetch attachments for email {item_id[:40]}."
                        att_data = await resp.json()
                    atts = att_data.get("attachments", [])
                    if not atts:
                        return "No attachments found for this email."
                    # Try to extract text from the first document attachment
                    doc_atts = [a for a in atts if any(
                        a.get("filename", "").lower().endswith(ext)
                        for ext in (".pdf", ".txt", ".docx", ".csv", ".md")
                    )]
                    if not doc_atts:
                        names = ", ".join(a.get("filename", "?") for a in atts)
                        return f"Attachments found but none are documents: {names}"
                    target = doc_atts[0]
                    idx = target.get("attachment_index", 0)
                    fname = target.get("filename", "attachment")
                    # Extract text via extract-text endpoint
                    ext_url = f"{hermes}/v1/attachments/extract-text/{item_id}/{idx}"
                    async with session.post(ext_url, headers=hermes_headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status != 200:
                            return f"Text extraction failed for '{fname}' (HTTP {resp.status})."
                        ext_data = await resp.json()
                    text_len = ext_data.get("text_length", 0)
                    pages = ext_data.get("pages", 0)
                    preview = ext_data.get("preview", "")
                    parts = [
                        f"Attachment: {fname}",
                        f"Type: {ext_data.get('content_type', '?')}, Pages: {pages}, Text: {text_len:,} chars",
                        f"Indexed for search: {ext_data.get('indexed', False)}",
                        "",
                        "Content preview:",
                        preview,
                    ]
                    if len(atts) > 1:
                        other = [a.get("filename", "?") for a in atts if a != target]
                        parts.append(f"\nOther attachments: {', '.join(other)}")
                    return "\n".join(parts)

                # Status / default: email service status
                else:
                    health, err = await _email_health()
                    if err:
                        return err
                    total = health.get("indexed_emails", {}).get("total", 0)
                    sent = health.get("indexed_emails", {}).get("sent", 0)
                    inbox = health.get("indexed_emails", {}).get("inbox", 0)
                    status_val = health.get("status", "unknown")
                    return f"Email service: {status_val}. {total:,} emails indexed ({inbox:,} inbox, {sent:,} sent)."

            # --- Research Lab ---
            elif studio == "research":
                rtimeout = aiohttp.ClientTimeout(total=10)

                # Detail: full report for a specific session
                if action == "detail" and item_id:
                    url = f"{base}/api/research-lab/session/{item_id}/result"
                    async with session.get(url, timeout=rtimeout) as resp:
                        if resp.status != 200:
                            return f"Could not fetch research session {item_id}."
                        data = await resp.json()
                        report = data.get("report", data.get("result", ""))
                        q = data.get("question", "")
                        rstatus = data.get("status", "")
                        if report:
                            # Truncate for voice — first 500 chars
                            summary = report[:500].strip()
                            if len(report) > 500:
                                summary += "... (truncated for voice, full report available in dashboard)"
                            return f"Research: {q}\nStatus: {rstatus}\n\n{summary}"
                        return f"Research '{q}' — status: {rstatus}. No report yet."

                # Status: aggregate counts by status
                elif action in ("status", "briefing"):
                    url = f"{base}/api/research-lab/sessions"
                    async with session.get(url, params={"limit": 50}, timeout=rtimeout) as resp:
                        if resp.status != 200:
                            return f"Research Lab API returned HTTP {resp.status}."
                        data = await resp.json()
                        sessions_list = data.get("sessions", data) if isinstance(data, dict) else data
                        if not sessions_list:
                            return "No research sessions found."
                        # Count by status
                        counts: dict[str, int] = {}
                        for s in sessions_list:
                            st = s.get("status", "unknown")
                            counts[st] = counts.get(st, 0) + 1
                        total = len(sessions_list)
                        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
                        # Also show latest 3
                        latest = []
                        for s in sessions_list[:3]:
                            q = s.get("question", "Untitled")[:60]
                            st = s.get("status", "?")
                            latest.append(f"- [{st}] {q}")
                        parts = [f"Research Lab: {total} sessions ({breakdown})."]
                        if latest:
                            parts.append("Latest:\n" + "\n".join(latest))
                        return "\n".join(parts)

                # Recent / List / Default: list recent sessions
                else:
                    url = f"{base}/api/research-lab/sessions"
                    params = {"limit": 5}
                    async with session.get(url, params=params, timeout=rtimeout) as resp:
                        if resp.status != 200:
                            return f"Research Lab API returned HTTP {resp.status}."
                        data = await resp.json()
                        sessions_list = data.get("sessions", data) if isinstance(data, dict) else data
                        if not sessions_list:
                            return "No research sessions found."
                        lines = []
                        for s in sessions_list[:5]:
                            q = s.get("question", "Untitled")[:80]
                            rstatus = s.get("status", "unknown")
                            sid = s.get("session_id") or s.get("id", "")
                            lines.append(f"- [{rstatus}] {q} (id: {sid})")
                        return f"{len(sessions_list)} recent research sessions:\n" + "\n".join(lines)

            # --- Podcast Studio ---
            elif studio == "podcast":
                ptimeout = aiohttp.ClientTimeout(total=10)

                # Detail: single project with sources and episodes
                if action == "detail" and item_id:
                    url = f"{base}/api/podcast-studio/projects"
                    params = {"id": item_id}
                    async with session.get(url, params=params, timeout=ptimeout) as resp:
                        if resp.status != 200:
                            return f"Could not fetch podcast project {item_id}."
                        data = await resp.json()
                        title = data.get("title", "Untitled")
                        pstatus = data.get("status", "unknown")
                        sources = data.get("metadata", {}).get("sourceCount", 0)
                        materials = data.get("researchMaterials", [])
                        desc = data.get("description", "")[:200]
                        parts = [f"Podcast: {title}", f"Status: {pstatus}", f"Sources: {sources}"]
                        if desc:
                            parts.append(f"Description: {desc}")
                        if materials:
                            parts.append(f"Materials: {len(materials)}")
                        # Check for episodes
                        try:
                            ep_url = f"{base}/api/podcast-studio/episodes"
                            async with session.get(ep_url, params={"projectId": item_id}, timeout=ptimeout) as eresp:
                                if eresp.status == 200:
                                    edata = await eresp.json()
                                    eps = edata.get("episodes", [])
                                    if eps:
                                        total_dur = sum(e.get("duration", 0) for e in eps)
                                        mins = int(total_dur // 60)
                                        parts.append(f"Episodes: {len(eps)} ({mins} min total audio)")
                        except Exception:
                            pass
                        return "\n".join(parts)

                # Status: aggregate project + episode counts
                elif action in ("status", "briefing"):
                    url = f"{base}/api/podcast-studio/projects"
                    async with session.get(url, timeout=ptimeout) as resp:
                        if resp.status != 200:
                            return f"Podcast Studio API returned HTTP {resp.status}."
                        projects = await resp.json()
                        if not isinstance(projects, list):
                            projects = []
                        # Count by status
                        counts: dict[str, int] = {}
                        for p in projects:
                            st = p.get("status", "unknown")
                            counts[st] = counts.get(st, 0) + 1
                        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
                        # Get episode totals
                        ep_info = ""
                        try:
                            ep_url = f"{base}/api/podcast-studio/episodes"
                            async with session.get(ep_url, timeout=ptimeout) as eresp:
                                if eresp.status == 200:
                                    edata = await eresp.json()
                                    eps = edata.get("episodes", [])
                                    total_dur = sum(e.get("duration", 0) for e in eps)
                                    hrs = int(total_dur // 3600)
                                    mins = int((total_dur % 3600) // 60)
                                    ep_info = f" {len(eps)} episodes ({hrs}h {mins}m total audio)."
                        except Exception:
                            pass
                        return f"Podcast Studio: {len(projects)} projects ({breakdown}).{ep_info}"

                # Recent: latest episodes across all projects
                elif action == "recent":
                    ep_url = f"{base}/api/podcast-studio/episodes"
                    async with session.get(ep_url, timeout=ptimeout) as eresp:
                        if eresp.status != 200:
                            return f"Podcast episodes API returned HTTP {eresp.status}."
                        edata = await eresp.json()
                        eps = edata.get("episodes", [])
                        if not eps:
                            return "No podcast episodes found."
                        lines = []
                        for e in eps[:8]:
                            title = e.get("title", "Episode")
                            dur = e.get("durationFormatted", "?")
                            provider = e.get("ttsProvider", "")
                            date = e.get("createdAtFormatted", "")
                            pstatus = e.get("status", "")
                            lines.append(f"- [{pstatus}] {title} ({dur}) — {provider} {date}")
                        return f"{len(eps)} episodes:\n" + "\n".join(lines)

                # List / Default: list projects
                else:
                    url = f"{base}/api/podcast-studio/projects"
                    async with session.get(url, timeout=ptimeout) as resp:
                        if resp.status != 200:
                            return f"Podcast Studio API returned HTTP {resp.status}."
                        projects = await resp.json()
                        if not projects:
                            return "No podcast projects found."
                        lines = []
                        for p in (projects[:5] if isinstance(projects, list) else []):
                            title = p.get("title") or p.get("name", "Untitled")
                            pstatus = p.get("status", "unknown")
                            sources = p.get("metadata", {}).get("sourceCount", 0)
                            pid = p.get("id", "")
                            lines.append(f"- [{pstatus}] {title} ({sources} sources, id: {pid})")
                        return f"{len(projects)} podcast projects:\n" + "\n".join(lines)

            # --- News Studio ---
            elif studio == "news":
                ntimeout = aiohttp.ClientTimeout(total=10)

                # Detail: full story by ID
                if action == "detail" and item_id:
                    url = f"{base}/api/news/stories/{item_id}"
                    async with session.get(url, timeout=ntimeout) as resp:
                        if resp.status != 200:
                            return f"Could not fetch story {item_id} (HTTP {resp.status})."
                        data = await resp.json()
                        story = data.get("story", data)
                        title = story.get("title", "Untitled")
                        headline = story.get("headline", "")
                        summary = story.get("summary", "")
                        cat = story.get("category", "")
                        wc = story.get("word_count", 0)
                        reading = story.get("reading_time_minutes", 0)
                        nstatus = story.get("status", "")
                        audio = story.get("audio_url", "")
                        published = str(story.get("published_at", ""))[:10]
                        parts = [f"{title}"]
                        if headline and headline != title:
                            parts.append(headline)
                        parts.append(f"Category: {cat} | Status: {nstatus} | {wc} words ({reading} min read)")
                        if audio:
                            dur = story.get("audio_duration_seconds", 0)
                            parts.append(f"Audio: {int(dur // 60)}:{int(dur % 60):02d}")
                        if published:
                            parts.append(f"Published: {published}")
                        if summary:
                            trunc = summary[:400].strip()
                            if len(summary) > 400:
                                trunc += "..."
                            parts.append(f"\n{trunc}")
                        return "\n".join(parts)

                # Status: aggregate stats
                elif action in ("status", "briefing"):
                    url = f"{base}/api/news/stories/stats"
                    async with session.get(url, timeout=ntimeout) as resp:
                        if resp.status != 200:
                            return f"News stats API returned HTTP {resp.status}."
                        data = await resp.json()
                        stats = data.get("stats", {})
                        totals = stats.get("totals", {})
                        audio = stats.get("audio", {})
                        content = stats.get("content", {})
                        cats = stats.get("categories", [])
                        by_status = totals.get("by_status", {})
                        status_str = ", ".join(f"{k}: {v}" for k, v in by_status.items())
                        parts = [
                            f"News Studio: {totals.get('all_stories', 0)} total stories ({status_str}).",
                            f"Audio: {audio.get('stories_with_audio', 0)} with audio, {audio.get('total_duration_formatted', '?')} total listening time.",
                            f"Content: avg {content.get('average_word_count', 0)} words, {content.get('total_words', 0):,} total words.",
                        ]
                        if cats:
                            cat_str = ", ".join(f"{c['category']}: {c['count']}" for c in cats[:6])
                            parts.append(f"Categories: {cat_str}")
                        activity = stats.get("recent_activity", [])
                        if activity:
                            recent_days = ", ".join(
                                f"{a['date']}: {a['stories_created']}" for a in activity[:3]
                            )
                            parts.append(f"Recent: {recent_days}")
                        return "\n".join(parts)

                # Recent: latest stories with more detail
                elif action == "recent":
                    url = f"{base}/api/news/stories"
                    params: dict[str, Any] = {"limit": 5, "status": "all"}
                    if query:
                        params["category"] = query
                    async with session.get(url, params=params, timeout=ntimeout) as resp:
                        if resp.status != 200:
                            return f"News API returned HTTP {resp.status}."
                        data = await resp.json()
                        stories = data.get("stories", [])
                        total = data.get("pagination", {}).get("total", len(stories))
                        if not stories:
                            return "No news stories found."
                        lines = []
                        for s in stories[:5]:
                            title = s.get("title", "Untitled")[:70]
                            nstatus = s.get("status", "")
                            cat = s.get("category", "")
                            wc = s.get("word_count", 0)
                            has_audio = "🔊" if s.get("audio_url") else ""
                            sid = s.get("id", "")
                            lines.append(f"- [{nstatus}] {title} ({cat}, {wc}w) {has_audio} (id: {sid})")
                        return f"{total} stories total, showing latest {len(stories)}:\n" + "\n".join(lines)

                # List / Default: simple list
                else:
                    url = f"{base}/api/news/stories"
                    params = {"limit": 5, "status": "published"}
                    async with session.get(url, params=params, timeout=ntimeout) as resp:
                        if resp.status != 200:
                            return f"News Studio API returned HTTP {resp.status}."
                        data = await resp.json()
                        stories = data.get("stories", [])
                        if not stories:
                            return "No published news stories found."
                        lines = []
                        for s in stories[:5]:
                            title = s.get("title", "Untitled")[:80]
                            nstatus = s.get("status", "")
                            sid = s.get("id", "")
                            lines.append(f"- [{nstatus}] {title} (id: {sid})")
                        return f"{len(stories)} published stories:\n" + "\n".join(lines)

            # --- Image Studio (direct DB via psql — API requires session auth only) ---
            elif studio == "image":
                try:
                    # Detail: single job by ID
                    if action == "detail" and item_id:
                        sql = (
                            f"SELECT id, status, LEFT(prompt,120) as prompt, model, "
                            f"width, height, result_url, error_message, "
                            f"generation_time_ms, created_at, completed_at "
                            f"FROM image_generation_jobs WHERE id = '{item_id}'"
                        )
                        proc = await asyncio.create_subprocess_exec(
                            "psql", "-d", "ecosystem_unified", "-t", "-A", "-F", "|",
                            "-c", sql,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                        output = stdout.decode().strip()
                        if not output:
                            return f"Image job {item_id} not found."
                        parts_row = output.split("|")
                        if len(parts_row) >= 9:
                            jid, jstatus, prompt, model = parts_row[0].strip(), parts_row[1].strip(), parts_row[2].strip(), parts_row[3].strip()
                            w, h = parts_row[4].strip(), parts_row[5].strip()
                            result_url = parts_row[6].strip()
                            err_msg = parts_row[7].strip()
                            gen_time = parts_row[8].strip()
                            info = [f"Image Job: {jid}", f"Status: {jstatus}", f"Prompt: {prompt}", f"Model: {model}", f"Size: {w}x{h}"]
                            if gen_time:
                                info.append(f"Generation time: {int(int(gen_time) / 1000)}s")
                            if err_msg:
                                info.append(f"Error: {err_msg}")
                            if result_url:
                                info.append(f"Result: {result_url}")
                            return "\n".join(info)
                        return f"Image job {item_id}: {output}"

                    # Status: aggregate counts by status
                    elif action in ("status", "briefing"):
                        sql = (
                            "SELECT status, COUNT(*) as cnt "
                            "FROM image_generation_jobs GROUP BY status ORDER BY cnt DESC"
                        )
                        proc = await asyncio.create_subprocess_exec(
                            "psql", "-d", "ecosystem_unified", "-t", "-A", "-F", "|",
                            "-c", sql,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                        output = stdout.decode().strip()
                        if not output:
                            return "No image generation jobs found."
                        counts = []
                        total = 0
                        for row in output.split("\n"):
                            rparts = row.split("|")
                            if len(rparts) >= 2:
                                st, cnt = rparts[0].strip(), int(rparts[1].strip())
                                counts.append(f"{st}: {cnt}")
                                total += cnt
                        return f"Image Studio: {total} jobs ({', '.join(counts)})."

                    # Recent / List / Default: latest jobs
                    else:
                        sql = (
                            "SELECT id, status, LEFT(prompt,60) as prompt, model, created_at "
                            "FROM image_generation_jobs ORDER BY created_at DESC LIMIT 5"
                        )
                        proc = await asyncio.create_subprocess_exec(
                            "psql", "-d", "ecosystem_unified", "-t", "-A", "-F", "|",
                            "-c", sql,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                        output = stdout.decode().strip()
                        if not output:
                            return "No image generation jobs found."
                        lines = []
                        for row in output.split("\n"):
                            rparts = row.split("|")
                            if len(rparts) >= 5:
                                jid, jstatus, prompt, model, created = rparts[0].strip(), rparts[1].strip(), rparts[2].strip(), rparts[3].strip(), rparts[4].strip()[:10]
                                lines.append(f"- [{jstatus}] {prompt} ({model}, {created}) (id: {jid})")
                            elif len(rparts) >= 3:
                                jstatus, prompt, model = rparts[0].strip(), rparts[1].strip(), rparts[2].strip()
                                lines.append(f"- [{jstatus}] {prompt} ({model})")
                        return f"{len(lines)} recent image jobs:\n" + "\n".join(lines)
                except Exception as e:
                    logger.error(f"Image studio DB error: {e}")
                    return f"Could not query image jobs: {str(e)}"

            # --- Workspace (POST with user_id) ---
            elif studio == "workspace":
                timeout = aiohttp.ClientTimeout(total=12)
                ws_headers = {
                    "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
                    "X-User-Id": ECOSYSTEM_USER_ID,
                }

                # Helper: fetch workspace list (reused by multiple actions)
                async def _fetch_workspaces():
                    url = f"{base}/api/workspace/list"
                    body = {"include_shared": True}
                    async with session.post(url, json=body, headers=ws_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return None, f"Workspace API returned HTTP {resp.status}."
                        data = await resp.json()
                        return data.get("workspaces", []), None

                # Detail: show a specific workspace's page/database counts + recent pages
                if action == "detail" and item_id:
                    # Fetch pages for this workspace
                    pages_url = f"{base}/api/workspace/pages"
                    async with session.get(pages_url, params={"workspace_id": item_id, "limit": "10"}, headers=ws_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Workspace detail API returned HTTP {resp.status}."
                        data = await resp.json()
                        pages = data.get("pages", [])
                        total = data.get("total", len(pages))
                        if not pages:
                            return f"Workspace {item_id}: no pages found."
                        lines = []
                        for p in pages[:10]:
                            title = p.get("properties", {}).get("title", [{}])[0].get("text", {}).get("content", "Untitled")
                            ptype = p.get("type", "page")
                            updated = str(p.get("updated_at", ""))[:10]
                            lines.append(f"- {title} ({ptype}, updated {updated})")
                        return f"Workspace has {total} pages:\n" + "\n".join(lines)

                # Status: list all workspaces with page counts
                elif action in ("status", "list"):
                    workspaces, err = await _fetch_workspaces()
                    if err:
                        return err
                    if not workspaces:
                        return "No workspaces found."
                    lines = []
                    for w in workspaces[:8]:
                        wid = w.get("id", "")
                        name = w.get("name") or w.get("title", "Untitled")
                        icon = w.get("settings", {}).get("icon", "")
                        shared = " (shared)" if w.get("is_shared") else ""
                        # Fetch page count per workspace
                        count_str = ""
                        try:
                            pages_url = f"{base}/api/workspace/pages"
                            async with session.get(pages_url, params={"workspace_id": wid, "limit": "1"}, headers=ws_headers, timeout=timeout) as presp:
                                if presp.status == 200:
                                    pdata = await presp.json()
                                    count_str = f" — {pdata.get('total', '?')} pages"
                        except Exception:
                            pass
                        lines.append(f"- {icon} {name}{shared} (id: {wid}){count_str}")
                    return f"{len(workspaces)} workspaces:\n" + "\n".join(lines)

                # Recent: latest pages across all workspaces
                elif action == "recent":
                    workspaces, err = await _fetch_workspaces()
                    if err:
                        return err
                    if not workspaces:
                        return "No workspaces found."
                    all_pages = []
                    for w in workspaces[:5]:
                        wid = w.get("id", "")
                        wname = w.get("name") or "Untitled"
                        try:
                            pages_url = f"{base}/api/workspace/pages"
                            async with session.get(pages_url, params={"workspace_id": wid, "limit": "5"}, headers=ws_headers, timeout=timeout) as presp:
                                if presp.status == 200:
                                    pdata = await presp.json()
                                    for p in pdata.get("pages", []):
                                        p["_workspace_name"] = wname
                                        all_pages.append(p)
                        except Exception:
                            pass
                    if not all_pages:
                        return "No pages found in any workspace."
                    # Sort by updated_at descending
                    all_pages.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
                    lines = []
                    for p in all_pages[:10]:
                        title = p.get("properties", {}).get("title", [{}])[0].get("text", {}).get("content", "Untitled")
                        ws = p.get("_workspace_name", "")
                        updated = str(p.get("updated_at", ""))[:10]
                        lines.append(f"- {title} [{ws}] (updated {updated})")
                    return f"{len(all_pages)} recent pages:\n" + "\n".join(lines)

                # Default: simple list (backward compatible)
                else:
                    workspaces, err = await _fetch_workspaces()
                    if err:
                        return err
                    if not workspaces:
                        return "No workspaces found."
                    lines = []
                    for w in workspaces[:8]:
                        wid = w.get("id", "")
                        name = w.get("name") or w.get("title", "Untitled")
                        icon = w.get("settings", {}).get("icon", "")
                        shared = " (shared)" if w.get("is_shared") else ""
                        lines.append(f"- {icon} {name}{shared} (id: {wid})")
                    return f"{len(workspaces)} workspaces:\n" + "\n".join(lines)

            else:
                return f"Unknown studio: {studio}. Available: calendar, email, research, podcast, news, image, workspace."

    except asyncio.TimeoutError:
        return f"{studio} studio timed out."
    except Exception as e:
        logger.error(f"check_studio error ({studio}): {e}")
        return f"Could not reach {studio} studio: {str(e)}"


# ---------------------------------------------------------------------------
# Skill Discovery handler (queries OpenClaw Skill Discovery API)
# ---------------------------------------------------------------------------

_skill_cache: dict[str, Any] = {}  # {"data": ..., "fetched_at": float}
_SKILL_CACHE_TTL = 300  # 5 minutes


async def handle_discover_skills(skill_name: str = "") -> str:
    """Query the Skill Discovery API and return a voice-friendly summary."""
    import time

    now = time.time()
    cache_valid = (
        _skill_cache.get("data")
        and (now - _skill_cache.get("fetched_at", 0)) < _SKILL_CACHE_TTL
    )

    try:
        if skill_name:
            # Fetch details for a specific skill
            url = f"{SKILL_DISCOVERY_URL}/api/v1/skills/{skill_name}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 404:
                        return f"Skill '{skill_name}' not found. Use discover_skills without a name to see all available skills."
                    data = await resp.json()

            lines = [f"Skill: {data.get('name', skill_name)}"]
            if data.get("description"):
                lines.append(f"Description: {data['description']}")
            if data.get("required_inputs"):
                inputs = data["required_inputs"]
                for action, fields in inputs.items():
                    if isinstance(fields, dict):
                        field_list = ", ".join(f"{k}" for k in fields.keys())
                    elif isinstance(fields, list):
                        field_list = ", ".join(str(f) for f in fields)
                    else:
                        field_list = str(fields)
                    lines.append(f"Inputs for '{action}': {field_list}")
            if data.get("gather_requirements"):
                lines.append(f"Before using: {data['gather_requirements'][:200]}")
            if data.get("triggers"):
                lines.append(f"Trigger phrases: {', '.join(data['triggers'][:5])}")
            return "\n".join(lines)

        # Fetch full catalog (use cache if valid)
        if cache_valid:
            catalog = _skill_cache["data"]
        else:
            url = f"{SKILL_DISCOVERY_URL}/api/v1/skills"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    catalog = await resp.json()
            _skill_cache["data"] = catalog
            _skill_cache["fetched_at"] = now

        active = catalog.get("active", [])
        available = catalog.get("available", [])

        lines = [f"{len(active)} active skills, {len(available)} available in the pool."]

        if active:
            lines.append("\nActive skills:")
            for s in active:
                desc = s.get("description", "")[:80]
                lines.append(f"- {s['name']}: {desc}")

        if available:
            lines.append(f"\nAvailable skills ({len(available)} in managed pool):")
            for s in available[:15]:
                desc = s.get("description", "")[:80]
                lines.append(f"- {s['name']}: {desc}")
            if len(available) > 15:
                lines.append(f"  ...and {len(available) - 15} more.")

        lines.append("\nUse discover_skills with a skill_name to get details and required inputs for a specific skill.")
        return "\n".join(lines)

    except asyncio.TimeoutError:
        return "Skill Discovery service timed out. It may not be running."
    except Exception as e:
        logger.error(f"discover_skills error: {e}")
        return f"Could not reach Skill Discovery service: {str(e)}"


# ---------------------------------------------------------------------------
# Network diagnostics handler
# ---------------------------------------------------------------------------

async def handle_diagnose_network(check: str = "full", target: str = "", port: int = 0) -> str:
    """Call homelab-netdiag API and return a voice-friendly summary."""
    try:
        async with aiohttp.ClientSession() as session:
            if check == "full":
                params = {}
                if target:
                    params["target"] = target
                url = f"{NETDIAG_URL}/api/diagnose"
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return "Could not reach the diagnostic service."
                    data = await resp.json()
                    s = data.get("summary", {})
                    checks = data.get("checks", {})

                    if s.get("status") == "healthy" and not s.get("issues"):
                        # Concise happy path for voice
                        gw_ms = checks.get("gateway", {}).get("latency_ms", "?")
                        inet_ms = checks.get("internet", {}).get("latency_ms", "?")
                        dk = checks.get("docker", {})
                        ts = checks.get("tailscale", {})
                        return (
                            f"Network is healthy. Gateway {gw_ms}ms, internet {inet_ms}ms. "
                            f"{dk.get('running', 0)} Docker containers running. "
                            f"{ts.get('online_peers', 0)} Tailscale peers online."
                        )

                    # Degraded — report issues
                    issues = s.get("issues", [])
                    parts = [f"Network is degraded. {len(issues)} issue{'s' if len(issues) != 1 else ''} found."]
                    for issue in issues[:5]:
                        parts.append(issue)
                    return " ".join(parts)

            elif check == "ping":
                if not target:
                    return "I need a hostname or IP to ping."
                url = f"{NETDIAG_URL}/api/ping"
                async with session.get(url, params={"target": target, "count": 3}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    if data.get("reachable"):
                        return f"{target} is reachable. Latency: {data.get('rtt_avg_ms', '?')}ms, {data.get('packet_loss_pct', 0)}% loss."
                    return f"{target} is unreachable. {data.get('error', '')}"

            elif check == "dns":
                if not target:
                    return "I need a hostname to look up."
                url = f"{NETDIAG_URL}/api/dns"
                async with session.get(url, params={"hostname": target}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("resolved"):
                        records = ", ".join(data.get("records", [])[:3])
                        return f"{target} resolves to {records}. Query time: {data.get('query_time_ms', '?')}ms."
                    return f"{target} did not resolve. DNS may be down."

            elif check == "port":
                if not target or not port:
                    return "I need a host and port number to check."
                url = f"{NETDIAG_URL}/api/port-check"
                async with session.get(url, params={"host": target, "port": port}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    status = "open" if data.get("open") else "closed"
                    return f"Port {port} on {target} is {status}."

            elif check == "tailscale":
                url = f"{NETDIAG_URL}/api/tailscale"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    online = data.get("online_count", 0)
                    total = data.get("peer_count", 0)
                    peers = data.get("peers", [])
                    offline = [p["hostname"] for p in peers if not p.get("online")]
                    result = f"Tailscale: {online} of {total} peers online."
                    if offline:
                        result += f" Offline: {', '.join(offline[:5])}."
                    return result

            elif check == "docker":
                url = f"{NETDIAG_URL}/api/docker"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    running = data.get("running", 0)
                    stopped = data.get("stopped", 0)
                    unhealthy = data.get("unhealthy", [])
                    result = f"{running} containers running, {stopped} stopped."
                    if unhealthy:
                        result += f" Unhealthy: {', '.join(unhealthy[:5])}."
                    return result

            elif check == "services":
                url = f"{NETDIAG_URL}/api/services"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    healthy = data.get("healthy", 0)
                    total = data.get("total", 0)
                    services = data.get("services", {})
                    down = [n for n, info in services.items() if not info.get("healthy")]
                    result = f"{healthy} of {total} homelab services healthy."
                    if down:
                        result += f" Down: {', '.join(down)}."
                    return result

            else:
                return f"Unknown check type: {check}. Use full, ping, dns, port, tailscale, docker, or services."

    except asyncio.TimeoutError:
        return "Network diagnostic timed out. The diagnostic service may be overloaded."
    except Exception as e:
        logger.error(f"diagnose_network error: {e}")
        return f"Diagnostic error: {str(e)}"


# ---------------------------------------------------------------------------
# Time handler
# ---------------------------------------------------------------------------

# Default fallback timezone - will be overridden by user's location if available
DEFAULT_TZ = ZoneInfo("America/Chicago")

# Map of US zip codes to timezones (simplified - major metro areas)
ZIP_TIMEZONE_MAP = {
    # Texas - Central
    "77": "America/Chicago",  # Houston area (77346 = Humble)
    "78": "America/Chicago",  # San Antonio
    "79": "America/Chicago",  # Amarillo
    "75": "America/Chicago",  # Dallas area
    "76": "America/Chicago",  # Fort Worth area
    # California - Pacific
    "90": "America/Los_Angeles",
    "91": "America/Los_Angeles",
    "92": "America/Los_Angeles",
    "93": "America/Los_Angeles",
    "94": "America/Los_Angeles",
    "95": "America/Los_Angeles",
    # New York - Eastern
    "10": "America/New_York",
    # ... add more as needed
}


def _get_user_timezone() -> ZoneInfo:
    """Determine user's timezone from location context if available."""
    global _current_user_location
    
    # Try to get timezone from location context
    location = getattr(_current_user_location, 'location_data', None)
    if location:
        # Check if we have a timezone override from location service
        tz_name = location.get('timezone')
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except Exception:
                pass
        
        # Try to infer from ZIP code prefix
        zip_code = location.get('zip_code') or location.get('postal_code', '')
        if zip_code and len(zip_code) >= 2:
            zip_prefix = zip_code[:2]
            tz_name = ZIP_TIMEZONE_MAP.get(zip_prefix)
            if tz_name:
                try:
                    return ZoneInfo(tz_name)
                except Exception:
                    pass
    
    # Fallback to default
    return DEFAULT_TZ


async def handle_get_time() -> str:
    """Return current date/time in user's timezone."""
    # Get UTC time and convert to user's timezone
    user_tz = _get_user_timezone()
    now = datetime.now(timezone.utc).astimezone(user_tz)
    return now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")


# ---------------------------------------------------------------------------
# Timer handler (in-memory timers with push notification on fire)
# ---------------------------------------------------------------------------

_active_timers: dict[str, dict[str, Any]] = {}  # id -> {label, fire_at, task}
_timer_counter = 0


async def _fire_timer(timer_id: str, label: str, delay: float):
    """Background task: wait then send push notification when timer fires."""
    from nova.push import send_push
    try:
        await asyncio.sleep(delay)
        msg = f"Timer done: {label}" if label else "Your timer is done!"
        logger.info(f"Timer {timer_id} fired: {msg}")

        # Send push notification
        try:
            await send_push(
                user_id=_current_user_id or "default",
                title="Timer",
                body=msg,
                data={"type": "timer_fired", "timer_id": timer_id, "label": label},
            )
        except Exception as e:
            logger.warning(f"Could not send timer push: {e}")

        # Also try server message if connected
        if _server_msg_fn:
            try:
                await _server_msg_fn({"type": "timer_fired", "timer_id": timer_id, "label": label})
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        _active_timers.pop(timer_id, None)


async def handle_manage_timer(
    action: str, duration_minutes: float = 0, label: str = "", timer_id: str = ""
) -> str:
    """Set, list, or cancel timers."""
    global _timer_counter

    if action == "set":
        if duration_minutes <= 0:
            return "I need a positive duration. How many minutes?"
        _timer_counter += 1
        tid = f"timer-{_timer_counter}"
        delay_sec = duration_minutes * 60
        fire_at = datetime.now(timezone.utc).astimezone(_get_user_timezone()) + timedelta(seconds=delay_sec)
        task = asyncio.create_task(_fire_timer(tid, label, delay_sec))
        _active_timers[tid] = {
            "label": label or f"{duration_minutes} minute timer",
            "fire_at": fire_at,
            "task": task,
            "duration_minutes": duration_minutes,
        }
        fire_str = fire_at.strftime("%-I:%M %p")
        lbl = f" for {label}" if label else ""
        if duration_minutes >= 60:
            hrs = int(duration_minutes // 60)
            mins = int(duration_minutes % 60)
            dur_str = f"{hrs} hour{'s' if hrs != 1 else ''}"
            if mins:
                dur_str += f" {mins} minute{'s' if mins != 1 else ''}"
        else:
            dur_str = f"{int(duration_minutes)} minute{'s' if duration_minutes != 1 else ''}"
        return f"Timer set{lbl}: {dur_str}. I'll notify you at {fire_str}."

    elif action == "list":
        if not _active_timers:
            return "No active timers."
        now = datetime.now(timezone.utc).astimezone(_get_user_timezone())
        lines = []
        for tid, info in _active_timers.items():
            remaining = (info["fire_at"] - now).total_seconds()
            if remaining <= 0:
                lines.append(f"{tid}: {info['label']} — firing now")
            elif remaining < 60:
                lines.append(f"{tid}: {info['label']} — {int(remaining)} seconds left")
            else:
                mins_left = int(remaining / 60)
                lines.append(f"{tid}: {info['label']} — {mins_left} minute{'s' if mins_left != 1 else ''} left")
        return f"{len(_active_timers)} active timer{'s' if len(_active_timers) != 1 else ''}. " + ". ".join(lines)

    elif action == "cancel":
        if timer_id and timer_id in _active_timers:
            _active_timers[timer_id]["task"].cancel()
            info = _active_timers.pop(timer_id)
            return f"Cancelled timer: {info['label']}."
        elif not timer_id and len(_active_timers) == 1:
            # Cancel the only active timer
            tid, info = next(iter(_active_timers.items()))
            info["task"].cancel()
            _active_timers.pop(tid)
            return f"Cancelled timer: {info['label']}."
        elif not timer_id and len(_active_timers) > 1:
            return f"You have {len(_active_timers)} timers. Which one? Use 'list timers' to see IDs."
        else:
            return f"Timer '{timer_id}' not found. Use 'list timers' to see active timers."

    return f"Unknown timer action: {action}. Use set, list, or cancel."


# ---------------------------------------------------------------------------
# Ticket management (homelab issue tracking)
# ---------------------------------------------------------------------------

async def handle_manage_ticket(
    action: str,
    ticket_id: str = "",
    title: str = "",
    description: str = "",
    priority: str = "medium",
    severity: str = "minor",
    category: str = "bug",
    component: str = "",
    tags: str = "",
    source_context: str = "",
    status: str = "",
    assigned_to: str = "",
    delegate_to: str = "openclaw",
    analysis: str = "",
    proposed_fix: str = "",
    resolution: str = "",
    affected_files: str = "",
    limit: str = "10",
) -> str:
    """Dispatch ticket management actions."""
    from nova.tickets import (
        handle_create_ticket,
        handle_list_tickets,
        handle_get_ticket,
        handle_update_ticket,
        handle_delegate_ticket,
    )

    if action == "create":
        if not title:
            return "A title is required to create a ticket."
        return await handle_create_ticket(
            title=title, description=description, priority=priority,
            severity=severity, category=category, component=component,
            tags=tags, source_context=source_context, assigned_to=assigned_to,
        )
    elif action == "list":
        return await handle_list_tickets(
            status=status, priority=priority, assigned_to=assigned_to, limit=limit,
        )
    elif action == "get":
        if not ticket_id:
            return "A ticket_id is required for the 'get' action."
        return await handle_get_ticket(ticket_id=ticket_id)
    elif action == "update":
        if not ticket_id:
            return "A ticket_id is required for the 'update' action."
        return await handle_update_ticket(
            ticket_id=ticket_id, status=status, priority=priority,
            assigned_to=assigned_to, analysis=analysis,
            proposed_fix=proposed_fix, resolution=resolution,
            affected_files=affected_files,
        )
    elif action == "delegate":
        if not ticket_id:
            return "A ticket_id is required for the 'delegate' action."
        return await handle_delegate_ticket(
            ticket_id=ticket_id, delegate_to=delegate_to,
        )
    else:
        return f"Unknown ticket action: {action}. Use create, list, get, update, or delegate."


# ---------------------------------------------------------------------------
# Workspace management (fast direct API calls)
# ---------------------------------------------------------------------------

async def handle_manage_workspace(
    action: str,
    page_id: str = "",
    purpose: str = "",
    description: str = "",
    user_id: str = "",
    role: str = "viewer",
    permission_id: str = "",
) -> str:
    """Handle workspace management operations via dashboard API."""
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": ECOSYSTEM_API_KEY,
    }

    try:
        async with aiohttp.ClientSession() as session:
            if action == "generate_template":
                if not purpose:
                    return "Please specify a purpose (e.g. 'meeting notes', 'project plan', 'bug report')."
                url = f"{ECOSYSTEM_URL}/api/ai/smart-templates"
                payload = {"action": "generate-template", "purpose": purpose}
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        block_count = len(data.get("blocks", []))
                        return f"Generated '{data.get('title')}' template with {block_count} blocks. {data.get('description', '')}"
                    return f"Template generation failed: {resp.status}"

            elif action == "infer_schema":
                if not description:
                    return "Please describe what the database should track (e.g. 'bug tracker', 'inventory')."
                url = f"{ECOSYSTEM_URL}/api/ai/smart-templates"
                payload = {"action": "infer-schema", "description": description}
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        schema = data.get("schema", {})
                        cols = [f"{v['name']} ({v['type']})" for v in schema.values()]
                        return f"Suggested schema with {len(cols)} columns: {', '.join(cols)}"
                    return f"Schema inference failed: {resp.status}"

            elif action == "grant_permission":
                if not page_id or not user_id:
                    return "page_id and user_id are required to grant permission."
                url = f"{ECOSYSTEM_URL}/api/pages/{page_id}/permissions"
                payload = {"userId": user_id, "role": role, "grantedBy": ECOSYSTEM_USER_ID}
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status in (200, 201):
                        return f"Granted {role} access to user {user_id} on page {page_id}."
                    return f"Permission grant failed: {resp.status}"

            elif action == "revoke_permission":
                if not page_id or not permission_id:
                    return "page_id and permission_id are required to revoke permission."
                url = f"{ECOSYSTEM_URL}/api/pages/{page_id}/permissions/{permission_id}"
                async with session.delete(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return f"Revoked permission {permission_id} from page {page_id}."
                    return f"Permission revoke failed: {resp.status}"

            elif action == "create_share_link":
                if not page_id:
                    return "page_id is required to create a share link."
                url = f"{ECOSYSTEM_URL}/api/pages/{page_id}/share"
                payload = {"role": role, "createdBy": ECOSYSTEM_USER_ID}
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        token = data.get("token", "")
                        return f"Created {role} share link for page {page_id}. Token: {token}"
                    return f"Share link creation failed: {resp.status}"

            elif action == "list_permissions":
                if not page_id:
                    return "page_id is required to list permissions."
                url = f"{ECOSYSTEM_URL}/api/pages/{page_id}/permissions"
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        perms = data.get("permissions", [])
                        if not perms:
                            return f"No permissions set on page {page_id}."
                        lines = [f"- {p['user_id']}: {p['role']}" for p in perms]
                        return f"Permissions on page {page_id}:\n" + "\n".join(lines)
                    return f"Permission list failed: {resp.status}"

            elif action == "list_share_links":
                if not page_id:
                    return "page_id is required to list share links."
                url = f"{ECOSYSTEM_URL}/api/pages/{page_id}/share"
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        links = data.get("links", [])
                        if not links:
                            return f"No active share links on page {page_id}."
                        lines = [f"- {l['token'][:8]}... ({l['role']}, {l['accessCount']} views)" for l in links]
                        return f"Share links on page {page_id}:\n" + "\n".join(lines)
                    return f"Share link list failed: {resp.status}"

            else:
                return f"Unknown workspace action: {action}. Use generate_template, infer_schema, grant_permission, revoke_permission, create_share_link, list_permissions, or list_share_links."

    except asyncio.TimeoutError:
        return "Workspace API timed out. Try again."
    except Exception as e:
        logger.error(f"Workspace management error: {e}")
        return f"Workspace error: {str(e)}"


# ---------------------------------------------------------------------------
# YouTube search and play (Tesla dashboard integration)
# ---------------------------------------------------------------------------

async def handle_youtube(action: str, query: str = "", video_id: str = "") -> str:
    """Search YouTube and trigger video playback on Tesla dashboard."""
    try:
        async with aiohttp.ClientSession() as session:
            if action == "search":
                if not query:
                    return "What would you like to search for on YouTube?"
                
                # Use dashboard YouTube search API
                url = f"{ECOSYSTEM_URL}/api/youtube/search?q={query.replace(' ', '+')}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return f"YouTube search failed: {resp.status}"
                    data = await resp.json()
                    videos = data.get("videos", [])
                    if not videos:
                        return f"No videos found for '{query}'."
                    
                    # Return top 5 results with IDs for play action
                    lines = [f"Found {len(videos)} videos for '{query}':"]
                    for i, v in enumerate(videos[:5], 1):
                        lines.append(f"{i}. {v['title']} (ID: {v['id']})")
                    lines.append("\nSay 'play the first one' or give me a video ID to start playing.")
                    return "\n".join(lines)
            
            elif action == "play":
                if not video_id:
                    return "Which video should I play? Give me a video ID from the search results."
                
                # Trigger play via Nova mirror event (Tesla dashboard subscribes to this)
                from nova.mirror import publish_event
                await publish_event({
                    "type": "youtube_play",
                    "video_id": video_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return f"Playing video {video_id} on Tesla dashboard."
            
            else:
                return f"Unknown YouTube action: {action}. Use 'search' or 'play'."
    
    except Exception as e:
        logger.error(f"YouTube tool error: {e}")
        return f"YouTube error: {str(e)}"


# ---------------------------------------------------------------------------
# Context Bridge handlers (PIC + KG-API orchestration)
# ---------------------------------------------------------------------------

async def handle_knowledge_query(
    query: str,
    include_personal: bool = True,
    include_knowledge: bool = True,
    include_dimensions: bool = True,
) -> str:
    """Query across PIC and KG-API through Context Bridge."""
    from nova.context_bridge import query_knowledge
    
    result = await query_knowledge(
        query=query,
        include_personal=include_personal,
        include_knowledge=include_knowledge,
        include_dimensions=include_dimensions,
    )
    
    if result.get("error"):
        return f"Knowledge query failed: {result['error']}"
    
    # Format the synthesis for voice
    synthesis = result.get("synthesis", "")
    if synthesis and synthesis != "No context available":
        return synthesis
    
    # Build response from components
    parts = []
    
    if include_personal and result.get("personal"):
        personal = result["personal"]
        if personal.get("identity"):
            parts.append(f"Your identity context is available.")
    
    if include_dimensions and result.get("applicable_dimensions"):
        dims = result["applicable_dimensions"]
        if dims:
            dim_names = ", ".join(d.get("label", d.get("id", "")) for d in dims[:3])
            parts.append(f"Relevant LIAM dimensions: {dim_names}")
    
    if include_knowledge and result.get("knowledge"):
        kg = result["knowledge"]
        if isinstance(kg, dict) and "answer" in kg:
            parts.append(kg["answer"])
    
    if not parts:
        return f"No knowledge found for '{query}'."
    
    return " | ".join(parts)


async def handle_get_enriched_context(
    include_goals: bool = True,
    include_relationships: bool = False,
) -> str:
    """Get enriched personal context from Context Bridge."""
    from nova.context_bridge import get_enriched_context
    
    result = await get_enriched_context(
        agent_id="nova-agent",
        include_goals=include_goals,
        include_relationships=include_relationships,
    )
    
    if result.get("error"):
        return f"Context fetch failed: {result['error']}"
    
    context_prompt = result.get("context_prompt", "")
    if context_prompt and context_prompt != "No context available.":
        return context_prompt
    
    # Build from components
    parts = []
    
    identity = result.get("identity", {})
    if identity and identity.get("profile"):
        profile = identity["profile"]
        parts.append(f"User: {profile.get('name', 'Unknown')}")
    
    goals = result.get("goals", [])
    if include_goals and goals:
        goal_strs = [f"{g.get('title', 'Untitled')} ({g.get('status', 'unknown')})" for g in goals[:3]]
        parts.append(f"Active goals: {', '.join(goal_strs)}")
    
    entities = result.get("relevant_entities", [])
    if entities:
        parts.append(f"Relevant knowledge: {len(entities)} entities linked to your context")
    
    if not parts:
        return "No enriched context available."
    
    return " | ".join(parts)


async def handle_link_goal_to_knowledge(
    goal_id: str,
    entity_id: str,
    relevance: float = 0.5,
    context: str = "",
) -> str:
    """Create bi-directional link between PIC goal and KG-API entity."""
    from nova.context_bridge import link_goal_to_entity
    
    result = await link_goal_to_entity(
        goal_id=goal_id,
        entity_id=entity_id,
        relevance=relevance,
        context=context,
    )
    
    if result.get("error"):
        return f"Link creation failed: {result['error']}"
    
    if result.get("linked"):
        ctx = f" ({context})" if context else ""
        return f"Successfully linked goal {goal_id} to entity {entity_id}{ctx}."
    
    return "Link creation did not complete. The goal or entity may not exist."


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    # Native casual tools
    "get_weather": handle_get_weather,
    "control_lights": handle_control_lights,
    "get_workstation_status": handle_get_workstation_status,
    "set_reminder": handle_set_reminder,
    # Search (fast, grounded)
    "web_search": handle_web_search,
    # Memory tools (PIC — Personal Integration Core)
    "save_memory": handle_save_memory,
    "recall_memory": handle_recall_memory,
    "forget_memory": handle_forget_memory,
    "search_past_conversations": handle_search_past_conversations,
    # Delegated (actions requiring browser/email/calendar/shell)
    "openclaw_delegate": handle_openclaw_delegate,
    # Studio quick-reads (direct dashboard API)
    "check_studio": handle_check_studio,
    # Skill discovery (dynamic skill catalog)
    "discover_skills": handle_discover_skills,
    # Network diagnostics (homelab-netdiag API)
    "diagnose_network": handle_diagnose_network,
    # Time (grounded current date/time)
    "get_time": handle_get_time,
    # Timers & alarms
    "manage_timer": handle_manage_timer,
    # Ticket tracker
    "manage_ticket": handle_manage_ticket,
    # Workspace management (fast direct API)
    "manage_workspace": handle_manage_workspace,
    # Notes & Productivity
    "manage_notes": handle_manage_notes,
    # ExoMind - Long-running tasks and reminders
    "exomind": handle_exomind,
    # Homelab infrastructure operations
    "service_status": handle_service_status,
    "service_logs": handle_service_logs,
    "service_restart": handle_service_restart,
    "service_start": handle_service_start,
    "service_stop": handle_service_stop,
    "service_health_check": handle_service_health_check,
    # Unified homelab_operations (per /claude-skills spec)
    "homelab_operations": handle_homelab_operations,
    # EV Charging & Route Planning (NREL AFDC API)
    "ev_route_planner": handle_ev_route_planner,
    # Tesla location refresh
    "tesla_location_refresh": handle_tesla_location_refresh,
    # YouTube search and play
    "youtube": handle_youtube,
    # Context Bridge - Unified knowledge orchestration (PIC + KG-API)
    "knowledge_query": handle_knowledge_query,
    "get_enriched_context": handle_get_enriched_context,
    "link_goal_to_knowledge": handle_link_goal_to_knowledge,
}


def set_progress_context(callback: Optional[ProgressCallback], user_id: Optional[str], location: Optional[Any] = None):
    """Set the current progress callback, user ID, and location for tool calls."""
    global _current_progress_callback, _current_user_id, _current_user_location
    _current_progress_callback = callback
    _current_user_id = user_id
    _current_user_location = location


async def dispatch_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call by name. Returns the result string.
    
    Implements a cache layer for read-only tools to enable zero-wait responses.
    Mutating tools (restart, start, stop, save_memory) bypass cache.
    """
    from nova.cache import get_cached, set_cached
    
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    
    # Tools that should NEVER be cached (mutating or user-specific context)
    UNCACHEABLE_TOOLS = {
        "service_restart", "service_start", "service_stop",  # Mutating (legacy)
        "homelab_operations",  # Mutating actions (restart/start/stop)
        "save_memory", "forget_memory",  # Mutating PIC
        "openclaw_delegate",  # Long-running, unique tasks
        "set_reminder", "manage_timer",  # Mutating
        "control_lights",  # Mutating
        "manage_ticket", "manage_workspace", "manage_notes", "exomind",  # Mutating
        "tesla_charge_control", "tesla_climate_control",  # Mutating
        "tesla_lock_control", "tesla_trunk_control",  # Mutating
        "tesla_wake", "tesla_honk_flash",  # Mutating
    }
    
    # Check cache for cacheable tools
    cache_key_args = {k: v for k, v in args.items() if k not in ("progress_callback", "user_id")}
    if name not in UNCACHEABLE_TOOLS:
        cached_result = await get_cached(name, cache_key_args)
        if cached_result is not None:
            logger.info(f"[Cache HIT] {name} — returning cached result")
            return cached_result
    
    try:
        # Inject progress context for openclaw_delegate
        if name == "openclaw_delegate":
            args["progress_callback"] = _current_progress_callback
            args["user_id"] = _current_user_id
        
        # Inject user_id for homelab ops tools that need approval
        if name in ("service_restart", "service_start", "service_stop"):
            args["user_id"] = _current_user_id or "default"
        
        # Inject user_id for unified homelab_operations mutating actions
        if name == "homelab_operations" and args.get("action") in ("restart", "start", "stop"):
            args["user_id"] = _current_user_id or "default"
        
        # Inject user_id for conversation search
        if name == "search_past_conversations" or name.startswith("tesla_"):
            args["user_id"] = _current_user_id or "default"
        
        result = await handler(**args)
        
        # Cache the result for cacheable tools
        if name not in UNCACHEABLE_TOOLS:
            await set_cached(name, cache_key_args, result)
            logger.debug(f"[Cache SET] {name}")
        
        return result
    except TypeError as e:
        logger.error(f"Tool {name} argument error: {e}")
        return f"Tool error: {str(e)}"
    except Exception as e:
        logger.error(f"Tool {name} execution error: {e}")
        return f"Tool error: {str(e)}"

# ---------------------------------------------------------------------------
# Unified Tesla Control Tool (Claude Skills Format)
# ---------------------------------------------------------------------------

TESLA_CONTROL_TOOL = {
    "type": "function",
    "function": {
        "name": "tesla_control",
        "description": "Control Tesla vehicles via Tesla Relay Service with approval-gated commands. Supports listing vehicles, getting status, controlling climate, charging, locks, trunk, navigation, and more. All commands follow tiered approval system (Tier 0-4) for security.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["vehicles", "status", "climate", "charge", "lock", "trunk", "wake", "honk_flash", "navigation"],
                    "description": "Tesla operation to perform. Query operations (vehicles, status) require no approval. Control operations follow tiered approval (Tier 0-4).",
                },
                "vehicle_identifier": {
                    "type": "string",
                    "description": "Vehicle identifier: model name (e.g. 'Model 3'), display name (e.g. 'Black Panther'), or VIN. Optional for most actions.",
                },
                "command": {
                    "type": "string",
                    "description": "Specific command for control actions. Examples: 'start'/'stop' (climate/charge), 'lock'/'unlock' (doors), 'set_temp' (climate), 'set_limit' (charge), 'honk'/'flash' (locate), 'open_frunk'/'open_trunk'.",
                },
                "value": {
                    "type": "number",
                    "description": "Command parameter value. Examples: temperature in Fahrenheit for set_temp, charge limit percentage (50-100) for set_limit, charging amps for set_amps.",
                },
                "destination": {
                    "type": "string",
                    "description": "Address or place name for navigation action (e.g. '1600 Amphitheatre Parkway, Mountain View, CA').",
                },
                "latitude": {
                    "type": "number",
                    "description": "GPS latitude for precise navigation. Use with longitude.",
                },
                "longitude": {
                    "type": "number",
                    "description": "GPS longitude for precise navigation. Use with latitude.",
                },
                "vin": {
                    "type": "string",
                    "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                },
            },
            "required": ["action"],
        },
    },
}

# Append unified Tesla tool to TOOL_DEFINITIONS
TOOL_DEFINITIONS.append(TESLA_CONTROL_TOOL)

# ---------------------------------------------------------------------------
# Legacy Tesla Tool Definitions (DEPRECATED - kept for backward compatibility)
# ---------------------------------------------------------------------------

TESLA_TOOL_DEFINITIONS_LEGACY = [
    {
        "type": "function",
        "function": {
            "name": "tesla_charge_control",
            "description": "DEPRECATED: Use tesla_control with action='charge' instead. Control Tesla charging: start, stop, set charge limit, or set charging amps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "set_limit", "set_amps"],
                        "description": "Charging action to perform.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Charge limit percentage (for set_limit action).",
                    },
                    "amps": {
                        "type": "integer",
                        "description": "Charging amps (for set_amps action).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_climate_control",
            "description": "Control Tesla climate: start, stop, or set temperature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "set_temp"],
                        "description": "Climate action to perform.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                    "temp": {
                        "type": "number",
                        "description": "Temperature in Fahrenheit (for set_temp action).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_lock_control",
            "description": "Lock or unlock Tesla doors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["lock", "unlock"],
                        "description": "Lock action to perform.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_trunk_control",
            "description": "Open Tesla trunk or frunk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "which": {
                        "type": "string",
                        "enum": ["front", "rear"],
                        "description": "Which trunk to open: front (frunk) or rear (trunk).",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                },
                "required": ["which"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_wake",
            "description": "Wake up a sleeping Tesla vehicle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_honk_flash",
            "description": "Honk the horn or flash the lights on a Tesla.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["honk", "flash"],
                        "description": "Action to perform.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_navigation",
            "description": "Send navigation destination to Tesla. Can send address/place name or GPS coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "Address or place name (e.g., '1600 Amphitheatre Parkway, Mountain View, CA' or 'Starbucks near me').",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "Optional GPS latitude for precise navigation.",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Optional GPS longitude for precise navigation.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, uses the first vehicle.",
                    },
                },
                "required": ["destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tesla_send_navigation",
            "description": "Send navigation destination to Tesla. Use this when the user wants to send directions, navigate to a location, or set a destination in their Tesla. Supports both address strings and GPS coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "Address or place name (e.g., '2030 W Gray St, Houston, TX' or 'Barnes & Noble River Oaks'). Required unless latitude/longitude are provided.",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "GPS latitude for precise navigation. Use with longitude for exact coordinates.",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "GPS longitude for precise navigation. Use with latitude for exact coordinates.",
                    },
                    "vin": {
                        "type": "string",
                        "description": "Vehicle VIN. If not provided, sends to the first vehicle.",
                    },
                },
                "required": ["destination"],
            },
        },
    },
]

# NOTE: Legacy granular Tesla tools (DEPRECATED in favor of unified tesla_control)
# These are kept for backward compatibility but should not be registered in bot.py

# ---------------------------------------------------------------------------
# Unified Tesla Tool Handler (skill-based, loaded at top of file)
# ---------------------------------------------------------------------------
# Tesla handlers are now loaded from skills/tesla-control at line 67-69
# Register unified Tesla handler (already imported from skill)
TOOL_HANDLERS["tesla_control"] = handle_tesla_control

# ---------------------------------------------------------------------------
# Homelab Diagnostics Tool (skill-based)
# ---------------------------------------------------------------------------

_diagnostics = _load_skill_module("homelab-diagnostics", "diagnostics")

async def handle_homelab_diagnostics(args: dict) -> dict:
    """Run homelab infrastructure diagnostics."""
    action = args.get("action", "full_diagnostics")
    
    # Call the skill's full_diagnostics function
    if action == "full_diagnostics":
        return await _diagnostics.full_diagnostics()
    elif action == "openclaw_health":
        return await _diagnostics.check_openclaw_health()
    elif action == "ai_inferencing_health":
        return await _diagnostics.check_ai_inferencing_health()
    elif action == "hermes_health":
        return await _diagnostics.check_hermes_health()
    elif action == "hermy_score":
        # For hermy_score, we need to gather components first
        components = {
            "openclaw": await _diagnostics.check_openclaw_health(),
            "ai_inferencing": await _diagnostics.check_ai_inferencing_health(),
            "hermes_core": await _diagnostics.check_hermes_health()
        }
        return await _diagnostics.calculate_hermy_score(components)
    else:
        return {"success": False, "error": f"Unknown action: {action}"}

# Homelab diagnostics tool definition
HOMELAB_DIAGNOSTICS_TOOL = {
    "type": "function",
    "function": {
        "name": "homelab_diagnostics",
        "description": "Run comprehensive AI Homelab infrastructure diagnostics. Check OpenClaw status, AI Inferencing health, Hermes Core connectivity, and calculate Hermy score (overall health 0-100). Use for system health queries, error investigations, or component status checks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["full_diagnostics", "openclaw_health", "ai_inferencing_health", "hermes_health", "hermy_score"],
                    "description": "Diagnostic action: full_diagnostics (complete report), openclaw_health (gateway status), ai_inferencing_health (key vault), hermes_health (email/calendar), hermy_score (0-100 health score)",
                },
            },
            "required": ["action"],
        },
    },
}

# Add to tool definitions
TOOL_DEFINITIONS.append(HOMELAB_DIAGNOSTICS_TOOL)

# Register handler
TOOL_HANDLERS["homelab_diagnostics"] = handle_homelab_diagnostics

# ---------------------------------------------------------------------------
# Query Frameworks Tool (Dynamic LIAM Framework Discovery)
# ---------------------------------------------------------------------------

async def handle_query_frameworks(args: dict) -> dict:
    """Query LIAM for scientific frameworks applicable to a problem."""
    problem_description = args.get("problem_description", "")
    dimension_id = args.get("dimension_id")
    category = args.get("category")
    limit = args.get("limit", 5)
    
    if not problem_description:
        return {
            "success": False,
            "error": "problem_description is required"
        }
    
    # Import the skill script
    import sys
    skill_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "skills/query-frameworks/scripts"
    )
    sys.path.insert(0, skill_path)
    
    try:
        from query_frameworks import query_frameworks
        
        result = await query_frameworks(
            problem_description=problem_description,
            dimension_id=dimension_id,
            category=category,
            limit=limit,
            use_context_bridge=True
        )
        
        if "error" in result:
            return {
                "success": False,
                "error": result["error"],
                "fallback": "Using hardcoded framework quick-reference from training knowledge"
            }
        
        return {
            "success": True,
            "query": result.get("query"),
            "frameworks": result.get("applicable_frameworks", []),
            "dimensions": result.get("dimensions_detected", []),
            "synthesis": result.get("synthesis", "")
        }
    except Exception as e:
        logger.error(f"Framework query failed: {e}")
        return {
            "success": False,
            "error": f"Framework query error: {str(e)}",
            "fallback": "Using hardcoded framework quick-reference from training knowledge"
        }
    finally:
        if skill_path in sys.path:
            sys.path.remove(skill_path)

QUERY_FRAMEWORKS_TOOL = {
    "type": "function",
    "function": {
        "name": "query_frameworks",
        "description": "Query LIAM (Life Intelligence Augmentation Matrix) for scientific frameworks applicable to a decision, problem, or life question. Returns framework names, when to use them, key concepts, limitations, and a synthesis of how they apply. Use this to dynamically discover frameworks as they are added to the knowledge graph. Always call this BEFORE applying frameworks to ensure you're using the latest available frameworks, not just hardcoded ones.",
        "parameters": {
            "type": "object",
            "properties": {
                "problem_description": {
                    "type": "string",
                    "description": "Natural language description of the decision, problem, or life question (e.g., 'Should I switch careers?', 'How to build sustainable habits?')"
                },
                "dimension_id": {
                    "type": "string",
                    "description": "Optional: Filter by LIAM dimension (e.g., 'habits', 'decision_fatigue', 'financial', 'metacognition')"
                },
                "category": {
                    "type": "string",
                    "enum": ["decision_making", "behavioral", "cognitive", "probabilistic", "computational", "systems"],
                    "description": "Optional: Filter by framework category"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of frameworks to return (default: 5)",
                    "default": 5
                }
            },
            "required": ["problem_description"]
        }
    }
}

TOOL_DEFINITIONS.append(QUERY_FRAMEWORKS_TOOL)
TOOL_HANDLERS["query_frameworks"] = handle_query_frameworks
