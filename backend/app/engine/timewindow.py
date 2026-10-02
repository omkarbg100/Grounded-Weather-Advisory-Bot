"""
Named time-window resolution for weather fact derivation.

The old implementation sliced the hourly array at magic indices: `now` meant
`[0:3]`, `this_evening` meant hours 18-22 of day one, `tomorrow` meant `[24:48]`.
The window definitions now live in app/policy_config.yaml, and the indices are
computed by matching the payload's own ISO timestamps rather than assuming the
forecast starts at local midnight.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app import policy_config

TimeWindow = str  # one of policy_config.window_names()


class WindowResolutionError(ValueError):
    """Raised when a requested window cannot be located in the payload."""


class ResolvedWindow:
    """
    An hourly slice of a forecast payload, expressed in payload indices.

    Attributes:
        name: the window name from policy config, or "custom".
        start_index / end_index: half-open slice over the hourly arrays.
        start_iso / end_iso: ISO timestamps bounding the slice.
        is_daytime_window: 1 when the slice contains at least one daytime hour.
    """

    __slots__ = (
        "name",
        "start_index",
        "end_index",
        "start_iso",
        "end_iso",
        "is_daytime_window",
        "label",
    )

    def __init__(
        self,
        name: str,
        start_index: int,
        end_index: int,
        start_iso: Optional[str] = None,
        end_iso: Optional[str] = None,
        is_daytime_window: bool = False,
        label: str = "",
    ):
        self.name = name
        self.start_index = start_index
        self.end_index = end_index
        self.start_iso = start_iso
        self.end_iso = end_iso
        self.is_daytime_window = bool(is_daytime_window)
        self.label = label or name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ResolvedWindow(name={self.name!r}, start_index={self.start_index}, "
            f"end_index={self.end_index}, daytime={self.is_daytime_window})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResolvedWindow):
            return NotImplemented
        return (
            self.name == other.name
            and self.start_index == other.start_index
            and self.end_index == other.end_index
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start": self.start_iso,
            "end": self.end_iso,
            "is_daytime_window": 1 if self.is_daytime_window else 0,
        }


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _hour_of(timestamp: str) -> Optional[int]:
    parsed = _parse_iso(timestamp)
    return parsed.hour if parsed else None


def _is_daytime(times: List[str], start: int, end: int) -> bool:
    day_start, day_end = policy_config.daytime_hours()
    for timestamp in times[start:end]:
        hour = _hour_of(timestamp)
        if hour is not None and day_start <= hour < day_end:
            return True
    return False


def _sliced(
    times: List[str],
    name: str,
    start: int,
    end: int,
    label: str,
    daytime_override: Optional[bool] = None,
) -> ResolvedWindow:
    start = max(0, min(start, len(times)))
    end = max(start, min(end, len(times)))
    # A window definition may pin daytimeness (this_evening is never a daytime
    # exposure window). Otherwise derive it from the hours actually covered.
    is_daytime = (
        daytime_override
        if daytime_override is not None
        else _is_daytime(times, start, end)
    )
    return ResolvedWindow(
        name=name,
        start_index=start,
        end_index=end,
        start_iso=times[start] if start < len(times) else None,
        end_iso=times[end - 1] if end > start else None,
        is_daytime_window=is_daytime,
        label=label,
    )


def resolve_window(
    times: List[str],
    time_window: TimeWindow = "today",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> ResolvedWindow:
    """
    Resolve a named (or explicit) window into payload indices.

    Falls back to the full payload when the requested window cannot be located,
    so a short forecast degrades to "use what we have" instead of raising.
    """
    times = times or []
    if not times:
        return ResolvedWindow(name=time_window, start_index=0, end_index=0, label=time_window)

    known = policy_config.window_names()
    if time_window not in known:
        raise WindowResolutionError(
            f"Unknown time window '{time_window}'. Known windows: {known}"
        )

    if time_window == "custom" or (start_time and end_time):
        return _resolve_custom(times, start_time, end_time)

    config = policy_config.window_config(time_window)
    label = config.get("description", time_window)
    span = int(config.get("span_hours", policy_config.default_window_span_hours()))
    daytime = config.get("daytime")
    total = len(times)

    day_offset = int(config.get("day_offset", 0))
    if day_offset:
        start = day_offset * 24
        if start >= total:
            # Forecast is shorter than a day; use its final 24 hours.
            start = max(0, total - span)
        return _sliced(times, time_window, start, start + span, label, daytime)

    start_hour = config.get("start_hour")
    if start_hour is None or int(start_hour) == 0:
        return _sliced(times, time_window, 0, span, label, daytime)

    # Locate the first occurrence of start_hour inside the leading day.
    target_hour = int(start_hour)
    search_limit = min(total, 24 if day_offset == 0 else total)
    first = None
    for index in range(search_limit):
        if _hour_of(times[index]) == target_hour:
            first = index
            break

    if first is None:
        # The payload does not reach that hour (e.g. a late-evening forecast).
        return _sliced(
            times, time_window, max(0, total - span), total, label, daytime
        )

    return _sliced(times, time_window, first, first + span, label, daytime)


def _resolve_custom(
    times: List[str],
    start_time: Optional[str],
    end_time: Optional[str],
) -> ResolvedWindow:
    start_dt = _parse_iso(start_time or "")
    end_dt = _parse_iso(end_time or "")

    if start_dt is None and end_dt is None:
        raise WindowResolutionError(
            "A custom window requires both start_time and end_time in ISO-8601 format."
        )

    start_index = 0
    end_index = len(times)

    if start_dt is not None:
        start_index = len(times)
        for index, timestamp in enumerate(times):
            parsed = _parse_iso(timestamp)
            if parsed is not None and parsed >= start_dt:
                start_index = index
                break

    if end_dt is not None:
        end_index = 0
        for index, timestamp in enumerate(times):
            parsed = _parse_iso(timestamp)
            if parsed is not None and parsed > end_dt:
                end_index = index
                break
        else:
            end_index = len(times)

    if end_index < start_index:
        start_index, end_index = 0, len(times)

    return _sliced(times, "custom", start_index, end_index, f"{start_time} to {end_time}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)