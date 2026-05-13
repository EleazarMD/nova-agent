import unittest
from types import SimpleNamespace

from nova.turn_ownership import (
    should_consume_llm_frame_after_orchestrator,
    should_fail_closed_after_turn_ingress_error,
)


class LLMRunFrame:
    pass


class LLMMessagesAppendFrame:
    pass


class NonRunFrame:
    pass


class TurnOwnershipTests(unittest.TestCase):
    def test_consumes_llm_run_for_completed_orchestrator_owned_turn(self):
        active_turn = SimpleNamespace(
            snapshot=SimpleNamespace(turn_id="turn-1", turn_complete_sent=True)
        )

        self.assertTrue(
            should_consume_llm_frame_after_orchestrator(
                LLMRunFrame(),
                active_turn,
                "turn-1",
            )
        )
        self.assertTrue(
            should_consume_llm_frame_after_orchestrator(
                LLMMessagesAppendFrame(),
                active_turn,
                "turn-1",
            )
        )

    def test_does_not_consume_when_turn_is_not_orchestrator_owned(self):
        active_turn = SimpleNamespace(
            snapshot=SimpleNamespace(turn_id="turn-1", turn_complete_sent=True)
        )

        self.assertFalse(
            should_consume_llm_frame_after_orchestrator(
                LLMRunFrame(),
                active_turn,
                "",
            )
        )
        self.assertFalse(
            should_consume_llm_frame_after_orchestrator(
                LLMRunFrame(),
                active_turn,
                "turn-2",
            )
        )

    def test_does_not_consume_non_run_frames_or_incomplete_turns(self):
        active_turn = SimpleNamespace(
            snapshot=SimpleNamespace(turn_id="turn-1", turn_complete_sent=True)
        )
        incomplete_turn = SimpleNamespace(
            snapshot=SimpleNamespace(turn_id="turn-1", turn_complete_sent=False)
        )

        self.assertFalse(
            should_consume_llm_frame_after_orchestrator(
                NonRunFrame(),
                active_turn,
                "turn-1",
            )
        )
        self.assertFalse(
            should_consume_llm_frame_after_orchestrator(
                LLMRunFrame(),
                incomplete_turn,
                "turn-1",
            )
        )
        self.assertFalse(
            should_consume_llm_frame_after_orchestrator(
                LLMRunFrame(),
                None,
                "turn-1",
            )
        )

    def test_turn_ingress_errors_fail_closed_once_runtime_exists(self):
        active_turn = SimpleNamespace(
            snapshot=SimpleNamespace(turn_id="turn-1", turn_complete_sent=False)
        )

        self.assertTrue(
            should_fail_closed_after_turn_ingress_error(
                "Did you get the analysis",
                active_turn,
            )
        )
        self.assertFalse(
            should_fail_closed_after_turn_ingress_error(
                "",
                active_turn,
            )
        )
        self.assertFalse(
            should_fail_closed_after_turn_ingress_error(
                "Did you get the analysis",
                None,
            )
        )


if __name__ == "__main__":
    unittest.main()
