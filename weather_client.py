"""
Open-Meteo adapter.

All HTTP calls and response parsing live here. The MCP tool functions import
from this module and stay thin, mirroring the alpaca_broker.py / alpaca_mcp_server.py
split in the Day 3 reference repo.

Open-Meteo needs no API key and no signup, so there is no secret handling in
this module. If a keyed provider is added later, follow the _secret() pattern
from alpaca_broker.py rather than reading os.environ directly.
"""

import time
from typing import Any, Dict, List, Optional

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = [0.5, 1.0]

# WMO weather interpretation codes used by Open-Meteo.
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


class WeatherAPIError(Exception):
    """Raised when the upstream weather API fails or a location cannot be resolved.

    Tool functions catch this and return a structured error dict so the agent
    sees a clean message instead of a stack trace.
    """


def _describe(code: Optional[int]) -> str:
    if code is None:
        return "unknown"
    return WMO_CODES.get(int(code), f"unknown code {code}")


def _get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """GET with bounded retries.

    Retries connection errors, timeouts, 429 and 5xx. Does not retry other 4xx,
    because a 400 from Open-Meteo means the request itself is wrong and a second
    identical attempt will fail the same way.
    """
    last_error = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code} from {url}"
            elif response.status_code >= 400:
                raise WeatherAPIError(
                    f"Weather API rejected the request (HTTP {response.status_code}). "
                    f"Check the location and parameters."
                )
            else:
                return response.json()

        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = f"{type(exc).__name__} calling {url}"

        if attempt < len(BACKOFF_SECONDS):
            time.sleep(BACKOFF_SECONDS[attempt])

    raise WeatherAPIError(f"Weather API unavailable after {MAX_ATTEMPTS} attempts: {last_error}")


def _parse_latlon(location: str) -> Optional[Dict[str, Any]]:
    """Return a resolved-location dict if the string is already 'lat,lon', else None."""
    if "," not in location:
        return None
    left, _, right = location.partition(",")
    try:
        lat = float(left.strip())
        lon = float(right.strip())
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {
        "name": f"{lat:.4f},{lon:.4f}",
        "latitude": lat,
        "longitude": lon,
        "country": None,
        "admin1": None,
    }


def resolve_location(location: str) -> Dict[str, Any]:
    """Turn a free-text location into coordinates.

    Accepts a place name ("Chicago", "Austin, Texas", "Pullman WA") or a raw
    "lat,lon" pair. Place names go through the Open-Meteo geocoding API and the
    first match wins.

    Args:
        location: City name, "city, region", or "lat,lon".

    Returns:
        dict with keys: name, latitude, longitude, country, admin1.

    Raises:
        WeatherAPIError: if the location is blank or cannot be resolved.
    """
    if not location or not location.strip():
        raise WeatherAPIError("No location given.")

    location = location.strip()

    direct = _parse_latlon(location)
    if direct:
        return direct

    payload = _get(GEOCODE_URL, {"name": location, "count": 1, "format": "json"})
    results = payload.get("results") or []

    if not results:
        raise WeatherAPIError(
            f"Could not resolve the location '{location}'. "
            f"Try a city name, 'city, state', or 'lat,lon'."
        )

    hit = results[0]
    return {
        "name": hit.get("name"),
        "latitude": hit.get("latitude"),
        "longitude": hit.get("longitude"),
        "country": hit.get("country"),
        "admin1": hit.get("admin1"),
    }


def _label(place: Dict[str, Any]) -> str:
    parts = [place.get("name"), place.get("admin1"), place.get("country")]
    return ", ".join(p for p in parts if p)


def fetch_current(location: str, units: str = "imperial") -> Dict[str, Any]:
    """Fetch current observed conditions for a location.

    Args:
        location: City name, "city, state", or "lat,lon".
        units: "imperial" for F / mph / inch, "metric" for C / km/h / mm.

    Returns:
        dict with location, coordinates, observed_at, temperature_f_or_c,
        feels_like, humidity_pct, wind_speed, precipitation, conditions, units.
    """
    place = resolve_location(location)
    imperial = units == "imperial"

    payload = _get(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
            "timezone": "auto",
        },
    )

    current = payload.get("current") or {}
    if not current:
        raise WeatherAPIError(f"No current conditions returned for '{location}'.")

    return {
        "location": _label(place),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "observed_at": current.get("time"),
        "timezone": payload.get("timezone"),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "precipitation": current.get("precipitation"),
        "conditions": _describe(current.get("weather_code")),
        "units": {
            "temperature": "F" if imperial else "C",
            "wind_speed": "mph" if imperial else "km/h",
            "precipitation": "inch" if imperial else "mm",
        },
    }


def fetch_forecast(location: str, days: int = 3, units: str = "imperial") -> Dict[str, Any]:
    """Fetch a multi-day daily forecast for a location.

    Args:
        location: City name, "city, state", or "lat,lon".
        days: Number of days starting today, clamped to 1 through 16.
        units: "imperial" or "metric".

    Returns:
        dict with location, coordinates, units, and a "days" list. Each entry has
        date, high, low, precip_chance_pct, precip_amount, wind_max, conditions.
    """
    place = resolve_location(location)
    imperial = units == "imperial"
    days = max(1, min(int(days), 16))

    payload = _get(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                ]
            ),
            "forecast_days": days,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
            "timezone": "auto",
        },
    )

    daily = payload.get("daily") or {}
    dates: List[str] = daily.get("time") or []
    if not dates:
        raise WeatherAPIError(f"No forecast returned for '{location}'.")

    def at(key: str, i: int):
        values = daily.get(key) or []
        return values[i] if i < len(values) else None

    entries = []
    for i, date in enumerate(dates):
        entries.append(
            {
                "date": date,
                "high": at("temperature_2m_max", i),
                "low": at("temperature_2m_min", i),
                "precip_chance_pct": at("precipitation_probability_max", i),
                "precip_amount": at("precipitation_sum", i),
                "wind_max": at("wind_speed_10m_max", i),
                "conditions": _describe(at("weather_code", i)),
            }
        )

    return {
        "location": _label(place),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": payload.get("timezone"),
        "units": {
            "temperature": "F" if imperial else "C",
            "wind_speed": "mph" if imperial else "km/h",
            "precipitation": "inch" if imperial else "mm",
        },
        "days": entries,
    }


if __name__ == "__main__":
    # Smoke test: python weather_client.py
    print(fetch_current("Pullman, Washington"))
    print(fetch_forecast("Chicago", days=3)["days"][0])
    try:
        resolve_location("Zzzzqqq")
    except WeatherAPIError as exc:
        print("expected failure:", exc)
