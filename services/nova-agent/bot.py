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
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputTransportMessageFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
    LLMRunFrame,
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
from nova.store import init_db, get_or_create_session, append_turn, get_history, get_compacted_context, _sync_message_to_backend, ensure_backend_conversation, get_backend_conversation, Turn, get_session_metadata, update_session_metadata_key, append_learning_event, get_recent_active_conversation, get_active_action_ledger_entry
from nova.pcg import build_context, record_observation, create_preference
from nova.context_budget import check_overflow_risk
from nova.context_compactor import (
    compact_if_over_latency_threshold,
    trim_tool_result_for_history,
    LATENCY_THRESHOLD_TOKENS,
    HISTORY_TOOL_RESULT_CAP,
)
from nova.tools import TOOL_DEFINITIONS, dispatch_tool, reset_conversation_search_count, set_progress_context
from nova.turn_policy import canonicalize_turn_text
from nova.turn_orchestrator import STATE_METADATA_KEY, TurnState, decide_turn, execute_turn_plan_result, turn_state_from_metadata, turn_state_to_metadata_value
from nova.turn_ownership import should_consume_llm_frame_after_orchestrator
from nova.turn_tool_policy import CORE_TOOL_NAMES, select_tool_budget
from nova.turn_context import (
    TurnContext,
    derive_goal,
    derive_evidence_budget,
    augment_tool_result,
    finalize_and_persist,
)
from nova.semantic_turn_resolver import resolve_semantic_turn
from nova.voice_turn_runtime import VoiceTurnRuntime
from nova.learning import consolidate_session_learning
from nova.session_planner import ensure_active_plan_for_turn, auto_link_workspace_page, emit_plan_state, fetch_project_pages

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
ALL_TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS if "function" in t]
TOOL_DEFINITIONS_BY_NAME = {
    td["function"]["name"]: td
    for td in TOOL_DEFINITIONS
    if "function" in td and "name" in td["function"]
}
TOOL_NAMES = [name for name in ALL_TOOL_NAMES if name in CORE_TOOL_NAMES]

# Convert OpenAI-format tool defs to Pipecat FunctionSchema/ToolsSchema
def _build_tools_schema(tool_names: set[str] | list[str] | None = None) -> ToolsSchema:
    schemas = []
    selected = list(tool_names) if tool_names else ALL_TOOL_NAMES
    for name in selected:
        td = TOOL_DEFINITIONS_BY_NAME.get(name)
        if not td:
            continue
        func = td.get("function", {})
        params = func.get("parameters", {})
        schemas.append(FunctionSchema(
            name=func["name"],
            description=func.get("description", ""),
            properties=params.get("properties", {}),
            required=params.get("required", []),
        ))
    return ToolsSchema(standard_tools=schemas)


