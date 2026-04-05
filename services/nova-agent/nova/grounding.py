"""
Nova Agent - Grounding Layer

Layer 3 of the Zero-Wait Ground-Truth Architecture.
Verifies facts, attaches citations, calculates confidence.

Features:
- Fact verification against retrieved data
- Citation attachment
- Confidence scoring
- Natural speech organization
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from loguru import logger


class ConfidenceLevel(str, Enum):
    """Confidence scoring levels."""
    HIGH = "high"       # 0.9-1.0 - Verified against source
    MEDIUM = "medium"   # 0.6-0.9 - Partial verification
    LOW = "low"         # 0.3-0.6 - Unverified
    UNKNOWN = "unknown" # 0.0-0.3 - No source


@dataclass
class GroundedResponse:
    """Fully grounded response with citations and confidence."""
    display_text: str       # Rich text for UI
    speech_text: str        # Natural speech for TTS
    domain: str             # productivity, news, tasks, etc.
    source: str             # cache, retrieval, llm
    confidence: float       # 0.0-1.0
    confidence_level: ConfidenceLevel
    citations: list[dict] = field(default_factory=list)
    delta: Optional[str] = None  # New information since last turn


@dataclass
class FactClaim:
    """A factual claim extracted from LLM response."""
    text: str
    verified: bool = False
    source: Optional[str] = None
    confidence: float = 0.0


class GroundingService:
    """
    Verifies and grounds LLM responses against retrieved data.
    
    Usage:
        grounding = GroundingService(
            speech_transform=transform_for_speech,
        )
        
        result = await grounding.ground(
            llm_response="The meeting is at 2 PM.",
            retrieved_data=["Calendar: 2:00 PM meeting with John"],
            domain="productivity",
        )
        
        # result.display_text → "**2:00 PM** — Meeting with John"
        # result.speech_text → "You have a meeting at 2 PM with John"
        # result.confidence → 0.95
        # result.citations → [{"source": "Hermes Calendar"}]
    """
    
    def __init__(
        self,
        speech_transform=None,
    ):
        self._speech_transform = speech_transform or self._default_speech_transform
    
    def _default_speech_transform(self, text: str) -> str:
        """Default speech transform."""
        from nova.text_utils import transform_for_speech
        return transform_for_speech(text)
    
    async def ground(
        self,
        llm_response: str,
        retrieved_data: list[str],
        domain: str = "general",
        source: str = "llm",
    ) -> GroundedResponse:
        """
        Ground an LLM response against retrieved data.
        
        Args:
            llm_response: Raw LLM response text
            retrieved_data: List of retrieved data strings
            domain: Query domain
            source: Response source (cache, retrieval, llm)
            
        Returns:
            GroundedResponse with display text, speech text, citations, confidence
        """
        if not llm_response.strip():
            return GroundedResponse(
                display_text="",
                speech_text="",
                domain=domain,
                source=source,
                confidence=0.0,
                confidence_level=ConfidenceLevel.UNKNOWN,
            )
        
        # If no retrieved data, use LLM response as-is with lower confidence
        if not retrieved_data:
            return self._ground_without_verification(llm_response, domain, source)
        
        # Verify claims against retrieved data
        claims = self._extract_claims(llm_response)
        verified_claims = self._verify_claims(claims, retrieved_data)
        
        # Calculate overall confidence
        confidence = self._calculate_confidence(verified_claims, retrieved_data)
        confidence_level = self._get_confidence_level(confidence)
        
        # Extract citations
        citations = self._extract_citations(retrieved_data)
        
        # Transform for display and speech
        display_text = self._format_for_display(llm_response, citations)
        speech_text = self._speech_transform(llm_response)
        
        return GroundedResponse(
            display_text=display_text,
            speech_text=speech_text,
            domain=domain,
            source=source,
            confidence=confidence,
            confidence_level=confidence_level,
            citations=citations,
        )
    
    def _ground_without_verification(
        self,
        response: str,
        domain: str,
        source: str,
    ) -> GroundedResponse:
        """Ground response without verification (cache hit or direct LLM)."""
        speech_text = self._speech_transform(response)
        
        # Higher confidence for cache (pre-verified)
        confidence = 0.95 if source == "cache" else 0.7
        level = ConfidenceLevel.HIGH if source == "cache" else ConfidenceLevel.MEDIUM
        
        return GroundedResponse(
            display_text=response,
            speech_text=speech_text,
            domain=domain,
            source=source,
            confidence=confidence,
            confidence_level=level,
            citations=[{"source": "cache", "cached": True}] if source == "cache" else [],
        )
    
    def _extract_claims(self, text: str) -> list[FactClaim]:
        """Extract factual claims from text."""
        claims = []
        
        # Extract time-based claims
        time_pattern = r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)'
        for match in re.finditer(time_pattern, text, re.IGNORECASE):
            claims.append(FactClaim(
                text=match.group(1),
                confidence=0.5,
            ))
        
        # Extract number-based claims
        number_pattern = r'(\d+)\s+(?:items?|tasks?|meetings?|emails?|events?)'
        for match in re.finditer(number_pattern, text, re.IGNORECASE):
            claims.append(FactClaim(
                text=match.group(0),
                confidence=0.5,
            ))
        
        # If no specific claims, treat whole response as one claim
        if not claims:
            claims.append(FactClaim(text=text, confidence=0.5))
        
        return claims
    
    def _verify_claims(
        self,
        claims: list[FactClaim],
        retrieved_data: list[str],
    ) -> list[FactClaim]:
        """Verify claims against retrieved data."""
        retrieved_text = " ".join(retrieved_data).lower()
        
        verified = []
        for claim in claims:
            claim_lower = claim.text.lower()
            
            # Check if claim text appears in retrieved data
            if claim_lower in retrieved_text:
                claim.verified = True
                claim.confidence = 0.95
                claim.source = "retrieved"
            # Check for partial matches
            elif any(word in retrieved_text for word in claim.text.lower().split() if len(word) > 3):
                claim.verified = True
                claim.confidence = 0.8
                claim.source = "retrieved"
            else:
                claim.confidence = 0.4
            
            verified.append(claim)
        
        return verified
    
    def _calculate_confidence(
        self,
        claims: list[FactClaim],
        retrieved_data: list[str],
    ) -> float:
        """Calculate overall confidence score."""
        if not claims:
            return 0.5
        
        # Average confidence of all claims
        avg_confidence = sum(c.confidence for c in claims) / len(claims)
        
        # Boost if we have retrieved data
        if retrieved_data:
            boost = min(0.1, len(retrieved_data) * 0.02)
            avg_confidence = min(1.0, avg_confidence + boost)
        
        return round(avg_confidence, 2)
    
    def _get_confidence_level(self, confidence: float) -> ConfidenceLevel:
        """Map confidence score to level."""
        if confidence >= 0.9:
            return ConfidenceLevel.HIGH
        elif confidence >= 0.6:
            return ConfidenceLevel.MEDIUM
        elif confidence >= 0.3:
            return ConfidenceLevel.LOW
        return ConfidenceLevel.UNKNOWN
    
    def _extract_citations(self, retrieved_data: list[str]) -> list[dict]:
        """Extract citations from retrieved data."""
        citations = []
        
        for data in retrieved_data:
            # Determine source type
            source = "unknown"
            if "calendar" in data.lower() or "schedule" in data.lower():
                source = "Hermes Calendar"
            elif "email" in data.lower() or "inbox" in data.lower():
                source = "Hermes Email"
            elif "task" in data.lower() or "todo" in data.lower():
                source = "Hermes Tasks"
            elif "weather" in data.lower():
                source = "Weather API"
            elif "search" in data.lower() or "news" in data.lower():
                source = "Web Search"
            elif "tesla" in data.lower() or "vehicle" in data.lower():
                source = "Tesla API"
            
            if source != "unknown":
                citations.append({"source": source})
        
        return citations
    
    def _format_for_display(self, text: str, citations: list[dict]) -> str:
        """Format text for display with citation markers."""
        if not citations:
            return text
        
        # Add citation markers
        sources = [c["source"] for c in citations]
        citation_str = " | ".join(sources)
        
        return f"{text}\n\n_Sources: {citation_str}_"


class GroundingProcessor:
    """
    Pipecat processor for grounding LLM responses.
    
    Placed after LLM in pipeline to:
    1. Buffer LLM response
    2. Ground against retrieved data
    3. Send grounded response to iOS
    """
    
    def __init__(
        self,
        grounding_service: Optional[GroundingService] = None,
        server_msg_fn: Optional[callable] = None,
    ):
        self._grounding = grounding_service or GroundingService()
        self._server_msg = server_msg_fn
        self._buffer: list[str] = []
        self._retrieved_data: list[str] = []
    
    def set_retrieved_data(self, data: list[str]):
        """Set retrieved data for grounding."""
        self._retrieved_data = data
    
    async def process(self, text: str, domain: str = "general") -> GroundedResponse:
        """Process text through grounding layer."""
        return await self._grounding.ground(
            llm_response=text,
            retrieved_data=self._retrieved_data,
            domain=domain,
        )
    
    async def send_grounded(self, response: GroundedResponse):
        """Send grounded response to iOS client."""
        if self._server_msg:
            await self._server_msg({
                "type": "grounded",
                "text": response.display_text,
                "speechText": response.speech_text,
                "domain": response.domain,
                "source": response.source,
                "confidence": response.confidence,
                "confidenceLevel": response.confidence_level.value,
                "citations": response.citations,
                "timestamp": None,
            })


# Singleton instance
_grounding_instance: Optional[GroundingService] = None


def get_grounding_service() -> GroundingService:
    """Get the global grounding service instance."""
    global _grounding_instance
    if _grounding_instance is None:
        _grounding_instance = GroundingService()
    return _grounding_instance


def init_grounding_service(speech_transform=None) -> GroundingService:
    """Initialize the global grounding service."""
    global _grounding_instance
    _grounding_instance = GroundingService(speech_transform=speech_transform)
    return _grounding_instance
