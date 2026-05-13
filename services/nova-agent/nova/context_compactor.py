"""
Nova Context Compactor — Anthropic-style proactive tool-result clearing.

Background
----------
context_budget.py only fires at >=80% of the 196K-token hard window. By then
the prompt is already crushing latency. Empirically, Nova's prompts stay well
under 80K tokens yet still cost 20+ seconds of TTFB on MiniMax M2.7 when the
live LLMContext holds large tool_result messages from previous tool calls in
the same turn (e.g. a 64K-char web_search result followed by manage_workspace).

This module implements two layered compactions matching Anthropic's
``clear_tool_uses_20250919`` shape:

  1. ``clear_old_tool_results(messages, keep_recent_pairs=N)``
       In a flat OpenAI-style message list with assistant/tool pairs, replace
       the ``content`` of any tool_result older than the N most recent ones
       with a tiny stub describing what was called. Per-tool TTLs let
       short-lived tools (web_search) decay faster than memory tools.

  2. ``compact_if_over_latency_threshold(messages, *, threshold_tokens=20000)``
       If the prompt's approximate token count crosses the latency threshold,
       call ``clear_old_tool_results`` with progressively smaller keep counts
       until the budget fits.

Both functions mutate-in-place when given a list and also return it for
chaining. Pure functions (no I/O), safe to call from tight loops.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from loguru import logger


# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

# Latency soft threshold — when an outgoing prompt exceeds this many tokens we
# start clearing old tool results. Default 20K matches the empirical sweet
# spot for MiniMax M2.7 (~6-8s TTFB).
LATENCY_THRESHOLD_TOKENS = int(os.environ.get("NOVA_COMPACT_LATENCY_TOKENS", "20000"))

# Hard ceiling — never let a prompt exceed this even if it means stubbing
# every tool result in history.
HARD_CEILING_TOKENS = int(os.environ.get("NOVA_COMPACT_HARD_CEILING", "60000"))

# How many of the most recent tool_call/tool_result pairs to keep verbatim
# at the soft threshold. Anthropic's default is 3.
DEFAULT_KEEP_RECENT_PAIRS = int(os.environ.get("NOVA_COMPACT_KEEP_RECENT", "3"))

# Hard cap on stored tool-result content in history (NOT live mid-turn).
# bot.py / text_chat.py originally allowed 64,000 char tool results to ride
# along in history; that's wasteful. The live mid-turn cap stays at 64K so
# the model can read the full result on the same turn it was called.
HISTORY_TOOL_RESULT_CAP = int(os.environ.get("NOVA_HISTORY_TOOL_RESULT_CAP", "12000"))

# Per-tool TTL multipliers — tools whose results stay relevant longer get a
# higher multiplier (effectively more "keep recent" slots before they're
# stubbed). Tools whose results decay fast (web_search results stale within
# minutes, status checks every few seconds) get < 1.
_TOOL_TTL_MULTIPLIER: dict[str, float] = {
    # Long-lived (memory, decisions)
    "recall_memory": 3.0,
    "save_memory": 3.0,
    "query_self_state": 3.0,
    "manage_task_plan": 2.5,
    # Medium-lived (knowledge of the environment)
    "query_workspace": 1.5,
    "manage_workspace": 1.5,
    "manage_notes": 1.5,
    "query_cig": 1.2,
    "query_context": 1.2,
    "kg_query": 1.2,
    "search_past_conversations": 1.2,
    # Short-lived (volatile observations)
    "web_search": 0.5,
    "check_studio": 0.6,
    "service_status": 0.4,
    "service_health_check": 0.4,
    "service_logs": 0.4,
    "homelab_diagnostics": 0.5,
    "get_time": 0.2,
    "get_weather": 0.6,
    "tesla_control": 0.5,
    "tesla_stream_monitor": 0.3,
}


# ---------------------------------------------------------------------------
# Token estimation (same heuristic as bot.py / text_chat.py)
# ---------------------------------------------------------------------------

def estimate_tokens(messages: Iterable[dict]) -> int:
    """Approximate token count using the 4-chars-per-token heuristic.

    This intentionally matches ``bot._estimate_tokens`` so logs are comparable.
    Includes the message content only — tool definitions and OpenAI envelope
    overhead are accounted for elsewhere.
    """
    total = 0
    for msg in messages:
        c = msg.get("content")
        if c is None:
            continue
        if isinstance(c, list):
            # Multi-modal content blocks.
            for block in c:
                if isinstance(block, dict):
                    total += len(str(block.get("text") or ""))
        else:
            total += len(str(c))
    return total // 4


# ---------------------------------------------------------------------------
# Tool-result clearing
# ---------------------------------------------------------------------------

def _tool_pair_indexes(messages: list[dict]) -> list[tuple[int, int, str]]:
    """Return list of (assistant_idx, tool_idx, tool_name) for each
    tool_call → tool result pair in *messages*, in chronological order.

    The OpenAI message convention for one tool call is:
        {"role": "assistant", "content": None|"...", "tool_calls": [{"function": {"name": ...}}]}
        {"role": "tool", "tool_call_id": "...", "content": "..."}

    A single assistant turn may contain multiple parallel tool_calls; each
    one becomes its own pair (assistant_idx repeats, tool_idx differs).
    """
    pairs: list[tuple[int, int, str]] = []
    pending_calls: list[tuple[int, str]] = []  # (assistant_idx, name) for each unmatched call
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"] or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                pending_calls.append((i, str(fn.get("name") or "unknown")))
        elif role == "tool":
            if pending_calls:
                assistant_idx, name = pending_calls.pop(0)
                pairs.append((assistant_idx, i, name))
    return pairs


def _stub_for(tool_name: str, args_preview: str = "") -> str:
    """Tiny placeholder that replaces a stubbed tool_result body."""
    if args_preview:
        return f"[result for {tool_name}({args_preview}) consumed in earlier turn]"
    return f"[result for {tool_name}() consumed in earlier turn]"


def _args_preview(message: dict, max_chars: int = 60) -> str:
    """Best-effort short preview of a tool_call's arguments JSON."""
    tcs = message.get("tool_calls") or []
    if not tcs:
        return ""
    first = tcs[0] if isinstance(tcs, list) and tcs else {}
    fn = first.get("function") or {} if isinstance(first, dict) else {}
    args = str(fn.get("arguments") or "")
    args = args.replace("\n", " ").strip()
    if len(args) > max_chars:
        return args[:max_chars].rstrip() + "…"
    return args


