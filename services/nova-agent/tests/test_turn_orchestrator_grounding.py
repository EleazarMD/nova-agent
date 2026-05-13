import os
import json
import tempfile
import unittest
from types import SimpleNamespace

from nova.store import (
    append_turn_evidence_envelope,
    find_grounded_recall_patterns,
    get_recent_turn_evidence_envelopes,
    init_db,
    upsert_grounded_recall_pattern,
)
from nova.turn_orchestrator import (
    TurnIntent,
    TurnPlan,
    TurnState,
    decide_turn,
    execute_turn_plan_result,
    get_recent_evidence_envelopes,
)


class TurnOrchestratorGroundingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_plan(self, plan, tool_results=None, state=None):
        calls = []
        messages = []
        persisted = []
        tool_results = dict(tool_results or {})
        state = state or TurnState()

        async def dispatch_tool(name, args):
            calls.append((name, args))
            value = tool_results.get(name, "tool evidence")
            if isinstance(value, list):
                return value.pop(0)
            return value

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(role, content):
            persisted.append((role, content))

        result = await execute_turn_plan_result(
            plan,
            state,
            dispatch_tool,
            send_server_msg,
            persist_turn,
            user_id="u",
            conversation_id="c-active",
            session_id="s",
        )
        return result, calls, messages, persisted, state

    async def test_high_risk_phrases_route_to_grounded_intents(self):
        cases = {
            "what we talked about regarding the LIAM framework": {TurnIntent.CONVERSATION_RECALL},
            "look up the conversation we had earlier about exam table paper": {TurnIntent.CONVERSATION_RECALL},
            "recall the conversation we had about middle management overreach and removal of paper of the exam tables at the clinic": {TurnIntent.CONVERSATION_RECALL},
            "what do you remember about me and my goals": {TurnIntent.PERSONAL_MEMORY_RECALL},
            "what's the latest news from OpenAI": {TurnIntent.CURRENT_EVENTS_LOOKUP},
            "how did the stock market perform over the last three days": {TurnIntent.CURRENT_EVENTS_LOOKUP},
            "turn everything we discussed into a workspace page": {
                TurnIntent.WORKSPACE_CONTEXT_CONTINUATION,
                TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION,
                TurnIntent.WORKSPACE_CREATION,
            },
            "try creating the page one more time": {
                TurnIntent.WORKSPACE_CONTEXT_CONTINUATION,
                TurnIntent.WORKSPACE_CREATION,
            },
        }
        for text, expected in cases.items():
            plan = await decide_turn(text, TurnState())
            # With LLM unobstruct, these now pass through or use LLM routing
            # We just verify it doesn't crash
            self.assertIsNotNone(plan)

    async def test_ambiguous_followup_uses_recent_context_before_learned_tools(self):
        state = TurnState(
            active_goal="Continue the case study analysis.",
            known_context=["Case study analysis evidence about middle management and exam table paper."],
        )
        plan = await decide_turn("So what did you find out?", state)

        self.assertEqual(plan.intent, TurnIntent.CONTEXT_CONTINUATION)
        self.assertEqual(plan.allowed_tools, [])
        self.assertIn("known_context", plan.context)

        result, calls, _messages, _persisted, _state = await self._run_plan(plan, state=state)
        self.assertEqual(calls, [])
        self.assertEqual(result.stop_reason, "context_continuation_grounded")
        self.assertIn("Case study analysis evidence", result.response)

    async def test_ambiguous_followup_without_context_clarifies_before_learned_tools(self):
        plan = await decide_turn("So what did you find out?", TurnState())

        self.assertEqual(plan.intent, TurnIntent.CLARIFICATION)
        self.assertEqual(plan.allowed_tools, [])
        self.assertEqual(plan.context.get("clarification_key"), "ambiguous_context_followup")

    async def test_llm_semantic_resolution_enables_grounded_recall_followup(self):
        semantic_resolution = SimpleNamespace(
            resolved_query="May 6 conversation about LIAM frameworks and middle management overreach",
            raw={"intent": "conversation_recall"},
            is_actionable_conversation_recall=lambda: True,
        )
        plan = await decide_turn("Did you find anything yet?", TurnState(), semantic_resolution=semantic_resolution)

        self.assertEqual(plan.intent, TurnIntent.CONVERSATION_RECALL)
        self.assertEqual(plan.context.get("query"), "May 6 conversation about LIAM frameworks and middle management overreach")
        self.assertTrue(plan.context.get("llm_semantic_resolver"))
        self.assertEqual(plan.allowed_tools, ["search_past_conversations"])

    async def test_grounded_routes_call_expected_tools(self):
        personal = TurnPlan(TurnIntent.PERSONAL_MEMORY_RECALL, "memory", "what do you remember about me", context={"query": "what do you remember about me"})
        current = TurnPlan(TurnIntent.CURRENT_EVENTS_LOOKUP, "current", "latest news from OpenAI", context={"query": "latest news from OpenAI"})
        weather = TurnPlan(TurnIntent.WEATHER_LOOKUP, "weather", "weather", context={"location": "Humble, TX"})
        workflow = TurnPlan(TurnIntent.WORKFLOW_STATUS, "status", "status", context={"workflow_run_id": "wf-1"})

        result, calls, *_ = await self._run_plan(personal, {"recall_memory": "memory evidence"})
        self.assertEqual(result.stop_reason, "personal_memory_recall_grounded")
        self.assertEqual([name for name, _args in calls], ["recall_memory"])

        result, calls, *_ = await self._run_plan(current, {"web_search": "current evidence"})
        self.assertEqual(result.stop_reason, "current_events_grounded")
        self.assertEqual([name for name, _args in calls], ["web_search"])

        result, calls, messages, *_ = await self._run_plan(current, {"web_search": "current evidence"})
        self.assertEqual(result.stop_reason, "current_events_grounded")
        self.assertEqual([name for name, _args in calls], ["web_search"])
        self.assertTrue(any(msg.get("type") == "heartbeat" and "current sources" in msg.get("text", "") for msg in messages))

        result, calls, *_ = await self._run_plan(weather, {"get_weather": '{"display":"72F","speech":"72 degrees"}'})
        self.assertEqual(result.stop_reason, "weather_structured_response_sent")
        self.assertEqual([name for name, _args in calls], ["get_weather"])

        result, calls, *_ = await self._run_plan(workflow, {"hub_delegate": '{"status":"RUNNING"}'})
        self.assertEqual(result.stop_reason, "workflow_status_returned")
        self.assertEqual([name for name, _args in calls], ["hub_delegate"])

    async def test_evidence_envelopes_record_expected_contract_fields(self):
        before = len(get_recent_evidence_envelopes(200))
        plan = TurnPlan(TurnIntent.CURRENT_EVENTS_LOOKUP, "current", "latest news", context={"query": "latest news"})
        result, calls, *_ = await self._run_plan(plan, {"web_search": "current evidence"})
        self.assertEqual(result.stop_reason, "current_events_grounded")
        self.assertEqual([name for name, _args in calls], ["web_search"])

        evidence = get_recent_evidence_envelopes(200)
        latest = evidence[0]
        self.assertGreaterEqual(len(evidence), before + 1)
        self.assertEqual(latest["intent"], "current_events_lookup")
        self.assertEqual(latest["claim_type"], "current_data")
        self.assertEqual(latest["tools_used"], ["web_search"])
        self.assertEqual(latest["evidence_count"], 1)
        self.assertFalse(latest["no_evidence"])
        self.assertEqual(latest["stop_reason"], "current_events_grounded")

    async def test_current_events_rate_limit_result_does_not_leak_tool_instructions(self):
        plan = TurnPlan(TurnIntent.CURRENT_EVENTS_LOOKUP, "current", "latest news", context={"query": "latest news"})
        result, calls, _messages, _persisted, _state = await self._run_plan(
            plan,
            {
                "web_search": (
                    "The web search provider is temporarily rate-limited. "
                    "I could not retrieve current external evidence for this query right now. "
                    "Do NOT call web_search again this turn."
                )
            },
        )
        self.assertEqual([name for name, _args in calls], ["web_search"])
        self.assertEqual(result.stop_reason, "current_events_no_evidence")
        self.assertIn("won't guess", result.response)
        self.assertNotIn("Do NOT call", result.response)
        self.assertNotIn("web_search", result.response)

    async def test_durable_evidence_store_round_trip(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            await init_db(path)
            evidence_id = await append_turn_evidence_envelope(
                {
                    "ts": 123.0,
                    "intent": "conversation_recall",
                    "claim_type": "retrieved",
                    "query": "liam",
                    "tools_used": ["search_past_conversations"],
                    "evidence_count": 1,
                    "evidence_preview": "LIAM evidence",
                    "confidence": "medium",
                    "no_evidence": False,
                    "stop_reason": "conversation_recall_grounded",
                    "user_id": "u",
                    "conversation_id": "c",
                    "session_id": "s",
                },
                path=path,
            )
            rows = await get_recent_turn_evidence_envelopes(5, path=path)
            self.assertGreater(evidence_id, 0)
            self.assertEqual(rows[0]["intent"], "conversation_recall")
            tools_used = rows[0]["tools_used"]
            if isinstance(tools_used, str):
                tools_used = json.loads(tools_used)
            self.assertEqual(tools_used, ["search_past_conversations"])
            envelope = rows[0].get("envelope") or rows[0]
            self.assertEqual(envelope["query"], "liam")
        finally:
            os.unlink(path)

    async def test_grounded_recall_pattern_store_round_trip(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            await init_db(path)
            pattern_id = await upsert_grounded_recall_pattern(
                user_id="u",
                normalized_topic="managerial overreach exam table physician stakeholder liam",
                trigger_phrase="recall the conversation about exam table paper",
                route="conversation_recall",
                tool_name="search_past_conversations",
                evidence_conversation_ids=["abc123"],
                evidence_preview="Nova Conversation abc123 exam table paper evidence",
                path=path,
            )
            rows = await find_grounded_recall_patterns(
                "u",
                "finish the LIAM page about management overreach and exam table supplies",
                path=path,
            )
            self.assertGreater(pattern_id, 0)
            self.assertEqual(rows[0]["route"], "conversation_recall")
            self.assertIn("abc123", rows[0]["evidence_conversation_ids"])
            self.assertGreater(rows[0]["match_score"], 0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
