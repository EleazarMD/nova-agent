"""
ML Query Logger Processor - captures user queries for ML training.

Sits in the Pipecat pipeline after user aggregator to log every query
with 100+ contextual features for predictive cache training.
"""

import time
from typing import Optional, Callable
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import LLMRunFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameProcessor

from nova.ml.simple_logger import SimpleQueryLogger


class MLQueryLoggerProcessor(FrameProcessor):
    """
    Captures user queries and logs them with contextual features for ML training.
    
    Placed after user_aggregator in pipeline to intercept:
    - LLMRunFrame: User query captured, log with features
    - LLMFullResponseEndFrame: Response complete, update outcome metrics
    """
    
    def __init__(
        self,
        ml_logger: SimpleQueryLogger,
        user_id: str,
        session_id: UUID,
        context_ref,
        device_type: str = "iphone",
        location_ref: Optional[dict] = None,
        context_fn: Optional[Callable] = None,
    ):
        super().__init__()
        self.ml_logger = ml_logger
        self.user_id = user_id
        self.session_id = session_id
        self.context_ref = context_ref
        self.device_type = device_type
        self.location_ref = location_ref or {}
        self.context_fn = context_fn
        
        # Track current query
        self.current_record_id: Optional[UUID] = None
        self.current_query_text: Optional[str] = None
        self.current_query_type: Optional[str] = None
        self.query_start_time: Optional[float] = None
        self.conversation_turn: int = 0
    
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        
        # Capture user query when LLM is about to run
        if isinstance(frame, LLMRunFrame):
            await self._log_user_query()
        
        # Update outcome when response is complete
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._update_outcome()
        
        await self.push_frame(frame, direction)
    
    async def _log_user_query(self):
        """Log user query with extracted features."""
        try:
            # Get last user message from context
            messages = self.context_ref.messages
            user_messages = [m for m in messages if m.get("role") == "user"]
            if not user_messages:
                return
            
            last_user_msg = user_messages[-1]
            query_text = last_user_msg.get("content", "")
            if not query_text or len(query_text) < 3:
                return
            
            self.conversation_turn += 1
            self.current_query_text = query_text
            self.query_start_time = time.time()
            
            # Classify query type (simple heuristic for now)
            query_type = self._classify_query(query_text)
            self.current_query_type = query_type
            
            # Extract context if callback provided
            context = {}
            if self.context_fn:
                try:
                    context = await self.context_fn()
                except Exception as e:
                    logger.debug(f"[ML Logger] Context extraction failed: {e}")
            
            # Get location from ref (updated by RTVI location messages)
            location = self.location_ref.copy() if self.location_ref else None
            
            # Log query with features
            self.current_record_id = await self.ml_logger.log_query(
                user_id=self.user_id,
                query_text=query_text,
                query_type=query_type,
                session_id=self.session_id,
                conversation_turn=self.conversation_turn,
                location=location,
                device_type=self.device_type,
                context=context,
                response_text=None,  # Will be updated later
            )
            
            if self.current_record_id:
                logger.debug(f"[ML Logger] Logged query #{self.conversation_turn}: {query_type} (id={self.current_record_id})")
        
        except Exception as e:
            logger.error(f"[ML Logger] Failed to log query: {e}")
    
    async def _update_outcome(self):
        """Update outcome metrics after response is delivered."""
        if not self.current_record_id or not self.query_start_time:
            return
        
        try:
            response_time_ms = int((time.time() - self.query_start_time) * 1000)
            
            # Update outcome (assume useful for now, can be refined later)
            await self.ml_logger.update_outcome(
                record_id=self.current_record_id,
                was_useful=True,  # Default to True, can add feedback mechanism later
                response_time_ms=response_time_ms,
                cache_hit=False,  # TODO: Track cache hits from speculative cache
            )
            
            logger.debug(f"[ML Logger] Updated outcome: {response_time_ms}ms")
        
        except Exception as e:
            logger.error(f"[ML Logger] Failed to update outcome: {e}")
        
        finally:
            # Reset for next query
            self.current_record_id = None
            self.current_query_text = None
            self.query_start_time = None
    
    def _classify_query(self, query_text: str) -> str:
        """
        Classify query type based on content.
        
        Simple keyword-based classification for Phase 1.
        Will be replaced with ML classifier in Phase 3.
        """
        query_lower = query_text.lower()
        
        # Email queries
        if any(kw in query_lower for kw in ["email", "inbox", "message", "mail"]):
            return "email"
        
        # Calendar queries
        if any(kw in query_lower for kw in ["calendar", "meeting", "schedule", "appointment", "event"]):
            return "calendar"
        
        # Weather queries
        if any(kw in query_lower for kw in ["weather", "temperature", "forecast", "rain", "sunny"]):
            return "weather"
        
        # News queries
        if any(kw in query_lower for kw in ["news", "headline", "article", "latest"]):
            return "news"
        
        # Tesla queries
        if any(kw in query_lower for kw in ["tesla", "car", "vehicle", "battery", "charge", "charging"]):
            return "tesla"
        
        # Homelab queries
        if any(kw in query_lower for kw in ["service", "container", "docker", "homelab", "server"]):
            return "homelab"
        
        # Memory/recall queries
        if any(kw in query_lower for kw in ["remember", "recall", "memory", "told you", "said"]):
            return "memory"
        
        # Search queries
        if any(kw in query_lower for kw in ["search", "find", "look up", "google"]):
            return "search"
        
        # Time queries
        if any(kw in query_lower for kw in ["time", "date", "day", "today", "tomorrow"]):
            return "time"
        
        # Reminder/task queries
        if any(kw in query_lower for kw in ["remind", "reminder", "task", "todo", "note"]):
            return "reminder"
        
        # Default: general conversation
        return "general"
