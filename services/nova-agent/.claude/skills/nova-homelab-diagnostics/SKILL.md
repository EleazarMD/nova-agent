---
name: nova-homelab-diagnostics
description: Run comprehensive infrastructure health checks on AI Homelab services. Check OpenClaw, AI Inferencing, Hermes Core, and calculate Hermy scores.
---

# Homelab Diagnostics

Performs comprehensive infrastructure health monitoring across all AI Homelab services. Checks service availability, response times, and calculates overall system health (Hermy score).

## When to Invoke

- User asks about system health or status
- Troubleshooting infrastructure issues
- Checking if services are running
- Investigating errors or outages
- Daily health check requests
- "What's wrong with the system?"

## Actions

- **full_check**: Run all diagnostic checks
- **service_check**: Check specific service health
- **gateway_status**: Check OpenClaw gateway
- **ai_inferencing**: Check AI Inferencing service
- **hermes_core**: Check Hermes Core connectivity
- **hermy_score**: Calculate overall Hermy score

## Services Checked

- OpenClaw Gateway (port 18793)
- AI Inferencing Service (port 9000)
- Hermes Core API (port 8780)
- AI Gateway (port 8777)
- Ecosystem Dashboard (port 8404)
- Nova Agent itself (port 18800)

## Examples

User: "Is everything running okay?"
Assistant: Invoking @nova-homelab-diagnostics to check system health...

User: "What's the Hermy score?"
Assistant: Invoking @nova-homelab-diagnostics to calculate system health...

## References

- Script: `services/nova-agent/skills/homelab-diagnostics/scripts/diagnostics.py`
- Handler: Various diagnostic functions
