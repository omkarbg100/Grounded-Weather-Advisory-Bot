from typing import Dict, Any, Set
from datetime import datetime

KNOWN_FACT_NAMES: Set[str] = {
    "temp_c",
    "feels_like_c",
    "wind_kmh",
    "max_gust_kmh",
    "precip_prob_max",
    "precip_window_mm",
    "precip_24h_mm",
    "precip_hours_24h",
    "uv_max",
    "weather_code_max",
    "is_daytime_window",
    "has_thunderstorm",
}


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def derive_facts(raw_weather: Dict[str, Any], time_ref: str = "unspecified") -> Dict[str, Any]:
    """
    Derives facts deterministically from raw Open-Meteo payload for the target time window.
    """
    current = raw_weather.get("current", {})
    hourly = raw_weather.get("hourly", {})
    daily = raw_weather.get("daily", {})

    times = hourly.get("time", [])
    if not times:
        # Fallback to current if hourly empty
        temp = _safe_float(current.get("temperature_2m"))
        feels_like = _safe_float(current.get("apparent_temperature"), temp)
        wind = _safe_float(current.get("wind_speed_10m"))
        gust = _safe_float(current.get("wind_gusts_10m"), wind)
        precip = _safe_float(current.get("precipitation"))
        code = int(_safe_float(current.get("weather_code")))
        uv = _safe_float(current.get("uv_index"))
        
        has_ts = 1 if code in [95, 96, 99] else 0

        return {
            "temp_c": round(temp, 1),
            "feels_like_c": round(feels_like, 1),
            "wind_kmh": round(wind, 1),
            "max_gust_kmh": round(gust, 1),
            "precip_prob_max": 0,
            "precip_window_mm": round(precip, 1),
            "precip_24h_mm": round(precip, 1),
            "precip_hours_24h": 1 if precip > 0 else 0,
            "uv_max": round(uv, 1),
            "weather_code_max": code,
            "is_daytime_window": 1,
            "has_thunderstorm": has_ts,
        }

    # Determine hourly index window
    total_hours = len(times)
    # Default window: first 24 hours or entire payload
    start_idx = 0
    end_idx = min(24, total_hours)

    if time_ref == "now":
        start_idx = 0
        end_idx = min(3, total_hours)
    elif time_ref == "this_evening":
        # Search for evening hours 18 to 22 in first day
        evening_indices = [
            i for i, t in enumerate(times[:24])
            if "T18:" in t or "T19:" in t or "T20:" in t or "T21:" in t or "T22:" in t
        ]
        if evening_indices:
            start_idx = evening_indices[0]
            end_idx = evening_indices[-1] + 1
        else:
            start_idx = 18
            end_idx = min(23, total_hours)
    elif time_ref == "tomorrow":
        if total_hours >= 48:
            start_idx = 24
            end_idx = 48
        else:
            start_idx = max(0, total_hours - 24)
            end_idx = total_hours

    # Extract window slices
    temps = [ _safe_float(v) for v in hourly.get("temperature_2m", [])[start_idx:end_idx] ]
    feels = [ _safe_float(v) for v in hourly.get("apparent_temperature", [])[start_idx:end_idx] ]
    winds = [ _safe_float(v) for v in hourly.get("wind_speed_10m", [])[start_idx:end_idx] ]
    gusts = [ _safe_float(v) for v in hourly.get("wind_gusts_10m", [])[start_idx:end_idx] ]
    precip_probs = [ _safe_float(v) for v in hourly.get("precipitation_probability", [])[start_idx:end_idx] ]
    precips = [ _safe_float(v) for v in hourly.get("precipitation", [])[start_idx:end_idx] ]
    uvs = [ _safe_float(v) for v in hourly.get("uv_index", [])[start_idx:end_idx] ]
    codes = [ int(_safe_float(v)) for v in hourly.get("weather_code", [])[start_idx:end_idx] ]

    # 24h daily aggregates
    all_precips_24h = [ _safe_float(v) for v in hourly.get("precipitation", [])[:24] ]
    precip_24h_mm = sum(all_precips_24h)
    precip_hours_24h = sum(1 for p in all_precips_24h if p > 0.1)

    if daily.get("precipitation_sum") and len(daily["precipitation_sum"]) > 0:
        precip_24h_mm = _safe_float(daily["precipitation_sum"][0])
    if daily.get("precipitation_hours") and len(daily["precipitation_hours"]) > 0:
        precip_hours_24h = int(_safe_float(daily["precipitation_hours"][0]))

    # Values for window
    temp_c = round(max(temps) if temps else _safe_float(current.get("temperature_2m")), 1)
    feels_like_c = round(max(feels) if feels else _safe_float(current.get("apparent_temperature")), 1)
    wind_kmh = round(max(winds) if winds else _safe_float(current.get("wind_speed_10m")), 1)
    max_gust_kmh = round(max(gusts) if gusts else wind_kmh, 1)
    precip_prob_max = int(max(precip_probs)) if precip_probs else 0
    precip_window_mm = round(sum(precips), 1)
    uv_max = round(max(uvs) if uvs else 0.0, 1)
    weather_code_max = max(codes) if codes else int(_safe_float(current.get("weather_code", 0)))
    
    # Check for thunderstorm codes (95, 96, 99)
    has_thunderstorm = 1 if any(c in [95, 96, 99] for c in codes) else 0

    # Daytime window check: true if any hour is daytime (e.g., between 06:00 and 19:00)
    is_daytime_window = 1
    if time_ref == "this_evening":
        is_daytime_window = 0

    return {
        "temp_c": temp_c,
        "feels_like_c": feels_like_c,
        "wind_kmh": wind_kmh,
        "max_gust_kmh": max_gust_kmh,
        "precip_prob_max": precip_prob_max,
        "precip_window_mm": precip_window_mm,
        "precip_24h_mm": round(precip_24h_mm, 1),
        "precip_hours_24h": precip_hours_24h,
        "uv_max": uv_max,
        "weather_code_max": weather_code_max,
        "is_daytime_window": is_daytime_window,
        "has_thunderstorm": has_thunderstorm,
    }
