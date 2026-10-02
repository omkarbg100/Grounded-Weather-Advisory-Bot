"""
Typed access to app/policy_config.yaml.

The deterministic engine, the tool layer and the LLM client all read their
constants from here instead of embedding literals in Python. The file is
validated once at import time so a malformed edit fails loudly at startup.
"""
import functools
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

POLICY_CONFIG_PATH = Path(__file__).resolve().parent / "policy_config.yaml"


class PolicyConfigError(ValueError):
    """Raised when policy_config.yaml is missing or structurally invalid."""


@functools.lru_cache(maxsize=1)
def load_policy_config(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = Path(path) if path else POLICY_CONFIG_PATH
    if not cfg_path.exists():
        raise PolicyConfigError(f"Policy config not found: {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise PolicyConfigError("Policy config must be a mapping at the top level.")

    required = [
        "severity_rank",
        "thunderstorm_codes",
        "wet_hour_mm",
        "daytime",
        "time_windows",
        "coordinate_bounds",
        "weather_request",
        "geocode_request",
        "agent",
    ]
    missing = [key for key in required if key not in raw]
    if missing:
        raise PolicyConfigError(f"Policy config missing required sections: {missing}")

    return raw


def _cfg() -> Dict[str, Any]:
    return load_policy_config()


# --- Severity ranking ---

def severity_rank(severity: str) -> int:
    """Higher rank wins. Unknown severities rank lowest."""
    ranks: List[str] = _cfg()["severity_rank"]
    try:
        return len(ranks) - ranks.index(severity.lower())
    except ValueError:
        return 0


def severity_order() -> List[str]:
    return list(_cfg()["severity_rank"])


# --- Fact derivation constants ---

def thunderstorm_codes() -> List[int]:
    return [int(code) for code in _cfg()["thunderstorm_codes"]]


def wet_hour_mm() -> float:
    return float(_cfg()["wet_hour_mm"])


def daytime_hours() -> tuple:
    daytime = _cfg()["daytime"]
    return int(daytime["start_hour"]), int(daytime["end_hour"])


# --- Time windows ---

def time_windows() -> Dict[str, Dict[str, Any]]:
    return dict(_cfg()["time_windows"])


def window_names() -> List[str]:
    return list(time_windows().keys())


def window_config(name: str) -> Dict[str, Any]:
    windows = time_windows()
    if name not in windows:
        raise PolicyConfigError(
            f"Unknown time window '{name}'. Known windows: {sorted(windows)}"
        )
    return dict(windows[name])


def default_window_span_hours() -> int:
    return int(_cfg()["default_window_span_hours"])


# --- Coordinates ---

def latitude_bounds() -> tuple:
    bounds = _cfg()["coordinate_bounds"]["latitude"]
    return float(bounds[0]), float(bounds[1])


def longitude_bounds() -> tuple:
    bounds = _cfg()["coordinate_bounds"]["longitude"]
    return float(bounds[0]), float(bounds[1])


def in_bounds(lat: float, lon: float) -> bool:
    lat_min, lat_max = latitude_bounds()
    lon_min, lon_max = longitude_bounds()
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


# --- Weather request ---

def weather_config() -> Dict[str, Any]:
    return dict(_cfg()["weather_request"])


def forecast_days_bounds() -> tuple:
    days = weather_config()["forecast_days"]
    return int(days["min"]), int(days["max"])


def clamp_forecast_days(days: Optional[int]) -> int:
    lo, hi = forecast_days_bounds()
    default = int(weather_config()["forecast_days"]["default"])
    if days is None:
        return default
    return max(lo, min(hi, int(days)))


# --- Geocode request ---

def geocode_config() -> Dict[str, Any]:
    return dict(_cfg()["geocode_request"])


def clamp_geocode_limit(limit: Optional[int]) -> int:
    cfg = geocode_config()
    lo, hi = 1, int(cfg["max_results"])
    if limit is None:
        return int(cfg["default_results"])
    return max(lo, min(hi, int(limit)))


# --- Agent ---

def agent_config() -> Dict[str, Any]:
    return dict(_cfg()["agent"])