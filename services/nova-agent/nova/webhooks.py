"""
Webhook HTTP server for receiving external events.

Runs alongside the Pipecat bot as a lightweight FastAPI app on a separate port.
External services (CIG, Pi Agent Hub, cron) POST events here, which get
routed through the EventBus to connected Pipecat sessions.

Endpoints:
    POST /webhooks/email     — CIG email notifications
    POST /webhooks/calendar  — CIG calendar reminders
    POST /webhooks/job       — Pi Agent Hub async job status updates
    POST /webhooks/custom    — Generic event
    GET  /api/events         — SSE stream for iOS proactive notifications
    GET  /health             — Health check
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from nova.events import event_bus, Event

WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "18801"))
WEBHOOK_TOKEN = os.environ.get("NOVA_AUTH_TOKEN", "")


def verify_token(request: Request):
    """Simple bearer token auth for webhooks."""
    if not WEBHOOK_TOKEN:
        return  # no auth configured
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Webhook server ready on :{WEBHOOK_PORT}")
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nova-webhooks"}


@app.get("/api/events")
async def sse_events(request: Request, user_id: str = "default"):
    """SSE stream for iOS proactive notifications.

    The iOS app connects here on startup and receives events as they arrive.
    Events are pushed via the EventBus when webhooks fire.
    Sends a heartbeat every 30s to keep the connection alive.

    Query params:
        user_id: User to subscribe events for (default: "default")
    """
    logger.info(f"SSE /api/events: client connected, user={user_id}")

    async def event_generator():
        queue: asyncio.Queue[Event] = asyncio.Queue()

        async def _on_event(event: Event):
            await queue.put(event)

        event_bus.subscribe_user(user_id, _on_event)
        try:
            # Initial connection confirmation
            yield f"data: {json.dumps({'type': 'connected', 'user_id': user_id})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    payload = {
                        "type": event.type,
                        "user_id": event.user_id,
                        "data": event.data,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive heartbeat
                    yield f": heartbeat\n\n"
        finally:
            event_bus.unsubscribe_user(user_id, _on_event)
            logger.info(f"SSE /api/events: client disconnected, user={user_id}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/webhooks/email")
async def webhook_email(request: Request):
    """CIG sends email notifications here.

    Expected body:
    {
        "user_id": "...",
        "from": "sender@example.com",
        "subject": "...",
        "is_urgent": true,
        "preview": "First 200 chars..."
    }
    """
    verify_token(request)
    body = await request.json()
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    await event_bus.emit(Event(
        type="email.received",
        user_id=user_id,
        data={
            "from": body.get("from", "unknown"),
            "subject": body.get("subject", ""),
            "is_urgent": body.get("is_urgent", False),
            "preview": body.get("preview", ""),
        },
    ))
    return {"ok": True}


@app.post("/webhooks/calendar")
async def webhook_calendar(request: Request):
    """CIG sends calendar reminders here.

    Expected body:
    {
        "user_id": "...",
        "event_title": "Team Standup",
        "starts_at": "2026-03-08T09:00:00-06:00",
        "minutes_until": 15,
        "location": "Zoom"
    }
    """
    verify_token(request)
    body = await request.json()
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    await event_bus.emit(Event(
        type="calendar.approaching",
        user_id=user_id,
        data={
            "event_title": body.get("event_title", ""),
            "starts_at": body.get("starts_at", ""),
            "minutes_until": body.get("minutes_until", 0),
            "location": body.get("location", ""),
        },
    ))
    return {"ok": True}


@app.post("/webhooks/job")
async def webhook_job(request: Request):
    """Pi Agent Hub posts job status updates here.

    Expected body:
    {
        "user_id": "...",
        "job_id": "...",
        "status": "completed" | "failed" | "waiting_approval",
        "summary": "Found 3 showtimes at Cinepolis...",
        "approval_description": "Confirm purchase: $52?"
    }
    """
    verify_token(request)
    body = await request.json()
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    status = body.get("status", "unknown")
    event_type = f"job.{status}"

    await event_bus.emit(Event(
        type=event_type,
        user_id=user_id,
        data={
            "job_id": body.get("job_id", ""),
            "status": status,
            "summary": body.get("summary", ""),
            "approval_description": body.get("approval_description"),
        },
    ))
    return {"ok": True}


@app.post("/webhooks/custom")
async def webhook_custom(request: Request):
    """Generic event endpoint.

    Expected body:
    {
        "user_id": "...",
        "type": "event.type.here",
        "data": { ... }
    }
    """
    verify_token(request)
    body = await request.json()
    user_id = body.get("user_id", "")
    event_type = body.get("type", "")
    if not user_id or not event_type:
        raise HTTPException(status_code=400, detail="user_id and type required")

    await event_bus.emit(Event(
        type=event_type,
        user_id=user_id,
        data=body.get("data", {}),
    ))
    return {"ok": True}
