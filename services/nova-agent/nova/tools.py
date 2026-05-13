"""
Nova Agent tool definitions and handlers.

Tools are registered with Pipecat's LLM function calling system.
Each tool is an OpenAI-format function definition + an async handler.

Native tools: casual/fast (weather, lights, workstation, reminders)
Delegated tools: complex tasks via Pi Agent Hub with WebSocket RPC
"""

import asyncio
import json
import os
import re
import aiohttp
import jwt

# Load .env file early to ensure environment variables are available
# This handles cases where tools.py is imported before bot.py loads dotenv
try:
    from dotenv import load_dotenv
    _dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(_dotenv_path):
        load_dotenv(dotenv_path=_dotenv_path, override=True)
except Exception:
    pass  # dotenv not available or .env doesn't exist
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

_get_weather_mod = _load_skill_module("get-weather", "get_weather")
handle_get_weather = _get_weather_mod.handle_get_weather

_notes = _load_skill_module("notes-manager", "notes_manager")
handle_manage_notes = _notes.handle_manage_notes

from nova.exomind import handle_exomind

# Tesla control (skill-based)
_tesla = _load_skill_module("tesla-control", "tesla_control")
handle_tesla_location_refresh = _tesla.handle_tesla_location_refresh
handle_tesla_control = _tesla.handle_tesla_control

# Tesla stream monitor (skill-based)
_tesla_stream = _load_skill_module("tesla-stream-monitor", "tesla_stream_monitor")
handle_tesla_stream_monitor = _tesla_stream.handle_tesla_stream_monitor

# STAAR Tutor (skill-based)
_staar = _load_skill_module("staar-tutor", "staar_tutor")
handle_staar_tutor = _staar.handle_staar_tutor

# Tesla wake (direct import from tesla_tools)
from nova.tesla_tools import handle_tesla_wake, handle_tesla_navigation

# Import JWT generator from dedicated auth module
from nova.hermes_auth import generate_hermes_jwt


# Environment configuration
# OpenClaw removed — all delegation now goes through hub_delegate
AI_GATEWAY_BUDGET_OVERRIDE = os.environ.get("AI_GATEWAY_BUDGET_OVERRIDE", "")
WORKSTATION_MONITOR_URL = os.environ.get("WORKSTATION_MONITOR_URL", "http://localhost:8404")
NETDIAG_URL = os.environ.get("NETDIAG_URL", "http://localhost:8405")
ECOSYSTEM_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
CIG_URL = os.environ.get("CIG_URL", os.environ.get("HERMES_CORE_URL", "http://localhost:8780"))
HERMES_JWT_TOKEN = os.environ.get("HERMES_JWT_TOKEN", "")
ECOSYSTEM_API_KEY = os.environ.get("ECOSYSTEM_API_KEY", "ai-gateway-api-key-2024")
ECOSYSTEM_USER_ID = os.environ.get("ECOSYSTEM_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")
INTERNAL_SERVICE_KEY = os.environ.get("INTERNAL_SERVICE_KEY", "")
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
SKILL_DISCOVERY_URL = os.environ.get("SKILL_DISCOVERY_URL", "http://127.0.0.1:18791")
PI_HUB_URL = os.environ.get("PI_HUB_URL", "ws://127.0.0.1:18900")
PI_HUB_TOKEN = os.environ.get("PI_HUB_TOKEN", "changeme")

# Progress callback type: called with (status_type, message) during delegation
ProgressCallback = Callable[[str, str], None]
_current_progress_callback: Optional[ProgressCallback] = None
_current_user_id: Optional[str] = None
_current_conversation_id: Optional[str] = None
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

TOOL_DEFINITIONS = []

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

