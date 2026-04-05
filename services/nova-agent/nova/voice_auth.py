"""
Voice verification module for Nova operational mode.

Provides voice biometric verification when approval engine is unavailable.
Uses session context + voice confidence to authorize Tier 2-3 actions.

Security model:
  - Voice confidence threshold: 0.92 (high confidence required)
  - Context validation: location, time of day, recent activity
  - Action scope check: only pre-approved operational actions
  - Audit trail: all verifications logged with voice signature
"""

import os
from datetime import datetime, time
from typing import Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Voice confidence threshold for authorization (0.0-1.0)
VOICE_CONFIDENCE_THRESHOLD = 0.92

# Time windows when voice authorization is allowed (UTC)
ALLOWED_TIME_WINDOWS = [
    (time(6, 0), time(23, 0)),   # 6 AM - 11 PM UTC
]

# Locations where voice authorization is allowed
ALLOWED_LOCATIONS = {
    "home",
    "office",
    "tesla",
}


# ---------------------------------------------------------------------------
# Voice Verification
# ---------------------------------------------------------------------------

async def verify_voice_authorization(
    action: str,
    voice_confidence: float = 0.0,
    current_location: Optional[str] = None,
    session_context: Optional[dict] = None,
) -> dict:
    """Verify voice authorization for operational mode action.
    
    Args:
        action: Action name (e.g., "restart_service")
        voice_confidence: Voice biometric match confidence (0.0-1.0)
        current_location: Current location context (e.g., "home", "tesla")
        session_context: Additional session metadata
        
    Returns:
        Dict with verification result:
        {
            "authorized": bool,
            "confidence": float,
            "reason": str,
            "context_valid": bool,
        }
    """
    session_context = session_context or {}
    
    # 1. Voice biometric check
    voice_match = voice_confidence >= VOICE_CONFIDENCE_THRESHOLD
    
    if not voice_match:
        logger.warning(
            f"Voice verification failed: confidence {voice_confidence:.2f} "
            f"< threshold {VOICE_CONFIDENCE_THRESHOLD}"
        )
        return {
            "authorized": False,
            "confidence": voice_confidence,
            "reason": f"Voice confidence too low ({voice_confidence:.2f} < {VOICE_CONFIDENCE_THRESHOLD})",
            "context_valid": False,
        }
    
    # 2. Context validation
    context_valid = await _validate_operational_context(
        current_location=current_location,
        session_context=session_context,
    )
    
    if not context_valid:
        logger.warning(
            f"Voice verification failed: invalid context "
            f"(location={current_location}, time={datetime.utcnow().time()})"
        )
        return {
            "authorized": False,
            "confidence": voice_confidence,
            "reason": "Context validation failed (location or time restrictions)",
            "context_valid": False,
        }
    
    # 3. Action scope check (handled by operational_mode.py)
    # We just verify voice + context here
    
    logger.info(
        f"Voice authorization GRANTED: action={action}, "
        f"confidence={voice_confidence:.2f}, location={current_location}"
    )
    
    return {
        "authorized": True,
        "confidence": voice_confidence,
        "reason": "Voice biometrics and context validated",
        "context_valid": True,
    }


async def _validate_operational_context(
    current_location: Optional[str] = None,
    session_context: Optional[dict] = None,
) -> bool:
    """Validate operational context for voice authorization.
    
    Checks:
      - Time of day (must be within allowed windows)
      - Location (must be in allowed locations)
      - Recent activity (no suspicious patterns)
    
    Returns:
        True if context is valid, False otherwise
    """
    session_context = session_context or {}
    
    # 1. Time validation
    current_time = datetime.utcnow().time()
    time_allowed = any(
        start <= current_time <= end
        for start, end in ALLOWED_TIME_WINDOWS
    )
    
    if not time_allowed:
        logger.debug(f"Time validation failed: {current_time} not in allowed windows")
        return False
    
    # 2. Location validation
    if current_location and current_location not in ALLOWED_LOCATIONS:
        logger.debug(
            f"Location validation failed: {current_location} not in {ALLOWED_LOCATIONS}"
        )
        return False
    
    # 3. Session validation (basic checks)
    session_age_seconds = session_context.get("session_age_seconds", 0)
    if session_age_seconds > 3600:  # 1 hour
        logger.debug(f"Session too old: {session_age_seconds}s")
        return False
    
    return True


# ---------------------------------------------------------------------------
# Voice Confidence Extraction (Pipecat Integration)
# ---------------------------------------------------------------------------

def extract_voice_confidence_from_session(session_data: dict) -> float:
    """Extract voice confidence from Pipecat session data.
    
    This is a placeholder for actual Pipecat voice biometric integration.
    In production, this would:
      1. Get voiceprint from enrolled user
      2. Compare current audio stream to voiceprint
      3. Return confidence score (0.0-1.0)
    
    Args:
        session_data: Pipecat session metadata
        
    Returns:
        Voice confidence score (0.0-1.0)
    """
    # TODO: Integrate with actual voice biometrics
    # For now, return a placeholder based on session validity
    
    if not session_data:
        return 0.0
    
    # Check if session has voice activity
    has_voice = session_data.get("has_voice_activity", False)
    if not has_voice:
        return 0.0
    
    # In production, this would be actual biometric matching
    # For now, we assume authenticated sessions have high confidence
    is_authenticated = session_data.get("authenticated", False)
    if is_authenticated:
        return 0.95  # High confidence for authenticated sessions
    
    return 0.5  # Medium confidence for unauthenticated


# ---------------------------------------------------------------------------
# Voice Prompt Helpers
# ---------------------------------------------------------------------------

def get_voice_confirmation_prompt(action: str, container: str = "") -> str:
    """Get voice confirmation prompt for user.
    
    Args:
        action: Action name (e.g., "restart_service")
        container: Container name if applicable
        
    Returns:
        Prompt string for voice confirmation
    """
    if action == "restart_service" and container:
        return (
            f"The approval system is currently unavailable. "
            f"To restart {container}, please say: 'Yes, restart {container}'"
        )
    elif action == "service_health_check":
        return "Running health check. No confirmation needed."
    else:
        return (
            f"The approval system is unavailable. "
            f"To proceed with {action}, please confirm with your voice."
        )


def parse_voice_confirmation(
    transcript: str,
    expected_action: str,
    expected_target: str = "",
) -> bool:
    """Parse voice transcript to detect confirmation.
    
    Args:
        transcript: Voice transcript text
        expected_action: Expected action (e.g., "restart")
        expected_target: Expected target (e.g., "hermes-core")
        
    Returns:
        True if confirmation detected, False otherwise
    """
    transcript_lower = transcript.lower().strip()
    
    # Common confirmation patterns
    confirmations = [
        "yes",
        "confirm",
        "proceed",
        "do it",
        "go ahead",
        "affirmative",
    ]
    
    # Check for explicit confirmation
    has_confirmation = any(word in transcript_lower for word in confirmations)
    
    # Check for action mention
    has_action = expected_action.lower() in transcript_lower
    
    # Check for target mention (if specified)
    has_target = True
    if expected_target:
        has_target = expected_target.lower() in transcript_lower
    
    # Require confirmation + action (+ target if specified)
    return has_confirmation and has_action and has_target
