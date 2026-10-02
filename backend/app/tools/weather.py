"""
Live forecast retrieval.

The field allowlist and the request bounds live in app/policy_config.yaml. The
caller (the agent's `get_forecast` tool) may choose the forecast horizon, which
is clamped server-side; it cannot choose which variables are requested, because
the SOP fact contract is derived from that list.
"""
import functools
import logging
from typing import Any, Dict, Optional

import httpx

from app import policy_config
from app.config import OPEN_METEO_FORECAST_URL

logger = logging.getLogger(__name__)


class WeatherUnavailable(Exception):
    """Raised when the forecast API fails or returns an unusable payload."""


@functools.lru_cache(maxsize=32)
def get_weather(
    latitude: float,
    longitude: float,
    forecast_days: Optional[int] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch a forecast for a coordinate.

    `forecast_days` is clamped to the configured range. Raises
    WeatherUnavailable after exhausting the configured retries.
    """
    lat_min, lat_max = policy_config.latitude_bounds()
    lon_min, lon_max = policy_config.longitude_bounds()
    if not (lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max):
        raise WeatherUnavailable(
            f"Coordinates out of range: ({latitude}, {longitude})"
        )

    config = policy_config.weather_config()
    days = policy_config.clamp_forecast_days(forecast_days)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone or config.get("timezone", "auto"),
        "forecast_days": days,
        "current": ",".join(config["current_fields"]),
        "hourly": ",".join(config["hourly_fields"]),
        "daily": ",".join(config["daily_fields"]),
    }

    attempts = int(config.get("max_retries", 0)) + 1
    timeout = float(config.get("timeout_seconds", 8.0))

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(OPEN_METEO_FORECAST_URL, params=params)
                response.raise_for_status()
                return _validate(response.json())
        except Exception as exc:  # network, HTTP status, or payload shape
            last_error = exc
            logger.warning(
                "Forecast fetch failed (attempt %s/%s) for (%s, %s): %s",
                attempt + 1, attempts, latitude, longitude, exc,
            )

    raise WeatherUnavailable(
        f"Failed to fetch weather data after {attempts} attempt(s): {last_error}"
    ) from last_error


def _validate(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise WeatherUnavailable("Forecast response is not a JSON object.")
    for key in ("current", "hourly"):
        if key not in data:
            raise WeatherUnavailable(f"Forecast payload missing '{key}'.")
    return data


def clear_cache() -> None:
    """Drop memoised forecasts. Used by tests and between eval runs."""
    get_weather.cache_clear()