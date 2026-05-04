"""
Nova Agent — Text Chat HTTP API (Port 18803)

Provides HTTP-based chat endpoint for Dashboard and web clients.
Uses the same LLM (MiniMax M2.5 via AI Gateway) and tools as the voice agent,
but without WebRTC/audio processing.

This is separate from port 18800 (iOS WebRTC voice) to avoid conflicts.

Port Assignments:
- 18800: iOS WebRTC voice (existing)
- 18801: Webhooks (existing)
- 18802: HTTPS proxy via Tailscale Serve (existing)
- 18803: Text chat HTTP API (this module)
"""

import asyncio
import json
import os
import time
import uuid
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from nova.store import init_db, get_or_create_session, append_turn, get_history, _sync_message_to_backend, ensure_backend_conversation, get_session_metadata, update_session_metadata_key
from nova.prompt import build_system_prompt
from nova.tools import TOOL_DEFINITIONS, dispatch_tool, reset_conversation_search_count, set_progress_context
from nova.turn_orchestrator import STATE_METADATA_KEY, TurnState, decide_turn, execute_turn_plan_result, get_orchestrator_metrics, turn_state_from_metadata, turn_state_to_metadata_value

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEXT_CHAT_PORT = int(os.environ.get("NOVA_TEXT_PORT", "18803"))
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
LLM_MODEL = os.environ.get("LLM_MODEL", "minimax-m2.5")

# Tool names for prompt builder
TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS if "function" in t]

# Max conversation turns to restore from DB
MAX_HISTORY_TURNS = 40
_turn_states: dict[str, TurnState] = {}


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    user_id: str = "dashboard"
    conversation_id: Optional[str] = None
    stream: bool = False


