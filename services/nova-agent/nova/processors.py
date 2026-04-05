"""
Nova Agent pipeline processors.

Frame processors that sit in the Pipecat pipeline to handle:
- Conversation persistence (user + assistant turn DB writes)
- Real-time reasoning/thinking events streamed to frontend
- Native text bridge (logging for iOS STT/TTS mode)

Server message types emitted for frontend transparency:
  {phase: "thinking"}       — LLM response started, waiting for first token
  {phase: "responding"}     — first text token received, LLM is streaming
  {phase: "done"}           — LLM response complete
  {type: "thinkingUpdate", text: "..."}  — streaming LLM text for ThinkingCard
"""

import asyncio
import time
from typing import Callable, Optional

from loguru import logger

from pipecat.frames.frames import (
    InputTransportMessageFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from nova.store import append_turn, _sync_message_to_backend
from nova.tools import reset_conversation_search_count


class ConversationPersistence(FrameProcessor):
    """Intercepts LLM frames to persist turns and stream reasoning events.
    
    Placed after the LLM in the pipeline to capture:
    - LLMFullResponseStartFrame: sends {phase: "thinking"} to frontend
    - LLMTextFrame: buffers text, streams {type: "thinkingUpdate"} to frontend
    - LLMFullResponseEndFrame: persists turn, sends {phase: "done"}
    - LLMRunFrame: triggers user turn persistence
    
    Persists to both SQLite (local fast access) and PostgreSQL (source of truth).
    
    Args:
        session_id: DB session ID for turn persistence.
        conversation_id: Backend conversation ID for PostgreSQL sync.
        user_id: User ID for backend sync.
        context_ref: LLMContext reference for watching new user messages.
        ack_flag: Mutable [bool] — per-turn spoken-ack dedup flag.
        tool_counter: Mutable [int] — per-turn tool call counter.
        server_msg_fn: Async callback to send RTVI server messages to frontend.
        hypothesis_validator: Optional HypothesisValidator for validation completion.
    """
    def __init__(
        self,
        session_id: str,
        context_ref,
        ack_flag,
        tool_counter,
        server_msg_fn: Optional[Callable] = None,
        conversation_id: str = "default",
        user_id: str = "default",
        hypothesis_validator = None,
    ):
        super().__init__()
        self._session_id = session_id
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._context = context_ref
        self._ack_flag = ack_flag
        self._tool_counter = tool_counter
        self._server_msg_fn = server_msg_fn
        self._hypothesis_validator = hypothesis_validator
        self._assistant_buffer: list[str] = []
        self._last_context_len = len(context_ref.messages)
        self._first_text_sent = False
        self._response_start_time: float = 0.0
        self._text_chunk_count: int = 0
        # Throttle thinkingUpdate to avoid flooding the data channel
        self._last_thinking_send: float = 0.0
        self._thinking_send_interval: float = 0.15  # seconds between sends
        self._pending_thinking_text: str = ""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # LLM started generating — send "thinking" phase
            self._first_text_sent = False
            self._response_start_time = time.monotonic()
            self._text_chunk_count = 0
            self._pending_thinking_text = ""
            await self._persist_new_user_turns()
            await self._send_msg({"phase": "thinking"})

        elif isinstance(frame, LLMTextFrame):
            self._assistant_buffer.append(frame.text)
            self._text_chunk_count += 1

            # First text chunk: transition to "responding" phase
            if not self._first_text_sent:
                self._first_text_sent = True
                ttfb = time.monotonic() - self._response_start_time
                await self._send_msg({"phase": "responding"})
                logger.debug(f"LLM first token in {ttfb:.2f}s")

            # Per Zero-Wait Protocol: thinkingUpdate is for internal reasoning only
            # Final response text goes ONLY in validated message, not streamed here

        elif isinstance(frame, LLMFullResponseEndFrame):
            # Per Zero-Wait Protocol: response text goes in validated only, not thinkingUpdate
            
            # Persist complete assistant turn
            full_text = ""
            if self._assistant_buffer:
                full_text = "".join(self._assistant_buffer)
                self._assistant_buffer.clear()
                if full_text.strip():
                    # SQLite (local fast access)
                    await append_turn(self._session_id, "assistant", full_text)
                    # PostgreSQL (source of truth) - fire and forget
                    asyncio.create_task(_sync_message_to_backend(
                        self._conversation_id, self._user_id, "assistant", full_text
                    ))
                    # Mirror: final assistant text for Tesla companion
                    await self._mirror_event("assistant_text", {"text": full_text, "isFinal": True})
                    elapsed = time.monotonic() - self._response_start_time
                    logger.debug(
                        f"Persisted assistant turn "
                        f"({len(full_text)} chars, {self._text_chunk_count} chunks, "
                        f"{elapsed:.1f}s)"
                    )

            # Finalize hypothesis validation if active
            if self._hypothesis_validator and self._hypothesis_validator.active:
                from nova.hypothesis import ValidationResult
                session = self._hypothesis_validator.current_session
                
                # Determine validation result by comparing hypothesis to final response
                if session and full_text.strip():
                    hypothesis_text = session.hypothesis_text.lower()
                    response_text = full_text.lower()
                    
                    # Simple heuristic: if response is significantly different, it's corrected
                    # If it adds detail, it's enriched. Otherwise, confirmed.
                    if len(response_text) > len(hypothesis_text) * 1.5:
                        result = ValidationResult.ENRICHED
                    elif hypothesis_text not in response_text and response_text not in hypothesis_text:
                        result = ValidationResult.CORRECTED
                    else:
                        result = ValidationResult.CONFIRMED
                    
                    # Send validated message with full text per Zero-Wait Protocol
                    # Determine if iOS should speak based on validation result
                    # - confirmed/enriched: suppress (hypothesis was already spoken)
                    # - corrected: speak (hypothesis was wrong, need to correct)
                    # - BUT only suppress if hypothesis actually had text and was spoken!
                    hypothesis_was_spoken = session.hypothesis_text.strip() and session.confidence > 0
                    should_suppress = (
                        result in (ValidationResult.CONFIRMED, ValidationResult.ENRICHED) 
                        and hypothesis_was_spoken
                    )
                    
                    await self._hypothesis_validator.validate(
                        validated_text=full_text,  # Full response for iOS response card
                        result=result,
                        suppress_speech=should_suppress,
                    )
                    logger.info(f"[Hypothesis] Validation completed: {result.value} ({len(full_text)} chars, suppress={should_suppress})")
                else:
                    # No session or empty text - still need to complete the turn
                    logger.warning(f"[Hypothesis] Validation incomplete - no session or empty text, completing turn anyway")
                    await self._send_msg({"type": "turn_complete"})
                    await self._send_msg({"phase": "done"})
            else:
                # No hypothesis mode - send validated event with full text for iOS
                # Per Zero-Wait Protocol: validated must include text field
                # ONLY send when there's actual response text (not during tool-call phase)
                if full_text.strip():
                    # Strip markdown for natural speech while keeping formatted text for display
                    from nova.text_utils import strip_markdown_for_speech
                    speech_text = strip_markdown_for_speech(full_text)
                    
                    await self._send_msg({
                        "type": "validated",
                        "text": full_text,  # Formatted text for iOS display
                        "speechText": speech_text,  # Clean text for TTS (no markdown)
                        "result": "direct",  # No hypothesis was made
                        "suppressSpeech": False,  # iOS should speak this (not streamed earlier)
                    })
                    logger.debug(f"[Response] Sent validated event (direct mode, {len(full_text)} chars, speech={len(speech_text)} chars)")

            # Signal turn complete (Zero-Wait Protocol)
            # This marks the end of the turn, but only send validated completion if we had text
            await self._send_msg({"type": "turn_complete"})
            
            # Send phase: done for backwards compatibility when we had actual text
            if full_text.strip():
                await self._send_msg({"phase": "done"})

        elif isinstance(frame, LLMRunFrame):
            await self._persist_new_user_turns()

        await self.push_frame(frame, direction)

    async def _send_msg(self, msg: dict):
        """Send a server message to the frontend if callback is available."""
        if self._server_msg_fn:
            try:
                await self._server_msg_fn(msg)
            except Exception as e:
                logger.warning(f"Failed to send server msg: {e}")

    async def _mirror_event(self, event_type: str, data: dict):
        """Publish an event to the Tesla companion mirror (non-blocking)."""
        try:
            from nova.mirror import publish_event
            logger.info(f"[Mirror] Sending {event_type} for user={self._user_id}: {str(data)[:60]}")
            await publish_event(self._user_id, event_type, data)
        except Exception as e:
            logger.warning(f"[Mirror] Failed to publish {event_type}: {e}")
    
    async def _generate_hypothesis_for_query(self, user_query: str, messages: list):
        """Generate hypothesis for qualifying user queries."""
        from nova.hypothesis_generator import should_use_hypothesis_mode, generate_hypothesis
        import os
        
        # Check if hypothesis mode should be used
        if not should_use_hypothesis_mode(user_query):
            logger.debug(f"[Hypothesis] Skipping for: {user_query[:60]}")
            return
        
        try:
            # Get conversation history (exclude current message)
            history = [msg for msg in messages[:-1] if msg.get("role") in ("user", "assistant")]
            
            # Generate hypothesis
            ai_gateway_url = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
            api_key = os.environ.get("AI_GATEWAY_API_KEY", "ai-gateway-api-key-2024")
            
            hypothesis_text, confidence, tools = await generate_hypothesis(
                user_query,
                history,
                ai_gateway_url,
                api_key,
            )
            
            # Start hypothesis validation session
            await self._hypothesis_validator.start_hypothesis(
                text=hypothesis_text,
                confidence=confidence,
                tools=tools,
            )
            
            logger.info(f"[Hypothesis] Started session with {len(tools)} tools")
            
        except Exception as e:
            logger.error(f"[Hypothesis] Generation failed: {e}")

    async def _persist_new_user_turns(self):
        msgs = self._context.messages
        if len(msgs) > self._last_context_len:
            for msg in msgs[self._last_context_len:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    self._ack_flag[0] = False
                    self._tool_counter[0] = 0
                    # Reset per-turn search counter
                    reset_conversation_search_count(self._user_id)
                    
                    # Hypothesis generation now handled by HypothesisInterceptor
                    # before the LLM pipeline (for Zero-Wait Protocol)
                    
                    # SQLite (local fast access)
                    await append_turn(self._session_id, "user", content)
                    # PostgreSQL (source of truth) - fire and forget
                    asyncio.create_task(_sync_message_to_backend(
                        self._conversation_id, self._user_id, "user", content
                    ))
                    # Mirror: user transcript for Tesla companion (strip location prefix)
                    clean_text = content
                    if content.startswith("[User location:"):
                        # Remove location prefix: "[User location: ...]\n\nActual text"
                        parts = content.split("\n\n", 1)
                        if len(parts) > 1:
                            clean_text = parts[1]
                    await self._mirror_event("user_transcript", {"text": clean_text, "isFinal": True})
                    logger.debug(f"Persisted user turn ({len(content)} chars)")
            self._last_context_len = len(msgs)


class NativeTextBridge(FrameProcessor):
    """Bridge for native STT/TTS mode that forwards events to Tesla mirror.
    
    Pipecat's RTVI processor already handles 'send-text' messages
    (user→LLM) and emits 'bot-llm-text' events (LLM→user) natively.
    This processor intercepts speaking state and forwards to mirror.
    """
    
    def __init__(self, user_id: str = "default"):
        super().__init__()
        self._user_id = user_id
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        # Aggressive filter: Check ALL frame types for conversation_update content
        frame_type = type(frame).__name__
        
        # Filter 1: Raw transport messages
        if isinstance(frame, InputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict):
                msg_type = msg.get("type")
                
                # Filter conversation_update type messages
                if msg_type == "conversation_update":
                    logger.warning(f"[AGGRESSIVE FILTER] Dropping conversation_update InputTransportMessageFrame")
                    return
                
                # Filter send-text containing conversation_update
                if msg_type == "send-text":
                    data = msg.get("data", {})
                    text = data.get("content", "") if isinstance(data, dict) else ""
                    if text and ("conversation_update" in text or '"type":"conversation_update"' in text):
                        logger.warning(f"[AGGRESSIVE FILTER] Dropping send-text with conversation_update content")
                        return
                    if text:
                        logger.info(f"Native STT → LLM (RTVI): {text[:80]}")
                
                elif msg_type == "client-speaking-state":
                    data = msg.get("data", {})
                    who = data.get("who")
                    active = data.get("active")
                    logger.info(f"Speaking state: who={who}, active={active}")
                    try:
                        from nova.mirror import publish_event
                        await publish_event(self._user_id, "speaking_state", {
                            "who": who,
                            "active": active,
                        })
                    except Exception as e:
                        logger.debug(f"Mirror publish failed: {e}")
            elif isinstance(msg, str):
                # Handle string messages too
                if "conversation_update" in msg:
                    logger.warning(f"[AGGRESSIVE FILTER] Dropping string message with conversation_update")
                    return
        
        # Filter 2: Check any frame with 'text' or 'content' attribute for conversation_update
        # This catches frames that Pipecat converts from transport messages
        text_content = None
        if hasattr(frame, 'text'):
            text_content = frame.text
        elif hasattr(frame, 'content'):
            text_content = frame.content
        
        if text_content and isinstance(text_content, str):
            if "conversation_update" in text_content or '"type":"conversation_update"' in text_content:
                logger.warning(f"[AGGRESSIVE FILTER] Dropping {frame_type} with conversation_update content: {text_content[:60]}...")
                return
        
        await self.push_frame(frame, direction)
