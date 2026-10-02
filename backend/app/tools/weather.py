import httpx
from typing import Dict, Any
from app.config import OPEN_METEO_FORECAST_URL


class WeatherUnavailable(Exception):
    """Raised when weather API call fails or payload is incomplete."""
    pass


def get_weather(lat: float, lon: float) -> Dict[str, Any]:
    """
    Fetches live weather forecast from Open-Meteo API for given lat, lon.
    Timeout 8s, 1 retry. Raises WeatherUnavailable on failure.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,wind_speed_10m,wind_gusts_10m,precipitation,weather_code,uv_index",
        "hourly": "temperature_2m,apparent_temperature,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,uv_index,weather_code",
        "daily": "precipitation_sum,precipitation_hours,weather_code,uv_index_max,wind_gusts_10m_max",
    }

    last_exception = None
    # 1 initial try + 1 retry = 2 attempts max
    for attempt in range(2):
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.get(OPEN_METEO_FORECAST_URL, params=params)
                response.raise_for_status()
                data = response.json()

                # Validate expected keys in payload
                if not isinstance(data, dict):
                    raise WeatherUnavailable("API response is not a valid JSON object.")
                for key in ["current", "hourly"]:
                    if key not in data:
                        raise WeatherUnavailable(f"Missing expected key '{key}' in weather payload.")

                return data
        except Exception as e:
            last_exception = e

    raise WeatherUnavailable(f"Failed to fetch weather data after retries: {str(last_exception)}") from last_exception
