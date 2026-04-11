"""
Homelab infrastructure operations for Nova.

Provides Docker container management with tiered safety:
  - READ-ONLY (no approval): logs, health checks, status
  - APPROVAL REQUIRED: restart, start, stop — ALL routed through the
    homelab's central ApprovalService (ecosystem-dashboard PostgreSQL).

All mutating operations are gated by the homelab approval engine:
  POST /api/security/approvals/request  → create approval (push + audit)
  GET  /api/security/approvals/{id}/status → poll for decision

The Dashboard (web) and iOS app are the ONLY surfaces where a human
can approve or deny. Nova NEVER auto-approves anything. Nova is a
consumer of the approval system, not an authority.
"""

import asyncio
import json
import os
from typing import Any

import aiohttp
from loguru import logger

# Import JWT generator from dedicated auth module
from nova.hermes_auth import generate_hermes_jwt


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Hermes Core — email/calendar intelligence service
HERMES_CORE_URL = os.environ.get("HERMES_CORE_URL", "http://localhost:8780")

# AI Inferencing — centralized API key vault and telemetry
AI_INFERENCING_URL = os.environ.get("AI_INFERENCING_URL", "http://localhost:9000")
AI_INFERENCING_ADMIN_KEY = os.environ.get("AI_INFERENCING_ADMIN_KEY", "ai-inferencing-admin-key-2024")


# ---------------------------------------------------------------------------
# Container allowlist — only these containers can be managed
# ---------------------------------------------------------------------------

# Managed Docker containers (aligned with component-registry.json)
MANAGED_CONTAINERS = {
    # Hermes Intelligence
    "cig",
    "hermes-chromadb",
    "hermes-neo4j",
    
    # OpenClaw (Docker components only)
    "openclaw-novnc",
    "openclaw-inference",
    
    # PIC/PKB (Personal Integration Core)
    "pkb-api",
    "pkb-neo4j",
    "pkb-redis",
    
    # AI Gateway
    "ai-gateway-postgres",
    "ai-gateway-redis",
    
    # AI/ML Services
    "comfyui",
    "nim-embeddings",
    "qwen-tts-api",
    "goose-mind-whisper",
    
    # Clinical KB
    "clinical-kb-api",
    "clinical-kb-neo4j",
    "clinical-kb-postgres",
    "postgres-clinical",
    "redis-clinical",
    
    # Story Intelligence
    "story-intelligence",
    "story-neo4j",
    "story-pgvector",
    
    # Monitoring
    "mlops-prometheus",
    "mlops-grafana",
    "mlops-metrics-api",
    "mlops-pushgateway",
    
    # Other
    "perplexica-searxng-1",
}

# Systemd services that can be managed (aligned with component-registry.json)
MANAGED_SYSTEMD_SERVICES = {
    # User services (systemctl --user)
    "openclaw-gateway.service",
    "openclaw-memory-sync.service",
    "tesla-relay.service",
    
    # System services (sudo systemctl)
    "ai-inferencing.service",
    "approval-service.service",
    "homelab-monitor.timer",
}

# Containers that should NEVER be touched by any agent
PROTECTED_CONTAINERS = {
    "tailscaled",
    "unifi-network-application",
    "postgres",  # Core ecosystem_unified database
    "portainer",
}


# ---------------------------------------------------------------------------
# Docker helpers (subprocess-based, no Docker SDK dependency)
# ---------------------------------------------------------------------------

async def _docker_exec(*args: str, timeout: int = 30) -> tuple[str, str, int]:
    """Run a docker command and return (stdout, stderr, returncode)."""
    cmd = ["docker"] + list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            stdout.decode().strip(),
            stderr.decode().strip(),
            proc.returncode or 0,
        )
    except asyncio.TimeoutError:
        return "", f"Command timed out after {timeout}s", 1
    except Exception as e:
        return "", str(e), 1


async def _container_exists(name: str) -> bool:
    """Check if a container exists (running or stopped)."""
    stdout, _, rc = await _docker_exec(
        "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"
    )
    return name in stdout.split()


async def _container_status(name: str) -> dict[str, Any]:
    """Get detailed status of a container."""
    stdout, stderr, rc = await _docker_exec(
        "inspect", "--format",
        '{"state":"{{.State.Status}}","health":"{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}","started":"{{.State.StartedAt}}","image":"{{.Config.Image}}"}',
        name,
    )
    if rc != 0:
        return {"error": stderr or "Container not found"}
    try:
        return json.loads(stdout)
    except Exception:
        return {"state": "unknown", "raw": stdout}