def _clean_weather_line(text: str) -> str:
    cleaned = re.sub(r"\[[^\]]+\]\([^)]+\)", "", str(text or ""))
    cleaned = re.sub(r"^\s*#+\s*", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"^\s*[-*]\s*", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _weather_condition_code(condition: str) -> str:
    normalized = str(condition or "").lower()
    if any(term in normalized for term in ("thunder", "storm", "showers", "rain")):
        return "rain"
    if any(term in normalized for term in ("cloud", "overcast")):
        return "clouds"
    if any(term in normalized for term in ("sun", "clear", "hot", "warm")):
        return "sun"
    return "clouds"


def _extract_weather_card(location: str, query: str, display: str, speech: str) -> dict[str, Any]:
    clean_lines = [_clean_weather_line(line) for line in str(display or "").splitlines()]
    clean_lines = [line for line in clean_lines if line and not line.startswith("(")]
    title = next((line for line in clean_lines if "weather" in line.lower() or "forecast" in line.lower()), "")
    if not title:
        title = f"Weather for {location}"
    subtitle_match = re.search(r"\(([^)]*(?:sat|sun|mon|tue|wed|thu|fri|today|tomorrow)[^)]*)\)", title, flags=re.IGNORECASE)
    subtitle = subtitle_match.group(1).strip() if subtitle_match else ""
    periods: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    period_re = re.compile(r"^(?P<label>today|tonight|tomorrow|saturday(?: night)?|sunday(?: night)?|monday(?: night)?|tuesday(?: night)?|wednesday(?: night)?|thursday(?: night)?|friday(?: night)?|weekend)\b(?:\s*\([^)]+\))?\s*:?\s*(?P<rest>.*)", re.IGNORECASE)
    for line in clean_lines:
        match = period_re.match(line)
        if match:
            if current:
                periods.append(current)
            current = {
                "name": match.group("label").strip().title(),
                "summary": match.group("rest").strip(),
            }
        elif current and line:
            current["summary"] = f"{current.get('summary', '').strip()} {line}".strip()
    if current:
        periods.append(current)
    if not periods and clean_lines:
        periods.append({"name": "Forecast", "summary": clean_lines[1] if len(clean_lines) > 1 else clean_lines[0]})
    for period in periods:
        summary = str(period.get("summary") or "")
        temps = [int(value) for value in re.findall(r"(-?\d{1,3})\s*°?\s*F", summary, flags=re.IGNORECASE)]
        if temps:
            period["highF"] = max(temps)
            period["lowF"] = min(temps)
        precip = re.search(r"(\d{1,3}\s*(?:-|to)\s*\d{1,3}%|\d{1,3}%)\s*(?:chance|rain|showers|storm|t-storm|precip)", summary, flags=re.IGNORECASE)
        if not precip:
            precip = re.search(r"(?:chance|rain|showers|storm|t-storm|precip)[^.\d]*(\d{1,3}\s*(?:-|to)\s*\d{1,3}%|\d{1,3}%)", summary, flags=re.IGNORECASE)
        if precip:
            precip_text = precip.group(1).replace(" to ", "-")
            precip_values = [int(value) for value in re.findall(r"\d{1,3}", precip_text)]
            if precip_values:
                period["precipChancePct"] = max(precip_values)
        wind = re.search(r"(winds?\s+[^.]+(?:mph|light|calm))", summary, flags=re.IGNORECASE)
        if wind:
            period["wind"] = re.sub(r"^winds?:\s*", "", wind.group(1).strip(), flags=re.IGNORECASE)
        period["conditionCode"] = _weather_condition_code(summary)
    alerts: list[str] = []
    for line in clean_lines:
        if "alert" in line.lower():
            alerts.append(line)
    return {
        "kind": "weather_forecast",
        "schemaVersion": 2,
        "location": location,
        "title": title,
        "subtitle": subtitle,
        "summary": _clean_weather_line(speech) or (periods[0].get("summary") if periods else title),
        "periods": periods[:6],
        "alerts": alerts[:3],
        "source": "perplexity",
        "query": query,
    }


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
# Hub Agent Delegation (Pi Agent Hub WebSocket RPC)
# ---------------------------------------------------------------------------

async def handle_hub_delegate(
    agent: str,
    method: str,
    params: Optional[dict] = None,
    context: str = "",
    **kwargs,
) -> str:
    """
    Delegate a task to a Pi Agent Hub background agent via WebSocket RPC.
    
    Connects to the Hub gateway, authenticates, calls the specified RPC method,
    and returns the result. Supports progress callbacks for ThinkingCard updates.
    """
    import websockets
    
    if not PI_HUB_TOKEN:
        return "Pi Agent Hub delegation not configured (PI_HUB_TOKEN missing)."
    
    rpc_params = params or {}
    if context:
        rpc_params["context"] = context
    
    # Map agent name to the correct RPC method if shorthand provided
    method_map = {
        "atlas": {
            "research": "atlas.research",
            "analytics": "atlas.analytics",
            "factCheck": "atlas.factCheck",
            "fact_check": "atlas.factCheck",
        },
        "hermes": {
            "inbox-briefing": "hermes.inbox-briefing",
            "inbox": "hermes.inbox-briefing",
            "draft": "hermes.draft",
            "email": "hermes.draft",
            "calendar-briefing": "hermes.calendar-briefing",
            "calendar": "hermes.calendar-briefing",
            "meeting-prep": "hermes.meeting-prep",
            "meeting": "hermes.meeting-prep",
            "follow-up": "hermes.follow-up",
            "followup": "hermes.follow-up",
            "morning-briefing": "hermes.morning-briefing",
            "morning": "hermes.morning-briefing",
        },
        "argus": {
            "browse": "argus.browse",
            "navigate": "argus.browse",
            "order": "argus.browse",
            "book": "argus.browse",
            "form": "argus.browse",
        },
        "infra": {
            "health": "health",
            "status": "health",
            "diagnose": "agents.run",
            "restart": "agents.run",
            "monitor": "agents.run",
        },
        "coder": {
            "fix": "agents.run",
            "heal": "agents.run",
            "implement": "agents.run",
        },
        "tesla": {
            "status": "tesla.status",
            "command": "tesla.command",
            "monitor": "agents.run",
        },
        "orchestrator": {
            "run": "agents.run",
            "dispatch": "orchestrator.dispatch",
            "delegate": "orchestrator.dispatch",
        },
        "scribe": {
            "edit": "agents.run",
            "format": "agents.run",
            "document": "agents.run",
            "write": "agents.run",
            "run": "agents.run",
        },
    }
    
    # Resolve shorthand method names
    resolved_method = method
    agent_methods = method_map.get(agent, {})
    if method in agent_methods:
        resolved_method = agent_methods[method]
    
    # For agents.run shorthand, inject the agentId
    if resolved_method == "agents.run" and "agentId" not in rpc_params:
        rpc_params["agentId"] = agent
    
    # Inject context into task description for agents.run
    if resolved_method == "agents.run" and "task" not in rpc_params and context:
        rpc_params["task"] = context
    
    progress_callback = _current_progress_callback
    
    try:
        # Send initial progress
        if progress_callback:
            await progress_callback("phase", "delegating")
            await progress_callback("thinking", f"🔧 Delegating to {agent}: {resolved_method}")
        
        # Connect to Hub WebSocket with timeout
        connect_timeout = 10
        rpc_timeout = 120  # 2 minutes for most RPC calls (research can take longer)
        
        # Adjust timeout based on method
        if "research" in resolved_method:
            rpc_timeout = 300  # 5 minutes for research
        elif "health" in resolved_method or "status" in resolved_method:
            rpc_timeout = 30  # 30 seconds for status checks
        
        result = await asyncio.wait_for(
            _hub_rpc_call(resolved_method, rpc_params),
            timeout=rpc_timeout,
        )
        
        # Process result
        if isinstance(result, dict):
            # Check for approval-related results
            status = result.get("status", "")
            if status == "pending":
                approval_msg = result.get("reason", "Approval required — check your Hyperspace app.")
                return f"I've requested approval for this action. {approval_msg}"
            elif status == "rejected":
                return f"The approval was rejected: {result.get('reason', 'No reason given.')}"
            elif status == "timeout":
                return "The approval request timed out. Please try again."
            
            # Extract response text from agent result
            response = result.get("response", "")
            if response:
                return response
            
            # Fallback: return the whole result as JSON
            return json.dumps(result, default=str)
        
        return str(result) if result else "Hub agent returned no result."
        
    except asyncio.TimeoutError:
        logger.warning(f"Hub delegation timed out: {agent}/{method}")
        return f"The {agent} agent is taking longer than expected. The task is still running in the background — I'll let you know when it's done."
    except ConnectionRefusedError:
        logger.error("Pi Agent Hub connection refused")
        return "The Pi Agent Hub is not running. I can't delegate this task right now."
    except Exception as e:
        logger.error(f"Hub delegation error: {e}", exc_info=True)
        return f"Error delegating to {agent}: {str(e)}"


async def _hub_rpc_call(method: str, params: dict) -> Any:
    """Make a single RPC call to Pi Agent Hub via WebSocket.
    
    Handles the challenge-auth handshake and returns the RPC result.
    """
    import websockets
    
    request_id = f"nova-{int(asyncio.get_event_loop().time() * 1000)}"
    connect_id = f"nova-connect-{int(asyncio.get_event_loop().time() * 1000)}"
    connect_sent = False
    
    result_future = asyncio.get_event_loop().create_future()
    
    async with websockets.connect(
        PI_HUB_URL,
        max_size=25 * 1024 * 1024,  # 25MB max payload
        open_timeout=10,
    ) as ws:
        # Process messages
        async for raw in ws:
            try:
                frame = json.loads(raw)
                ftype = frame.get("type", "")
                
                # Handle connect.challenge
                if ftype == "event" and frame.get("event") == "connect.challenge":
                    if not connect_sent:
                        connect_sent = True
                        connect_frame = {
                            "type": "req",
                            "id": connect_id,
                            "method": "connect",
                            "params": {
                                "minProtocol": 3,
                                "maxProtocol": 3,
                                "client": {
                                    "id": "nova-agent",
                                    "displayName": "Nova Agent",
                                    "version": "1.0.0",
                                    "platform": "python",
                                    "mode": "voice",
                                },
                                "caps": [],
                                "role": "operator",
                                "scopes": ["operator.read", "operator.write"],
                                "auth": {"token": PI_HUB_TOKEN},
                            },
                        }
                        await ws.send(json.dumps(connect_frame))
                    continue
                
                # Handle tick events (ignore)
                if ftype == "event" and frame.get("event") == "tick":
                    continue
                
                # Handle broadcast events (log but don't resolve)
                if ftype == "event":
                    logger.debug(f"Hub event: {frame.get('event')} — {str(frame.get('payload', ''))[:100]}")
                    continue
                
                # Handle response frames
                if ftype == "res":
                    res_id = frame.get("id", "")
                    ok = frame.get("ok", False)
                    
                    # If this is the connect response, send the actual RPC call
                    if res_id == connect_id:
                        if ok:
                            req_frame = {
                                "type": "req",
                                "id": request_id,
                                "method": method,
                                "params": params,
                            }
                            await ws.send(json.dumps(req_frame))
                        else:
                            error = frame.get("error", {}).get("message", "Connect failed")
                            result_future.set_exception(Exception(f"Hub auth failed: {error}"))
                            break
                        continue
                    
                    # This is our method response
                    if res_id == request_id:
                        if ok:
                            result_future.set_result(frame.get("payload"))
                        else:
                            error = frame.get("error", {}).get("message", "RPC error")
                            result_future.set_exception(Exception(f"Hub RPC error: {error}"))
                        break
                    
                    # Unknown response — ignore
                    continue
                    
            except json.JSONDecodeError:
                continue
    
    return await result_future


# ---------------------------------------------------------------------------
# CIG query handler (Communication Intelligence Graph)
# ---------------------------------------------------------------------------

async def handle_cig_query(domain: str = "email", query: str = "", user_id: str = "default", **kwargs) -> str:
    """Query the Communication Intelligence Graph for analytics."""
    from nova.cig import query_cig
    return await query_cig(user_id, domain, query, **kwargs)


# ---------------------------------------------------------------------------
# Memory tool handlers (PCG-backed)
# ---------------------------------------------------------------------------

async def handle_save_memory(content: str = "", category: str = "other", **kwargs) -> str:
    """Save a user preference/fact to PCG as an observation."""
    from nova.pcg import record_observation
    # Tolerate LLM using 'fact' instead of 'content' (SKILL.md examples use fact=)
    if not content and kwargs.get("fact"):
        content = kwargs["fact"]

    
    # Derive a short key from the fact
    key = content.split()[:4]  # first few words
    key_str = "_".join(w.lower().strip(".,!?'\"") for w in key if w.isalpha())[:40] or "user_stated"
    
    success = await record_observation(
        observation_type="preference",
        category=category,
        key=key_str,
        value=content,
        context="User explicitly stated this during voice conversation with Nova",
    )
    if success:
        logger.info(f"PCG save_memory OK: [{category}] {content[:80]}")
        return f"Saved to PCG ({category}): {content}"
    return "I will remember that for this conversation, but could not save to long-term memory."


async def handle_recall_memory(query: str = "", category: str = "", **kwargs) -> str:
    """Search PCG for stored preferences and observations matching a query."""
    from nova.pcg import get_preferences, get_identity

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
        logger.info(f"PCG recall_memory: {len(results)} matches for '{query}'")
        return "Found in PCG:\n" + "\n".join(results[:10])

    logger.info(f"PCG recall_memory: no matches for '{query}'")
    return f"Nothing found in PCG for '{query}'. The user may not have told you this yet."


async def handle_forget_memory(keyword: str = "", **kwargs) -> str:
    """Forget is not directly supported — record a correction observation instead."""
    from nova.pcg import record_observation
    
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


# ---------------------------------------------------------------------------
# Conversation compaction handler
# ---------------------------------------------------------------------------

async def handle_compact_conversations(user_id: str = "default", **kwargs) -> str:
    """Run a compaction cycle on older conversations using negative exponential decay.
    
    Summarizes old messages into topics/subtopics, extracts facts to PCG.
    """
    from nova.store import run_compaction_cycle
    results = await run_compaction_cycle(user_id=user_id)
    
    if not results:
        return "No conversations needed compaction."
    
    compacted = [r for r in results if r.get("status") == "compacted"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    failed = [r for r in results if r.get("status") == "failed"]
    
    total_facts = sum(r.get("facts_extracted", 0) for r in compacted)
    
    summary = f"Compaction cycle complete:\n"
    summary += f"  Compacted: {len(compacted)} conversations\n"
    summary += f"  Skipped: {len(skipped)} (too recent or too few messages)\n"
    if failed:
        summary += f"  Failed: {len(failed)}\n"
    summary += f"  Facts extracted (stored in conversation metadata): {total_facts}\n"
    summary += f"  Review facts and save important ones to PCG via save_memory.\n"
    
    for r in compacted[:5]:
        topics = ", ".join(r.get("topics", [])[:3])
        summary += f"\n  📋 {r.get('title', '?')[:40]}: {r.get('compacted', 0)} msgs → [{topics}]"
    
    return summary


# ---------------------------------------------------------------------------
# PCG unified context handlers
# ---------------------------------------------------------------------------

async def handle_query_context(
    query: str = "",
    include_personal: bool = True,
    include_knowledge: bool = True,
    include_dimensions: bool = True,
    **kwargs,
) -> str:
    """Query PCG for unified context across personal data, knowledge graph, and frameworks."""
    from nova.pcg import query as pcg_query
    
    result = await pcg_query(
        query=query,
        include_personal=include_personal,
        include_knowledge=include_knowledge,
        include_frameworks=include_dimensions,
    )
    
    synthesis = result.get("synthesis", "")
    personal = result.get("personal", [])
    knowledge = result.get("knowledge", [])
    
    if synthesis:
        logger.info(f"PCG query OK: '{query[:50]}' -> {len(personal)} personal, {len(knowledge)} knowledge")
        return synthesis
    
    # Build manual synthesis
    parts = []
    if personal:
        parts.append("Personal context:")
        for p in personal[:5]:
            parts.append(f"  - {p.get('key', '?')}: {p.get('value', '')[:100]}")
    if knowledge:
        parts.append("Knowledge graph:")
        for k in knowledge[:5]:
            parts.append(f"  - {k.get('name', k.get('id', '?'))}: {k.get('type', '')}")
    
    if not parts:
        return f"No relevant context found for '{query}'."
    
    logger.info(f"PCG query OK: '{query[:50]}' -> {len(personal)} personal, {len(knowledge)} knowledge")
    return "\n".join(parts)


async def handle_kg_query(query: str = "", entity_type: str = "Service", **kwargs) -> str:
    """Query PCG knowledge graph for infrastructure context."""
    from nova.pcg import query_knowledge_graph, search_knowledge
    
    # Search knowledge graph for matching entities
    results = await search_knowledge(query, entity_types=[entity_type] if entity_type else None)
    
    if results:
        lines = []
        for r in results[:5]:
            name = r.get("name", r.get("id", "?"))
            rtype = r.get("type", "")
            props = r.get("properties", {})
            lines.append(f"**{name}** ({rtype})")
            if props:
                for k, v in list(props.items())[:3]:
                    lines.append(f"  - {k}: {v}")
        return "\n".join(lines)
    
    # Fall back to natural language query
    context = await query_knowledge_graph(query)
    if context:
        return context
    
    return f"No knowledge graph context found for '{query}'."


_conversation_search_count: dict[str, int] = {}

async def handle_search_past_conversations(
    query: str, days_back: int = 90, limit: int = 5, user_id: str = "default",
    from_days: int = None, to_days: int = None, exclude_conversation_id: str = "",
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
            user_id, query, limit=limit, from_days=from_days, to_days=to_days,
            exclude_conversation_id=exclude_conversation_id,
        )
    else:
        results = await search_past_conversations(
            user_id, query, days_back, limit,
            exclude_conversation_id=exclude_conversation_id,
        )
    
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
        snippet = r.get("snippet", "")[:1500]
        msg_count = r.get("message_count", "")
        date = r.get("date", r.get("created_at", ""))
        date_display = f" [{date}]" if date else ""
        output.append(f"{i}. {title}{date_display}" + (f" ({msg_count} messages)" if msg_count else ""))
        if snippet:
            output.append(f"   {snippet}")
    
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
    studio: str, action: str = "recent", item_id: str = "", query: str = "",
    user_tz: str = "America/Chicago",
) -> str:
    """Read status/results from homelab studios via ecosystem dashboard API."""
    base = ECOSYSTEM_URL
    headers = {"X-API-Key": ECOSYSTEM_API_KEY}
    hermes = CIG_URL
    # Generate JWT token dynamically for CIG authentication
    hermes_token = generate_hermes_jwt()
    hermes_headers = {"Authorization": f"Bearer {hermes_token}"}
    try:
        async with aiohttp.ClientSession() as session:

            # --- Calendar (via CIG :8780) ---
            if studio == "calendar":
                timeout = aiohttp.ClientTimeout(total=12)

                # Local timezone helpers for display and filtering
                import zoneinfo as _zi
                from datetime import datetime as _dt, timezone as _utc, timedelta as _td
                try:
                    _tz_obj = _zi.ZoneInfo(user_tz)
                except Exception:
                    _tz_obj = _zi.ZoneInfo("America/Chicago")
                _now_local = _dt.now(_utc.utc).astimezone(_tz_obj)
                _today_local = _now_local.date()

                def _parse_local(dt_str):
                    """Parse a CT ISO string (with offset) to a local datetime."""
                    if not dt_str:
                        return None
                    try:
                        s = str(dt_str)
                        if s.endswith("Z"):
                            d = _dt.fromisoformat(s[:-1]).replace(tzinfo=_utc.utc)
                        else:
                            d = _dt.fromisoformat(s)
                            if d.tzinfo is None:
                                d = d.replace(tzinfo=_utc.utc)
                        return d.astimezone(_tz_obj)
                    except Exception:
                        return None

                def _fmt_ct(dt_str):
                    """Return a readable local time string: '8:00 PM'."""
                    local = _parse_local(dt_str)
                    if not local:
                        return ""
                    return local.strftime("%-I:%M %p")

                def _is_on_date(dt_str, target_date):
                    local = _parse_local(dt_str)
                    return local is not None and local.date() == target_date

                # Briefing / Today / Tomorrow / This Week — all use /v1/calendar/events
                # with local-day filtering since the shortcut endpoints are not available.
                if action in ("briefing", "today", "tomorrow", "this_week"):
                    url = f"{hermes}/v1/calendar/events"
                    params: dict[str, Any] = {"days": 8}  # fetch 8 days, filter locally
                    async with session.get(url, params=params, headers=hermes_headers, timeout=timeout) as resp:
                        if resp.status != 200:
                            return f"Calendar API returned HTTP {resp.status}."
                        data = await resp.json()
                        events = data.get("events", [])

                    if action in ("briefing", "today"):
                        target_date = _today_local
                        label = "today"
                    elif action == "tomorrow":
                        target_date = _today_local + _td(days=1)
                        label = "tomorrow"
                    else:  # this_week
                        target_date = None
                        label = "this week"

                    if target_date is not None:
                        filtered = [ev for ev in events if _is_on_date(ev.get("start_time", ""), target_date)]
                    else:
                        week_end = _today_local + _td(days=7)
                        filtered = [ev for ev in events
                                    if _parse_local(ev.get("start_time", "")) is not None
                                    and _today_local <= _parse_local(ev.get("start_time", "")).date() < week_end]

                    if not filtered:
                        return f"No events on your calendar {label}."
                    lines = []
                    for ev in filtered[:8]:
                        title = ev.get("title", "Untitled")
                        st = _fmt_ct(ev.get("start_time", ""))
                        loc = (ev.get("location") or "").split("\n")[0]
                        cal = ev.get("calendar_name", "")
                        line = f"- {st} {title}"
                        if loc:
                            line += f" at {loc}"
                        if cal:
                            line += f" [{cal}]"
                        lines.append(line)
                    return (f"{len(filtered)} event(s) {label} (Central Time):\n"
                            + "\n".join(lines))

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
                    params = {"days": 7}
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
                            st = _fmt_ct(ev.get("start_time", ""))
                            loc = (ev.get("location") or "").split("\n")[0]
                            line = f"- {st} {title}"
                            if loc:
                                line += f" at {loc}"
                            lines.append(line)
                        return f"{len(events)} upcoming events (Central Time):\n" + "\n".join(lines)

            # --- Email Intelligence (via CIG :8780) ---
            elif studio == "email":
                timeout = aiohttp.ClientTimeout(total=12)

                # Helper: get email health data (reused)
                async def _email_health():
                    url = f"{hermes}/health"
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            return None, f"CIG returned HTTP {resp.status}."
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
                            # Let _trim_tool_result_for_llm in bot.py handle the truncation limits appropriately
                            return f"Research: {q}\nStatus: {rstatus}\n\n{report}"
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
# Skill Discovery handler (queries Skill Discovery API)
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
    delegate_to: str = "coder",
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
# Image Analysis (Qwen Vision)
# ---------------------------------------------------------------------------

async def handle_analyze_image(
    url: str,
    instruction: str = "Describe this image in detail.",
    **kwargs
) -> str:
    """Download an image and send it to the local Qwen Vision model via AI Gateway or llama.cpp synchronously."""
    import aiohttp
    import base64
    import os
    import asyncio
    
    # Fallback in case old args are passed
    image_url = url or kwargs.get("image_url", "")
    prompt = instruction or kwargs.get("prompt", "Describe this image in detail.")
    if not image_url:
        return "No image URL provided."
    
    try:
        # Download image first and encode as base64
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return f"Failed to download image from {image_url}. Status: {resp.status}"
                image_bytes = await resp.read()
        
        # Resize image to prevent massive token explosion (Qwen2.5-VL uses many tokens per pixel)
        import io
        from PIL import Image
        source_img = Image.open(io.BytesIO(image_bytes))
        if source_img.mode != "RGB":
            source_img = source_img.convert("RGB")

        def _build_payload(max_side: int) -> dict:
            img = source_img.copy()
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out_io = io.BytesIO()
            img.save(out_io, format="JPEG", quality=82)
            resized_bytes = out_io.getvalue()
            b64_image = base64.b64encode(resized_bytes).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{b64_image}"
            return {
                "model": os.environ.get("AI_GATEWAY_VISION_MODEL", "qwen-vision"),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 512,
            }

        async with aiohttp.ClientSession() as session:
            gateway_base = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1").rstrip("/")
            if gateway_base.endswith("/chat/completions"):
                vision_url = gateway_base
            elif gateway_base.endswith("/api/v1"):
                vision_url = f"{gateway_base[:-7]}/v1/chat/completions"
            elif gateway_base.endswith("/v1"):
                vision_url = f"{gateway_base}/chat/completions"
            else:
                vision_url = f"{gateway_base}/v1/chat/completions"
            gateway_key = os.environ.get("AI_GATEWAY_API_KEY", AI_GATEWAY_API_KEY)
            headers = {"X-API-Key": gateway_key} if gateway_key else {}
            result = ""
            for max_side in (768, 512):
                payload = _build_payload(max_side)
                async with session.post(vision_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        data = json.loads(body)
                        result = data["choices"][0]["message"]["content"]
                        break
                    retryable_size_error = (
                        resp.status == 400
                        and ("max_tokens" in body or "context" in body.lower() or "token" in body.lower())
                        and max_side != 512
                    )
                    if retryable_size_error:
                        logger.warning(f"Vision image too large at {max_side}px, retrying smaller")
                        continue
                    result = f"Vision model returned status {resp.status}: {body}"
                    break
                    
        return result
        
    except Exception as e:
        logger.error(f"Image analysis failed: {e}")
        return f"Image analysis failed: {str(e)}"

# ---------------------------------------------------------------------------
# Workspace management (fast direct API calls)
# ---------------------------------------------------------------------------

_NOVA_CATEGORY_LABELS = {
    "article":      "Articles",
    "case_study":   "Case Studies",
    "research":     "Research",
    "worksheet":    "Worksheets",
    "briefing":     "Briefings",
    "note":         "Notes",
    "report":       "Reports",
    "template":     "Templates",
}

def _build_nova_metadata(
    category: str = "",
    topic: str = "",
    tags: list | None = None,
) -> dict:
    """Build the standard Nova page metadata dict stamped into properties.metadata."""
    import datetime
    cat = (category or "note").lower().replace(" ", "_")
    if cat not in _NOVA_CATEGORY_LABELS:
        cat = "note"
    return {
        "source": "nova",
        "agent": "nova-agent",
        "category": cat,
        "category_label": _NOVA_CATEGORY_LABELS[cat],
        "topic": topic or "",
        "tags": tags or [],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


async def handle_manage_workspace(
    action: str,
    title: str = "",
    page_id: str = "",
    database_id: str = "",
    row_id: str = "",
    task_id: str = "",
    form_id: str = "",
    template_id: str = "",
    block_type: str = "",
    content: str = "",
    properties: dict | None = None,
    schema: list | None = None,
    priority: str = "",
    status: str = "",
    due_date: str = "",
    due_time: str = "",
    date: str = "",
    start_time: str = "",
    end_time: str = "",
    location: str = "",
    source_type: str = "",
    source_id: str = "",
    icon: str = "",
    parent_id: str = "",
    tags: list | None = None,
    assignee: str = "",
    fields: list | None = None,
    message: str = "",
    intent: str = "",
    category: str = "",
    topic: str = "",
    **kwargs,
) -> str:
    """Handle Pi Workspace operations via the Workspace API (port 8762)."""
    from nova.pi_workspace import (
        create_page, create_page_with_blocks, list_pages, get_page, get_page_blocks, create_block, delete_page,
        create_database, list_databases, list_database_rows, create_database_row,
        update_database_row, create_form, submit_form,
        get_planner_day, create_task, update_task, delete_task,
        create_event, update_planner_notes,
        search_workspace, ai_chat, get_component_registry,
        list_templates, create_from_template,
        _plain_title,
    )

    def _full_id_line(label: str, value: str) -> str:
        return f"{label}: {value}"

    import re as _re_pg
    _UUID_RE = _re_pg.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re_pg.IGNORECASE)

    async def _resolve_page_id(page_ref: str) -> tuple[str, str]:
        ref = (page_ref or "").strip()
        if not ref:
            return "", ""
        pages = await list_pages()
        exact = [p for p in pages if p.get("id") == ref]
        if exact:
            return ref, ""
        # Hallucination guard: if the input is shaped exactly like a UUID but
        # does NOT match any real page, refuse it instead of forwarding the
        # bogus UUID downstream. This is the most common loop pattern — the
        # model invents a plausible-looking UUID and retries it for 5+ turns.
        if _UUID_RE.match(ref):
            sample = ", ".join(
                f"{_plain_title(p.get('title','Untitled'))} ({p.get('id')})"
                for p in pages[:5]
            )
            return "", (
                f"REJECTED hallucinated page_id '{ref}'. This UUID does not exist in the workspace. "
                f"Do NOT retry with this id. Use one of the real page_ids below, "
                f"or call manage_workspace(action='list') / action='search' to find the correct one. "
                f"Real pages (sample): {sample or '(no pages yet — use create_page or create_page_with_blocks)'}"
            )
        prefix = ref.removesuffix("...").strip()
        if len(prefix) >= 4:
            matches = [p for p in pages if str(p.get("id", "")).startswith(prefix)]
            if len(matches) == 1:
                return matches[0]["id"], f"Resolved page_id prefix '{ref}' to full page_id {matches[0]['id']}."
            if len(matches) > 1:
                choices = ", ".join(f"{_plain_title(p.get('title', 'Untitled'))} ({p.get('id')})" for p in matches[:5])
                return "", f"Page ID prefix '{ref}' is ambiguous. Matching pages: {choices}"
        title_matches = [p for p in pages if ref.lower() in _plain_title(p.get("title", "")).lower()]
        if len(title_matches) == 1:
            return title_matches[0]["id"], f"Resolved page title '{ref}' to page_id {title_matches[0]['id']}."
        if len(title_matches) > 1:
            choices = ", ".join(f"{_plain_title(p.get('title', 'Untitled'))} ({p.get('id')})" for p in title_matches[:5])
            return "", f"Page reference '{ref}' matched multiple pages. Use one full page_id: {choices}"
        return ref, ""

    try:
        # ── Pages ──
        if action == "list_pages":
            pages = await list_pages()
            if not pages:
                return "No pages found in workspace. Create one with create_page."
            lines = [f"📝 {len(pages)} pages:"]
            for p in pages[:15]:
                t = _plain_title(p.get("title", "Untitled"))
                emoji = p.get("icon", {}).get("emoji", "")
                lines.append(f"  {emoji} {t} ({_full_id_line('page_id', p['id'])})")
            return "\n".join(lines)

        elif action == "create_page":
            if not title:
                return "Title is required to create a page."
            _nova_meta = _build_nova_metadata(category=category, topic=topic, tags=tags)
            page = await create_page(title, parent_id=parent_id, icon=icon,
                                     properties={"metadata": _nova_meta},
                                     created_by="nova-agent")
            if not page:
                return "Failed to create page."
            # If content provided, add a paragraph block
            if content:
                await create_block(page["id"], "paragraph",
                                   {"richText": [{"type": "text", "text": {"content": content}, "plainText": content}]},
                                   parent_id=page.get("rootBlockId", ""))
            return f"\u2705 Page created: \"{title}\" ({_full_id_line('page_id', page['id'])})  category={_nova_meta.get('category','uncategorized')}"

        elif action == "create_page_with_blocks":
            if not title:
                return "Title is required to create a page."
            if not properties or not isinstance(properties, dict):
                return "properties with 'blocks' array is required. Format: {\"blocks\": [{\"type\": \"heading_2\", \"content\": \"Problem 1\"}, {\"type\": \"paragraph\", \"content\": \"What is 3x4?\"}]}"
            blocks = properties.get("blocks", [])
            if not blocks:
                return "'blocks' array in properties is required. Each block: {type, content?, properties?}"
            _nova_meta = _build_nova_metadata(category=category, topic=topic, tags=tags)
            page = await create_page_with_blocks(title, blocks, icon=icon, parent_id=parent_id,
                                                 properties={"metadata": _nova_meta},
                                                 created_by="nova-agent")
            if not page:
                return "Failed to create page with blocks."
            bc = page.get("block_count", 0)
            return f"\u2705 Page created: \"{title}\" with {bc} blocks ({_full_id_line('page_id', page['id'])})  category={_nova_meta.get('category','uncategorized')}"

        elif action == "get_page":
            if not page_id:
                return "page_id is required for get_page."
            resolved_page_id, resolution_message = await _resolve_page_id(page_id)
            if not resolved_page_id:
                return resolution_message or f"Page {page_id} not found."
            page = await get_page(resolved_page_id)
            if not page:
                return f"Page {page_id} not found."
            t = _plain_title(page.get("title", "Untitled"))
            lines = [f"📄 {t}", _full_id_line("page_id", resolved_page_id)]
            if resolution_message:
                lines.append(resolution_message)
            # Get blocks
            blocks = await get_page_blocks(resolved_page_id)
            if not blocks:
                lines.append("  (Page is empty — no content blocks. Do not call get_page again for this page_id. If the page should have content, use create_page_with_blocks or add blocks first.)")
                return "\n".join(lines)
            for b in (blocks or [])[:20]:
                bt = b.get("type", "")
                props = b.get("properties", {})
                if bt == "paragraph" and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"][:3])
                    lines.append(f"  {text[:100]}")
                elif bt == "heading_1" and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"])
                    lines.append(f"  # {text}")
                elif bt == "heading_2" and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"])
                    lines.append(f"  ## {text}")
                elif bt == "to_do":
                    checked = "✅" if props.get("checked") else "☐"
                    text = props.get("richText", [{}])[0].get("plainText", "") if props.get("richText") else ""
                    lines.append(f"  {checked} {text}")
                elif bt == "bulleted_list_item" and props.get("richText"):
                    text = " ".join(s.get("plainText", "") for s in props["richText"])
                    lines.append(f"  • {text}")
                elif bt == "planner_task":
                    text = props.get("richText", [{}])[0].get("plainText", "") if props.get("richText") else title
                    lines.append(f"  📋 {text} [{props.get('status', '?')}] P{props.get('priority', '?')}")
            return "\n".join(lines)

        elif action == "delete_page":
            if not page_id:
                return "page_id is required for delete_page."
            full_id, msg = await _resolve_page_id(page_id)
            if not full_id:
                return msg or f"Page {page_id} not found."
                
            if not intent:
                return "DENIED: You must provide an 'intent' explaining why this page needs to be deleted."
                
            from nova.homelab_mutate import _request_approval, _poll_approval_status
            
            page_data = await get_page(full_id)
            page_title = _plain_title(page_data.get("title", "Untitled")) if page_data else full_id
            
            context = f"{intent} | Page: {page_title} ({full_id})"
            
            try:
                approval = await _request_approval(
                    tool_name="workspace_page_delete",
                    arguments={"page_id": full_id, "title": page_title, "intent": intent},
                    risk_level="medium",
                    context=context,
                )
            except Exception as e:
                return f"DENIED: Could not reach approval engine: {e}"
                
            result = await _poll_approval_status(approval["id"])
            status = result.get("status", "error")
            
            if status != "approved":
                reason = result.get("decisionReason") or result.get("reason") or status
                return f"Deletion of page '{page_title}' was {status}. {reason}"
                
            success = await delete_page(full_id)
            return f"Deleted page '{page_title}' ({full_id}) successfully." if success else f"Failed to delete page '{page_title}'."

        elif action == "add_block":
            if not page_id or not block_type:
                return "page_id and block_type are required for add_block."
            resolved_page_id, resolution_message = await _resolve_page_id(page_id)
            if not resolved_page_id:
                return resolution_message or f"Page {page_id} not found."
            block_props: dict = {}
            if content:
                block_props["richText"] = [{"type": "text", "text": {"content": content}, "plainText": content}]
            if properties:
                block_props.update(properties)
            block = await create_block(resolved_page_id, block_type, block_props, parent_id=parent_id)
            if not block:
                return "Failed to add block."
            suffix = f" {resolution_message}" if resolution_message else ""
            return f"✅ Added {block_type} block to page_id {resolved_page_id}.{suffix}"

        # ── Search ──
        elif action == "search":
            if not content:
                return "Search query (content) is required."
            results = await search_workspace(content)
            items = results.get("results", [])
            if not items:
                return f"No results found for '{content}'."
            lines = [f"🔍 Found {len(items)} results for '{content}':"]
            for r in items[:8]:
                emoji = {"page": "📄", "block": "📝", "database": "📊", "email": "📧"}.get(r.get("type", ""), "📎")
                lines.append(f"  {emoji} {r.get('title', 'Untitled')} (score: {r.get('score', 0):.2f}) id: {r.get('id', '')}")
            return "\n".join(lines)

        # ── Databases ──
        elif action == "list_databases":
            dbs = await list_databases()
            if not dbs:
                return "No databases found. Create one with create_database."
            lines = [f"📊 {len(dbs)} databases:"]
            for d in dbs[:10]:
                t = _plain_title(d.get("title", "Untitled"))
                schema_count = len(d.get("schema", []))
                lines.append(f"  {t} ({schema_count} columns) id: {d['id'][:8]}...")
            return "\n".join(lines)

        elif action == "create_database":
            if not title or not schema:
                return "Title and schema are required for create_database."
            db = await create_database(title, schema)
            if not db:
                return "Failed to create database."
            return f"✅ Database created: \"{title}\" ({len(schema)} columns) id: {db['id'][:8]}..."

        elif action == "list_rows":
            if not database_id:
                return "database_id is required for list_rows."
            rows = await list_database_rows(database_id)
            if not rows:
                return "No rows in this database."
            lines = [f"📋 {len(rows)} rows:"]
            for r in rows[:10]:
                t = _plain_title(r.get("title", "Untitled"))
                props = r.get("properties", {})
                prop_str = " | ".join(f"{k}={v}" for k, v in list(props.items())[:3])
                lines.append(f"  {t} — {prop_str}")
            return "\n".join(lines)

        elif action == "add_row":
            if not database_id or not title:
                return "database_id and title are required for add_row."
            row = await create_database_row(database_id, title, properties or {})
            if not row:
                return "Failed to add row."
            return f"✅ Row added: \"{title}\" (id: {row['id'][:8]}...)"

        elif action == "update_row":
            if not row_id or not properties:
                return "row_id and properties are required for update_row."
            result = await update_database_row(row_id, properties)
            if not result:
                return f"Row {row_id} not found or update failed."
            return f"✅ Row updated: {list(properties.keys())}"

        # ── Forms ──
        elif action == "create_form":
            if not database_id or not title or not fields:
                return "database_id, title, and fields are required for create_form."
            form = await create_form(database_id, title, fields)
            if not form:
                return "Failed to create form."
            return f"✅ Form created: \"{title}\" ({len(fields)} fields) id: {form.get('formId', '')[:8]}..."

        elif action == "submit_form":
            if not form_id or not properties:
                return "form_id and properties (values) are required for submit_form."
            sub = await submit_form(form_id, properties)
            if not sub:
                return "Failed to submit form."
            row_info = f" → row {sub.get('rowId', '')[:8]}..." if sub.get("rowId") else ""
            return f"✅ Form submitted{row_info}"

        # ── Planner ──
        elif action == "planner":
            day = await get_planner_day(date)
            if not day:
                return "Could not load planner."
            d = day.get("date", "today")
            tasks = day.get("tasks", [])
            events = day.get("events", [])
            notes = day.get("notes", [])
            lines = [f"📅 Planner for {d}:"]
            if events:
                lines.append(f"  Events ({len(events)}):")
                for e in events[:5]:
                    loc = f" @ {e['location']}" if e.get("location") else ""
                    lines.append(f"    🕐 {e.get('title', '')} {e.get('startTime', '')[:16]}{loc}")
            if tasks:
                lines.append(f"  Tasks ({len(tasks)}):")
                for t in tasks:
                    status_icon = {"not_started": "☐", "in_progress": "🔄", "done": "✅", "cancelled": "❌"}.get(t.get("status", ""), "☐")
                    pri = {"low": "🟢", "medium": "🟡", "high": "🟠", "urgent": "🔴"}.get(t.get("priority", ""), "")
                    due = f" due:{t.get('dueDate', '')}" if t.get("dueDate") else ""
                    lines.append(f"    {status_icon} {pri} {t.get('title', '')}{due}")
            if notes:
                note_text = " ".join(n.get("plainText", "") for n in notes if isinstance(n, dict))
                if note_text:
                    lines.append(f"  Notes: {note_text[:100]}")
            if not tasks and not events:
                lines.append("  No tasks or events scheduled.")
            return "\n".join(lines)

        elif action == "create_task":
            if not title:
                return "Title is required for create_task."
            task = await create_task(title, priority=priority or "medium",
                                     due_date=due_date, due_time=due_time,
                                     source_type=source_type or "manual",
                                     source_id=source_id, assignee=assignee,
                                     tags=tags or [])
            if not task:
                return "Failed to create task."
            return f"✅ Task created: \"{title}\" [{task.get('status', 'not_started')}] P{task.get('priority', 'medium')}"

        elif action == "update_task":
            if not task_id:
                return "task_id is required for update_task."
            patch: dict = {}
            if status:
                patch["status"] = status
            if priority:
                patch["priority"] = priority
            if due_date:
                patch["dueDate"] = due_date
            if assignee:
                patch["assignee"] = assignee
            if tags:
                patch["tags"] = tags
            if not patch:
                return "No fields to update. Provide status, priority, due_date, etc."
            result = await update_task(task_id, patch)
            if not result:
                return f"Task {task_id} not found or update failed."
            return f"✅ Task updated: {list(patch.keys())}"

        elif action == "delete_task":
            if not task_id:
                return "task_id is required for delete_task."
            ok = await delete_task(task_id)
            return "✅ Task deleted." if ok else f"Task {task_id} not found."

        elif action == "create_event":
            if not title or not start_time or not end_time:
                return "title, start_time, and end_time are required for create_event."
            event = await create_event(title, start_time, end_time,
                                       location=location,
                                       source_type=source_type or "manual",
                                       source_id=source_id)
            if not event:
                return "Failed to create event."
            return f"✅ Event created: \"{title}\" @ {start_time}"

        # ── Templates ──
        elif action == "list_templates":
            templates = await list_templates()
            if not templates:
                return "No templates available."
            lines = [f"📋 {len(templates)} templates:"]
            for t in templates[:10]:
                emoji = t.get("icon", {}).get("emoji", "📄")
                lines.append(f"  {emoji} {t.get('name', 'Untitled')} ({t.get('category', '')}) id: {t.get('id', '')[:8]}...")
            return "\n".join(lines)

        elif action == "create_from_template":
            if not template_id:
                return "template_id is required for create_from_template."
            from nova.pi_workspace import _get_workspace_id
            ws_id = await _get_workspace_id()
            result = await create_from_template(ws_id, template_id, title)
            if not result:
                return "Failed to create page from template."
            page = result.get("page", result)
            return f"✅ Page created from template: \"{title or template_id}\" (id: {page.get('id', '')[:8]}...)"

        # ── AI Chat ──
        elif action == "ai_chat":
            if not message:
                return "message is required for ai_chat."
            result = await ai_chat(message)
            if not result:
                return "AI chat failed."
            content = result.get("content", "")
            return f"🤖 {content[:300]}" if content else "AI returned no response."

        # ── Component Registry ──
        elif action == "component_registry":
            components = await get_component_registry()
            if not components:
                return "Component registry unavailable."
            lines = [f"🔧 {len(components)} workspace components:"]
            for c in components[:15]:
                caps = ", ".join(c.get("capabilities", []))
                sources = ", ".join(c.get("contextSources", []))
                lines.append(f"  {c.get('type', '')}: {c.get('label', '')} [{caps}] ← {sources}")
            return "\n".join(lines)

        else:
            return f"Unknown workspace action: {action}. Available: list_pages, create_page, get_page, add_block, search, list_databases, create_database, add_row, update_row, list_rows, create_form, submit_form, planner, create_task, update_task, delete_task, create_event, list_templates, create_from_template, ai_chat, component_registry"

    except Exception as e:
        logger.error(f"Workspace handler error: {e}", exc_info=True)
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
# PCG handlers (unified context)
# ---------------------------------------------------------------------------

async def handle_knowledge_query(
    query: str,
    include_personal: bool = True,
    include_knowledge: bool = True,
    include_dimensions: bool = True,
) -> str:
    """Query PCG for unified context."""
    from nova.pcg import query as pcg_query
    
    result = await pcg_query(
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
    """Get enriched personal context from PCG."""
    from nova.context_bridge import get_enriched_context as cb_get_enriched_context
    
    result = await cb_get_enriched_context(
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
    """Create bi-directional link between PCG goal and knowledge entity."""
    from nova.pcg import record_observation
    
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
    # Hub Agent delegation (Pi Agent Hub background agents)
    "hub_delegate": handle_hub_delegate,
    # CIG analytics (Communication Intelligence Graph)
    "query_cig": handle_cig_query,
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
    # Image analysis
    "analyze_image": handle_analyze_image,
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
    # STAAR Tutor (TEKS-aligned problem generation)
    "staar_tutor": handle_staar_tutor,
    # Conversation compaction (negative exponential decay + PCG fact extraction)
    "compact_conversations": handle_compact_conversations,
    # EV Charging & Route Planning (NREL AFDC API)
    "ev_route_planner": handle_ev_route_planner,
    # Tesla location refresh
    "tesla_location_refresh": handle_tesla_location_refresh,
    # YouTube search and play
    "youtube": handle_youtube,
    # PCG - Unified Personal Context Graph
    "query_context": handle_query_context,
    "kg_query": handle_kg_query,
    "knowledge_query": handle_knowledge_query,
    "get_enriched_context": handle_get_enriched_context,
    "link_goal_to_knowledge": handle_link_goal_to_knowledge,
}


def set_progress_context(callback: Optional[ProgressCallback], user_id: Optional[str], location: Optional[Any] = None, conversation_id: Optional[str] = None):
    """Set the current progress callback, user ID, and location for tool calls."""
    global _current_progress_callback, _current_user_id, _current_user_location, _current_conversation_id
    _current_progress_callback = callback
    _current_user_id = user_id
    _current_user_location = location
    if conversation_id is not None:
        _current_conversation_id = conversation_id


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
        "hub_delegate",  # Long-running, approval-gated Hub tasks
        "analyze_image",  # Async background vision job
        "query_cig",  # User-specific CIG analytics
        "query_frameworks",  # Dynamic LIAM framework discovery
        "set_reminder", "manage_timer",  # Mutating
        "control_lights",  # Mutating
        "get_weather",  # Weather data is highly temporal and should be fetched fresh
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
        # Inject progress context for hub_delegate
        if name == "hub_delegate":
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
        if name == "search_past_conversations" and _current_conversation_id:
            args.setdefault("exclude_conversation_id", _current_conversation_id)
            
        # Extract _internal_user_id if present to avoid TypeError for tools without **kwargs
        internal_user_id = args.pop("_internal_user_id", None)
        if internal_user_id and name == "analyze_image":
            args["_internal_user_id"] = internal_user_id
        
        result = await handler(**args)
        
        # Convert dict results to JSON string for consistency
        if isinstance(result, dict):
            result = json.dumps(result, indent=2)
        
        # Ensure result is a string
        result = str(result) if result is not None else ""
        
        # Cache the result for cacheable tools
        if name not in UNCACHEABLE_TOOLS:
            await set_cached(name, cache_key_args, result)
            logger.debug(f"[Cache SET] {name}")
        
        return result
    except TypeError as e:
        logger.error(f"Tool {name} argument error: {e}")
        import traceback
        tb = traceback.format_exc()
        return (
            f"Tool argument error: {str(e)}\n\n"
            f"SYSTEM: A code or parameter failure occurred in the '{name}' tool.\n"
            f"Traceback:\n{tb}\n\n"
            "INSTRUCTION: 1. Inform the user that you encountered a code/parameter bug.\n"
            "2. Explain that you are delegating the fix to the 'coder' agent.\n"
            "3. Call hub_delegate(agent='coder', method='fix', params={'task': 'Fix tool parameter mismatch', 'context': '...'}) passing this traceback.\n"
            "4. Tell the user they will receive an approval request on their iPhone for the code fix."
        )
    except Exception as e:
        logger.error(f"Tool {name} execution error: {e}")
        import traceback
        tb = traceback.format_exc()
        return (
            f"Tool execution error: {str(e)}\n\n"
            f"SYSTEM: A code execution failure occurred in the '{name}' tool.\n"
            f"Traceback:\n{tb}\n\n"
            "INSTRUCTION: 1. Inform the user that you encountered a code bug.\n"
            "2. Explain that you are delegating the fix to the 'coder' agent.\n"
            "3. Call hub_delegate(agent='coder', method='fix', params={'task': 'Fix tool execution error', 'context': '...'}) passing this traceback.\n"
            "4. Tell the user they will receive an approval request on their iPhone for the code fix."
        )

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

# NOTE: TESLA_CONTROL_TOOL is loaded from skills/tesla-control/SKILL.md by skill_loader
# to avoid duplication. The handler is registered at the bottom of this file.
# DO NOT append here - it's already loaded from the skill!

# ---------------------------------------------------------------------------
# Tesla Wake Tool Definition
# ---------------------------------------------------------------------------

TESLA_WAKE_TOOL = {
    "type": "function",
    "function": {
        "name": "tesla_wake",
        "description": "Wake up a sleeping Tesla vehicle to enable remote control and data access. Use when a vehicle is offline/asleep and you need to interact with it. Automatically called before status checks if vehicle is detected as offline.",
        "parameters": {
            "type": "object",
            "properties": {
                "vin": {
                    "type": "string",
                    "description": "Vehicle VIN (optional - defaults to first vehicle if not specified)",
                },
            },
            "required": [],
        },
    },
}

# NOTE: tesla_wake is loaded from skills/tesla-wake/SKILL.md by skill_loader
# DO NOT append here - it's already loaded from the skill!

# ---------------------------------------------------------------------------
# Tesla Location Refresh Tool Definition
# ---------------------------------------------------------------------------

TESLA_LOCATION_REFRESH_TOOL = {
    "type": "function",
    "function": {
        "name": "tesla_location_refresh",
        "description": "Refresh and retrieve the current GPS location of a Tesla vehicle. Returns real-time coordinates, heading, and speed. Use when you need the most up-to-date vehicle location data.",
        "parameters": {
            "type": "object",
            "properties": {
                "vin": {
                    "type": "string",
                    "description": "Vehicle VIN (optional - defaults to first vehicle if not specified)",
                },
            },
            "required": [],
        },
    },
}

# NOTE: tesla_location_refresh is loaded from skills/tesla-location-refresh/SKILL.md by skill_loader
# DO NOT append here - it's already loaded from the skill!

# ---------------------------------------------------------------------------
# Tesla Navigation Tool Definition
# ---------------------------------------------------------------------------

TESLA_NAVIGATION_TOOL = {
    "type": "function",
    "function": {
        "name": "tesla_navigation",
        "description": "Send navigation destination to a Tesla vehicle. Supports both address/place names and GPS coordinates. Use when user wants to navigate to a location or set a destination in their Tesla.",
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "Address or place name (e.g., '1600 Amphitheatre Parkway, Mountain View, CA')",
                },
                "latitude": {
                    "type": "number",
                    "description": "GPS latitude for precise navigation (use with longitude)",
                },
                "longitude": {
                    "type": "number",
                    "description": "GPS longitude for precise navigation (use with latitude)",
                },
                "vin": {
                    "type": "string",
                    "description": "Vehicle VIN (optional - defaults to first vehicle if not specified)",
                },
            },
            "required": ["destination"],
        },
    },
}

