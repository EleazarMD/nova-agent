"""
Hypothesis-Validation Framework for Nova Agent.

Enables zero-wait responses where Nova speaks immediately from trained knowledge
(hypothesis) while validation tools execute in the background, then confirms,
corrects, or enriches the response with grounded data.

Frontend Message Protocol:
  {"type": "hypothesis", "text": "...", "confidence": 0.7}
  {"type": "validating", "tools": ["weather_api", "web_search"]}
  {"type": "validationStep", "tool": "weather_api", "status": "running|completed|failed"}
  {"type": "validated", "text": "...", "result": "confirmed|corrected|enriched"}
  {"type": "sources", "citations": [{"title": "...", "url": "...", "type": "web|api|cache"}]}

Architecture:
  1. LLM generates hypothesis from trained knowledge (fast, <500ms)
  2. Framework identifies validation tools needed
  3. Tools execute in parallel while hypothesis is spoken
  4. Results are compared to hypothesis
  5. Correction/enrichment is spoken if needed
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from loguru import logger


class ValidationResult(Enum):
    """Outcome of validating a hypothesis against real data."""
    CONFIRMED = "confirmed"      # Hypothesis was accurate
    CORRECTED = "corrected"      # Hypothesis had errors, now fixed
    ENRICHED = "enriched"        # Hypothesis was correct but incomplete
    FAILED = "failed"            # Validation failed (tool error)


class ToolStatus(Enum):
    """Status of a validation tool execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Citation:
    """Source citation for validated information."""
    title: str
    url: Optional[str] = None
    source_type: str = "api"  # web, api, cache, memory
    
    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "type": self.source_type,
        }


@dataclass
class ValidationStep:
    """Tracks a single tool's validation progress."""
    tool_name: str
    status: ToolStatus = ToolStatus.PENDING
    result: Optional[str] = None
    citation: Optional[Citation] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    
    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at) * 1000)
        return None


@dataclass
class HypothesisSession:
    """Manages a single hypothesis-validation cycle."""
    hypothesis_text: str
    confidence: float
    validation_tools: list[str] = field(default_factory=list)
    steps: dict[str, ValidationStep] = field(default_factory=dict)
    validated_text: Optional[str] = None
    validation_result: Optional[ValidationResult] = None
    citations: list[Citation] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    
    def add_tool(self, tool_name: str) -> ValidationStep:
        """Register a tool for validation."""
        step = ValidationStep(tool_name=tool_name)
        self.steps[tool_name] = step
        self.validation_tools.append(tool_name)
        return step
    
    def start_tool(self, tool_name: str):
        """Mark a tool as running."""
        if tool_name in self.steps:
            self.steps[tool_name].status = ToolStatus.RUNNING
            self.steps[tool_name].started_at = time.time()
    
    def complete_tool(self, tool_name: str, result: str, citation: Optional[Citation] = None):
        """Mark a tool as completed with its result."""
        if tool_name in self.steps:
            step = self.steps[tool_name]
            step.status = ToolStatus.COMPLETED
            step.result = result
            step.citation = citation
            step.completed_at = time.time()
            if citation:
                self.citations.append(citation)
    
    def fail_tool(self, tool_name: str, error: str):
        """Mark a tool as failed."""
        if tool_name in self.steps:
            step = self.steps[tool_name]
            step.status = ToolStatus.FAILED
            step.result = error
            step.completed_at = time.time()
    
    @property
    def all_completed(self) -> bool:
        """Check if all validation tools have finished."""
        return all(
            s.status in (ToolStatus.COMPLETED, ToolStatus.FAILED)
            for s in self.steps.values()
        )
    
    @property
    def any_failed(self) -> bool:
        """Check if any validation tool failed."""
        return any(s.status == ToolStatus.FAILED for s in self.steps.values())


