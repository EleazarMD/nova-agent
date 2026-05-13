import os
import tempfile
import unittest

from nova.action_ledger import create_action_entry
from nova.store import get_action_ledger_entry, init_db, upsert_action_ledger_entry
from nova.turn_orchestrator import TurnIntent, TurnState, decide_turn, execute_turn_plan_result


class TurnOrchestratorTeslaIncidentReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="nova_tesla_incident_replay_", suffix=".db")
        os.close(fd)
        await init_db(self.db_path)

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    async def _execute_with_temp_store(self, plan, state, dispatch_tool):
        messages = []
        persisted = []

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(role, content):
            persisted.append((role, content))

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_active = store.get_active_action_ledger_entry
        original_upsert = store.upsert_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_active(**_kwargs):
            return await original_active(path=self.db_path)

        async def temp_upsert(entry, **_kwargs):
            return await original_upsert(entry, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        store.get_action_ledger_entry = temp_get
        store.get_active_action_ledger_entry = temp_active
        store.upsert_action_ledger_entry = temp_upsert
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.get_active_action_ledger_entry = original_active
            store.upsert_action_ledger_entry = original_upsert
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append
        return result, messages, persisted

    async def test_initial_request_only_plans_and_requires_confirmation(self):
        state = TurnState()
        plan = await decide_turn("Send directions to Starbucks on I-10 to my Tesla", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "unexpected"

        result, _messages, _persisted = await self._execute_with_temp_store(plan, state, dispatch_tool)
        entry = await get_action_ledger_entry(state.active_action_id, path=self.db_path)

        self.assertEqual(plan.intent, TurnIntent.TESLA_NAVIGATION_PLAN)
        self.assertEqual(calls, [])
        self.assertEqual(result.stop_reason, "tesla_navigation_awaiting_confirmation")
        self.assertIn("I have not sent it yet", result.response)
        self.assertEqual(entry["status"], "awaiting_confirmation")
        self.assertIn("Starbucks", entry["target"]["destination"])

    async def test_model_three_confirmation_resolves_vin_before_send(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks on Model 3",
            status="awaiting_confirmation",
            target_json={"destination": "Starbucks", "vehicle_hint": "Model 3", "vin": ""},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("yes please", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            if name == "tesla_control":
                return "Your Tesla vehicles:\n- Black Panther (Model 3): online [VIN: VIN3]\n- Ruby (Model X): online [VIN: VINX]"
            return "Navigation sent to Tesla: Starbucks"

        result, _messages, _persisted = await self._execute_with_temp_store(plan, state, dispatch_tool)
        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)

        self.assertEqual(calls, [
            ("tesla_control", {"action": "vehicles"}),
            ("tesla_navigation", {"destination": "Starbucks", "vin": "VIN3"}),
        ])
        self.assertEqual(result.stop_reason, "tesla_navigation_sent")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["target"]["resolved_vehicle"]["display_name"], "Black Panther")

    async def test_unresolved_model_three_never_falls_back_to_model_x(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks on Model 3",
            status="awaiting_confirmation",
            target_json={"destination": "Starbucks", "vehicle_hint": "Model 3", "vin": ""},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("go ahead and send it", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "Your Tesla vehicles:\n- Ruby (Model X): online [VIN: VINX]"

        result, _messages, _persisted = await self._execute_with_temp_store(plan, state, dispatch_tool)
        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)

        self.assertEqual(calls, [("tesla_control", {"action": "vehicles"})])
        self.assertEqual(result.stop_reason, "tesla_navigation_vehicle_unresolved")
        self.assertIn("wrong Tesla", result.response)
        self.assertEqual(stored["status"], "awaiting_confirmation")

    async def test_failure_report_then_retry_succeeds_without_false_claim_before_evidence(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to 266 Brookview on Model 3",
            status="tool_failed",
            target_json={"destination": "266 Brookview St", "vehicle_hint": "Model 3", "vin": "VIN3"},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        entry.tool_attempts.append({"source": "tesla_navigation", "status": "failed", "summary": "first attempt failed"})
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)

        failure_plan = await decide_turn("directions never showed up", state)
        async def no_tool_dispatch(name, args):
            raise AssertionError(f"unexpected tool call {name} {args}")

        failure_result, _messages, _persisted = await self._execute_with_temp_store(failure_plan, state, no_tool_dispatch)
        self.assertEqual(failure_plan.intent, TurnIntent.ACTIVE_ACTION_FAILURE_REPORT)
        self.assertEqual(failure_result.stop_reason, "active_action_failure_reported")
        self.assertIn("unresolved", failure_result.response)

        retry_plan = await decide_turn("try again", state)
        calls = []

        async def retry_dispatch(name, args):
            calls.append((name, args))
            return "Navigation sent to Tesla: 266 Brookview St"

        retry_result, _messages, _persisted = await self._execute_with_temp_store(retry_plan, state, retry_dispatch)
        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)

        self.assertEqual(retry_plan.intent, TurnIntent.ACTIVE_ACTION_RETRY)
        self.assertEqual(calls, [("tesla_navigation", {"destination": "266 Brookview St", "vin": "VIN3"})])
        self.assertEqual(retry_result.stop_reason, "tesla_navigation_retry_sent")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(len(stored["tool_attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
