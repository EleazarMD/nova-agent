"""
Push notification fallback for offline users.

When an event fires but no Pipecat WebRTC session is active for the user,
send a push notification via the Dashboard's APNs proxy endpoint.
"""

import os
import aiohttp
from loguru import logger

from nova.events import Event, event_bus

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8404")
DASHBOARD_AUTH_TOKEN = os.environ.get("NOVA_AUTH_TOKEN", "")


async def send_push(user_id: str, title: str, body: str, data: dict | None = None):
    """Send a push notification via the Dashboard APNs proxy."""
    url = f"{DASHBOARD_URL}/api/push/send"
    headers = {"Content-Type": "application/json"}
    if DASHBOARD_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {DASHBOARD_AUTH_TOKEN}"

    payload = {
        "user_id": user_id,
        "title": title,
        "body": body,
        "data": data or {},
        "category": "NOVA_NOTIFICATION",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    logger.info(f"Push sent to {user_id}: {title}")
                else:
                    text = await resp.text()
                    logger.warning(f"Push failed ({resp.status}): {text[:200]}")
    except Exception as e:
        logger.error(f"Push error: {e}")


def format_push_notification(event: Event) -> tuple[str, str] | None:
    """Format an event into a push title + body. Returns None to skip."""
    d = event.data

    if event.type == "email.received":
        sender = d.get("from", "someone")
        subject = d.get("subject", "New email")
        if d.get("is_urgent"):
            return ("Urgent Email", f"From {sender}: {subject}")
        return ("New Email", f"From {sender}: {subject}")

    if event.type == "calendar.approaching":
        title = d.get("event_title", "Event")
        minutes = d.get("minutes_until", 0)
        return ("Calendar Reminder", f"{title} starts in {minutes} minutes")

    if event.type == "job.completed":
        return ("Task Complete", d.get("summary", "A background task finished."))

    if event.type == "job.failed":
        return ("Task Failed", d.get("summary", "A background task encountered an error."))

    if event.type == "job.waiting_approval":
        desc = d.get("approval_description", "Action needs your approval")
        return ("Approval Needed", desc)

    return None


# Track which users have active WebRTC sessions
_active_users: set[str] = set()


def mark_user_active(user_id: str):
    _active_users.add(user_id)


def mark_user_inactive(user_id: str):
    _active_users.discard(user_id)


def is_user_active(user_id: str) -> bool:
    return user_id in _active_users


async def offline_fallback_handler(event: Event):
    """EventBus subscriber that sends push notifications to offline users."""
    if is_user_active(event.user_id):
        return  # User is connected via WebRTC, Pipecat handles it

    notification = format_push_notification(event)
    if not notification:
        return

    title, body = notification
    await send_push(
        user_id=event.user_id,
        title=title,
        body=body,
        data={"event_type": event.type, **event.data},
    )


def register_push_fallback():
    """Register the offline push handler for all event types."""
    for topic in [
        "email.received",
        "calendar.approaching",
        "job.completed",
        "job.failed",
        "job.waiting_approval",
    ]:
        event_bus.subscribe(topic, offline_fallback_handler)
    logger.info("Push notification fallback registered for offline users")
