"""
Nova Agent — Pipecat voice agent bot.

Uses SmallWebRTCTransport (self-hosted, no cloud dependency) with
OpenAI-compatible LLM service pointed at AI Gateway → MiniMax M2.5.

Based on: https://github.com/pipecat-ai/pipecat-examples/tree/main/p2p-webrtc/voice-agent
"""

import os
import json
import time
import asyncio

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    InputTransportMessageFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frameworks.rtvi import (
    RTVIObserverParams,
    RTVIFunctionCallReportLevel,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
# Server audio mode: Whisper STT + Qwen TTS
from pipecat.services.whisper.stt import WhisperSTTService
from nova.qwen_tts_pipecat import QwenTTSService

import os as _os
_dotenv_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_dotenv_path, override=True)

from nova.events import event_bus
from nova.notify import create_event_handler
from nova.prompt import build_system_prompt
from nova.push import mark_user_active, mark_user_inactive, register_push_fallback
from nova.store import init_db, get_or_create_session, append_turn, get_history, _sync_message_to_backend, ensure_backend_conversation, get_backend_conversation, Turn, get_session_metadata, update_session_metadata_key, append_learning_event
from nova.pcg import build_context, record_observation, create_preference
from nova.tools import TOOL_DEFINITIONS, dispatch_tool, set_progress_context
from nova.turn_orchestrator import STATE_METADATA_KEY, TurnState, decide_turn, execute_turn_plan, turn_state_from_metadata, turn_state_to_metadata_value
from nova.turn_policy import canonicalize_turn_text
from nova.learning import consolidate_session_learning

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
LLM_MODEL = os.environ.get("LLM_MODEL", "minimax-m2.7")

# All known aliases for the primary human collapse into ONE canonical
# user_id (see nova/user_resolver.py). Without this, the same iOS
# conversation could be stored under 'eleazar', the device UUID, AND
# the PIC canonical UUID — which is exactly the bug that made Nova
# look like she'd lost memory between reconnects.
from nova.user_resolver import canonical_user_id as _resolve_user_id  # noqa: E402

# Tool names for prompt builder
TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS if "function" in t]

# Convert OpenAI-format tool defs to Pipecat FunctionSchema/ToolsSchema
def _build_tools_schema() -> ToolsSchema:
    schemas = []
    for td in TOOL_DEFINITIONS:
        func = td.get("function", {})
        params = func.get("parameters", {})
        schemas.append(FunctionSchema(
            name=func["name"],
            description=func.get("description", ""),
            properties=params.get("properties", {}),
            required=params.get("required", []),
        ))
    return ToolsSchema(standard_tools=schemas)

PIPECAT_TOOLS = _build_tools_schema()

# Max conversation turns to restore from DB
MAX_HISTORY_TURNS = 40


# ---------------------------------------------------------------------------
# Bot entry point (called by Pipecat runner)
# ---------------------------------------------------------------------------

