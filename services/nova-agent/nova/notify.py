"""
Notification bridge between EventBus and Pipecat sessions.

When an event fires (email, calendar, job), this module formats a
natural-language notification and injects it into the active Pipecat
pipeline so Nova speaks it to the user.
"""

from pipecat.frames.frames import LLMRunFrame, TranscriptionFrame
from loguru import logger

from nova.events import Event


def format_notification(event: Event) -> str | None:
    """Convert an event into a natural-language prompt for Nova to speak."""
    d = event.data

    if event.type == "email.received":
        sender = d.get("from", "someone")
        subject = d.get("subject", "no subject")
        urgent = d.get("is_urgent", False)
        if urgent:
            return f"[URGENT EMAIL] You just got an urgent email from {sender} about: {subject}. Want me to read it?"
        return f"New email from {sender}: {subject}. Want me to summarize it?"

    if event.type == "calendar.approaching":
        title = d.get("event_title", "an event")
        minutes = d.get("minutes_until", 0)
        location = d.get("location", "")
        loc_str = f" at {location}" if location else ""
        return f"Heads up — {title}{loc_str} starts in {minutes} minutes."

    if event.type == "job.completed":
        summary = d.get("summary", "Task finished.")
        return f"Your background task is done. {summary}"

    if event.type == "job.failed":
        summary = d.get("summary", "Something went wrong.")
        return f"A background task failed: {summary}"

    if event.type == "job.waiting_approval":
        desc = d.get("approval_description", "An action needs your approval.")
        return f"I need your approval: {desc}"

    # Generic fallback
    return f"Notification: {event.type} — {d.get('summary', d.get('message', str(d)))}"


def create_event_handler(task, user_id: str):
    """Create an event handler bound to a Pipecat task.

    Returns an async function suitable for EventBus.subscribe_user().
    When an event arrives, it injects a system message into the pipeline
    so Nova speaks the notification to the user.
    """

    async def handle_event(event: Event):
        text = format_notification(event)
        if not text:
            return

        logger.info(f"Notify user={user_id}: {text[:80]}")

        # Inject as a transcription frame so it goes through the LLM
        # context aggregator → LLM → TTS pipeline naturally
        frame = TranscriptionFrame(
            text=text,
            user_id="system",
            timestamp="",
        )
        await task.queue_frames([frame, LLMRunFrame()])

    return handle_event
