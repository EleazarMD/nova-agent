"""
Nova Operational Mode - Emergency privileges when approval engine is down.

This module provides graceful degradation when the central ApprovalService
(port 8407) becomes unreachable. Nova enters "operational mode" with voice-
verified privileges to maintain basic homelab functionality.

Security model:
  - Voice biometrics + session context = existing trust anchor
  - Time-bound: operational mode expires after 30 minutes
  - Scope-limited: Tier 0-3 only, no Tier 4 (admin/destructive)
  - Immutable audit: all actions logged with voice signature
  - Owner notification: push alerts for all operational actions

Architecture:
  Normal Mode: Nova → Approval Engine → Human approval → Execute
  Degraded Mode: Nova → Voice verification → Execute → Notify owner
  Emergency Mode: Critical services down → Auto-execute → Audit + alert
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp
from loguru import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Standalone approval-service microservice (JIT Zero-Tolerance Infrastructure Approvals, port 8407)
APPROVAL_SERVICE_URL = os.environ.get(
    "APPROVAL_SERVICE_URL", "http://127.0.0.1:8407"
)
DASHBOARD_API_URL = os.environ.get(
    "DASHBOARD_API_URL", "http://127.0.0.1:8407"
)

# Operational mode expires after this duration
OPERATIONAL_MODE_TIMEOUT = timedelta(minutes=30)

# Actions Nova can execute without approval engine (voice-verified)
VOICE_AGENT_OPERATIONAL_SCOPE = {
    # Diagnostics (no voice check needed - read-only)
    "service_health_check",
    "service_status",
    "service_logs",
    "test_connectivity",
    "list_processes",
    
    # Repairs (voice verification required)
    "restart_service",           # CIG, Dashboard, etc.
    "refresh_config",
    "reload_nginx",
    "clear_cache",
    "docker_restart",
    
    # Degraded mode (voice + context check)
    "start_degraded_mode",
    "disable_non_critical",
    "route_to_fallback_llm",
}

# Actions that are ALWAYS blocked, even in operational mode
TIER_4_BLOCKED_ACTIONS = {
    "erase_data",
    "factory_reset",
    "delete_database",
    "modify_security_policy",
    "grant_admin_access",
}


# ---------------------------------------------------------------------------
# Operational Mode State
# ---------------------------------------------------------------------------

class OperationalModeContext:
    """Tracks operational mode state and expiration."""
    
    def __init__(self, reason: str, voice_session_id: str):
        self.active = True
        self.reason = reason
        self.activated_at = datetime.utcnow()
        self.expires_at = self.activated_at + OPERATIONAL_MODE_TIMEOUT
        self.voice_session_id = voice_session_id
        self.actions_executed = []
        
    def is_expired(self) -> bool:
        """Check if operational mode has expired."""
        return datetime.utcnow() > self.expires_at
    
    def time_remaining(self) -> timedelta:
        """Get time remaining before expiration."""
        return max(timedelta(0), self.expires_at - datetime.utcnow())
    
    def record_action(self, action: str, details: dict[str, Any]):
        """Record an action executed in operational mode."""
        self.actions_executed.append({
            "action": action,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details,
        })


# Global operational mode state (None = normal mode)
_operational_mode: Optional[OperationalModeContext] = None


# ---------------------------------------------------------------------------
# Approval Engine Health Check
# ---------------------------------------------------------------------------

async def check_approval_engine_health() -> bool:
    """Check if approval service is reachable and healthy.
    
    Returns:
        True if approval engine is operational, False otherwise.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Use dashboard health endpoint - it serves approvals
            async with session.get(f"{APPROVAL_SERVICE_URL}/api/health") as resp:
                if resp.status == 200:
                    logger.debug("Approval engine health check: OK")
                    return True
                else:
                    logger.warning(
                        f"Approval engine health check failed: HTTP {resp.status}"
                    )
                    return False
    except asyncio.TimeoutError:
        logger.warning("Approval engine health check: timeout")
        return False
    except Exception as e:
        logger.warning(f"Approval engine health check failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Operational Mode Activation
# ---------------------------------------------------------------------------

async def enter_operational_mode(
    reason: str,
    voice_session_id: str,
) -> OperationalModeContext:
    """Activate operational mode with voice-verified privileges.
    
    Args:
        reason: Why operational mode was activated (e.g., "approval_engine_unreachable")
        voice_session_id: Current voice session ID for audit trail
        
    Returns:
        OperationalModeContext with privileges and expiration
    """
    global _operational_mode
    
    logger.warning(
        f"OPERATIONAL MODE ACTIVATED: {reason} (session: {voice_session_id})"
    )
    
    _operational_mode = OperationalModeContext(
        reason=reason,
        voice_session_id=voice_session_id,
    )
    
    # Log security event
    await _log_security_event(
        event_type="operational_mode_activated",
        reason=reason,
        voice_session_id=voice_session_id,
        expires_at=_operational_mode.expires_at.isoformat(),
    )
    
    # Notify owner via push notification
    await _notify_owner(
        title="Nova: Operational Mode Activated",
        body=f"Reason: {reason}. Voice-verified privileges active for 30 minutes.",
        data={
            "type": "operational_mode",
            "reason": reason,
            "expires_at": _operational_mode.expires_at.isoformat(),
        },
    )
    
    return _operational_mode


async def exit_operational_mode(reason: str = "normal_recovery"):
    """Deactivate operational mode and return to normal approval flow."""
    global _operational_mode
    
    if not _operational_mode:
        return
    
    logger.info(f"OPERATIONAL MODE DEACTIVATED: {reason}")
    
    # Log security event
    await _log_security_event(
        event_type="operational_mode_deactivated",
        reason=reason,
        duration_seconds=(
            datetime.utcnow() - _operational_mode.activated_at
        ).total_seconds(),
        actions_executed=len(_operational_mode.actions_executed),
    )
    
    _operational_mode = None


def get_operational_mode() -> Optional[OperationalModeContext]:
    """Get current operational mode context, if active."""
    global _operational_mode
    
    if _operational_mode and _operational_mode.is_expired():
        logger.warning("Operational mode expired, reverting to normal mode")
        asyncio.create_task(exit_operational_mode(reason="timeout"))
        return None
    
    return _operational_mode


# ---------------------------------------------------------------------------
# Action Execution with Fallback
# ---------------------------------------------------------------------------

async def execute_with_fallback(
    action: str,
    tier: int,
    voice_verified: bool = False,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Execute action with approval engine fallback to operational mode.
    
    Flow:
      1. Check if approval engine is healthy
      2. If healthy and tier >= 2: require approval (normal path)
      3. If unhealthy and tier <= 3: enter operational mode (degraded path)
      4. If tier 4: always escalate to human, never auto-execute
    
    Args:
        action: Action name (e.g., "restart_service")
        tier: Security tier (0-4)
        voice_verified: Whether voice biometrics confirmed user identity
        details: Additional context for audit trail
        
    Returns:
        Result dict with status, message, and execution details
    """
    details = details or {}
    
    # Tier 4 is ALWAYS blocked in operational mode
    if tier >= 4:
        return {
            "status": "blocked",
            "message": "Tier 4 actions require human approval via dashboard/iOS app",
            "tier": tier,
        }
    
    # Check if action is in blocked list
    if action in TIER_4_BLOCKED_ACTIONS:
        return {
            "status": "blocked",
            "message": f"Action '{action}' is permanently blocked in operational mode",
        }
    
    # Check approval engine health
    approval_healthy = await check_approval_engine_health()
    
    # Normal path: approval engine is healthy
    if approval_healthy:
        if tier >= 2:
            return {
                "status": "requires_approval",
                "message": "Approval engine is available. Please approve via dashboard or iOS app.",
                "approval_url": f"{DASHBOARD_API_URL}/infrastructure/approvals",
            }
        else:
            # Tier 0-1: auto-execute (quick approval tier)
            return {
                "status": "auto_approved",
                "message": f"Tier {tier} action auto-approved",
                "tier": tier,
            }
    
    # Degraded path: approval engine is down
    # SECURITY: Nova does NOT self-escalate. Inform user and refuse.
    logger.warning(
        f"Approval engine unreachable for tier {tier} action: {action}. Refusing — Nova cannot self-authorize."
    )
    
    await _log_security_event(
        event_type="approval_engine_unreachable",
        action=action,
        tier=tier,
        details=details,
    )
    
    return {
        "status": "blocked",
        "message": (
            f"The approval service is unreachable. Action '{action}' cannot be executed. "
            f"Please restore the approval service (port 8407) and try again, "
            f"or approve this action directly from the dashboard or iOS app once it recovers."
        ),
        "reason": "approval_engine_unreachable",
        "tier": tier,
    }


# ---------------------------------------------------------------------------
# Audit and Notification Helpers
# ---------------------------------------------------------------------------

async def _log_security_event(event_type: str, **kwargs):
    """Log security event to audit trail."""
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "event_type": event_type,
        "source": "nova_operational_mode",
        **kwargs,
    }
    
    # Log locally
    logger.warning(f"SECURITY EVENT: {event}")
    
    # Send to dashboard audit log (best effort, don't fail if unavailable)
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                f"{DASHBOARD_API_URL}/api/security/audit",
                json=event,
            )
    except Exception as e:
        logger.debug(f"Could not send audit event to dashboard: {e}")


async def _notify_owner(title: str, body: str, data: dict[str, Any]):
    """Send push notification to owner (best effort)."""
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                f"{DASHBOARD_API_URL}/api/notifications/send",
                json={
                    "title": title,
                    "body": body,
                    "data": data,
                    "priority": "high",
                },
            )
    except Exception as e:
        logger.debug(f"Could not send push notification: {e}")
