"""
Event bus for Nova Agent.

Lightweight async pub/sub that routes external events (email, calendar,
job completion) to connected Pipecat sessions so the agent can proactively
notify the user.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional
from loguru import logger


@dataclass
class Event:
    type: str  # "email.received", "calendar.approaching", "job.completed", etc.
    user_id: str
    data: dict = field(default_factory=dict)


# Subscriber = async function that receives an Event
Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """Simple async event bus with topic-based routing."""

    def __init__(self):
        # topic → list of subscribers
        self._subscribers: dict[str, list[Subscriber]] = {}
        # user_id → list of subscribers (catch-all for a user)
        self._user_subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(self, topic: str, handler: Subscriber):
        """Subscribe to a specific event topic."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(handler)
        logger.debug(f"EventBus: subscribed to '{topic}'")

    def unsubscribe(self, topic: str, handler: Subscriber):
        """Remove a subscription."""
        if topic in self._subscribers:
            self._subscribers[topic] = [h for h in self._subscribers[topic] if h is not handler]

    def subscribe_user(self, user_id: str, handler: Subscriber):
        """Subscribe to all events for a specific user."""
        if user_id not in self._user_subscribers:
            self._user_subscribers[user_id] = []
        self._user_subscribers[user_id].append(handler)
        logger.debug(f"EventBus: user '{user_id}' subscribed")

    def unsubscribe_user(self, user_id: str, handler: Subscriber):
        """Remove a user subscription."""
        if user_id in self._user_subscribers:
            self._user_subscribers[user_id] = [
                h for h in self._user_subscribers[user_id] if h is not handler
            ]

    async def emit(self, event: Event):
        """Emit an event to all matching subscribers."""
        handlers = []

        # Topic subscribers
        if event.type in self._subscribers:
            handlers.extend(self._subscribers[event.type])

        # Wildcard topic subscribers (e.g., "email.*" matches "email.received")
        prefix = event.type.split(".")[0] + ".*"
        if prefix in self._subscribers:
            handlers.extend(self._subscribers[prefix])

        # User-specific catch-all subscribers
        if event.user_id in self._user_subscribers:
            handlers.extend(self._user_subscribers[event.user_id])

        if not handlers:
            logger.debug(f"EventBus: no subscribers for '{event.type}' user='{event.user_id}'")
            return

        logger.info(f"EventBus: emitting '{event.type}' to {len(handlers)} handler(s)")
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"EventBus handler error for '{event.type}': {e}")


# Singleton
event_bus = EventBus()
