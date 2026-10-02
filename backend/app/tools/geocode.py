"""
Location resolution via the Open-Meteo geocoding API.

`geocode_search` returns ranked candidates so the agent can let the model pick
between, say, "Springfield, Illinois" and "Springfield, Missouri", instead of
silently taking whichever result the API happened to rank first.
"""
import functools
import logging
from typing import Any, Dict, List, Optional

import httpx

from app import policy_config
from app.config import OPEN_METEO_GEOCODE_URL

logger = logging.getLogger(__name__)


class LocationNotFound(Exception):
    """Raised when a lookup yields no results or the service errors."""


@functools.lru_cache(maxsize=128)
def geocode_search(
    name: str,
    country_code: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Look up place names, returning up to `limit` candidates.

    Each candidate is `{name, admin1, country, latitude, longitude}`. Raises
    LocationNotFound on an empty query, no results, or a service error.
    """
    query = (name or "").strip()
    if not query:
        raise LocationNotFound("Location name cannot be empty.")

    config = policy_config.geocode_config()
    count = policy_config.clamp_geocode_limit(limit)

    params: Dict[str, Any] = {
        "name": query,
        "count": count,
        "language": config.get("language", "en"),
        "format": "json",
    }
    if country_code:
        params["countryCode"] = country_code.strip().upper()

    try:
        with httpx.Client(timeout=float(config.get("timeout_seconds", 8.0))) as client:
            response = client.get(OPEN_METEO_GEOCODE_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise LocationNotFound(f"Geocoding service error for '{query}': {exc}") from exc

    return _normalise(data, query, count)


def _normalise(data: Any, query: str, count: int) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise LocationNotFound(f"No location found for '{query}'.")

    results = data.get("results") or []
    candidates: List[Dict[str, Any]] = []
    for item in results[:count]:
        try:
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not policy_config.in_bounds(latitude, longitude):
            continue
        candidates.append(
            {
                "name": item.get("name") or query,
                "admin1": item.get("admin1") or "",
                "country": item.get("country") or "",
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    if not candidates:
        raise LocationNotFound(f"No location found for '{query}'.")
    return candidates


def geocode_city(name: str) -> Dict[str, Any]:
    """
    Single best match for a place name. Kept for the offline fallback path,
    which has no model to disambiguate with.
    """
    return geocode_search(name, limit=1)[0]


def clear_cache() -> None:
    geocode_search.cache_clear()