class ChatResponse(BaseModel):
    response: str
    conversation_id: str
    model: str
    usage: Optional[dict] = None
    tool_calls: Optional[list] = None
    citations: Optional[list] = None


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Nova Text Chat API",
    description="HTTP-based chat endpoint for Dashboard (port 18803)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    """Initialize database, cache, warming service, and orchestrator on startup."""
    await init_db()
    # Initialize enhanced cache layer
    from nova.cache import init_cache
    await init_cache()
    # Initialize proactive warming service
    from nova.warming import init_warming_service
    from nova.tools import dispatch_tool
    await init_warming_service(dispatch_tool)
    # Initialize cache orchestrator
    from nova.cache_orchestrator import init_orchestrator
    await init_orchestrator()
    logger.info(f"Nova Text Chat API starting on port {TEXT_CHAT_PORT}")


@app.get("/health")
async def health():
    """Health check endpoint."""
    from nova.cache import get_cache_stats
    return {
        "status": "ok",
        "service": "nova-text-chat",
        "port": TEXT_CHAT_PORT,
        "model": LLM_MODEL,
        "cache": get_cache_stats(),
    }


@app.get("/cache/stats")
async def cache_stats():
    """Get tool result cache statistics."""
    from nova.cache import get_cache_stats
    return get_cache_stats()


@app.post("/cache/clear")
async def cache_clear():
    """Clear the tool result cache."""
    from nova.cache import clear_cache
    count = await clear_cache()
    return {"cleared": count, "status": "ok"}


@app.post("/cache/invalidate/{tool_name}")
async def cache_invalidate(tool_name: str):
    """Invalidate cache entries for a specific tool."""
    from nova.cache import invalidate_cache
    count = await invalidate_cache(tool_name)
    return {"invalidated": count, "tool": tool_name, "status": "ok"}


@app.get("/cache/warming")
async def cache_warming_candidates():
    """Get queries that would be pre-warmed at current time based on learned patterns."""
    from nova.cache import get_warming_candidates
    return {"candidates": get_warming_candidates()}


@app.post("/cache/persist")
async def cache_persist():
    """Manually trigger cache persistence to SQLite."""
    from nova.cache import persist_cache
    await persist_cache()
    return {"status": "ok", "message": "Cache persisted to database"}


# =============================================================================
# Warming Service Endpoints
# =============================================================================

@app.get("/warming/status")
async def warming_status():
    """Get proactive warming service status."""
    from nova.warming import get_warming_status
    return get_warming_status()


@app.get("/warming/upcoming")
async def warming_upcoming(hours: int = 2):
    """Get warmings scheduled for the next N hours."""
    from nova.warming import get_warming_service
    service = get_warming_service()
    if service is None:
        return {"error": "Warming service not initialized"}
    return {"upcoming": service.get_next_warmings(hours)}


@app.get("/warming/seasonal")
async def warming_seasonal():
    """Get current seasonal context for warming decisions."""
    from nova.warming import get_warming_service
    service = get_warming_service()
    if service is None:
        return {"error": "Warming service not initialized"}
    return service.get_seasonal_context()


# =============================================================================
# Cache Orchestrator Endpoints
# =============================================================================

@app.get("/orchestrator/status")
async def orchestrator_status():
    """Get cache orchestrator status."""
    from nova.cache_orchestrator import get_orchestrator_status
    return get_orchestrator_status()


@app.get("/turn-orchestrator/status")
async def turn_orchestrator_status():
    return {
        "status": "ok",
        "state_cache_entries": len(_turn_states),
        "metrics": get_orchestrator_metrics(),
    }


@app.get("/turn-orchestrator/metrics")
async def turn_orchestrator_metrics():
    return get_orchestrator_metrics()


@app.get("/orchestrator/recommendations")
async def orchestrator_recommendations():
    """Get latest cache recommendations."""
    from nova.cache_orchestrator import get_orchestrator
    orch = get_orchestrator()
    if orch is None:
        return {"error": "Orchestrator not initialized"}
    return orch.get_recommendations()


@app.post("/orchestrator/analyze")
async def orchestrator_analyze():
    """Manually trigger cache analysis."""
    from nova.cache_orchestrator import trigger_analysis
    result = await trigger_analysis()
    return {"status": "ok", "result": result}


# =============================================================================
# Hypothesis-Validation Framework Endpoints
# =============================================================================

@app.get("/hypothesis/status")
async def hypothesis_status():
    """Get hypothesis validator status and session stats."""
    from nova.hypothesis import get_hypothesis_validator
    validator = get_hypothesis_validator()
    if validator is None:
        return {"initialized": False, "active": False}
    return {
        "initialized": True,
        "active": validator.active,
        "current_session": {
            "hypothesis": validator.current_session.hypothesis_text[:100] if validator.current_session else None,
            "confidence": validator.current_session.confidence if validator.current_session else None,
            "tools": validator.current_session.validation_tools if validator.current_session else [],
        } if validator.current_session else None,
        "stats": validator.get_session_stats(),
    }


@app.get("/hypothesis/protocol")
async def hypothesis_protocol():
    """Get the hypothesis-validation protocol specification for frontend integration."""
    return {
        "version": "1.0",
        "description": "Zero-Wait Response Protocol - Nova speaks from trained knowledge while validation tools execute",
        "message_types": {
            "hypothesis": {
                "description": "Initial response from LLM training knowledge",
                "fields": {"text": "string", "confidence": "float (0.0-1.0)"},
                "example": {"type": "hypothesis", "text": "Dallas is usually warm this time of year...", "confidence": 0.7}
            },
            "validating": {
                "description": "Tools being called to validate the hypothesis",
                "fields": {"tools": "string[]"},
                "example": {"type": "validating", "tools": ["get_weather", "web_search"]}
            },
            "validationStep": {
                "description": "Progress update for each validation tool",
                "fields": {"tool": "string", "status": "running|completed|failed"},
                "example": {"type": "validationStep", "tool": "get_weather", "status": "completed"}
            },
            "validated": {
                "description": "Final validation result with optional correction",
                "fields": {"text": "string (optional)", "result": "confirmed|corrected|enriched"},
                "example": {"type": "validated", "text": "Actually, it's 82°F with high humidity.", "result": "corrected"}
            },
            "sources": {
                "description": "Citations for validated information",
                "fields": {"citations": "[{title, url, type}]"},
                "example": {"type": "sources", "citations": [{"title": "OpenWeatherMap", "url": None, "type": "api"}]}
            }
        },
        "flow": [
            "1. User asks question requiring external data",
            "2. LLM generates hypothesis from training → {type: 'hypothesis'}",
            "3. Validation tools identified → {type: 'validating'}",
            "4. Each tool reports progress → {type: 'validationStep'}",
            "5. Results compared to hypothesis → {type: 'validated'}",
            "6. Sources provided → {type: 'sources'}"
        ],
        "validation_tools": [
            "get_weather", "web_search", "check_studio", "get_time",
            "tesla_status", "service_health_check"
        ],
        "delegated_validation": {
            "tool": "hub_delegate",
            "description": "Hub delegation can serve as validation when task is research/lookup oriented",
            "validation_keywords": ["search", "find", "look up", "research", "check", "verify", "investigate", "analyze"],
            "action_keywords": ["order", "buy", "purchase", "send", "create", "schedule", "restart", "delete"],
            "citation_types": {
                "browser-search": {"title": "Argus Browser Research", "type": "web"},
                "hermes-email": {"title": "CIG Email Search", "type": "api"},
                "hermes-calendar": {"title": "CIG Calendar", "type": "api"},
                "homelab-diagnostics": {"title": "Infra Agent Diagnostics", "type": "api"},
                "default": {"title": "Hub Agent Research", "type": "web"}
            }
        }
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    
    Accepts standard OpenAI format and routes to AI Gateway with Nova's tools.
    """
    try:
        body = await request.json()
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        # Collapse all known aliases (eleazar/default/dashboard/device-UUID)
        # to ONE canonical user_id so SQLite/Postgres stop sharding the
        # same human's conversations across multiple keyspaces.
        from nova.user_resolver import canonical_user_id
        user_id = canonical_user_id(body.get("user", "dashboard"))
        
        if not messages:
            raise HTTPException(status_code=400, detail="messages required")
        
        # Get or create conversation session
        conversation_id = body.get("conversation_id") or f"text_{uuid.uuid4().hex[:12]}"
        session = await get_or_create_session(user_id, conversation_id)
        
        # Build system prompt with tools
        system_prompt = build_system_prompt(user_id, TOOL_NAMES)
        
        # Restore conversation history
        history = await get_history(session.session_id, MAX_HISTORY_TURNS)
        
        # Build full message list
        full_messages = [{"role": "system", "content": system_prompt}]
        for turn in history:
            full_messages.append({"role": turn.role, "content": turn.content})
        
        # Add new user message
        user_message = messages[-1].get("content", "") if messages else ""
        full_messages.append({"role": "user", "content": user_message})
        
        # Save user turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "user", user_message)
        await _sync_message_to_backend(
            conversation_id, user_id, "user", user_message
        )
        
        # Call AI Gateway
        response_text, tool_calls, usage = await _call_llm(
            full_messages, 
            stream=stream,
            tools=TOOL_DEFINITIONS,
        )
        
        # Execute tool calls if any
        if tool_calls:
            tool_results = await _execute_tools(tool_calls, user_id)
            # Add tool results and get final response
            for tc, result in zip(tool_calls, tool_results):
                full_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result),
                })
            
            # Get final response after tool execution
            response_text, _, usage = await _call_llm(full_messages, stream=False)
        
        # Save assistant turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "assistant", response_text)
        await _sync_message_to_backend(
            conversation_id, user_id, "assistant", response_text
        )
        
        # Return OpenAI-compatible response
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": LLM_MODEL,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }],
            "usage": usage,
            "conversation_id": conversation_id,
        }
        
    except Exception as e:
        logger.error(f"Chat completions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat")
