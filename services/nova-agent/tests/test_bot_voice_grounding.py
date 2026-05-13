import unittest

import bot


class BotVoiceGroundingTests(unittest.TestCase):
    def test_web_search_result_is_wrapped_with_grounding_requirements(self):
        wrapped = bot._trim_tool_result_for_llm(
            "web_search",
            "### Result\n- Anthropic funding reported by Example Source",
        )

        self.assertIn("WEB SEARCH EVIDENCE:", wrapped)
        self.assertIn("Include source names or citations", wrapped)
        self.assertIn("reported", wrapped)
        self.assertIn("confidence is limited", wrapped)

    def test_non_web_search_result_is_not_wrapped(self):
        wrapped = bot._trim_tool_result_for_llm("get_weather", "sunny")

        self.assertEqual(wrapped, "sunny")


if __name__ == "__main__":
    unittest.main()
