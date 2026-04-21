"""Nova Hatchet worker — Phase 3.

Two workflows that let cross-service DAGs (Hermes, CIG triage, Argus, etc.)
*speak through Nova* without reaching into Nova's in-process event bus:

  - `nova.notifyUser` — fire-and-forget push / iOS banner delivery.
      Input: { user_id, title, body, priority?, metadata? }
      Output: { delivered_devices: int, via: [channels...] }
      Routes: dashboard APNs (always) + Nova event_bus.emit() when the
      worker is colocated with the bot (hands the same notification to
      an active WebRTC session so Nova speaks it too).

  - `nova.askUser` — blocking question with a human-in-the-loop answer.
      Input: { user_id, question, context?, options?, timeout_s?, priority? }
      Output: { status: "answered" | "denied" | "expired",
                answer: str | None,       # the user's typed/dictated answer
                resolved_by: str | None,
                resolved_at: ISO }
      Mechanism: creates an approval-service request with
      `action_type="agent_question"` and the question in the payload.
      Hyperspace iOS renders it as an Ask card and the user responds
      (approve = their answer in decision_reason, deny = refusal).
      The workflow polls the approval-service until resolution or
      configurable timeout (default 10 min).

Neither workflow reaches into Nova's process directly — both are pure
HTTP callers so they can run out-of-band of the Pipecat bot's event
loop. The bot *additionally* subscribes to the same event_bus topic in
process when available, which gives it a chance to interject voice
before the iOS notification arrives.

Runs as a standalone process (`nova-workflows.service`) because the
Hatchet action listener is long-lived and shouldn't share its event
loop with pipecat's audio pipeline.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

import httpx
from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("nova-workflows")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Dashboard is the system-of-record for device registration + APNs delivery.
# Reuse the same endpoint Nova's in-process PushNotificationService hits
# (services/nova-agent/services/nova-agent/nova/push_notifications.py).
DASHBOARD_URL = os.environ.get(
    "NOVA_DASHBOARD_URL",
    os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8404"),
).rstrip("/")
DASHBOARD_NOTIFICATIONS_ENDPOINT = os.environ.get(
    "NOVA_DASHBOARD_NOTIFICATIONS_ENDPOINT",
    "/api/notifications/send",
)
DASHBOARD_API_KEY = os.environ.get("NOVA_DASHBOARD_API_KEY", "")

APPROVAL_SERVICE_URL = os.environ.get(
    "APPROVAL_SERVICE_URL", "http://127.0.0.1:8407"
).rstrip("/")
APPROVAL_SERVICE_API_KEY = os.environ.get(
    "APPROVAL_SERVICE_API_KEY",
    os.environ.get("PI_HUB_APPROVAL_API_KEY", ""),
)

# Matches the poll cadence CIG's `request_approval` helper uses so the iOS
# latency characteristics are identical regardless of which worker asks.
_APPROVAL_POLL_INTERVAL_S = 3.0

# ---------------------------------------------------------------------------
# Hatchet client
# ---------------------------------------------------------------------------
hatchet = Hatchet()


# ---------------------------------------------------------------------------
# Audit metadata shared with the CIG worker. Keeping the shape identical
# means the hub's workflows.* RPC gateway (pi-agent-hub/src/index.ts) can
# use the same `additionalMetadata` map unchanged across runtimes.
# ---------------------------------------------------------------------------
def _audit_meta(ctx: Context) -> dict[str, str]:
    md = ctx.additional_metadata or {}
    return {
        "user_id": str(md.get("user_id", "system")),
        "request_id": str(md.get("request_id", ctx.workflow_run_id)),
        "source_client": str(md.get("source_client", "direct")),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# nova.notifyUser
# ===========================================================================
class NotifyUserInput(BaseModel):
    user_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=2000)
    # Maps to APS `interruption-level`:
    #   normal → passive (suppressed in focus)
    #   high   → active  (banner + sound)
    #   urgent → time-sensitive (bypasses most focus modes)
    priority: str = Field(default="normal", pattern="^(normal|high|urgent)$")
    # Free-form metadata delivered to the iOS app's userInfo payload.
    # Useful for deep-linking the notification back to its source (e.g.
    # {"type": "email", "email_id": "..."} so tapping opens Hermes).
    metadata: dict = Field(default_factory=dict)


notify_wf = hatchet.workflow(
    name="nova.notifyUser",
    description=(
        "Fire-and-forget notification for a user: dashboard APNs push + "
        "(when Nova is connected) in-process event_bus delivery so the "
        "voice pipeline speaks the message aloud. Tier 0 — no approval. "
        "Use when a background DAG has information the user needs but "
        "doesn't need a decision from them. For questions, use "
        "`nova.askUser` instead."
    ),
    input_validator=NotifyUserInput,
)


async def _dashboard_push(
    *,
    user_id: str,
    title: str,
    body: str,
    priority: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """POST to the dashboard's /api/notifications/send endpoint.

    Returns the parsed response body (success path includes
    `devicesNotified`). Raises on network/5xx so Hatchet can retry.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if DASHBOARD_API_KEY:
        headers["X-API-Key"] = DASHBOARD_API_KEY

    payload: dict[str, Any] = {
        "userId": user_id,
        "title": title,
        "body": body,
        "priority": priority,
        "data": metadata,
    }
    if priority in ("high", "urgent"):
        payload["sound"] = "alert.caf"

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.post(
            f"{DASHBOARD_URL}{DASHBOARD_NOTIFICATIONS_ENDPOINT}",
            json=payload,
        )
        if resp.status_code == 404:
            # No registered iOS device — not an error, just nothing to do.
            return {"devicesNotified": 0, "reason": "no_devices"}
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}