async def simple_chat(req: ChatRequest):
    """
    Simple chat endpoint for Dashboard.
    
    Simpler interface than OpenAI format - just message + user_id.
    """
    try:
        from nova.user_resolver import canonical_user_id
        req.user_id = canonical_user_id(req.user_id)
        # Get or create conversation session
        conversation_id = req.conversation_id or f"text_{uuid.uuid4().hex[:12]}"
        session = await get_or_create_session(req.user_id, conversation_id)
        
        # Build system prompt with tools
        system_prompt = build_system_prompt(req.user_id, TOOL_NAMES)
        
        # Restore conversation history
        history = await get_history(session.session_id, MAX_HISTORY_TURNS)
        
        # Build full message list
        messages = [{"role": "system", "content": system_prompt}]
        for turn in history:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": req.message})
        
        # Ensure backend conversation exists with session context
        session_ctx = {
            "client": "dashboard",
            "audio_mode": "text",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await ensure_backend_conversation(
            conversation_id, req.user_id,
            f"Nova Chat {conversation_id[:8]}",
            session_context=session_ctx,
        )
        
        # Set user context for tool dispatch (needed for user_id injection)
        set_progress_context(None, req.user_id)
        
        # Reset per-turn search counter
        reset_conversation_search_count(req.user_id)

        if conversation_id not in _turn_states:
            metadata = await get_session_metadata(session.session_id)
            _turn_states[conversation_id] = turn_state_from_metadata(metadata)
        turn_state = _turn_states[conversation_id]
        plan = await decide_turn(req.message, turn_state)
        orchestrated_response: list[str] = []

        async def _send_server_msg(msg: dict):
            if msg.get("type") == "validated":
                orchestrated_response.append(str(msg.get("text") or msg.get("speechText") or ""))

        async def _persist(role: str, content: str):
            await append_turn(session.session_id, role, content)
            await _sync_message_to_backend(conversation_id, req.user_id, role, content)

        orchestration_result = await execute_turn_plan_result(
            plan,
            turn_state,
            dispatch_tool,
            _send_server_msg,
            _persist,
        )
        if orchestration_result.handled:
            await update_session_metadata_key(
                session.session_id,
                STATE_METADATA_KEY,
                turn_state_to_metadata_value(turn_state),
            )
            response_text = orchestration_result.response or (
                orchestrated_response[-1] if orchestrated_response else "Handled by Nova Turn Orchestrator."
            )
            return ChatResponse(
                response=response_text,
                conversation_id=conversation_id,
                model=LLM_MODEL,
                usage=None,
                tool_calls=orchestration_result.tools_used or None,
            )
        
        # Save user turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "user", req.message)
        await _sync_message_to_backend(
            conversation_id, req.user_id, "user", req.message
        )
        
        if plan.learned_candidate:
            from nova.store import append_learning_event
            await append_learning_event(
                event_type="candidate_applied",
                source_layer="orchestrator",
                session_id=session.session_id,
                conversation_id=conversation_id,
                user_id=req.user_id,
                canonical_text=req.message,
                payload={
                    "candidate_id": plan.learned_candidate.get("id"),
                    "intent": plan.intent.value
                }
            )
            tools = plan.learned_candidate.get("tools_used", [])
            if tools:
                tool_name = tools[0]
                instruction = f"\n\n[SYSTEM ASSISTIVE ROUTING: The user's request matches a learned pattern for the '{tool_name}' tool. Call this tool immediately to assist them.]"
                messages[-1]["content"] += instruction
                logger.info(f"NOVA_LEARNING_ROUTING_TEXT | Injected assistive routing for {tool_name}")

        # Start background learning consolidation
        from nova.learning import consolidate_session_learning
        import asyncio
        asyncio.create_task(consolidate_session_learning(session.session_id))
        
        # Call AI Gateway with tools
        response_text, tool_calls, usage = await _call_llm(
            messages,
            stream=req.stream,
            tools=TOOL_DEFINITIONS,
        )
        
        executed_tools = []
        
        # Execute tool calls if any
        if tool_calls:
            tool_results = await _execute_tools(tool_calls, req.user_id)
            executed_tools = [tc.get("function", {}).get("name") for tc in tool_calls]
            
            # Add tool results and get final response
            for tc, result in zip(tool_calls, tool_results):
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result),
                })
            
            # Get final response after tool execution
            response_text, _, usage = await _call_llm(messages, stream=False)
        
        # Save assistant turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "assistant", response_text)
        await _sync_message_to_backend(
            conversation_id, req.user_id, "assistant", response_text
        )
        
        return ChatResponse(
            response=response_text,
            conversation_id=conversation_id,
            model=LLM_MODEL,
            usage=usage,
            tool_calls=executed_tools if executed_tools else None,
        )
        
    except Exception as e:
        logger.error(f"Simple chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversations")