class HypothesisValidator:
    """
    Orchestrates hypothesis generation and validation.
    
    Usage:
        validator = HypothesisValidator(send_msg_fn)
        
        # Start hypothesis phase
        session = await validator.start_hypothesis(
            "The weather in Dallas is likely warm today, around 75°F.",
            confidence=0.7,
            tools=["get_weather"]
        )
        
        # Tool executes (called by tool handler)
        await validator.tool_started("get_weather")
        result = await get_weather({"location": "Dallas"})
        await validator.tool_completed("get_weather", result, Citation("OpenWeatherMap", type="api"))
        
        # Validate and potentially correct
        await validator.validate(
            "Actually, it's 82°F in Dallas with high humidity.",
            result=ValidationResult.CORRECTED
        )
    """
    
    def __init__(self, send_msg_fn: Callable):
        """
        Args:
            send_msg_fn: Async function to send messages to frontend.
        """
        self._send_msg = send_msg_fn
        self._current_session: Optional[HypothesisSession] = None
        self._sessions: list[HypothesisSession] = []
    
    @property
    def active(self) -> bool:
        """Check if there's an active hypothesis session."""
        return self._current_session is not None
    
    @property
    def current_session(self) -> Optional[HypothesisSession]:
        return self._current_session
    
    async def start_hypothesis(
        self,
        text: str,
        confidence: float = 0.7,
        tools: Optional[list[str]] = None,
    ) -> HypothesisSession:
        """
        Start a new hypothesis-validation cycle.
        
        Args:
            text: The hypothesis text (LLM's best guess from training).
            confidence: Confidence level 0.0-1.0.
            tools: List of tool names that will validate this hypothesis.
        
        Returns:
            The new HypothesisSession.
        """
        # Close any existing session
        if self._current_session:
            self._sessions.append(self._current_session)
        
        session = HypothesisSession(
            hypothesis_text=text,
            confidence=confidence,
        )
        
        # Register validation tools
        if tools:
            for tool in tools:
                session.add_tool(tool)
        
        self._current_session = session
        
        # Send hypothesis to frontend ONLY if there's user-facing text
        # Skip empty hypotheses (internal reasoning, tool calls, etc.)
        if text.strip() and confidence > 0:
            await self._send_msg({
                "type": "hypothesis",
                "text": text,
                "confidence": confidence,
            })
            logger.info(f"[Hypothesis] Started: '{text[:60]}...' (conf={confidence}, tools={tools})")
        else:
            logger.debug(f"[Hypothesis] Skipped empty hypothesis (conf={confidence}, tools={tools})")
        
        # If tools specified, send validating message
        if tools:
            await self._send_msg({
                "type": "validating",
                "tools": tools,
            })
        return session
    
    async def tool_started(self, tool_name: str):
        """Signal that a validation tool has started executing."""
        if not self._current_session:
            return
        
        self._current_session.start_tool(tool_name)
        
        await self._send_msg({
            "type": "validationStep",
            "tool": tool_name,
            "status": "running",
        })
        
        logger.debug(f"[Hypothesis] Tool started: {tool_name}")
    
    async def tool_completed(
        self,
        tool_name: str,
        result: str,
        citation: Optional[Citation] = None,
    ):
        """Signal that a validation tool has completed."""
        if not self._current_session:
            return
        
        self._current_session.complete_tool(tool_name, result, citation)
        
        await self._send_msg({
            "type": "validationStep",
            "tool": tool_name,
            "status": "completed",
        })
        
        logger.debug(f"[Hypothesis] Tool completed: {tool_name} ({len(result)} chars)")
    
    async def tool_failed(self, tool_name: str, error: str):
        """Signal that a validation tool has failed."""
        if not self._current_session:
            return
        
        self._current_session.fail_tool(tool_name, error)
        
        await self._send_msg({
            "type": "validationStep",
            "tool": tool_name,
            "status": "failed",
        })
        
        logger.warning(f"[Hypothesis] Tool failed: {tool_name} - {error}")
    
    async def validate(
        self,
        validated_text: Optional[str] = None,
        result: ValidationResult = ValidationResult.CONFIRMED,
        suppress_speech: bool = False,
    ):
        """
        Complete the validation phase with final result.
        
        Sends a comprehensive validated message per Zero-Wait Protocol spec:
        - text: Final user-facing response (markdown supported)
        - result: confirmed/corrected/enriched
        - suppressSpeech: Whether iOS should speak the response
        - hypothesis: Original hypothesis text (for transparency)
        - confidence: Original confidence score
        - validationSteps: Summary of all validation steps
        - sources: Grounding sources/citations
        - delta: What changed from hypothesis to final
        
        Args:
            validated_text: The corrected/enriched text (if different from hypothesis).
            result: The validation outcome.
            suppress_speech: If True, mark validated message as text-only (no TTS).
        """
        if not self._current_session:
            logger.warning("[Hypothesis] validate() called with no active session")
            return
        
        session = self._current_session
        session.validated_text = validated_text
        session.validation_result = result
        
        # Build validation steps summary
        validation_steps = []
        for tool_name, step in session.steps.items():
            step_data = {
                "tool": tool_name,
                "status": step.status.value if hasattr(step.status, 'value') else str(step.status),
            }
            if step.result:
                step_data["result"] = step.result[:100]  # Truncate for UI
            validation_steps.append(step_data)
        
        # Build sources array from citations
        sources = [c.to_dict() for c in session.citations]
        
        # Build delta (what changed from hypothesis to final)
        delta = None
        if validated_text and result != ValidationResult.CONFIRMED:
            delta = {
                "added": [],  # Could be populated by comparing texts
                "changed": [],
                "removed": [],
            }
            # Simple heuristic: if text is longer, things were added
            if len(validated_text) > len(session.hypothesis_text) * 1.2:
                delta["added"].append("Additional details")
        
        # Send comprehensive validated message per Zero-Wait Protocol
        msg: dict[str, Any] = {
            "type": "validated",
            "result": result.value,
            "suppressSpeech": suppress_speech,
            # Include original hypothesis for transparency
            "hypothesis": session.hypothesis_text,
            "confidence": session.confidence,
        }
        
        # Include text field (required by spec)
        if validated_text:
            msg["text"] = validated_text
        else:
            # If no new text, use hypothesis as the final text
            msg["text"] = session.hypothesis_text
        
        # Include validation transparency data
        if validation_steps:
            msg["validationSteps"] = validation_steps
        if sources:
            msg["sources"] = sources
        if delta:
            msg["delta"] = delta
        
        await self._send_msg(msg)
        
        logger.info(
            f"[Hypothesis] Validated: {result.value} "
            f"({len(session.citations)} citations, {len(validation_steps)} steps, suppress_speech={suppress_speech})"
        )
        
        # Archive session
        self._sessions.append(session)
        self._current_session = None
    
    async def cancel(self):
        """Cancel the current hypothesis session without validation."""
        if self._current_session:
            logger.info("[Hypothesis] Session cancelled")
            self._current_session = None
    
    def get_session_stats(self) -> dict:
        """Get statistics about hypothesis sessions."""
        if not self._sessions:
            return {"total": 0}
        
        results = {}
        for session in self._sessions:
            if session.validation_result:
                key = session.validation_result.value
                results[key] = results.get(key, 0) + 1
        
        return {
            "total": len(self._sessions),
            "results": results,
            "avg_confidence": sum(s.confidence for s in self._sessions) / len(self._sessions),
        }


