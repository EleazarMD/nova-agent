"""
Nova Session Mirror — SSE event stream for Tesla Companion Mode.

Provides a lightweight Server-Sent Events endpoint that the Tesla browser
subscribes to. It mirrors all conversation events from an active iPhone
voice session in real-time.

Architecture:
  bot.py pipeline → publish_event() → in-memory queue → SSE stream → Tesla browser

Port: 18804 (Nova port block 18800-18809)
"""

import asyncio
import json
import os
import secrets
import time
from typing import Optional

from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from sse_starlette.sse import EventSourceResponse

MIRROR_PORT = 18804

app = FastAPI(title="Nova Session Mirror")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory pub/sub: one queue per subscriber (not per user)
# Multiple Tesla browsers can subscribe to the same user's session.
# ---------------------------------------------------------------------------

_subscribers: dict[str, list[asyncio.Queue]] = {}  # user_id → [queue, ...]
_active_sessions: dict[str, dict] = {}  # user_id → session metadata
_event_history: dict[str, list] = {}  # user_id → [event, ...]
_session_tokens: dict[str, str] = {}  # user_id → session_token (for privacy)
_MAX_HISTORY = 20  # Keep last 20 events per user

_api_keys: set[str] = set()  # Valid API keys for dashboard access

def add_api_key(api_key: str):
    """Add a valid API key for dashboard authentication."""
    _api_keys.add(api_key)

# Initialize API keys from environment on module load
_dashboard_api_key = os.getenv("DASHBOARD_API_KEY") or "dashboard-internal-api-key-2024"
print(f"[Mirror INIT] Dashboard API key: {_dashboard_api_key[:20]}...")
_api_keys.add(_dashboard_api_key)
print(f"[Mirror INIT] Added API key to set, total keys: {len(_api_keys)}")
logger.info(f"[Mirror] Initialized with dashboard API key")

def validate_api_key(api_key: str) -> bool:
    """Validate an API key."""
    return api_key in _api_keys

def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def _get_queues(user_id: str) -> list:
    """Get all subscriber queues for a user."""
    return _subscribers.get(user_id, [])


async def publish_event(user_id: str, event_type: str, data: dict):
    """Publish an event to all subscribers watching this user's session.

    Called from bot.py alongside _send_server_msg().
    Non-blocking — drops events if no subscribers.
    """
    event = {"type": event_type, "data": data, "ts": time.time()}
    
    # Store important events in history for late-joining subscribers
    should_store = event_type in ("user_transcript", "assistant_text")
    if event_type in ("user_transcript", "assistant_text"):
        # Store all transcripts, not just final ones
        if user_id not in _event_history:
            _event_history[user_id] = []
        _event_history[user_id].append(event)
        # Trim to max history
        if len(_event_history[user_id]) > _MAX_HISTORY:
            _event_history[user_id] = _event_history[user_id][-_MAX_HISTORY:]
        logger.info(f"[Mirror] Stored {event_type} in history (total={len(_event_history[user_id])})")
    
    queues = _get_queues(user_id)
    if not queues:
        logger.debug(f"[Mirror] No subscribers for {user_id}, buffering {event_type}")
        return

    logger.info(f"[Mirror] Publishing {event_type} to {len(queues)} subscribers: {str(data)[:80]}")
    dead = []
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)

    # Clean up dead queues
    if dead:
        for q in dead:
            try:
                _subscribers[user_id].remove(q)
            except (KeyError, ValueError):
                pass


async def mark_session_active(user_id: str, conversation_id: str, client_type: str, vehicle_id: str = None):
    """Mark a voice session as active (called on WebRTC connect).
    
    Args:
        user_id: The user's ID
        conversation_id: The conversation ID
        client_type: Client type (e.g., 'ios')
        vehicle_id: Optional vehicle ID for session binding (privacy)
    
    Returns:
        Session token for authenticating browser subscribers
    """
    # Generate unique session token for this session
    session_token = generate_session_token()
    _session_tokens[user_id] = session_token
    
    _active_sessions[user_id] = {
        "conversation_id": conversation_id,
        "client_type": client_type,
        "vehicle_id": vehicle_id,
        "session_token": session_token,
        "started_at": time.time(),
    }
    await publish_event(user_id, "session_start", {
        "conversation_id": conversation_id,
        "client_type": client_type,
        "vehicle_id": vehicle_id,
    })
    logger.info(f"[Mirror] Session active: user={user_id}, conv={conversation_id}, vehicle={vehicle_id}")
    return session_token


async def mark_session_inactive(user_id: str):
    """Mark a user's voice session as ended."""
    _active_sessions.pop(user_id, None)
    _event_history.pop(user_id, None)  # Clear history for new session
    _session_tokens.pop(user_id, None)  # Invalidate session token
    await publish_event(user_id, "session_end", {})
    logger.info(f"[Mirror] Session ended: user={user_id}")


# ---------------------------------------------------------------------------
# SSE Endpoints
# ---------------------------------------------------------------------------