async def list_conversations(user_id: str = "dashboard", limit: int = 20):
    """
    List conversations for a user.
    
    Fetches from PostgreSQL backend (source of truth).
    Falls back to SQLite if backend is unavailable.
    """
    try:
        from nova.store import get_backend_conversations, get_user_sessions, get_history
        
        # Try backend first (source of truth)
        backend_convs = await get_backend_conversations(user_id, limit)
        if backend_convs:
            return {"conversations": backend_convs, "total": len(backend_convs), "source": "backend"}
        
        # Fallback to SQLite
        sessions = await get_user_sessions(user_id, limit)
        conversations = []
        for session in sessions:
            history = await get_history(session.session_id, limit=3)
            first_user_msg = next((t for t in history if t.role == "user"), None)
            last_msg = history[-1] if history else None
            conversations.append({
                "id": session.conversation_id,
                "session_id": session.session_id,
                "title": (first_user_msg.content[:50] + "...") if first_user_msg and len(first_user_msg.content) > 50 else (first_user_msg.content if first_user_msg else "New conversation"),
                "preview": (last_msg.content[:80] + "...") if last_msg and len(last_msg.content) > 80 else (last_msg.content if last_msg else ""),
                "message_count": len(history),
                "created_at": session.created_at,
                "updated_at": session.last_active,
            })
        return {"conversations": conversations, "total": len(conversations), "source": "sqlite"}
        
    except Exception as e:
        logger.error(f"List conversations error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user_id: str = "dashboard"):
    """
    Get full conversation history by ID.
    
    Fetches from PostgreSQL backend (source of truth).
    Falls back to SQLite if backend is unavailable.
    """
    try:
        from nova.store import get_backend_conversation, get_or_create_session, get_history
        
        # Try backend first
        backend_conv = await get_backend_conversation(conversation_id, user_id)
        if backend_conv:
            return backend_conv
        
        # Fallback to SQLite
        session = await get_or_create_session(user_id, conversation_id)
        history = await get_history(session.session_id, limit=100)
        messages = [
            {
                "role": t.role,
                "content": t.content,
                "timestamp": t.timestamp,
                "tool_calls": json.loads(t.tool_calls) if t.tool_calls else None,
            }
            for t in history
        ]
        return {
            "id": conversation_id,
            "session_id": session.session_id,
            "messages": messages,
            "created_at": session.created_at,
            "updated_at": session.last_active,
        }
        
    except Exception as e:
        logger.error(f"Get conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, user_id: str = "dashboard"):
    """
    Delete a conversation and all its messages.
    """
    try:
        import aiosqlite
        from nova.store import DB_PATH
        
        session_id = f"{user_id}:{conversation_id}"
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Delete turns first (foreign key)
            await db.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            # Delete session
            await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await db.commit()
        
        logger.info(f"Deleted conversation {conversation_id} for user {user_id}")
        return {"status": "deleted", "conversation_id": conversation_id}
        
    except Exception as e:
        logger.error(f"Delete conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# LLM Helpers
# ---------------------------------------------------------------------------

async def _call_llm(
    messages: list,
    stream: bool = False,
    tools: list = None,
) -> tuple[str, list, dict]:
    """
    Call AI Gateway for LLM completion.
    
    Returns:
        Tuple of (response_text, tool_calls, usage)
    """
    url = f"{AI_GATEWAY_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
    }
    
    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": stream,
        "max_tokens": 4096,
        "temperature": 0.1,  # Low for agentic precision
    }
    
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body, timeout=60) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"AI Gateway error: {resp.status} - {error_text}")
                raise Exception(f"AI Gateway error: {resp.status}")
            
            data = await resp.json()
            
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            response_text = message.get("content", "")
            tool_calls = message.get("tool_calls", [])
            usage = data.get("usage", {})
            
            return response_text, tool_calls, usage


async def _execute_tools(tool_calls: list, user_id: str) -> list:
    """Execute tool calls and return results."""
    results = []
    
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "")
        args_str = func.get("arguments", "{}")
        
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {}
        
        logger.info(f"Executing tool: {name} with args: {args}")
        
        try:
            result = await dispatch_tool(name, args)
            results.append(result)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            results.append({"error": str(e)})
    
    return results


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def run_text_chat_server():
    """Run the text chat server standalone."""
    import uvicorn
    
    await init_db()
    logger.info(f"Nova Text Chat API starting on port {TEXT_CHAT_PORT}")
    
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=TEXT_CHAT_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(run_text_chat_server())
