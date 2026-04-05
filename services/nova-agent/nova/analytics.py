"""
Analytics & Monitoring System for Nova Agent.

Provides:
- Conversation metrics (duration, turn count, latency)
- Tool usage tracking (frequency, success rates, timing)
- LLM performance stats (tokens, latency, model usage)
- User engagement metrics (session frequency, retention)
- Export to PostgreSQL for dashboard visualization
"""

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Callable
from collections import defaultdict
from loguru import logger


@dataclass
class ToolUsage:
    """Metrics for a single tool execution."""
    tool_name: str
    start_time: datetime
    end_time: Optional[datetime] = None
    success: bool = False
    error_type: Optional[str] = None
    latency_ms: float = 0.0
    args_summary: str = ""  # Truncated args for privacy
    result_summary: str = ""  # Truncated result
    fallback_used: bool = False


@dataclass
class ConversationMetrics:
    """Metrics for a complete conversation session."""
    session_id: str
    user_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    
    # Turn stats
    user_turns: int = 0
    assistant_turns: int = 0
    total_turns: int = 0
    
    # Timing
    total_duration_ms: float = 0.0
    avg_response_latency_ms: float = 0.0
    max_response_latency_ms: float = 0.0
    
    # Tool usage
    tool_calls: List[ToolUsage] = field(default_factory=list)
    tool_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tool_success_rates: Dict[str, float] = field(default_factory=dict)
    
    # LLM stats
    llm_calls: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    models_used: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Hypothesis validation
    hypotheses_generated: int = 0
    hypotheses_confirmed: int = 0
    hypotheses_corrected: int = 0
    
    # Errors
    errors_count: int = 0
    error_types: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # User engagement
    interruptions: int = 0  # User spoke while assistant was speaking
    clarifications_requested: int = 0
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for storage."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.total_duration_ms / 1000 if self.total_duration_ms else 0,
            "turns": {
                "user": self.user_turns,
                "assistant": self.assistant_turns,
                "total": self.total_turns,
            },
            "latency": {
                "avg_ms": self.avg_response_latency_ms,
                "max_ms": self.max_response_latency_ms,
            },
            "tools": {
                "calls": len(self.tool_calls),
                "by_type": dict(self.tool_counts),
                "success_rate": self._calculate_overall_success_rate(),
            },
            "llm": {
                "calls": self.llm_calls,
                "tokens_input": self.total_tokens_input,
                "tokens_output": self.total_tokens_output,
                "models": dict(self.models_used),
            },
            "hypothesis_validation": {
                "generated": self.hypotheses_generated,
                "confirmed": self.hypotheses_confirmed,
                "corrected": self.hypotheses_corrected,
                "accuracy": (
                    self.hypotheses_confirmed / self.hypotheses_generated * 100
                    if self.hypotheses_generated > 0 else 0
                ),
            },
            "errors": {
                "count": self.errors_count,
                "by_type": dict(self.error_types),
            },
        }
    
    def _calculate_overall_success_rate(self) -> float:
        """Calculate overall tool success rate."""
        if not self.tool_calls:
            return 100.0
        successes = sum(1 for t in self.tool_calls if t.success)
        return (successes / len(self.tool_calls)) * 100
    
    def finalize(self):
        """Finalize metrics when session ends."""
        self.end_time = datetime.now()
        self.total_duration_ms = (
            (self.end_time - self.start_time).total_seconds() * 1000
        )
        
        # Calculate tool success rates
        tool_results = defaultdict(lambda: {"success": 0, "total": 0})
        for tool in self.tool_calls:
            tool_results[tool.tool_name]["total"] += 1
            if tool.success:
                tool_results[tool.tool_name]["success"] += 1
        
        for tool_name, stats in tool_results.items():
            self.tool_success_rates[tool_name] = (
                stats["success"] / stats["total"] * 100
            )


