import unittest

from nova.turn_tool_policy import select_tool_budget


ALL_TOOLS = [
    "get_time",
    "recall_memory",
    "save_memory",
    "search_past_conversations",
    "web_search",
    "get_weather",
    "query_cig",
    "hub_delegate",
    "tesla_control",
    "tesla_navigation",
]


class TurnToolPolicyTests(unittest.TestCase):
    def test_medium_confidence_learned_candidate_bounds_tools(self):
        budget = select_tool_budget(
            "Did you get the case study analysis?",
            ALL_TOOLS,
            "pass_through",
            learned_candidate={
                "id": 42,
                "intent": "conversation_recall",
                "confidence": 0.725,
                "suggested_tools": ["search_past_conversations"],
            },
        )

        self.assertEqual(budget.reason, "learned_nudge:bounded_assist:conversation_recall")
        self.assertEqual(budget.nudge_level, 2)
        self.assertAlmostEqual(budget.activation, 0.125)
        self.assertEqual(budget.learning_rate, 0.05)
        self.assertEqual(budget.candidate_id, 42)
        self.assertEqual(budget.names, ["get_time", "search_past_conversations"])
        self.assertNotIn("web_search", budget.names)
        self.assertNotIn("query_cig", budget.names)
        self.assertNotIn("tesla_control", budget.names)

    def test_low_activation_quarantines_pass_through_tools(self):
        budget = select_tool_budget(
            "Maybe use something from before",
            ALL_TOOLS,
            "pass_through",
            learned_candidate={
                "id": 43,
                "intent": "conversation_recall",
                "confidence": 0.65,
                "suggested_tools": ["search_past_conversations"],
            },
        )

        self.assertEqual(budget.reason, "learned_nudge:quarantine:conversation_recall")
        self.assertEqual(budget.nudge_level, 1)
        self.assertEqual(budget.names, ["get_time", "recall_memory", "save_memory"])
        self.assertNotIn("search_past_conversations", budget.names)
        self.assertNotIn("web_search", budget.names)

    def test_no_activation_uses_existing_keyword_fallback(self):
        budget = select_tool_budget(
            "check my email thread",
            ALL_TOOLS,
            "pass_through",
            learned_candidate={
                "id": 44,
                "intent": "email_lookup",
                "confidence": 0.55,
                "suggested_tools": ["query_cig"],
            },
        )

        self.assertEqual(budget.reason, "fallback:keyword_groups")
        self.assertEqual(budget.nudge_level, 0)
        self.assertIn("query_cig", budget.names)

    def test_high_confidence_candidate_requires_current_text_support(self):
        budget = select_tool_budget(
            "So what did you find out",
            ALL_TOOLS,
            "pass_through",
            learned_candidate={
                "id": 84,
                "intent": "email_lookup",
                "confidence": 0.838,
                "suggested_tools": ["query_cig"],
            },
        )

        self.assertEqual(budget.reason, "learned_nudge:quarantine:email_lookup")
        self.assertEqual(budget.nudge_level, 2)
        self.assertEqual(budget.gradient_hint, "require_current_text_support_before_tool_activation")
        self.assertNotIn("query_cig", budget.names)
        self.assertNotIn("web_search", budget.names)

    def test_active_action_binding_context_restricts_pass_through_to_tesla_tools(self):
        budget = select_tool_budget(
            "Go ahead\n\n[ACTIVE ACTION BINDING CONTEXT]\nintent: tesla_navigation_plan\n[/ACTIVE ACTION BINDING CONTEXT]",
            ALL_TOOLS,
            "pass_through",
        )

        self.assertEqual(budget.reason, "active_action_binding_context:tesla_only")
        self.assertEqual(budget.names, ["tesla_control", "tesla_navigation"])
        self.assertNotIn("query_cig", budget.names)
        self.assertNotIn("search_past_conversations", budget.names)
        self.assertNotIn("web_search", budget.names)


if __name__ == "__main__":
    unittest.main()