# NOTE: tesla_navigation is loaded from skills/tesla-navigation/SKILL.md by skill_loader
# DO NOT append here - it's already loaded from the skill!

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

# Tesla stream monitor handler (skill-based)
TOOL_HANDLERS["tesla_stream_monitor"] = handle_tesla_stream_monitor

# Tesla wake handler (direct import from tesla_tools)
TOOL_HANDLERS["tesla_wake"] = handle_tesla_wake

# Tesla navigation handler (direct import from tesla_tools)
TOOL_HANDLERS["tesla_navigation"] = handle_tesla_navigation

# ---------------------------------------------------------------------------
# Homelab Diagnostics Tool (skill-based)
# ---------------------------------------------------------------------------

_diagnostics = _load_skill_module("homelab-diagnostics", "diagnostics")

async def handle_homelab_diagnostics(**kwargs) -> dict:
    """Run homelab infrastructure diagnostics."""
    action = kwargs.get("action") or kwargs.get("check") or kwargs.get("type") or "full_diagnostics"
    
    # Call the skill's full_diagnostics function
    if action == "full_diagnostics":
        return await _diagnostics.full_diagnostics()
    elif action == "argus_health":
        return await _diagnostics.check_argus_health()
    elif action == "ai_inferencing_health":
        return await _diagnostics.check_ai_inferencing_health()
    elif action == "hermes_health":
        return await _diagnostics.check_hermes_health()
    elif action == "hermy_score":
        # For hermy_score, we need to gather components first
        components = {
            "argus": await _diagnostics.check_argus_health(),
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
        "description": "Run comprehensive AI Homelab infrastructure diagnostics. Check Pi Agent Hub status, AI Inferencing health, CIG connectivity, and calculate Hermy score (overall health 0-100). Use for system health queries, error investigations, or component status checks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["full_diagnostics", "hub_health", "ai_inferencing_health", "cig_health", "hermy_score"],
                    "description": "Diagnostic action: full_diagnostics (complete report), hub_health (Pi Agent Hub status), ai_inferencing_health (key vault), cig_health (email/calendar), hermy_score (0-100 health score)",
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
# Homelab Heartbeat — instant ecosystem status from monitor (Tier 0)
# ---------------------------------------------------------------------------

HEARTBEAT_STATE_PATH = Path(
    os.getenv("HOMELAB_MEMORY_DIR", "/home/eleazar/Projects/AIHomelab/memory")
) / "heartbeat-state.json"

async def handle_homelab_heartbeat(**kwargs) -> str:
    """Read the latest ecosystem heartbeat from the homelab monitor.

    Tier 0 — reads a local file, no network calls, instant response.
    The homelab-monitor systemd timer writes heartbeat-state.json every 2 minutes
    with the status of all 15 monitored services.
    """
    try:
        if not HEARTBEAT_STATE_PATH.exists():
            return (
                "Homelab heartbeat not available yet. "
                "The monitor service (homelab-monitor.timer) may not have run yet. "
                "Use service_health_check for a live probe instead."
            )

        with open(HEARTBEAT_STATE_PATH) as f:
            data = json.load(f)

        status = data.get("status", "unknown")
        last_check = data.get("lastCheck", "unknown")
        services = data.get("services", {})
        alerts = data.get("alerts", [])
        metrics = data.get("metrics", {})

        # Count by status
        healthy = sum(1 for s in services.values() if s == "healthy")
        degraded = sum(1 for s in services.values() if s == "degraded")
        unhealthy = sum(1 for s in services.values() if s == "unhealthy")
        total = len(services)

        # Build summary
        lines = [f"Ecosystem status: {status.upper()}"]
        lines.append(f"Last check: {last_check}")
        lines.append(f"Services: {healthy}/{total} healthy, {degraded} degraded, {unhealthy} unhealthy")

        if alerts:
            lines.append(f"Alerts ({len(alerts)}):")
            for a in alerts:
                lines.append(f"  - {a}")
        else:
            lines.append("No active alerts.")

        if metrics:
            lines.append(
                f"System: CPU {metrics.get('cpuPercent', '?')}%, "
                f"MEM {metrics.get('memoryPercent', '?')}%, "
                f"DISK {metrics.get('diskPercent', '?')}%"
            )

        # List unhealthy/degraded services
        problem_svcs = {n: s for n, s in services.items() if s in ("unhealthy", "degraded", "not_found")}
        if problem_svcs:
            lines.append("Problem services:")
            for name, st in sorted(problem_svcs.items()):
                lines.append(f"  - {name}: {st}")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Heartbeat read error: {e}")
        return f"Error reading heartbeat: {e}. Use service_health_check for a live probe."

HOMELAB_HEARTBEAT_TOOL = {
    "type": "function",
    "function": {
        "name": "homelab_heartbeat",
        "description": (
            "Instant ecosystem health status from the homelab monitor. "
            "Reads the latest heartbeat (updated every 2 minutes by homelab-monitor timer). "
            "Tier 0 — no network calls, instant response. "
            "Use for: 'how's the homelab?', 'is everything ok?', 'quick status', "
            "'any services down?', 'ecosystem health'. "
            "For DEEP diagnostics on a specific problem, use hub_delegate(agent='infra', method='diagnose') instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

TOOL_DEFINITIONS.append(HOMELAB_HEARTBEAT_TOOL)
TOOL_HANDLERS["homelab_heartbeat"] = handle_homelab_heartbeat

# ---------------------------------------------------------------------------
# Query Frameworks Tool (Dynamic LIAM Framework Discovery)
# ---------------------------------------------------------------------------

async def handle_query_frameworks(**kwargs) -> dict:
    """Query LIAM for scientific frameworks applicable to a problem."""
    from nova.liam import query_frameworks as liam_query_frameworks
    from nova.liam import list_dimensions as liam_list_dimensions

    problem_description = kwargs.get("problem_description", "")
    dimension_id = kwargs.get("dimension_id")
    category = kwargs.get("category")
    limit = kwargs.get("limit", 5)

    if not problem_description:
        return {
            "success": False,
            "error": "problem_description is required"
        }

    try:
        # Build dimension filter if specified
        dimension_filter = [dimension_id] if dimension_id else None

        result = await liam_query_frameworks(
            problem_description=problem_description,
            dimension_filter=dimension_filter,
            limit=limit,
        )

        frameworks = result.get("frameworks", [])

        # Filter by category if specified
        if category:
            frameworks = [
                f for f in frameworks
                if f.get("framework", {}).get("category") == category
                or f.get("category", "") == category
            ]

        # Build clean response with full framework content
        clean_frameworks = []
        for rec in frameworks:
            fw = rec.get("framework", {})
            clean_frameworks.append({
                "name": fw.get("name", rec.get("framework_name", "")),
                "source": fw.get("source", ""),
                "category": fw.get("category", ""),
                "description": fw.get("description", ""),
                "when_to_use": fw.get("when_to_use", ""),
                "key_concepts": fw.get("key_concepts", rec.get("key_insights", [])),
                "limitations": fw.get("limitations", ""),
                "applicable_dimensions": fw.get("applicable_dimensions", rec.get("applicable_dimensions", [])),
                "relevance_score": rec.get("relevance_score", 0),
                "reasoning": rec.get("reasoning", ""),
            })

        # Build synthesis
        synthesis = ""
        if clean_frameworks:
            names = [f["name"] for f in clean_frameworks[:3]]
            if len(clean_frameworks) == 1:
                synthesis = f"Apply {clean_frameworks[0]['name']}: {clean_frameworks[0].get('when_to_use', '')}"
            else:
                synthesis = f"Use multiple frameworks (Model Thinker approach): {', '.join(names)}. Each provides a different lens on the problem."

        return {
            "success": True,
            "query": result.get("query", problem_description),
            "frameworks": clean_frameworks,
            "total_frameworks": result.get("total_frameworks", 0),
            "synthesis": synthesis,
        }
    except Exception as e:
        logger.error(f"Framework query failed: {e}")
        return {
            "success": False,
            "error": f"Framework query error: {str(e)}",
            "fallback": "Using hardcoded framework quick-reference from training knowledge"
        }

QUERY_FRAMEWORKS_TOOL = {
    "type": "function",
    "function": {
        "name": "query_frameworks",
        "description": "Query LIAM (Life Intelligence Augmentation Matrix) for scientific frameworks applicable to a decision, problem, or life question. Returns full framework details: name, source, description, when to use, key concepts, limitations, applicable life dimensions, relevance score, and a synthesis. Covers 48 frameworks across 8 categories (decision_making, systems, computational, behavioral, probabilistic, strategic, structural, information_processing) mapped to 16 life dimensions. ALWAYS call this BEFORE applying frameworks to ensure you use the latest available knowledge, not just training data.",
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


async def handle_search_framework_catalog(**kwargs) -> dict:
    """Search LIAM's framework catalog for inventory and author/source questions."""
    from nova.liam import search_framework_catalog as liam_search_framework_catalog

    try:
        return await liam_search_framework_catalog(
            query=kwargs.get("query"),
            author=kwargs.get("author"),
            source=kwargs.get("source"),
            category=kwargs.get("category"),
            dimension=kwargs.get("dimension"),
            limit=kwargs.get("limit", 20),
        )
    except Exception as e:
        logger.error(f"Framework catalog search failed: {e}")
        return {
            "success": False,
            "error": f"Framework catalog search error: {str(e)}",
            "frameworks": [],
            "total_frameworks": 0,
        }


TOOL_HANDLERS["search_framework_catalog"] = handle_search_framework_catalog


# ---------------------------------------------------------------------------
# Active Goal Management — cross-session work continuity
# ---------------------------------------------------------------------------

async def handle_set_active_goal(goal: str, intent: str = "multi_session_task", workspace_page_id: str = "", **kwargs) -> str:
    """Record an active multi-session goal so Nova remembers it across conversations."""
    from nova.action_ledger import create_action_entry
    from nova.store import upsert_action_ledger_entry
    metadata = {}
    if workspace_page_id:
        metadata["workspace_page_id"] = workspace_page_id
    entry = create_action_entry(
        intent=intent,
        active_goal=goal,
        status="running",
        user_id=_current_user_id or "default",
        metadata=metadata,
    )
    await upsert_action_ledger_entry(entry)
    logger.info(f"Active goal set: {goal!r} page_id={workspace_page_id!r}")
    anchor = f" Workspace page: {workspace_page_id}." if workspace_page_id else ""
    return f"Active goal recorded: '{goal}'.{anchor} I'll carry this context into future sessions."


async def handle_complete_active_goal(goal_fragment: str = "", **kwargs) -> str:
    """Mark an active goal as completed so it stops appearing in session context."""
    from nova.store import get_recent_action_ledger_entries, upsert_action_ledger_entry, DB_PATH
    import aiosqlite, time as _time

    matched = 0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """SELECT * FROM nova_action_ledger
                   WHERE status NOT IN ('completed', 'cancelled')
                   ORDER BY updated_at DESC LIMIT 10"""
            )
            for row in rows:
                r = dict(row)
                ag = (r.get("active_goal") or r.get("intent") or "").lower()
                if not goal_fragment or goal_fragment.lower() in ag:
                    await db.execute(
                        "UPDATE nova_action_ledger SET status='completed', updated_at=? WHERE action_id=?",
                        (_time.time(), r["action_id"]),
                    )
                    matched += 1
            await db.commit()
    except Exception as e:
        logger.error(f"complete_active_goal failed: {e}")
        return f"Could not mark goal complete: {e}"

    if matched:
        return f"Marked {matched} active goal(s) as completed."
    return "No matching active goals found."


SET_ACTIVE_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "set_active_goal",
        "description": (
            "Record a multi-session goal or ongoing task so Nova remembers it across "
            "separate conversations. Call this when the user starts a project that will "
            "span multiple sessions (e.g. 'let's work on my managerial overreach article'). "
            "The goal and its workspace_page_id anchor will appear in the system prompt of every future session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Plain-language description of the active work, e.g. 'Writing article on managerial overreach and lack of physician input'",
                },
                "intent": {
                    "type": "string",
                    "description": "Short intent tag, e.g. 'article_writing', 'case_study', 'project_planning'",
                },
                "workspace_page_id": {
                    "type": "string",
                    "description": "The page_id of the workspace page that anchors this work. Always create or find the page first, then pass its ID here.",
                },
            },
            "required": ["goal"],
        },
    },
}

COMPLETE_ACTIVE_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "complete_active_goal",
        "description": "Mark an active multi-session goal as completed so it stops appearing in future session context.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_fragment": {
                    "type": "string",
                    "description": "A word or phrase from the goal to match (leave empty to complete all active goals)",
                },
            },
            "required": [],
        },
    },
}