class AnalyticsCollector:
    """
    Collects and manages conversation analytics.
    
    Tracks metrics per session and aggregates for reporting.
    """
    
    def __init__(self, user_id: str, session_id: str):
        self.user_id = user_id
        self.session_id = session_id
        self.metrics = ConversationMetrics(
            session_id=session_id,
            user_id=user_id,
            start_time=datetime.now(),
        )
        
        # Active tracking
        self._current_turn_start: Optional[float] = None
        self._active_tool: Optional[ToolUsage] = None
        self._llm_call_start: Optional[float] = None
    
    # -------------------------------------------------------------------------
    # Turn Tracking
    # -------------------------------------------------------------------------
    
    def start_user_turn(self):
        """Start tracking a user turn."""
        self.metrics.user_turns += 1
        self.metrics.total_turns += 1
        self._current_turn_start = time.time()
    
    def start_assistant_turn(self):
        """Start tracking assistant response generation."""
        self.metrics.assistant_turns += 1
        self._current_turn_start = time.time()
    
    def end_turn(self, latency_ms: Optional[float] = None):
        """End current turn and record latency."""
        if self._current_turn_start:
            calculated_latency = (time.time() - self._current_turn_start) * 1000
            latency = latency_ms or calculated_latency
            
            # Update rolling average
            n = self.metrics.total_turns
            old_avg = self.metrics.avg_response_latency_ms
            self.metrics.avg_response_latency_ms = (
                (old_avg * (n - 1) + latency) / n
            )
            
            # Update max
            if latency > self.metrics.max_response_latency_ms:
                self.metrics.max_response_latency_ms = latency
            
            self._current_turn_start = None
    
    # -------------------------------------------------------------------------
    # Tool Tracking
    # -------------------------------------------------------------------------
    
    def start_tool_call(self, tool_name: str, args: Dict):
        """Start tracking a tool execution."""
        self._active_tool = ToolUsage(
            tool_name=tool_name,
            start_time=datetime.now(),
            args_summary=self._summarize_args(args),
        )
        self.metrics.tool_counts[tool_name] += 1
    
    def end_tool_call(
        self,
        success: bool,
        result: Any = None,
        error: Optional[Exception] = None,
        fallback_used: bool = False,
    ):
        """End tool execution tracking."""
        if self._active_tool:
            tool = self._active_tool
            tool.end_time = datetime.now()
            tool.success = success
            tool.fallback_used = fallback_used
            tool.latency_ms = (
                (tool.end_time - tool.start_time).total_seconds() * 1000
            )
            
            if result:
                tool.result_summary = self._summarize_result(result)
            
            if error:
                tool.error_type = type(error).__name__
                self.metrics.errors_count += 1
                self.metrics.error_types[type(error).__name__] += 1
            
            self.metrics.tool_calls.append(tool)
            self._active_tool = None
    
    def _summarize_args(self, args: Dict) -> str:
        """Create privacy-safe args summary."""
        # Truncate and redact sensitive fields
        summary = {}
        for k, v in args.items():
            if any(sensitive in k.lower() for sensitive in ["password", "token", "key", "secret"]):
                summary[k] = "***REDACTED***"
            else:
                summary[k] = str(v)[:50]  # Truncate long values
        return json.dumps(summary)[:200]
    
    def _summarize_result(self, result: Any) -> str:
        """Create truncated result summary."""
        text = str(result)
        return text[:200] + "..." if len(text) > 200 else text
    
    # -------------------------------------------------------------------------
    # LLM Tracking
    # -------------------------------------------------------------------------
    
    def start_llm_call(self):
        """Start tracking LLM call."""
        self._llm_call_start = time.time()
        self.metrics.llm_calls += 1
    
    def end_llm_call(
        self,
        model: str,
        tokens_input: int = 0,
        tokens_output: int = 0,
    ):
        """End LLM call tracking."""
        self.metrics.models_used[model] += 1
        self.metrics.total_tokens_input += tokens_input
        self.metrics.total_tokens_output += tokens_output
        self._llm_call_start = None
    
    # -------------------------------------------------------------------------
    # Hypothesis Tracking
    # -------------------------------------------------------------------------
    
    def record_hypothesis_generated(self):
        """Record hypothesis generation."""
        self.metrics.hypotheses_generated += 1
    
    def record_hypothesis_confirmed(self):
        """Record hypothesis validation success."""
        self.metrics.hypotheses_confirmed += 1
    
    def record_hypothesis_corrected(self):
        """Record hypothesis required correction."""
        self.metrics.hypotheses_corrected += 1
    
    # -------------------------------------------------------------------------
    # Event Tracking
    # -------------------------------------------------------------------------
    
    def record_interruption(self):
        """Record user interruption."""
        self.metrics.interruptions += 1
    
    def record_clarification_request(self):
        """Record user asked for clarification."""
        self.metrics.clarifications_requested += 1
    
    def record_error(self, error: Exception):
        """Record an error."""
        self.metrics.errors_count += 1
        self.metrics.error_types[type(error).__name__] += 1
    
    # -------------------------------------------------------------------------
    # Finalization
    # -------------------------------------------------------------------------
    
    def finalize(self) -> ConversationMetrics:
        """Finalize and return metrics."""
        self.metrics.finalize()
        return self.metrics
    
    async def persist(self, db_pool=None):
        """Persist metrics to database."""
        self.finalize()
        
        # TODO: Implement database persistence
        # For now, just log
        logger.info(f"Session {self.session_id} metrics:")
        logger.info(json.dumps(self.metrics.to_dict(), indent=2))
        
        # Could also write to file or send to monitoring service
        # await _persist_to_timeseries_db(self.metrics)


