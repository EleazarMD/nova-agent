"""
Hypothesis Interceptor - Zero-Wait Protocol Implementation

Intercepts user text frames BEFORE they reach the main LLM and generates
fast hypothesis responses for immediate speech (<500ms target).
"""

import asyncio
from typing import Optional
from loguru import logger

from pipecat.frames.frames import Frame, TextFrame, LLMMessagesFrame, InputTransportMessageFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class HypothesisInterceptor(FrameProcessor):
    """
    Intercepts user text frames to generate fast hypothesis responses.
    
    This processor sits BEFORE the LLM in the pipeline and:
    1. Detects user text frames
    2. Generates hypothesis immediately (target <500ms)
    3. Sends hypothesis message to iOS for instant speech
    4. Allows frame to continue to main LLM for full processing
    """
    
    def __init__(
        self,
        hypothesis_validator,
        server_msg_fn,
        ai_gateway_url: str,
        api_key: str,
    ):
        super().__init__()
        self._hypothesis_validator = hypothesis_validator
        self._server_msg_fn = server_msg_fn
        self._ai_gateway_url = ai_gateway_url
        self._api_key = api_key
        self._processing_hypothesis = False
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        
        # Intercept user text input BEFORE aggregation
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputTransportMessageFrame):
            message = frame.message
            if isinstance(message, dict) and message.get("type") == "send-text":
                # iOS native mode sends: {"type": "send-text", "data": {"content": "..."}}
                data = message.get("data", {})
                user_query = data.get("content", "") if isinstance(data, dict) else ""
                
                if user_query and not self._processing_hypothesis:
                    logger.info(f"[HypothesisInterceptor] User query detected: {user_query[:60]}...")
                    
                    # Generate hypothesis with empty history for speed
                    asyncio.create_task(self._generate_and_send_hypothesis(
                        user_query,
                        []  # Empty history for speed - hypothesis doesn't need full context
                    ))
        
        # Always pass frame through to continue pipeline
        await self.push_frame(frame, direction)
    
    async def _generate_and_send_hypothesis(self, user_query: str, history: list):
        """Generate hypothesis and send to iOS immediately."""
        from nova.hypothesis_generator import should_use_hypothesis_mode, generate_hypothesis
        
        # Prevent concurrent hypothesis generation
        if self._processing_hypothesis:
            return
        
        self._processing_hypothesis = True
        
        try:
            # Strip location prefix if present
            clean_query = user_query
            if user_query.startswith("[User location:"):
                parts = user_query.split("\n\n", 1)
                if len(parts) > 1:
                    clean_query = parts[1]
            
            # Check if hypothesis mode should be used
            if not should_use_hypothesis_mode(clean_query):
                logger.debug(f"[HypothesisInterceptor] Skipping for: {clean_query[:60]}")
                return
            
            # Generate hypothesis (target <500ms) using clean query
            start_time = asyncio.get_event_loop().time()
            
            hypothesis_text, confidence, tools = await generate_hypothesis(
                clean_query,  # Use clean query without location prefix
                history,
                self._ai_gateway_url,
                self._api_key,
            )
            
            elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            
            # Only send if we have a real hypothesis (not empty fallback)
            if hypothesis_text and confidence > 0:
                # Start hypothesis validation session
                await self._hypothesis_validator.start_hypothesis(
                    text=hypothesis_text,
                    confidence=confidence,
                    tools=tools,
                )
                
                logger.info(
                    f"[HypothesisInterceptor] Generated in {elapsed_ms:.0f}ms: "
                    f"'{hypothesis_text[:60]}...' (conf={confidence}, tools={tools})"
                )
            else:
                logger.debug(
                    f"[HypothesisInterceptor] No hypothesis generated ({elapsed_ms:.0f}ms)"
                )
                
        except Exception as e:
            logger.error(f"[HypothesisInterceptor] Failed: {e}")
        finally:
            self._processing_hypothesis = False
