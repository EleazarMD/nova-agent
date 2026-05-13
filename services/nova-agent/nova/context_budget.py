"""Context-window budget instrumentation for Nova.

Phase 1 (this module): observe-only. Logs `NOVA_TURN_OVERFLOW_RISK` whenever
an outgoing prompt approaches the model's hard window. Drives the empirical
decision on whether to ship mid-turn auto-compaction (G3).

Validated against MiniMax M2.7 architecture:
  - Hard window: 196K tokens per sequence (vLLM recipes / MiniMax docs)
  - Lightning + Softmax + MoE hybrid → long context is cheap, but window
    is still bounded.
  - Reference: arxiv.org/abs/2501.08313, docs.vllm.ai/.../MiniMax-M2.html

Threshold rationale:
  Claude Code triggers auto-compact at ~87% of 200K. M2.7 has nearly the
  same hard ceiling, so we use 80% as the soft alarm (room for the next
  tool call to run before hitting the cliff) and 92% as the critical alarm.

Phase 2 (future, only if logs show it's needed): add a compactor that
summarizes the oldest tool results in the live message stack when the
critical threshold trips, before the next LLM call.
"""

from __future__ import annotations

import os
from typing import Optional

from loguru import logger

# Default tuned for MiniMax M2.7. Override per-deployment via env var.
NOVA_LLM_WINDOW = int(os.environ.get("NOVA_LLM_WINDOW", "196000"))

# Soft alarm — log a warning so we can review trends.
SOFT_THRESHOLD_PCT = float(os.environ.get("NOVA_OVERFLOW_SOFT_PCT", "0.80"))

# Critical alarm — log error level; the next tool call may not fit.
CRIT_THRESHOLD_PCT = float(os.environ.get("NOVA_OVERFLOW_CRIT_PCT", "0.92"))


def check_overflow_risk(
    approx_tokens: int,
    *,
    path: str,
    message_count: int,
    tools_in_turn: Optional[int] = None,
    intent: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Optional[str]:
    """Emit `NOVA_TURN_OVERFLOW_RISK` if approx_tokens crosses a threshold.

    Returns the severity string ("soft"|"crit") if a log was emitted,
    else None. Pure observation — no mutation of caller state.
    """
    if NOVA_LLM_WINDOW <= 0 or approx_tokens <= 0:
        return None

    pct = approx_tokens / NOVA_LLM_WINDOW
    if pct < SOFT_THRESHOLD_PCT:
        return None

    severity = "crit" if pct >= CRIT_THRESHOLD_PCT else "soft"
    parts = [
        f"NOVA_TURN_OVERFLOW_RISK severity={severity}",
        f"path={path}",
        f"approx_tokens={approx_tokens}",
        f"window={NOVA_LLM_WINDOW}",
        f"pct={pct:.0%}",
        f"messages={message_count}",
    ]
    if tools_in_turn is not None:
        parts.append(f"tools={tools_in_turn}")
    if intent:
        parts.append(f"intent={intent}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}={v}")

    line = " | ".join(parts)
    if severity == "crit":
        logger.error(line)
    else:
        logger.warning(line)
    return severity