def clear_old_tool_results(
    messages: list[dict],
    *,
    keep_recent_pairs: int = DEFAULT_KEEP_RECENT_PAIRS,
    aggressive: bool = False,
) -> tuple[list[dict], int]:
    """Stub out tool_result bodies older than the most recent
    ``keep_recent_pairs`` pairs.

    Mutates ``messages`` in place. Returns (messages, num_stubbed).

    Rules (walking newest → oldest):

    1. The newest ``keep_recent_pairs`` pairs are *always* kept verbatim,
       regardless of tool name. This matches Anthropic's
       ``clear_tool_uses_20250919`` default of 3 recent uses.

    2. Short-lived tools (multiplier < 1) can be evicted *earlier* than
       step 1 would suggest. Specifically, if more recent same-tool pairs
       already fill the per-tool slot quota
       ``max(1, round(keep_recent_pairs * mult))``, the current pair is
       stubbed even if it would otherwise be inside the global keep window.

    3. Long-lived tools (multiplier > 1) get *extra* slots beyond the
       global keep window. They stay verbatim until their per-tool slot
       quota is filled.

    4. When ``aggressive=True``, multipliers are ignored entirely; only
       the global ``keep_recent_pairs`` rule applies (used near the hard
       ceiling).
    """
    pairs = _tool_pair_indexes(messages)
    if not pairs:
        return messages, 0

    # Walk newest → oldest. Each tool gets an effective keep window of
    #     keep_N_eff = max(1, round(keep_recent_pairs * multiplier))
    # A pair is kept iff its global rank (newest=1) is <= keep_N_eff.
    # Long-lived tools (mult > 1) naturally get bigger windows; short-lived
    # (mult < 1) get smaller. This matches Anthropic's ``clear_tool_uses``
    # semantics: the "kept" set is contiguous from the newest pair.
    global_rank = 0
    decisions: list[tuple[int, int, str, bool]] = []  # (assistant_idx, tool_idx, name, keep)
    for assistant_idx, tool_idx, name in reversed(pairs):
        global_rank += 1
        if aggressive:
            keep = global_rank <= keep_recent_pairs
        else:
            mult = _TOOL_TTL_MULTIPLIER.get(name, 1.0)
            keep_n_eff = max(1, int(round(keep_recent_pairs * mult)))
            keep = global_rank <= keep_n_eff
        decisions.append((assistant_idx, tool_idx, name, keep))

    # Second pass: apply stubs.
    stubbed = 0
    for assistant_idx, tool_idx, name, keep in decisions:
        if keep:
            continue
        tool_msg = messages[tool_idx]
        content = tool_msg.get("content")
        if not isinstance(content, str) or content.startswith("[result for "):
            continue
        preview = _args_preview(messages[assistant_idx])
        tool_msg["content"] = _stub_for(name, preview)
        stubbed += 1

    return messages, stubbed