@notify_wf.task(retries=2)
async def notify(input: NotifyUserInput, ctx: Context) -> dict[str, Any]:
    meta = _audit_meta(ctx)
    via: list[str] = []
    devices = 0
    push_error: str | None = None

    try:
        result = await _dashboard_push(
            user_id=input.user_id,
            title=input.title,
            body=input.body,
            priority=input.priority,
            metadata=input.metadata,
        )
        devices = int(result.get("devicesNotified") or 0)
        via.append("apns")
        log.info(
            "notifyUser: user=%s priority=%s devices=%d title=%r [user_id=%s]",
            input.user_id, input.priority, devices, input.title[:50],
            meta["user_id"],
        )
    except httpx.HTTPError as e:
        push_error = f"apns_error: {e!s}"
        log.warning(
            "notifyUser: APNs delivery failed user=%s err=%s [user_id=%s]",
            input.user_id, e, meta["user_id"],
        )

    return {
        "user_id": input.user_id,
        "delivered_devices": devices,
        "via": via,
        "error": push_error,
        "priority": input.priority,
        **meta,
    }


# ===========================================================================
# nova.askUser
# ===========================================================================
class AskUserInput(BaseModel):
    user_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=2000)
    # Optional context shown to the user alongside the question (e.g.
    # "triggered because your flight was delayed — reschedule?").
    context: str = ""
    # When non-empty, the iOS app can render this as a multiple-choice
    # question; otherwise it's a free-text reply in decision_reason.
    options: list[str] = Field(default_factory=list)
    # Minutes until the question expires unanswered. Keep modest — if the
    # user doesn't answer within this window the DAG shouldn't hang.
    expiry_minutes: float = Field(default=10.0, ge=0.5, le=60.0)
    priority: str = Field(default="high", pattern="^(normal|high|urgent)$")


ask_wf = hatchet.workflow(
    name="nova.askUser",
    description=(
        "Blocking question to the user, answered via Hyperspace iOS. "
        "Creates an approval-service record with "
        "`action_type='agent_question'` and polls until the user taps "
        "Approve (carrying their answer in decision_reason) or Deny. "
        "Tier 3-ish — always requires human interaction. Use sparingly: "
        "most cross-service flows should prefer `nova.notifyUser` + a "
        "workspace :Task entry so the user can answer async."
    ),
    input_validator=AskUserInput,
)


class _AskResult(NamedTuple):
    status: str  # "answered" | "denied" | "expired"
    answer: str | None
    resolved_by: str | None
    resolved_at: str | None
    approval_id: str