@app.get("/mirror/{user_id}/stream")
async def mirror_stream(
    user_id: str, 
    token: Optional[str] = Query(None), 
    vehicle_id: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """SSE stream of all conversation events for a user's active session.

    The Tesla browser subscribes to this endpoint. Events are the same
    ones sent to the iPhone via RTVI, just reformatted for SSE.
    
    Authentication (in order of priority):
    1. API key header or query param (for dashboard/server access)
    2. Session token (for browser companion mode)
    3. No auth if no active session (allows pre-connection)
    """
    # Check API key first (dashboard authentication) - header or query param
    effective_api_key = x_api_key or api_key
    if effective_api_key and _api_keys and effective_api_key in _api_keys:
        logger.info(f"[Mirror] Dashboard API key accepted for user={user_id}")
    else:
        # Privacy validation: check session token if session exists
        session = _active_sessions.get(user_id)
        if session:
            expected_token = session.get("session_token")
            expected_vehicle = session.get("vehicle_id")
            
            # If session has a token, require it for subscription
            if expected_token and token != expected_token:
                logger.warning(f"[Mirror] Rejected subscriber: invalid token for user={user_id}")
                return JSONResponse(
                    status_code=403,
                    content={"error": "Invalid session token", "message": "Session token mismatch"}
                )
            
            # If session is bound to a vehicle, require matching vehicle_id
            if expected_vehicle and vehicle_id and vehicle_id != expected_vehicle:
                logger.warning(f"[Mirror] Rejected subscriber: vehicle mismatch for user={user_id} (expected={expected_vehicle}, got={vehicle_id})")
                return JSONResponse(
                    status_code=403,
                    content={"error": "Vehicle mismatch", "message": "This session is bound to a different vehicle"}
                )
    
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    # Register subscriber
    if user_id not in _subscribers:
        _subscribers[user_id] = []
    _subscribers[user_id].append(queue)
    logger.info(f"[Mirror] Subscriber connected: user={user_id}, vehicle={vehicle_id}, total={len(_subscribers[user_id])}")

    # Send current session state if active
    if user_id in _active_sessions:
        session = _active_sessions[user_id]
        await queue.put({
            "type": "session_start",
            "data": session,
            "ts": time.time(),
        })
    
    # Replay event history for late-joining subscribers
    if user_id in _event_history:
        history = _event_history[user_id]
        logger.info(f"[Mirror] Replaying {len(history)} events to new subscriber")
        for event in history:
            await queue.put(event)

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event["type"],
                        "data": json.dumps(event["data"]),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive to prevent connection timeout
                    yield {"event": "keepalive", "data": ""}
        except asyncio.CancelledError:
            pass
        finally:
            # Unregister subscriber
            try:
                _subscribers[user_id].remove(queue)
                if not _subscribers[user_id]:
                    del _subscribers[user_id]
            except (KeyError, ValueError):
                pass
            logger.info(f"[Mirror] Subscriber disconnected: user={user_id}")

    return EventSourceResponse(event_generator())


@app.get("/mirror/{user_id}/status")
async def session_status(user_id: str, include_token: bool = False):
    """Check if a user has an active voice session.
    
    Args:
        include_token: If true, include session_token in response (for iOS app to share with browser)
    """
    session = _active_sessions.get(user_id)
    if session:
        result = {
            "active": True,
            "conversation_id": session["conversation_id"],
            "client_type": session["client_type"],
            "vehicle_id": session.get("vehicle_id"),
            "started_at": session["started_at"],
            "subscribers": len(_get_queues(user_id)),
        }
        # Only include token if explicitly requested (for iOS app)
        if include_token:
            result["session_token"] = session.get("session_token")
        return result
    return {"active": False}


@app.get("/mirror/active-sessions")
async def active_sessions():
    """List all users with active voice sessions."""
    return {
        "sessions": {
            uid: {
                **meta,
                "subscribers": len(_get_queues(uid)),
            }
            for uid, meta in _active_sessions.items()
        }
    }


@app.post("/mirror/{user_id}/control/mute")
async def mute_user(user_id: str, x_api_key: str = Header(None, alias="X-API-Key")):
    """Signal to iOS app to mute microphone."""
    if x_api_key and x_api_key not in _api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    await publish_event(user_id, "control_mute", {"muted": True})
    logger.info(f"[Mirror] Mute command sent: user={user_id}")
    return {"status": "ok", "command": "mute"}


@app.post("/mirror/{user_id}/control/unmute")
async def unmute_user(user_id: str, x_api_key: str = Header(None, alias="X-API-Key")):
    """Signal to iOS app to unmute microphone."""
    if x_api_key and x_api_key not in _api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    await publish_event(user_id, "control_unmute", {"muted": False})
    logger.info(f"[Mirror] Unmute command sent: user={user_id}")
    return {"status": "ok", "command": "unmute"}


@app.post("/mirror/{user_id}/control/{command}")
async def send_control_command(
    user_id: str, 
    command: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Generic control command endpoint for dashboard → iPhone communication.
    
    Supported commands: mute, unmute, stop_listening, start_listening
    """
    if x_api_key and x_api_key not in _api_keys:
        raise HTTPException(status_code=403, detail="Invalid API key")
    
    valid_commands = {"mute", "unmute", "stop_listening", "start_listening"}
    if command not in valid_commands:
        raise HTTPException(status_code=400, detail=f"Invalid command. Valid: {valid_commands}")
    
    event_type = f"control_{command}"
    await publish_event(user_id, event_type, {"command": command})
    logger.info(f"[Mirror] Control command sent: user={user_id}, command={command}")
    return {"status": "ok", "command": command}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nova-mirror",
        "active_sessions": len(_active_sessions),
        "total_subscribers": sum(len(qs) for qs in _subscribers.values()),
    }