# =============================================================================
# Tool Classification for Hypothesis-Validation
# =============================================================================

# Tools that provide grounded/factual data (good for validation)
VALIDATION_TOOLS = {
    "get_weather",      # Weather API - validates weather claims
    "web_search",       # Web search - validates facts, news, current events
    "check_studio",     # Calendar/email - validates schedule claims
    "get_time",         # Time API - validates time claims
    "tesla_status",     # Tesla API - validates vehicle state claims
    "service_health_check",  # Homelab - validates infrastructure claims
}

# OpenClaw delegation - special case: can be BOTH action AND validation
# depending on the task. When task involves research/lookup, it's validation.
DELEGATED_VALIDATION_TOOL = "openclaw_delegate"

# OpenClaw skills that provide validation data (grounded facts)
OPENCLAW_VALIDATION_SKILLS = {
    "browser-search",       # Web research via browser
    "browser-navigate",     # Page content extraction
    "hermes-email",         # Email search/read
    "hermes-calendar",      # Calendar lookup
    "homelab-diagnostics",  # Infrastructure investigation
    "research",             # Deep research tasks
    "fact-check",           # Fact verification
}

# Tools that are actions, not validations (don't use for hypothesis)
ACTION_TOOLS = {
    "control_lights",
    "set_reminder",
    "save_memory",
    "forget_memory",
    # Note: openclaw_delegate is handled specially - can be action OR validation
    "service_restart",
    "service_start",
    "service_stop",
    "tesla_charge_control",
    "tesla_climate_control",
    "tesla_lock_control",
    "tesla_trunk_control",
    "tesla_wake",
    "tesla_honk_flash",
    "manage_timer",
    "manage_ticket",
    "manage_workspace",
}

# Tools that retrieve stored data (medium confidence)
RETRIEVAL_TOOLS = {
    "recall_memory",
    "search_past_conversations",
    "discover_skills",
}


def classify_tool(tool_name: str) -> str:
    """Classify a tool for hypothesis-validation purposes."""
    if tool_name in VALIDATION_TOOLS:
        return "validation"
    elif tool_name == DELEGATED_VALIDATION_TOOL:
        return "delegated"  # Special: depends on task content
    elif tool_name in ACTION_TOOLS:
        return "action"
    elif tool_name in RETRIEVAL_TOOLS:
        return "retrieval"
    return "unknown"


