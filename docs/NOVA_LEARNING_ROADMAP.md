# Nova Zero-Wait Architecture & Neural Learning Roadmap

## Objective
Enable Nova to learn from organic interactions and progressively bypass the LLM cloud deliberation latency by transforming historically successful multi-turn interactions into zero-wait deterministic paths.

## Phase 1: Event Sourcing & Canonical Capture (Complete)
- **Goal:** Fix the pipeline so that Nova actually records raw interaction data.
- **Implementation:** Added `CanonicalTurnText` to separate the routing envelope (`[User location]`, `🧭 MODE POLICY`) from the actual semantic request. Created `learning_events` table in SQLite to track `user_turn_received`, `orchestrator_decision`, `tool_call_started`, and `tool_call_completed`.

## Phase 2: Post-LLM Attribution & Episode Extraction (Complete)
- **Goal:** Make sense of the event traces.
- **Implementation:** At the end of a session, a background async job `consolidate_session_learning` looks backwards over the event traces. If it finds a successful tool execution (e.g. `save_memory`), it extracts the preceding user utterance and saves the mapping to `learned_plan_candidates` with an initial confidence.

## Phase 3: Assistive Routing (Complete)
- **Goal:** Use learned patterns to accelerate the LLM contextually without writing static rules.
- **Implementation:** `decide_turn` checks incoming user input against `learned_plan_candidates`. If confidence is ≥0.80, it injects a zero-shot instruction into the LLM system frame (e.g., `[SYSTEM ASSISTIVE ROUTING...]`). This bypasses the LLM's deliberation phase (the "thinking" latency) but still utilizes the LLM's parameter-extraction capabilities.

## Phase 4: Neural Semantic Matching (Vector Search) (Complete)
- **Goal:** Generalize patterns from specific strings to entire semantic clusters.
- **Implementation:** Hooked `learned_plan_candidates` into the local NVIDIA Llama 3.2 NV EmbedQA model. When `upsert_learned_plan_candidate` runs, it vectorizes the canonical user text. During `decide_turn`, Nova embeds the incoming audio transcript and computes Cosine Similarity. A similarity score > 0.75 activates Assistive Routing.

## Phase 5: Auto-Action Promotion (True Zero-Wait) (Pending)
- **Goal:** Completely bypass the LLM for high-confidence routines.
- **Implementation:** When a candidate's confidence is >0.95 or usage >3x, promote it from `ASSISTIVE` to `AUTO_ACTION`. During `decide_turn`, if `AUTO_ACTION` matches, invoke a lightning-fast local SLM or grammar parser specifically to extract the tool arguments. Dispatch the `StrategyHandler` immediately. The response latency drops from ~3s to ~200ms.

## Phase 6: Outcome Penalty & Unlearning (Pending)
- **Goal:** Prune bad generalizations.
- **Implementation:** If Nova executes an Assistive or Auto-Action, and the immediate user response is negative or corrective ("No", "Stop", "I meant"), the background consolidator slashes the `success_score` and confidence of the matched rule. Below a threshold, the rule is purged from `learned_plan_candidates`.