def _validate_container(container: str, allowlist: set[str] | None = None) -> str | None:
    """Validate container name. Returns error string or None if valid."""
    if container in PROTECTED_CONTAINERS:
        return f"DENIED: {container} is a protected system container and can NEVER be managed by any agent."
    target = allowlist or MANAGED_CONTAINERS
    if container not in target:
        return (
            f"Container '{container}' is not in the managed allowlist. "
            f"Known containers: {', '.join(sorted(target))}"
        )
    return None


# ---------------------------------------------------------------------------
# Tool handlers — read-only (no approval needed)
# ---------------------------------------------------------------------------

async def handle_service_status(container: str = "") -> str:
    """Get status of homelab containers. No approval needed (read-only)."""
    if container:
        if container in ["cig", "pcg", "tesla-relay", "openclaw-browser"]:
            import subprocess
            try:
                # Use --user flag for user services like openclaw-browser
                user_flag = ["--user"] if container == "openclaw-browser" else []
                res = subprocess.run(["systemctl"] + user_flag + ["is-active", container], capture_output=True, text=True, timeout=2)
                st = "Up (healthy)" if res.returncode == 0 else "Exited (down)"
                return f"{container}: {st} [systemd]"
            except:
                return f"{container}: unknown [systemd]"

        if container in PROTECTED_CONTAINERS:
            return f"{container} is a protected system container. Status check not available through this tool."
        status = await _container_status(container)
        if "error" in status:
            return f"Container '{container}': {status['error']}"
        health = f", health={status['health']}" if status['health'] != 'none' else ""
        return (
            f"{container}: {status['state']}{health}, "
            f"image={status.get('image', '?')}, "
            f"started={status.get('started', '?')[:19]}"
        )

    # All containers
    stdout, _, rc = await _docker_exec(
        "ps", "-a",
        "--format", "{{.Names}}\t{{.Status}}",
    )
    if rc != 0:
        return "Could not query Docker."

    lines = []
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        name = parts[0].strip()
        st = parts[1].strip() if len(parts) > 1 else "unknown"
        marker = ""
        if name in PROTECTED_CONTAINERS:
            marker = " [protected]"
        elif name in MANAGED_CONTAINERS:
            marker = " [managed]"
        lines.append(f"- {name}: {st}{marker}")

    # Add systemd services
    for svc in ["cig", "pcg", "tesla-relay", "openclaw-browser"]:
        import subprocess
        try:
            # Use --user flag for user services like openclaw-browser
            user_flag = ["--user"] if svc == "openclaw-browser" else []
            res = subprocess.run(["systemctl"] + user_flag + ["is-active", svc], capture_output=True, text=True, timeout=2)
            st = "Up (healthy)" if res.returncode == 0 else "Exited (down)"
            lines.append(f"- {svc}: {st} [systemd]")
        except:
            pass

    return f"{len(lines)} containers/services:\n" + "\n".join(sorted(lines))


async def handle_service_logs(container: str, lines: int = 50) -> str:
    """Get recent logs from a container. No approval needed (read-only)."""
    if container in PROTECTED_CONTAINERS:
        return f"{container} is a protected system container."
    if not await _container_exists(container):
        return f"Container '{container}' not found."

    lines = min(lines, 200)
    stdout, stderr, rc = await _docker_exec(
        "logs", "--tail", str(lines), "--timestamps", container,
        timeout=15,
    )
    if rc != 0:
        return f"Could not get logs: {stderr}"

    output = stdout or stderr
    if not output:
        return f"No recent logs from {container}."

    if len(output) > 4000:
        output = output[-4000:]
        output = "...(truncated)\n" + output

    return f"Last {lines} log lines from {container}:\n{output}"


async def _probe_hermes_health() -> dict[str, Any]:
    """Probe Hermes Core application-level health: email, calendar, databases.

    Returns a structured dict with status, email counts, calendar stats,
    and component health (Neo4j, ChromaDB, LLM gateway).
    """
    result: dict[str, Any] = {"reachable": False}
    # Generate JWT token dynamically for authentication
    hermes_token = generate_hermes_jwt()
    hermes_headers = {"Authorization": f"Bearer {hermes_token}"}
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Core health endpoint — email counts + component status
            async with session.get(
                f"{HERMES_CORE_URL}/health", timeout=timeout
            ) as resp:
                if resp.status != 200:
                    result["error"] = f"HTTP {resp.status}"
                    return result
                data = await resp.json()
                result["reachable"] = True
                result["status"] = data.get("status", "unknown")
                result["components"] = data.get("components", {})
                idx = data.get("indexed_emails", {})
                result["emails"] = {
                    "total": idx.get("total", 0),
                    "inbox": idx.get("inbox", 0),
                    "sent": idx.get("sent", 0),
                }

            # 2. Calendar stats (non-fatal if unavailable)
            try:
                async with session.get(
                    f"{HERMES_CORE_URL}/v1/calendar/neo4j/stats",
                    headers=hermes_headers,
                    timeout=timeout,
                ) as cal_resp:
                    if cal_resp.status == 200:
                        cal_data = await cal_resp.json()
                        result["calendar"] = {
                            "calendars": cal_data.get("calendars", 0),
                            "events": cal_data.get("events", 0),
                            "last_sync": cal_data.get("last_sync", "unknown"),
                        }
            except Exception:
                result["calendar"] = {"error": "calendar stats unavailable"}

    except asyncio.TimeoutError:
        result["error"] = "timeout (Hermes Core not responding)"
    except Exception as e:
        result["error"] = str(e)

    return result


