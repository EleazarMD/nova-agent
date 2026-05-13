import json
from loguru import logger


def _as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _weather_desc(value) -> str:
    try:
        return str(value[0].get("value") or "")
    except Exception:
        return ""


def _build_weather_speech(display_text: str) -> str:
    """Build a concise, conversational version of the weather for TTS."""
    if not display_text:
        return ""
    if "I could not retrieve" in display_text:
        return display_text
    
    first_sentence = display_text.split(". ")[0].strip()
    speech = first_sentence.replace("Fahrenheit", "degrees")
    if "°F" in speech:
        speech = speech.replace("°F", " degrees")
    if not speech.endswith("."):
        speech += "."
    return speech

def _weather_condition_code(condition: str) -> str:
    normalized = str(condition or "").lower()
    if any(term in normalized for term in ("thunder", "storm", "showers", "rain")):
        return "rain"
    if any(term in normalized for term in ("cloud", "overcast")):
        return "clouds"
    if any(term in normalized for term in ("sun", "clear", "hot", "warm")):
        return "sun"
    return "clouds"

def _extract_weather_card(location: str, query: str, display: str, speech: str, data: dict = None) -> dict:
    """Extract a structured display card for the UI."""
    periods = []
    current_weather = {}
    chart_points = []
    metrics = []
    
    if data and "current_condition" in data:
        current = data["current_condition"][0]
        temp_f = _as_int(current.get("temp_F"))
        feels_like_f = _as_int(current.get("FeelsLikeF"))
        humidity = _as_int(current.get("humidity"))
        wind_mph = _as_int(current.get("windspeedMiles"))
        condition = _weather_desc(current.get("weatherDesc", []))
        condition_code = _weather_condition_code(condition)
        current_weather = {
            "temperatureF": temp_f,
            "feelsLikeF": feels_like_f,
            "condition": condition,
            "conditionCode": condition_code,
            "humidityPct": humidity,
            "windMph": wind_mph,
            "windDirection": current.get("winddir16Point", ""),
            "uvIndex": _as_int(current.get("uvIndex")),
            "visibilityMiles": _as_int(current.get("visibilityMiles")),
        }
        metrics = [
            {"label": "Feels Like", "value": f"{feels_like_f}°", "tone": "warm" if feels_like_f >= 80 else "cool"},
            {"label": "Humidity", "value": f"{humidity}%", "tone": "humid" if humidity >= 70 else "neutral"},
            {"label": "Wind", "value": f"{wind_mph} mph", "tone": "windy" if wind_mph >= 15 else "calm"},
            {"label": "UV Index", "value": str(current_weather["uvIndex"]), "tone": "alert" if current_weather["uvIndex"] >= 8 else "neutral"},
        ]
        periods.append({
            "name": "Current",
            "summary": f"{condition}, {humidity}% humidity",
            "highF": temp_f,
            "lowF": temp_f,
            "precipChancePct": _as_int(current.get("chanceofrain")),
            "wind": f"{current.get('winddir16Point', '')} {wind_mph} mph".strip(),
            "conditionCode": condition_code
        })
        
        if "weather" in data:
            for day in data["weather"]:
                date_str = day.get("date", "")
                hourly = day.get("hourly", [{}])[0]
                high_f = _as_int(day.get("maxtempF"))
                low_f = _as_int(day.get("mintempF"))
                precip = _as_int(hourly.get("chanceofrain") or hourly.get("chanceofsnow"))
                condition = _weather_desc(hourly.get("weatherDesc", []))
                chart_points.append({
                    "label": date_str,
                    "highF": high_f,
                    "lowF": low_f,
                    "precipChancePct": precip,
                })
                periods.append({
                    "name": date_str,
                    "summary": condition,
                    "highF": high_f,
                    "lowF": low_f,
                    "precipChancePct": precip,
                    "wind": f"{hourly.get('winddir16Point', '')} {hourly.get('WindGustMiles', 0)} mph",
                    "conditionCode": _weather_condition_code(condition)
                })

    return {
        "kind": "weather_forecast",
        "schemaVersion": 3,
        "location": location,
        "title": f"Weather for {location}",
        "subtitle": "Live updates",
        "summary": speech or "Current weather condition",
        "current": current_weather,
        "periods": periods[:6],
        "alerts": [],
        "metrics": metrics,
        "charts": [
            {
                "kind": "temperature_range",
                "title": "Temperature Trend",
                "points": chart_points[:5],
                "xKey": "label",
                "series": [
                    {"key": "highF", "label": "High", "color": "#fb923c"},
                    {"key": "lowF", "label": "Low", "color": "#38bdf8"},
                ],
            },
            {
                "kind": "precipitation_bar",
                "title": "Precipitation Chance",
                "points": chart_points[:5],
                "xKey": "label",
                "series": [
                    {"key": "precipChancePct", "label": "Rain", "color": "#60a5fa"},
                ],
            },
        ],
        "layout": {
            "template": "weather_hero_forecast",
            "accent": current_weather.get("conditionCode", "clouds"),
            "density": "rich",
            "supports": ["hero", "metricGrid", "forecastStrip", "lineChart", "barChart", "alertBanner"],
        },
        "source": "wttr.in",
        "query": query,
    }

async def handle_get_weather(location: str = "", query: str = "", **kwargs):
    if not location or location.strip().lower() in {"current location", "my location", "here", "near me"}:
        live_location = ""
        from nova.tools import _current_user_location
        if isinstance(_current_user_location, dict):
            live_location = str(_current_user_location.get("location") or _current_user_location.get("address") or "")
        if live_location:
            location = live_location
    location = (location or "Humble, TX").strip()
    weather_query = (query or "").strip()
    import aiohttp
    
    # URL encode the location
    import urllib.parse
    loc_query = urllib.parse.quote(location)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://wttr.in/{loc_query}?format=j1", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    current = data["current_condition"][0]
                    temp_f = current["temp_F"]
                    feels_like = current["FeelsLikeF"]
                    condition = current["weatherDesc"][0]["value"]
                    humidity = current["humidity"]
                    wind_mph = current["windspeedMiles"]
                    wind_dir = current["winddir16Point"]
                    
                    display = (
                        f"Current weather for {location}.\n"
                        f"Temperature: {temp_f}°F, feels like {feels_like}°F.\n"
                        f"Condition: {condition}.\n"
                        f"Humidity: {humidity}%.\n"
                        f"Wind: {wind_dir} {wind_mph} mph.\n"
                        f"Source: wttr.in live data."
                    )
                else:
                    display = f"I could not retrieve current weather for {location}. The weather service returned status {resp.status}."
    except Exception as e:
        logger.error(f"Weather API error: {e}")
        display = f"I could not retrieve current weather for {location} right now due to a network error."

    speech = _build_weather_speech(display) or display
    card = _extract_weather_card(location, query, display, speech, data if 'data' in locals() else None)
    return {
        "display": display,
        "speech": speech,
        "speakable": speech,
        "card": card,
    }
