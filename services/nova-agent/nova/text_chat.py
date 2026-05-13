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
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from nova.store import init_db, get_or_create_session, append_turn, get_history, get_compacted_context, _sync_message_to_backend, ensure_backend_conversation, get_session_metadata, update_session_metadata_key, get_turn_policy_metrics
from nova.prompt import build_system_prompt
from nova.pcg import build_context as _build_pcg_context
from nova.context_budget import check_overflow_risk
from nova.context_compactor import compact_if_over_latency_threshold, LATENCY_THRESHOLD_TOKENS
from nova.tools import TOOL_DEFINITIONS, dispatch_tool, reset_conversation_search_count, set_progress_context
from nova.turn_orchestrator import STATE_METADATA_KEY, TurnState, decide_turn, execute_turn_plan_result, get_orchestrator_metrics, turn_state_from_metadata, turn_state_to_metadata_value
from nova.turn_tool_policy import CORE_TOOL_NAMES, select_tool_budget
from nova.turn_context import (
    TurnContext,
    derive_goal,
    derive_evidence_budget,
    augment_tool_result,
    finalize_and_persist,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEXT_CHAT_PORT = int(os.environ.get("NOVA_TEXT_PORT", "18803"))
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
LLM_MODEL = os.environ.get("LLM_MODEL", "minimax-m2.5")

# Tool names for prompt builder
ALL_TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS if "function" in t]
TOOL_DEFINITIONS_BY_NAME = {
    td["function"]["name"]: td
    for td in TOOL_DEFINITIONS
    if "function" in td and "name" in td["function"]
}
TOOL_NAMES = [name for name in ALL_TOOL_NAMES if name in CORE_TOOL_NAMES]


def _selected_tool_definitions(tool_names: list[str]) -> list[dict]:
    return [TOOL_DEFINITIONS_BY_NAME[name] for name in tool_names if name in TOOL_DEFINITIONS_BY_NAME]

# Max conversation turns to restore from DB
MAX_HISTORY_TURNS = int(os.environ.get("NOVA_TEXT_HISTORY_TURNS", "8"))
MAX_LLM_MESSAGE_CHARS = int(os.environ.get("NOVA_MAX_LLM_MESSAGE_CHARS", "1800"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("NOVA_MAX_TOOL_RESULT_CHARS", "2500"))
_turn_states: dict[str, TurnState] = {}


def _estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(msg.get("content") or "")) for msg in messages) // 4


def _trim_message_content(content: str, limit: int = MAX_LLM_MESSAGE_CHARS) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[trimmed for latency]"


def _trim_context_messages(messages: list[dict], max_recent: int = MAX_HISTORY_TURNS) -> list[dict]:
    trimmed: list[dict] = []
    for msg in messages[-max_recent:]:
        role = msg.get("role")
        content = _trim_message_content(str(msg.get("content") or ""))
        if role in ("user", "assistant") and content.strip():
            trimmed.append({"role": role, "content": content})
    return trimmed


def _trim_tool_result(result: object) -> str:
    if isinstance(result, dict) and "card" in result and ("speakable" in result or "speech" in result):
        speakable = result.get("speakable") or result.get("speech") or result.get("display") or ""
        card = result.get("card") or {}
        compact = {
            "speakable": speakable,
            "card": {
                "kind": card.get("kind"),
                "schemaVersion": card.get("schemaVersion"),
                "title": card.get("title"),
                "summary": card.get("summary"),
                "source": card.get("source"),
            }
        }
        text = json.dumps(compact)
        return text if text.strip() else str(speakable)
    text = json.dumps(result) if isinstance(result, (dict, list)) else str(result)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS].rstrip() + "\n[trimmed tool result for latency]"


def _compact_system_prompt(prompt: str) -> str:
    if os.environ.get("NOVA_COMPACT_SYSTEM_PROMPT", "1").lower() in {"0", "false", "no"}:
        return prompt
    marker = "\n\n## Who You Are\n"
    if marker not in prompt:
        return prompt
    _, rest = prompt.split(marker, 1)
    compact_identity = (
        "You are Nova, a personal AI voice assistant and companion. Speak naturally and concisely. "
        "Use deterministic orchestrators and direct tools for specific/current/personal data. "
        "Answer from trained knowledge only for general facts. Use zero-wait behavior: do not stall "
        "or promise action without calling the tool in the same response. PCG is memory, CIG is "
        "email/calendar/contact intelligence, Pi Agent Hub delegates specialist work, and AI Gateway "
        "routes LLM/search/vision."
    )
    return compact_identity + marker + rest


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
    cards: Optional[list] = None
    workspace_page_id: Optional[str] = None


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
        "metrics": await get_turn_policy_metrics(),
        "process_metrics": get_orchestrator_metrics(),
    }


