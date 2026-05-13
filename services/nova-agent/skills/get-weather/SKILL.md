---
name: get-weather
tool_name: get_weather
description: >
  Fetch current weather conditions, forecasts, and active alerts using the wttr.in live weather API.
parameters:
  type: object
  properties:
    location:
      type: string
      description: The city, state, or zip code to get weather for. Use an empty string to default to the user's current location.
    query:
      type: string
      description: Optional specific question (e.g., 'Will it rain tomorrow?', 'Is there a tornado warning?').
---

# Get Weather

This skill retrieves highly accurate, real-time weather information by querying the live wttr.in API.

## When to Invoke
- The user asks for the current temperature.
- The user asks about rain, snow, or wind.
- The user asks for a weather forecast.
- The user asks about active weather alerts.

## Instructions
This skill automatically queries the `wttr.in` API to fetch up-to-date data. It will return the current temperature in Fahrenheit, conditions, humidity, and wind.

Do not cache this tool's response, as weather data is highly temporal.
