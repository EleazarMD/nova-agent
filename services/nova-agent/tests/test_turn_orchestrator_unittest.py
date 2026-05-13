import unittest

from nova.turn_orchestrator import TurnIntent, TurnState, decide_turn, execute_turn_plan_result
from nova.turn_orchestrator import STATE_METADATA_KEY, turn_state_from_metadata, turn_state_to_metadata_value


class TurnOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_then_workspace_uses_one_cig_search_and_sets_pending_scribe(self):
        state = TurnState()
        plan = decide_turn(
            "Find the email from Natalie about World Cup and create workspace advisory pages.",
            state,
        )
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "matching email context"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, ["query_cig"])
        self.assertEqual(calls, [("query_cig", {"domain": "search", "query": "Find the email from Natalie about World Cup and"})])
        self.assertTrue(state.pending_scribe)
        self.assertEqual(state.known_context, ["matching email context"])

    async def test_workspace_continuation_delegates_to_scribe_once(self):
        state = TurnState(active_goal="Create advisory pages", pending_scribe=True, known_context=["prior context"])
        plan = decide_turn("Single page advisories with all of the topics above.", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "scribe accepted"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.WORKSPACE_CREATION_CONTINUATION)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, ["hub_delegate"])
        self.assertEqual(calls[0][0], "hub_delegate")
        self.assertEqual(calls[0][1]["agent"], "scribe")
        self.assertIn("prior context", calls[0][1]["context"])
        self.assertEqual(calls[0][1]["params"]["work_order"]["workspace_target"], "Pi Workspace")
        self.assertIn("goal", calls[0][1]["params"]["work_order"])
        self.assertIn("deliverable", calls[0][1]["params"]["work_order"])
        self.assertIn("constraints", calls[0][1]["params"]["work_order"])
        self.assertFalse(state.pending_scribe)

    async def test_workspace_request_without_structure_asks_clarification(self):
        state = TurnState()
        plan = decide_turn("Create a workspace document about neighborhood outreach.", state)
        calls = []
        messages = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "unexpected"

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.CLARIFICATION)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, [])
        self.assertEqual(calls, [])
        self.assertEqual(state.pending_clarification, "workspace_structure")
        self.assertEqual(result.stop_reason, "awaiting_clarification")
        self.assertTrue(any("one polished page" in str(msg) for msg in messages))

    async def test_clarification_answer_delegates_and_clears_pending_clarification(self):
        state = TurnState(
            active_goal="Create a workspace document about neighborhood outreach.",
            pending_clarification="workspace_structure",
            known_context=["prior context"],
        )
        plan = decide_turn("One polished page.", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "scribe accepted"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.WORKSPACE_CREATION_CONTINUATION)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, ["hub_delegate"])
        self.assertEqual(calls[0][0], "hub_delegate")
        self.assertEqual(calls[0][1]["params"]["work_order"]["goal"], "Create a workspace document about neighborhood outreach.")
        self.assertEqual(state.pending_clarification, "")

    def test_pending_clarification_does_not_capture_unrelated_short_reply(self):
        state = TurnState(
            active_goal="Create a workspace document about neighborhood outreach.",
            pending_clarification="workspace_structure",
        )
        plan = decide_turn("Actually never mind.", state)

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)

    def test_pending_clarification_does_not_delegate_on_generic_workspace_word(self):
        state = TurnState(
            active_goal="Create a workspace document about neighborhood outreach.",
            pending_clarification="workspace_structure",
        )
        plan = decide_turn("Open my workspace.", state)

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)

    def test_pass_through_for_general_questions(self):
        state = TurnState()
        plan = decide_turn("What is int64?", state)

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)

    async def test_research_brief_triggers_workflow_and_stores_run_id(self):
        state = TurnState()
        plan = decide_turn("Research neighborhood outreach grants and make me a brief.", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return '{"ok": true, "workflowRunId": "wf-123"}'

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.WORKFLOW_TRIGGER)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, ["hub_delegate"])
        self.assertEqual(calls[0][0], "hub_delegate")
        self.assertEqual(calls[0][1]["agent"], "orchestrator")
        self.assertEqual(calls[0][1]["method"], "workflows.trigger")
        self.assertEqual(calls[0][1]["params"]["name"], "atlas-research-brief")
        self.assertEqual(state.active_workflow_run_id, "wf-123")
        self.assertEqual(state.active_workflow_name, "atlas-research-brief")

    async def test_workflow_trigger_without_run_id_does_not_store_active_workflow(self):
        state = TurnState()
        plan = decide_turn("Research neighborhood outreach grants and make me a brief.", state)

        async def dispatch_tool(_name, _args):
            return "Error delegating to orchestrator: Hub RPC error"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertTrue(result.handled)
        self.assertEqual(result.stop_reason, "workflow_trigger_failed")
        self.assertEqual(state.active_workflow_run_id, "")
        self.assertIn("did not return a workflow run ID", result.response)

    async def test_workflow_status_uses_active_run_id(self):
        state = TurnState(
            active_workflow_run_id="wf-123",
            active_workflow_name="atlas-research-brief",
            active_workflow_goal="Research neighborhood outreach grants and make me a brief.",
        )
        plan = decide_turn("Is it done yet?", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return '{"ok": true, "run": {"status": "RUNNING"}}'

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.WORKFLOW_STATUS)
        self.assertTrue(result.handled)
        self.assertEqual(result.tools_used, ["hub_delegate"])
        self.assertEqual(calls[0][1]["method"], "workflows.getRun")
        self.assertEqual(calls[0][1]["params"]["run_id"], "wf-123")
        self.assertEqual(result.stop_reason, "workflow_status_returned")

    def test_workflow_status_without_active_run_passes_through(self):
        state = TurnState()
        plan = decide_turn("Is it done yet?", state)

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)

    def test_turn_state_metadata_round_trip(self):
        state = TurnState(
            active_goal="Create pages",
            known_context=["context"],
            suggested_topics=["topic"],
            pending_scribe=True,
            pending_clarification="workspace_structure",
            active_workflow_run_id="wf-123",
            active_workflow_name="atlas-research-brief",
            active_workflow_goal="Research grants",
            last_intent="lookup_then_workspace_creation",
            turns_handled=3,
        )
        metadata = {STATE_METADATA_KEY: turn_state_to_metadata_value(state)}
        restored = turn_state_from_metadata(metadata)

        self.assertEqual(restored.active_goal, "Create pages")
        self.assertEqual(restored.known_context, ["context"])
        self.assertEqual(restored.suggested_topics, ["topic"])
        self.assertTrue(restored.pending_scribe)
        self.assertEqual(restored.pending_clarification, "workspace_structure")
        self.assertEqual(restored.active_workflow_run_id, "wf-123")
        self.assertEqual(restored.active_workflow_name, "atlas-research-brief")
        self.assertEqual(restored.active_workflow_goal, "Research grants")
        self.assertEqual(restored.last_intent, "lookup_then_workspace_creation")
        self.assertEqual(restored.turns_handled, 3)


if __name__ == "__main__":
    unittest.main()