@app.get("/turn-orchestrator/metrics")
async def turn_orchestrator_metrics():
    durable = await get_turn_policy_metrics()
    durable["process"] = get_orchestrator_metrics()
    return durable


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
                "example": {"type": "sources", "citations": [{"title": "Perplexity", "url": None, "type": "web"}]}
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
        
        # Build system prompt with full PCG context
        pcg_ctx = await _build_pcg_context(user_id)
        system_prompt = build_system_prompt(
            user_name=pcg_ctx.get("user_name"),
            user_timezone=pcg_ctx.get("user_timezone", "America/Chicago"),
            tool_names=TOOL_NAMES,
            memory_snippets=pcg_ctx.get("memory_snippets"),
            preferences_by_category=pcg_ctx.get("preferences_by_category"),
            identity=pcg_ctx.get("identity"),
            daily_snapshot=pcg_ctx.get("daily_snapshot"),
            recent_insights=pcg_ctx.get("recent_insights"),
            recent_session_digest=pcg_ctx.get("recent_session_digest"),
            dream_insights=pcg_ctx.get("dream_insights"),
            active_goals=pcg_ctx.get("active_goals"),
            active_task_plans=pcg_ctx.get("active_task_plans"),
            recent_turn_outcomes=pcg_ctx.get("recent_turn_outcomes"),
        )
        system_prompt = _compact_system_prompt(system_prompt)
        
        compacted = await get_compacted_context(
            conversation_id=conversation_id,
            user_id=user_id,
            max_recent_turns=MAX_HISTORY_TURNS,
        )
        if compacted:
            history_messages = _trim_context_messages(compacted)
        else:
            history = await get_history(session.session_id, MAX_HISTORY_TURNS)
            history_messages = _trim_context_messages([
                {"role": turn.role, "content": turn.content}
                for turn in history
            ])
        
        # Build full message list
        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.extend(history_messages)
        
        # Add new user message
        user_message = messages[-1].get("content", "") if messages else ""
        full_messages.append({"role": "user", "content": user_message})
        plan_preview = await decide_turn(user_message, turn_state_from_metadata(await get_session_metadata(session.session_id)))
        tool_budget = select_tool_budget(user_message, ALL_TOOL_NAMES, plan_preview.intent.value)
        # Reasoning scaffold — same TurnContext architecture as voice/iOS and simple_chat
        turn_id = f"openai-{uuid.uuid4().hex[:12]}"
        turn_context = TurnContext(
            turn_id=turn_id,
            user_text=user_message,
            goal=derive_goal(user_message, getattr(plan_preview, "goal", "") or "", plan_preview.intent.value),
            intent=plan_preview.intent.value,
            evidence_budget=derive_evidence_budget(getattr(plan_preview, "evidence_budget", 0) or 0, plan_preview.intent.value),
        )
        logger.info(
            f"NOVA_TURN_CONTEXT_INIT | path=chat_completions turn_id={turn_id} "
            f"intent={plan_preview.intent.value} goal={turn_context.goal[:80]!r} "
            f"evidence_budget={turn_context.evidence_budget}"
        )
        _approx = _estimate_tokens(full_messages)
        logger.info(
            f"NOVA_PROMPT_BUDGET | text_chat messages={len(full_messages)} "
            f"history={len(history_messages)} approx_tokens={_approx} "
            f"system_chars={len(system_prompt)} tools={len(tool_budget.names)} "
            f"intent={plan_preview.intent.value} tool_reason={tool_budget.reason}"
        )
        check_overflow_risk(
            _approx, path="text_chat",
            message_count=len(full_messages),
            tools_in_turn=len(tool_budget.names),
            intent=plan_preview.intent.value,
        )
        
        # Save user turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "user", user_message)
        await _sync_message_to_backend(
            conversation_id, user_id, "user", user_message
        )
        
        # Call AI Gateway
        response_text, tool_calls, usage = await _call_llm(
            full_messages, 
            stream=stream,
            tools=_selected_tool_definitions(tool_budget.names),
        )
        
        cards = []
        # Execute tool calls if any
        if tool_calls:
            tool_results = await _execute_tools(tool_calls, user_id)
            cards = [result.get("card") for result in tool_results if isinstance(result, dict) and result.get("card")]
            # Add tool results and get final response
            for tc, result in zip(tool_calls, tool_results):
                tool_name = tc.get("function", {}).get("name", "")
                tool_args_str = tc.get("function", {}).get("arguments", "{}") or "{}"
                tool_content = _trim_tool_result(result)
                # Reasoning scaffold — append TURN ANCHOR / SIGNAL / COMPLETION CHECK
                try:
                    augmented = augment_tool_result(turn_context, tool_name, str(tool_args_str)[:80], tool_content)
                    if augmented != tool_content:
                        tool_content = augmented
                        logger.info(
                            f"NOVA_TURN_ANCHOR_INJECTED | path=chat_completions tool={tool_name} "
                            f"posture={turn_context.posture} calls={len(turn_context.tool_history)} "
                            f"evidence={len(turn_context.evidence_log)}"
                        )
                except Exception as _e:
                    logger.warning(f"chat_completions scaffold injection failed (non-fatal): {_e}")
                full_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_content,
                })
            
            # Latency-aware compaction: if multiple tools fired and pushed
            # the prompt over the threshold, stub the older tool_results
            # before the follow-up LLM call. The model still has the most
            # recent few results verbatim (per nova.context_compactor).
            compact_if_over_latency_threshold(
                full_messages,
                threshold_tokens=LATENCY_THRESHOLD_TOKENS,
                path="text_chat_post_tools",
            )

            # Get final response after tool execution
            response_text, _, usage = await _call_llm(full_messages, stream=False)
            if not response_text and tool_results:
                response_text = next(
                    (
                        str(result.get("speakable") or result.get("speech") or result.get("display") or "")
                        for result in tool_results
                        if isinstance(result, dict)
                    ),
                    response_text,
                )
        
        # Save assistant turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "assistant", response_text or "")
        await _sync_message_to_backend(
            conversation_id, user_id, "assistant", response_text or ""
        )

        # Persist turn summary for cross-turn memory (vertical reasoning durability)
        asyncio.create_task(finalize_and_persist(turn_context, user_id, conversation_id))

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
            "cards": cards or None,
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
        
        # Build system prompt with full PCG context
        pcg_ctx = await _build_pcg_context(req.user_id)
        system_prompt = build_system_prompt(
            user_name=pcg_ctx.get("user_name"),
            user_timezone=pcg_ctx.get("user_timezone", "America/Chicago"),
            tool_names=TOOL_NAMES,
            memory_snippets=pcg_ctx.get("memory_snippets"),
            preferences_by_category=pcg_ctx.get("preferences_by_category"),
            identity=pcg_ctx.get("identity"),
            daily_snapshot=pcg_ctx.get("daily_snapshot"),
            recent_insights=pcg_ctx.get("recent_insights"),
            recent_session_digest=pcg_ctx.get("recent_session_digest"),
            dream_insights=pcg_ctx.get("dream_insights"),
            active_goals=pcg_ctx.get("active_goals"),
            active_task_plans=pcg_ctx.get("active_task_plans"),
            recent_turn_outcomes=pcg_ctx.get("recent_turn_outcomes"),
        )
        system_prompt = _compact_system_prompt(system_prompt)
        
        compacted = await get_compacted_context(
            conversation_id=conversation_id,
            user_id=req.user_id,
            max_recent_turns=MAX_HISTORY_TURNS,
        )
        if compacted:
            history_messages = _trim_context_messages(compacted)
        else:
            history = await get_history(session.session_id, MAX_HISTORY_TURNS)
            history_messages = _trim_context_messages([
                {"role": turn.role, "content": turn.content}
                for turn in history
            ])
        
        # Build full message list
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_messages)
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
        tool_budget = select_tool_budget(req.message, ALL_TOOL_NAMES, plan.intent.value)
        # Reasoning scaffold — same TurnContext architecture as voice/iOS bot.py
        turn_id = f"text-{uuid.uuid4().hex[:12]}"
        turn_context = TurnContext(
            turn_id=turn_id,
            user_text=req.message,
            goal=derive_goal(req.message, getattr(plan, "goal", "") or "", plan.intent.value),
            intent=plan.intent.value,
            evidence_budget=derive_evidence_budget(getattr(plan, "evidence_budget", 0) or 0, plan.intent.value),
        )
        logger.info(
            f"NOVA_TURN_CONTEXT_INIT | path=simple_chat turn_id={turn_id} "
            f"intent={plan.intent.value} goal={turn_context.goal[:80]!r} "
            f"evidence_budget={turn_context.evidence_budget}"
        )
        _approx = _estimate_tokens(messages)
        logger.info(
            f"NOVA_PROMPT_BUDGET | simple_chat messages={len(messages)} "
            f"history={len(history_messages)} approx_tokens={_approx} "
            f"system_chars={len(system_prompt)} tools={len(tool_budget.names)} "
            f"intent={plan.intent.value} tool_reason={tool_budget.reason}"
        )
        check_overflow_risk(
            _approx, path="simple_chat",
            message_count=len(messages),
            tools_in_turn=len(tool_budget.names),
            intent=plan.intent.value,
        )
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
            user_id=req.user_id,
            conversation_id=conversation_id,
            session_id=session.session_id,
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
                workspace_page_id=orchestration_result.workspace_page_id or None,
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

        # Call AI Gateway with tools
        response_text, tool_calls, usage = await _call_llm(
            messages,
            stream=req.stream,
            tools=_selected_tool_definitions(tool_budget.names),
        )
        
        executed_tools = []
        cards = []
        
        # Execute tool calls if any
        if tool_calls:
            tool_results = await _execute_tools(tool_calls, req.user_id, session_id=session.session_id, canonical_text=req.message)
            executed_tools = [tc.get("function", {}).get("name") for tc in tool_calls]
            cards = [result.get("card") for result in tool_results if isinstance(result, dict) and result.get("card")]
            
            # Add tool results and get final response
            for tc, result in zip(tool_calls, tool_results):
                tool_name = tc.get("function", {}).get("name", "")
                tool_args_str = tc.get("function", {}).get("arguments", "{}") or "{}"
                tool_content = _trim_tool_result(result)
                # Reasoning scaffold — append TURN ANCHOR / SIGNAL / COMPLETION CHECK
                try:
                    augmented = augment_tool_result(turn_context, tool_name, str(tool_args_str)[:80], tool_content)
                    if augmented != tool_content:
                        tool_content = augmented
                        logger.info(
                            f"NOVA_TURN_ANCHOR_INJECTED | path=simple_chat tool={tool_name} "
                            f"posture={turn_context.posture} calls={len(turn_context.tool_history)} "
                            f"evidence={len(turn_context.evidence_log)}"
                        )
                except Exception as _e:
                    logger.warning(f"simple_chat scaffold injection failed (non-fatal): {_e}")
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_content,
                })
            
            # Get final response after tool execution
            response_text, _, usage = await _call_llm(messages, stream=False)
            if not response_text and tool_results:
                response_text = next(
                    (
                        str(result.get("speakable") or result.get("speech") or result.get("display") or "")
                        for result in tool_results
                        if isinstance(result, dict)
                    ),
                    response_text,
                )
        
        # Save assistant turn (SQLite + PostgreSQL)
        await append_turn(session.session_id, "assistant", response_text or "")
        await _sync_message_to_backend(
            conversation_id, req.user_id, "assistant", response_text or ""
        )

        # Persist turn summary for cross-turn memory (vertical reasoning durability)
        asyncio.create_task(finalize_and_persist(turn_context, req.user_id, conversation_id))

        # Start background learning consolidation AFTER tools have run
        from nova.learning import consolidate_session_learning
        asyncio.create_task(consolidate_session_learning(session.session_id))
        
        return ChatResponse(
            response=response_text,
            conversation_id=conversation_id,
            model=LLM_MODEL,
            usage=usage,
            tool_calls=executed_tools if executed_tools else None,
            cards=cards or None,
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


def _require_picode_access(request: Request):
    token = os.environ.get("NOVA_PICODE_TOKEN") or os.environ.get("NOVA_AUTH_TOKEN") or ""
    host = request.client.host if request.client else ""
    if not token or host in {"127.0.0.1", "::1", "localhost"}:
        return
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-api-key", "")
    if auth == f"Bearer {token}" or key == token:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _limit(value: int, default: int = 50, maximum: int = 500) -> int:
    try:
        n = int(value)
    except Exception:
        n = default
    return max(1, min(n, maximum))


def _redacted_settings() -> dict:
    keys = [
        "NOVA_PORT", "WEBHOOK_PORT", "NOVA_TEXT_PORT", "NOVA_MIRROR_PORT",
        "AI_GATEWAY_URL", "LLM_MODEL", "NOVA_VOICE_HISTORY_TURNS",
        "NOVA_TEXT_HISTORY_TURNS", "NOVA_MAX_LLM_MESSAGE_CHARS",
        "NOVA_MAX_TOOL_RESULT_CHARS", "NOVA_COMPACT_SYSTEM_PROMPT",
        "SQLITE_PATH", "DATABASE_URL", "NIM_EMBED_URL", "NIM_EMBED_MODEL",
        "CIG_URL", "PI_HUB_URL", "PI_WORKSPACE_URL", "SKILL_DISCOVERY_URL",
    ]
    out = {}
    for key in keys:
        value = os.environ.get(key)
        if value is None:
            continue
        if any(secret in key.lower() for secret in ("key", "token", "password", "secret")):
            out[key] = "[redacted]"
        elif key == "DATABASE_URL":
            out[key] = value.split("@")[-1] if "@" in value else value
        else:
            out[key] = value
    return out


@app.get("/picode/manifest")
async def picode_manifest(request: Request):
    _require_picode_access(request)
    return {
        "service": "nova-agent",
        "surface": "picode-observability",
        "version": "0.1",
        "read_only": True,
        "endpoints": {
            "overview": "GET /picode/overview",
            "settings": "GET /picode/settings",
            "conversations": "GET /picode/conversations?user_id=&limit=",
            "conversation": "GET /picode/conversations/{conversation_id}?user_id=",
            "conversation_search": "GET /picode/conversations/search?user_id=&q=&days_back=&limit=",
            "memory": "GET /picode/memory?query=&category=",
            "cache": "GET /picode/cache",
            "orchestrators": "GET /picode/orchestrators",
            "skills": "GET /picode/skills?include_body=false",
            "skill_bindings": "GET /picode/skills/bindings",
            "skill": "GET /picode/skills/{skill_name}?include_body=true",
            "skill_resources": "GET /picode/skills/{skill_name}/resources",
            "skill_resource": "GET /picode/skills/{skill_name}/resources/{resource_path}",
            "task_artifacts": "GET /picode/task-artifacts?user_id=&conversation_id=&status=&limit=",
            "task_artifacts_summary": "GET /picode/task-artifacts/summary?limit=",
            "task_artifact_timeline": "GET /picode/task-artifacts/{task_id}/timeline",
            "task_artifact_qa_failures": "GET /picode/task-artifacts/qa/failures?limit=",
            "task_artifact_recent_handoffs": "GET /picode/task-artifacts/handoffs/recent?limit=",
            "task_artifact": "GET /picode/task-artifacts/{task_id}",
            "active_task_artifact": "GET /picode/task-artifacts/active/{session_id}",
            "learning_events": "GET /picode/learning/events?limit=",
            "turn_policy_observations": "GET /picode/turn-policy/observations?limit=",
            "grounding_recent": "GET /picode/grounding/recent?limit=",
            "grounding_summary": "GET /picode/grounding/summary?limit=",
            "grounding_no_evidence": "GET /picode/grounding/no-evidence?limit=",
            "grounding_risk": "GET /picode/grounding/risk?limit=",
            "action_ledger_recent": "GET /picode/action-ledger/recent?limit=&status=",
            "action_ledger_summary": "GET /picode/action-ledger/summary?limit=",
            "action_ledger_entry": "GET /picode/action-ledger/{action_id}",
        },
        "auth": "localhost allowed; otherwise Bearer or X-API-Key NOVA_PICODE_TOKEN/NOVA_AUTH_TOKEN",
    }


@app.get("/picode/overview")
async def picode_overview(request: Request):
    _require_picode_access(request)
    from nova.cache import get_cache_stats
    from nova.cache_orchestrator import get_orchestrator_status
    from nova.warming import get_warming_status
    from nova.store import get_turn_policy_metrics
    return {
        "status": "ok",
        "service": "nova-agent",
        "ports": {
            "webrtc": int(os.environ.get("NOVA_PORT", "18800")),
            "webhooks": int(os.environ.get("WEBHOOK_PORT", "18801")),
            "text_chat": TEXT_CHAT_PORT,
        },
        "model": LLM_MODEL,
        "cache": get_cache_stats(),
        "cache_orchestrator": get_orchestrator_status(),
        "warming": get_warming_status(),
        "turn_orchestrator": {
            "state_cache_entries": len(_turn_states),
            "process_metrics": get_orchestrator_metrics(),
            "durable_metrics": await get_turn_policy_metrics(),
        },
    }


@app.get("/picode/settings")
async def picode_settings(request: Request):
    _require_picode_access(request)
    return {
        "settings": _redacted_settings(),
        "tools": {
            "core": TOOL_NAMES,
            "available_count": len(ALL_TOOL_NAMES),
            "available": ALL_TOOL_NAMES,
        },
        "limits": {
            "max_history_turns": MAX_HISTORY_TURNS,
            "max_llm_message_chars": MAX_LLM_MESSAGE_CHARS,
            "max_tool_result_chars": MAX_TOOL_RESULT_CHARS,
        },
    }


@app.get("/picode/conversations")
async def picode_conversations(request: Request, user_id: str = "dashboard", limit: int = 50):
    _require_picode_access(request)
    return await list_conversations(user_id=user_id, limit=_limit(limit))


@app.get("/picode/conversations/search")
async def picode_conversation_search(
    request: Request,
    q: str,
    user_id: str = "dashboard",
    days_back: int = 30,
    limit: int = 10,
):
    _require_picode_access(request)
    from nova.user_resolver import canonical_user_id
    from nova.store import search_past_conversations
    rows = await search_past_conversations(
        canonical_user_id(user_id),
        q,
        days_back=max(1, min(int(days_back), 365)),
        limit=_limit(limit, default=10, maximum=50),
    )
    return {"query": q, "results": rows, "total": len(rows)}


@app.get("/picode/conversations/{conversation_id}")
async def picode_conversation(request: Request, conversation_id: str, user_id: str = "dashboard"):
    _require_picode_access(request)
    return await get_conversation(conversation_id=conversation_id, user_id=user_id)


@app.get("/picode/memory")
async def picode_memory(request: Request, query: str = "", category: str = ""):
    _require_picode_access(request)
    from nova.pcg import get_identity, get_preferences, get_goals, query as pcg_query
    data = {
        "identity": await get_identity(),
        "preferences": await get_preferences(categories=[category] if category else None),
        "goals": await get_goals(),
    }
    if query.strip():
        data["query"] = await pcg_query(query)
    return data


@app.get("/picode/cache")
async def picode_cache(request: Request):
    _require_picode_access(request)
    from nova.cache import get_cache_stats, get_warming_candidates
    from nova.warming import get_warming_status, get_warming_service
    service = get_warming_service()
    return {
        "stats": get_cache_stats(),
        "warming_candidates": get_warming_candidates(),
        "warming_status": get_warming_status(),
        "upcoming": service.get_next_warmings(2) if service else [],
    }


@app.get("/picode/orchestrators")
async def picode_orchestrators(request: Request):
    _require_picode_access(request)
    from nova.cache_orchestrator import get_orchestrator_status, get_orchestrator
    orch = get_orchestrator()
    return {
        "turn": {
            "state_cache_entries": len(_turn_states),
            "process_metrics": get_orchestrator_metrics(),
            "durable_metrics": await get_turn_policy_metrics(),
        },
        "cache": {
            "status": get_orchestrator_status(),
            "recommendations": orch.get_recommendations() if orch else {},
        },
    }


@app.get("/picode/grounding/recent")
async def picode_grounding_recent(request: Request, limit: int = Query(25, ge=1, le=200)):
    _require_picode_access(request)
    from nova.store import get_recent_turn_evidence_envelopes
    evidence = await get_recent_turn_evidence_envelopes(limit)
    return {"evidence": evidence, "total": len(evidence), "read_only": True, "durable": True}


@app.get("/picode/grounding/summary")
async def picode_grounding_summary(request: Request, limit: int = Query(500, ge=1, le=500)):
    _require_picode_access(request)
    from nova.store import get_grounding_summary
    return await get_grounding_summary(limit)


@app.get("/picode/grounding/no-evidence")
async def picode_grounding_no_evidence(request: Request, limit: int = Query(100, ge=1, le=500)):
    _require_picode_access(request)
    from nova.store import get_recent_no_evidence_envelopes
    evidence = await get_recent_no_evidence_envelopes(limit)
    return {"evidence": evidence, "total": len(evidence), "read_only": True, "durable": True}


@app.get("/picode/grounding/risk")
async def picode_grounding_risk(request: Request, limit: int = Query(100, ge=1, le=500)):
    _require_picode_access(request)
    from nova.store import get_grounding_risk_observations
    return await get_grounding_risk_observations(limit)


@app.get("/picode/action-ledger/recent")
async def picode_action_ledger_recent(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    status: str = "",
):
    _require_picode_access(request)
    from nova.store import get_recent_action_ledger_entries
    actions = await get_recent_action_ledger_entries(limit=limit, status=status)
    return {"actions": actions, "total": len(actions), "read_only": True, "durable": True}


@app.get("/picode/action-ledger/summary")
async def picode_action_ledger_summary(request: Request, limit: int = Query(500, ge=1, le=500)):
    _require_picode_access(request)
    from nova.store import get_action_ledger_summary
    return await get_action_ledger_summary(limit=limit)


@app.get("/picode/action-ledger/{action_id}")
async def picode_action_ledger_entry(request: Request, action_id: str):
    _require_picode_access(request)
    from nova.store import get_action_ledger_entry
    entry = await get_action_ledger_entry(action_id)
    return {"action": entry, "found": entry is not None, "read_only": True, "durable": True}


@app.get("/picode/skills")
async def picode_skills(request: Request, include_body: bool = False):
    _require_picode_access(request)
    from nova.skill_loader import list_skills
    skills = list_skills(include_body=include_body)
    return {"skills": skills, "total": len(skills), "read_only": True}


@app.get("/picode/skills/bindings")
async def picode_skill_bindings(request: Request):
    _require_picode_access(request)
    from nova.skill_loader import get_skill_bindings
    return {"bindings": get_skill_bindings(), "read_only": True}


@app.get("/picode/skills/{skill_name}/resources/{resource_path:path}")
async def picode_skill_resource(request: Request, skill_name: str, resource_path: str, max_chars: int = 20000):
    _require_picode_access(request)
    from nova.skill_loader import read_skill_resource
    try:
        resource = read_skill_resource(skill_name, resource_path, max_chars=max_chars)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not resource:
        raise HTTPException(status_code=404, detail="Skill resource not found")
    return resource


@app.get("/picode/skills/{skill_name}/resources")
async def picode_skill_resources(request: Request, skill_name: str):
    _require_picode_access(request)
    from nova.skill_loader import list_skill_resources
    resources = list_skill_resources(skill_name)
    if not resources:
        raise HTTPException(status_code=404, detail="Skill not found")
    return resources


@app.get("/picode/skills/{skill_name}")
async def picode_skill(request: Request, skill_name: str, include_body: bool = True):
    _require_picode_access(request)
    from nova.skill_loader import get_skill
    try:
        skill = get_skill(skill_name, include_body=include_body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.get("/picode/task-artifacts")
async def picode_task_artifacts(
    request: Request,
    user_id: str = "",
    conversation_id: str = "",
    status: str = "",
    limit: int = 50,
):
    _require_picode_access(request)
    from nova.task_artifacts import list_task_artifacts
    artifacts = await list_task_artifacts(
        user_id=user_id,
        conversation_id=conversation_id,
        status=status,
        limit=_limit(limit, default=50, maximum=500),
    )
    return {"artifacts": artifacts, "total": len(artifacts)}


@app.get("/picode/task-artifacts/summary")
async def picode_task_artifacts_summary(request: Request, limit: int = 500):
    _require_picode_access(request)
    from nova.task_artifacts import get_task_artifact_summary
    return await get_task_artifact_summary(limit=_limit(limit, default=500, maximum=500))


@app.get("/picode/task-artifacts/qa/failures")
async def picode_task_artifact_qa_failures(request: Request, limit: int = 100):
    _require_picode_access(request)
    from nova.task_artifacts import get_task_artifact_qa_failures
    return await get_task_artifact_qa_failures(limit=_limit(limit, default=100, maximum=500))


@app.get("/picode/task-artifacts/handoffs/recent")
async def picode_task_artifact_recent_handoffs(request: Request, limit: int = 50):
    _require_picode_access(request)
    from nova.task_artifacts import get_recent_task_artifact_handoffs
    return await get_recent_task_artifact_handoffs(limit=_limit(limit, default=50, maximum=500))


@app.get("/picode/task-artifacts/{task_id}/timeline")
async def picode_task_artifact_timeline(request: Request, task_id: str):
    _require_picode_access(request)
    from nova.task_artifacts import get_task_artifact_timeline
    timeline = await get_task_artifact_timeline(task_id)
    if not timeline:
        raise HTTPException(status_code=404, detail="Task artifact not found")
    return timeline


@app.get("/picode/task-artifacts/active/{session_id:path}")
async def picode_active_task_artifact(request: Request, session_id: str):
    _require_picode_access(request)
    from nova.task_artifacts import get_active_task_artifact
    artifact = await get_active_task_artifact(session_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Active task artifact not found")
    return artifact


@app.get("/picode/task-artifacts/{task_id}")
async def picode_task_artifact(request: Request, task_id: str):
    _require_picode_access(request)
    from nova.task_artifacts import get_task_artifact
    artifact = await get_task_artifact(task_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Task artifact not found")
    return artifact


@app.get("/picode/learning/events")
async def picode_learning_events(request: Request, limit: int = 100):
    _require_picode_access(request)
    from nova.store import get_recent_learning_events
    events = await get_recent_learning_events(_limit(limit, default=100, maximum=500))
    return {"events": events, "total": len(events)}


@app.get("/picode/turn-policy/observations")
async def picode_turn_policy_observations(request: Request, limit: int = 100):
    _require_picode_access(request)
    from nova.store import get_recent_turn_policy_observations
    observations = await get_recent_turn_policy_observations(_limit(limit, default=100, maximum=500))
    return {"observations": observations, "total": len(observations)}


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
            
            response_text = message.get("content") or ""
            tool_calls = message.get("tool_calls", [])
            usage = data.get("usage", {})
            
            return response_text, tool_calls, usage


_LEARNING_TOOLS = {
    "save_memory", "recall_memory", "search_past_conversations", "query_cig",
    "web_search", "hub_delegate", "tesla_control", "get_weather",
    "manage_workspace", "analyze_image",
}

_LEARNING_INTENT_MAP = {
    "save_memory": "memory_save_request",
    "recall_memory": "memory_recall_request",
    "search_past_conversations": "conversation_recall_request",
    "query_cig": "email_lookup",
    "web_search": "web_research_request",
    "get_weather": "weather_lookup",
    "tesla_control": "tesla_control",
    "hub_delegate": "hub_delegate",
    "manage_workspace": "workspace_management",
    "analyze_image": "image_analysis",
}

async def _execute_tools(
    tool_calls: list,
    user_id: str,
    session_id: str = "",
    canonical_text: str = "",
) -> list:
    """Execute tool calls and return results."""
    import asyncio
    from nova.store import append_learning_event
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
            if name in _LEARNING_TOOLS and session_id:
                result_str = json.dumps(result) if not isinstance(result, str) else result
                failure_markers = ("error", "failed", "not found", "no result")
                success = bool(result_str.strip()) and not any(m in result_str.lower()[:150] for m in failure_markers)
                asyncio.create_task(append_learning_event(
                    event_type="tool_call_completed",
                    source_layer="text_chat",
                    session_id=session_id,
                    canonical_text=canonical_text,
                    tool_name=name,
                    tool_args=args,
                    success=success,
                    outcome="success" if success else "failed",
                    payload={"promotion_candidate": success},
                ))
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