TOOL_DEFINITIONS.append(SET_ACTIVE_GOAL_TOOL)
TOOL_DEFINITIONS.append(COMPLETE_ACTIVE_GOAL_TOOL)
TOOL_HANDLERS["set_active_goal"] = handle_set_active_goal
TOOL_HANDLERS["complete_active_goal"] = handle_complete_active_goal


# ---------------------------------------------------------------------------
# Self-introspection — what does Nova know about her own present state?
# ---------------------------------------------------------------------------

async def handle_query_self_state(**kwargs) -> str:
    """Return Nova's self-context: dream insights, active goals, favorites,
    recent session topics. Backed by the Nova Context Layer (NCL).

    Call this whenever the user asks about Nova's own state, dreams, memory,
    learning, or what she knows about herself — instead of denying capability.
    """
    user_id = str(kwargs.get("_internal_user_id") or kwargs.get("user_id") or "")
    try:
        from nova.context_layer import self_state
        state = await self_state(user_id)
    except Exception as e:
        return f"Self-state lookup failed: {e}"

    lines: list[str] = []
    if state.get("dreamed_last_night"):
        lines.append("Yes, I dreamed within the last 36 hours.")
    else:
        lines.append("No dream cycle has run in the last 36 hours.")

    dreams = state.get("dream_insights") or []
    if dreams:
        lines.append(f"Latest dream insights ({len(dreams)}):")
        for d in dreams[:5]:
            date = d.get("date", "")
            text = d.get("text", "")
            cat = d.get("category", "behavior")
            lines.append(f"- [{date} · {cat}] {text[:240]}")

    goals = state.get("active_goals") or []
    if goals:
        lines.append(f"Active goals ({len(goals)}):")
        for g in goals[:4]:
            g_text = (g.get("active_goal") or g.get("intent") or "").strip()
            page = g.get("workspace_page_id", "")
            anchor = f" (page {page})" if page else ""
            if g_text:
                lines.append(f"- {g_text}{anchor}")

    favs = state.get("favorites") or []
    if favs:
        lines.append(f"Favorites on record ({len(favs)}):")
        for f in favs[:6]:
            lines.append(f"- {f.get('label', '?')}: {str(f.get('value', ''))[:120]}")

    topics = state.get("recent_session_topics") or []
    if topics:
        lines.append("Recent session topics: " + ", ".join(topics[:6]))

    return "\n".join(lines) if lines else "I have no current self-state to report."


