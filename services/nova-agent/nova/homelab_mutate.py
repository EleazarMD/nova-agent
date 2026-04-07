"""
Homelab infrastructure mutating operations for Nova.

All mutating operations (restart, start, stop) are gated by the homelab
approval engine (ecosystem-dashboard PostgreSQL). Nova is a CONSUMER of
the approval system, never an authority.

Approval flow:
  POST /api/security/approvals/request  → create approval (push + audit)
  GET  /api/security/approvals/{id}/status → poll for decision

The Dashboard (web) and iOS app are the ONLY surfaces where a human
can approve or deny.
"""

import asyncio
import os
from typing import Any

import aiohttp
from loguru import logger

from nova.homelab_ops import (
    MANAGED_CONTAINERS,
    PROTECTED_CONTAINERS,
    _container_exists,
    _container_status,
    _docker_exec,
    _validate_container,
)
from nova.operational_mode import (
    check_approval_engine_health,
    execute_with_fallback,
    get_operational_mode,
)


# ---------------------------------------------------------------------------
# Configuration — homelab approval engine
# ---------------------------------------------------------------------------

DASHBOARD_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
# Use dashboard approval API directly (port 8404) - standalone approval-service removed
APPROVAL_SERVICE_URL = os.environ.get("APPROVAL_SERVICE_URL", DASHBOARD_URL)
APPROVAL_SERVICE_API_KEY = os.environ.get("APPROVAL_SERVICE_API_KEY", "ai-gateway-api-key-2024")
_AGENT_JWT = os.environ.get("HERMES_JWT_TOKEN", "")
APPROVAL_POLL_INTERVAL = 3  # seconds between status polls
APPROVAL_TIMEOUT = 600      # max seconds to wait for a decision

# User ID for the primary homelab owner — approvals are routed to this user
_OWNER_USER_ID = os.environ.get("NOVA_OWNER_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")

# JIT expiry per action — Zero-Tolerance tier mapping
# Tier 2 (restart, start): 1 minute — stale context invalidates the action
# Tier 3 (stop): 5 minutes — destructive, requires deliberate response
_APPROVAL_EXPIRY_HOURS: dict[str, float] = {
    "service_restart": 1 / 60,   # 1 minute
    "service_start":   1 / 60,   # 1 minute
    "service_stop":    5 / 60,   # 5 minutes
}


# ---------------------------------------------------------------------------
# Homelab approval engine client
# ---------------------------------------------------------------------------