async def _create_and_poll_approval(
    *,
    user_id: str,
    question: str,
    context: str,
    options: list[str],
    priority: str,
    expiry_minutes: float,
    request_id: str,
) -> _AskResult:
    """Create the approval and poll until resolved or expired.

    The approval-service side is the same service CIG's send-email and
    the hub's approval RPCs use — see approval-service/src/types.ts
    for the action_type catalog. `agent_question` must be registered
    there for Hyperspace iOS to render it.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if APPROVAL_SERVICE_API_KEY:
        headers["X-API-Key"] = APPROVAL_SERVICE_API_KEY
    if request_id:
        headers["X-Request-Id"] = request_id

    body = {
        "action_type": "agent_question",
        "user_id": user_id,
        "agent": {"id": "nova", "name": "Nova", "type": "nova"},
        "payload": {
            "question": question,
            "context": context,
            "options": options,
            "request_id": request_id,
        },
        "context": context or question,
        "title": "Nova: question",
        "ai_reasoning": context,
        "urgency": priority,
        "expiry_hours": expiry_minutes / 60.0,
    }

    timeout_s = expiry_minutes * 60.0
    started = time.monotonic()

    async with httpx.AsyncClient(
        base_url=APPROVAL_SERVICE_URL, headers=headers, timeout=10.0
    ) as client:
        created = await client.post("/api/approvals", json=body)
        if created.status_code >= 400:
            raise RuntimeError(
                f"approval-service POST /api/approvals failed: "
                f"{created.status_code} {created.text[:200]}"
            )
        data = created.json()
        approval_id = (
            (data.get("approval") or {}).get("id")
            or data.get("approval_id")
            or data.get("id")
            or str(uuid.uuid4())
        )
        log.info(
            "askUser: approval %s created for user=%s request_id=%s",
            approval_id, user_id, request_id,
        )

        while time.monotonic() - started < timeout_s:
            await asyncio.sleep(_APPROVAL_POLL_INTERVAL_S)
            try:
                poll = await client.get(f"/api/approvals/{approval_id}")
            except httpx.RequestError:
                continue
            if poll.status_code != 200:
                continue
            pd = poll.json()
            approval = pd.get("approval", pd)
            status = approval.get("status")
            if status in ("approved", "denied", "rejected"):
                ans = approval.get("decision_reason") or ""
                norm_status = "answered" if status == "approved" else "denied"
                return _AskResult(
                    status=norm_status,
                    answer=ans if norm_status == "answered" else None,
                    resolved_by=approval.get("reviewed_by")
                    or approval.get("resolved_by"),
                    resolved_at=_now_iso(),
                    approval_id=approval_id,
                )
            if status == "expired":
                break

    return _AskResult(
        status="expired",
        answer=None,
        resolved_by=None,
        resolved_at=_now_iso(),
        approval_id=approval_id,
    )


@ask_wf.task(execution_timeout=timedelta(minutes=15), retries=0)
async def ask(input: AskUserInput, ctx: Context) -> dict[str, Any]:
    meta = _audit_meta(ctx)
    result = await _create_and_poll_approval(
        user_id=input.user_id,
        question=input.question,
        context=input.context,
        options=input.options,
        priority=input.priority,
        expiry_minutes=input.expiry_minutes,
        request_id=meta["request_id"],
    )
    log.info(
        "askUser: user=%s status=%s answer=%r [user_id=%s request_id=%s]",
        input.user_id, result.status,
        (result.answer or "")[:80],
        meta["user_id"], meta["request_id"],
    )
    return {
        "user_id": input.user_id,
        "status": result.status,
        "answer": result.answer,
        "resolved_by": result.resolved_by,
        "resolved_at": result.resolved_at,
        "approval_id": result.approval_id,
        **meta,
    }


# ===========================================================================
# Worker bootstrap
# ===========================================================================
def main() -> None:
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        log.error("HATCHET_CLIENT_TOKEN unset — aborting")
        sys.exit(1)

    worker = hatchet.worker(
        name="nova-workflows-worker",
        slots=5,
        workflows=[notify_wf, ask_wf],
    )
    log.info(
        "nova-workflows-worker starting: workflows=%s",
        [notify_wf.name, ask_wf.name],
    )
    worker.start()


if __name__ == "__main__":
    main()