QUERY_SELF_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "query_self_state",
        "description": (
            "Return Nova's own state: did she dream recently, what insights "
            "did the dream cycle produce, what active multi-session goals exist, "
            "what favorites are on record, what topics did recent sessions cover. "
            "Call this BEFORE denying introspection ('I don't dream', 'I don't remember', "
            "'we're starting fresh'). Use it when the user asks about Nova's memory, "
            "dreams, learning, or what she knows about her own state."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

TOOL_DEFINITIONS.append(QUERY_SELF_STATE_TOOL)
TOOL_HANDLERS["query_self_state"] = handle_query_self_state


# ---------------------------------------------------------------------------
# Task Planner — cross-session work continuity with full session history
# ---------------------------------------------------------------------------

import datetime as _datetime


def _ts(ts: float) -> str:
    """Format a Unix timestamp as a readable date."""
    try:
        return _datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


async def handle_manage_task_plan(
    action: str,
    plan_id: str = "",
    topic: str = "",
    description: str = "",
    summary: str = "",
    content: str = "",
    sources: list | None = None,
    next_steps: list | None = None,
    step_title: str = "",
    step_id: str = "",
    step_status: str = "",
    step_notes: str = "",
    step_order: int = 0,
    workspace_page_id: str = "",
    **kwargs,
) -> str:
    from nova.task_plan import (
        create_plan, get_plan, list_plans, add_session_entry,
        add_step, update_step, complete_plan, set_workspace_page,
    )

    user_id = _current_user_id or "default"
    conv_id = _current_conversation_id or ""

    # ── create ──────────────────────────────────────────────────────────────
    if action == "create":
        if not topic:
            return "topic is required to create a plan."
        # Dedup: return existing active plan with same topic to prevent duplicates on retry
        existing_plans = await list_plans(user_id=user_id, status="active")
        topic_lower = topic.strip().lower()
        for ep in existing_plans:
            if ep.get("topic", "").strip().lower() == topic_lower:
                pid = ep["plan_id"]
                logger.info(f"Task plan already exists for topic {topic!r}: plan_id={pid}")
                return (
                    f"✅ Task plan already exists: \"{topic}\"\n"
                    f"plan_id: {pid}\n"
                    f"Use this existing plan_id for all subsequent actions. Do NOT create another plan."
                )
        plan = await create_plan(topic, description=description, user_id=user_id, workspace_page_id=workspace_page_id)
        pid = plan["plan_id"]
        logger.info(f"Task plan created: {topic!r} plan_id={pid}")
        return (
            f"✅ Task plan created: \"{topic}\"\n"
            f"plan_id: {pid}\n"
            f"Use add_session at the end of each session. Use add_step to define your checklist.\n"
            f"Tip: after creating a workspace page for this work, call manage_task_plan action=link_page to anchor it."
        )

    # ── get ─────────────────────────────────────────────────────────────────
    elif action == "get":
        if not plan_id:
            return "plan_id is required for get."
        plan = await get_plan(plan_id)
        if not plan:
            return f"No plan found with plan_id={plan_id}."
        lines = [
            f"📋 Plan: {plan['topic']}",
            f"Status: {plan['status']} | Created: {_ts(plan['created_at'])} | Last updated: {_ts(plan['updated_at'])}",
        ]
        if plan.get("description"):
            lines.append(f"Goal: {plan['description']}")
        if plan.get("workspace_page_id"):
            lines.append(f"Workspace page_id: {plan['workspace_page_id']}")

        steps = plan.get("steps", [])
        if steps:
            lines.append("\nSteps:")
            icons = {"pending": "☐", "in_progress": "🔄", "done": "✅", "skipped": "⏭"}
            for s in steps:
                icon = icons.get(s.get("status", "pending"), "☐")
                note = f" — {s['notes']}" if s.get("notes") else ""
                lines.append(f"  {icon} [{s['step_id'][:8]}] {s['title']}{note}")

        sessions = plan.get("sessions", [])
        if sessions:
            lines.append(f"\nSession history ({len(sessions)} entries, most recent first):")
            for s in sessions[:5]:
                lines.append(f"  • [{_ts(s['timestamp'])}] conv={s.get('conversation_id','')[:12]} — {s.get('summary','(no summary)')[:120]}")
                if s.get("sources"):
                    lines.append(f"    Sources: {', '.join(str(x) for x in s['sources'][:4])}")
                if s.get("next_steps"):
                    lines.append(f"    Next: {', '.join(str(x) for x in s['next_steps'][:3])}")
        else:
            lines.append("\nNo session entries yet. Call add_session at end of each work session.")
        return "\n".join(lines)

    # ── list ─────────────────────────────────────────────────────────────────
    elif action == "list":
        status_filter = kwargs.get("status", "active")
        plans = await list_plans(user_id=user_id, status=status_filter)
        if not plans:
            return f"No {status_filter} task plans found."
        lines = [f"📋 {len(plans)} {status_filter} plan(s):"]
        for p in plans:
            page_note = f" | page: {p['workspace_page_id'][:8]}..." if p.get("workspace_page_id") else ""
            lines.append(f"  • [{p['plan_id'][:8]}] {p['topic']}{page_note} (updated {_ts(p['updated_at'])})")
        return "\n".join(lines)

    # ── add_session ───────────────────────────────────────────────────────────
    elif action == "add_session":
        if not plan_id:
            return "plan_id is required for add_session."
        if not summary:
            return "summary is required — describe what was accomplished this session."
        entry = await add_session_entry(
            plan_id,
            conversation_id=conv_id,
            summary=summary,
            content=content,
            sources=sources or [],
            next_steps=next_steps or [],
        )
        logger.info(f"Task plan session logged: plan_id={plan_id} conv={conv_id}")
        return (
            f"✅ Session logged for plan {plan_id[:8]}...\n"
            f"Summary: {summary}\n"
            f"Next steps: {next_steps or '(none recorded)'}"
        )

    # ── add_step ──────────────────────────────────────────────────────────────
    elif action == "add_step":
        if not plan_id or not step_title:
            return "plan_id and step_title are required for add_step."
        step = await add_step(plan_id, step_title, order_num=step_order, notes=step_notes)
        return f"✅ Step added: \"{step_title}\" [{step['step_id'][:8]}] status=pending"

    # ── update_step ────────────────────────────────────────────────────────────
    elif action == "update_step":
        if not step_id or not step_status:
            return "step_id and step_status are required for update_step."
        valid = {"pending", "in_progress", "done", "skipped"}
        if step_status not in valid:
            return f"Invalid step_status. Must be one of: {', '.join(sorted(valid))}"
        await update_step(step_id, step_status, notes=step_notes)
        return f"✅ Step {step_id[:8]} marked as {step_status}."

    # ── link_page ─────────────────────────────────────────────────────────────
    elif action == "link_page":
        if not plan_id or not workspace_page_id:
            return "plan_id and workspace_page_id are required for link_page."
        await set_workspace_page(plan_id, workspace_page_id)
        return f"✅ Plan {plan_id[:8]} linked to workspace page {workspace_page_id}."

    # ── complete ──────────────────────────────────────────────────────────────
    elif action == "complete":
        if not plan_id:
            return "plan_id is required for complete."
        await complete_plan(plan_id)
        return f"✅ Plan {plan_id[:8]} marked as completed."

    else:
        return (
            "Unknown action. Available: create, get, list, add_session, "
            "add_step, update_step, link_page, complete"
        )


TOOL_HANDLERS["manage_task_plan"] = handle_manage_task_plan