class AnalyticsAggregator:
    """
    Aggregates analytics across multiple sessions for reporting.
    
    Provides:
    - Daily/weekly/monthly summaries
    - Trend analysis
    - Performance comparisons
    """
    
    def __init__(self):
        self._sessions: List[ConversationMetrics] = []
    
    def add_session(self, metrics: ConversationMetrics):
        """Add a completed session."""
        self._sessions.append(metrics)
    
    def get_daily_summary(self, date: Optional[datetime] = None) -> Dict:
        """Get summary for a specific day."""
        target_date = date or datetime.now()
        day_sessions = [
            s for s in self._sessions
            if s.start_time.date() == target_date.date()
        ]
        
        if not day_sessions:
            return {"error": "No sessions for this date"}
        
        return {
            "date": target_date.date().isoformat(),
            "total_sessions": len(day_sessions),
            "total_turns": sum(s.total_turns for s in day_sessions),
            "avg_session_duration_min": (
                sum(s.total_duration_ms for s in day_sessions) / 
                len(day_sessions) / 60000
            ),
            "total_tool_calls": sum(len(s.tool_calls) for s in day_sessions),
            "tool_success_rate": (
                sum(s._calculate_overall_success_rate() for s in day_sessions) / 
                len(day_sessions)
            ),
            "total_errors": sum(s.errors_count for s in day_sessions),
            "hypothesis_accuracy": (
                sum(
                    s.hypotheses_confirmed / s.hypotheses_generated * 100
                    if s.hypotheses_generated > 0 else 100
                    for s in day_sessions
                ) / len(day_sessions)
            ),
        }
    
    def get_tool_usage_report(self, days: int = 7) -> Dict:
        """Get tool usage report for last N days."""
        cutoff = datetime.now() - timedelta(days=days)
        recent_sessions = [s for s in self._sessions if s.start_time > cutoff]
        
        tool_stats = defaultdict(lambda: {"calls": 0, "successes": 0})
        
        for session in recent_sessions:
            for tool in session.tool_calls:
                tool_stats[tool.tool_name]["calls"] += 1
                if tool.success:
                    tool_stats[tool.tool_name]["successes"] += 1
        
        return {
            "period_days": days,
            "tools": {
                name: {
                    "calls": stats["calls"],
                    "successes": stats["successes"],
                    "failures": stats["calls"] - stats["successes"],
                    "success_rate": stats["successes"] / stats["calls"] * 100
                    if stats["calls"] > 0 else 0,
                }
                for name, stats in tool_stats.items()
            },
        }


# Global aggregators per user
_user_collectors: Dict[str, AnalyticsCollector] = {}
_global_aggregator = AnalyticsAggregator()


def get_analytics_collector(user_id: str, session_id: str) -> AnalyticsCollector:
    """Get or create analytics collector for a session."""
    key = f"{user_id}:{session_id}"
    if key not in _user_collectors:
        _user_collectors[key] = AnalyticsCollector(user_id, session_id)
    return _user_collectors[key]


def remove_analytics_collector(user_id: str, session_id: str):
    """Remove collector and add to global aggregator."""
    key = f"{user_id}:{session_id}"
    if key in _user_collectors:
        collector = _user_collectors.pop(key)
        metrics = collector.finalize()
        _global_aggregator.add_session(metrics)


def get_global_aggregator() -> AnalyticsAggregator:
    """Get the global analytics aggregator."""
    return _global_aggregator
