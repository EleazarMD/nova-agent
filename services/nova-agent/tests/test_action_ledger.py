import os
import tempfile
import unittest

from nova.action_ledger import action_is_active, create_action_entry
from nova.store import (
    append_action_ledger_evidence,
    get_action_ledger_entry,
    get_action_ledger_summary,
    get_active_action_ledger_entry,
    get_recent_action_ledger_entries,
    init_db,
    update_action_ledger_status,
    upsert_action_ledger_entry,
)


class ActionLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="nova_action_ledger_", suffix=".db")
        os.close(fd)
        await init_db(self.db_path)

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    async def test_action_ledger_round_trip_and_active_lookup(self):
        entry = create_action_entry(
            intent="tesla_navigation_send",
            active_goal="Send Starbucks I-10 and Garth to Model 3",
            status="awaiting_confirmation",
            user_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            target_json={"vehicle": "Model 3", "destination": "Starbucks"},
            required_tools=["tesla_control"],
            required_evidence=["tesla_send_result"],
        )

        await upsert_action_ledger_entry(entry, path=self.db_path)
        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        active = await get_active_action_ledger_entry(user_id="user-1", session_id="session-1", path=self.db_path)

        self.assertIsNotNone(stored)
        self.assertEqual(stored["intent"], "tesla_navigation_send")
        self.assertEqual(stored["target"]["vehicle"], "Model 3")
        self.assertEqual(stored["required_tools"], ["tesla_control"])
        self.assertEqual(active["action_id"], entry.action_id)
        self.assertTrue(action_is_active(active))

    async def test_successful_evidence_satisfies_action(self):
        entry = create_action_entry(
            intent="tesla_navigation_send",
            active_goal="Send destination to Model 3",
            status="running",
            user_id="user-1",
            session_id="session-1",
            required_evidence=["tesla_send_result"],
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)

        ok = await append_action_ledger_evidence(
            entry.action_id,
            {"source": "tesla_control", "status": "success", "summary": "destination accepted"},
            status="completed",
            path=self.db_path,
        )
        stored = await get_action_ledger_entry(entry.action_id, path=self.db_path)
        active = await get_active_action_ledger_entry(user_id="user-1", session_id="session-1", path=self.db_path)

        self.assertTrue(ok)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["evidence_status"], "satisfied")
        self.assertEqual(stored["last_tool_result"]["source"], "tesla_control")
        self.assertIsNone(active)
        self.assertFalse(action_is_active(stored))

    async def test_failed_action_remains_available_for_status_or_retry(self):
        entry = create_action_entry(
            intent="tesla_navigation_send",
            active_goal="Send destination to Model 3",
            status="running",
            user_id="user-1",
            session_id="session-1",
        )
        await upsert_action_ledger_entry(entry, path=self.db_path)

        await update_action_ledger_status(
            entry.action_id,
            "tool_failed",
            evidence_status="failed",
            user_visible_status="Tesla send failed before confirmation.",
            last_error="tesla_api_timeout",
            path=self.db_path,
        )
        active = await get_active_action_ledger_entry(user_id="user-1", session_id="session-1", path=self.db_path)

        self.assertIsNotNone(active)
        self.assertEqual(active["status"], "tool_failed")
        self.assertEqual(active["last_error"], "tesla_api_timeout")
        self.assertTrue(action_is_active(active))

    async def test_recent_entries_and_summary_are_read_only_views(self):
        planned = create_action_entry(
            intent="tesla_navigation_plan",
            active_goal="Plan Tesla navigation",
            status="awaiting_confirmation",
            user_id="user-1",
            session_id="session-1",
        )
        failed = create_action_entry(
            intent="tesla_navigation_plan",
            active_goal="Retry Tesla navigation",
            status="tool_failed",
            user_id="user-1",
            session_id="session-1",
        )
        await upsert_action_ledger_entry(planned, path=self.db_path)
        await upsert_action_ledger_entry(failed, path=self.db_path)
        await append_action_ledger_evidence(
            failed.action_id,
            {"source": "tesla_navigation", "status": "failed", "summary": "offline"},
            status="tool_failed",
            path=self.db_path,
        )

        recent = await get_recent_action_ledger_entries(limit=10, path=self.db_path)
        failures = await get_recent_action_ledger_entries(limit=10, status="tool_failed", path=self.db_path)
        summary = await get_action_ledger_summary(limit=10, path=self.db_path)

        self.assertEqual(len(recent), 2)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["action_id"], failed.action_id)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["tool_attempts"], 1)
        self.assertTrue(summary["read_only"])
        self.assertTrue(summary["durable"])


if __name__ == "__main__":
    unittest.main()