async def _probe_ai_inferencing_health() -> dict[str, Any]:
    """Probe AI Inferencing application-level health: services, keys, providers.

    Returns a structured dict with status, service count, key count, and provider health.
    """
    result: dict[str, Any] = {"reachable": False}
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Health endpoint
            async with session.get(
                f"{AI_INFERENCING_URL}/health", timeout=timeout
            ) as resp:
                if resp.status != 200:
                    result["error"] = f"HTTP {resp.status}"
                    return result
                data = await resp.json()
                result["reachable"] = True
                result["status"] = data.get("status", "unknown")

            # 2. Admin services endpoint — get service and key counts
            try:
                async with session.get(
                    f"{AI_INFERENCING_URL}/api/v1/admin/keys/services",
                    headers={"X-Admin-Key": AI_INFERENCING_ADMIN_KEY},
                    timeout=timeout,
                ) as svc_resp:
                    if svc_resp.status == 200:
                        svc_data = await svc_resp.json()
                        services = svc_data.get("services", [])
                        result["services"] = len(services)
                        result["keys"] = sum(int(s.get("key_count", 0)) for s in services)
            except Exception:
                result["services"] = "unavailable"
                result["keys"] = "unavailable"

    except asyncio.TimeoutError:
        result["error"] = "timeout (AI Inferencing not responding)"
    except Exception as e:
        result["error"] = str(e)

    return result


async def handle_service_health_check(container: str = "") -> str:
    """Deep health check: container status + ports + application-level probes.

    When no container is specified, also probes Hermes Core's application
    health to report email/calendar/database status alongside container state.
    """
    if container:
        if container in ["cig", "pcg", "tesla-relay"]:
            import subprocess
            try:
                res = subprocess.run(["systemctl", "is-active", container], capture_output=True, text=True, timeout=2)
                st = "Up (healthy)" if res.returncode == 0 else "Exited (down)"
                return f"{container}: {st} [systemd]"
            except:
                return f"{container}: unknown [systemd]"

        if container in PROTECTED_CONTAINERS:
            return f"{container} is protected. Use standard monitoring."

        status = await _container_status(container)
        if "error" in status:
            return f"{container}: {status['error']}"

        parts = [
            f"{container}:",
            f"  State: {status.get('state', '?')}",
            f"  Health: {status.get('health', 'none')}",
            f"  Image: {status.get('image', '?')}",
            f"  Started: {status.get('started', '?')[:19]}",
        ]

        stdout, _, rc = await _docker_exec("port", container)
        if rc == 0 and stdout:
            parts.append(f"  Ports: {stdout.replace(chr(10), ', ')}")

        # If checking a Hermes container, also probe application health
        if container == "cig":
            hermes = await _probe_hermes_health()
            if hermes.get("reachable"):
                comps = hermes.get("components", {})
                emails = hermes.get("emails", {})
                parts.append(f"  App Status: {hermes.get('status', '?')}")
                parts.append(
                    f"  Emails: {emails.get('total', 0):,} total "
                    f"({emails.get('inbox', 0):,} inbox, {emails.get('sent', 0):,} sent)"
                )
                parts.append(
                    f"  Neo4j: {comps.get('neo4j', '?')}, "
                    f"ChromaDB: {comps.get('chromadb', '?')}, "
                    f"LLM: {comps.get('llm_gateway', '?')}"
                )
                cal = hermes.get("calendar", {})
                if cal and not cal.get("error"):
                    parts.append(
                        f"  Calendar: {cal.get('calendars', 0)} calendars, "
                        f"{cal.get('events', 0)} events, "
                        f"last sync: {cal.get('last_sync', '?')}"
                    )
            else:
                parts.append(f"  App Probe: FAILED — {hermes.get('error', 'unreachable')}")

        return "\n".join(parts)

    # All managed containers
    results = []
    for name in sorted(MANAGED_CONTAINERS):
        status = await _container_status(name)
        if "error" in status:
            results.append(f"- {name}: NOT FOUND")
        else:
            state = status.get("state", "?")
            health = status.get("health", "none")
            ok = state == "running" and health in ("healthy", "none")
            health_str = f" ({health})" if health != "none" else ""
            results.append(f"- {'OK' if ok else 'WARN'} {name}: {state}{health_str}")

    # Application-level probes for data services
    app_probes = []
    
    # Probe Hermes Core
    hermes = await _probe_hermes_health()
    if hermes.get("reachable"):
        comps = hermes.get("components", {})
        emails = hermes.get("emails", {})
        neo4j_ok = comps.get("neo4j", "?") in ("connected", "healthy", "ok")
        chroma_ok = comps.get("chromadb", "?") in ("connected", "healthy", "ok")
        llm_ok = comps.get("llm_gateway", "?") in ("connected", "healthy", "ok", "reachable")
        app_probes.append(
            f"- Hermes Email: {hermes.get('status', '?')} — "
            f"{emails.get('total', 0):,} emails indexed "
            f"({emails.get('inbox', 0):,} inbox, {emails.get('sent', 0):,} sent)"
        )
        app_probes.append(
            f"  Databases: Neo4j={'OK' if neo4j_ok else comps.get('neo4j', '?')}, "
            f"ChromaDB={'OK' if chroma_ok else comps.get('chromadb', '?')}, "
            f"LLM Gateway={'OK' if llm_ok else comps.get('llm_gateway', '?')}"
        )
        cal = hermes.get("calendar", {})
        if cal and not cal.get("error"):
            app_probes.append(
                f"- Hermes Calendar: {cal.get('calendars', 0)} calendars, "
                f"{cal.get('events', 0)} events, last sync: {cal.get('last_sync', '?')}"
            )
    else:
        app_probes.append(
            f"- Hermes Core: UNREACHABLE — {hermes.get('error', 'cannot connect to ' + HERMES_CORE_URL)}"
        )

    # Probe AI Inferencing
    ai_inf = await _probe_ai_inferencing_health()
    if ai_inf.get("reachable"):
        app_probes.append(
            f"- AI Inferencing: {ai_inf.get('status', '?')} — "
            f"{ai_inf.get('services', '?')} services, {ai_inf.get('keys', '?')} API keys managed"
        )
    else:
        app_probes.append(
            f"- AI Inferencing: UNREACHABLE — {ai_inf.get('error', 'cannot connect to ' + AI_INFERENCING_URL)}"
        )

    output = f"Health check for {len(MANAGED_CONTAINERS)} managed containers:\n"
    output += "\n".join(results)
    if app_probes:
        output += "\n\nApplication-level probes:\n" + "\n".join(app_probes)

    return output


