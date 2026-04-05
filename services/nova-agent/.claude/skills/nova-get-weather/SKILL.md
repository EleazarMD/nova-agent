---
name: nova-get-weather
description: Get current weather conditions and forecasts for any location using OpenWeather API through AI Gateway.
---

# Get Weather

Retrieves current weather conditions and forecasts for any location. Uses OpenWeather API through the AI Gateway.

## When to Invoke

- User asks about current weather
- Weather forecast requests
- Temperature inquiries
- Weather conditions for a specific city
- "What's the weather like?"
- Planning outdoor activities

## Actions

- **current**: Get current weather conditions
- **forecast**: Get weather forecast
- **location**: Get weather by coordinates

## Parameters

- `location`: City name or location
- `units`: Temperature units (metric/imperial)
- `days`: Forecast days (default: 3)

## Examples

User: "What's the weather in New York?"
Assistant: Invoking @nova-get-weather for current conditions...

User: "Will it rain tomorrow?"
Assistant: Invoking @nova-get-weather for forecast information...

## References

- Handler: `handle_get_weather()` in tools.py
- API: OpenWeather via AI Gateway
