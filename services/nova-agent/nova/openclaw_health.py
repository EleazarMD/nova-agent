"""
OpenClaw delegation health monitoring and validation.

Ensures Nova-OpenClaw integration remains stable and provides
early detection of configuration or connectivity issues.
"""

import os
import asyncio
import aiohttp
from typing import Dict, Optional, Tuple
from loguru import logger
from datetime import datetime, timedelta


class OpenClawHealthMonitor:
    """Monitor OpenClaw gateway health and delegation readiness."""
    
    def __init__(self):
        self.last_check: Optional[datetime] = None
        self.last_status: Optional[Dict] = None
        self.consecutive_failures = 0
        self.check_interval = timedelta(minutes=5)
        
    def get_config(self) -> Tuple[str, str]:
        """Get OpenClaw configuration from environment (runtime read)."""
        url = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18790")
        token = os.environ.get("OPENCLAW_TOKEN", "")
        return url, token
    
    async def check_health(self) -> Dict:
        """
        Comprehensive health check for OpenClaw delegation.
        
        Returns:
            {
                "healthy": bool,
                "url": str,
                "token_configured": bool,
                "gateway_reachable": bool,
                "auth_valid": bool,
                "response_time_ms": float,
                "error": Optional[str],
                "timestamp": str
            }
        """
        url, token = self.get_config()
        
        result = {
            "healthy": False,
            "url": url,
            "token_configured": bool(token),
            "gateway_reachable": False,
            "auth_valid": False,
            "response_time_ms": None,
            "error": None,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Check 1: Token configured
        if not token:
            result["error"] = "OPENCLAW_TOKEN not configured"
            logger.warning("[OpenClaw Health] Token not configured")
            return result
        
        # Check 2: Gateway reachability and auth
        try:
            start = asyncio.get_event_loop().time()
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{url}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "messages": [{"role": "user", "content": "health check"}],
                        "stream": False
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    elapsed = (asyncio.get_event_loop().time() - start) * 1000
                    result["response_time_ms"] = round(elapsed, 2)
                    
                    if resp.status == 200:
                        result["gateway_reachable"] = True
                        result["auth_valid"] = True
                        result["healthy"] = True
                        self.consecutive_failures = 0
                        logger.debug(f"[OpenClaw Health] ✅ Healthy ({elapsed:.0f}ms)")
                    elif resp.status == 401:
                        result["gateway_reachable"] = True
                        result["error"] = "Authentication failed (invalid token)"
                        logger.warning("[OpenClaw Health] ❌ Auth failed")
                    else:
                        result["gateway_reachable"] = True
                        result["error"] = f"Gateway returned {resp.status}"
                        logger.warning(f"[OpenClaw Health] ⚠️  Gateway error: {resp.status}")
                        
        except asyncio.TimeoutError:
            result["error"] = "Gateway timeout (>10s)"
            self.consecutive_failures += 1
            logger.error("[OpenClaw Health] ❌ Timeout")
        except aiohttp.ClientConnectorError as e:
            result["error"] = f"Cannot connect to gateway: {e}"
            self.consecutive_failures += 1
            logger.error(f"[OpenClaw Health] ❌ Connection failed: {e}")
        except Exception as e:
            result["error"] = f"Health check failed: {e}"
            self.consecutive_failures += 1
            logger.error(f"[OpenClaw Health] ❌ Unexpected error: {e}")
        
        self.last_check = datetime.utcnow()
        self.last_status = result
        
        # Alert on sustained failures
        if self.consecutive_failures >= 3:
            logger.critical(
                f"[OpenClaw Health] 🚨 {self.consecutive_failures} consecutive failures - "
                "delegation may be broken"
            )
        
        return result
    
    async def validate_startup_config(self) -> bool:
        """
        Validate OpenClaw configuration on Nova startup.
        
        Returns:
            True if configuration is valid and gateway is reachable
        """
        logger.info("[OpenClaw Health] Validating startup configuration...")
        
        result = await self.check_health()
        
        if result["healthy"]:
            logger.info(
                f"[OpenClaw Health] ✅ Configuration valid "
                f"({result['response_time_ms']}ms response time)"
            )
            return True
        else:
            logger.error(
                f"[OpenClaw Health] ❌ Configuration invalid: {result['error']}"
            )
            logger.error(
                "[OpenClaw Health] OpenClaw delegation will not work until this is fixed"
            )
            return False
    
    def should_check(self) -> bool:
        """Determine if it's time for a periodic health check."""
        if not self.last_check:
            return True
        return datetime.utcnow() - self.last_check >= self.check_interval
    
    async def periodic_check(self):
        """Run periodic health checks in background."""
        while True:
            try:
                await asyncio.sleep(300)  # 5 minutes
                if self.should_check():
                    await self.check_health()
            except Exception as e:
                logger.error(f"[OpenClaw Health] Periodic check error: {e}")


# Global singleton
_health_monitor = OpenClawHealthMonitor()


async def check_openclaw_health() -> Dict:
    """Public API: Check OpenClaw delegation health."""
    return await _health_monitor.check_health()


async def validate_openclaw_startup() -> bool:
    """Public API: Validate OpenClaw configuration on startup."""
    return await _health_monitor.validate_startup_config()


def start_health_monitoring():
    """Start background health monitoring."""
    asyncio.create_task(_health_monitor.periodic_check())
    logger.info("[OpenClaw Health] Background monitoring started")
