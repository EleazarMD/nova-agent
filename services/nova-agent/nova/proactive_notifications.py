"""
Proactive Notification System for Nova Agent.

Integrates EventBus with push notifications and real-time streaming.
Provides:
- Calendar alerts (meeting approaching)
- Email notifications (urgent emails)
- Job completions (Pi Agent Hub, background tasks)
- Service alerts (system issues)
- Tesla status (charge complete, climate ready)

Emits to:
- WebRTC data channel (real-time when app open)
- SSE mirror (Tesla companion display)
- Push notifications (APNs when app backgrounded)
"""

import asyncio
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta
from loguru import logger

from nova.events import Event, event_bus


@dataclass
class NotificationConfig:
    """Configuration for notification throttling and priorities."""
    min_interval_seconds: int = 60  # Don't spam user
    calendar_alert_minutes: int = 10  # Alert 10 min before meeting
    urgent_email_only: bool = True  # Only push urgent emails
    tesla_charge_alert: bool = True  # Alert when charging complete


class ProactiveNotifier:
    """
    Manages proactive notifications to users across multiple channels.
    
    Channels:
    1. Real-time WebRTC (when iOS app is foreground)
    2. SSE Mirror (Tesla companion always sees these)
    3. Push notifications (APNs when app is backgrounded)
    """
    
    def __init__(
        self,
        user_id: str,
        server_msg_fn=None,  # WebRTC data channel
        mirror_publish_fn=None,  # Tesla SSE
        push_notify_fn=None,  # APNs
        config: Optional[NotificationConfig] = None,
    ):
        self.user_id = user_id
        self._server_msg = server_msg_fn
        self._mirror = mirror_publish_fn
        self._push = push_notify_fn
        self._config = config or NotificationConfig()
        
        # Throttling tracking
        self._last_notification_time: Optional[datetime] = None
        self._recent_notifications: list[str] = []  # Dedup cache
    
    def _should_notify(self, notification_type: str) -> bool:
        """Check throttling rules before sending."""
        now = datetime.now()
        
        # Rate limiting
        if self._last_notification_time:
            elapsed = (now - self._last_notification_time).total_seconds()
            if elapsed < self._config.min_interval_seconds:
                logger.debug(f"Notification throttled: {notification_type} ({elapsed:.0f}s < {self._config.min_interval_seconds}s)")
                return False
        
        # Dedup check (same notification in last 5 minutes)
        if notification_type in self._recent_notifications:
            logger.debug(f"Notification deduped: {notification_type}")
            return False
        
        return True
    
    def _record_notification(self, notification_type: str):
        """Record notification for throttling/dedup."""
        self._last_notification_time = datetime.now()
        self._recent_notifications.append(notification_type)
        
        # Trim dedup cache (keep last 20)
        if len(self._recent_notifications) > 20:
            self._recent_notifications = self._recent_notifications[-20:]
    
    async def _send_to_all_channels(
        self,
        title: str,
        body: str,
        priority: str = "normal",  # normal, high, urgent
        metadata: Optional[dict] = None,
    ):
        """Send notification across all active channels."""
        
        # 1. WebRTC data channel (if app is open)
        if self._server_msg:
            try:
                await self._server_msg({
                    "type": "proactive_notification",
                    "title": title,
                    "body": body,
                    "priority": priority,
                    "timestamp": datetime.now().isoformat(),
                    "metadata": metadata or {},
                })
                logger.info(f"[WebRTC] Proactive notification: {title}")
            except Exception as e:
                logger.warning(f"[WebRTC] Failed to send notification: {e}")
        
        # 2. Tesla SSE mirror (always visible in car)
        if self._mirror:
            try:
                await self._mirror(self.user_id, "notification", {
                    "title": title,
                    "body": body,
                    "priority": priority,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info(f"[Mirror] Proactive notification: {title}")
            except Exception as e:
                logger.warning(f"[Mirror] Failed to send notification: {e}")
        
        # 3. Push notification (if app backgrounded)
        if self._push and priority in ("high", "urgent"):
            try:
                await self._push(
                    user_id=self.user_id,
                    title=title,
                    body=body,
                    priority=priority,
                    metadata=metadata,
                )
                logger.info(f"[Push] Proactive notification: {title}")
            except Exception as e:
                logger.warning(f"[Push] Failed to send notification: {e}")
    
    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------
    
    async def on_email_received(self, event: Event):
        """Handle new email notification."""
        if not self._config.urgent_email_only:
            return
        
        data = event.data
        if not data.get("is_urgent") and not data.get("is_important"):
            return
        
        notification_type = f"email:{data.get('message_id', 'unknown')}"
        if not self._should_notify(notification_type):
            return
        
        sender = data.get("from", "Someone")
        subject = data.get("subject", "New message")
        
        await self._send_to_all_channels(
            title="📧 Urgent Email",
            body=f"From {sender}: {subject}",
            priority="high" if data.get("is_urgent") else "normal",
            metadata={
                "type": "email",
                "sender": sender,
                "subject": subject,
                "message_id": data.get("message_id"),
            },
        )
        self._record_notification(notification_type)
    
    async def on_calendar_approaching(self, event: Event):
        """Handle upcoming meeting alert."""
        data = event.data
        minutes = data.get("minutes_until", 0)
        
        # Only alert if within configured window
        if minutes > self._config.calendar_alert_minutes:
            return
        
        event_id = data.get("event_id", "unknown")
        notification_type = f"calendar:{event_id}"
        
        if not self._should_notify(notification_type):
            return
        
        title = data.get("event_title", "Meeting")
        location = data.get("location", "")
        
        loc_str = f" at {location}" if location else ""
        body = f"{title}{loc_str} starts in {minutes} minutes"
        
        await self._send_to_all_channels(
            title="📅 Meeting Alert",
            body=body,
            priority="high" if minutes <= 5 else "normal",
            metadata={
                "type": "calendar",
                "event_id": event_id,
                "title": title,
                "location": location,
                "minutes_until": minutes,
            },
        )
        self._record_notification(notification_type)
    
    async def on_job_completed(self, event: Event):
        """Handle background job completion."""
        data = event.data
        job_id = data.get("job_id", "unknown")
        notification_type = f"job:{job_id}"
        
        # Don't throttle completions - user wants to know
        summary = data.get("summary", "Task finished")
        
        await self._send_to_all_channels(
            title="✅ Task Complete",
            body=summary,
            priority="normal",
            metadata={
                "type": "job_completed",
                "job_id": job_id,
                "summary": summary,
            },
        )
        self._record_notification(notification_type)
    
    async def on_job_failed(self, event: Event):
        """Handle background job failure."""
        data = event.data
        job_id = data.get("job_id", "unknown")
        
        summary = data.get("summary", "Task failed")
        
        await self._send_to_all_channels(
            title="❌ Task Failed",
            body=summary,
            priority="high",
            metadata={
                "type": "job_failed",
                "job_id": job_id,
                "error": data.get("error"),
            },
        )
        self._record_notification(f"job:{job_id}")
    
    async def on_job_waiting_approval(self, event: Event):
        """Handle approval required notification."""
        data = event.data
        approval_id = data.get("approval_id", "unknown")
        
        desc = data.get("approval_description", "Action needs approval")
        
        await self._send_to_all_channels(
            title="⚠️ Approval Required",
            body=desc,
            priority="urgent",
            metadata={
                "type": "approval_required",
                "approval_id": approval_id,
                "description": desc,
            },
        )
    
    async def on_tesla_charge_complete(self, event: Event):
        """Handle Tesla charging completion."""
        if not self._config.tesla_charge_alert:
            return
        
        data = event.data
        vin = data.get("vin", "unknown")
        notification_type = f"tesla:charge:{vin}"
        
        if not self._should_notify(notification_type):
            return
        
        battery_level = data.get("battery_level", "unknown")
        
        await self._send_to_all_channels(
            title="🔋 Charge Complete",
            body=f"Your Tesla is charged to {battery_level}%",
            priority="normal",
            metadata={
                "type": "tesla_charge_complete",
                "vin": vin,
                "battery_level": battery_level,
            },
        )
        self._record_notification(notification_type)
    
    async def on_service_alert(self, event: Event):
        """Handle system/service health alert."""
        data = event.data
        service = data.get("service_name", "Unknown service")
        severity = data.get("severity", "warning")  # warning, error, critical
        
        # Always notify on critical, throttle warnings
        if severity == "warning" and not self._should_notify(f"service:{service}"):
            return
        
        status = data.get("status", "has an issue")
        
        priority_map = {
            "critical": "urgent",
            "error": "high",
            "warning": "normal",
        }
        
        await self._send_to_all_channels(
            title=f"🔧 {service.title()}",
            body=f"Service {status}",
            priority=priority_map.get(severity, "normal"),
            metadata={
                "type": "service_alert",
                "service": service,
                "severity": severity,
                "status": status,
            },
        )
        if severity == "warning":
            self._record_notification(f"service:{service}")
    
    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------
    
    def subscribe_all(self):
        """Subscribe to all relevant event types."""
        event_bus.subscribe("email.received", self.on_email_received)
        event_bus.subscribe("calendar.approaching", self.on_calendar_approaching)
        event_bus.subscribe("job.completed", self.on_job_completed)
        event_bus.subscribe("job.failed", self.on_job_failed)
        event_bus.subscribe("job.waiting_approval", self.on_job_waiting_approval)
        event_bus.subscribe("tesla.charge_complete", self.on_tesla_charge_complete)
        event_bus.subscribe("service.alert", self.on_service_alert)
        
        logger.info(f"ProactiveNotifier subscribed for user={self.user_id}")
    
    def unsubscribe_all(self):
        """Unsubscribe from all events."""
        event_bus.unsubscribe("email.received", self.on_email_received)
        event_bus.unsubscribe("calendar.approaching", self.on_calendar_approaching)
        event_bus.unsubscribe("job.completed", self.on_job_completed)
        event_bus.unsubscribe("job.failed", self.on_job_failed)
        event_bus.unsubscribe("job.waiting_approval", self.on_job_waiting_approval)
        event_bus.unsubscribe("tesla.charge_complete", self.on_tesla_charge_complete)
        event_bus.unsubscribe("service.alert", self.on_service_alert)
        
        logger.info(f"ProactiveNotifier unsubscribed for user={self.user_id}")


def create_proactive_notifier(
    user_id: str,
    server_msg_fn=None,
    mirror_publish_fn=None,
    push_notify_fn=None,
    config: Optional[NotificationConfig] = None,
) -> ProactiveNotifier:
    """Factory function to create and configure a ProactiveNotifier."""
    notifier = ProactiveNotifier(
        user_id=user_id,
        server_msg_fn=server_msg_fn,
        mirror_publish_fn=mirror_publish_fn,
        push_notify_fn=push_notify_fn,
        config=config,
    )
    notifier.subscribe_all()
    return notifier
