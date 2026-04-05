"""
Nova Agent - Multi-Channel Output System

Leverages Pipecat's spoke architecture for separate streaming pipelines:
- Speech Spoke: Natural language for TTS (markdown stripped, abbreviations expanded)
- Display Spoke: Rich formatted text for UI cards (markdown, citations)
- Metadata Spoke: Sources, confidence, domain for thinking cards

This replaces the manual dual-field approach (text + speechText in same message).
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from pipecat.frames.frames import (
    Frame,
    TextFrame,
    LLMFullResponseEndFrame,
    InputTransportMessageFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from loguru import logger


class OutputChannel(str, Enum):
    """Output channels for multi-channel streaming."""
    SPEECH = "speech"      # Natural language for TTS
    DISPLAY = "display"    # Rich formatted text for UI
    METADATA = "metadata"  # Sources, confidence, citations


@dataclass
class MultiChannelFrame(Frame):
    """Frame that can route to multiple output channels."""
    text: str = ""
    channels: list[OutputChannel] = field(default_factory=lambda: [OutputChannel.DISPLAY])
    metadata: Optional[dict] = None  # sources, confidence, domain, etc.


@dataclass
class SpeechFrame(Frame):
    """Frame for speech/TTS output (natural language)."""
    text: str = ""
    confidence: float = 1.0
    source: str = "llm"  # cache, retrieval, llm


@dataclass
class DisplayFrame(Frame):
    """Frame for display/UI output (rich text with markdown)."""
    text: str = ""
    sources: list[dict] = field(default_factory=list)
    domain: str = "general"


@dataclass
class MetadataFrame(Frame):
    """Frame for metadata (thinking card updates)."""
    phase: str = "thinking"  # thinking, retrieving, validating, done
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0
    domain: str = "general"


class MultiChannelRouter(FrameProcessor):
    """
    Routes LLM output to multiple channels.
    
    Placed AFTER the LLM in the pipeline. Takes TextFrame and:
    1. Emits to SPEECH spoke: strip markdown, natural speech
    2. Emits to DISPLAY spoke: raw text with markdown
    3. Optionally emits to METADATA spoke: sources, confidence
    
    For Ground-Truth Architecture:
    - Cache hit: SPEECH + DISPLAY fire immediately (<100ms)
    - Retrieval: METADATA fires during fetch, then SPEECH + DISPLAY on complete
    """
    
    def __init__(
        self,
        speech_transform_fn=None,  # Function to convert text to speech
        server_msg_fn=None,         # For sending to iOS client
    ):
        super().__init__()
        self._speech_transform = speech_transform_fn or self._default_speech_transform
        self._server_msg = server_msg_fn
        self._buffer: list[str] = []
    
    def _default_speech_transform(self, text: str) -> str:
        """Default: strip markdown for natural speech."""
        from nova.text_utils import strip_markdown_for_speech
        return strip_markdown_for_speech(text)
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        if isinstance(frame, TextFrame):
            self._buffer.append(frame.text)
        
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Combine buffered text
            full_text = "".join(self._buffer)
            self._buffer.clear()
            
            if not full_text.strip():
                await self.push_frame(frame, direction)
                return
            
            # Transform for speech (natural language)
            speech_text = self._speech_transform(full_text)
            
            # Emit to SPEECH spoke
            speech_frame = SpeechFrame(text=speech_text, source="llm")
            await self.push_frame(speech_frame, direction)
            
            # Emit to DISPLAY spoke
            display_frame = DisplayFrame(text=full_text)
            await self.push_frame(display_frame, direction)
            
            # Send to iOS client (combines both in one message for compatibility)
            # Per Zero-Wait Protocol: use "validated" message type
            if self._server_msg:
                await self._server_msg({
                    "type": "validated",
                    "text": full_text,
                    "speechText": speech_text,
                    "result": "direct",  # No hypothesis - direct LLM response
                    "source": "llm",
                    "suppressSpeech": False,  # iOS should speak this
                    "timestamp": None,  # Will be set by transport
                })
        
        else:
            await self.push_frame(frame, direction)


class CacheResponseProcessor(FrameProcessor):
    """
    Handles cache-hit responses for Zero-Wait Ground-Truth.
    
    When speculative cache hits, this processor:
    1. Sends immediate SPEECH frame (no LLM round-trip)
    2. Sends DISPLAY frame with cache source indicator
    3. Sends METADATA frame with confidence and citations
    
    This enables <100ms response time for cached queries.
    """
    
    def __init__(
        self,
        cache,
        server_msg_fn=None,
    ):
        super().__init__()
        self._cache = cache
        self._server_msg = server_msg_fn
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        if isinstance(frame, InputTransportMessageFrame):
            # Check cache BEFORE LLM processing
            user_text = frame.message.get("text", "") if isinstance(frame.message, dict) else str(frame.message)
            
            cache_result = await self._cache.lookup(user_text)
            if cache_result:
                logger.info(f"[Cache] HIT: {cache_result.cache_key}")
                
                # Emit immediate validated response per Zero-Wait Protocol
                if self._server_msg:
                    await self._server_msg({
                        "type": "validated",
                        "text": cache_result.display_text,
                        "speechText": cache_result.speech_text,
                        "domain": cache_result.domain,
                        "result": "direct",  # Cache hit = instant direct response
                        "source": "cache",
                        "cacheKey": cache_result.cache_key,
                        "confidence": cache_result.confidence,
                        "citations": cache_result.citations,
                        "suppressSpeech": False,  # iOS should speak cached response
                        "timestamp": None,
                    })
                
                # Don't push to LLM - response already sent
                return
        
        await self.push_frame(frame, direction)


class GroundedResponseProcessor(FrameProcessor):
    """
    Processes LLM response for grounding layer.
    
    Takes raw LLM output and:
    1. Verifies facts against retrieved data
    2. Attaches citations/sources
    3. Calculates confidence score
    4. Organizes into natural speech
    
    This is Layer 3 of the Ground-Truth Architecture.
    """
    
    def __init__(
        self,
        grounding_service=None,
        server_msg_fn=None,
    ):
        super().__init__()
        self._grounding = grounding_service
        self._server_msg = server_msg_fn
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        if isinstance(frame, LLMFullResponseEndFrame):
            full_text = "".join(self._buffer) if hasattr(self, '_buffer') else ""
            self._buffer.clear()
            
            if not full_text.strip():
                await self.push_frame(frame, direction)
                return
            
            # Ground the response
            if self._grounding:
                grounded = await self._grounding.ground(full_text)
                
                # Send grounded response per Zero-Wait Protocol
                if self._server_msg:
                    await self._server_msg({
                        "type": "validated",
                        "text": grounded.display_text,
                        "speechText": grounded.speech_text,
                        "domain": grounded.domain,
                        "result": "direct",  # Grounded direct response
                        "source": "retrieval",
                        "confidence": grounded.confidence,
                        "citations": grounded.citations,
                        "delta": grounded.delta,
                        "suppressSpeech": False,
                        "timestamp": None,
                    })
            else:
                # No grounding service - just send as-is
                await self.push_frame(frame, direction)
        
        elif isinstance(frame, TextFrame):
            if not hasattr(self, '_buffer'):
                self._buffer = []
            self._buffer.append(frame.text)
        
        else:
            await self.push_frame(frame, direction)


# Backwards compatibility: helper to convert old-style message to new channels
def convert_to_channels(msg: dict) -> list[Frame]:
    """Convert old-style {text, speechText} message to channel frames."""
    frames = []
    
    if "speechText" in msg:
        frames.append(SpeechFrame(
            text=msg["speechText"],
            confidence=msg.get("confidence", 1.0),
            source=msg.get("source", "llm"),
        ))
    
    if "text" in msg:
        frames.append(DisplayFrame(
            text=msg["text"],
            sources=msg.get("citations", []),
            domain=msg.get("domain", "general"),
        ))
    
    if "phase" in msg or "sources" in msg:
        frames.append(MetadataFrame(
            phase=msg.get("phase", "done"),
            sources=msg.get("sources", []),
            confidence=msg.get("confidence", 1.0),
            domain=msg.get("domain", "general"),
        ))
    
    return frames
