"""
The fact contract.

Every fact the engine can produce is declared here: how it is read out of an
Open-Meteo payload, how it is summarised over a time window, and how it renders
for a user-facing reply. SOP conditions are validated against these names, so
adding a fact is a one-line change here plus an optional SOP condition.

Previously this was a bare `set` of strings plus hardcoded slice logic in
`derive_facts`.
"""
from typing import Any, Dict, List, NamedTuple, Optional


class Accumulator:
    """Reducer over one hourly series within the window."""

    __slots__ = ("values",)

    def __init__(self, values: Optional[List[Any]] = None):
        self.values: List[Any] = list(values or [])

    def add(self, value: Any) -> None:
        if value is not None:
            self.values.append(value)

    def max(self, default: float = 0.0) -> float:
        numeric = [float(v) for v in self.values if isinstance(v, (int, float))]
        return max(numeric) if numeric else default

    def sum(self) -> float:
        return float(
            sum(float(v) for v in self.values if isinstance(v, (int, float)))
        )

    def count_above(self, threshold: float) -> int:
        return sum(
            1
            for v in self.values
            if isinstance(v, (int, float)) and float(v) > threshold
        )

    def contains_any(self, candidates: List[Any]) -> bool:
        return any(v in candidates for v in self.values)


class FactSpec(NamedTuple):
    name: str
    series: Optional[str]           # hourly field this fact is built from
    reduce: Optional[str]           # 'max' | 'sum' | 'count_above' | 'contains' | 'constant'
    unit: str
    label: str
    threshold: float = 0.0          # used by 'count_above'
    candidates: Optional[List[Any]] = None   # used by 'contains'
    constant: Any = None            # used by 'constant'
    daily_field: Optional[str] = None
    as_int: bool = False             # emit an int rather than a float


FACT_SPECS: List[FactSpec] = [
    FactSpec("temp_c", "temperature_2m", "max", "C", "temperature"),
    FactSpec("feels_like_c", "apparent_temperature", "max", "C", "apparent temperature"),
    FactSpec("wind_kmh", "wind_speed_10m", "max", "km/h", "sustained wind speed"),
    FactSpec("max_gust_kmh", "wind_gusts_10m", "max", "km/h", "peak gust"),
    FactSpec("precip_prob_max", "precipitation_probability", "max", "%", "peak precipitation probability"),
    FactSpec("precip_window_mm", "precipitation", "sum", "mm", "precipitation in window"),
    FactSpec("precip_24h_mm", None, "daily", "mm", "24-hour precipitation total", daily_field="precipitation_sum"),
    FactSpec("precip_hours_24h", None, "daily", "h", "wet hours in 24h", daily_field="precipitation_hours"),
    FactSpec("uv_max", "uv_index", "max", "", "peak UV index"),
    FactSpec("weather_code_max", "weather_code", "max", "", "worst weather code", as_int=True),
    FactSpec("is_daytime_window", None, "daytime", "", "daytime hours present", constant=0, as_int=True),
    FactSpec("has_thunderstorm", "weather_code", "contains", "", "thunderstorm present", as_int=True),
]

FACT_SPEC_BY_NAME: Dict[str, FactSpec] = {spec.name: spec for spec in FACT_SPECS}

# Fact names SOP conditions may reference. Derived from the registry so the
# SOP loader cannot drift away from what the engine can actually compute.
KNOWN_FACT_NAMES = set(FACT_SPEC_BY_NAME.keys())

# Facts that always hold a constant for a given window rather than reading data.
CONSTANT_FACTS = ("is_daytime_window",)

# Human-readable rendering, used by the model-facing fact summary and by the
# deterministic fallback reply.
def describe_fact(name: str, value: Any) -> str:
    spec = FACT_SPEC_BY_NAME.get(name)
    if spec is None:
        return f"{name}: {value}"
    label = spec.label
    if name == "has_thunderstorm":
        return f"thunderstorms in window: {'yes' if value else 'no'}"
    if name == "is_daytime_window":
        return f"daytime hours in window: {'yes' if value else 'no'}"
    if name == "weather_code_max":
        return f"most severe weather code: {value}"
    if spec.unit:
        return f"{label}: {value} {spec.unit}"
    return f"{label}: {value}"


def describe_facts(facts: Dict[str, Any]) -> str:
    """Comma-separated `label: value unit` pairs, for prompts and fallbacks."""
    parts = [describe_fact(name, facts[name]) for name in FACT_SPEC_BY_NAME if name in facts]
    for name, value in facts.items():
        if name not in FACT_SPEC_BY_NAME:
            parts.append(f"{name}: {value}")
    return ", ".join(parts)