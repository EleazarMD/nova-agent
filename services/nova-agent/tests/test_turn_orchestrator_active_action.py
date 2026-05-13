import unittest
from unittest.mock import patch

from nova.turn_orchestrator import TurnIntent, TurnState, decide_turn, execute_turn_plan_result


class TurnOrchestratorActiveActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_yes_please_binds_to_active_action_confirmation(self):
        state = TurnState(active_action_id="action-1", active_goal="Send Starbucks to Model 3")
        plan = await decide_turn("Yes please", state)

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_CONFIRMATION)
        self.assertEqual(plan.context["action_id"], "action-1")
        self.assertEqual(plan.allowed_tools, [])

    async def test_status_question_binds_to_active_action_status(self):
        state = TurnState(active_action_id="action-1", active_goal="Send Starbucks to Model 3")
        plan = await decide_turn("What's taking long?", state)

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_STATUS)

    async def test_failure_report_binds_to_active_action_failure_report(self):
        state = TurnState(active_action_id="action-1", active_goal="Send Starbucks to Model 3")
        plan = await decide_turn("The directions never showed up in my Tesla", state)

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_FAILURE_REPORT)

    async def test_active_action_status_reports_ledger_without_tools(self):
        state = TurnState(active_action_id="action-1", active_goal="Send Starbucks to Model 3")
        plan = await decide_turn("What's taking long?", state)
        messages = []
        persisted = []
        calls = []
        entry = {
            "action_id": "action-1",
            "active_goal": "Send Starbucks to Model 3",
            "status": "tool_failed",
            "evidence_status": "failed",
            "user_visible_status": "Tesla command timed out.",
            "last_error": "tesla_api_timeout",
            "tool_attempts": [{"source": "tesla_control", "status": "failed"}],
        }

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "unexpected"

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(role, content):
            persisted.append((role, content))

        async def fake_get_action_ledger_entry(action_id):
            self.assertEqual(action_id, "action-1")
            return entry

        with patch("nova.store.get_action_ledger_entry", fake_get_action_ledger_entry):
            result = await execute_turn_plan_result(
                plan,
                state,
                dispatch_tool,
                send_server_msg,
                persist_turn,
                user_id="user-1",
                conversation_id="conversation-1",
                session_id="session-1",
            )

        self.assertTrue(result.handled)
        self.assertEqual(result.stop_reason, "active_action_status_returned")
        self.assertEqual(result.tools_used, [])
        self.assertEqual(calls, [])
        self.assertIn("Ledger status is tool_failed", result.response)
        self.assertIn("will not claim the action completed", result.response)
        self.assertTrue(any(role == "assistant" for role, _content in persisted))
        self.assertTrue(result.response)
        self.assertEqual(result.display_text, result.response)
        self.assertFalse(any(msg.get("type") == "validated" for msg in messages))

    async def test_confirmation_does_not_fake_execution_without_executor(self):
        state = TurnState(active_action_id="action-1", active_goal="Send Starbucks to Model 3")
        plan = await decide_turn("Go ahead and send it", state)
        entry = {
            "action_id": "action-1",
            "active_goal": "Send Starbucks to Model 3",
            "status": "awaiting_confirmation",
            "evidence_status": "missing",
            "tool_attempts": [],
        }

        async def dispatch_tool(_name, _args):
            return "unexpected"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        async def fake_get_action_ledger_entry(_action_id):
            return entry

        with patch("nova.store.get_action_ledger_entry", fake_get_action_ledger_entry):
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_CONFIRMATION)
        self.assertEqual(result.stop_reason, "active_action_confirmation_requires_executor")
        self.assertIn("I have not executed it yet", result.response)
        self.assertIn("will not pretend", result.response)


if __name__ == "__main__":
    unittest.main()
