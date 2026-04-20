"""
Push Notification Service for Nova Agent.

Integrates with the dashboard's notification API to send APNs messages
to iOS devices when the app is backgrounded or for high-priority alerts.

Architecture:
- Nova Agent → Dashboard API (/api/notifications/send) → Push Service → APNs → iOS
- Device tokens stored in mobile_devices table
- Supports both immediate and scheduled notifications
"""

import asyncio
import aiohttp
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime
from loguru import logger


# Standalone approval-service microservice configuration
import os
DASHBOARD_BASE_URL = os.environ.get(
    "APPROVAL_SERVICE_URL",
    os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8407"),
)
NOTIFICATIONS_ENDPOINT = "/api/notifications/send"


@dataclass
class PushNotification:
    """Represents a push notification to be sent."""
    user_id: str
    title: str
    body: str
    priority: str = "normal"  # normal, high, urgent
    badge_count: Optional[int] = None
    sound: str = "default"
    metadata: Optional[dict] = None
    scheduled_for: Optional[datetime] = None


class PushNotificationService:
    """
    Service for sending push notifications to iOS devices.
    
    Uses the dashboard's notification API which handles:
    - Device token lookup from mobile_devices table
    - APNs connection management
    - Delivery tracking
    """
    
    def __init__(
        self,
        dashboard_url: str = DASHBOARD_BASE_URL,
        api_key: Optional[str] = None,
    ):
        self._dashboard_url = dashboard_url.rstrip("/")
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialized = False
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._session
    
    async def initialize(self):
        """Initialize the push notification service."""
        if self._initialized:
            return
        
        # Test connection to dashboard
        try:
            session = await self._get_session()
            async with session.get(f"{self._dashboard_url}/api/health") as resp:
                if resp.status == 200:
                    logger.info("PushNotificationService connected to dashboard API")
                    self._initialized = True
                else:
                    logger.warning(f"Dashboard health check failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Failed to connect to dashboard for push notifications: {e}")
            # Don't fail - push notifications are best-effort
            self._initialized = True  # Mark as initialized but will log errors
    
    async def send_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        priority: str = "normal",
        metadata: Optional[dict] = None,
        badge_count: Optional[int] = None,
    ) -> bool:
        """
        Send a push notification to a user's iOS device.
        
        Args:
            user_id: User ID to send to
            title: Notification title
            body: Notification body
            priority: normal, high, or urgent
            metadata: Additional data for the notification
            badge_count: App badge count (optional)
            
        Returns:
            True if notification was sent successfully
        """
        if not self._initialized:
            await self.initialize()
        
        # Build notification payload
        payload = {
            "userId": user_id,
            "title": title,
            "body": body,
            "priority": priority,
            "data": metadata or {},
        }
        
        if badge_count is not None:
            payload["badge"] = badge_count
        
        # Add sound for high/urgent priorities
        if priority in ("high", "urgent"):
            payload["sound"] = "alert.caf"  # Custom alert sound
        
        try:
            session = await self._get_session()
            url = f"{self._dashboard_url}{NOTIFICATIONS_ENDPOINT}"
            
            headers = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    devices_notified = result.get("devicesNotified", 0)
                    logger.info(f"Push notification sent to {devices_notified} device(s) for user={user_id}")
                    return True
                elif resp.status == 404:
                    logger.debug(f"No registered devices for user={user_id}")
                    return False
                else:
                    text = await resp.text()
                    logger.warning(f"Push notification failed: {resp.status} - {text}")
                    return False
                    
        except aiohttp.ClientError as e:
            logger.error(f"Push notification network error: {e}")
            return False
        except Exception as e:
            logger.error(f"Push notification unexpected error: {e}")
            return False
    
    async def send_to_all_devices(
        self,
        user_ids: List[str],
        title: str,
        body: str,
        priority: str = "normal",
        metadata: Optional[dict] = None,
    ) -> dict[str, bool]:
        """
        Send notification to multiple users.
        
        Returns dict of user_id -> success status
        """
        results = {}
        
        # Send in parallel
        tasks = [
            self.send_notification(
                user_id=user_id,
                title=title,
                body=body,
                priority=priority,
                metadata=metadata,
            )
            for user_id in user_ids
        ]
        
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        
        for user_id, result in zip(user_ids, completed):
            if isinstance(result, Exception):
                logger.error(f"Push notification error for {user_id}: {result}")
                results[user_id] = False
            else:
                results[user_id] = result
        
        return results
    
    async def get_device_status(self, user_id: str) -> dict:
        """
        Get push notification status for a user's devices.
        
        Returns info about registered devices and last notification time.
        """
        try:
            session = await self._get_session()
            url = f"{self._dashboard_url}/api/notifications/devices/{user_id}"
            
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return {"error": f"Status {resp.status}"}
        except Exception as e:
            return {"error": str(e)}
    
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# Singleton instance
_push_service: Optional[PushNotificationService] = None


async def get_push_service() -> PushNotificationService:
    """Get the global push notification service instance."""
    global _push_service
    if _push_service is None:
        _push_service = PushNotificationService()
        await _push_service.initialize()
    return _push_service


async def send_push_notification(
    user_id: str,
    title: str,
    body: str,
    priority: str = "normal",
    metadata: Optional[dict] = None,
) -> bool:
    """
    Convenience function to send a push notification.
    
    Example:
        await send_push_notification(
            user_id="user-123",
            title="📧 New Email",
            body="From boss: Urgent meeting in 5 min",
            priority="urgent",
        )
    """
    service = await get_push_service()
    return await service.send_notification(
        user_id=user_id,
        title=title,
        body=body,
        priority=priority,
        metadata=metadata,
    )
