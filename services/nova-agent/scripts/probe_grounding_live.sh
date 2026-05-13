#!/usr/bin/env bash
set -euo pipefail

export NOVA_TEXT_URL="${NOVA_TEXT_URL:-http://127.0.0.1:18803}"
export NOVA_PROBE_MESSAGE="${1:-latest news from OpenAI}"
export NOVA_PROBE_USER_ID="${NOVA_PROBE_USER_ID:-grounding-probe}"
export NOVA_PROBE_CONVERSATION_ID="grounding-probe-$(date +%s)"

python3 - <<'PY'
import json
import os
import time
import urllib.request

base_url = os.environ["NOVA_TEXT_URL"]
message = os.environ["NOVA_PROBE_MESSAGE"]
user_id = os.environ["NOVA_PROBE_USER_ID"]
conversation_id = os.environ["NOVA_PROBE_CONVERSATION_ID"]


def request_json(method, path, payload=None, timeout=45):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

health = request_json("GET", "/health", timeout=8)
if health.get("status") != "ok":
    raise SystemExit(f"Nova health check failed: {health}")
print(json.dumps({"health": "ok", "service": health.get("service")}, sort_keys=True))

chat = request_json(
    "POST",
    "/chat",
    {
        "message": message,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "stream": False,
    },
    timeout=90,
)
print(json.dumps({
    "chat_response_preview": str(chat.get("response") or "")[:300],
    "conversation_id": chat.get("conversation_id"),
    "tool_calls": chat.get("tool_calls"),
}, sort_keys=True))

if "web_search" not in (chat.get("tool_calls") or []):
    raise SystemExit(f"Expected web_search tool call, got: {chat.get('tool_calls')}")
response_text = str(chat.get("response") or "").lower()
if "current external evidence" not in response_text and "won't guess" not in response_text:
    raise SystemExit("Response did not look grounded or refusal-safe")

latest = None
for _ in range(10):
    recent = request_json("GET", "/picode/grounding/recent?limit=10", timeout=8)
    for item in recent.get("evidence", []):
        envelope = item.get("envelope") or {}
        if (
            envelope.get("intent") == "current_events_lookup"
            and envelope.get("conversation_id") == conversation_id
            and "web_search" in (envelope.get("tools_used") or [])
        ):
            latest = envelope
            break
    if latest:
        break
    time.sleep(0.5)

if not latest:
    raise SystemExit("Did not find durable current_events_lookup web_search evidence envelope")

print(json.dumps({
    "live_websearch_grounding": "ok",
    "intent": latest.get("intent"),
    "tools_used": latest.get("tools_used"),
    "evidence_count": latest.get("evidence_count"),
    "no_evidence": latest.get("no_evidence"),
    "stop_reason": latest.get("stop_reason"),
    "conversation_id": latest.get("conversation_id"),
}, sort_keys=True))
PY