# ---------------------------------------------------------------------------
# Latency-aware compaction entry point
# ---------------------------------------------------------------------------

def compact_if_over_latency_threshold(
    messages: list[dict],
    *,
    threshold_tokens: int = LATENCY_THRESHOLD_TOKENS,
    hard_ceiling_tokens: int = HARD_CEILING_TOKENS,
    path: str = "unspecified",
) -> tuple[list[dict], dict]:
    """Compact a live message list when its token count crosses a threshold.

    Returns (messages, stats_dict). ``stats_dict`` contains:
        before_tokens, after_tokens, pairs_stubbed, action
    where ``action`` is one of ``"none"|"soft"|"hard"``.
    """
    before = estimate_tokens(messages)
    stats = {
        "before_tokens": before,
        "after_tokens": before,
        "pairs_stubbed": 0,
        "action": "none",
    }
    if before < threshold_tokens:
        return messages, stats

    # First pass: soft compaction with per-tool TTL.
    _, stubbed_soft = clear_old_tool_results(
        messages,
        keep_recent_pairs=DEFAULT_KEEP_RECENT_PAIRS,
        aggressive=False,
    )
    after_soft = estimate_tokens(messages)
    stats["pairs_stubbed"] = stubbed_soft
    stats["after_tokens"] = after_soft
    stats["action"] = "soft"

    if after_soft <= hard_ceiling_tokens:
        logger.info(
            f"NOVA_CONTEXT_COMPACT | path={path} action=soft "
            f"before={before} after={after_soft} pairs_stubbed={stubbed_soft}"
        )
        return messages, stats

    # Second pass: hard compaction — ignore per-tool multipliers.
    _, stubbed_hard = clear_old_tool_results(
        messages,
        keep_recent_pairs=1,
        aggressive=True,
    )
    after_hard = estimate_tokens(messages)
    stats["pairs_stubbed"] = stubbed_soft + stubbed_hard
    stats["after_tokens"] = after_hard
    stats["action"] = "hard"
    logger.warning(
        f"NOVA_CONTEXT_COMPACT | path={path} action=hard "
        f"before={before} after_soft={after_soft} after_hard={after_hard} "
        f"pairs_stubbed={stubbed_soft + stubbed_hard}"
    )
    return messages, stats


# ---------------------------------------------------------------------------
# History-storage trimming (the partner to live compaction)
# ---------------------------------------------------------------------------

def trim_tool_result_for_history(text: str, *, limit: int = HISTORY_TOOL_RESULT_CAP) -> str:
    """Apply the ``HISTORY_TOOL_RESULT_CAP`` to a tool result before it lands
    in the persisted history or restored context.

    Distinct from ``_trim_tool_result_for_llm`` in bot.py which keeps a 64K
    cap for the live in-turn result. This trim is only applied to results
    that are about to ride along in subsequent turns' restored context.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[trimmed for history storage]"
