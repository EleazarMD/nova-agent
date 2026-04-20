"""
Error Handling & Fallback System for Nova Agent.

Provides:
- Automatic web_search fallback when browser/Hub delegation fails
- Circuit breakers for external APIs (Tesla, Perplexity, etc.)
- Graceful degradation to local responses
- Retry logic with exponential backoff
- Error classification and recovery strategies
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Callable, Any, Dict, List
from enum import Enum
from loguru import logger


class ErrorSeverity(Enum):
    """Error severity levels for different handling strategies."""
    TRANSIENT = "transient"      # Retry likely to succeed
    DEGRADED = "degraded"        # Fallback to reduced functionality
    CRITICAL = "critical"        # Requires human intervention
    FATAL = "fatal"             # Complete failure


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"           # Normal operation
    OPEN = "open"               # Failing, reject fast
    HALF_OPEN = "half_open"     # Testing if recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for external API protection.
    
    Prevents cascade failures by stopping requests to failing services.
    """
    name: str
    failure_threshold: int = 5        # Failures before opening
    recovery_timeout: int = 60        # Seconds before half-open
    half_open_max_calls: int = 3      # Test calls in half-open
    
    # State
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: Optional[datetime] = None
    success_count: int = 0
    
    def record_success(self):
        """Record a successful call."""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_max_calls:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                logger.info(f"Circuit {self.name}: CLOSED (recovered)")
        else:
            self.failure_count = max(0, self.failure_count - 1)
    
    def record_failure(self) -> bool:
        """
        Record a failed call.
        
        Returns True if circuit just opened.
        """
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit {self.name}: OPEN (failed {self.failure_count} times)")
                return True
        
        elif self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.success_count = 0
            logger.warning(f"Circuit {self.name}: OPEN (recovery failed)")
            return True
        
        return False
    
    def can_execute(self) -> bool:
        """Check if execution should proceed."""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout elapsed
            if self.last_failure_time:
                elapsed = (datetime.now() - self.last_failure_time).total_seconds()
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(f"Circuit {self.name}: HALF_OPEN (testing recovery)")
                    return True
            return False
        
        return True  # HALF_OPEN allows limited execution


# Registry of circuit breakers per service
_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(service_name: str) -> CircuitBreaker:
    """Get or create circuit breaker for a service."""
    if service_name not in _circuit_breakers:
        _circuit_breakers[service_name] = CircuitBreaker(name=service_name)
    return _circuit_breakers[service_name]


@dataclass
class FallbackResult:
    """Result from a fallback operation."""
    success: bool
    data: Any
    source: str  # Which fallback provided the result
    message: str = ""
    confidence: float = 1.0


class FallbackOrchestrator:
    """
    Orchestrates fallback strategies when primary tools fail.
    
    Fallback chain:
    1. Primary tool (e.g., browser/Hub delegation)
    2. Web search fallback
    3. Local knowledge / cache
    4. Graceful "I don't know" with suggestions
    """
    
    def __init__(self):
        self._fallback_handlers: Dict[str, List[Callable]] = {}
    
    def register_fallback(self, tool_name: str, fallback_fn: Callable):
        """Register a fallback handler for a tool."""
        if tool_name not in self._fallback_handlers:
            self._fallback_handlers[tool_name] = []
        self._fallback_handlers[tool_name].append(fallback_fn)
    
    async def execute_with_fallback(
        self,
        tool_name: str,
        primary_fn: Callable,
        *args,
        **kwargs
    ) -> FallbackResult:
        """
        Execute primary function with fallback chain.
        
        Args:
            tool_name: Name of the tool for fallback lookup
            primary_fn: Primary function to execute
            *args, **kwargs: Arguments for primary function
            
        Returns:
            FallbackResult with data from primary or fallback
        """
        # Check circuit breaker
        circuit = get_circuit_breaker(tool_name)
        
        if not circuit.can_execute():
            logger.warning(f"Circuit open for {tool_name}, skipping to fallback")
            return await self._run_fallbacks(tool_name, *args, **kwargs)
        
        # Try primary
        try:
            result = await primary_fn(*args, **kwargs)
            circuit.record_success()
            return FallbackResult(
                success=True,
                data=result,
                source=tool_name,
                message="Primary execution successful",
            )
        except Exception as e:
            circuit.record_failure()
            logger.warning(f"Primary {tool_name} failed: {e}")
            return await self._run_fallbacks(tool_name, *args, **kwargs)
    
    async def _run_fallbacks(self, tool_name: str, *args, **kwargs) -> FallbackResult:
        """Run registered fallback handlers."""
        fallbacks = self._fallback_handlers.get(tool_name, [])
        
        for i, fallback_fn in enumerate(fallbacks):
            try:
                result = await fallback_fn(*args, **kwargs)
                if result:
                    return FallbackResult(
                        success=True,
                        data=result,
                        source=f"fallback_{i+1}",
                        message=f"Fallback {i+1} succeeded",
                        confidence=0.8 - (i * 0.1),  # Decreasing confidence per fallback
                    )
            except Exception as e:
                logger.warning(f"Fallback {i+1} for {tool_name} failed: {e}")
                continue
        
        # All fallbacks exhausted
        return FallbackResult(
            success=False,
            data=None,
            source="none",
            message="All fallback strategies exhausted",
            confidence=0.0,
        )


