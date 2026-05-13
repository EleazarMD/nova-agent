import unittest
from unittest.mock import patch

from nova import tools


class WeatherPerplexityTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_weather_uses_perplexity_web_search_only(self):
        calls = []

        async def fake_web_search(query: str, deep_mode: bool = False) -> str:
            calls.append((query, deep_mode))
            return "Humble, TX weather: 80°F, clear, 60% humidity, 5 mph wind.\n\n(2 sources available.)"

        with patch.object(tools, "handle_web_search", fake_web_search):
            result = await tools.handle_get_weather("Humble, TX")

        self.assertEqual(len(calls), 1)
        self.assertIn("Current weather right now for Humble, TX", calls[0][0])
        self.assertFalse(calls[0][1])
        self.assertIn(result["card"]["source"], {"perplexity", "wttr.in"})
        self.assertEqual(result["card"]["kind"], "weather_forecast")
        self.assertGreaterEqual(result["card"]["schemaVersion"], 2)
        self.assertEqual(result["card"]["location"], "Humble, TX")
        self.assertIn("periods", result["card"])
        self.assertIn("layout", result["card"])
        self.assertIn("charts", result["card"])
        self.assertIn("Humble, TX weather", result["display"])
        self.assertIn("Humble, TX weather", result["speech"])

    async def test_get_weather_uses_live_location_for_here(self):
        calls = []

        async def fake_web_search(query: str, deep_mode: bool = False) -> str:
            calls.append((query, deep_mode))
            return "Live location weather."

        with patch.object(tools, "_current_user_location", {"location": "Spring, TX"}), patch.object(tools, "handle_web_search", fake_web_search):
            result = await tools.handle_get_weather("here")

        self.assertEqual(len(calls), 1)
        self.assertIn("Spring, TX", calls[0][0])
        self.assertEqual(result["card"]["location"], "Spring, TX")
        self.assertEqual(result["card"]["source"], "perplexity")

    async def test_get_weather_extracts_structured_forecast_periods(self):
        async def fake_web_search(query: str, deep_mode: bool = False) -> str:
            return (
                "### Humble, TX Weekend Forecast\n\n"
                "**Saturday:** High 85°F, low 70°F, partly sunny. 30-40% chance of thunderstorms. Winds ESE 5-15 mph.\n\n"
                "**Sunday:** High 86°F, low 71°F, mostly sunny. 20% chance of rain. Winds light.\n\n"
                "**Alerts:** None active."
            )

        with patch.object(tools, "handle_web_search", fake_web_search):
            result = await tools.handle_get_weather("Humble, TX", query="What's the forecast for this weekend?")

        card = result["card"]
        self.assertEqual(card["kind"], "weather_forecast")
        self.assertGreaterEqual(card["schemaVersion"], 2)
        self.assertIn("Weekend Forecast", card["title"])
        self.assertGreaterEqual(len(card["periods"]), 2)
        self.assertEqual(card["periods"][0]["name"], "Saturday")
        self.assertEqual(card["periods"][0]["highF"], 85)
        self.assertEqual(card["periods"][0]["lowF"], 70)
        self.assertEqual(card["periods"][0]["precipChancePct"], 40)
        self.assertEqual(card["periods"][0]["conditionCode"], "rain")
        self.assertIn("wind", card["periods"][0])
        self.assertIn("Saturday", result["speech"])
        self.assertIn("Sunday", result["speech"])
        self.assertNotEqual(result["speech"], card["title"])


if __name__ == "__main__":
    unittest.main()
