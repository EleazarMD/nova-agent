"""
Nova Agent - Parallel Retrieval

Layer 2 of the Zero-Wait Ground-Truth Architecture.
Queries multiple data sources simultaneously on cache miss.

Features:
- Parallel execution of retrieval tasks
- Streaming progress updates (retrievalStep messages)
- Result aggregation and deduplication
- Timeout handling per source
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum

from loguru import logger


class RetrievalSource(str, Enum):
    """Data sources for parallel retrieval."""
    HERMES_CORE = "hermes_core"       # Calendar, Email, Tasks
    WEB_SEARCH = "web_search"         # News, facts, general web
    KNOWLEDGE_GRAPH = "knowledge"     # PIC, LIAM, vector store
    WEATHER = "weather"               # Weather API
    TESLA = "tesla"                   # Vehicle status


@dataclass
class RetrievalResult:
    """Result from a single retrieval source."""
    source: RetrievalSource
    data: str
    success: bool
    duration_ms: int = 0
    error: Optional[str] = None
    citations: list[dict] = field(default_factory=list)


@dataclass
class AggregatedResults:
    """Combined results from all retrieval sources."""
    results: list[RetrievalResult]
    total_duration_ms: int
    domain: str
    
    @property
    def successful(self) -> list[RetrievalResult]:
        return [r for r in self.results if r.success]
    
    @property
    def failed(self) -> list[RetrievalResult]:
        return [r for r in self.results if not r.success]
    
    @property
    def all_citations(self) -> list[dict]:
        citations = []
        for r in self.successful:
            citations.extend(r.citations)
        return citations
    
    def to_display_text(self) -> str:
        """Combine all successful results for display."""
        parts = []
        for r in self.successful:
            if r.data.strip():
                parts.append(r.data)
        return "\n\n".join(parts)


class ParallelRetriever:
    """
    Parallel retrieval from multiple data sources.
    
    Usage:
        retriever = ParallelRetriever(
            tool_dispatcher=dispatch_tool,
            progress_callback=send_retrieval_step,
        )
        
        # Query multiple sources in parallel
        results = await retriever.retrieve(
            query="what's the latest AI news",
            domain="news",
            sources=[RetrievalSource.WEB_SEARCH],
        )
        
        # Send aggregated response
        await send_grounded(results.to_display_text(), results.all_citations)
    """
    
    def __init__(
        self,
        tool_dispatcher: Callable,
        progress_callback: Optional[Callable] = None,
    ):
        self._tool_dispatcher = tool_dispatcher
        self._progress = progress_callback
        self._timeout_seconds = 10.0
    
    def set_timeout(self, seconds: float):
        """Set timeout for individual retrieval calls."""
        self._timeout_seconds = seconds
    
    async def retrieve(
        self,
        query: str,
        domain: str,
        sources: list[RetrievalSource],
        user_id: str = "default",
    ) -> AggregatedResults:
        """
        Execute parallel retrieval from multiple sources.
        
        Args:
            query: User query
            domain: Query domain (productivity, news, tasks, knowledge)
            sources: List of sources to query
            user_id: User ID for tool calls
            
        Returns:
            AggregatedResults with all source responses
        """
        start_time = time.monotonic()
        
        # Send retrieving message
        if self._progress:
            await self._progress({
                "type": "retrieving",
                "domain": domain,
                "sources": [s.value for s in sources],
            })
        
        # Map domain to default sources if not specified
        if not sources:
            sources = self._get_default_sources(domain)
        
        # Create tasks for parallel execution
        tasks = []
        for source in sources:
            task = self._retrieve_from_source(
                source=source,
                query=query,
                domain=domain,
                user_id=user_id,
            )
            tasks.append(task)
        
        # Execute all in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append(RetrievalResult(
                    source=sources[i],
                    data="",
                    success=False,
                    error=str(result),
                ))
            else:
                processed_results.append(result)
        
        total_duration = int((time.monotonic() - start_time) * 1000)
        
        # Send completion for each source
        for result in processed_results:
            if self._progress:
                await self._progress({
                    "type": "retrievalStep",
                    "source": result.source.value,
                    "status": "completed" if result.success else "failed",
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                })
        
        return AggregatedResults(
            results=processed_results,
            total_duration_ms=total_duration,
            domain=domain,
        )
    
    def _get_default_sources(self, domain: str) -> list[RetrievalSource]:
        """Get default sources for a domain."""
        mapping = {
            "productivity": [RetrievalSource.HERMES_CORE],
            "news": [RetrievalSource.WEB_SEARCH],
            "tasks": [RetrievalSource.HERMES_CORE],
            "knowledge": [RetrievalSource.KNOWLEDGE_GRAPH, RetrievalSource.WEB_SEARCH],
            "weather": [RetrievalSource.WEATHER],
        }
        return mapping.get(domain, [RetrievalSource.WEB_SEARCH])
    
    async def _retrieve_from_source(
        self,
        source: RetrievalSource,
        query: str,
        domain: str,
        user_id: str,
    ) -> RetrievalResult:
        """Retrieve data from a single source."""
        start_time = time.monotonic()
        
        try:
            if source == RetrievalSource.HERMES_CORE:
                return await self._retrieve_hermes(query, domain, user_id, start_time)
            elif source == RetrievalSource.WEB_SEARCH:
                return await self._retrieve_web(query, domain, start_time)
            elif source == RetrievalSource.KNOWLEDGE_GRAPH:
                return await self._retrieve_knowledge(query, start_time)
            elif source == RetrievalSource.WEATHER:
                return await self._retrieve_weather(query, start_time)
            elif source == RetrievalSource.TESLA:
                return await self._retrieve_tesla(user_id, start_time)
            else:
                return RetrievalResult(
                    source=source,
                    data="",
                    success=False,
                    duration_ms=int((time.monotonic() - start_time) * 1000),
                    error=f"Unknown source: {source}",
                )
        except asyncio.TimeoutError:
            return RetrievalResult(
                source=source,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error="Timeout",
            )
        except Exception as e:
            return RetrievalResult(
                source=source,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error=str(e),
            )
    
    async def _retrieve_hermes(
        self,
        query: str,
        domain: str,
        user_id: str,
        start_time: float,
    ) -> RetrievalResult:
        """Retrieve from Hermes Core (calendar, email, tasks)."""
        
        # Determine what to fetch based on query
        if any(kw in query.lower() for kw in ["schedule", "meeting", "calendar", "appointments"]):
            tool_name = "check_studio"
            tool_args = {"query": "what's on my schedule today"}
        elif any(kw in query.lower() for kw in ["task", "todo", "pending"]):
            tool_name = "check_studio"
            tool_args = {"query": "what tasks do I have pending"}
        elif any(kw in query.lower() for kw in ["email", "unread"]):
            tool_name = "check_studio"
            tool_args = {"query": "any unread emails"}
        else:
            tool_name = "check_studio"
            tool_args = {"query": query}
        
        try:
            result = await asyncio.wait_for(
                self._tool_dispatcher(tool_name, tool_args, user_id),
                timeout=self._timeout_seconds,
            )
            result_text = result if isinstance(result, str) else str(result)
            
            return RetrievalResult(
                source=RetrievalSource.HERMES_CORE,
                data=result_text,
                success=True,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                citations=[{"source": "Hermes Core", "type": domain}],
            )
        except asyncio.TimeoutError:
            return RetrievalResult(
                source=RetrievalSource.HERMES_CORE,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error="Hermes Core timeout",
            )
    
    async def _retrieve_web(
        self,
        query: str,
        domain: str,
        start_time: float,
    ) -> RetrievalResult:
        """Retrieve from web search."""
        try:
            result = await asyncio.wait_for(
                self._tool_dispatcher("web_search", {"query": query}),
                timeout=self._timeout_seconds,
            )
            result_text = result if isinstance(result, str) else str(result)
            
            return RetrievalResult(
                source=RetrievalSource.WEB_SEARCH,
                data=result_text,
                success=True,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                citations=[{"source": "Web Search", "query": query}],
            )
        except asyncio.TimeoutError:
            return RetrievalResult(
                source=RetrievalSource.WEB_SEARCH,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error="Web search timeout",
            )
    
    async def _retrieve_knowledge(
        self,
        query: str,
        start_time: float,
    ) -> RetrievalResult:
        """Retrieve from Knowledge Graph (PIC/LIAM)."""
        # TODO: Implement when PIC/LIAM integration is ready
        return RetrievalResult(
            source=RetrievalSource.KNOWLEDGE_GRAPH,
            data="",
            success=False,
            duration_ms=int((time.monotonic() - start_time) * 1000),
            error="Knowledge Graph not yet integrated",
        )
    
    async def _retrieve_weather(
        self,
        query: str,
        start_time: float,
    ) -> RetrievalResult:
        """Retrieve weather data."""
        try:
            result = await asyncio.wait_for(
                self._tool_dispatcher("get_weather", {}),
                timeout=self._timeout_seconds,
            )
            result_text = result if isinstance(result, str) else str(result)
            
            return RetrievalResult(
                source=RetrievalSource.WEATHER,
                data=result_text,
                success=True,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                citations=[{"source": "Weather API"}],
            )
        except asyncio.TimeoutError:
            return RetrievalResult(
                source=RetrievalSource.WEATHER,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error="Weather API timeout",
            )
    
    async def _retrieve_tesla(
        self,
        user_id: str,
        start_time: float,
    ) -> RetrievalResult:
        """Retrieve Tesla vehicle status."""
        try:
            result = await asyncio.wait_for(
                self._tool_dispatcher("tesla_vehicle_status", {"user_id": user_id}),
                timeout=self._timeout_seconds,
            )
            result_text = result if isinstance(result, str) else str(result)
            
            return RetrievalResult(
                source=RetrievalSource.TESLA,
                data=result_text,
                success=True,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                citations=[{"source": "Tesla API"}],
            )
        except asyncio.TimeoutError:
            return RetrievalResult(
                source=RetrievalSource.TESLA,
                data="",
                success=False,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                error="Tesla API timeout",
            )


# Singleton instance
_retriever_instance: Optional[ParallelRetriever] = None


def get_parallel_retriever() -> ParallelRetriever:
    """Get the global parallel retriever instance."""
    global _retriever_instance
    return _retriever_instance


def init_parallel_retriever(
    tool_dispatcher: Callable,
    progress_callback: Optional[Callable] = None,
) -> ParallelRetriever:
    """Initialize the global parallel retriever."""
    global _retriever_instance
    _retriever_instance = ParallelRetriever(
        tool_dispatcher=tool_dispatcher,
        progress_callback=progress_callback,
    )
    return _retriever_instance
