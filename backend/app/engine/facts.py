"""
Weather fact derivation.

Facts are computed deterministically from an Open-Meteo payload over a window
resolved by `engine.timewindow`. Every constant and reducer comes from
`engine.fact_registry` and `app.policy_config`, so no magic numbers remain here.

The LLM never writes into the returned dict: it can only request a window, and
the engine produces the values.
"""
from typing import Any, Dict, List, Optional

from app import policy_config
from app.engine.fact_registry import (
    Accumulator,
    FACT_SPECS,
    KNOWN_FACT_NAMES,  # re-exported for the SOP loader
)
from app.engine.timewindow import ResolvedWindow, TimeWindow, resolve_window

__all__ = ["derive_facts", "KNOWN_FACT_NAMES", "resolve_window", "ResolvedWindow", "TimeWindow"]


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _numeric(values: Any) -> List[float]:
    out: List[float] = []
    for value in values or []:
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def _hourly_times(raw: Dict[str, Any]) -> List[str]:
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    return [str(t) for t in times]


def _reduce(spec_reduce: str, accumulator: Accumulator, fallback: float) -> Any:
    if spec_reduce == "max":
        return round(accumulator.max(fallback), 1)
    if spec_reduce == "sum":
        return round(accumulator.sum(), 1)
    if spec_reduce == "daily":
        return fallback
    if spec_reduce == "count_above":
        return accumulator.count_above(fallback)
    if spec_reduce == "contains":
        return 1 if accumulator.contains_any(
            policy_config.thunderstorm_codes()
        ) else 0
    return fallback


def derive_facts(
    raw_weather: Dict[str, Any],
    window: Optional[ResolvedWindow] = None,
    time_window: Optional[TimeWindow] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Derive the fact dict for a forecast payload.

    Pass an already-resolved `window`, or a `time_window` name (plus optional
    ISO bounds) to have it resolved here. Returns a dict keyed by every name in
    `FACT_SPEC_BY_NAME` so SOP conditions always find the key they reference.
    """
    raw_weather = raw_weather or {}
    times = _hourly_times(raw_weather)

    if window is None:
        window = resolve_window(times, time_window or "today", start_time, end_time)

    current = raw_weather.get("current") or {}
    hourly = raw_weather.get("hourly") or {}
    daily = raw_weather.get("daily") or {}

    start = window.start_index
    end = window.end_index

    facts: Dict[str, Any] = {}

    for spec in FACT_SPECS:
        if spec.reduce == "daytime":
            facts[spec.name] = 1 if window.is_daytime_window else 0
            continue

        if spec.reduce == "daily":
            daily_values = daily.get(spec.daily_field) or []
            if daily_values:
                value = _safe_float(daily_values[0])
                facts[spec.name] = int(value) if spec.name == "precip_hours_24h" else round(value, 1)
            else:
                facts[spec.name] = _daily_from_hourly(spec, hourly)
            continue

        series = _numeric((hourly.get(spec.series) or [])[start:end]) if spec.series else []
        accumulator = Accumulator(series)

        if series:
            value = _reduce(
                spec.reduce,
                accumulator,
                policy_config.wet_hour_mm() if spec.reduce == "count_above" else 0.0,
            )
            facts[spec.name] = int(value) if spec.as_int else value
            continue

        # No hourly coverage for this window: fall back to `current` observations.
        facts[spec.name] = _fallback_from_current(spec.name, spec, current)

    return facts


def _daily_from_hourly(spec, hourly: Dict[str, Any]) -> Any:
    """
    24-hour aggregates when the payload carries no daily block.

    Both facts are defined over the leading 24 hourly entries, which is the same
    window the API's `daily` block reports.
    """
    leading = _numeric((hourly.get("precipitation") or [])[:24])
    if spec.name == "precip_24h_mm":
        return round(sum(leading), 1)
    if spec.name == "precip_hours_24h":
        return sum(1 for value in leading if value > policy_config.wet_hour_mm())
    return 0


def _fallback_from_current(name: str, spec, current: Dict[str, Any]) -> Any:
    """Last-resort values when the hourly slice is empty (e.g. short payloads)."""
    if name == "temp_c":
        return round(_safe_float(current.get("temperature_2m")), 1)
    if name == "feels_like_c":
        return round(
            _safe_float(current.get("apparent_temperature"), _safe_float(current.get("temperature_2m"))),
            1,
        )
    if name == "wind_kmh":
        return round(_safe_float(current.get("wind_speed_10m")), 1)
    if name == "max_gust_kmh":
        return round(
            _safe_float(current.get("wind_gusts_10m"), _safe_float(current.get("wind_speed_10m"))),
            1,
        )
    if name == "precip_window_mm":
        return round(_safe_float(current.get("precipitation")), 1)
    if name == "precip_24h_mm":
        return round(_safe_float(current.get("precipitation")), 1)
    if name == "precip_hours_24h":
        return 1 if _safe_float(current.get("precipitation")) > 0 else 0
    if name == "uv_max":
        return round(_safe_float(current.get("uv_index")), 1)
    if name == "weather_code_max":
        return int(_safe_float(current.get("weather_code")))
    if name == "has_thunderstorm":
        code = int(_safe_float(current.get("weather_code")))
        return 1 if code in policy_config.thunderstorm_codes() else 0
    if name == "precip_prob_max":
        return 0
    return spec.constant if spec.constant is not None else 0