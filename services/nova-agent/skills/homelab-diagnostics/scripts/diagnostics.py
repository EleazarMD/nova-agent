#!/usr/bin/env python3
"""
Homelab Diagnostics Script for Nova Agent

Provides comprehensive infrastructure health monitoring:
- OpenClaw gateway status
- AI Inferencing service health
- Hermes Core connectivity
- Component status checks
- Hermy score calculation
- Error investigation
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp


# Service URLs
OPENCLAW_URL = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18793")
AI_INFERENCING_URL = os.environ.get("AI_INFERENCING_URL", "http://localhost:9000")
HERMES_CORE_URL = os.environ.get("HERMES_CORE_URL", "http://localhost:8780")
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8777/api/v1")
AI_INFERENCING_ADMIN_KEY = os.environ.get("AI_INFERENCING_ADMIN_KEY", "ai-inferencing-admin-key-2024")

# Load component registry
SCRIPT_DIR = Path(__file__).parent
REGISTRY_PATH = SCRIPT_DIR.parent / "templates" / "component-registry.json"

def load_component_registry() -> Dict[str, Any]:
    """Load the formalized component registry."""
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load component registry: {e}", file=sys.stderr)
        return {"components": {}}


async def check_openclaw_health() -> Dict[str, Any]:
    """Check OpenClaw gateway health and status."""
    result = {
        "status": "unknown",
        "gateway_running": False,
        "details": {}
    }
    
    try:
        # Check if openclaw-gateway service is running
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "is-active", "openclaw-gateway",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        is_active = stdout.decode().strip() == "active"
        
        if is_active:
            result["gateway_running"] = True
            
            # Get service status details
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "status", "openclaw-gateway", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            status_output = stdout.decode()
            
            # Parse PID and uptime from status
            for line in status_output.split('\n'):
                if 'Main PID:' in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        result["details"]["gateway_pid"] = int(parts[2])
                elif 'Active:' in line and 'since' in line:
                    result["details"]["last_start"] = line.split('since')[1].split(';')[0].strip()
            
            # Check config file
            config_path = os.path.expanduser("~/.openclaw/openclaw.json")
            if os.path.exists(config_path):
                result["details"]["config_valid"] = True
                result["details"]["config_path"] = config_path
            
            result["status"] = "healthy"
        else:
            result["status"] = "stopped"
            
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    
    return result


async def check_ai_inferencing_health() -> Dict[str, Any]:
    """Check AI Inferencing service health."""
    result = {
        "status": "unknown",
        "service_running": False,
        "details": {}
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{AI_INFERENCING_URL}/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    health_data = await resp.json()
                    result["status"] = "healthy"
                    result["service_running"] = True
                    result["details"] = health_data
                else:
                    result["status"] = "unhealthy"
                    result["details"]["http_status"] = resp.status
                    
    except Exception as e:
        result["status"] = "unreachable"
        result["error"] = str(e)
    
    return result


async def check_hermes_health() -> Dict[str, Any]:
    """Check Hermes Core health."""
    result = {
        "status": "unknown",
        "service_running": False,
        "details": {}
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HERMES_CORE_URL}/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    health_data = await resp.json()
                    result["status"] = "healthy"
                    result["service_running"] = True
                    result["details"] = health_data
                else:
                    result["status"] = "unhealthy"
                    result["details"]["http_status"] = resp.status
                    
    except Exception as e:
        result["status"] = "unreachable"
        result["error"] = str(e)
    
    return result


async def calculate_hermy_score(components: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate overall homelab health score (0-100) using formalized registry."""
    
    registry = load_component_registry()
    component_groups = registry.get("components", {})
    
    scores = {}
    breakdown = {}
    
    # Calculate score for each component group
    for group_name, group_data in component_groups.items():
        weight = group_data.get("weight", 0)
        services = group_data.get("services", {})
        
        if not services:
            continue
        
        # Count healthy services
        healthy_count = 0
        total_count = 0
        service_status = {}
        
        for service_id, service_info in services.items():
            # Skip optional services if not checked
            if service_info.get("optional") and service_id not in components:
                continue
            
            total_count += 1
            is_healthy = components.get(service_id, {}).get("status") == "healthy"
            service_status[service_id] = is_healthy
            if is_healthy:
                healthy_count += 1
        
        # Calculate group score
        if total_count > 0:
            group_score = int((healthy_count / total_count) * 100)
        else:
            group_score = 100  # No services to check
        
        scores[group_name] = group_score
        breakdown[group_name] = {
            "score": group_score,
            "weight": weight,
            "healthy": healthy_count,
            "total": total_count,
            "services": service_status
        }
    
    # Calculate weighted total
    hermy_score = 0
    for group_name, group_data in component_groups.items():
        weight = group_data.get("weight", 0)
        group_score = scores.get(group_name, 100)
        hermy_score += group_score * weight
    
    hermy_score = int(hermy_score)
    
    # Determine status
    if hermy_score >= 90:
        status = "excellent"
        grade = "A"
    elif hermy_score >= 80:
        status = "good"
        grade = "B"
    elif hermy_score >= 70:
        status = "fair"
        grade = "C"
    elif hermy_score >= 60:
        status = "poor"
        grade = "D"
    else:
        status = "critical"
        grade = "F"
    
    return {
        "hermy_score": hermy_score,
        "status": status,
        "grade": grade,
        "breakdown": breakdown,
        "total_services_checked": sum(b["total"] for b in breakdown.values()),
        "total_services_healthy": sum(b["healthy"] for b in breakdown.values())
    }


async def full_diagnostics() -> Dict[str, Any]:
    """Run full homelab diagnostics."""
    
    # Check all components
    openclaw = await check_openclaw_health()
    ai_inferencing = await check_ai_inferencing_health()
    hermes = await check_hermes_health()
    
    components = {
        "openclaw": openclaw,
        "ai_inferencing": ai_inferencing,
        "hermes_core": hermes
    }
    
    # Calculate Hermy score
    hermy_data = await calculate_hermy_score(components)
    
    # Collect warnings and errors
    warnings = []
    errors = []
    
    for name, component in components.items():
        if component.get("status") == "unhealthy":
            warnings.append({
                "severity": "medium",
                "component": name,
                "message": f"{name} is unhealthy"
            })
        elif component.get("status") in ["error", "unreachable", "stopped"]:
            errors.append({
                "severity": "high",
                "component": name,
                "message": f"{name} is {component['status']}"
            })
    
    return {
        "success": True,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "hermy_score": hermy_data["hermy_score"],
        "status": hermy_data["status"],
        "summary": f"Hermy score: {hermy_data['hermy_score']}/100 ({hermy_data['grade']})",
        "components": components,
        "hermy_breakdown": hermy_data["breakdown"],
        "warnings": warnings,
        "errors": errors
    }


async def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: diagnostics.py <action>")
        print("Actions: full_diagnostics, openclaw_health, ai_inferencing_health, hermes_health, hermy_score")
        sys.exit(1)
    
    action = sys.argv[1]
    
    if action == "full_diagnostics":
        result = await full_diagnostics()
    elif action == "openclaw_health":
        result = await check_openclaw_health()
    elif action == "ai_inferencing_health":
        result = await check_ai_inferencing_health()
    elif action == "hermes_health":
        result = await check_hermes_health()
    elif action == "hermy_score":
        components = {
            "openclaw": await check_openclaw_health(),
            "ai_inferencing": await check_ai_inferencing_health(),
            "hermes_core": await check_hermes_health()
        }
        result = await calculate_hermy_score(components)
    else:
        result = {"success": False, "error": f"Unknown action: {action}"}
    
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
