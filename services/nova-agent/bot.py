"""
Nova Agent — Pipecat voice agent bot.

Uses SmallWebRTCTransport (self-hosted, no cloud dependency) with
OpenAI-compatible LLM service pointed at AI Gateway → MiniMax M2.5.

Based on: https://github.com/pipecat-ai/pipecat-examples/tree/main/p2p-webrtc/voice-agent
"""

import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
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

load_dotenv(override=True)

from nova.events import event_bus
from nova.notify import create_event_handler
from nova.prompt import build_system_prompt
from nova.push import mark_user_active, mark_user_inactive, register_push_fallback
from nova.store import init_db, get_or_create_session, append_turn, get_history
from nova.pic import build_pic_context, record_observation, create_preference
from nova.tools import TOOL_DEFINITIONS, dispatch_tool, set_progress_context

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
AI_GATEWAY_API_KEY = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
LLM_MODEL = os.environ.get("LLM_MODEL", "minimax-m2.5")

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
    logger.info(f"Session {session.session_id}: restored {len(prior_turns)} turns")

    # ── PIC context (identity, preferences, goals) ──────────────────
    pic_ctx = await build_pic_context(user_id)
    logger.info(f"PIC context: {len(pic_ctx.get('memory_snippets', []))} memory items")

    # ── Dynamic system prompt (shaped by PIC preferences) ────────────────
    system_prompt = build_system_prompt(
        user_name=pic_ctx.get("user_name") or (user_id if user_id != "default" else None),
        user_timezone=pic_ctx.get("user_timezone", "America/Chicago"),
        tool_names=TOOL_NAMES,
        memory_snippets=pic_ctx.get("memory_snippets"),
        preferences_by_category=pic_ctx.get("preferences_by_category"),
        identity=pic_ctx.get("identity"),
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
    # MiniMax sends tool_call.index=None in streaming chunks.
    # Pipecat's _process_context compares tool_call.index != func_idx (int),
    # so None != 0 incorrectly triggers the multi-tool branch.
    # Fix: wrap the stream to normalize index=None → index=0.

    class MiniMaxLLMService(OpenAILLMService):
        """Patches tool_call.index for MiniMax streaming compatibility."""

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

        async def get_chat_completions(self, params_from_context):
            params = self.build_chat_completion_params(params_from_context)
            raw_stream = await self._client.chat.completions.create(**params)

            class _PatchedStream:
                """Normalizes tool_call.index=None→0 for Pipecat compatibility."""
                def __init__(self, raw):
                    self._raw = raw
                def __aiter__(self):
                    return self._iter()
                async def _iter(self):
                    async for chunk in self._raw:
                        if chunk.choices:
                            for c in chunk.choices:
                                if c.delta and c.delta.tool_calls:
                                    for tc in c.delta.tool_calls:
                                        if tc.index is None:
                                            tc.index = 0
                        yield chunk
                async def close(self):
                    if hasattr(self._raw, "close"):
                        await self._raw.close()
                async def aclose(self):
                    if hasattr(self._raw, "aclose"):
                        await self._raw.aclose()
                    elif hasattr(self._raw, "close"):
                        await self._raw.close()

            return _PatchedStream(raw_stream)

    llm = MiniMaxLLMService(
        api_key=AI_GATEWAY_API_KEY,
        base_url=AI_GATEWAY_URL,
        model=LLM_MODEL,
        params=OpenAILLMService.InputParams(
            temperature=0.1,
            max_tokens=8192,
        ),
        function_call_timeout_secs=600.0,  # OpenClaw tasks can take minutes
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
    _SLOW_TOOLS = {"openclaw_delegate", "web_search"}
    # Per-turn dedup: only one spoken ack per user message to prevent feedback
    # loops where the mic picks up the TTS and re-sends it as a new utterance.
    _ack_sent_this_turn: list[bool] = [False]
    # Tool iteration tracking — prevent runaway tool chains
    _tool_calls_this_turn: list[int] = [0]
    _MAX_TOOL_CALLS_BEFORE_HEARTBEAT = 3
    _MAX_TOOL_CALLS_BEFORE_WARN = 6
    _MAX_TOOL_CALLS_HARD_LIMIT = 10

    def _build_spoken_ack(tool_name: str, args: dict) -> str | None:
        """Generate a contextual spoken acknowledgment from tool name + args.
        Returns None for fast tools that don't need an ack."""
        if tool_name == "openclaw_delegate":
            task = args.get("task", "")[:80]
            if task:
                return f"On it — {task.rstrip('.')}."
            return "Working on that for you."
        elif tool_name == "web_search":
            query = args.get("query", "")[:60]
            if query:
                return f"Searching for {query}."
            return "Searching the web."
        return None

    def make_tool_handler(tool_name: str):
        async def handler(params: FunctionCallParams):
            args = dict(params.arguments)
            _tool_calls_this_turn[0] += 1
            call_num = _tool_calls_this_turn[0]
            logger.info(f"Tool call #{call_num}: {tool_name} args={str(args)[:100]}")

            # ── Hard limit: prevent runaway tool chains ──
            if call_num > _MAX_TOOL_CALLS_HARD_LIMIT:
                logger.warning(f"Tool call hard limit reached ({call_num}), forcing response")
                await params.result_callback(
                    "SYSTEM: Tool call limit reached. You MUST respond to the user now "
                    "with whatever information you have gathered so far. Do NOT call any more tools."
                )
                return

            # ── Auto-heartbeat after sustained tool chains ──
            if call_num == _MAX_TOOL_CALLS_BEFORE_HEARTBEAT and not _ack_sent_this_turn[0]:
                _ack_sent_this_turn[0] = True
                await _send_server_msg({"type": "heartbeat", "text": "Still working on it..."})
                logger.info(f"Auto-heartbeat at tool call #{call_num}")

            # ── Soft warning: tell LLM to wrap up ──
            if call_num == _MAX_TOOL_CALLS_BEFORE_WARN:
                logger.warning(f"Soft tool limit reached ({call_num}), injecting wrap-up hint")

            # ── Fast path: instant spoken acknowledgment (once per turn) ──
            if tool_name in _SLOW_TOOLS and not _ack_sent_this_turn[0]:
                ack = _build_spoken_ack(tool_name, args)
                if ack:
                    _ack_sent_this_turn[0] = True
                    await _send_server_msg({"type": "heartbeat", "text": ack})
                    logger.info(f"Dual-path ack: '{ack}'")

            # For OpenClaw, also populate the ThinkingCard
            if tool_name == "openclaw_delegate":
                task_desc = args.get("task", "")[:120]
                await _send_server_msg({"phase": "delegating"})
                await _send_server_msg({
                    "type": "thinking",
                    "text": f"🔧 Delegating to OpenClaw: {task_desc}",
                })

            # ── Background path: tool executes ──
            result = await dispatch_tool(tool_name, args)

            # Clear ThinkingCard phase so the LLM's next response is spoken, not swallowed
            if tool_name == "openclaw_delegate":
                await _send_server_msg({"phase": "done"})

            # ── Inject wrap-up hint at soft limit ──
            if call_num >= _MAX_TOOL_CALLS_BEFORE_WARN:
                result = (
                    result + "\n\nSYSTEM: You have made many tool calls this turn. "
                    "Summarize what you've found so far and respond to the user. "
                    "Do not make additional tool calls unless absolutely critical."
                )

            # ── Result path: feed back to LLM for comprehensive spoken response ──
            await params.result_callback(result)
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
    # Memory tools (PIC — Personal Integration Core)
    llm.register_function("save_memory", make_tool_handler("save_memory"))
    llm.register_function("recall_memory", make_tool_handler("recall_memory"))
    llm.register_function("forget_memory", make_tool_handler("forget_memory"))
    # Delegated (actions requiring browser/email/calendar/shell)
    llm.register_function("openclaw_delegate", make_tool_handler("openclaw_delegate"))
    # Studio quick-reads (direct dashboard API)
    llm.register_function("check_studio", make_tool_handler("check_studio"))
    # Skill discovery (dynamic skill catalog from OpenClaw)
    llm.register_function("discover_skills", make_tool_handler("discover_skills"))
    # Network diagnostics
    llm.register_function("diagnose_network", make_tool_handler("diagnose_network"))
    # Time (grounded current date/time)
    llm.register_function("get_time", make_tool_handler("get_time"))
    # Timers & alarms
    llm.register_function("manage_timer", make_tool_handler("manage_timer"))
    # Context Bridge - link goals to knowledge graph
    llm.register_function("link_goal_to_knowledge", make_tool_handler("link_goal_to_knowledge"))

    # ── Personal Context Graph (PIC + KG-API via Context Bridge) ───────
    llm.register_function("save_memory", make_tool_handler("save_memory"))
    llm.register_function("recall_memory", make_tool_handler("recall_memory"))
    llm.register_function("query_context", make_tool_handler("query_context"))
    llm.register_function("kg_query", make_tool_handler("kg_query"))
    # Homelab infrastructure operations (Docker container management)
    llm.register_function("service_status", make_tool_handler("service_status"))
    llm.register_function("service_logs", make_tool_handler("service_logs"))
    llm.register_function("service_restart", make_tool_handler("service_restart"))
    llm.register_function("service_start", make_tool_handler("service_start"))
    llm.register_function("service_stop", make_tool_handler("service_stop"))
    llm.register_function("service_health_check", make_tool_handler("service_health_check"))

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
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ])
    else:
        # iOS native STT/TTS mode - text via data channel, no VAD needed
        from pipecat.frames.frames import (
            InputTransportMessageFrame,
            OutputTransportMessageFrame,
            TranscriptionFrame,
            TextFrame,
        )
        import re as _re

        # Regex to strip raw tool-call syntax the LLM may emit as text.
        # Matches patterns like: [web_search query="..."], [get_weather location="..."]
        _TOOL_CALL_TEXT_RE = _re.compile(
            r'\[(?:web_search|get_weather|check_studio|recall_memory|save_memory|forget_memory'
            r'|control_lights|get_workstation_status|set_reminder|openclaw_delegate'
            r'|search_past_conversations|discover_skills|diagnose_network|get_time'
            r'|manage_timer|manage_ticket|manage_workspace|manage_notes|exomind'
            r'|service_status|service_logs|service_restart|service_start|service_stop'
            r'|service_health_check|homelab_operations|homelab_diagnostics'
            r'|ev_route_planner|tesla_location_refresh|tesla_control|youtube'
            r'|knowledge_query|get_enriched_context|link_goal_to_knowledge'
            r'|query_frameworks)\b[^\]]*\]',
            _re.IGNORECASE,
        )

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
                            logger.info(f"Native STT → LLM (RTVI): {text[:80]}")

                elif isinstance(frame, OutputTransportMessageFrame):
                    msg = frame.message
                    if isinstance(msg, dict):
                        msg_type = msg.get("type", "")
                        logger.info(f"LLM → Client ({msg_type}): {str(msg)[:120]}")

                # Filter LLMTextFrames: strip raw tool-call syntax
                if isinstance(frame, LLMTextFrame):
                    original = frame.text
                    cleaned = _TOOL_CALL_TEXT_RE.sub('', original)
                    if cleaned != original:
                        logger.warning(f"🧹 Stripped tool syntax from LLM text: {original[:120]}")
                        if not cleaned.strip():
                            # Entire frame was tool syntax — drop it
                            return
                        frame.text = cleaned

                # Log LLMFullResponseStartFrame/EndFrame
                if isinstance(frame, LLMFullResponseStartFrame):
                    logger.info(f"✅ LLMFullResponseStartFrame → triggers bot-llm-started")
                if isinstance(frame, LLMFullResponseEndFrame):
                    logger.info(f"✅ LLMFullResponseEndFrame → triggers bot-llm-stopped")

                await self.push_frame(frame, direction)

        text_bridge = NativeTextBridge()
        # Use the same aggregator pair as server mode, just without VAD
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

        pipeline = Pipeline([
            transport.input(),
            user_aggregator,
            llm,
            assistant_aggregator,
            text_bridge,  # Log output frames after assistant_aggregator
            transport.output(),
        ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Don't kill the connection during long tool calls (OpenClaw can take 30-60s).
        # The idle observer only watches BotSpeaking/UserSpeaking frames, which aren't
        # emitted during tool execution, so it fires prematurely.
        cancel_on_idle_timeout=False,
        rtvi_observer_params=RTVIObserverParams(
            # Expose function call details so iOS SDK fires
            # onLLMFunctionCallStarted / InProgress / Stopped callbacks
            function_call_report_level={
                "*": RTVIFunctionCallReportLevel.NAME,
                "openclaw_delegate": RTVIFunctionCallReportLevel.FULL,
            },
        ),
    )
    # Wire up RTVI ref now that task exists
    _rtvi_ref[0] = task._rtvi

    # Let tools.py send server messages (e.g. citations from web_search)
    from nova.tools import set_server_msg_fn, set_web_search_agent_mode
    set_server_msg_fn(_send_server_msg)
    # iOS sends "Deep (Verified)" orchestration mode — use sonar-pro for web search
    set_web_search_agent_mode("deep")

    # ── Conversation persistence ─────────────────────────────────────────
    # Buffer assistant text chunks, persist complete turns on boundaries.
    # Uses Pipecat's task-level frame observation API:
    #   task.set_reached_downstream_filter() + on_frame_reached_downstream
    # (LLMService only exposes on_function_calls_started and on_completion_timeout)
    _assistant_buffer: list[str] = []

    # Tell the PipelineTask which frame types we want to observe
    task.set_reached_downstream_filter((
        LLMTextFrame,
        LLMFullResponseStartFrame,
        LLMFullResponseEndFrame,
    ))

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
                        # New user turn — reset per-turn dedup and tool counter
                        _ack_sent_this_turn[0] = False
                        _tool_calls_this_turn[0] = 0
                        await append_turn(session.session_id, "user", content)
                        logger.debug(f"Persisted user turn ({len(content)} chars)")
                self._last_len = len(msgs)

    ctx_watcher = _ContextWatcher()

    @task.event_handler("on_frame_reached_downstream")
    async def on_frame_downstream(task_ref, frame):
        if isinstance(frame, LLMTextFrame):
            _assistant_buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseStartFrame):
            logger.info("🎯 LLMFullResponseStartFrame reached downstream → RTVI should send bot-llm-started")
            await ctx_watcher.check_and_persist()
        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.info("🎯 LLMFullResponseEndFrame reached downstream → RTVI should send bot-llm-stopped")
            if _assistant_buffer:
                full_text = "".join(_assistant_buffer)
                _assistant_buffer.clear()
                if full_text.strip():
                    await append_turn(session.session_id, "assistant", full_text)
                    logger.debug(f"Persisted assistant turn ({len(full_text)} chars)")

    # Event bus: proactive notifications while user is connected
    event_handler = create_event_handler(task, user_id)

    # Progress callback for OpenClaw streaming updates
    async def on_openclaw_progress(status_type: str, message: str):
        """Send OpenClaw progress to iOS ThinkingCard via server messages.
        
        Uses custom message types so they don't pollute the LLM response:
        - phase     → {phase: ...}      — sets processingStatus label on ThinkingCard
        - status    → {type: heartbeat}  — periodic 'still working' pulses (spoken via TTS)
        - thinking  → {type: thinking}   — tool/reasoning updates for ThinkingCard
        - narration → {type: thinking}   — spoken channel deltas for ThinkingCard
        """
        logger.info(f"OpenClaw progress ({status_type}): {message[:80]}")
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
        set_progress_context(on_openclaw_progress, user_id)
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
            user_id = auth_data.get("user_id", "default")
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
                user_id = rd.get("user_id", "default")
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
                user_id = rd.get("user_id", "default")
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
                user_id = rd.get("user_id", "default")
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
