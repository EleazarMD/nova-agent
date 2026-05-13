import os
import tempfile
import unittest

from nova.store import get_action_ledger_entry, init_db
from nova.action_ledger import create_action_entry
from nova.store import upsert_action_ledger_entry
from nova.turn_orchestrator import TurnIntent, TurnState, decide_turn, execute_turn_plan_result


class TurnOrchestratorTeslaNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="nova_tesla_navigation_", suffix=".db")
        os.close(fd)
        await init_db(self.db_path)

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    async def test_tesla_navigation_request_creates_awaiting_confirmation_action(self):
        state = TurnState()
        plan = await decide_turn("Send directions to 266 Brookview St in Channelview Texas to my Tesla", state)
        calls = []
        messages = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "unexpected"

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_upsert = store.upsert_action_ledger_entry

        async def temp_upsert(entry):
            return await original_upsert(entry, path=self.db_path)

        store.upsert_action_ledger_entry = temp_upsert
        try:
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
        finally:
            store.upsert_action_ledger_entry = original_upsert

        self.assertEqual(plan.intent, TurnIntent.TESLA_NAVIGATION_PLAN)
        self.assertTrue(result.handled)
        self.assertEqual(result.stop_reason, "tesla_navigation_awaiting_confirmation")
        self.assertEqual(calls, [])
        self.assertTrue(state.active_action_id)
        entry = await get_action_ledger_entry(state.active_action_id, path=self.db_path)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "awaiting_confirmation")
        self.assertEqual(entry["required_tools"], ["tesla_navigation"])
        self.assertEqual(entry["required_evidence"], ["tesla_navigation_result"])
        self.assertIn("266 Brookview", entry["target"]["destination"])
        self.assertIn("I have not sent it yet", result.response)
        self.assertTrue(result.response)
        self.assertEqual(result.display_text, result.response)

    async def test_tesla_navigation_model_three_hint_is_stored(self):
        state = TurnState()
        plan = await decide_turn("Send directions to Starbucks on I-10 to the Model 3", state)

        self.assertEqual(plan.intent, TurnIntent.TESLA_NAVIGATION_PLAN)
        self.assertEqual(plan.context["vehicle_hint"], "Model 3")
        self.assertIn("Starbucks", plan.context["destination"])

    async def test_confirmation_after_plan_binds_to_active_action(self):
        state = TurnState(active_action_id="action-1", active_goal="Prepare to send Tesla navigation to Starbucks")
        plan = await decide_turn("Yes please", state)

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_CONFIRMATION)
        self.assertEqual(plan.context["action_id"], "action-1")

    async def test_confirmation_passes_through_for_llm_binding_if_turn_state_missing(self):
        state = TurnState()
        plan = await decide_turn("Go ahead", state)

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)

    async def test_confirmation_executes_navigation_and_records_success_evidence(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks",
            status="awaiting_confirmation",
            user_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            target_json={"destination": "Starbucks I-10 and Garth", "vehicle_hint": "", "vin": ""},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("Yes please", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "Navigation sent to Tesla: Starbucks I-10 and Garth"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        store.get_action_ledger_entry = temp_get
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append

        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        self.assertEqual(calls, [("tesla_navigation", {"destination": "Starbucks I-10 and Garth"})])
        self.assertEqual(result.stop_reason, "tesla_navigation_sent")
        self.assertEqual(result.tools_used, ["tesla_navigation"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["evidence_status"], "satisfied")
        self.assertEqual(stored["last_tool_result"]["status"], "success")

    async def test_confirmation_records_failed_navigation_without_claiming_sent(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks",
            status="awaiting_confirmation",
            target_json={"destination": "Starbucks", "vehicle_hint": "", "vin": ""},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("Go ahead", state)

        async def dispatch_tool(_name, _args):
            return "Tesla account not connected"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        store.get_action_ledger_entry = temp_get
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append

        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        self.assertEqual(result.stop_reason, "tesla_navigation_failed")
        self.assertIn("did not confirm success", result.response)
        self.assertEqual(stored["status"], "tool_failed")
        self.assertEqual(stored["last_tool_result"]["status"], "failed")

    async def test_confirmation_refuses_unresolved_vehicle_hint(self):
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
        plan = await decide_turn("Yes please", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "unexpected"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        store.get_action_ledger_entry = temp_get
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get

        self.assertEqual(calls, [("tesla_control", {"action": "vehicles"})])
        self.assertEqual(result.stop_reason, "tesla_navigation_vehicle_unresolved")
        self.assertIn("wrong Tesla", result.response)

    async def test_confirmation_resolves_vehicle_hint_to_vin_before_navigation(self):
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
        plan = await decide_turn("Yes please", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            if name == "tesla_control":
                return "Your Tesla vehicles:\n- Black Panther (Model 3): online [VIN: VIN3]\n- Ruby (Model X): online [VIN: VINX]"
            return "Navigation sent to Tesla: Starbucks"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence
        original_upsert = store.upsert_action_ledger_entry

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        async def temp_upsert(updated, **_kwargs):
            return await original_upsert(updated, path=self.db_path)

        store.get_action_ledger_entry = temp_get
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        store.upsert_action_ledger_entry = temp_upsert
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append
            store.upsert_action_ledger_entry = original_upsert

        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        self.assertEqual(calls, [
            ("tesla_control", {"action": "vehicles"}),
            ("tesla_navigation", {"destination": "Starbucks", "vin": "VIN3"}),
        ])
        self.assertEqual(result.stop_reason, "tesla_navigation_sent")
        self.assertEqual(result.tools_used, ["tesla_control", "tesla_navigation"])
        self.assertEqual(stored["target"]["vin"], "VIN3")
        self.assertEqual(stored["target"]["resolved_vehicle"]["display_name"], "Black Panther")

    async def test_retry_failed_navigation_reuses_vin_and_records_success(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks on Model 3",
            status="tool_failed",
            target_json={"destination": "Starbucks", "vehicle_hint": "Model 3", "vin": "VIN3"},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        entry.tool_attempts.append({"source": "tesla_navigation", "status": "failed", "summary": "timeout"})
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("try again", state)
        calls = []

        async def dispatch_tool(name, args):
            calls.append((name, args))
            return "Navigation sent to Tesla: Starbucks"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        store.get_action_ledger_entry = temp_get
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append

        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_RETRY)
        self.assertEqual(calls, [("tesla_navigation", {"destination": "Starbucks", "vin": "VIN3"})])
        self.assertEqual(result.stop_reason, "tesla_navigation_retry_sent")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["evidence_status"], "satisfied")
        self.assertEqual(len(stored["tool_attempts"]), 2)
        self.assertTrue(stored["last_tool_result"]["payload"]["retry"])

    async def test_retry_failed_navigation_records_second_failure(self):
        entry = create_action_entry(
            intent=TurnIntent.TESLA_NAVIGATION_PLAN.value,
            active_goal="Prepare to send Tesla navigation to Starbucks",
            status="tool_failed",
            target_json={"destination": "Starbucks", "vehicle_hint": "", "vin": ""},
            required_tools=["tesla_navigation"],
            required_evidence=["tesla_navigation_result"],
        )
        entry.tool_attempts.append({"source": "tesla_navigation", "status": "failed", "summary": "first failure"})
        await upsert_action_ledger_entry(entry, path=self.db_path)
        state = TurnState(active_action_id=entry.action_id, active_goal=entry.active_goal)
        plan = await decide_turn("send it again", state)

        async def dispatch_tool(_name, _args):
            return "vehicle unavailable"

        async def send_server_msg(_msg):
            pass

        async def persist_turn(_role, _content):
            pass

        import nova.store as store
        original_get = store.get_action_ledger_entry
        original_update = store.update_action_ledger_status
        original_append = store.append_action_ledger_evidence

        async def temp_get(action_id, **_kwargs):
            return await original_get(action_id, path=self.db_path)

        async def temp_update(action_id, status, **kwargs):
            return await original_update(action_id, status, path=self.db_path, **kwargs)

        async def temp_append(action_id, evidence, **kwargs):
            return await original_append(action_id, evidence, path=self.db_path, **kwargs)

        store.get_action_ledger_entry = temp_get
        store.update_action_ledger_status = temp_update
        store.append_action_ledger_evidence = temp_append
        try:
            result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)
        finally:
            store.get_action_ledger_entry = original_get
            store.update_action_ledger_status = original_update
            store.append_action_ledger_evidence = original_append

        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        self.assertEqual(result.stop_reason, "tesla_navigation_retry_failed")
        self.assertIn("did not confirm success", result.response)
        self.assertEqual(stored["status"], "tool_failed")
        self.assertEqual(len(stored["tool_attempts"]), 2)
        self.assertEqual(stored["last_tool_result"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
