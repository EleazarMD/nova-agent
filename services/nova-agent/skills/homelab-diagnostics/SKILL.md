---
name: homelab-diagnostics
tool_name: diagnose_network
description: >
  Comprehensive homelab infrastructure health monitoring and diagnostics.
  Use for checking service health, calculating Hermy score, and investigating errors.
parameters:
  type: object
  properties:
    check:
      type: string
      description: "Type of diagnostic check: full, dns, latency, port, service"
      default: "full"
    target:
      type: string
      description: "Target host or service to check"
    port:
      type: integer
      description: "Port number for port checks"
---

# Homelab Diagnostics

Deep health monitoring for AI Homelab infrastructure with component-level diagnostics and overall health scoring.

## When to Invoke

- Checking overall homelab health
- Investigating service failures
- Calculating Hermy score (homelab health metric)
- Diagnosing Pi Agent Hub, AI Inferencing, or CIG issues
- Running comprehensive infrastructure checks

## Actions

### full_diagnostics
Run complete homelab health check with Hermy score calculation.

**Returns:**
- Hermy score (0-100) with grade (A-F)
- Component status breakdown
- Warnings and errors
- Service health details

### pi_agent_hub_health
Check Pi Agent Hub status.

### ai_inferencing_health
Check AI Inferencing service health.

### cig_health
Check CIG connectivity and status.

### hermy_score
Calculate overall homelab health score using formalized component registry.

## Hermy Score Grading

- **90-100 (A)**: Excellent - All systems operational
- **80-89 (B)**: Good - Minor issues
- **70-79 (C)**: Fair - Some degradation
- **60-69 (D)**: Poor - Significant issues
- **0-59 (F)**: Critical - Major failures

## Monitored Components

- **Pi Agent Hub**: Hub service status
- **AI Inferencing**: Service health, endpoint availability
- **CIG**: Connectivity, health status
- **Component Registry**: Weighted scoring by importance

## Examples

User: Run a full health check on the homelab
Assistant: Invoking @homelab-diagnostics action=full_diagnostics

User: What's the Hermy score?
Assistant: Invoking @homelab-diagnostics action=hermy_score

User: Check if the Pi Agent Hub is running
Assistant: Invoking @homelab-diagnostics action=pi_agent_hub_health

## Technical Details

- Component registry: `templates/component-registry.json`
- Weighted scoring by component importance
- Systemd service checks for Pi Agent Hub
- HTTP health endpoints for AI services
- Timeout: 5 seconds per component

## References

- Script: `scripts/diagnostics.py`
- Pi Agent Hub: http://127.0.0.1:18793
- AI Inferencing: http://localhost:9000
- CIG: http://localhost:8780
