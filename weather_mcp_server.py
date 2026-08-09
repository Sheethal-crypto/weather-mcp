"""
Weather MCP server.

Exposes three tools over the Open-Meteo adapter in weather_client.py. Every tool
function here is thin: resolve arguments, call the adapter, catch WeatherAPIError,
return a dict. No requests calls and no response parsing live in this file.

Every tool returns a dict carrying a "status" key of "ok" or "error". The agent is
instructed to treat "error" as a failed lookup and say so, rather than filling the
gap with its own guess.

Run locally:
    python weather_mcp_server.py
Then point an MCP client at http://localhost:8000/mcp
"""

import os
from datetime import date as date_type
from datetime import timedelta
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from weather_client import WeatherAPIError, fetch_current, fetch_forecast

mcp = FastMCP("weather")

# Thresholds for the recommendation tool. Named here so the docstrings can cite
# them and so a reviewer can see the judgment calls in one place.
UMBRELLA_CHANCE_HIGH = 50      # percent chance that triggers on its own
UMBRELLA_CHANCE_MODERATE = 30  # percent chance that triggers only with volume
UMBRELLA_AMOUNT_MEANINGFUL = 0.10  # inches, the volume that makes moderate chance matter
UMBRELLA_WIND_USELESS = 25     # mph above which an umbrella inverts, advise a jacket
JACKET_HIGH_BELOW = 62         # daytime high in F below which a jacket is worth carrying
JACKET_LOW_BELOW = 50          # overnight low in F that matters for evening plans
WIND_ADVISORY = 30             # mph max sustained wind worth flagging
HEAT_ADVISORY = 95             # daytime high in F worth flagging


def _error(message: str) -> Dict[str, Any]:
    return {"status": "error", "error": message}