# ---------------------------------------------------------------------------
# Unified homelab_operations handler (per /claude-skills spec)
# ---------------------------------------------------------------------------

async def homelab_operations(
    action: str,
    container: str = "",
    service: str = "",
    lines: int = 50,
) -> dict:
    """
    Unified homelab operations handler.
    
    Dispatches to appropriate handler based on action parameter.
    READ-ONLY actions: status, logs, health_check (no approval)
    MUTATING actions: restart, start, stop (approval required)
    
    Args:
        action: Operation to perform (restart, start, stop, status, logs, health_check)
        container: Docker container name (for container operations)
        service: Systemd service name (for service operations)
        lines: Number of log lines (for logs action)
    
    Returns:
        dict with success, result data, and approval info for mutating operations
    """
    # Validate action
    valid_actions = {"restart", "start", "stop", "status", "logs", "health_check"}
    if action not in valid_actions:
        return {
            "success": False,
            "error": f"Invalid action: {action}. Valid actions: {', '.join(sorted(valid_actions))}"
        }
    
    # READ-ONLY operations (no approval needed)
    if action == "status":
        result = await handle_service_status(container)
        return {"success": True, "action": action, "result": result}
    
    if action == "logs":
        if not container:
            return {"success": False, "error": "container parameter required for logs action"}
        result = await handle_service_logs(container, lines)
        return {"success": True, "action": action, "container": container, "result": result}
    
    if action == "health_check":
        result = await handle_service_health_check(container)
        return {"success": True, "action": action, "result": result}
    
    # MUTATING operations (approval required)
    # These require the approval service integration
    if action in {"restart", "start", "stop"}:
        # For mutating operations, we need container or service
        target = container or service
        if not target:
            return {
                "success": False, 
                "error": f"{action} requires either 'container' or 'service' parameter"
            }
        
        # Validate target
        if container:
            error = _validate_container(container)
            if error:
                return {"success": False, "error": error}
        
        # Return approval-required response
        # Note: Actual approval flow is handled by the caller (bot.py)
        return {
            "success": True,
            "action": action,
            "target": target,
            "target_type": "container" if container else "service",
            "approval_required": True,
            "message": f"{action} {target} requires approval. Creating approval request...",
        }
    
    # Should never reach here
    return {"success": False, "error": f"Unhandled action: {action}"}