# Global orchestrator
_fallback_orchestrator = FallbackOrchestrator()


async def web_search_fallback(query: str, **kwargs) -> Optional[str]:
    """
    Web search fallback for browser/Hub delegation failures.
    
    When browser automation fails (safety blocks, navigation errors),
    fall back to web search for current information.
    """
    try:
        from nova.tools import web_search
        
        logger.info(f"Web search fallback for: {query[:60]}...")
        
        # Augment query for better results
        search_query = f"current information about {query}"
        
        result = await web_search(query=search_query)
        
        if result and not result.startswith("Error"):
            logger.info("Web search fallback succeeded")
            return f"[Via web search] {result}"
        
        return None
        
    except Exception as e:
        logger.error(f"Web search fallback failed: {e}")
        return None


async def local_knowledge_fallback(query: str, **kwargs) -> Optional[str]:
    """
    Local knowledge fallback when all external sources fail.
    
    Uses speculative cache and long-term memory for best-effort answer.
    """
    try:
        from nova.speculative_cache import get_cache
        from nova.memory import get_memory_store
        
        logger.info(f"Local knowledge fallback for: {query[:60]}...")
        
        # Try speculative cache
        cache = get_cache()
        cache_result = await cache.lookup(query)
        if cache_result:
            return f"[From local cache] {cache_result.display_text}"
        
        # Try long-term memory
        # Note: Would need user_id from context
        # memory = get_memory_store(user_id)
        # memories = await memory.recall(query, limit=3)
        # if memories:
        #     return f"[From memory] {memories[0].content}"
        
        return None
        
    except Exception as e:
        logger.error(f"Local knowledge fallback failed: {e}")
        return None


async def graceful_degradation_response(query: str, **kwargs) -> str:
    """
    Final fallback: graceful "I don't know" with suggestions.
    """
    suggestions = [
        "I can search the web for current information",
        "I can check your emails or calendar for related details",
        "You could ask me to try again in a moment",
    ]
    
    return (
        f"I'm having trouble getting current information about that right now. "
        f"Here are some alternatives:\n"
        + "\n".join(f"- {s}" for s in suggestions)
    )


# Register fallbacks
_fallback_orchestrator.register_fallback("hub_delegate", web_search_fallback)
_fallback_orchestrator.register_fallback("hub_delegate", local_knowledge_fallback)
_fallback_orchestrator.register_fallback("hub_delegate", graceful_degradation_response)

_fallback_orchestrator.register_fallback("browser_navigate", web_search_fallback)
_fallback_orchestrator.register_fallback("browser_navigate", graceful_degradation_response)


async def execute_with_fallback(
    tool_name: str,
    primary_fn: Callable,
    *args,
    **kwargs
) -> FallbackResult:
    """
    Convenience function: execute with automatic fallback.
    
    Example:
        result = await execute_with_fallback(
            "hub_delegate",
            hub_delegate_tool,
            task="Check my recent Amazon orders"
        )
        
        if result.success:
            return result.data
        else:
            return "I'm unable to help with that right now"
    """
    return await _fallback_orchestrator.execute_with_fallback(
        tool_name, primary_fn, *args, **kwargs
    )


class RetryWithBackoff:
    """
    Retry decorator with exponential backoff.
    
    Usage:
        @RetryWithBackoff(max_retries=3, base_delay=1.0)
        async def my_async_function():
            # May fail transiently
            pass
    """
    
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_exceptions: tuple = (Exception,),
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_exceptions = retryable_exceptions
    
    def __call__(self, fn: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(self.max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except self.retryable_exceptions as e:
                    last_exception = e
                    
                    if attempt == self.max_retries:
                        logger.error(f"Max retries ({self.max_retries}) exceeded for {fn.__name__}")
                        raise
                    
                    delay = min(
                        self.base_delay * (self.exponential_base ** attempt),
                        self.max_delay,
                    )
                    
                    logger.warning(
                        f"Attempt {attempt + 1}/{self.max_retries + 1} failed for {fn.__name__}: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    
                    await asyncio.sleep(delay)
            
            # Should never reach here
            raise last_exception
        
        return wrapper


# Convenience retry decorator
retry_with_backoff = RetryWithBackoff(
    max_retries=3,
    base_delay=1.0,
    retryable_exceptions=(ConnectionError, TimeoutError, asyncio.TimeoutError),
)
