import unittest
from unittest.mock import patch

from nova.learned_router import LearnedRouteCandidate, arbitrate_learned_route, evaluate_learned_router, learned_router_example_from_observation, propose_learned_route
from nova.turn_orchestrator import TurnIntent, TurnState, decide_turn


class LearnedRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_read_only_candidate_promotes_pass_through(self):
        candidate = LearnedRouteCandidate(
            intent="current_events_lookup",
            confidence=0.93,
            suggested_tools=["web_search"],
            safety_level="safe_read_only",
        )

        result = arbitrate_learned_route(candidate, "pass_through")

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "promote")
        self.assertIn("pass_through_safe_read_only_high_confidence", result.positive_evidence)

    def test_side_effect_candidate_is_blocked(self):
        candidate = LearnedRouteCandidate(
            intent="workspace_creation",
            confidence=0.99,
            suggested_tools=["hub_delegate"],
            safety_level="side_effect",
        )

        result = arbitrate_learned_route(candidate, "pass_through")

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "block")
        self.assertIn("candidate_not_safe_read_only", result.negative_evidence)

    async def test_decide_turn_can_promote_safe_learned_pass_through(self):
        candidate = LearnedRouteCandidate(
            intent="current_events_lookup",
            confidence=0.94,
            suggested_tools=["web_search"],
            safety_level="safe_read_only",
            action="promote",
        )

        with patch("nova.turn_orchestrator.get_shadow_plan_candidates", return_value=[]), patch(
            "nova.turn_orchestrator.propose_learned_route",
            return_value=candidate,
        ):
            plan = await decide_turn("Tell me about the new model release from that company", TurnState())

        self.assertEqual(plan.intent, TurnIntent.CURRENT_EVENTS_LOOKUP)
        self.assertEqual(plan.allowed_tools, ["web_search"])
        self.assertEqual(plan.context["learned_route_candidate"]["action"], "promote")

    async def test_decide_turn_keeps_side_effect_learned_candidate_in_shadow(self):
        candidate = LearnedRouteCandidate(
            intent="workspace_creation",
            confidence=0.99,
            suggested_tools=["hub_delegate"],
            safety_level="side_effect",
            action="block",
        )

        with patch("nova.turn_orchestrator.get_shadow_plan_candidates", return_value=[]), patch(
            "nova.turn_orchestrator.propose_learned_route",
            return_value=candidate,
        ):
            plan = await decide_turn("Can you think through this idea with me", TurnState())

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)
        self.assertEqual(plan.learned_candidate["action"], "block")

    async def test_legacy_shadow_candidate_never_promotes_auto_action(self):
        raw_candidate = {
            "id": 300,
            "intent": "web_research_request",
            "confidence": 0.99,
            "tools_used": ["web_search"],
            "trigger_text": "latest company news",
            "match_type": "semantic",
            "similarity": 0.99,
        }

        with patch("nova.turn_orchestrator.get_shadow_plan_candidates", return_value=[raw_candidate]), patch(
            "nova.turn_orchestrator.propose_learned_route",
            return_value=None,
        ):
            plan = await decide_turn("Can you think through this idea with me", TurnState())

        self.assertEqual(plan.intent, TurnIntent.PASS_THROUGH)
        self.assertNotEqual(plan.intent, TurnIntent.AUTO_ACTION)
        self.assertEqual(plan.learned_candidate["id"], 300)

    async def test_safe_learned_candidate_cannot_override_active_action_followup(self):
        candidate = LearnedRouteCandidate(
            intent="current_events_lookup",
            confidence=0.99,
            suggested_tools=["web_search"],
            safety_level="safe_read_only",
            action="promote",
        )

        with patch("nova.turn_orchestrator.get_shadow_plan_candidates", return_value=[]), patch(
            "nova.turn_orchestrator.propose_learned_route",
            return_value=candidate,
        ):
            plan = await decide_turn("yes please", TurnState(active_action_id="action-1", active_goal="Send Tesla navigation"))

        self.assertEqual(plan.intent, TurnIntent.ACTIVE_ACTION_CONFIRMATION)
        self.assertEqual(plan.allowed_tools, [])

    async def test_safe_learned_candidate_cannot_override_tesla_navigation_plan(self):
        candidate = LearnedRouteCandidate(
            intent="current_events_lookup",
            confidence=0.99,
            suggested_tools=["web_search"],
            safety_level="safe_read_only",
            action="promote",
        )

        with patch("nova.turn_orchestrator.get_shadow_plan_candidates", return_value=[]), patch(
            "nova.turn_orchestrator.propose_learned_route",
            return_value=candidate,
        ):
            plan = await decide_turn("Send directions to Starbucks on I-10 to my Tesla", TurnState())

        self.assertEqual(plan.intent, TurnIntent.TESLA_NAVIGATION_PLAN)
        self.assertEqual(plan.allowed_tools, [])

    def test_example_from_observation_parses_tools_and_positive_label(self):
        example = learned_router_example_from_observation({
            "id": 42,
            "normalized_text": "latest model news",
            "deterministic_intent": "current_events_lookup",
            "tools_used": "[\"web_search\"]",
            "handled": 1,
            "outcome": "handled",
            "stop_reason": "current_events_grounded",
            "latency_ms": 123,
        })

        self.assertEqual(example["text"], "latest model news")
        self.assertEqual(example["label_tools"], ["web_search"])
        self.assertTrue(example["positive"])

    async def test_evaluate_learned_router_reports_coverage_and_matches(self):
        candidate = LearnedRouteCandidate(
            intent="current_events_lookup",
            confidence=0.94,
            suggested_tools=["web_search"],
            safety_level="safe_read_only",
            action="promote",
        )

        with patch(
            "nova.learned_router.build_learned_router_eval_dataset",
            return_value=[{
                "id": 1,
                "text": "latest model news",
                "label_intent": "current_events_lookup",
                "label_tools": ["web_search"],
                "handled": 1,
                "outcome": "handled",
                "positive": True,
                "stop_reason": "current_events_grounded",
                "latency_ms": 1,
            }],
        ), patch("nova.learned_router.propose_learned_route", return_value=candidate):
            report = await evaluate_learned_router(limit=1)

        self.assertEqual(report["examples"], 1)
        self.assertEqual(report["with_candidate"], 1)
        self.assertEqual(report["intent_matches"], 1)
        self.assertEqual(report["promotions"], 1)

    async def test_noisy_status_trigger_candidate_is_suppressed(self):
        raw_candidate = {
            "id": 176,
            "intent": "web_research_request",
            "confidence": 0.95,
            "tools_used": ["web_search"],
            "trigger_text": "I don't see anything in my black panther yet",
            "match_type": "semantic",
            "similarity": 0.98,
        }

        with patch("nova.learned_router.get_shadow_plan_candidates", return_value=[raw_candidate]):
            candidate = await propose_learned_route("I don't see anything in my black panther yet")

        self.assertIsNone(candidate)

    async def test_high_confidence_candidate_without_text_support_stays_shadow(self):
        raw_candidate = {
            "id": 200,
            "intent": "web_research_request",
            "confidence": 0.95,
            "tools_used": ["web_search"],
            "trigger_text": "latest company news",
            "match_type": "semantic",
            "similarity": 0.98,
        }

        with patch("nova.learned_router.get_shadow_plan_candidates", return_value=[raw_candidate]):
            candidate = await propose_learned_route("I am driving to work")

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.action, "shadow")
        self.assertIn("current_text_lacks_intent_support", candidate.negative_evidence)

    async def test_all_blocked_candidates_do_not_count_as_coverage(self):
        raw_candidate = {
            "id": 113,
            "intent": "tesla_control",
            "confidence": 0.95,
            "tools_used": ["tesla_control"],
            "trigger_text": "Try again",
            "match_type": "semantic",
            "similarity": 0.9,
        }

        with patch(
            "nova.learned_router.build_learned_router_eval_dataset",
            return_value=[{
                "id": 1,
                "text": "try again",
                "label_intent": "pass_through",
                "label_tools": [],
                "handled": 0,
                "outcome": "pass_through",
                "positive": False,
                "stop_reason": "",
                "latency_ms": 0,
            }],
        ), patch("nova.learned_router.get_shadow_plan_candidates", return_value=[raw_candidate]):
            report = await evaluate_learned_router(limit=1)

        self.assertEqual(report["with_candidate"], 0)
        self.assertEqual(report["coverage_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
