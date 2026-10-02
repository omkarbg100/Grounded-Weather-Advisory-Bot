import httpx
from typing import Dict, Any
from app.config import OPEN_METEO_GEOCODE_URL


class LocationNotFound(Exception):
    """Raised when geocoding city lookup yields no results or fails."""
    pass


def geocode_city(name: str) -> Dict[str, Any]:
    """
    Geocodes city name using Open-Meteo Geocoding API.
    Returns dict: {name, admin1, country, lat, lon}.
    Raises LocationNotFound on empty results or HTTP error.
    """
    if not name or not name.strip():
        raise LocationNotFound("City name cannot be empty.")

    params = {
        "name": name.strip(),
        "count": 1,
        "language": "en",
        "format": "json"
    }

    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(OPEN_METEO_GEOCODE_URL, params=params)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if not results or len(results) == 0:
                raise LocationNotFound(f"No location found for city: '{name}'")

            top = results[0]
            return {
                "name": top.get("name", name),
                "admin1": top.get("admin1", ""),
                "country": top.get("country", ""),
                "lat": float(top.get("latitude")),
                "lon": float(top.get("longitude")),
            }
    except LocationNotFound:
        raise
    except Exception as e:
        raise LocationNotFound(f"Geocoding service error for city '{name}': {str(e)}") from e