def _resolve_target_date(value: Optional[str]) -> Optional[str]:
    """Turn 'today', 'tomorrow', None or an ISO date into an ISO date string."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    if cleaned in ("", "today"):
        return None
    if cleaned == "tomorrow":
        return (date_type.today() + timedelta(days=1)).isoformat()
    return value.strip()


@mcp.tool()
def get_current_weather(location: str, units: str = "imperial") -> Dict[str, Any]:
    """Get current observed weather conditions for a location.

    Use this for questions about right now ("what is it like outside in Denver").
    For anything about later today or a future day, use get_forecast instead.

    Args:
        location: City name, "city, state", or "lat,lon". Example: "Austin, Texas".
        units: "imperial" for F, mph and inches, or "metric" for C, km/h and mm.
            Defaults to "imperial".

    Returns:
        On success, a dict with status "ok" plus the resolved location, observed_at
        timestamp, temperature, feels_like, humidity_pct, wind_speed, precipitation,
        a plain-English conditions string, and the units used.
        On failure, a dict with status "error" and a human-readable error message.
    """
    try:
        payload = fetch_current(location, units=units)
    except WeatherAPIError as exc:
        return _error(str(exc))

    payload["status"] = "ok"
    return payload


@mcp.tool()
def get_forecast(location: str, days: int = 3, units: str = "imperial") -> Dict[str, Any]:
    """Get a daily forecast for the next N days at a location.

    Day one of the returned list is today in the location's own timezone, not the
    caller's. Use this for planning questions spanning more than one day.

    Args:
        location: City name, "city, state", or "lat,lon". Example: "Chicago".
        days: How many days to return, starting today. Clamped to 1 through 16.
            Defaults to 3.
        units: "imperial" or "metric". Defaults to "imperial".

    Returns:
        On success, a dict with status "ok", the resolved location, the units used,
        and a "days" list. Each day has date, high, low, precip_chance_pct,
        precip_amount, wind_max and a plain-English conditions string.
        On failure, a dict with status "error" and a human-readable error message.
    """
    try:
        payload = fetch_forecast(location, days=days, units=units)
    except WeatherAPIError as exc:
        return _error(str(exc))

    payload["status"] = "ok"
    return payload


@mcp.tool()
def get_day_recommendation(
    location: str, date: Optional[str] = None, units: str = "imperial"
) -> Dict[str, Any]:
    """Decide what to carry and what to watch out for on a given day.

    This applies threshold logic to the forecast rather than returning it raw.

    Umbrella rule, two factors because probability alone is misleading. A 45 percent
    chance of 0.01 inches is a sprinkle; a 35 percent chance of half an inch soaks
    you. So the answer is "yes" when chance is at or above 50 percent, or when chance
    is at or above 30 percent AND expected accumulation is at least 0.10 inches.
    It is "maybe" when chance is at or above 30 percent with less accumulation than
    that, and "no" otherwise. If max wind is above 25 mph the recommendation switches
    to a rain jacket instead, because an umbrella inverts at that speed.

    Jacket rule: yes when the daytime high is below 62F, or when the overnight low is
    below 50F, which is the case that catches people out on evening plans.

    Advisories are raised for max wind above 30 mph, a daytime high above 95F, and any
    thunderstorm in the day's conditions.

    Args:
        location: City name, "city, state", or "lat,lon".
        date: Target day as "YYYY-MM-DD", or "today" or "tomorrow". Defaults to today.
            Must fall inside the next 16 days.
        units: "imperial" or "metric". Thresholds above are calibrated in Fahrenheit
            and mph, so "imperial" is recommended. Defaults to "imperial".

    Returns:
        On success, a dict with status "ok", the resolved location and date, the
        forecast row the decision was based on, an "umbrella" and "jacket" block each
        carrying the recommendation and the rule that fired, an "advisories" list, and
        a one-sentence summary.
        On failure, a dict with status "error" and a human-readable error message,
        including when the requested date is outside the forecast window.
    """
    target = _resolve_target_date(date)

    try:
        forecast = fetch_forecast(location, days=16, units=units)
    except WeatherAPIError as exc:
        return _error(str(exc))

    entries = forecast["days"]
    if target is None:
        day = entries[0]
    else:
        matches = [d for d in entries if d["date"] == target]
        if not matches:
            return _error(
                f"{target} is outside the forecast window. "
                f"Available dates are {entries[0]['date']} through {entries[-1]['date']}."
            )
        day = matches[0]

    chance = day.get("precip_chance_pct") or 0
    amount = day.get("precip_amount") or 0.0
    wind = day.get("wind_max") or 0.0
    high = day.get("high")
    low = day.get("low")
    conditions = day.get("conditions") or ""

    # Umbrella
    if chance >= UMBRELLA_CHANCE_HIGH:
        umbrella_call = "yes"
        umbrella_rule = f"precipitation chance {chance}% is at or above {UMBRELLA_CHANCE_HIGH}%"
    elif chance >= UMBRELLA_CHANCE_MODERATE and amount >= UMBRELLA_AMOUNT_MEANINGFUL:
        umbrella_call = "yes"
        umbrella_rule = (
            f"precipitation chance {chance}% is at or above {UMBRELLA_CHANCE_MODERATE}% "
            f"and expected accumulation {amount} is at or above {UMBRELLA_AMOUNT_MEANINGFUL}"
        )
    elif chance >= UMBRELLA_CHANCE_MODERATE:
        umbrella_call = "maybe"
        umbrella_rule = (
            f"precipitation chance {chance}% is at or above {UMBRELLA_CHANCE_MODERATE}% "
            f"but expected accumulation {amount} is light"
        )
    else:
        umbrella_call = "no"
        umbrella_rule = f"precipitation chance {chance}% is below {UMBRELLA_CHANCE_MODERATE}%"

    if umbrella_call in ("yes", "maybe") and wind > UMBRELLA_WIND_USELESS:
        umbrella_call = "rain jacket instead"
        umbrella_rule += (
            f"; max wind {wind} is above {UMBRELLA_WIND_USELESS}, so an umbrella will invert"
        )

    # Jacket
    if high is not None and high < JACKET_HIGH_BELOW:
        jacket_call = "yes"
        jacket_rule = f"daytime high {high} is below {JACKET_HIGH_BELOW}"
    elif low is not None and low < JACKET_LOW_BELOW:
        jacket_call = "yes"
        jacket_rule = (
            f"overnight low {low} is below {JACKET_LOW_BELOW}, which matters for evening plans"
        )
    else:
        jacket_call = "no"
        jacket_rule = f"high {high} and low {low} both stay above the jacket thresholds"

    # Advisories
    advisories = []
    if wind > WIND_ADVISORY:
        advisories.append(f"High wind: max {wind} expected.")
    if high is not None and high > HEAT_ADVISORY:
        advisories.append(f"Heat: high of {high} expected.")
    if "thunderstorm" in conditions:
        advisories.append(f"Thunderstorms in the forecast ({conditions}).")

    summary_bits = [f"{day['date']} in {forecast['location']}: {conditions}, high {high}, low {low}."]
    if umbrella_call == "yes":
        summary_bits.append("Bring an umbrella.")
    elif umbrella_call == "maybe":
        summary_bits.append("An umbrella is optional.")
    elif umbrella_call == "rain jacket instead":
        summary_bits.append("Wear a rain jacket rather than carrying an umbrella.")
    if jacket_call == "yes":
        summary_bits.append("Bring a jacket.")
    if advisories:
        summary_bits.append(" ".join(advisories))

    return {
        "status": "ok",
        "location": forecast["location"],
        "date": day["date"],
        "units": forecast["units"],
        "forecast": day,
        "umbrella": {"recommendation": umbrella_call, "rule": umbrella_rule},
        "jacket": {"recommendation": jacket_call, "rule": jacket_rule},
        "advisories": advisories,
        "summary": " ".join(summary_bits),
    }


if __name__ == "__main__":
    port = int(os.environ.get("DATABRICKS_APP_PORT", os.environ.get("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