async def _request_approval(
    tool_name: str,
    arguments: dict,
    risk_level: str,
    context: str,
) -> dict:
    """Submit an approval request to the dashboard approval API (port 8404).
    
    Uses the unified dashboard endpoint which stores approvals in PostgreSQL
    and delivers push notifications via APNs to iOS.
    """
    url = f"{APPROVAL_SERVICE_URL}/api/approvals"
    expiry_hours = _APPROVAL_EXPIRY_HOURS.get(tool_name, 1 / 60)  # default: 1 minute
    payload = {
        "action_type": tool_name,
        "user_id": _OWNER_USER_ID,
        "agent": {
            "id": "nova-agent",
            "name": "Nova",
            "type": "voice_assistant",
        },
        "payload": arguments,
        "context": context,
        "title": f"Nova: {tool_name.replace('_', ' ').title()}",
        "ai_reasoning": context,
        "urgency": "high" if risk_level == "high" else "medium",
        "expiry_hours": expiry_hours,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers={
                "X-API-Key": APPROVAL_SERVICE_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 201:
                data = await resp.json()
                approval_id = data.get("approval_id") or data.get("approval", {}).get("id")
                logger.info(
                    f"[Approval] Requested: {tool_name} ({risk_level}) → id={approval_id}"
                )
                # Normalise response: callers expect data["id"]
                if approval_id and "id" not in data:
                    data["id"] = approval_id
                return data
            body = await resp.text()
            logger.error(f"[Approval] Request failed {resp.status}: {body}")
            raise RuntimeError(f"Approval request failed ({resp.status}): {body}")


async def _poll_approval_status(approval_id: str) -> dict:
    """Poll the dashboard approval API until a decision is made or timeout."""
    url = f"{APPROVAL_SERVICE_URL}/api/approvals/{approval_id}"
    elapsed = 0

    async with aiohttp.ClientSession() as session:
        while elapsed < APPROVAL_TIMEOUT:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"[Approval] Poll failed {resp.status}: {body}")
                    return {"status": "error", "reason": body}

                data = await resp.json()
                # approval-service wraps response in {"approval": {...}}
                approval = data.get("approval", data)
                status = approval.get("status", "pending")

                if status != "pending":
                    logger.info(
                        f"[Approval] {approval_id} resolved: {status}"
                    )
                    # Normalise: callers check data["status"] and data["decisionReason"]
                    return {
                        "status": status,
                        "decisionReason": approval.get("decision_reason") or approval.get("reviewed_by"),
                    }

            await asyncio.sleep(APPROVAL_POLL_INTERVAL)
            elapsed += APPROVAL_POLL_INTERVAL

    logger.warning(f"[Approval] {approval_id} timed out after {APPROVAL_TIMEOUT}s")
    return {"status": "expired", "reason": "Nova-side poll timeout"}


# ---------------------------------------------------------------------------
# Tool handlers — mutating (ALL require homelab approval)
# ---------------------------------------------------------------------------

async def handle_service_restart(
    container: str,
    intent: str = "",
    user_id: str = "default",
) -> str:
    """Restart a container. Requires human approval via approval-service (Tier 2, 1-minute JIT)."""
    err = _validate_container(container)
    if err:
        return err
    if not await _container_exists(container):
        return f"Container '{container}' not found."

    context = intent or f"Restart {container} to restore service health"

    # Approval required — normal path only, no self-escalation fallbacks
    try:
        fallback_result = await execute_with_fallback(
            action="restart_service",
            tier=2,
            details={"container": container, "intent": context, "user_id": user_id},
        )
    except Exception:
        fallback_result = {"status": "requires_approval"}

    if fallback_result["status"] == "blocked":
        return f"Restart of {container} blocked: {fallback_result.get('message', 'unauthorized')}"

    # Normal approval flow
    try:
        approval = await _request_approval(
            tool_name="service_restart",
            arguments={"container": container, "intent": context},
            risk_level="medium",
            context=context,
        )
    except Exception as e:
        return f"DENIED: Could not reach homelab approval engine: {e}"

    result = await _poll_approval_status(approval["id"])
    status = result.get("status", "error")

    if status != "approved":
        reason = result.get("decisionReason") or result.get("reason") or status
        return f"Restart of {container} was {status}. {reason}"

    # Human approved — execute
    _, stderr, rc = await _docker_exec("restart", container, timeout=60)
    if rc != 0:
        return f"Restart failed after approval: {stderr}"

    await asyncio.sleep(3)
    cs = await _container_status(container)
    state = cs.get("state", "unknown")
    health = cs.get("health", "none")

    result_str = f"Restarted {container} (approved). State: {state}"
    if health != "none":
        result_str += f" (health: {health})"

    logger.info(f"Service restart completed (approval {approval['id']}): {container} → {state}")
    return result_str


async def handle_service_start(
    container: str,
    intent: str = "",
    user_id: str = "default",
) -> str:
    """Start a stopped container. Requires homelab approval (medium risk)."""
    err = _validate_container(container)
    if err:
        return err
    if not await _container_exists(container):
        return f"Container '{container}' not found."

    cs = await _container_status(container)
    if cs.get("state") == "running":
        return f"{container} is already running."

    context = intent or f"Start {container} (currently stopped)"

    try:
        approval = await _request_approval(
            tool_name="service_start",
            arguments={"container": container, "intent": context},
            risk_level="medium",
            context=context,
        )
    except Exception as e:
        return f"DENIED: Could not reach homelab approval engine: {e}"

    result = await _poll_approval_status(approval["id"])
    status = result.get("status", "error")

    if status != "approved":
        reason = result.get("decisionReason") or result.get("reason") or status
        return f"Start of {container} was {status}. {reason}"

    _, stderr, rc = await _docker_exec("start", container, timeout=60)
    if rc != 0:
        return f"Start failed after approval: {stderr}"

    await asyncio.sleep(3)
    cs = await _container_status(container)
    state = cs.get("state", "unknown")
    health = cs.get("health", "none")

    result_str = f"Started {container} (approved). State: {state}"
    if health != "none":
        result_str += f" (health: {health})"

    logger.info(f"Service start completed (approval {approval['id']}): {container} → {state}")
    return result_str


async def handle_service_stop(
    container: str,
    intent: str,
    user_id: str = "default",
) -> str:
    """Stop a running container. Requires homelab approval (HIGH risk).

    The 'intent' parameter is REQUIRED — the agent must explain why
    this container needs to be stopped.
    """
    err = _validate_container(container)
    if err:
        return err

    if not intent:
        return "DENIED: You must provide an 'intent' explaining why this container needs to be stopped."

    if not await _container_exists(container):
        return f"Container '{container}' not found."

    cs = await _container_status(container)
    if cs.get("state") != "running":
        return f"{container} is not running (state: {cs.get('state', 'unknown')})."

    context = (
        f"{intent} | "
        f"Container state: {cs.get('state', '?')}, health: {cs.get('health', '?')}"
    )

    try:
        approval = await _request_approval(
            tool_name="service_stop",
            arguments={"container": container, "intent": intent},
            risk_level="high",
            context=context,
        )
    except Exception as e:
        return f"DENIED: Could not reach homelab approval engine: {e}"

    result = await _poll_approval_status(approval["id"])
    status = result.get("status", "error")

    if status != "approved":
        reason = result.get("decisionReason") or result.get("reason") or status
        return f"Stop of {container} was {status}. {reason}"

    # Human approved — execute
    _, stderr, rc = await _docker_exec("stop", container, timeout=60)
    if rc != 0:
        return f"Stop failed after approval: {stderr}"

    logger.info(f"Service stop completed (approval {approval['id']}): {container}")
    return f"Stopped {container}. Approved via homelab security."