def is_openclaw_validation_task(task: str) -> bool:
    """
    Determine if an OpenClaw delegation task is for validation (research/lookup)
    vs action (purchase, send email, etc.).
    
    Args:
        task: The task description passed to openclaw_delegate.
    
    Returns:
        True if the task appears to be research/validation oriented.
    """
    task_lower = task.lower()
    
    # Validation keywords - task is gathering/verifying information
    validation_keywords = [
        "search", "find", "look up", "lookup", "research", "check",
        "what is", "what are", "how much", "how many", "when is",
        "where is", "who is", "verify", "confirm", "investigate",
        "analyze", "compare", "review", "summarize", "read",
        "get info", "get information", "find out", "tell me about",
    ]
    
    # Action keywords - task is performing an action
    action_keywords = [
        "order", "buy", "purchase", "book", "reserve", "send",
        "create", "make", "schedule", "set up", "configure",
        "install", "deploy", "restart", "start", "stop",
        "delete", "remove", "cancel", "update", "modify",
    ]
    
    has_validation = any(kw in task_lower for kw in validation_keywords)
    has_action = any(kw in task_lower for kw in action_keywords)
    
    # If both present, prefer action (safer to not hypothesize actions)
    if has_action:
        return False
    return has_validation


def get_openclaw_citation(task: str, skills_used: Optional[list[str]] = None) -> Citation:
    """
    Generate a citation for OpenClaw delegation based on task and skills.
    
    Args:
        task: The task description.
        skills_used: Optional list of OpenClaw skills that were invoked.
    
    Returns:
        A Citation object for the OpenClaw result.
    """
    task_lower = task.lower()
    
    # Determine source type based on task/skills
    if skills_used:
        if any(s in skills_used for s in ["browser-search", "browser-navigate"]):
            return Citation("OpenClaw Browser Research", source_type="web")
        elif any(s in skills_used for s in ["hermes-email"]):
            return Citation("OpenClaw Email Search", source_type="api")
        elif any(s in skills_used for s in ["hermes-calendar"]):
            return Citation("OpenClaw Calendar", source_type="api")
        elif any(s in skills_used for s in ["homelab-diagnostics"]):
            return Citation("OpenClaw Infrastructure Analysis", source_type="api")
    
    # Infer from task description
    if "email" in task_lower:
        return Citation("OpenClaw Email Research", source_type="api")
    elif "calendar" in task_lower or "schedule" in task_lower or "meeting" in task_lower:
        return Citation("OpenClaw Calendar Research", source_type="api")
    elif "price" in task_lower or "cost" in task_lower or "buy" in task_lower:
        return Citation("OpenClaw Market Research", source_type="web")
    elif "homelab" in task_lower or "container" in task_lower or "service" in task_lower:
        return Citation("OpenClaw Infrastructure Analysis", source_type="api")
    
    return Citation("OpenClaw Research", source_type="web")


def should_use_hypothesis(tool_names: list[str]) -> bool:
    """
    Determine if hypothesis-validation should be used for a set of tools.
    
    Returns True if at least one validation tool is present and
    no action tools are present (actions should not be hypothesized).
    """
    has_validation = any(t in VALIDATION_TOOLS for t in tool_names)
    has_action = any(t in ACTION_TOOLS for t in tool_names)
    return has_validation and not has_action


def estimate_confidence(tool_name: str, query_type: str = "factual") -> float:
    """
    Estimate confidence for LLM hypothesis based on query type.
    
    Args:
        tool_name: The validation tool being used.
        query_type: Type of query (factual, temporal, personal, technical).
    
    Returns:
        Confidence score 0.0-1.0.
    """
    # Base confidence by tool type
    base_confidence = {
        "get_weather": 0.4,      # Weather changes, low confidence
        "web_search": 0.5,       # Facts may be outdated
        "check_studio": 0.3,    # Personal data, LLM doesn't know
        "get_time": 0.9,         # LLM knows time zones well
        "tesla_status": 0.2,     # Real-time state, can't predict
        "service_health_check": 0.3,  # Infrastructure state varies
    }
    
    # Query type modifiers
    type_modifier = {
        "factual": 0.0,      # No change for general facts
        "temporal": -0.2,    # Time-sensitive = lower confidence
        "personal": -0.3,    # Personal data = much lower
        "technical": 0.1,    # Technical knowledge = slightly higher
    }
    
    confidence = base_confidence.get(tool_name, 0.5)
    confidence += type_modifier.get(query_type, 0.0)
    
    return max(0.1, min(0.9, confidence))


# =============================================================================
# Global Validator Instance
# =============================================================================

_validator: Optional[HypothesisValidator] = None


def init_hypothesis_validator(send_msg_fn: Callable) -> HypothesisValidator:
    """Initialize the global hypothesis validator."""
    global _validator
    _validator = HypothesisValidator(send_msg_fn)
    logger.info("[Hypothesis] Validator initialized")
    return _validator


def get_hypothesis_validator() -> Optional[HypothesisValidator]:
    """Get the global hypothesis validator instance."""
    return _validator