async def run_bot(
    webrtc_connection,
    user_id: str = "default",
    audio_mode: str = "native",
    conversation_id: str = "default",
):
    """
    Run Nova bot with configurable audio processing mode.
    
    Args:
        webrtc_connection: The WebRTC connection from Pipecat
        user_id: User identifier for session tracking
        audio_mode: "native" (iOS STT/TTS, default) or "server" (server-side Deepgram/OpenAI)
        conversation_id: Conversation ID for history persistence (from iOS app)
    """
    use_server_audio = audio_mode == "server"
    logger.info(f"Starting bot for user={user_id}, audio_mode={audio_mode}, conv={conversation_id}")

    # ── Session & history ────────────────────────────────────────────────
    session = await get_or_create_session(user_id, conversation_id)
    prior_turns = await get_history(session.session_id, limit=MAX_HISTORY_TURNS)
    
    # If no local history, try loading from PostgreSQL backend (iOS app creates new conversations)
    if not prior_turns:
        backend_conv = await get_backend_conversation(conversation_id, user_id)
        if backend_conv and backend_conv.get("messages"):
            logger.info(f"Loading {len(backend_conv['messages'])} messages from backend")
            prior_turns = [
                Turn(
                    role=msg["role"],
                    content=msg["content"],
                    timestamp=msg.get("timestamp", ""),
                    tool_calls=None,
                )
                for msg in backend_conv["messages"]
                if msg["role"] in ("user", "assistant")
            ]
            # Sync to local SQLite for faster access
            for turn in prior_turns:
                await append_turn(session.session_id, turn.role, turn.content)
    
    logger.info(f"Session {session.session_id}: restored {len(prior_turns)} turns")

    # Ensure conversation exists in PostgreSQL backend for search/retrieval
    await ensure_backend_conversation(conversation_id, user_id)

    # ── PCG context (identity, preferences, goals) ──────────────────
    pcg_ctx = await build_context(user_id)
    logger.info(f"PCG context: {len(pcg_ctx.get('memory_snippets', []))} memory items")

    # ── Dynamic system prompt (shaped by PCG preferences) ────────────────
    system_prompt = build_system_prompt(
        user_name=pcg_ctx.get("user_name") or (user_id if user_id != "default" else None),
        user_timezone=pcg_ctx.get("user_timezone", "America/Chicago"),
        tool_names=TOOL_NAMES,
        memory_snippets=pcg_ctx.get("memory_snippets"),
        preferences_by_category=pcg_ctx.get("preferences_by_category"),
        identity=pcg_ctx.get("identity"),
        daily_snapshot=pcg_ctx.get("daily_snapshot"),
    )

    # ── Transport ────────────────────────────────────────────────────────
    # NOTE: Even in native mode, we enable audio to ensure Pipecat's
    # client_connected state fires. The iOS client handles its own STT/TTS.
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )

    # Initialize the connection - this sets _connect_invoked=True and fires
    # the on_client_connected event handler. Required for Pipecat 0.0.104+.
    await webrtc_connection.connect()
    logger.info("WebRTC connection initialized")

    # ── LLM (MiniMax-compatible) ───────────────────────────────────────
    # Use MiniMaxLLMService from nova.minimax_llm (supports thinking levels)
    from nova.minimax_llm import MiniMaxLLMService as _MiniMaxLLM

    # Orchestration mode → thinking level mapping
    # Fast mode = low thinking (no reasoning chain, 4K tokens, fast)
    # Default/medium = medium thinking (reasoning on, 8K tokens)
    # Deep/Verified = high thinking (reasoning on, 16K tokens, deep analysis)
    _thinking_level = "medium"  # default

    class MiniMaxLLMService(_MiniMaxLLM):
        """Extends MiniMaxLLMService with frame logging for voice agent."""

        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            """Log frame pushing to trace LLMFullResponseStartFrame/EndFrame emission."""
            from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame
            
            # Log outgoing frames
            if isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
                logger.info(f"🚀 MiniMaxLLMService.push_frame: {type(frame).__name__} → {direction}")
            elif isinstance(frame, LLMTextFrame):
                logger.info(f"📝 MiniMaxLLMService.push_frame: LLMTextFrame ({len(frame.text)} chars)")
            
            # Call parent to actually push
            await super().push_frame(frame, direction)

    llm = MiniMaxLLMService(
        thinking=_thinking_level,
        api_key=AI_GATEWAY_API_KEY,
        base_url=AI_GATEWAY_URL,
        model=LLM_MODEL,
        params=OpenAILLMService.InputParams(
            temperature=0.1,
            max_tokens=8192,
            # Thinking level: low (fast initial responses), high for complex tool chains
            # Gateway maps: low=no thinking (~85 tok/s), high=extended thinking (16K tokens)
            extra_body={"thinking": "low"},  # Default to fast responses
        ),
        function_call_timeout_secs=600.0,  # Hub delegation tasks can take minutes
    )

    # Register tool handlers (Pipecat 0.0.104 FunctionCallParams API)
    from pipecat.services.llm_service import FunctionCallParams

    # Mutable ref — populated after PipelineTask is created
    _rtvi_ref: list = [None]

    async def _send_server_msg(msg: dict):
        """Send a custom server message to iOS via RTVI protocol."""
        rtvi = _rtvi_ref[0]
        if rtvi is None:
            logger.warning(f"RTVI not ready, dropping server msg: {msg}")
            return
        try:
            await rtvi.send_server_message(msg)
            logger.debug(f"Sent server msg: {msg}")
        except Exception as e:
            logger.warning(f"Could not send server message: {e}")

    # ── Dual-path response: fast spoken ack + background tool + LLM result ──
    # Only truly slow tools get a spoken ack. check_studio is fast (<2s) and
    # the LLM often chains multiple calls, so acking each one spams the user.
    _SLOW_TOOLS = {"hub_delegate", "web_search", "query_cig", "query_frameworks", "tesla_control", "tesla_stream_monitor", "tesla_location_refresh", "tesla_wake", "tesla_navigation", "service_status", "homelab_diagnostics", "manage_workspace", "staar_tutor", "compact_conversations", "analyze_spreadsheet", "analyze_image"}
    # Per-turn dedup: only one spoken ack per user message to prevent feedback
    # loops where the mic picks up the TTS and re-sends it as a new utterance.
    _ack_sent_this_turn: list[bool] = [False]
    # Tool iteration tracking — runaway loop guard only (not a quality throttle)
    _tool_calls_this_turn: list[int] = [0]
    _MAX_TOOL_CALLS_BEFORE_HEARTBEAT = 4
    _MAX_TOOL_CALLS_HARD_LIMIT = 10  # Pure infinite-loop guard
    # Per-tool-per-turn rate limits — differentiated by provider cost/risk
    # Cloud APIs (paid, rate-limited externally): tight limits
    # Local/homelab APIs (free, internal): generous limits
    _PER_TOOL_LIMITS: dict[str, int] = {
        # ── Cloud / paid APIs ──────────────────────────────────────────────
        "web_search": 3,            # Perplexity Sonar — paid per call; allow 1 natural LLM retry
        "get_weather": 2,           # OpenWeatherMap — paid API
        "hub_delegate": 2,          # Hub RPC — approval-gated
        # ── Local / homelab ───────────────────────────────────────────────
        "query_cig": 3,             # CIG analytics
        "check_studio": 2,          # Local CIG/Hermes
        "query_frameworks": 5,      # Local LIAM/PCG
        "recall_memory": 5,         # Local PCG
        "search_past_conversations": 2,  # Local DB
        "tesla_control": 4,         # Local relay → Tesla cloud
        "service_status": 6,        # Local Docker API
        "homelab_diagnostics": 4,   # Local Docker API
        "homelab_operations": 4,    # Local Docker API
    }
    _per_tool_call_counts: dict[str, int] = {}
    # Track recent tool calls to detect duplicates
    # Note: hub_delegate is excluded from dedup because each delegation
    # is a unique long-running task — even if args look similar, the context
    # and state differ between calls.
    _DEDUP_EXCLUDED_TOOLS = {"hub_delegate", "tesla_wake"}
    _latest_user_event: dict[str, str] = {}
    _tool_call_count_this_turn: list[int] = [0]
    _search_tools_exhausted: list[bool] = [False]
    _structured_final_response_this_turn: list[bool] = [False]
    _PROVIDER_CLASS: dict[str, str] = {
        "web_search": "cloud:perplexity",
        "get_weather": "cloud:openweathermap",
        "query_cig": "local:cig",
        "hub_delegate": "local:pi-hub",
        "check_studio": "local:cig",
        "query_frameworks": "local:pcg",
        "recall_memory": "local:pcg",
        "save_memory": "local:pcg",
        "search_past_conversations": "local:sqlite",
        "tesla_control": "local:tesla-relay",
        "tesla_wake": "local:tesla-relay",
        "tesla_navigation": "local:tesla-relay",
        "tesla_stream_monitor": "local:tesla-relay",
        "tesla_location_refresh": "local:tesla-relay",
        "service_status": "local:docker",
        "homelab_diagnostics": "local:docker",
        "homelab_operations": "local:docker",
        "control_lights": "local:homeassistant",
        "get_workstation_status": "local:workstation",
        "manage_workspace": "local:pi-workspace",
        "manage_notes": "local:pi-workspace",
        "manage_timer": "local:nova",
        "set_reminder": "local:nova",
    }

    async def _record_learning_event(**kwargs):
        try:
            await append_learning_event(
                session_id=session.session_id,
                conversation_id=conversation_id,
                user_id=user_id,
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"Learning event append failed: {e}")

    def _build_spoken_ack(tool_name: str, args: dict) -> str | None:
        """Generate a contextual spoken acknowledgment from tool name + args.
        Returns None for fast tools that don't need an ack."""
        if tool_name == "hub_delegate":
            agent = args.get("agent", "")
            method = args.get("method", "")
            if agent:
                return f"On it — delegating to {agent} for {method}."
            return "Working on that for you."
        elif tool_name == "web_search":
            query = args.get("query", "")[:60]
            if query:
                return f"Searching for {query}."
            return "Searching the web."
        elif tool_name == "tesla_wake":
            return "Waking up your Tesla."
        elif tool_name == "manage_workspace":
            action = args.get("action", "")
            title = args.get("title", "")[:40]
            if action == "create_page_with_blocks" and title:
                return f"Creating {title} in your workspace."
            elif action == "create_from_template" and title:
                return f"Building {title} from template."
            elif action in ("create_page", "create_database", "create_form"):
                return f"Setting up {title or 'new ' + action.replace('create_', '')}."
            elif action == "search":
                return "Searching your workspace."
            return "Working on your workspace."
        elif tool_name == "staar_tutor":
            action = args.get("action", "")
            if action == "generate":
                return "Generating STAAR practice problems."
            elif action == "create_session":
                return "Setting up a practice session."
            elif action == "get_progress":
                return "Checking progress."
            return "Working on STAAR problems."
        elif tool_name == "compact_conversations":
            return "Compacting older conversations and extracting facts."
        return None

    def _build_thinking_text(tool_name: str, args: dict) -> str:
        """Generate a thinking description for the UI progress indicator."""
        desc_map = {
            "hub_delegate": f"🔧 Delegating to {args.get('agent', 'Hub')} agent: {args.get('method', '')}...",
            "query_cig": f"📊 Querying CIG {args.get('domain', 'analytics')} analytics...",
            "tesla_control": "Checking Tesla vehicle status...",
            "tesla_stream_monitor": "Monitoring Tesla data stream...",
            "tesla_location_refresh": "Getting Tesla vehicle location...",
            "tesla_wake": "Waking up Tesla vehicle...",
            "tesla_navigation": f"Sending navigation to {args.get('destination', 'destination')[:40]}...",
            "web_search": f"Searching for {args.get('query', 'information')[:40]}...",
            "query_frameworks": f"🧠 Querying LIAM frameworks for {args.get('problem_description', 'decision support')[:40]}...",
            "service_status": f"Checking status of {args.get('container', 'services')}...",
            "homelab_diagnostics": "Running homelab diagnostics...",
            "manage_workspace": f"📝 Workspace: {args.get('action', 'processing')}...",
            "staar_tutor": f"📚 STAAR: {args.get('action', 'generating')} problems...",
            "compact_conversations": "🧠 Compacting conversations & extracting facts...",
            "analyze_spreadsheet": "📊 Fetching attachment and sending to Atlas for analysis...",
            "analyze_image": "👁️ Sending image to Qwen Vision model...",
        }
        return desc_map.get(tool_name, f"Executing {tool_name}...")

    def _is_search_tool(tool_name: str) -> bool:
        return tool_name in {"query_cig", "search_past_conversations", "check_studio"}

    def _stop_searching_message(tool_name: str) -> str:
        if tool_name == "query_cig":
            return (
                "SYSTEM: You have already searched CIG enough for this turn. "
                "Do not call query_cig, check_studio, or search_past_conversations again. "
                "Use the email/context results you already have. If the user asked to create workspace pages or documents, delegate to Scribe now with hub_delegate."
            )
        if tool_name == "search_past_conversations":
            return (
                "SYSTEM: You have already searched conversation history enough for this turn. "
                "Do not search again. Summarize what you found and continue the user's task. "
                "If document construction is requested, delegate to Scribe now."
            )
        return (
            "SYSTEM: You have already checked the local context enough for this turn. "
            "Do not call more search/read tools. Answer, ask one focused clarification, or delegate to the right agent now."
        )

    def make_tool_handler(tool_name: str):
        async def handler(params: FunctionCallParams):
            logger.info(f"🔥 HANDLER ENTERED for {tool_name}")
            args = dict(params.arguments)
            
            # Check for duplicate tool call (same tool with same args)
            # Skip dedup for long-running delegation tools where each call is unique
            args_str = json.dumps(args, sort_keys=True, default=str)
            if (tool_name not in _DEDUP_EXCLUDED_TOOLS and
                _last_tool_call["name"] == tool_name and 
                _last_tool_call["args"] == args_str and
                _last_tool_call["result"] is not None):
                logger.warning(f"🔥 Duplicate tool call detected: {tool_name}, forcing response")
                await params.result_callback(
                    f"SYSTEM: Duplicate {tool_name} call blocked. Do not call this tool again with the same arguments. "
                    "Use the prior result already in context and respond to the user now."
                )
                logger.info(f"🔥 Duplicate callback completed for {tool_name}")
                return

            if _search_tools_exhausted[0] and _is_search_tool(tool_name):
                logger.warning(f"NOVA_TRAFFIC | tool={tool_name} | provider=local:search | status=search_budget_exhausted")
                await params.result_callback(_stop_searching_message(tool_name))
                return
            
            _tool_calls_this_turn[0] += 1
            call_num = _tool_calls_this_turn[0]
            provider_class = _PROVIDER_CLASS.get(tool_name, "local:unknown")
            logger.info(f"🔥 Tool call #{call_num}: {tool_name} [{provider_class}] args={str(args)[:100]}")

            # ── Hard limit: pure runaway-loop guard (not a quality throttle) ──
            if call_num > _MAX_TOOL_CALLS_HARD_LIMIT:
                logger.warning(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | status=hard_limit_exceeded | total_calls={call_num}")
                await params.result_callback(
                    "SYSTEM: Runaway tool loop detected. You MUST respond to the user now "
                    "with whatever information you have gathered so far. Do NOT call any more tools."
                )
                return

            # ── Per-tool rate limit (cloud=tight, local=generous) ──
            tool_count = _per_tool_call_counts.get(tool_name, 0) + 1
            _per_tool_call_counts[tool_name] = tool_count
            tool_limit = _PER_TOOL_LIMITS.get(tool_name, 99)  # no limit for unlisted tools
            if tool_count > tool_limit:
                logger.warning(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | status=rate_limited | call={tool_count}/{tool_limit}")
                if _is_search_tool(tool_name):
                    _search_tools_exhausted[0] = True
                    await params.result_callback(_stop_searching_message(tool_name))
                else:
                    await params.result_callback(
                        f"SYSTEM: {tool_name} has already been called {tool_count - 1} times this turn. "
                        "Use the data you already have to answer the user. Do not call more tools unless required to complete a user-requested action."
                    )
                return

            # ── Auto-heartbeat after sustained tool chains ──
            if call_num == _MAX_TOOL_CALLS_BEFORE_HEARTBEAT and not _ack_sent_this_turn[0]:
                _ack_sent_this_turn[0] = True
                await _send_server_msg({"type": "heartbeat", "text": "Still working on it..."})
                logger.info(f"Auto-heartbeat at tool call #{call_num}")

            # ── Fast path: instant spoken acknowledgment (once per turn) ──
            if tool_name in _SLOW_TOOLS and not _ack_sent_this_turn[0]:
                ack = _build_spoken_ack(tool_name, args)
                if ack:
                    _ack_sent_this_turn[0] = True
                    await _send_server_msg({"type": "heartbeat", "text": ack})
                    logger.info(f"Dual-path ack: '{ack}'")

            # For slow tools, populate the ThinkingCard with progress
            if tool_name in _SLOW_TOOLS:
                thinking_text = _build_thinking_text(tool_name, args)
                await _send_server_msg({"phase": "thinking"})
                await _send_server_msg({
                    "type": "thinking",
                    "text": thinking_text,
                })
                
            # Emit granular validationStep for UI
            await _send_server_msg({
                "type": "validationStep",
                "tool": tool_name,
                "status": "running",
            })
            
            from nova.hypothesis import get_hypothesis_validator
            validator = get_hypothesis_validator()
            if validator and validator.active:
                # We skip sending the message again since we just sent it, 
                # but we need to record it in the session
                validator.current_session.start_tool(tool_name)

            await _record_learning_event(
                event_type="tool_call_started",
                source_layer="llm_tool_loop",
                raw_text=_latest_user_event.get("raw_text", ""),
                canonical_text=_latest_user_event.get("canonical_text", ""),
                location=_latest_user_event.get("location", ""),
                mode_policy=_latest_user_event.get("mode_policy", ""),
                tool_name=tool_name,
                tool_args=args,
                payload={
                    "provider_class": provider_class,
                    "tool_call_index": call_num,
                },
            )

            # ── Background path: tool executes ──
            _t_start = time.monotonic()
            
            async def _heartbeat_loop():
                try:
                    while True:
                        await asyncio.sleep(15)
                        await _send_server_msg({"type": "heartbeat", "text": "Still working on it..."})
                except asyncio.CancelledError:
                    pass

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            
            try:
                # Add timeout to prevent tools from hanging indefinitely
                # Slow delegation tools need much longer timeouts
                _SLOW_TOOL_TIMEOUTS = {
                    "hub_delegate": 300,        # 5 min — Hub RPC with approval flow
                    "web_search": 30,           # 30s — Perplexity Sonar
                    "tesla_wake": 60,           # 60s — wake + poll for vehicle online
                    "service_status": 30,       # 30s — Docker API
                    "homelab_diagnostics": 60,  # 60s — Full diagnostics
                    "manage_workspace": 300,    # 5 min — API latency + potential approval flow
                }
                tool_timeout = _SLOW_TOOL_TIMEOUTS.get(tool_name, 30.0)
                result = await asyncio.wait_for(dispatch_tool(tool_name, args), timeout=tool_timeout)
                result_str = str(result)
                _latency_ms = int((time.monotonic() - _t_start) * 1000)
                _bytes_out = len(result_str.encode())
                logger.info(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | latency={_latency_ms}ms | bytes_out={_bytes_out} | status=ok")
                logger.info(f"Tool {tool_name} returned type={type(result).__name__}, len={len(result_str)}, content={result_str[:200]}")
                lower_result = result_str.lower()
                if _is_search_tool(tool_name) and (
                    "no cig knowledge graph results" in lower_result
                    or "no emails found" in lower_result
                    or "thread lookup failed" in lower_result
                    or "thread not found" in lower_result
                    or "you've already searched" in lower_result
                    or "no past conversations found" in lower_result
                ):
                    _search_tools_exhausted[0] = True
            except asyncio.TimeoutError:
                _latency_ms = int((time.monotonic() - _t_start) * 1000)
                logger.error(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | latency={_latency_ms}ms | bytes_out=0 | status=timeout")
                result = f"Tool {tool_name} timed out after {tool_timeout:.0f}s. The operation took too long to complete."
                await _send_server_msg({
                    "type": "validationStep",
                    "tool": tool_name,
                    "status": "failed",
                    "result": "Timed out",
                })
                if validator and validator.active:
                    validator.current_session.fail_tool(tool_name, "Timed out")
            except Exception as e:
                _latency_ms = int((time.monotonic() - _t_start) * 1000)
                logger.error(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | latency={_latency_ms}ms | bytes_out=0 | status=error | err={e}")
                result = f"Tool execution error: {str(e)}"
                await _send_server_msg({
                    "type": "validationStep",
                    "tool": tool_name,
                    "status": "failed",
                    "result": "Error occurred",
                })
                if validator and validator.active:
                    validator.current_session.fail_tool(tool_name, str(e))
            finally:
                heartbeat_task.cancel()
            
            # Structured-card support: tools may return a dict of the shape
            #   {"speakable": "<text for LLM/TTS>", "card": {"kind": "...", ...}}
            # in which case we forward the card to iOS as a server message and
            # only feed the speakable text back to the LLM. Falls through to
            # normal string handling for every other tool.
            if isinstance(result, dict) and result.get("display") and result.get("speech"):
                display_text = str(result.get("display") or "")
                speech_text = str(result.get("speech") or result.get("speakable") or display_text)
                card_payload = result.get("card") or {}
                if card_payload:
                    try:
                        await _send_server_msg({
                            "type": "card",
                            "kind": card_payload.get("kind", "generic"),
                            "tool": tool_name,
                            "data": card_payload,
                        })
                        logger.info(f"Emitted card ({card_payload.get('kind')}) for tool {tool_name}")
                    except Exception as e:
                        logger.error(f"Failed to emit card for {tool_name}: {e}")
                from nova.text_utils import strip_markdown_for_speech
                speech_text = strip_markdown_for_speech(speech_text)
                await _send_server_msg({
                    "type": "validated",
                    "text": display_text,
                    "speechText": speech_text,
                    "result": tool_name,
                    "suppressSpeech": False,
                })
                if display_text.strip():
                    await append_turn(session.session_id, "assistant", display_text)
                    asyncio.create_task(_sync_message_to_backend(
                        conversation_id, user_id, "assistant", display_text,
                        model=LLM_MODEL,
                    ))
                _structured_final_response_this_turn[0] = True
                result = f"{tool_name} completed. A structured visual response and separate speech summary have already been sent to the user. Do not add another answer."
            elif isinstance(result, dict) and "card" in result and "speakable" in result:
                card_payload = result.get("card") or {}
                try:
                    await _send_server_msg({
                        "type": "card",
                        "kind": card_payload.get("kind", "generic"),
                        "tool": tool_name,
                        "data": card_payload,
                    })
                    logger.info(f"Emitted card ({card_payload.get('kind')}) for tool {tool_name}")
                except Exception as e:
                    logger.error(f"Failed to emit card for {tool_name}: {e}")
                result = str(result.get("speakable") or "")

            # Validate result is not empty
            result_str = str(result) if result is not None else ""
            if not result_str or not result_str.strip():
                logger.error(f"Tool {tool_name} returned empty result (type={type(result).__name__}), injecting error message")
                result = f"Tool {tool_name} executed but returned no data. Please try again or use a different approach."
                await _send_server_msg({
                    "type": "validationStep",
                    "tool": tool_name,
                    "status": "failed",
                    "result": "No data returned",
                })
                if validator and validator.active:
                    validator.current_session.fail_tool(tool_name, "No data returned")
            else:
                result = result_str
                # Only emit completed if it wasn't already emitted as failed in the except blocks
                # We can check if it's an error string, but simply emitting completed here is fine for successful paths.
                # Actually, wait, the exception blocks set result to a string and continue, so they fall through here!
                # We should conditionally emit "completed" only if not an error.
                if not result_str.startswith("Tool execution error:") and not result_str.startswith(f"Tool {tool_name} timed out"):
                    # Extract a snippet for the UI
                    snippet = result_str[:100]
                    await _send_server_msg({
                        "type": "validationStep",
                        "tool": tool_name,
                        "status": "completed",
                        "result": snippet,
                    })
                    if validator and validator.active:
                        validator.current_session.complete_tool(tool_name, snippet)

            # Clear ThinkingCard phase so the LLM's next response is spoken, not swallowed
            if tool_name in _SLOW_TOOLS:
                await _send_server_msg({"phase": "done"})

            final_result_str = str(result) if result is not None else ""
            tool_success = bool(final_result_str.strip()) and not final_result_str.startswith("Tool execution error:") and not final_result_str.startswith(f"Tool {tool_name} timed out")
            await _record_learning_event(
                event_type="tool_call_completed",
                source_layer="llm_tool_loop",
                raw_text=_latest_user_event.get("raw_text", ""),
                canonical_text=_latest_user_event.get("canonical_text", ""),
                location=_latest_user_event.get("location", ""),
                mode_policy=_latest_user_event.get("mode_policy", ""),
                tool_name=tool_name,
                tool_args=args,
                success=tool_success,
                latency_ms=locals().get("_latency_ms", 0),
                outcome="success" if tool_success else "failed",
                payload={
                    "provider_class": provider_class,
                    "result_preview": final_result_str[:500],
                    "result_chars": len(final_result_str),
                    "promotion_candidate": tool_success and tool_name in {"save_memory", "recall_memory", "query_cig", "hub_delegate", "tesla_control", "get_weather"},
                },
            )

            # ── Result path: feed back to LLM for comprehensive spoken response ──
            logger.info(f"🔥 Calling result_callback for {tool_name} with {len(str(result))} chars: {str(result)[:150]}")
            try:
                await params.result_callback(result)
                logger.info(f"🔥 result_callback completed for {tool_name}")
                # Cache the result for deduplication
                _last_tool_call["name"] = tool_name
                _last_tool_call["args"] = json.dumps(args, sort_keys=True, default=str)
                _last_tool_call["result"] = result
                logger.info(f"🔥 Cached result for {tool_name}")
                # Small delay to ensure Pipecat processes the result before handler returns
                await asyncio.sleep(0.1)
                logger.info(f"🔥 HANDLER COMPLETE for {tool_name}")
            except Exception as e:
                logger.error(f"🔥 result_callback failed for {tool_name}: {e}", exc_info=True)
        return handler

    # Native casual tools
    llm.register_function("get_weather", make_tool_handler("get_weather"))
    llm.register_function("control_lights", make_tool_handler("control_lights"))
    llm.register_function("get_workstation_status", make_tool_handler("get_workstation_status"))
    llm.register_function("set_reminder", make_tool_handler("set_reminder"))
    # Search (fast, grounded via Perplexity Sonar)
    llm.register_function("web_search", make_tool_handler("web_search"))
    # Conversation search (recall past discussions)
    llm.register_function("search_past_conversations", make_tool_handler("search_past_conversations"))
    # Memory tools (PCG — Personal Context Graph)
    llm.register_function("save_memory", make_tool_handler("save_memory"))
    llm.register_function("recall_memory", make_tool_handler("recall_memory"))
    llm.register_function("forget_memory", make_tool_handler("forget_memory"))
    # Delegated (actions requiring browser/email/calendar/shell)
    # Hub Agent delegation (Pi Agent Hub background agents)
    llm.register_function("hub_delegate", make_tool_handler("hub_delegate"))
    # CIG analytics (Communication Intelligence Graph)
    llm.register_function("query_cig", make_tool_handler("query_cig"))
    # Studio quick-reads (direct dashboard API)
    llm.register_function("check_studio", make_tool_handler("check_studio"))
    # Skill discovery (dynamic skill catalog)
    llm.register_function("discover_skills", make_tool_handler("discover_skills"))
    # Network diagnostics
    llm.register_function("diagnose_network", make_tool_handler("diagnose_network"))
    # Time (grounded current date/time)
    llm.register_function("get_time", make_tool_handler("get_time"))
    # Timers & alarms
    llm.register_function("manage_timer", make_tool_handler("manage_timer"))
    # PCG - link goals to knowledge graph
    llm.register_function("link_goal_to_knowledge", make_tool_handler("link_goal_to_knowledge"))

    # ── Personal Context Graph (PCG) ────────────────────────────────────────
    llm.register_function("query_context", make_tool_handler("query_context"))
    llm.register_function("kg_query", make_tool_handler("kg_query"))
    llm.register_function("knowledge_query", make_tool_handler("knowledge_query"))
    llm.register_function("get_enriched_context", make_tool_handler("get_enriched_context"))
    llm.register_function("query_frameworks", make_tool_handler("query_frameworks"))
    # Homelab infrastructure operations (Docker container management)
    llm.register_function("service_status", make_tool_handler("service_status"))
    llm.register_function("service_logs", make_tool_handler("service_logs"))
    llm.register_function("service_restart", make_tool_handler("service_restart"))
    llm.register_function("service_start", make_tool_handler("service_start"))
    llm.register_function("service_stop", make_tool_handler("service_stop"))
    llm.register_function("service_health_check", make_tool_handler("service_health_check"))
    llm.register_function("homelab_heartbeat", make_tool_handler("homelab_heartbeat"))

    # ── Tesla vehicle control (unified tool + legacy compatibility) ───────
    llm.register_function("tesla_control", make_tool_handler("tesla_control"))
    llm.register_function("tesla_stream_monitor", make_tool_handler("tesla_stream_monitor"))
    llm.register_function("tesla_location_refresh", make_tool_handler("tesla_location_refresh"))
    llm.register_function("tesla_wake", make_tool_handler("tesla_wake"))
    llm.register_function("tesla_navigation", make_tool_handler("tesla_navigation"))

    # ── STAAR Tutor (TEKS-aligned problem generation) ───────────────────
    llm.register_function("staar_tutor", make_tool_handler("staar_tutor"))

    # ── Conversation Compaction (negative exponential decay + PCG facts) ──
    llm.register_function("compact_conversations", make_tool_handler("compact_conversations"))

    # ── Analyze Spreadsheet (email attachment → Atlas data analysis) ──
    llm.register_function("analyze_spreadsheet", make_tool_handler("analyze_spreadsheet"))
    llm.register_function("analyze_image", make_tool_handler("analyze_image"))

    # ── Missing tool registrations ──────────────────────────────────────
    llm.register_function("manage_ticket", make_tool_handler("manage_ticket"))
    llm.register_function("query_workspace", make_tool_handler("query_workspace"))
    llm.register_function("manage_workspace", make_tool_handler("manage_workspace"))
    llm.register_function("manage_notes", make_tool_handler("manage_notes"))
    llm.register_function("exomind", make_tool_handler("exomind"))
    llm.register_function("ev_route_planner", make_tool_handler("ev_route_planner"))
    llm.register_function("youtube", make_tool_handler("youtube"))
    llm.register_function("homelab_operations", make_tool_handler("homelab_operations"))
    llm.register_function("homelab_diagnostics", make_tool_handler("homelab_diagnostics"))

    # ── Context with history restoration ─────────────────────────────────
    messages = [{"role": "system", "content": system_prompt}]

    # Restore prior conversation turns from DB
    for turn in prior_turns:
        msg = {"role": turn.role, "content": turn.content}
        if turn.tool_calls:
            import json as _json
            try:
                msg["tool_calls"] = _json.loads(turn.tool_calls)
            except Exception:
                pass
        messages.append(msg)

    if prior_turns:
        logger.info(f"Restored {len(prior_turns)} turns into LLM context")

    context = LLMContext(messages=messages)
    # Register tools with context
    context.set_tools(PIPECAT_TOOLS)
    
    # Log what messages are being sent to LLM
    logger.info(f"LLM context messages ({len(messages)}):")
    for i, msg in enumerate(messages[-3:]):  # Show last 3 messages
        role = msg.get("role", "")
        content = msg.get("content", "")
        logger.info(f"  [{i+1}] {role}: {content[:100]}...")

    # ── Persistence interceptor (sits before assistant_aggregator) ────────
    # The LLMAssistantContextAggregator swallows LLMTextFrame / LLMFullResponse*
    # frames without re-emitting them, so the task-level downstream observer
    # never fires.  This lightweight processor intercepts those frames BEFORE
    # they are consumed.
    _pi_buffer: list[str] = []
    _pi_thinking_sent: list[bool] = [False]
    _pi_first_text: list[bool] = [False]

    class PersistenceInterceptor(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if isinstance(frame, LLMFullResponseStartFrame):
                _pi_first_text[0] = False
                if not _structured_final_response_this_turn[0]:
                    _pi_thinking_sent[0] = True
                    await _send_server_msg({"phase": "thinking"})
                    await ctx_watcher.check_and_persist()

            elif isinstance(frame, LLMTextFrame):
                if _structured_final_response_this_turn[0]:
                    return
                _pi_buffer.append(frame.text)
                if not _pi_first_text[0]:
                    _pi_first_text[0] = True
                    await _send_server_msg({"phase": "responding"})

            elif isinstance(frame, LLMFullResponseEndFrame):
                if _structured_final_response_this_turn[0]:
                    _pi_buffer.clear()
                    _structured_final_response_this_turn[0] = False
                elif _pi_buffer:
                    full_text = "".join(_pi_buffer)
                    _pi_buffer.clear()
                    if full_text.strip():
                        await append_turn(session.session_id, "assistant", full_text)
                        asyncio.create_task(_sync_message_to_backend(
                            conversation_id, user_id, "assistant", full_text,
                            model=LLM_MODEL,
                        ))
                        logger.info(f"Persisted assistant turn ({len(full_text)} chars)")
                        from nova.text_utils import strip_markdown_for_speech
                        speech_text = strip_markdown_for_speech(full_text)
                        await _send_server_msg({
                            "type": "validated",
                            "text": full_text,
                            "speechText": speech_text,
                            "result": "direct",
                            # RTVI already streams each LLMTextFrame to iOS as bot-llm-text
                            # (which iOS speaks natively). suppressSpeech=True prevents the
                            # validated event from triggering a second TTS pass.
                            "suppressSpeech": True,
                        })
                await _send_server_msg({"type": "turn_complete"})
                if _pi_thinking_sent[0]:
                    await _send_server_msg({"phase": "done"})
                    _pi_thinking_sent[0] = False
                
                # Async background learning consolidation
                asyncio.create_task(consolidate_session_learning(session.session_id))

            await self.push_frame(frame, direction)

    class TurnOrchestratorFrameProcessor(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            text = ""
            if isinstance(frame, InputTransportMessageFrame):
                msg = frame.message
                if isinstance(msg, dict) and msg.get("type") == "send-text":
                    data = msg.get("data", {})
                    text = data.get("content", "") if isinstance(data, dict) else ""
            elif isinstance(frame, TranscriptionFrame):
                if getattr(frame, "user_id", "") != "system":
                    text = getattr(frame, "text", "") or ""

            if text:
                canonical = canonicalize_turn_text(text)
                _latest_user_event.clear()
                _latest_user_event.update(canonical.to_dict())
                plan = await decide_turn(text, _turn_state)
                await _record_learning_event(
                    event_type="user_turn_received",
                    source_layer="transport",
                    raw_text=canonical.raw_text,
                    canonical_text=canonical.canonical_text,
                    location=canonical.location,
                    mode_policy=canonical.mode_policy,
                    outcome="received" if canonical.canonical_text else "blank_after_canonicalization",
                    payload={
                        "audio_mode": audio_mode,
                        "frame_type": type(frame).__name__,
                    },
                )
                await _record_learning_event(
                    event_type="orchestrator_decision",
                    source_layer="native_turn_orchestrator",
                    raw_text=canonical.raw_text,
                    canonical_text=canonical.canonical_text,
                    location=canonical.location,
                    mode_policy=canonical.mode_policy,
                    outcome=plan.intent.value,
                    payload={
                        "intent": plan.intent.value,
                        "goal": plan.goal,
                        "allowed_tools": plan.allowed_tools,
                        "evidence_budget": plan.evidence_budget,
                        "stop_conditions": plan.stop_conditions,
                    },
                )

                async def _persist(role: str, content: str):
                    await append_turn(session.session_id, role, content)
                    asyncio.create_task(_sync_message_to_backend(
                        conversation_id, user_id, role, content,
                        model=LLM_MODEL if role == "assistant" else None,
                    ))

                handled = await execute_turn_plan(
                    plan,
                    _turn_state,
                    dispatch_tool,
                    _send_server_msg,
                    _persist,
                )
                if handled:
                    try:
                        await update_session_metadata_key(
                            session.session_id,
                            STATE_METADATA_KEY,
                            turn_state_to_metadata_value(_turn_state),
                        )
                    except Exception as e:
                        logger.warning(f"TurnOrchestrator state persist failed: {e}")
                    return

                if plan.learned_candidate:
                    tools = plan.learned_candidate.get("tools_used", [])
                    if tools:
                        tool_name = tools[0]
                        instruction = f"\n\n[SYSTEM ASSISTIVE ROUTING: The user's request matches a learned pattern for the '{tool_name}' tool. Call this tool immediately to assist them.]"
                        if isinstance(frame, InputTransportMessageFrame):
                            if isinstance(frame.message, dict) and frame.message.get("type") == "send-text":
                                if isinstance(frame.message.get("data"), dict):
                                    frame.message["data"]["content"] += instruction
                        elif isinstance(frame, TranscriptionFrame):
                            frame.text += instruction
                        logger.info(f"NOVA_LEARNING_ROUTING | Injected assistive routing for {tool_name}")

            await self.push_frame(frame, direction)

    turn_orchestrator = TurnOrchestratorFrameProcessor()

    # Build pipeline based on audio mode
    if use_server_audio:
        # Server-side STT/TTS mode using local Whisper + Qwen TTS
        stt = WhisperSTTService(
            model="Systran/faster-whisper-medium",
            device="cuda",
            compute_type="float16",
        )
        tts = QwenTTSService(
            voice="american_female_warm",
        )
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )
        pipeline = Pipeline([
            transport.input(),
            stt,
            turn_orchestrator,
            user_aggregator,
            llm,
            PersistenceInterceptor(),
            tts,
            transport.output(),
            assistant_aggregator,
        ])
    else:
        # iOS native STT/TTS mode - text via data channel, no VAD needed
        from pipecat.frames.frames import (
            OutputTransportMessageFrame,
            TextFrame,
        )
        import re as _re

        # Regex to strip raw tool-call syntax the LLM may emit as text.
        # Matches patterns like: [web_search query="..."], [get_weather location="..."]
        _TOOL_CALL_TEXT_RE = _re.compile(
            r'\[(?:web_search|get_weather|check_studio|recall_memory|save_memory|forget_memory'
            r'|control_lights|get_workstation_status|set_reminder|hub_delegate'
            r'|search_past_conversations|discover_skills|diagnose_network|get_time'
            r'|manage_timer|manage_ticket|manage_workspace|manage_notes|exomind'
            r'|service_status|service_logs|service_restart|service_start|service_stop'
            r'|service_health_check|homelab_operations|homelab_diagnostics'
            r'|ev_route_planner|tesla_location_refresh|tesla_control|youtube'
            r'|knowledge_query|get_enriched_context|link_goal_to_knowledge'
            r'|query_frameworks|analyze_image|analyze_spreadsheet)\b[^\]]*\]',
            _re.IGNORECASE,
        )
        _native_tts_in_table: list[bool] = [False]

        def _suppress_markdown_table_chunks(text: str) -> str:
            parts = _re.split("(\n)", text)
            kept: list[str] = []
            current = ""
            for part in parts:
                if part == "\n":
                    stripped = current.strip()
                    if stripped.startswith("|") or _native_tts_in_table[0]:
                        _native_tts_in_table[0] = False
                    else:
                        kept.append(current)
                        kept.append(part)
                    current = ""
                    continue
                current += part
            if current:
                stripped = current.strip()
                if stripped.startswith("|"):
                    _native_tts_in_table[0] = True
                elif _native_tts_in_table[0]:
                    if "|" in current:
                        _native_tts_in_table[0] = True
                    else:
                        _native_tts_in_table[0] = False
                        kept.append(current)
                else:
                    kept.append(current)
            return "".join(kept)

        class NativeTextBridge(FrameProcessor):
            """Bridge for native STT/TTS mode with tool-syntax filtering.

            Pipecat's RTVI processor handles 'send-text' messages (user→LLM)
            and emits 'bot-llm-text' events (LLM→user) natively.
            This processor also strips any raw tool-call text the LLM may
            accidentally emit (e.g. '[web_search query="..."]') so it is
            never spoken or displayed on iOS.
            """
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)

                if isinstance(frame, InputTransportMessageFrame):
                    msg = frame.message
                    if isinstance(msg, dict) and msg.get("type") == "send-text":
                        data = msg.get("data", {})
                        text = data.get("content", "") if isinstance(data, dict) else ""
                        if text:
                            # Detect orchestration mode from iOS prefix and update thinking level
                            if "MODE POLICY:" in text:
                                if "FAST" in text.split("MODE POLICY:")[1].split("\n")[0]:
                                    llm.set_thinking("low")
                                    set_web_search_agent_mode("fast")
                                elif "DEEP" in text.split("MODE POLICY:")[1].split("\n")[0]:
                                    llm.set_thinking("high")
                                    set_web_search_agent_mode("deep")
                                else:
                                    llm.set_thinking("medium")
                                    set_web_search_agent_mode("fast")
                            logger.info(f"Native STT → LLM (RTVI): {text[:80]}")

                elif isinstance(frame, OutputTransportMessageFrame):
                    msg = frame.message
                    if isinstance(msg, dict):
                        msg_type = msg.get("type", "")
                        logger.info(f"LLM → Client ({msg_type}): {str(msg)[:120]}")

                # Filter LLMTextFrames: strip raw tool-call syntax + markdown for TTS
                if isinstance(frame, LLMTextFrame):
                    original = frame.text
                    # Step 1: Strip raw tool-call syntax
                    cleaned = _TOOL_CALL_TEXT_RE.sub('', original)
                    if cleaned != original:
                        logger.warning(f"🧹 Stripped tool syntax from LLM text: {original[:120]}")
                        if not cleaned.strip():
                            # Entire frame was tool syntax — drop it
                            return
                    cleaned = _suppress_markdown_table_chunks(cleaned)
                    if not cleaned.strip():
                        return
                    # Step 2: Strip markdown for TTS (bold, headers, pipes, emojis)
                    # iOS TTS reads raw markdown verbatim — pipes become "pipe", dashes become "minus"
                    from nova.text_utils import strip_markdown_for_speech
                    pre_strip = cleaned
                    cleaned = strip_markdown_for_speech(cleaned)
                    if cleaned != pre_strip:
                        logger.debug(f"🧼 Stripped markdown from LLM text: '{pre_strip[:80]}' → '{cleaned[:80]}'")
                    if cleaned != original:
                        frame.text = cleaned

                # Log LLMFullResponseStartFrame/EndFrame
                if isinstance(frame, LLMFullResponseStartFrame):
                    _native_tts_in_table[0] = False
                    logger.info(f"✅ LLMFullResponseStartFrame → triggers bot-llm-started")
                if isinstance(frame, LLMFullResponseEndFrame):
                    _native_tts_in_table[0] = False
                    logger.info(f"✅ LLMFullResponseEndFrame → triggers bot-llm-stopped")

                await self.push_frame(frame, direction)

        text_bridge = NativeTextBridge()
        # Use the same aggregator pair as server mode, just without VAD
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

        pipeline = Pipeline([
            transport.input(),
            turn_orchestrator,
            user_aggregator,
            llm,
            PersistenceInterceptor(),
            assistant_aggregator,
            text_bridge,
            transport.output(),
        ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Don't kill the connection during long tool calls (Hub delegation can take 30-60s).
        # The idle observer only watches BotSpeaking/UserSpeaking frames, which aren't
        # emitted during tool execution, so it fires prematurely.
        cancel_on_idle_timeout=False,
        rtvi_observer_params=RTVIObserverParams(
            # Expose function call details so iOS SDK fires
            # onLLMFunctionCallStarted / InProgress / Stopped callbacks
            function_call_report_level={
                "*": RTVIFunctionCallReportLevel.NAME,
                "hub_delegate": RTVIFunctionCallReportLevel.FULL,
            },
        ),
    )
    # Wire up RTVI ref now that task exists
    _rtvi_ref[0] = task._rtvi

    # Let tools.py send server messages (e.g. citations from web_search)
    from nova.tools import set_server_msg_fn, set_web_search_agent_mode
    set_server_msg_fn(_send_server_msg)
    # Default to medium thinking; NativeTextBridge updates this per message based on MODE POLICY

    # Persist user messages by hooking the context aggregator
    _original_context_messages = context.messages

    class _ContextWatcher:
        """Watch LLMContext.messages for new user turns and persist them."""
        def __init__(self):
            self._last_len = len(_original_context_messages)

        async def check_and_persist(self):
            msgs = context.messages
            if len(msgs) > self._last_len:
                for msg in msgs[self._last_len:]:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user" and content:
                        # New user turn — reset per-turn dedup, tool counter, per-tool counts, and dedup cache
                        _ack_sent_this_turn[0] = False
                        _tool_calls_this_turn[0] = 0
                        _per_tool_call_counts.clear()
                        _last_tool_call.update({"name": None, "args": None, "result": None})
                        _structured_final_response_this_turn[0] = False
                        _search_tools_exhausted[0] = False
                        await append_turn(session.session_id, "user", content)
                        await _sync_message_to_backend(
                            conversation_id, user_id, "user", content
                        )
                        logger.debug(f"Persisted user turn ({len(content)} chars)")
                self._last_len = len(msgs)

    ctx_watcher = _ContextWatcher()

    # Event bus: proactive notifications while user is connected
    event_handler = create_event_handler(task, user_id)

    # Progress callback for Hub delegation streaming updates
    async def on_hub_progress(status_type: str, message: str):
        """Send Hub delegation progress to iOS ThinkingCard via server messages.
        
        Uses custom message types so they don't pollute the LLM response:
        - phase     → {phase: ...}      — sets processingStatus label on ThinkingCard
        - status    → {type: heartbeat}  — periodic 'still working' pulses (spoken via TTS)
        - thinking  → {type: thinking}   — tool/reasoning updates for ThinkingCard
        - narration → {type: thinking}   — spoken channel deltas for ThinkingCard
        """
        logger.info(f"Hub progress ({status_type}): {message[:80]}")
        if status_type == "phase":
            await _send_server_msg({"phase": message})
        elif status_type == "status":
            await _send_server_msg({"type": "heartbeat", "text": message})
        elif status_type in ("thinking", "narration"):
            await _send_server_msg({"type": "thinking", "text": message})

    # Keepalive task to prevent iOS WebRTC from closing idle data channel connections
    _keepalive_task: list[asyncio.Task | None] = [None]

    async def _keepalive_loop():
        """Send periodic pings to keep the data channel alive."""
        while True:
            await asyncio.sleep(10)  # Every 10 seconds
            try:
                await _send_server_msg({"type": "ping"})
                logger.debug("Sent keepalive ping")
            except Exception as e:
                logger.warning(f"Keepalive ping failed: {e}")
                break

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected (user={user_id}), session={session.session_id}")
        mark_user_active(user_id)
        event_bus.subscribe_user(user_id, event_handler)
        set_progress_context(on_hub_progress, user_id)
        # Start keepalive to prevent iOS from closing idle connections
        _keepalive_task[0] = asyncio.create_task(_keepalive_loop())
        # Skip LLM-generated greeting (MiniMax calls tools unprompted on
        # system-only prompts). iOS client already shows its own greeting.
        # The LLM will activate on the first real user message.
        if prior_turns:
            logger.info(f"Resuming conversation with {len(prior_turns)} prior turns")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Client disconnected (user={user_id})")
        # Cancel keepalive task
        if _keepalive_task[0] and not _keepalive_task[0].done():
            _keepalive_task[0].cancel()
        event_bus.unsubscribe_user(user_id, event_handler)
        mark_user_inactive(user_id)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


# ---------------------------------------------------------------------------
# Standalone runner (development)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequestHandler,
        SmallWebRTCRequest,
        SmallWebRTCPatchRequest,
        IceCandidate,
    )

    import asyncio
    import uvicorn
    from nova.webhooks import app as webhook_app, WEBHOOK_PORT

    # Create FastAPI app for WebRTC signaling
    webrtc_app = FastAPI(title="Nova Agent WebRTC")
    webrtc_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # WebRTC request handler
    request_handler = SmallWebRTCRequestHandler()

    @webrtc_app.websocket("/")
    async def websocket_root(websocket: WebSocket):
        """Handle WebSocket-based WebRTC signaling at root path (iOS default)."""
        await websocket.accept()
        logger.info(f"WebSocket connected at /: {websocket.client}")
        
        try:
            # First message: auth/config from iOS
            auth_data = await websocket.receive_json()
            logger.info(f"WebSocket auth received: {list(auth_data.keys())}")
            
            # Extract auth fields
            user_id = _resolve_user_id(auth_data.get("user_id", "default"))
            audio_mode = auth_data.get("audio_mode", "native")
            conversation_id = auth_data.get("conversation_id", "default")
            token = auth_data.get("token", "")
            
            # TODO: Validate token if needed
            logger.info(f"Auth accepted: user={user_id}, audio={audio_mode}, conv={conversation_id}")
            
            # Send nova.connected to signal iOS to proceed with WebRTC setup
            await websocket.send_json({"type": "nova.connected", "status": "ready"})
            logger.info("Sent nova.connected to iOS")
            
            # Wait for SDP offer from iOS
            logger.info("Waiting for SDP offer from iOS...")
            
            # Next message: SDP offer from iOS
            data = await websocket.receive_json()
            logger.info(f"WebSocket SDP received: {list(data.keys())}")
            
            # Extract custom app fields
            WEBRTC_KEYS = {"sdp", "type", "pc_id", "restart_pc", "request_data", "requestData"}
            app_data = {k: v for k, v in data.items() if k not in WEBRTC_KEYS}
            webrtc_body = {k: v for k, v in data.items() if k in WEBRTC_KEYS}
            
            # Add auth info to request_data
            app_data["user_id"] = user_id
            app_data["audio_mode"] = audio_mode
            app_data["conversation_id"] = conversation_id
            
            # Merge app-level fields into request_data
            existing_rd = webrtc_body.get("request_data") or webrtc_body.pop("requestData", None) or {}
            if isinstance(existing_rd, dict):
                existing_rd.update(app_data)
            else:
                existing_rd = app_data
            webrtc_body["request_data"] = existing_rd
            
            webrtc_request = SmallWebRTCRequest.from_dict(webrtc_body)
            
            async def on_connection(connection):
                rd = webrtc_request.request_data or {}
                user_id = _resolve_user_id(rd.get("user_id", "default"))
                audio_mode = rd.get("audio_mode", "native")
                conversation_id = rd.get("conversation_id", "default")
                logger.info(f"Starting bot: user={user_id}, audio={audio_mode}, conv={conversation_id}")
                asyncio.create_task(run_bot(connection, user_id, audio_mode, conversation_id))
            
            answer = await request_handler.handle_web_request(webrtc_request, on_connection)
            answer["sessionId"] = answer.get("pc_id", "")
            
            # Send SDP answer back to iOS
            await websocket.send_json(answer)
            logger.info(f"WebSocket sent answer: pc_id={answer.get('pc_id')}")
            
            # Keep connection open for ICE candidates
            while True:
                try:
                    ice_data = await websocket.receive_json()
                    logger.info(f"WebSocket ICE candidates: {len(ice_data.get('candidates', []))}")
                    
                    candidates = [
                        IceCandidate(
                            candidate=c.get("candidate", ""),
                            sdp_mid=c.get("sdpMid", ""),
                            sdp_mline_index=c.get("sdpMLineIndex", 0),
                        )
                        for c in ice_data.get("candidates", [])
                    ]
                    patch_request = SmallWebRTCPatchRequest(
                        pc_id=ice_data.get("pc_id", answer.get("pc_id", "")),
                        candidates=candidates,
                    )
                    await request_handler.handle_patch_request(patch_request)
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected")
                    break
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                    break
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            await websocket.close()

    @webrtc_app.websocket("/connect")
    async def websocket_connect(websocket: WebSocket):
        """Handle WebSocket-based WebRTC signaling from iOS client."""
        await websocket.accept()
        logger.info(f"WebSocket connected: {websocket.client}")
        
        try:
            # Wait for SDP offer from iOS
            data = await websocket.receive_json()
            logger.info(f"WebSocket received: {list(data.keys())}")
            
            # Extract custom app fields
            WEBRTC_KEYS = {"sdp", "type", "pc_id", "restart_pc", "request_data", "requestData"}
            app_data = {k: v for k, v in data.items() if k not in WEBRTC_KEYS}
            webrtc_body = {k: v for k, v in data.items() if k in WEBRTC_KEYS}
            
            # Merge app-level fields into request_data
            existing_rd = webrtc_body.get("request_data") or webrtc_body.pop("requestData", None) or {}
            if isinstance(existing_rd, dict):
                existing_rd.update(app_data)
            else:
                existing_rd = app_data
            webrtc_body["request_data"] = existing_rd
            
            webrtc_request = SmallWebRTCRequest.from_dict(webrtc_body)
            
            async def on_connection(connection):
                rd = webrtc_request.request_data or {}
                user_id = _resolve_user_id(rd.get("user_id", "default"))
                audio_mode = rd.get("audio_mode", "native")
                conversation_id = rd.get("conversation_id", "default")
                logger.info(f"Starting bot: user={user_id}, audio={audio_mode}, conv={conversation_id}")
                asyncio.create_task(run_bot(connection, user_id, audio_mode, conversation_id))
            
            answer = await request_handler.handle_web_request(webrtc_request, on_connection)
            answer["sessionId"] = answer.get("pc_id", "")
            
            # Send SDP answer back to iOS
            await websocket.send_json(answer)
            logger.info(f"WebSocket sent answer: pc_id={answer.get('pc_id')}")
            
            # Keep connection open for ICE candidates
            while True:
                try:
                    ice_data = await websocket.receive_json()
                    logger.info(f"WebSocket ICE candidates: {len(ice_data.get('candidates', []))}")
                    
                    candidates = [
                        IceCandidate(
                            candidate=c.get("candidate", ""),
                            sdp_mid=c.get("sdpMid", ""),
                            sdp_mline_index=c.get("sdpMLineIndex", 0),
                        )
                        for c in ice_data.get("candidates", [])
                    ]
                    patch_request = SmallWebRTCPatchRequest(
                        pc_id=ice_data.get("pc_id", answer.get("pc_id", "")),
                        candidates=candidates,
                    )
                    await request_handler.handle_patch_request(patch_request)
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected")
                    break
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                    break
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            await websocket.close()

    @webrtc_app.post("/connect")
    async def connect(request: Request):
        """Handle WebRTC connection requests from Pipecat iOS SDK."""
        try:
            # Log raw request for debugging
            raw_body = await request.body()
            content_type = request.headers.get("content-type", "")
            logger.info(f"WebRTC connect: content-type={content_type}, body_len={len(raw_body)}")
            
            if not raw_body:
                # iOS SDK sends empty POST first - create a new session
                import uuid
                session_id = str(uuid.uuid4())
                logger.info(f"Creating new session: {session_id}")
                return {"sessionId": session_id, "status": "ready"}
            
            body = json.loads(raw_body)
            logger.info(f"WebRTC connect body keys={list(body.keys())}")

            # If no SDP present, this is a pre-flight / session-init request
            if "sdp" not in body:
                import uuid
                session_id = str(uuid.uuid4())
                logger.info(f"No SDP in request, returning session: {session_id}")
                return {"sessionId": session_id, "status": "ready"}

            # Separate custom app fields from WebRTC-required fields.
            # SmallWebRTCRequest only accepts: sdp, type, pc_id, restart_pc, request_data
            WEBRTC_KEYS = {"sdp", "type", "pc_id", "restart_pc", "request_data", "requestData"}
            app_data = {k: v for k, v in body.items() if k not in WEBRTC_KEYS}
            webrtc_body = {k: v for k, v in body.items() if k in WEBRTC_KEYS}

            # Merge app-level fields into request_data so run_bot can read them
            existing_rd = webrtc_body.get("request_data") or webrtc_body.pop("requestData", None) or {}
            if isinstance(existing_rd, dict):
                existing_rd.update(app_data)
            else:
                existing_rd = app_data
            webrtc_body["request_data"] = existing_rd

            webrtc_request = SmallWebRTCRequest.from_dict(webrtc_body)

            async def on_connection(connection):
                # Extract user_id, audio_mode, conversation_id from request data
                rd = webrtc_request.request_data or {}
                user_id = _resolve_user_id(rd.get("user_id", "default"))
                audio_mode = rd.get("audio_mode", "native")
                conversation_id = rd.get("conversation_id", "default")
                logger.info(f"Starting bot: user={user_id}, audio={audio_mode}, conv={conversation_id}")
                asyncio.create_task(run_bot(connection, user_id, audio_mode, conversation_id))

            answer = await request_handler.handle_web_request(webrtc_request, on_connection)
            # Add sessionId to response for iOS SDK compatibility
            answer["sessionId"] = answer.get("pc_id", "")
            logger.info(f"WebRTC answer: pc_id={answer.get('pc_id')}")
            return answer
        except json.JSONDecodeError as e:
            logger.error(f"WebRTC connect JSON error: {e}, body={raw_body[:200]}")
            return {"error": "Invalid JSON", "detail": str(e)}
        except Exception as e:
            logger.error(f"WebRTC connect error: {e}")
            raise

    @webrtc_app.patch("/connect")
    async def patch_connect(request: Request):
        """Handle ICE candidate patches (iOS SDK uses PATCH /connect)."""
        try:
            body = await request.json()
            pc_id = body.get("pc_id", "")
            candidates = body.get("candidates", [])
            logger.info(f"ICE candidates for pc_id={pc_id}, count={len(candidates)}")
            
            ice_candidates = [
                IceCandidate(
                    candidate=c.get("candidate", ""),
                    sdp_mid=c.get("sdpMid", ""),
                    sdp_mline_index=c.get("sdpMLineIndex", 0),
                )
                for c in candidates
            ]
            patch_request = SmallWebRTCPatchRequest(pc_id=pc_id, candidates=ice_candidates)
            await request_handler.handle_patch_request(patch_request)
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"ICE candidate error: {e}")
            return {"error": str(e)}

    @webrtc_app.post("/ice-candidate")
    async def ice_candidate(request: Request):
        """Handle ICE candidate patches."""
        body = await request.json()
        candidates = [
            IceCandidate(
                candidate=c.get("candidate", ""),
                sdp_mid=c.get("sdpMid", ""),
                sdp_mline_index=c.get("sdpMLineIndex", 0),
            )
            for c in body.get("candidates", [])
        ]
        patch_request = SmallWebRTCPatchRequest(
            pc_id=body.get("pc_id", ""),
            candidates=candidates,
        )
        await request_handler.handle_patch_request(patch_request)
        return {"status": "ok"}

    @webrtc_app.get("/health")
    async def health():
        return {"status": "ok", "service": "nova-agent"}

    async def main():
        logger.info(f"Nova Agent starting — LLM: {LLM_MODEL} via {AI_GATEWAY_URL}")
        await init_db()
        logger.info("Conversation store initialized (PIC for memory)")
        register_push_fallback()

        webrtc_port = int(os.environ.get("NOVA_PORT", "18800"))

        # WebRTC signaling server
        webrtc_config = uvicorn.Config(
            webrtc_app,
            host="0.0.0.0",
            port=webrtc_port,
            log_level="info",
        )
        webrtc_server = uvicorn.Server(webrtc_config)

        # Webhook server
        webhook_config = uvicorn.Config(
            webhook_app,
            host="0.0.0.0",
            port=WEBHOOK_PORT,
            log_level="info",
        )
        webhook_server = uvicorn.Server(webhook_config)

        logger.info(f"WebRTC: :{webrtc_port} | Webhooks: :{WEBHOOK_PORT}")

        # Text chat server (conversations API)
        from nova.text_chat import app as text_chat_app, TEXT_CHAT_PORT
        text_chat_config = uvicorn.Config(
            text_chat_app,
            host="0.0.0.0",
            port=TEXT_CHAT_PORT,
            log_level="info",
        )
        text_chat_server = uvicorn.Server(text_chat_config)
        logger.info(f"Text Chat API: :{TEXT_CHAT_PORT}")

        await asyncio.gather(
            webrtc_server.serve(),
            webhook_server.serve(),
            text_chat_server.serve(),
        )

    asyncio.run(main())