def _first_string(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _compact_metadata_value(value, max_len: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _extract_image_context(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    candidates = []
    for key in (
        "imageContext",
        "image_context",
        "contextualImageMetadata",
        "contextual_image_metadata",
        "imageMetadata",
        "image_metadata",
        "image",
        "attachment",
    ):
        value = data.get(key)
        if value:
            candidates.append(value)

    for attachment in data.get("attachments") or []:
        if isinstance(attachment, dict):
            mime_type = _first_string(attachment.get("mimeType"), attachment.get("mime_type"), attachment.get("type"))
            if "image" in mime_type.lower() or attachment.get("imageUrl") or attachment.get("image_url") or attachment.get("url"):
                candidates.append(attachment)

    if not candidates:
        return ""

    merged: dict[str, object] = {}
    for candidate in candidates:
        if isinstance(candidate, dict):
            merged.update(candidate)
        else:
            merged.setdefault("metadata", candidate)

    image_url = _first_string(
        merged.get("imageUrl"),
        merged.get("image_url"),
        merged.get("url"),
        merged.get("sourceUrl"),
        merged.get("source_url"),
    )

    lines = ["[CONTEXTUAL IMAGE INPUT]"]
    if image_url:
        lines.append(f"Image URL: {image_url}")

    location_obj = None
    for location_key in ("location", "geoLocation", "geolocation", "gps", "GPS", "coordinates"):
        value = merged.get(location_key)
        if isinstance(value, dict):
            location_obj = value
            break

    if location_obj:
        lat = location_obj.get("latitude", location_obj.get("lat"))
        lon = location_obj.get("longitude", location_obj.get("lon", location_obj.get("lng")))
        if lat not in (None, "") and lon not in (None, ""):
            lines.append(f"image_geolocation: latitude={lat}, longitude={lon}")
        for key in ("accuracy", "altitude", "timestamp", "capturedAt", "captured_at", "placeName", "place_name", "address"):
            if key in location_obj and location_obj.get(key) not in (None, ""):
                lines.append(f"image_geolocation_{key}: {_compact_metadata_value(location_obj.get(key))}")

    for key in (
        "id",
        "fileName",
        "filename",
        "mimeType",
        "mime_type",
        "width",
        "height",
        "createdAt",
        "created_at",
        "capturedAt",
        "captured_at",
        "caption",
        "altText",
        "alt_text",
        "source",
        "latitude",
        "lat",
        "longitude",
        "lon",
        "lng",
        "location",
        "geoLocation",
        "geolocation",
        "gps",
        "GPS",
        "coordinates",
        "placeName",
        "place_name",
        "address",
        "exif",
        "EXIF",
        "metadata",
    ):
        if key in merged and merged.get(key) not in (None, ""):
            lines.append(f"{key}: {_compact_metadata_value(merged.get(key))}")
    lines.append("Instruction: Treat this as image context for the current user turn. If an Image URL is present and the user asks about the image, call analyze_image with that URL and use the metadata, including image geolocation when present, to guide the prompt.")
    lines.append("[/CONTEXTUAL IMAGE INPUT]")
    return "\n".join(lines)


def _enrich_send_text_with_image_context(message: dict) -> str:
    if not isinstance(message, dict) or message.get("type") != "send-text":
        return ""
    data = message.get("data", {})
    if not isinstance(data, dict):
        return ""
    content = data.get("content", "")
    if not isinstance(content, str):
        return ""
    if "[CONTEXTUAL IMAGE INPUT]" in content:
        return content
    image_context = _extract_image_context(data)
    if image_context:
        data["content"] = f"{content.rstrip()}\n\n{image_context}" if content.strip() else image_context
        return data["content"]
    return content


IMAGE_CONTEXT_METADATA_KEY = "latest_image_context"
_IMAGE_REFERENCE_TERMS = ("image", "photo", "picture", "screenshot", "attachment", "attached")


def _extract_contextual_image_block(text: str) -> str:
    if not text or "[CONTEXTUAL IMAGE INPUT]" not in text:
        return ""
    start = text.find("[CONTEXTUAL IMAGE INPUT]")
    end_marker = "[/CONTEXTUAL IMAGE INPUT]"
    end = text.find(end_marker, start)
    if end == -1:
        return text[start:].strip()
    return text[start:end + len(end_marker)].strip()


def _looks_like_image_followup(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    return any(term in normalized for term in _IMAGE_REFERENCE_TERMS)


PIPECAT_TOOLS = _build_tools_schema(TOOL_NAMES)

# Max conversation turns to restore from DB
MAX_HISTORY_TURNS = int(os.environ.get("NOVA_VOICE_HISTORY_TURNS", "8"))
MAX_LLM_MESSAGE_CHARS = int(os.environ.get("NOVA_MAX_LLM_MESSAGE_CHARS", "1800"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("NOVA_MAX_TOOL_RESULT_CHARS", "2500"))
NOVA_VAD_CONFIDENCE = float(os.environ.get("NOVA_VAD_CONFIDENCE", "0.65"))
NOVA_VAD_START_SECS = float(os.environ.get("NOVA_VAD_START_SECS", "0.15"))
NOVA_VAD_STOP_SECS = float(os.environ.get("NOVA_VAD_STOP_SECS", "0.9"))
NOVA_VAD_MIN_VOLUME = float(os.environ.get("NOVA_VAD_MIN_VOLUME", "0.35"))


def _estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(msg.get("content") or "")) for msg in messages) // 4


def _trim_message_content(content: str, limit: int = MAX_LLM_MESSAGE_CHARS) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[...trimmed for prompt budget...]"


def _is_interim_send_text_message(message: dict) -> bool:
    if not isinstance(message, dict) or message.get("type") != "send-text":
        return False
    data = message.get("data", {})
    if not isinstance(data, dict):
        return False
    for key in ("is_final", "final", "isFinal"):
        if key in data:
            return data.get(key) is False
    status = str(data.get("status") or data.get("transcript_status") or data.get("transcriptStatus") or "").lower()
    return status in {"partial", "interim", "tentative", "recognizing"}


def _first_nonempty_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_continuity_payload(message: dict) -> dict:
    if not isinstance(message, dict):
        return {}
    data = message.get("data", {})
    continuity = {}
    if isinstance(data, dict) and isinstance(data.get("continuity"), dict):
        continuity.update(data.get("continuity") or {})
    if isinstance(message.get("continuity"), dict):
        continuity.update(message.get("continuity") or {})
    for source in (data if isinstance(data, dict) else {}, message):
        for key in (
            "userId", "user_id", "conversationId", "conversation_id", "clientTurnId", "client_turn_id",
            "lastClientTurnId", "last_client_turn_id", "lastServerTurnId", "last_server_turn_id",
            "lastTurnId", "last_turn_id", "lastIntent", "last_intent", "lastTool", "last_tool",
            "lastTaskArtifactId", "last_task_artifact_id", "lastCardKind", "last_card_kind",
            "lastErrorCode", "last_error_code", "activeTaskArtifactId", "active_task_artifact_id",
            "activeGoal", "active_goal", "activeAgentRunId", "active_agent_run_id",
            "lastKnownPhase", "last_known_phase", "retry",
        ):
            if key in source and key not in continuity:
                continuity[key] = source.get(key)
    return continuity


def _apply_continuity_to_turn_state(state: TurnState, continuity: dict) -> bool:
    if not isinstance(continuity, dict) or not continuity:
        return False
    changed = False
    active_goal = _first_nonempty_string(continuity.get("activeGoal"), continuity.get("active_goal"))
    if active_goal and active_goal != state.active_goal:
        state.active_goal = active_goal
        changed = True
    artifact_id = _first_nonempty_string(
        continuity.get("activeTaskArtifactId"),
        continuity.get("active_task_artifact_id"),
        continuity.get("lastTaskArtifactId"),
        continuity.get("last_task_artifact_id"),
    )
    if artifact_id and artifact_id != state.active_task_artifact_id:
        state.active_task_artifact_id = artifact_id
        changed = True
    agent_run_id = _first_nonempty_string(continuity.get("activeAgentRunId"), continuity.get("active_agent_run_id"))
    if agent_run_id and agent_run_id != state.active_workflow_run_id:
        state.active_workflow_run_id = agent_run_id
        changed = True
    last_intent = _first_nonempty_string(continuity.get("lastIntent"), continuity.get("last_intent"))
    if last_intent and last_intent != state.last_intent:
        state.last_intent = last_intent
        changed = True
    return changed


def _trim_context_messages(messages: list[dict], max_recent: int = MAX_HISTORY_TURNS) -> list[dict]:
    if not messages:
        return []
    trimmed: list[dict] = []
    for msg in messages[-max_recent:]:
        role = msg.get("role")
        content = _trim_message_content(str(msg.get("content") or ""))
        if role in ("user", "assistant") and content.strip():
            trimmed.append({"role": role, "content": content})
    return trimmed


def _trim_tool_result_for_llm(tool_name: str, result: object) -> str:
    text = str(result) if result is not None else ""
    
    # Deep data tools need larger limits to function correctly
    # 64,000 characters allows us to safely ingest ~10,000+ word research documents
    if tool_name in {
        "query_frameworks", "query_cig", "search_past_conversations", "web_search", 
        "query_context", "kg_query", "manage_workspace", "check_studio", "homelab_operations",
        "service_logs"
    }:
        limit = 64000
    else:
        limit = MAX_TOOL_RESULT_CHARS

    if len(text) <= limit:
        trimmed = text
    else:
        trimmed_content = text[:limit].rstrip() + f"\n[trimmed {tool_name} result for voice latency; use this summary and answer now]"
        
        # If the original text was valid JSON, ensure we return valid JSON
        # so we don't crash strict LLM API parsers like MiniMax
        is_json = False
        if text.strip().startswith("{") or text.strip().startswith("["):
            try:
                import json
                json.loads(text)
                is_json = True
            except Exception:
                pass
                
        if is_json:
            import json
            trimmed = json.dumps({
                "_meta": f"Result truncated to {limit} chars for latency.",
                "partial_data": trimmed_content
            })
        else:
            trimmed = trimmed_content

    if tool_name == "web_search":
        return (
            "WEB SEARCH EVIDENCE:\n"
            f"{trimmed}\n\n"
            "ANSWER REQUIREMENTS: Answer only from the evidence above. Include source names or citations in the display text. "
            "For investment, valuation, revenue, IPO, partnership, or legal claims, use cautious wording like 'reported' or "
            "'according to' unless the evidence is from an official company or regulator source. If evidence is weak, aggregated, "
            "or only from analysis/social/video sources, say that confidence is limited."
        )
    return trimmed


def _compact_system_prompt_for_voice(prompt: str) -> str:
    if os.environ.get("NOVA_COMPACT_SYSTEM_PROMPT", "1").lower() in {"0", "false", "no"}:
        return prompt
    marker = "\n\n## Who You Are\n"
    if marker not in prompt:
        return prompt
    _, rest = prompt.split(marker, 1)
    compact_identity = (
        "You are Nova, a personal AI voice assistant and companion running on the user's iPhone. "
        "Speak naturally and concisely through iOS text-to-speech. Use deterministic orchestrators "
        "and direct tools for specific/current/personal data. Answer from trained knowledge only for "
        "general facts, and use zero-wait behavior: do not stall or promise action without calling the "
        "tool in the same response. PCG is long-term memory, CIG is email/calendar/contact intelligence, "
        "Pi Agent Hub delegates specialist work, and AI Gateway routes LLM/search/vision."
    )
    return compact_identity + marker + rest


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
                    content=_trim_message_content(msg["content"]),
                    timestamp=msg.get("timestamp", ""),
                    tool_calls=None,
                )
                for msg in backend_conv["messages"][-MAX_HISTORY_TURNS:]
                if msg["role"] in ("user", "assistant")
            ]
            # Sync to local SQLite for faster access
            for turn in prior_turns:
                await append_turn(session.session_id, turn.role, turn.content)
    
    if audio_mode == "native" and len(prior_turns) < 2 and conversation_id not in {"default", ""}:
        recent = await get_recent_active_conversation(
            user_id,
            exclude_conversation_id=conversation_id,
            max_age_secs=int(os.environ.get("NOVA_NATIVE_CONTEXT_CONTINUITY_SECS", "1800")),
            min_turns=2,
        )
        if recent:
            continuity_turns = await get_history(recent["session_id"], limit=MAX_HISTORY_TURNS)
            if continuity_turns:
                prior_turns = continuity_turns
                logger.warning(
                    f"NOVA_CONTEXT_CONTINUITY | new_conv={conversation_id} restored_from={recent['conversation_id']} "
                    f"turns={len(prior_turns)} age_secs={int(time.time() - float(recent['last_active']))}"
                )

    logger.info(f"Session {session.session_id}: restored {len(prior_turns)} turns")

    # Ensure conversation exists in PostgreSQL backend for search/retrieval
    await ensure_backend_conversation(conversation_id, user_id)

    session_metadata = await get_session_metadata(session.session_id)
    _turn_state = turn_state_from_metadata(session_metadata)
    latest_image_context = session_metadata.get(IMAGE_CONTEXT_METADATA_KEY) if isinstance(session_metadata, dict) else None
    if not latest_image_context:
        for turn in reversed(prior_turns):
            if turn.role == "user":
                latest_image_context = _extract_contextual_image_block(turn.content)
                if latest_image_context:
                    break
    _latest_image_context: list[str] = [latest_image_context if isinstance(latest_image_context, str) else ""]

    # ── PCG context (identity, preferences, goals) ──────────────────
    pcg_ctx = await build_context(user_id)
    logger.info(f"PCG context: {len(pcg_ctx.get('memory_snippets', []))} memory items")
    # Inject daily snapshot (non-persisted) so calendar lookup can use it without tool calls
    _turn_state.daily_snapshot = pcg_ctx.get("daily_snapshot") or {}

    # ── Dynamic system prompt (shaped by PCG preferences) ────────────────
    system_prompt = build_system_prompt(
        user_name=pcg_ctx.get("user_name") or (user_id if user_id != "default" else None),
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
    system_prompt = _compact_system_prompt_for_voice(system_prompt)

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
    _llm_response_started_at: list[float | None] = [None]
    _llm_first_text_at: list[float | None] = [None]
    _llm_text_chars_this_response: list[int] = [0]

    class MiniMaxLLMService(_MiniMaxLLM):
        """Extends MiniMaxLLMService with frame logging for voice agent."""

        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            """Log frame pushing to trace LLMFullResponseStartFrame/EndFrame emission."""
            from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame
            
            # Log outgoing frames
            if isinstance(frame, LLMFullResponseStartFrame):
                _llm_response_started_at[0] = time.monotonic()
                _llm_first_text_at[0] = None
                _llm_text_chars_this_response[0] = 0
                logger.info(f"🚀 MiniMaxLLMService.push_frame: {type(frame).__name__} → {direction}")
                logger.info(f"NOVA_LLM_TIMING | event=response_start | model={LLM_MODEL}")
            elif isinstance(frame, LLMFullResponseEndFrame):
                total_ms = 0
                first_text_ms = 0
                if _llm_response_started_at[0] is not None:
                    total_ms = int((time.monotonic() - _llm_response_started_at[0]) * 1000)
                if _llm_response_started_at[0] is not None and _llm_first_text_at[0] is not None:
                    first_text_ms = int((_llm_first_text_at[0] - _llm_response_started_at[0]) * 1000)
                logger.info(f"🚀 MiniMaxLLMService.push_frame: {type(frame).__name__} → {direction}")
                logger.info(
                    f"NOVA_LLM_TIMING | event=response_end | model={LLM_MODEL} | "
                    f"duration_ms={total_ms} | first_text_ms={first_text_ms} | text_chars={_llm_text_chars_this_response[0]}"
                )
            elif isinstance(frame, LLMTextFrame):
                if _llm_response_started_at[0] is not None and _llm_first_text_at[0] is None:
                    _llm_first_text_at[0] = time.monotonic()
                    first_text_ms = int((_llm_first_text_at[0] - _llm_response_started_at[0]) * 1000)
                    logger.info(f"NOVA_LLM_TIMING | event=first_text | model={LLM_MODEL} | first_text_ms={first_text_ms}")
                _llm_text_chars_this_response[0] += len(frame.text or "")
                logger.info(f"📝 MiniMaxLLMService.push_frame: LLMTextFrame ({len(frame.text)} chars)")
            
            # Call parent to actually push
            await super().push_frame(frame, direction)

    # Initial thinking level: "low" by default for fast voice responses.
    # NativeTextBridge swaps this per turn via llm.set_thinking() based on the
    # MODE POLICY prefix iOS injects. Gateway translates body.thinking →
    # reasoning_budget for llama-server (low → 0 / medium → 2048 / high → -1).
    # NB: passing extra_body=... directly to InputParams is a no-op — Pydantic
    # silently drops unknown kwargs. The `thinking=` constructor arg below is
    # the supported path; MiniMaxLLMService.__init__ writes it into
    # _settings.extra["extra_body"]["thinking"] so OpenAI SDK forwards it.
    llm = MiniMaxLLMService(
        thinking="low",
        api_key=AI_GATEWAY_API_KEY,
        base_url=AI_GATEWAY_URL,
        model=LLM_MODEL,
        params=OpenAILLMService.InputParams(
            # MiniMax M2.7 official sampling per the model card / Unsloth notes:
            # temperature=1.0, top_p=0.95, top_k=40. We previously ran at 0.1
            # which mode-collapsed the reasoning trajectory and produced
            # repeated "I'll just call hermes-email" hallucinations on tool
            # turns. 1.0 + top_p 0.95 restores the diversity the model was
            # trained for; reproducibility for tools comes from the chat
            # template + tool schema, not from low temp.
            temperature=1.0,
            top_p=0.95,
            max_tokens=16384,
        ),
        function_call_timeout_secs=600.0,  # Hub delegation tasks can take minutes
    )

    # Register tool handlers (Pipecat 0.0.104 FunctionCallParams API)
    from pipecat.services.llm_service import FunctionCallParams

    # Mutable ref — populated after PipelineTask is created
    _rtvi_ref: list = [None]
    _server_msg_backlog: list[dict] = []
    _current_turn_id: list[str] = [""]
    _llm_watchdog_task: list[asyncio.Task | None] = [None]
    _active_voice_turn: list[VoiceTurnRuntime | None] = [None]
    _orchestrator_consumed_turn_id: list[str] = [""]

    async def _auto_add_session_for_plan(
        plan_id: str,
        tc,  # TurnContext
        conversation_id: str,
    ) -> None:
        """P2b: Persist a session entry to the active plan when a turn ends.

        Writes a compact summary to nova_task_plan_sessions so tomorrow's
        session can call manage_task_plan(action=get) and see exactly what
        was accomplished. Only writes if the turn actually used tools or
        produced evidence — avoids creating empty session noise.
        """
        try:
            if not plan_id or tc is None:
                return
            # Skip turns that did no real work
            if not tc.tool_history and not tc.evidence_log:
                return
            # Build summary
            tools_used = list(dict.fromkeys(tc.tool_history))[:8]
            evidence = [ev.summary for ev in (tc.evidence_log or [])][:4]
            failures = list(dict.fromkeys(tc.failures or []))[:4]
            posture = getattr(tc, "posture", "")
            goal = getattr(tc, "goal", "") or ""

            summary_parts: list[str] = []
            if goal:
                summary_parts.append(f"Goal: {goal[:140]}")
            if tools_used:
                summary_parts.append(f"Tools: {', '.join(tools_used)}")
            if posture:
                summary_parts.append(f"Posture at close: {posture}")
            summary = " | ".join(summary_parts) or "Turn produced evidence"

            content_parts: list[str] = []
            if evidence:
                content_parts.append("Evidence collected:")
                for i, ev in enumerate(evidence, 1):
                    content_parts.append(f"  {i}. {ev[:200]}")
            if failures:
                content_parts.append(f"Failures: {', '.join(failures)}")
            content = "\n".join(content_parts)

            from nova.task_plan import add_session_entry as _add_session_entry
            await _add_session_entry(
                plan_id,
                conversation_id=conversation_id,
                summary=summary,
                content=content,
                sources=tools_used,
                next_steps=[],
            )
            logger.info(
                f"NOVA_PLANNER | auto_session_added | plan_id={plan_id} "
                f"tools={len(tools_used)} evidence={len(evidence)} "
                f"failures={len(failures)}"
            )
        except Exception as e:
            logger.warning(f"NOVA_PLANNER_AUTO_SESSION_FAILED | plan_id={plan_id} err={e}")

    async def _send_server_msg(msg: dict):
        """Send a custom server message to iOS via RTVI protocol."""
        rtvi = _rtvi_ref[0]
        if rtvi is None:
            if len(_server_msg_backlog) >= 100:
                _server_msg_backlog.pop(0)
            _server_msg_backlog.append(dict(msg))
            logger.warning(f"RTVI not ready, queued server msg: {msg}")
            return
        try:
            await rtvi.send_server_message(msg)
            logger.debug(f"Sent server msg: {msg}")
        except Exception as e:
            logger.warning(f"Could not send server message: {e}")
            if msg.get("type") in {"validated", "turn_complete", "turn_status", "validationStep"}:
                if len(_server_msg_backlog) >= 100:
                    _server_msg_backlog.pop(0)
                _server_msg_backlog.append(dict(msg))

    async def _flush_server_msg_backlog():
        if not _server_msg_backlog or _rtvi_ref[0] is None:
            return
        pending = list(_server_msg_backlog)
        _server_msg_backlog.clear()
        for msg in pending:
            await _send_server_msg(msg)

    def _new_turn_id() -> str:
        return f"turn-{int(time.time() * 1000)}"

    def _ensure_voice_turn_runtime(turn_id: str | None = None) -> VoiceTurnRuntime:
        if _active_voice_turn[0] is not None:
            return _active_voice_turn[0]

        active_turn_id = turn_id or _current_turn_id[0] or _new_turn_id()
        _current_turn_id[0] = active_turn_id

        async def _voice_persist(role: str, content: str):
            await append_turn(session.session_id, role, content)

        _active_voice_turn[0] = VoiceTurnRuntime(
            turn_id=active_turn_id,
            conversation_id=conversation_id,
            session_id=session.session_id,
            user_id=user_id,
            send_server_msg=_send_server_msg,
            persist_turn=_voice_persist,
            sync_backend=_sync_message_to_backend,
            model=LLM_MODEL,
        )
        return _active_voice_turn[0]

    async def _complete_orphaned_tool_failure(tool_name: str, failure_notice: str) -> None:
        runtime = _ensure_voice_turn_runtime()
        await runtime.tool_failed(tool_name, failure_notice)
        await runtime.complete_with_error(failure_notice)

    async def _emit_turn_status(phase: str, message: str = "", tool: str = "", severity: str = "info"):
        if _active_voice_turn[0] is not None:
            await _active_voice_turn[0].emit_status(phase, message, tool=tool, severity=severity)
            return
        payload = {
            "type": "turn_status",
            "turn_id": _current_turn_id[0],
            "phase": phase,
            "message": message,
            "severity": severity,
        }
        if tool:
            payload["tool"] = tool
        await _send_server_msg(payload)

    def _cancel_llm_watchdog():
        if _active_voice_turn[0] is not None:
            _active_voice_turn[0].cancel_watchdog()
        task = _llm_watchdog_task[0]
        if task and not task.done():
            task.cancel()
        _llm_watchdog_task[0] = None

    def _start_llm_watchdog():
        if _active_voice_turn[0] is not None:
            _active_voice_turn[0].start_watchdog()
            return
        _cancel_llm_watchdog()
        turn_id = _current_turn_id[0]

        async def _watch():
            try:
                await asyncio.sleep(3)
                if _current_turn_id[0] == turn_id:
                    await _emit_turn_status("waiting_for_model", "I heard you. I’m waiting on the model to start responding.")
                await asyncio.sleep(5)
                if _current_turn_id[0] == turn_id:
                    await _emit_turn_status("model_slow", "This is taking longer than normal. I’m still working on it.", severity="warning")
                await asyncio.sleep(12)
                if _current_turn_id[0] == turn_id:
                    await _emit_turn_status("model_stalled", "The model is still delayed. I'll keep the connection alive and finish when it returns.", severity="warning")
                # Keep sending heartbeats every 30s so iOS doesn't fire stale (180s threshold)
                while _current_turn_id[0] == turn_id:
                    await asyncio.sleep(30)
                    if _current_turn_id[0] == turn_id:
                        await _send_server_msg({"type": "heartbeat", "text": "Still processing…"})
            except asyncio.CancelledError:
                pass

        _llm_watchdog_task[0] = asyncio.create_task(_watch())

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
    # M2.7 alignment: soft synthesis nudge before hard limit. MiniMax M2.7 is
    # designed to fan-out parallel tool calls in 2–3 LLM passes and then
    # synthesize — not to chain deep sequential lookups. After this many
    # individual tool calls within a turn, inject a SYSTEM hint asking the
    # model to synthesize with the data it already has.
    # 10 (was 6): research/meeting-prep turns legitimately need 8-10 evidence
    # calls across CIG, search, recall, and workspace tools before synthesis.
    _SOFT_SYNTHESIS_NUDGE_AT = 10
    _MAX_TOOL_CALLS_HARD_LIMIT = 20  # Pure infinite-loop guard
    _soft_synthesis_nudged_this_turn: list[bool] = [False]
    # Agentic recovery: track per-turn tool failures so M2.7 can self-recover
    # up to _MAX_AGENTIC_RECOVERIES times before the turn is surfaced as error.
    _tool_failures_this_turn: list[int] = [0]
    _MAX_AGENTIC_RECOVERIES = 2
    # P1b: Per-turn read-cache for idempotent tools. Eliminates the
    # "fetched the same page 7 times in 54s" pattern observed in 8 PM logs.
    # Keyed by (tool_name, json.dumps(args, sort_keys=True)) → str result.
    _tool_read_cache_this_turn: dict[tuple[str, str], str] = {}
    _CACHEABLE_READ_TOOLS = {
        "manage_workspace",        # only when action in (get_page, search, list_pages, etc.)
        "manage_task_plan",        # only when action in (get, list)
        "search_past_conversations",
        "query_cig",
        "query_context",
        "recall_memory",
        "knowledge_query",
        "kg_query",
        "get_enriched_context",
        "discover_skills",
        "service_status",
        "service_logs",
        "get_workstation_status",
        "check_studio",
        "background_task_status",
    }
    _CACHEABLE_ACTIONS_BY_TOOL = {
        "manage_workspace": {"get_page", "search", "list_pages", "list_databases",
                             "list_database_rows", "get_database_row", "list"},
        "manage_task_plan": {"get", "list"},
    }
    # Per-tool-per-turn rate limits — differentiated by provider cost/risk
    # Cloud APIs (paid, rate-limited externally): tight limits
    # Local/homelab APIs (free, internal): generous limits
    _PER_TOOL_LIMITS: dict[str, int] = {
        # ── Cloud / paid APIs ──────────────────────────────────────────────
        "web_search": 3,            # Perplexity Sonar — paid per call; allow 1 natural LLM retry
        "get_weather": 2,           # Perplexity Sonar — paid per call
        "hub_delegate": 2,          # Hub RPC — approval-gated
        "tesla_control": 3,         # Local relay → Tesla cloud
        "tesla_wake": 2,            # Local relay → Tesla cloud
        "tesla_navigation": 2,      # Local relay → Tesla cloud
        "tesla_stream_monitor": 3,  # Local relay → Tesla cloud
        "tesla_location_refresh": 3,# Local relay → Tesla cloud
        # ── Local / homelab ───────────────────────────────────────────────
        "query_cig": 3,             # CIG analytics — 3 is enough; loop guard
        "check_studio": 3,          # Local CIG/Hermes — 3 is enough; loop guard
        "query_frameworks": 10,     # Local LIAM/PCG
        "recall_memory": 10,        # Local PCG
        "save_memory": 5,           # Local PCG
        "search_past_conversations": 10,  # Local DB
        "service_status": 6,        # Local Docker API
        "homelab_diagnostics": 4,   # Local Docker API
        "homelab_operations": 4,    # Local Docker API
        "manage_workspace": 12,     # Pi Workspace API — search(3)+list(1)+create(1)+blocks(3)+verify(1)+update(2)+spare(1)
        "manage_task_plan": 8,      # Local SQLite — enough for full create protocol
    }
    _per_tool_call_counts: dict[str, int] = {}
    _last_tool_call: dict[str, object] = {"name": None, "args": None, "result": None}
    _consecutive_dedup_counts: dict[str, int] = {}  # per-tool consecutive duplicate hit counter
    # Reasoning scaffold — anchors the LLM on the turn's goal across long tool chains
    _active_turn_context: list[Optional[TurnContext]] = [None]
    # Track recent tool calls to detect duplicates
    # Note: hub_delegate is excluded from dedup because each delegation
    # is a unique long-running task — even if args look similar, the context
    # and state differ between calls.
    _DEDUP_EXCLUDED_TOOLS = {"hub_delegate", "tesla_wake", "analyze_image"}
    _latest_user_event: dict[str, str] = {}
    _last_recorded_user_turn_key: list[str] = [""]
    _tool_call_count_this_turn: list[int] = [0]
    _search_tools_exhausted: list[bool] = [False]
    _PROVIDER_CLASS: dict[str, str] = {
        "web_search": "cloud:perplexity",
        "get_weather": "cloud:perplexity",
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

    async def _ensure_user_turn_learning_event():
        canonical_text = (_latest_user_event.get("canonical_text") or "").strip()
        if not canonical_text:
            return
        key = f"{session.session_id}:{canonical_text}"
        if _last_recorded_user_turn_key[0] == key:
            return
        _last_recorded_user_turn_key[0] = key
        await _record_learning_event(
            event_type="user_turn_received",
            source_layer="transport",
            raw_text=_latest_user_event.get("raw_text", ""),
            canonical_text=canonical_text,
            location=_latest_user_event.get("location", ""),
            mode_policy=_latest_user_event.get("mode_policy", ""),
            outcome="received",
            payload={
                "audio_mode": audio_mode,
                "capture": "ensure_user_turn_learning_event",
            },
        )

    async def _active_action_binding_context_text(user_text: str) -> str:
        stripped = str(user_text or "").strip().lower().strip(" .!?")
        confirmations = {"yes", "yes please", "go ahead", "send it", "send it now", "please do", "do it", "confirm"}
        if stripped not in confirmations:
            return ""
        try:
            entry = await get_active_action_ledger_entry(
                user_id=user_id,
                session_id=session.session_id,
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.warning(f"Active action binding context lookup failed: {e}")
            return ""
        if not entry or str(entry.get("status") or "") != "awaiting_confirmation":
            return ""
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        intent = str(entry.get("intent") or "")
        if intent != "tesla_navigation_plan":
            return ""
        destination = str(target.get("destination") or "").strip()
        vehicle_hint = str(target.get("vehicle_hint") or "").strip()
        vin = str(target.get("vin") or "").strip()
        action_id = str(entry.get("action_id") or "").strip()
        lines = [
            "",
            "",
            "[ACTIVE ACTION BINDING CONTEXT]",
            "There is a durable pending action awaiting confirmation.",
            f"action_id: {action_id}",
            "intent: tesla_navigation_plan",
            "status: awaiting_confirmation",
            f"destination: {destination}",
            f"vehicle_hint: {vehicle_hint}",
            f"vin: {vin}",
            "If the user confirmation means proceed, act on this pending action.",
            "For Tesla navigation, call tesla_navigation with the recorded destination and vin if present.",
            "If a vehicle_hint exists but vin is missing, call tesla_control with action=vehicles first and resolve the exact vehicle; do not guess.",
            "Do not call query_cig, check_studio, search_past_conversations, web_search, or memory tools for this confirmation.",
            "Do not claim completion unless tesla_navigation returns successful evidence.",
            "[/ACTIVE ACTION BINDING CONTEXT]",
        ]
        return "\n".join(lines)

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
        if tool_name == "manage_workspace":
            return (
                "SYSTEM: manage_workspace has been called the maximum number of times this turn. "
                "If every search returned no results, the page does not exist yet — stop searching and CREATE it now using create_page_with_blocks or create_page. "
                "Do not search again. Either create the requested content or tell the user exactly what you found and ask for the missing details."
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
                dedup_count = _consecutive_dedup_counts.get(tool_name, 0) + 1
                _consecutive_dedup_counts[tool_name] = dedup_count
                logger.warning(f"🔥 Duplicate tool call detected: {tool_name} (consecutive #{dedup_count}), forcing response")
                if dedup_count >= 2:
                    # LLM is stuck in a loop — soft messages aren't working.
                    # Inject a terminal message and force the turn to end.
                    logger.error(f"NOVA_DEDUP_HARD_STOP | tool={tool_name} | consecutive_dupes={dedup_count} | forcing turn end")
                    await params.result_callback(
                        f"SYSTEM HARD STOP: {tool_name} has been called with identical arguments {dedup_count} times in a row. "
                        "The tool loop is broken. You MUST stop calling tools immediately and speak your response now. "
                        "Tell the user what you found and what you need from them. Do NOT call any tool."
                    )
                else:
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
            if _active_voice_turn[0] is not None:
                if tool_name == "search_past_conversations":
                    await _active_voice_turn[0].emit_status(
                        "grounding_context",
                        "Searching prior conversations and excluding the active thread.",
                        tool=tool_name,
                    )
                await _active_voice_turn[0].tool_started(tool_name, f"Using {tool_name}.")
            await _emit_turn_status("tool_selected", f"Using {tool_name}.", tool=tool_name)

            # ── Hard limit: pure runaway-loop guard (not a quality throttle) ──
            if call_num > _MAX_TOOL_CALLS_HARD_LIMIT:
                logger.warning(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | status=hard_limit_exceeded | total_calls={call_num}")
                await params.result_callback(
                    "SYSTEM: Runaway tool loop detected. You MUST respond to the user now "
                    "with whatever information you have gathered so far. Do NOT call any more tools."
                )
                return

            # ── M2.7 soft synthesis nudge ────────────────────────────────────
            # MiniMax M2.7 is designed to fan-out parallel tool calls in 2-3
            # LLM passes and then synthesize. After ~6 individual calls without
            # a synthesized response, gently push the model toward responding.
            # Unlike the hard limit, this does NOT block the call — it lets it
            # proceed but marks that the next pass should prefer text.
            if call_num == _SOFT_SYNTHESIS_NUDGE_AT and not _soft_synthesis_nudged_this_turn[0]:
                _soft_synthesis_nudged_this_turn[0] = True
                logger.info(
                    f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | "
                    f"status=soft_synthesis_nudge | total_calls={call_num}"
                )

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
                # speakable: iOS may TTS this to fill dead-air mid-turn while
                # the model is suppressing its own transitional filler text.
                await _send_server_msg({
                    "type": "heartbeat",
                    "text": "Still working on it...",
                    "speakable": True,
                })
                logger.info(f"Auto-heartbeat at tool call #{call_num}")

            # ── Fast path: instant spoken acknowledgment (once per turn) ──
            if tool_name in _SLOW_TOOLS and not _ack_sent_this_turn[0]:
                ack = _build_spoken_ack(tool_name, args)
                if ack:
                    _ack_sent_this_turn[0] = True
                    # speakable: lets iOS play a brief audible "searching..."
                    # so the user hears progress while LLM transitional text
                    # is being suppressed.
                    await _send_server_msg({
                        "type": "heartbeat",
                        "text": ack,
                        "speakable": True,
                    })
                    logger.info(f"Dual-path ack: '{ack}'")

            # For slow tools, populate the ThinkingCard with progress
            if tool_name in _SLOW_TOOLS:
                thinking_text = _build_thinking_text(tool_name, args)
                await _send_server_msg({"phase": "thinking"})
                await _send_server_msg({
                    "type": "thinking",
                    "text": thinking_text,
                })
                await _emit_turn_status("tool_running", thinking_text, tool=tool_name)
                
            # Emit granular validationStep for UI
            _provider_phase = "querying" if provider_class.startswith("local:") else "fetching"
            await _send_server_msg({
                "type": "validationStep",
                "tool": tool_name,
                "status": "running",
                "phase": _provider_phase,
                "turn_id": _current_turn_id[0],
            })
            
            await _ensure_user_turn_learning_event()
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

            # ── P1b: Per-turn read cache ────────────────────────────────────
            # If this exact (tool_name, args) combo was already executed this
            # turn for an idempotent read tool, return the cached result and
            # nudge M2.7 to use the data already in context.
            def _is_cacheable_call(_tn: str, _a: dict) -> bool:
                if _tn not in _CACHEABLE_READ_TOOLS:
                    return False
                _action = (_a.get("action") or "").lower()
                _allowed = _CACHEABLE_ACTIONS_BY_TOOL.get(_tn)
                if _allowed is None:
                    # Tool is fully cacheable (no action sub-routing)
                    return True
                # Tool has action-based dispatch: only cache safe actions
                return _action in _allowed

            _cache_key: tuple[str, str] | None = None
            if _is_cacheable_call(tool_name, args):
                # Strip volatile fields before keying
                _key_args = {k: v for k, v in args.items() if k != "_internal_user_id"}
                _cache_key = (tool_name, json.dumps(_key_args, sort_keys=True, default=str))
                if _cache_key in _tool_read_cache_this_turn:
                    cached = _tool_read_cache_this_turn[_cache_key]
                    logger.info(
                        f"NOVA_TOOL_CACHE_HIT | tool={tool_name} "
                        f"action={args.get('action','-')} | bytes={len(cached)}"
                    )
                    if _active_voice_turn[0] is not None:
                        try:
                            await _active_voice_turn[0].emit_status(
                                "tool_cache_hit",
                                f"Reusing cached {tool_name} result from earlier this turn.",
                                tool=tool_name,
                            )
                        except Exception:
                            pass
                    cached_with_hint = (
                        f"{cached}\n\n"
                        f"SYSTEM: This is a CACHED result from earlier this turn. "
                        f"You already have this data — use it directly instead of "
                        f"calling {tool_name} again with the same arguments."
                    )
                    try:
                        await params.result_callback(cached_with_hint)
                    except Exception as cb_exc:
                        logger.warning(f"NOVA_TOOL_CACHE_CALLBACK_FAILED | {cb_exc}")
                    return

            # Inject internal user id for tools that need to spawn background tasks
            args["_internal_user_id"] = user_id
            
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
                # Calendar-specific: after a CALENDAR_LOOKUP turn completes its
                # check_studio call, prevent re-fetch within the same turn only.
                # Do NOT fire on email/contacts results — "meeting" appears in
                # email subjects constantly and would block legitimate email searches.
                if tool_name == "check_studio" and isinstance(_turn_state, object):
                    from nova.turn_orchestrator import TurnIntent
                    if getattr(_turn_state, "last_intent", "") == TurnIntent.CALENDAR_LOOKUP.value:
                        _search_tools_exhausted[0] = True
            except asyncio.TimeoutError:
                _latency_ms = int((time.monotonic() - _t_start) * 1000)
                logger.error(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | latency={_latency_ms}ms | bytes_out=0 | status=timeout")
                result = f"Tool {tool_name} timed out after {tool_timeout:.0f}s. The operation took too long to complete."
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].tool_failed(tool_name, f"{tool_name} timed out.")
                await _emit_turn_status("tool_failed", f"{tool_name} timed out.", tool=tool_name, severity="error")
                await _send_server_msg({
                    "type": "validationStep",
                    "tool": tool_name,
                    "status": "failed",
                    "result": "Timed out",
                    "latency_ms": _latency_ms,
                    "turn_id": _current_turn_id[0],
                })
            except Exception as e:
                _latency_ms = int((time.monotonic() - _t_start) * 1000)
                logger.error(f"NOVA_TRAFFIC | tool={tool_name} | provider={provider_class} | latency={_latency_ms}ms | bytes_out=0 | status=error | err={e}")
                result = f"Tool execution error: {str(e)}"
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].tool_failed(tool_name, f"{tool_name} failed.")
                await _emit_turn_status("tool_failed", f"{tool_name} failed.", tool=tool_name, severity="error")
                await _send_server_msg({
                    "type": "validationStep",
                    "tool": tool_name,
                    "status": "failed",
                    "result": "Error occurred",
                    "latency_ms": _latency_ms,
                    "turn_id": _current_turn_id[0],
                })
            finally:
                heartbeat_task.cancel()
            
            # Structured-card support: tools may return a dict of the shape
            #   {"speakable": "<text for LLM/TTS>", "card": {"kind": "...", ...}}
            # in which case we forward the card to iOS as a server message and
            # only feed the speakable text back to the LLM. Falls through to
            # normal string handling for every other tool.
            parsed_result = result
            if isinstance(result, str):
                try:
                    parsed_result = json.loads(result)
                except Exception:
                    parsed_result = result

            if isinstance(parsed_result, dict) and parsed_result.get("display") and parsed_result.get("speech"):
                display_text = str(parsed_result.get("display") or "")
                speech_text = str(parsed_result.get("speech") or parsed_result.get("speakable") or display_text)
                card_payload = parsed_result.get("card") or {}
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
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].complete_with_structured_response(
                        display_text,
                        speech_text,
                        result=tool_name,
                    )
                else:
                    await _send_server_msg({
                        "type": "validated",
                        "result": tool_name,
                        "text": display_text,
                        "speechText": speech_text,
                        "suppressSpeech": True,
                    })
                    await _send_server_msg({"type": "turn_complete"})
                logger.info(f"🔥 HANDLER COMPLETE for {tool_name} after structured response")
                
                # We MUST call the Pipecat callback even when short-circuiting,
                # otherwise the LLM pipeline task hangs forever waiting for the tool result.
                try:
                    await params.result_callback("SYSTEM: Handled directly via structured UI response. Do not respond further. End this turn now.")
                except Exception as e:
                    logger.warning(f"Failed to send short-circuit callback for {tool_name}: {e}")
                    
                return
            elif isinstance(parsed_result, dict) and "card" in parsed_result and "speakable" in parsed_result:
                card_payload = parsed_result.get("card") or {}
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
                result = str(parsed_result.get("speakable") or "")
            elif isinstance(parsed_result, (dict, list)):
                result = json.dumps(parsed_result, indent=2)

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
                    "latency_ms": locals().get("_latency_ms", 0),
                    "turn_id": _current_turn_id[0],
                })
            else:
                result = result_str
                # Only emit completed if it wasn't already emitted as failed in the except blocks
                # We can check if it's an error string, but simply emitting completed here is fine for successful paths.
                # Actually, wait, the exception blocks set result to a string and continue, so they fall through here!
                # We should conditionally emit "completed" only if not an error.
                if not result_str.startswith("Tool execution error:") and not result_str.startswith(f"Tool {tool_name} timed out"):
                    # Extract a snippet for the UI
                    snippet = result_str[:100]
                    if _active_voice_turn[0] is not None:
                        await _active_voice_turn[0].tool_completed(tool_name, f"{tool_name} completed.")
                    await _emit_turn_status("tool_completed", f"{tool_name} completed.", tool=tool_name)
                    _result_preview = result_str[:200].strip()
                    await _send_server_msg({
                        "type": "validationStep",
                        "tool": tool_name,
                        "status": "completed",
                        "result": snippet,
                        "result_preview": _result_preview,
                        "latency_ms": locals().get("_latency_ms", 0),
                        "turn_id": _current_turn_id[0],
                    })
            # Clear ThinkingCard phase so the LLM's next response is spoken, not swallowed
            if tool_name in _SLOW_TOOLS:
                await _send_server_msg({"phase": "done"})

            final_result_str = str(result) if result is not None else ""
            failure_markers = (
                "timed out",
                "returned http 404",
                "returned http 500",
                "tool execution error:",
                "tool error:",
                "search failed",
                "search error",
                "temporarily rate-limited",
                "daily budget limit",
                "daily_budget_exceeded",
                "could not retrieve current external evidence",
                "no emails found",
                "thread lookup failed",
                "thread not found",
                "calendar search api returned http 404",
                "pi agent timed out",
            )
            # Only scan the header/status portion of the result (first 350 chars).
            # Scanning the full result causes false positives when embedded data
            # payloads (conversation excerpts, email bodies, web search snippets)
            # happen to contain error phrases from prior Nova failures.
            _result_header = final_result_str[:350].lower()
            tool_success = bool(final_result_str.strip()) and not any(marker in _result_header for marker in failure_markers)

            # ── P1b: write result to per-turn cache (success only) ───────────
            if tool_success and _cache_key is not None:
                _tool_read_cache_this_turn[_cache_key] = final_result_str
                logger.info(
                    f"NOVA_TOOL_CACHE_STORE | tool={tool_name} "
                    f"action={args.get('action','-')} | bytes={len(final_result_str)} "
                    f"| keys_in_cache={len(_tool_read_cache_this_turn)}"
                )

            # ── Auto-link workspace page to active plan ───────────────────────
            # When manage_workspace creates a page successfully, wire it to the
            # active session plan so the spine has a permanent workspace anchor.
            if tool_success and tool_name == "manage_workspace":
                _ws_action = (args or {}).get("action", "")
                if _ws_action in ("create_page", "create_page_with_blocks"):
                    import re as _re
                    _m = _re.search(r"page_id:\s*([0-9a-f-]{8,})", final_result_str, _re.IGNORECASE)
                    if not _m:
                        _m = _re.search(r"\(([0-9a-f]{8}-[0-9a-f-]{4,36})\)", final_result_str)
                    _created_page_id = _m.group(1).strip() if _m else ""
                    if _created_page_id:
                        async def _link_page_hook(
                            _pid=_created_page_id,
                            _title=(args or {}).get("title", ""),
                            _uid=user_id,
                            _plan_id=_turn_state.active_plan_id,
                            _user_text=_latest_user_event.get("canonical_text", ""),
                            _goal=_turn_state.active_goal,
                        ):
                            try:
                                linked = await auto_link_workspace_page(
                                    user_id=_uid,
                                    plan_id=_plan_id or None,
                                    page_id=_pid,
                                    page_title=_title,
                                    text=_user_text,
                                    plan_goal=_goal,
                                    conversation_id=conversation_id,
                                )
                                if linked:
                                    _turn_state.active_plan_id = linked.get("plan_id", _turn_state.active_plan_id)
                                    _turn_state.active_plan_topic = linked.get("topic", _turn_state.active_plan_topic)
                                    _turn_state.active_plan_page_id = _pid
                                    # Seed the page into the session dictionary so the next
                                    # planner_hook can include it in ## Known Workspace Pages
                                    _kp_existing = {p["page_id"] for p in _turn_state.known_workspace_pages}
                                    if _pid not in _kp_existing:
                                        _turn_state.known_workspace_pages.append({
                                            "page_id": _pid,
                                            "title": _title,
                                            "project_key": linked.get("project_key", "") or "",
                                        })
                                    await emit_plan_state(_send_server_msg, linked)
                                    logger.info(
                                        f"NOVA_PLANNER | page_linked | "
                                        f"plan_id={_turn_state.active_plan_id} page_id={_pid}"
                                    )
                                    # ── P2a: auto-populate plan steps from heading_2 blocks ──
                                    try:
                                        from nova.task_plan import (
                                            add_step as _add_step,
                                            get_plan as _get_plan,
                                        )
                                        _plan_full = await _get_plan(linked["plan_id"])
                                        # Only seed steps if the plan currently has none
                                        if _plan_full and not _plan_full.get("steps"):
                                            _blocks = []
                                            _props = (args or {}).get("properties") or {}
                                            if isinstance(_props, dict):
                                                _blocks = _props.get("blocks") or []
                                            if not _blocks:
                                                _blocks = (args or {}).get("blocks") or []
                                            _step_count = 0
                                            for _i, _b in enumerate(_blocks):
                                                if not isinstance(_b, dict):
                                                    continue
                                                if _b.get("type") == "heading_2":
                                                    _step_title = (_b.get("content") or "").strip()[:140]
                                                    if _step_title:
                                                        await _add_step(
                                                            linked["plan_id"],
                                                            _step_title,
                                                            order_num=_i,
                                                        )
                                                        _step_count += 1
                                                        if _step_count >= 12:
                                                            break
                                            if _step_count:
                                                logger.info(
                                                    f"NOVA_PLANNER | auto_seeded_steps | "
                                                    f"plan_id={linked['plan_id']} count={_step_count}"
                                                )
                                                # Re-emit plan_state so iOS planner panel shows steps
                                                _plan_full = await _get_plan(linked["plan_id"])
                                                if _plan_full:
                                                    await emit_plan_state(_send_server_msg, _plan_full)
                                    except Exception as _se:
                                        logger.warning(f"NOVA_PLANNER_AUTO_STEPS_FAILED | {_se}")
                                    # ── P2c: bidirectional plan↔page indexing ──
                                    # Write properties.plan_id and properties.project_key onto
                                    # the workspace page so search/filter by project works.
                                    try:
                                        import aiohttp as _aiohttp
                                        _pkey_for_index = linked.get("project_key", "") or ""
                                        _pi_ws_base = os.environ.get(
                                            "PI_WORKSPACE_URL", "http://localhost:8762"
                                        ).rstrip("/")
                                        _index_payload = {
                                            "properties": {
                                                "plan_id": linked["plan_id"],
                                                "project_key": _pkey_for_index,
                                            }
                                        }
                                        _index_url = f"{_pi_ws_base}/api/pages/{_pid}"
                                        async with _aiohttp.ClientSession() as _sess:
                                            async with _sess.patch(
                                                _index_url,
                                                json=_index_payload,
                                                timeout=_aiohttp.ClientTimeout(total=5),
                                            ) as _resp:
                                                if _resp.status >= 400:
                                                    _txt = await _resp.text()
                                                    logger.warning(
                                                        f"NOVA_PLANNER_INDEX_FAILED | "
                                                        f"page_id={_pid} status={_resp.status} body={_txt[:200]}"
                                                    )
                                                else:
                                                    logger.info(
                                                        f"NOVA_PLANNER | page_indexed | "
                                                        f"page_id={_pid} plan_id={linked['plan_id']} "
                                                        f"project_key={_pkey_for_index!r}"
                                                    )
                                    except Exception as _ie:
                                        logger.warning(f"NOVA_PLANNER_INDEX_EXC | {_ie}")
                            except Exception as _le:
                                logger.warning(f"NOVA_PLANNER_LINK_FAILED | {_le}")
                        asyncio.create_task(_link_page_hook())

            if not tool_success:
                failure_notice = (
                    f"TOOL_ERROR [{tool_name}]: "
                    f"{final_result_str[:240].strip() or 'no usable result was returned'}\n"
                    "SYSTEM: The above tool failed. Assess the error and decide: "
                    "(a) retry with corrected arguments, "
                    "(b) try an alternative tool, or "
                    "(c) answer the user directly from what you already know. "
                    "Do NOT repeat the exact same call that just failed."
                )
                _tool_failures_this_turn[0] += 1
                # ── M2.7 agentic recovery ────────────────────────────────────
                # Tools that warrant agentic retry (non-destructive, no approval gate)
                _RECOVERABLE_TOOLS = {
                    "manage_task_plan", "search_past_conversations", "query_cig",
                    "manage_workspace", "recall_memory", "web_search", "get_weather",
                    "service_status", "service_logs", "check_studio",
                    "tesla_control", "tesla_navigation", "tesla_wake",
                    "get_time", "manage_timer", "get_workstation_status",
                }
                _allow_agentic_recovery = (
                    tool_name in _RECOVERABLE_TOOLS
                    and _tool_failures_this_turn[0] <= _MAX_AGENTIC_RECOVERIES
                )
                if _allow_agentic_recovery:
                    logger.warning(
                        f"NOVA_AGENTIC_RECOVERY | tool={tool_name} "
                        f"failure_num={_tool_failures_this_turn[0]} "
                        f"max={_MAX_AGENTIC_RECOVERIES} | passing error to M2.7"
                    )
                    if _active_voice_turn[0] is not None:
                        await _active_voice_turn[0].tool_failed(tool_name, f"{tool_name} failed — M2.7 deciding recovery")
                    await _record_learning_event(
                        event_type="tool_call_completed",
                        source_layer="llm_tool_loop",
                        raw_text=_latest_user_event.get("raw_text", ""),
                        canonical_text=_latest_user_event.get("canonical_text", ""),
                        location=_latest_user_event.get("location", ""),
                        mode_policy=_latest_user_event.get("mode_policy", ""),
                        tool_name=tool_name,
                        tool_args=args,
                        success=False,
                        latency_ms=locals().get("_latency_ms", 0),
                        outcome="failed_agentic_recovery",
                        payload={"provider_class": provider_class, "result_preview": final_result_str[:200]},
                    )
                    # Pass failure to M2.7 with run_llm=True — let it self-recover
                    try:
                        await params.result_callback(failure_notice)  # default run_llm=True
                    except Exception as cb_exc:
                        logger.warning(f"NOVA_AGENTIC_RECOVERY_CALLBACK_FAILED | {cb_exc}")
                    logger.info(f"🔥 HANDLER COMPLETE for {tool_name} (agentic recovery handed to M2.7)")
                    return
                else:
                    # Hard-surface: exceeded recovery budget or non-recoverable tool
                    _surfaced_failure = (
                        f"I tried to use {tool_name}, but it failed: "
                        f"{final_result_str[:240].strip() or 'no usable result was returned'}"
                    )
                    if _active_voice_turn[0] is not None:
                        await _active_voice_turn[0].tool_failed(tool_name, _surfaced_failure)
                        await _active_voice_turn[0].complete_with_error(_surfaced_failure)
                    else:
                        logger.warning(f"NOVA_TOOL_ERROR_FINAL_WITHOUT_RUNTIME | tool={tool_name}")
                        await _complete_orphaned_tool_failure(tool_name, _surfaced_failure)
                    failure_notice = _surfaced_failure  # use clean version for callback below
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
                    "promotion_candidate": tool_success and tool_name in {"save_memory", "recall_memory", "search_past_conversations", "query_cig", "web_search", "hub_delegate", "tesla_control", "get_weather"},
                },
            )
            if not tool_success:
                # CRITICAL: still close out the function call in Pipecat's aggregator.
                # If we skip result_callback, the tool_call_id stays in
                # `_function_calls_in_progress` forever and blocks ALL subsequent
                # LLM re-invocations after tool calls (Issue A). We pass run_llm=False
                # because the voice runtime already surfaced the error via
                # `complete_with_error` above — we don't want a duplicate LLM response.
                try:
                    await params.result_callback(
                        failure_notice,
                        properties=FunctionCallResultProperties(run_llm=False),
                    )
                except Exception as cb_exc:
                    logger.warning(
                        f"NOVA_SURFACED_FAILURE_CALLBACK_FAILED | tool={tool_name} err={cb_exc}"
                    )
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].complete_turn()
                logger.info(f"🔥 HANDLER COMPLETE for {tool_name} after surfaced failure")
                return

            if tool_name == "analyze_image":
                image_question = str(args.get("prompt") or "").strip()
                result = (
                    "VISION ANALYSIS RESULT:\n"
                    f"{str(result).strip()}\n\n"
                    "SYSTEM: When answering the user about this image, do not return one dense paragraph. "
                    "Format the visual answer for the app display with short Markdown sections: "
                    "`### Summary`, `### Notable details`, and, when useful, `### Answer to your question`. "
                    "Use concise bullets under details. Keep speech natural and avoid dumping raw metadata. "
                    f"User image question: {image_question}"
                )
                _last_tool_call["name"] = tool_name
                _last_tool_call["args"] = json.dumps(args, sort_keys=True, default=str)
                _last_tool_call["result"] = result

            result = _trim_tool_result_for_llm(tool_name, result)

            # ── M2.7 synthesis nudge ────────────────────────────────────────
            # Once we've crossed _SOFT_SYNTHESIS_NUDGE_AT calls in this turn,
            # append a SYSTEM hint to every subsequent tool result so the model
            # is repeatedly reminded to synthesize rather than chain more
            # lookups. This aligns with M2.7's fan-out-then-synthesize design.
            if _soft_synthesis_nudged_this_turn[0]:
                result_str = str(result) if result is not None else ""
                synthesis_hint = (
                    "\n\nSYSTEM: You have made several lookups this turn "
                    f"({_tool_calls_this_turn[0]} so far). Per M2.7 protocol, "
                    "synthesize a final response now using the data you already "
                    "have. Only call another tool if it is strictly required to "
                    "complete a user-requested action."
                )
                if not result_str.endswith(synthesis_hint):
                    result = result_str + synthesis_hint

            # ── Reasoning scaffold: unified injection (shared with text_chat.py) ──
            tc = _active_turn_context[0]
            if tc is not None:
                try:
                    args_preview = json.dumps(args, default=str)[:80]
                    result_str_in = str(result) if result is not None else ""
                    augmented = augment_tool_result(tc, tool_name, args_preview, result_str_in)
                    if augmented != result_str_in:
                        result = augmented
                        logger.info(
                            f"NOVA_TURN_ANCHOR_INJECTED | tool={tool_name} "
                            f"posture={tc.posture} calls={len(tc.tool_history)} evidence={len(tc.evidence_log)}"
                        )
                except Exception as e:
                    logger.warning(f"TurnContext update/render failed (non-fatal): {e}")

            logger.info(f"🔥 Calling result_callback for {tool_name} with {len(str(result))} chars: {str(result)[:150]}")
            try:
                await params.result_callback(result)
                logger.info(f"🔥 result_callback completed for {tool_name}")
                # Cache the result for deduplication; clear consecutive-dedup counter on success
                _last_tool_call["name"] = tool_name
                _last_tool_call["args"] = json.dumps(args, sort_keys=True, default=str)
                _last_tool_call["result"] = result
                _consecutive_dedup_counts.pop(tool_name, None)
                logger.info(f"🔥 Cached result for {tool_name}")
                # Small delay to ensure Pipecat processes the result before handler returns
                await asyncio.sleep(0.1)
                logger.info(f"🔥 HANDLER COMPLETE for {tool_name}")
            except Exception as e:
                logger.error(f"🔥 result_callback failed for {tool_name}: {e}", exc_info=True)
                if _active_voice_turn[0] is not None and not _active_voice_turn[0].snapshot.final_response_sent:
                    await _active_voice_turn[0].complete_with_error(
                        f"I used {tool_name}, but the model pipeline failed while processing the tool result. Please try again."
                    )
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
    # Non-blocking background delegation (long research / writing tasks)
    llm.register_function("delegate_background", make_tool_handler("delegate_background"))
    llm.register_function("background_task_status", make_tool_handler("background_task_status"))
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
    llm.register_function("set_active_goal", make_tool_handler("set_active_goal"))
    llm.register_function("complete_active_goal", make_tool_handler("complete_active_goal"))
    llm.register_function("manage_task_plan", make_tool_handler("manage_task_plan"))
    llm.register_function("search_framework_catalog", make_tool_handler("search_framework_catalog"))

    compacted_messages = await get_compacted_context(
        conversation_id=conversation_id,
        user_id=user_id,
        max_recent_turns=MAX_HISTORY_TURNS,
    )
    restored_context_messages = _trim_context_messages(compacted_messages or [
        {"role": turn.role, "content": turn.content}
        for turn in prior_turns[-MAX_HISTORY_TURNS:]
    ])
    _semantic_recent_messages: list[dict] = list(restored_context_messages)

    messages = [{"role": "system", "content": system_prompt}]
    for msg in restored_context_messages:
        messages.append(msg)

    _approx = _estimate_tokens(messages)
    logger.info(
        f"NOVA_PROMPT_BUDGET | restored_context={len(restored_context_messages)} "
        f"messages={len(messages)} approx_tokens={_approx} "
        f"system_chars={len(system_prompt)}"
    )
    check_overflow_risk(
        _approx, path="voice_init",
        message_count=len(messages),
        extra={"restored": len(restored_context_messages)},
    )

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
    _pi_first_text: list[bool] = [False]
    _pi_has_tool_call: list[bool] = [False]

    class PersistenceInterceptor(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if isinstance(frame, LLMFullResponseStartFrame):
                _pi_first_text[0] = False
                _pi_has_tool_call[0] = False
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].llm_started()
                await _send_server_msg({"phase": "thinking"})
                await ctx_watcher.check_and_persist()

            elif isinstance(frame, FunctionCallsStartedFrame):
                _pi_has_tool_call[0] = True

            elif isinstance(frame, LLMTextFrame):
                if _active_voice_turn[0] is not None and _active_voice_turn[0].snapshot.turn_complete_sent:
                    return
                _pi_buffer.append(frame.text)
                if not _pi_first_text[0]:
                    _pi_first_text[0] = True
                    if _active_voice_turn[0] is not None:
                        await _active_voice_turn[0].append_llm_text(frame.text)
                    await _send_server_msg({"phase": "responding"})
                elif _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].append_llm_text(frame.text)

            elif isinstance(frame, LLMFullResponseEndFrame):
                if _active_voice_turn[0] is not None and _active_voice_turn[0].snapshot.turn_complete_sent:
                    _pi_buffer.clear()
                    return
                if _pi_has_tool_call[0]:
                    # Intermediate LLM pass — this generation contained a tool call.
                    # The real final response comes after the tool executes and the
                    # LLM runs again. Discard buffered text and let the next pass
                    # produce the actual response.
                    _pi_buffer.clear()
                    _pi_has_tool_call[0] = False
                    logger.debug(f"NOVA_LLM_INTERMEDIATE_PASS_SKIPPED | turn_id={_current_turn_id[0]}")
                elif _pi_buffer:
                    full_text = "".join(_pi_buffer)
                    _pi_buffer.clear()
                    # ── Stream-truncation detection (finish_reason=length) ────
                    # M2.7 truncates mid-response when the generation runs out
                    # of token budget. Signs: substantial text (>150 chars) that
                    # ends without terminal punctuation. Retry with a nudge rather
                    # than silently deliver a half-finished answer.
                    _TRUNCATION_MIN_CHARS = 150
                    _terminal_punct = (".", "!", "?", ":", "```", "*", ">")
                    _looks_truncated = (
                        len(full_text.strip()) >= _TRUNCATION_MIN_CHARS
                        and not any(full_text.rstrip().endswith(p) for p in _terminal_punct)
                    )
                    if _looks_truncated and _active_voice_turn[0] is not None:
                        logger.warning(
                            f"NOVA_STREAM_TRUNCATION_DETECTED | turn_id={_current_turn_id[0]} "
                            f"chars={len(full_text)} last50={full_text[-50:]!r}"
                        )
                        _plan_anchor = ""
                        if _turn_state.active_plan_id:
                            _plan_anchor = (
                                f" (Active plan: {_turn_state.active_plan_topic or _turn_state.active_plan_id})"
                            )
                        recovery_hint = (
                            f"SYSTEM: Your last response was cut off due to token limit.{_plan_anchor} "
                            "Please continue from where you left off and complete your response. "
                            "Keep the response concise — summarize if needed."
                        )
                        try:
                            await context.add_message({"role": "assistant", "content": full_text})
                            await context.add_message({"role": "user", "content": recovery_hint})
                            await task.queue_frame(LLMRunFrame())
                            logger.info(f"NOVA_STREAM_TRUNCATION_RETRY | turn_id={_current_turn_id[0]}")
                        except Exception as _tre:
                            logger.warning(f"NOVA_STREAM_TRUNCATION_RETRY_FAILED | {_tre}")
                            from nova.text_utils import strip_markdown_for_speech
                            speech_text = strip_markdown_for_speech(full_text)
                            await _active_voice_turn[0].complete_with_text(
                                full_text,
                                speech_text=speech_text,
                                result="direct",
                                suppress_speech=True,
                            )
                    elif full_text.strip():
                        logger.info(f"Persisted assistant turn ({len(full_text)} chars)")
                        from nova.text_utils import strip_markdown_for_speech
                        speech_text = strip_markdown_for_speech(full_text)
                        if _active_voice_turn[0] is not None:
                            await _active_voice_turn[0].complete_with_text(
                                full_text,
                                speech_text=speech_text,
                                result="direct",
                                suppress_speech=True,
                            )
                elif _active_voice_turn[0] is not None:
                    if _active_voice_turn[0].snapshot.llm_error:
                        await _active_voice_turn[0].complete_with_error("I heard you, but the model service failed before it returned a response. Please try that again.")
                    elif _active_voice_turn[0].snapshot.llm_started:
                        logger.warning(
                            f"NOVA_EMPTY_LLM_RESPONSE_SUPPRESSED | "
                            f"turn_id={_current_turn_id[0]} tools_started={_active_voice_turn[0].snapshot.tools_started}"
                        )
                
                # Async background learning consolidation
                asyncio.create_task(consolidate_session_learning(session.session_id))

            elif isinstance(frame, ErrorFrame):
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].llm_failed(str(getattr(frame, "error", "") or "Pipeline error"))

            await self.push_frame(frame, direction)

    class TurnOrchestratorFrameProcessor(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if isinstance(frame, ErrorFrame):
                if _active_voice_turn[0] is not None:
                    await _active_voice_turn[0].llm_failed(str(getattr(frame, "error", "") or "Pipeline error"))
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, (LLMRunFrame, LLMMessagesAppendFrame, LLMMessagesUpdateFrame)) and should_consume_llm_frame_after_orchestrator(
                frame,
                _active_voice_turn[0],
                _orchestrator_consumed_turn_id[0],
            ):
                logger.warning(
                    f"NOVA_ORCHESTRATOR_CONSUMED_LLM_RUN_SUPPRESSED | "
                    f"turn_id={_active_voice_turn[0].snapshot.turn_id} frame={type(frame).__name__}"
                )
                return

            text = ""
            turn_continuity = {}
            if isinstance(frame, InputTransportMessageFrame):
                msg = frame.message
                if isinstance(msg, dict) and msg.get("type") == "session-resume":
                    continuity = _extract_continuity_payload(msg)
                    changed = _apply_continuity_to_turn_state(_turn_state, continuity)
                    if changed:
                        try:
                            await update_session_metadata_key(
                                session.session_id,
                                STATE_METADATA_KEY,
                                turn_state_to_metadata_value(_turn_state),
                            )
                        except Exception as e:
                            logger.warning(f"Session resume state persist failed: {e}")
                    logger.info(
                        f"NOVA_SESSION_RESUME | conv={conversation_id} changed={changed} "
                        f"client_turn={continuity.get('lastClientTurnId') or continuity.get('last_client_turn_id') or ''} "
                        f"server_turn={continuity.get('lastServerTurnId') or continuity.get('last_server_turn_id') or ''} "
                        f"artifact={_turn_state.active_task_artifact_id}"
                    )
                    await _send_server_msg({
                        "type": "session_resumed",
                        "conversationId": conversation_id,
                        "activeTaskArtifactId": _turn_state.active_task_artifact_id,
                        "activeGoal": _turn_state.active_goal,
                        "activeAgentRunId": _turn_state.active_workflow_run_id,
                    })
                    return
                if isinstance(msg, dict) and msg.get("type") == "send-text":
                    if _is_interim_send_text_message(msg):
                        logger.info("NOVA_STT_INTERIM_IGNORED | transport=turn_orchestrator")
                        return
                    turn_continuity = _extract_continuity_payload(msg)
                    if turn_continuity and _apply_continuity_to_turn_state(_turn_state, turn_continuity):
                        try:
                            await update_session_metadata_key(
                                session.session_id,
                                STATE_METADATA_KEY,
                                turn_state_to_metadata_value(_turn_state),
                            )
                        except Exception as e:
                            logger.warning(f"Turn continuity state persist failed: {e}")
                    text = _enrich_send_text_with_image_context(msg)
                    image_block = _extract_contextual_image_block(text)
                    if image_block:
                        _latest_image_context[0] = image_block
                        try:
                            await update_session_metadata_key(
                                session.session_id,
                                IMAGE_CONTEXT_METADATA_KEY,
                                image_block,
                            )
                        except Exception as e:
                            logger.warning(f"Image context metadata persist failed: {e}")
                    elif _latest_image_context[0] and _looks_like_image_followup(text):
                        data = msg.get("data", {})
                        if isinstance(data, dict):
                            text = f"{text.rstrip()}\n\n{_latest_image_context[0]}"
                            data["content"] = text
            elif isinstance(frame, TranscriptionFrame):
                if getattr(frame, "user_id", "") != "system":
                    text = getattr(frame, "text", "") or ""

            if text:
                _current_turn_id[0] = _new_turn_id()
                _orchestrator_consumed_turn_id[0] = ""
                _ack_sent_this_turn[0] = False
                _tool_calls_this_turn[0] = 0
                _soft_synthesis_nudged_this_turn[0] = False
                _tool_failures_this_turn[0] = 0
                _tool_read_cache_this_turn.clear()
                _per_tool_call_counts.clear()
                _consecutive_dedup_counts.clear()
                _last_tool_call.clear()
                _last_tool_call.update({"name": None, "args": None, "result": None})
                _search_tools_exhausted[0] = False
                # Persist previous turn summary before resetting — cross-turn memory layer
                asyncio.create_task(finalize_and_persist(_active_turn_context[0], user_id, conversation_id))
                # ── P2b: Auto add_session if active plan was touched this turn ──
                _prev_plan_id = getattr(_turn_state, "active_plan_id", "") or ""
                _prev_tc = _active_turn_context[0]
                if _prev_plan_id and _prev_tc is not None:
                    asyncio.create_task(_auto_add_session_for_plan(
                        plan_id=_prev_plan_id,
                        tc=_prev_tc,
                        conversation_id=conversation_id,
                    ))
                _active_turn_context[0] = None  # will be created after decide_turn
                _active_voice_turn[0] = None  # force fresh runtime for this turn
                runtime = _ensure_voice_turn_runtime(_current_turn_id[0])
                try:
                    canonical = canonicalize_turn_text(text)
                    live_location = (canonical.location or "").strip()
                    reset_conversation_search_count(user_id)
                    _latest_user_event.clear()
                    _latest_user_event.update(canonical.to_dict())
                    set_progress_context(
                        on_hub_progress,
                        user_id,
                        {"location": live_location} if live_location else None,
                        conversation_id=conversation_id,
                    )
                    await runtime.heard_user(
                        raw_text=canonical.raw_text,
                        canonical_text=canonical.canonical_text,
                        location=canonical.location,
                        mode_policy=canonical.mode_policy,
                    )
                    semantic_started = time.monotonic()
                    semantic_resolution = await resolve_semantic_turn(
                        current_text=canonical.canonical_text or text,
                        recent_messages=_semantic_recent_messages,
                        model=LLM_MODEL,
                        ai_gateway_url=AI_GATEWAY_URL,
                        api_key=AI_GATEWAY_API_KEY,
                        timeout_secs=4.0,
                    )
                    logger.info(
                        f"NOVA_SEMANTIC_RESOLVER_TIMING | duration_ms={int((time.monotonic() - semantic_started) * 1000)} "
                        f"resolved={semantic_resolution is not None}"
                    )
                    llm_text = text
                    if not _turn_state.active_action_id:
                        active_action_context = await _active_action_binding_context_text(canonical.canonical_text or text)
                        if active_action_context:
                            llm_text = f"{text.rstrip()}{active_action_context}"
                            logger.info("NOVA_ACTIVE_ACTION_BINDING_CONTEXT_INJECTED | source=durable_ledger intent=tesla_navigation_plan")
                    plan = await decide_turn(text, _turn_state, semantic_resolution=semantic_resolution)
                    tool_budget = select_tool_budget(
                        llm_text,
                        ALL_TOOL_NAMES,
                        plan.intent.value,
                        learned_candidate=plan.learned_candidate,
                    )
                    # Initialize the reasoning scaffold for this turn — anchors goal across tool chains
                    _active_turn_context[0] = TurnContext(
                        turn_id=_current_turn_id[0],
                        user_text=canonical.canonical_text or text,
                        goal=derive_goal(canonical.canonical_text or text, getattr(plan, "goal", "") or "", plan.intent.value),
                        intent=plan.intent.value,
                        evidence_budget=derive_evidence_budget(_eb if isinstance(_eb := (getattr(plan, "evidence_budget", 0) or 0), int) else sum(_eb.values()) if isinstance(_eb, dict) else 0, plan.intent.value),
                    )
                    logger.info(
                        f"NOVA_TURN_CONTEXT_INIT | turn_id={_current_turn_id[0]} "
                        f"intent={plan.intent.value} goal={_active_turn_context[0].goal[:80]!r} "
                        f"evidence_budget={_active_turn_context[0].evidence_budget}"
                    )

                    # ── Session planner hook ─────────────────────────────────
                    # Best-effort, non-blocking. Creates a plan spine when the
                    # user's turn looks like multi-step work AND no plan is
                    # active yet. Emits plan_state to iOS so the planner panel
                    # populates immediately, before the LLM even starts.
                    async def _planner_hook(
                        _text=canonical.canonical_text or text,
                        _intent=plan.intent.value,
                        _goal=getattr(plan, "goal", "") or "",
                        _uid=user_id,
                        _conv=conversation_id,
                        _sid=session.session_id,
                    ):
                        try:
                            if _turn_state.active_plan_id:
                                # Plan already known this session — skip creation, just emit
                                from nova.task_plan import get_plan as _get_plan
                                existing = await _get_plan(_turn_state.active_plan_id)
                                if existing:
                                    await emit_plan_state(_send_server_msg, existing)
                                    return
                            active_plan = await ensure_active_plan_for_turn(
                                text=_text,
                                plan_intent=_intent,
                                plan_goal=_goal,
                                user_id=_uid,
                                conversation_id=_conv,
                                session_id=_sid,
                            )
                            if active_plan:
                                _turn_state.active_plan_id = active_plan.get("plan_id", "")
                                _turn_state.active_plan_topic = active_plan.get("topic", "")
                                _turn_state.active_plan_page_id = active_plan.get("workspace_page_id", "")
                                await emit_plan_state(_send_server_msg, active_plan)
                                # Patch the live system message with a concise plan anchor
                                # so M2.7 sees the active goal on every LLM pass this turn.
                                try:
                                    # P0a: Show FULL UUID — never truncate. M2.7 was
                                    # pattern-completing a hallucinated UUID when it saw
                                    # `plan_id: 04244a45-749c-47...` in the anchor.
                                    _ap_id = active_plan.get("plan_id", "")
                                    _ap_topic = active_plan.get("topic", "")
                                    _ap_pkey = active_plan.get("project_key", "") or ""
                                    _ap_page = active_plan.get("workspace_page_id", "") or ""
                                    _anchor_lines = [
                                        "",
                                        "",
                                        "[SESSION PLAN ACTIVE]",
                                        f"  topic: {_ap_topic}",
                                        f"  plan_id: {_ap_id}",
                                    ]
                                    if _ap_pkey:
                                        _anchor_lines.append(f"  project_key: {_ap_pkey}")
                                    if _ap_page:
                                        _anchor_lines.append(f"  workspace_page_id: {_ap_page}")
                                    _anchor_lines.extend([
                                        "  To reload full step history and session entries, "
                                        f"call: manage_task_plan(action='get', plan_id='{_ap_id}')",
                                        "  Do not abandon this goal. Do not create a new plan; "
                                        "this plan is already active for this work.",
                                    ])

                                    # Build the known-pages dictionary from two sources:
                                    # 1. DB: pages linked to this project via project_key
                                    # 2. Session: pages discovered/created this session
                                    try:
                                        _db_pages = await fetch_project_pages(
                                            user_id=_uid,
                                            plan_id=_ap_id,
                                            project_key=_ap_pkey or None,
                                        )
                                        # Merge DB pages into session state (deduplicate by page_id)
                                        _existing_ids = {
                                            p["page_id"]
                                            for p in _turn_state.known_workspace_pages
                                        }
                                        for _dbp in _db_pages:
                                            if _dbp["page_id"] not in _existing_ids:
                                                _turn_state.known_workspace_pages.append(_dbp)
                                                _existing_ids.add(_dbp["page_id"])
                                    except Exception as _fpe:
                                        logger.warning(f"NOVA_PLANNER_PAGE_FETCH | {_fpe}")

                                    # Render the page dictionary block
                                    if _turn_state.known_workspace_pages:
                                        _anchor_lines.append("")
                                        _anchor_lines.append("## Known Workspace Pages")
                                        _anchor_lines.append(
                                            "Use these real page_ids directly. "
                                            "If the page you need is not listed, call "
                                            "manage_workspace(action='search', query='...') first."
                                        )
                                        for _kp in _turn_state.known_workspace_pages[-15:]:
                                            _kp_title = (_kp.get("title") or "untitled")[:60]
                                            _anchor_lines.append(
                                                f"  - {_kp['page_id']}  \"{_kp_title}\""
                                            )

                                    _anchor_line = "\n".join(_anchor_lines)
                                    if context.messages and context.messages[0].get("role") == "system":
                                        existing = context.messages[0].get("content", "")
                                        if "[SESSION PLAN ACTIVE]" not in existing:
                                            context.messages[0]["content"] = existing + _anchor_line
                                        else:
                                            # Plan anchor already present — refresh only the pages block
                                            import re as _re_patch
                                            _pages_marker = "## Known Workspace Pages"
                                            if _turn_state.known_workspace_pages:
                                                _pages_block = "\n".join(
                                                    _anchor_lines[
                                                        next(
                                                            (i for i, l in enumerate(_anchor_lines) if _pages_marker in l),
                                                            len(_anchor_lines),
                                                        ):
                                                    ]
                                                )
                                                if _pages_marker in existing:
                                                    context.messages[0]["content"] = _re_patch.sub(
                                                        rf"{_re_patch.escape(_pages_marker)}.*",
                                                        _pages_block,
                                                        existing,
                                                        flags=_re_patch.DOTALL,
                                                    )
                                                else:
                                                    context.messages[0]["content"] = existing + "\n" + _pages_block
                                except Exception as _pe:
                                    logger.warning(f"NOVA_PLANNER_SYSTEM_PATCH_FAILED | {_pe}")
                        except Exception as _e:
                            logger.warning(f"NOVA_PLANNER_HOOK_FAILED | {_e}")
                    asyncio.create_task(_planner_hook())

                    context.set_tools(_build_tools_schema(tool_budget.names))

                    # ── M2.7 thinking level per intent ───────────────────────
                    # MoE models perform significantly better on multi-step
                    # planning/research when the reasoning budget is unlocked.
                    # low  → fast, for simple queries and casual conversation
                    # medium → balanced, for research, workspace, and planning
                    # (high is too slow for real-time voice — reserved for
                    #  background/async tasks only)
                    _MEDIUM_THINKING_INTENTS = {
                        "workspace_creation", "workspace_context_continuation",
                        "lookup_then_workspace_creation", "workspace_management",
                        "web_research_request", "email_lookup",
                        "calendar_lookup", "conversation_recall",
                        "tesla_navigation_plan", "active_action_status",
                    }
                    _intent_val = plan.intent.value
                    _thinking_level = (
                        "medium"
                        if _intent_val in _MEDIUM_THINKING_INTENTS
                        else "low"
                    )
                    # Also elevate to medium for pass_through when active plan
                    # exists — this is a planning/work session continuation
                    if _intent_val == "pass_through" and _turn_state.active_plan_id:
                        _thinking_level = "medium"
                    llm.set_thinking(_thinking_level)
                    logger.info(
                        f"NOVA_M2.7_THINKING | intent={_intent_val} "
                        f"thinking={_thinking_level} active_plan={bool(_turn_state.active_plan_id)}"
                    )

                    # ── P1a: Parallel tool-call instruction for fan-out work ──
                    # M2.7 supports parallel_tool_calls but defaults to serial
                    # narrative reasoning ("Now let me…"). Without this hint we
                    # observed 14 sequential calls where 8 could have been one
                    # batch. This unlock cuts LLM round-trips 3-5× on workspace
                    # building and multi-source research.
                    _PARALLEL_BATCH_INTENTS = {
                        "workspace_creation", "workspace_management",
                        "workspace_context_continuation",
                        "lookup_then_workspace_creation",
                        "web_research_request", "email_lookup",
                        "calendar_lookup", "conversation_recall",
                    }
                    _wants_parallel = (
                        _intent_val in _PARALLEL_BATCH_INTENTS
                        or (_intent_val == "pass_through" and _turn_state.active_plan_id)
                    )
                    if _wants_parallel and context.messages and context.messages[0].get("role") == "system":
                        _parallel_hint = (
                            "\n\n[PARALLEL TOOL CALLS]\n"
                            "When you need to perform multiple INDEPENDENT operations "
                            "(e.g. adding 5 blocks to the same page, fetching 3 different "
                            "pages, running 2 web searches with different queries, looking "
                            "up the same fact in CIG and PCG simultaneously), emit them ALL "
                            "in a single response as a parallel `tool_calls` array. Do NOT "
                            "narrate \"Now let me add the next block\" between each call \u2014 "
                            "that turns 8 parallelizable operations into 8 sequential round-trips. "
                            "Only chain serially when a later call genuinely needs a prior "
                            "call's result (e.g. needing a page_id from create before add_block)."
                        )
                        existing = context.messages[0].get("content", "")
                        if "[PARALLEL TOOL CALLS]" not in existing:
                            context.messages[0]["content"] = existing + _parallel_hint

                    await runtime.routed(plan.intent.value, len(tool_budget.names))
                    logger.info(
                        f"NOVA_TOOL_BUDGET | intent={plan.intent.value} reason={tool_budget.reason} "
                        f"selected_tools={len(tool_budget.names)} groups={','.join(tool_budget.groups)} "
                        f"nudge_level={tool_budget.nudge_level} activation={tool_budget.activation:.3f} "
                        f"confidence={tool_budget.confidence:.3f} learning_rate={tool_budget.learning_rate:.3f} "
                        f"candidate_id={tool_budget.candidate_id} optimizer={tool_budget.optimizer} "
                        f"names={','.join(tool_budget.names)}"
                    )
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
                            "tool_budget": {
                                "reason": tool_budget.reason,
                                "names": tool_budget.names,
                                "groups": tool_budget.groups,
                                "nudge_level": tool_budget.nudge_level,
                                "activation": tool_budget.activation,
                                "confidence": tool_budget.confidence,
                                "learning_rate": tool_budget.learning_rate,
                                "optimizer": tool_budget.optimizer,
                                "candidate_id": tool_budget.candidate_id,
                                "candidate_intent": tool_budget.candidate_intent,
                                "suggested_tools": tool_budget.suggested_tools,
                                "gradient_hint": tool_budget.gradient_hint,
                            },
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
                except Exception as e:
                    logger.exception(
                        f"NOVA_TURN_INGRESS_RECOVERED | turn_id={_current_turn_id[0]} frame={type(frame).__name__}"
                    )
                    await runtime.complete_with_error(
                        "I hit an internal turn-routing error before I could process that. Please try again."
                    )
                    return

                async def _persist(role: str, content: str):
                    await append_turn(session.session_id, role, content)
                    if role in {"user", "assistant"} and str(content or "").strip():
                        _semantic_recent_messages.append({"role": role, "content": str(content)})
                        del _semantic_recent_messages[:-MAX_HISTORY_TURNS]
                    asyncio.create_task(_sync_message_to_backend(
                        conversation_id, user_id, role, content,
                        model=LLM_MODEL if role == "assistant" else None,
                    ))

                lower_turn_text = (canonical.canonical_text or text or "").lower()
                if (
                    ("weather" in lower_turn_text or "temperature" in lower_turn_text or "outside" in lower_turn_text)
                    and not any(term in lower_turn_text for term in ("tomorrow", "week", "weekend", "forecast", "rain later"))
                ):
                    try:
                        await runtime.send_server_msg({"phase": "thinking"})
                        await runtime.send_server_msg({"type": "thinking", "text": "Checking the current outdoor weather..."})
                        weather_result = await dispatch_tool(
                            "get_weather",
                            {"location": live_location or "Humble, TX", "query": canonical.canonical_text or text},
                        )
                        weather_data = weather_result
                        if isinstance(weather_result, str):
                            try:
                                weather_data = json.loads(weather_result)
                            except json.JSONDecodeError:
                                weather_data = weather_result
                        if isinstance(weather_data, dict) and weather_data.get("display"):
                            await runtime.complete_with_text(
                                str(weather_data.get("display") or ""),
                                speech_text=str(weather_data.get("speech") or weather_data.get("speakable") or weather_data.get("display") or ""),
                                result="get_weather_fast_path",
                                suppress_speech=False,
                            )
                        else:
                            await runtime.complete_with_text(
                                str(weather_data or "I couldn't get the weather right now."),
                                result="get_weather_fast_path",
                                suppress_speech=False,
                            )
                        await _persist("user", canonical.canonical_text or text)
                        return
                    except Exception:
                        logger.exception(f"NOVA_WEATHER_FAST_PATH_FAILED | turn_id={_current_turn_id[0]}")

                try:
                    orchestrator_result = await execute_turn_plan_result(
                        plan,
                        _turn_state,
                        dispatch_tool,
                        _send_server_msg,
                        _persist,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                    )
                except Exception:
                    logger.exception(
                        f"NOVA_TURN_ORCHESTRATOR_EXECUTION_RECOVERED | "
                        f"turn_id={_current_turn_id[0]} intent={getattr(plan.intent, 'value', plan.intent)}"
                    )
                    await runtime.complete_with_error(
                        "I hit an internal routing error while processing that. Please try again."
                    )
                    return
                handled = orchestrator_result.handled
                if handled:
                    _orchestrator_consumed_turn_id[0] = _current_turn_id[0]
                    if _active_voice_turn[0] is not None:
                        await _active_voice_turn[0].emit_final_from_orchestrator(
                            display_text=orchestrator_result.display_text or orchestrator_result.response,
                            speech_text=orchestrator_result.speech_text or orchestrator_result.response,
                            result_label=orchestrator_result.result_label or "turn_orchestrator",
                            suppress_speech=False,
                            card=orchestrator_result.card,
                        )
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
                    await _record_learning_event(
                        event_type="candidate_applied",
                        source_layer="orchestrator",
                        canonical_text=canonical.canonical_text,
                        payload={
                            "candidate_id": plan.learned_candidate.get("id"),
                            "intent": plan.intent.value
                        }
                    )
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

                if llm_text != text:
                    if isinstance(frame, InputTransportMessageFrame):
                        if isinstance(frame.message, dict) and frame.message.get("type") == "send-text":
                            data = frame.message.get("data")
                            if isinstance(data, dict):
                                data["content"] = llm_text
                    elif isinstance(frame, TranscriptionFrame):
                        frame.text = llm_text

                if _active_voice_turn[0] is not None:
                    _active_voice_turn[0].start_watchdog()

            await self.push_frame(frame, direction)

    turn_orchestrator = TurnOrchestratorFrameProcessor()

    # ── Speculative Cache: pre-fetch layer only ────────────────────────
    # Warms the dispatch_tool cache so tool calls return instantly.
    # Does NOT intercept user frames — the LLM always sees every query.
    # This avoids stale-data-as-truth, enrichment hallucination, and
    # pattern misrouting that a hard-gate CacheResponseProcessor causes.
    from nova.speculative_cache import init_speculative_cache, get_speculative_cache
    _spec_cache = init_speculative_cache(
        tool_dispatcher=lambda tool_name, tool_args: dispatch_tool(tool_name, tool_args),
    )
    # Warm critical entries immediately (calendar, weather) in background
    async def _initial_cache_warm():
        try:
            await asyncio.sleep(3)  # Let session settle first
            result = await _spec_cache.warm_all()
            warmed = [k for k, v in result.items() if v]
            if warmed:
                logger.info(f"NOVA_SPEC_CACHE | initial warm succeeded: {warmed}")
        except Exception as e:
            logger.warning(f"NOVA_SPEC_CACHE | initial warm failed: {e}")
    asyncio.create_task(_initial_cache_warm())
    # Start periodic warming (every 10 min)
    _spec_cache.start_scheduled_warming(interval_seconds=600)
    logger.info("NOVA_SPEC_CACHE | initialized as pre-fetch layer (no hard gate)")

    # Build pipeline based on audio mode
    if use_server_audio:
        # Server-side STT/TTS mode using local Whisper + Qwen TTS
        vad_params = VADParams(
            confidence=NOVA_VAD_CONFIDENCE,
            start_secs=NOVA_VAD_START_SECS,
            stop_secs=NOVA_VAD_STOP_SECS,
            min_volume=NOVA_VAD_MIN_VOLUME,
        )
        logger.info(
            f"NOVA_VAD_CONFIG | confidence={NOVA_VAD_CONFIDENCE} "
            f"start_secs={NOVA_VAD_START_SECS} stop_secs={NOVA_VAD_STOP_SECS} "
            f"min_volume={NOVA_VAD_MIN_VOLUME}"
        )
        stt = WhisperSTTService(
            model="Systran/faster-whisper-medium",
            device="cuda",
            compute_type="float16",
        )
        _voice_pref_path = os.path.expanduser("~/.config/nova/voice-preference.json")
        try:
            import json as _pref_json
            _selected_voice = _pref_json.load(open(_voice_pref_path)).get("voice_id", "american_female_warm")
        except Exception:
            _selected_voice = "american_female_warm"
        logger.info(f"NOVA_TTS | using voice: {_selected_voice}")
        tts = QwenTTSService(
            voice=_selected_voice,
        )
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=vad_params),
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
                        if _is_interim_send_text_message(msg):
                            logger.info("NOVA_STT_INTERIM_IGNORED | transport=native_text_bridge")
                            return
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
    await _flush_server_msg_backlog()

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

        def prune_context(self):
            system = context.messages[:1]
            # Step 1 — latency-aware tool-result clearing on the live history
            # (the body that's about to ride along into the next user turn).
            # _trim_context_messages already strips tool_call/tool roles, but
            # when this is invoked mid-pipeline the in-flight tool_results are
            # still present. We stub the old ones here so the next turn's
            # prompt is small even if the previous turn had a 64K-char tool.
            live_body = list(context.messages[1:])
            compact_if_over_latency_threshold(
                live_body,
                threshold_tokens=LATENCY_THRESHOLD_TOKENS,
                path="voice_prune",
            )
            # Step 2 — sliding window + per-message char cap as before.
            recent = _trim_context_messages(live_body, MAX_HISTORY_TURNS)
            context.messages[:] = system + recent
            self._last_len = min(self._last_len, len(context.messages))
            _approx = _estimate_tokens(context.messages)
            logger.info(
                f"NOVA_PROMPT_BUDGET | pruned_live_context messages={len(context.messages)} "
                f"approx_tokens={_approx}"
            )
            check_overflow_risk(
                _approx, path="voice_live",
                message_count=len(context.messages),
                extra={"after_prune": True},
            )

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
                        _soft_synthesis_nudged_this_turn[0] = False
                        _tool_failures_this_turn[0] = 0
                        _tool_read_cache_this_turn.clear()
                        _per_tool_call_counts.clear()
                        _consecutive_dedup_counts.clear()
                        _last_tool_call.clear()
                        _last_tool_call.update({"name": None, "args": None, "result": None})
                        _search_tools_exhausted[0] = False
                        # Persist previous turn summary before resetting — cross-turn memory layer
                        asyncio.create_task(finalize_and_persist(_active_turn_context[0], user_id, conversation_id))
                        # ── P2b: Auto add_session if active plan was touched this turn ──
                        _prev_plan_id_ctx = getattr(_turn_state, "active_plan_id", "") or ""
                        _prev_tc_ctx = _active_turn_context[0]
                        if _prev_plan_id_ctx and _prev_tc_ctx is not None:
                            asyncio.create_task(_auto_add_session_for_plan(
                                plan_id=_prev_plan_id_ctx,
                                tc=_prev_tc_ctx,
                                conversation_id=conversation_id,
                            ))
                        _active_turn_context[0] = None
                        await append_turn(session.session_id, "user", content)
                        await _sync_message_to_backend(
                            conversation_id, user_id, "user", content
                        )
                        logger.debug(f"Persisted user turn ({len(content)} chars)")
                self._last_len = len(msgs)
                self.prune_context()

    ctx_watcher = _ContextWatcher()

    # Event bus: proactive notifications while user is connected
    event_handler = create_event_handler(task, user_id, _send_server_msg)

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
                await _send_server_msg({"type": "heartbeat", "text": ""})
                logger.debug("Sent keepalive heartbeat")
            except Exception as e:
                logger.warning(f"Keepalive heartbeat failed: {e}")
                break

    async def _warm_llm_kv_cache() -> None:
        """Fire a 1-token completion through the full system prompt + history to
        pre-build the llama-server KV cache.  With --cache-reuse 256 this drops
        first-turn TTFT from 30-120 s to 2-5 s."""
        try:
            import aiohttp
            warm_messages = list(messages) + [
                {"role": "user", "content": "[SYSTEM: KV cache warm — discard this]"}
            ]
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{AI_GATEWAY_URL}/chat/completions",
                    json={
                        "model": "minimax-m2.7",
                        "messages": warm_messages,
                        "max_tokens": 1,
                        "stream": False,
                        "temperature": 0,
                    },
                    headers={"Authorization": f"Bearer {AI_GATEWAY_API_KEY}"},
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    await resp.read()
            logger.info("NOVA_LLM_KV_WARM | KV cache pre-built for first user turn")
        except Exception as e:
            logger.warning(f"NOVA_LLM_KV_WARM | non-fatal warm-up failed: {e}")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected (user={user_id}), session={session.session_id}")
        mark_user_active(user_id)
        event_bus.subscribe_user(user_id, event_handler)
        set_progress_context(on_hub_progress, user_id, conversation_id=conversation_id)
        # Start keepalive to prevent iOS from closing idle connections
        _keepalive_task[0] = asyncio.create_task(_keepalive_loop())
        # Pre-warm the LLM KV cache so the first real user turn hits the cache
        # instead of processing ~30K tokens cold (30-120s TTFT → 2-5s).
        asyncio.create_task(_warm_llm_kv_cache())
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
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
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

    import os
    import shutil
    import uuid

    # Set up local image storage for vision
    IMAGE_STORE = os.path.join(os.path.dirname(__file__), "data", "images")
    os.makedirs(IMAGE_STORE, exist_ok=True)
    webrtc_app.mount("/api/vision/images", StaticFiles(directory=IMAGE_STORE), name="images")

    @webrtc_app.post("/api/vision/upload")
    async def upload_vision_image(file: UploadFile = File(...)):
        """Handle image uploads from iOS client for Qwen Vision analysis."""
        try:
            # Generate unique filename with original extension
            ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
            file_id = str(uuid.uuid4())
            filename = f"{file_id}{ext}"
            file_path = os.path.join(IMAGE_STORE, filename)
            
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            logger.info(f"Saved vision image: {filename}")
            
            # The URL iOS will send to Nova via hidden system prompt
            # Must be accessible via the AI gateway/Nova server URL
            webrtc_port = os.environ.get("NOVA_PORT", "18800")
            serving_url = f"http://100.108.41.22:{webrtc_port}/api/vision/images/{filename}"
            
            return {
                "success": True,
                "url": serving_url,
                "imageUrl": serving_url,
                "id": file_id,
                "file": {
                    "id": file_id,
                    "url": serving_url,
                    "fileName": filename,
                }
            }
        except Exception as e:
            logger.error(f"Failed to upload image: {e}")
            raise HTTPException(status_code=500, detail=str(e))

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

    @webrtc_app.post("/disconnect")
    async def disconnect_session(request: Request):
        """
        iOS explicit disconnect endpoint.
        Body: {"pc_id": "SmallWebRTCConnection#N-..."}
        Closes the peer connection server-side so the iOS doesn't loop on 404.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        pc_id = body.get("pc_id", "")
        if pc_id and pc_id in request_handler._pcs_map:
            conn = request_handler._pcs_map.get(pc_id)
            if conn:
                try:
                    await conn.disconnect()
                except Exception as e:
                    logger.warning(f"Disconnect cleanup error for {pc_id}: {e}")
            request_handler._pcs_map.pop(pc_id, None)
            logger.info(f"Disconnected session: {pc_id}")
            return {"status": "ok", "pc_id": pc_id}
        # Already gone — still return 200 so iOS doesn't retry
        logger.info(f"Disconnect requested for unknown/expired pc_id: {pc_id!r}")
        return {"status": "ok", "pc_id": pc_id, "note": "session already closed"}

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
