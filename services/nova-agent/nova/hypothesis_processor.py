"""
Hypothesis Processor for Nova Agent.

Intercepts user messages to generate fast hypothesis responses before LLM execution.
"""

import asyncio
from typing import Callable, Optional
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMMessagesFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from nova.hypothesis_generator import generate_hypothesis, should_use_hypothesis_mode


class HypothesisPreprocessor(FrameProcessor):
    """
    Preprocessor that generates hypothesis responses before LLM execution.
    
    Intercepts LLMMessagesFrame to:
    1. Extract user query
    2. Determine if hypothesis mode should be used
    3. Generate fast hypothesis using Minimax M2.5
    4. Start hypothesis validation session
    5. Pass frame to LLM for full response
    
    Args:
        hypothesis_validator: HypothesisValidator instance
        ai_gateway_url: AI Gateway URL for hypothesis generation
        api_key: AI Gateway API key
    """
    
    def __init__(
        self,
        hypothesis_validator,
        ai_gateway_url: str,
        api_key: str,
    ):
        super().__init__()
        self._validator = hypothesis_validator
        self._ai_gateway_url = ai_gateway_url
        self._api_key = api_key
        self._processing_hypothesis = False
    
    async def process_frame(self, frame: Frame, direction):
        """Intercept LLMMessagesFrame to generate hypothesis."""
        await super().process_frame(frame, direction)
        
        # Only process LLMMessagesFrame (user message to LLM)
        if not isinstance(frame, LLMMessagesFrame):
            await self.push_frame(frame, direction)
            return
        
        # Avoid re-processing during hypothesis generation
        if self._processing_hypothesis:
            await self.push_frame(frame, direction)
            return
        
        # Extract user query from messages
        messages = frame.messages
        if not messages:
            await self.push_frame(frame, direction)
            return
        
        # Get last user message
        user_message = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_message = msg.get("content", "")
                break
        
        if not user_message:
            await self.push_frame(frame, direction)
            return
        
        # Check if hypothesis mode should be used
        if not should_use_hypothesis_mode(user_message):
            logger.debug(f"[Hypothesis] Skipping hypothesis mode for: {user_message[:60]}")
            await self.push_frame(frame, direction)
            return
        
        # Generate hypothesis
        try:
            self._processing_hypothesis = True
            logger.info(f"[Hypothesis] Generating for: {user_message[:60]}")
            
            # Get conversation history (last few messages for context)
            history = [msg for msg in messages[:-1] if msg.get("role") in ("user", "assistant")]
            
            hypothesis_text, confidence, tools = await generate_hypothesis(
                user_message,
                history,
                self._ai_gateway_url,
                self._api_key,
            )
            
            # Start hypothesis validation session
            await self._validator.start_hypothesis(
                text=hypothesis_text,
                confidence=confidence,
                tools=tools,
            )
            
            logger.info(f"[Hypothesis] Started validation session with {len(tools)} tools")
            
        except Exception as e:
            logger.error(f"[Hypothesis] Generation failed: {e}")
        finally:
            self._processing_hypothesis = False
        
        # Pass frame to LLM for full response
        await self.push_frame(frame, direction)
