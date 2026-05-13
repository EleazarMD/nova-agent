---
name: cig-tool-troubleshooting
description: >
  Troubleshoot CIG (Communication Intelligence Graph) tool call failures in Nova.
  Diagnose connectivity, timeout, and routing issues with the CIG service on port 8780.
---

# CIG Tool Troubleshooting

Diagnose and resolve CIG (Communication Intelligence Graph) tool call failures in Nova.

## When to Invoke

- Nova reports "CIG is unhealthy" or timing out on CIG queries
- `query_cig` tool calls fail with timeout errors
- Email/contact/graph queries return no results or errors
- CIG-related functionality stops working after restarts

## Quick Diagnosis

### 1. Check CIG Service Health

```bash
curl -s -H "X-API-Key: nova-agent-key-2024" http://localhost:8780/health | python3 -m json.tool
```

Expected: `{"service":"cig","status":"healthy",...}`

### 2. Verify CIG Graph Stats

```bash
curl -s -H "X-API-Key: nova-agent-key-2024" http://localhost:8780/v1/graph/stats | python3 -m json.tool
```

Expected: Node counts (persons, emails, threads, etc.)

### 3. Test Direct Tool Call

```bash
cd /home/eleazar/Projects/AIHomelab/services/nova-agent/services/nova-agent
./venv/bin/python -c "
import asyncio
from nova.tools import dispatch_tool
async def test():
    result = await dispatch_tool('query_cig', {'domain': 'graph', 'query': 'stats'})
    print(result)
asyncio.run(test())
"
```

## Common Issues & Fixes

### Issue: CIG Service Not Running

**Symptom**: Connection refused or timeout on port 8780

**Fix**:
```bash
sudo systemctl restart cig.service
# or
docker restart cig
```

### Issue: Timeout on Graph Queries

**Symptom**: `asyncio.TimeoutError` or empty response after 12s

**Cause**: Neo4j queries may be slow on large graphs

**Check**:
```bash
# Test graph endpoint directly
curl -s -m 15 http://localhost:8780/v1/graph/stats
```

**Fix**: CIG has 12s timeout in `nova/cig.py`. If consistently slow:
1. Check Neo4j: `docker logs neo4j-cig --tail 50`
2. Restart CIG service to clear connection pool

### Issue: AI Gateway Routing Problems

**Symptom**: CIG health OK but Nova still fails tool calls

**Check**: Multiple AI Gateway instances conflicting

```bash
# Find all gateway processes
ps aux | grep "server.js" | grep -v grep | grep -v typescript | grep -v codeium
```

**Fix**: Kill stale instances, restart single gateway:
```bash
sudo pkill -9 -f "node server.js"
sleep 3
sudo systemctl restart ai-gateway-v2
```

### Issue: Missing API Key

**Symptom**: 401 Unauthorized errors

**Fix**: Verify `CIG_API_KEY` in Nova environment matches CIG service

```bash
grep CIG_API_KEY /home/eleazar/Projects/AIHomelab/services/nova-agent/services/nova-agent/.env
```

## Dependencies

CIG tool calls depend on:
- **CIG service** (port 8780) - Core graph API
- **Neo4j** (port 7689) - Graph database
- **AI Gateway** (port 8777) - For LLM context enrichment
- **AI Inferencing** (port 9000) - Provider discovery

## Verification Steps

After fixes, verify full chain:

```bash
# 1. CIG health
curl http://localhost:8780/health

# 2. Tool dispatch
python3 -c "from nova.tools import dispatch_tool; ..."

# 3. Nova log
tail -f /var/log/nova-agent.log | grep -i cig
```

## References

- CIG config: `/services/cig/api.py`
- Nova CIG module: `/services/nova-agent/services/nova-agent/nova/cig.py`
- Tool definitions: `/services/nova-agent/services/nova-agent/nova/tools.py`
