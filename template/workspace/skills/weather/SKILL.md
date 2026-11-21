---
name: weather
description: Current weather and forecasts for any city, region, airport code or coordinates (rain, temperature, travel planning).
---

<!-- Adapted from OpenClaw's bundled "weather" skill (MIT, © OpenClaw Foundation). -->

# Weather

Use for current weather, rain/temperature checks, forecasts and travel planning. You need a place: a city, region,
airport code or coordinates. If the user gave none, use the server's usual city if you know it (e.g. Dhaka for a
Bangladeshi server) and say which place you used.

1. Call the `weather` tool with the place (and `days` 1–3 if they asked about later).
2. Answer short: place · condition · temperature (feels like) · rain chance/precipitation · wind · humidity.
3. For "will it rain / should I take an umbrella": answer yes/no first, then the numbers.
4. For forecasts: one line per day — date, condition, min–max °C, rain.
5. Never invent weather. If the tool fails, say so and suggest trying a nearby bigger city.
