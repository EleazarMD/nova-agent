import unittest
import asyncio

from nova.voice_turn_runtime import VoiceTurnRuntime


class VoiceTurnRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def _runtime(self):
        messages = []
        persisted = []
        synced = []

        async def send_server_msg(msg):
            messages.append(msg)

        async def persist_turn(role, content):
            persisted.append((role, content))

        async def sync_backend(*args, **kwargs):
            synced.append((args, kwargs))

        runtime = VoiceTurnRuntime(
            turn_id="turn-1",
            conversation_id="conversation-1",
            session_id="session-1",
            user_id="user-1",
            send_server_msg=send_server_msg,
            persist_turn=persist_turn,
            sync_backend=sync_backend,
            model="test-model",
        )
        return runtime, messages, persisted, synced

    async def test_direct_final_suppresses_duplicate_final_attempts(self):
        runtime, messages, persisted, synced = await self._runtime()

        await runtime.complete_with_text("First answer", speech_text="First answer", suppress_speech=False)
        await runtime.complete_with_text("Second answer", speech_text="Second answer", suppress_speech=False)
        await runtime.complete_with_error("Error answer")
        await asyncio.sleep(0)

        validated = [msg for msg in messages if msg.get("type") == "validated"]
        completed = [msg for msg in messages if msg.get("type") == "turn_complete"]

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["text"], "First answer")
        self.assertEqual(len(completed), 1)
        self.assertEqual(persisted, [("assistant", "First answer")])
        self.assertEqual(len(synced), 1)

    async def test_orchestrator_final_emits_once_without_persisting_again(self):
        runtime, messages, persisted, synced = await self._runtime()

        await runtime.emit_final_from_orchestrator(
            display_text="Ledger says awaiting confirmation.",
            speech_text="Ledger says awaiting confirmation.",
            result_label="turn_orchestrator",
        )
        await runtime.complete_with_text("Stale LLM answer", speech_text="Stale LLM answer", suppress_speech=False)

        validated = [msg for msg in messages if msg.get("type") == "validated"]
        completed = [msg for msg in messages if msg.get("type") == "turn_complete"]

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["text"], "Ledger says awaiting confirmation.")
        self.assertEqual(validated[0]["result"], "turn_orchestrator")
        self.assertEqual(len(completed), 1)
        self.assertEqual(persisted, [])
        self.assertEqual(synced, [])

    async def test_structured_final_uses_runtime_single_final_gate(self):
        runtime, messages, persisted, synced = await self._runtime()

        await runtime.complete_with_structured_response(
            "# Weather\nSunny",
            "Weather is sunny.",
            result="get_weather",
        )
        await runtime.emit_final_from_orchestrator(
            display_text="Duplicate orchestrator final",
            speech_text="Duplicate orchestrator final",
        )
        await asyncio.sleep(0)

        validated = [msg for msg in messages if msg.get("type") == "validated"]
        completed = [msg for msg in messages if msg.get("type") == "turn_complete"]

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["text"], "# Weather\nSunny")
        self.assertEqual(validated[0]["speechText"], "Weather is sunny.")
        self.assertEqual(validated[0]["result"], "get_weather")
        self.assertEqual(len(completed), 1)
        self.assertEqual(persisted, [("assistant", "# Weather\nSunny")])
        self.assertEqual(len(synced), 1)


if __name__ == "__main__":
    unittest.main()
