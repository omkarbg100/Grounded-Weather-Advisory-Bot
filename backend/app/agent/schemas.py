"""
Tool argument and result contracts.

Every value the model can influence passes through one of these models. Bounds
live on the field (`le`, `ge`, `max_length`) so an out-of-range argument is
rejected at validation time rather than being silently used.

Note the deliberate asymmetry: models describing *inputs* (queries, coordinates,
windows) are strict, while models describing *outputs* the model will read
(forecasts, policy evaluations) are permissive. The model must never be able to
influence engine output, only engine inputs.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ToolError(Exception):
    """Raised by a tool handler. Surfaced to the model as a recoverable result."""


class ToolResult(BaseModel):
    """
    What a tool handler returns.

    Attributes:
        payload: the JSON the model sees. Keep it small and self-describing.
        updates: state keys to merge into the graph, never shown to the model.
        is_error: when true the model is told to recover rather than guess.
    """

    payload: Dict[str, Any] = Field(default_factory=dict)
    updates: Dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False

    @classmethod
    def ok(cls, payload: Dict[str, Any], **updates) -> "ToolResult":
        return cls(payload=payload, updates=updates, is_error=False)

    @classmethod
    def fail(cls, message: str, **payload) -> "ToolResult":
        body: Dict[str, Any] = {"error": message}
        body.update(payload)
        return cls(payload=body, updates={}, is_error=True)


# --- search_location ---

class SearchLocationArgs(BaseModel):
    query: str = Field(
        ...,
        min_length=2,
        max_length=120,
        description=(
            "Place name to look up, as the user wrote it plus any disambiguating "
            "detail they gave (country, state or region)."
        ),
    )
    country_code: Optional[str] = Field(
        None,
        min_length=2,
        max_length=2,
        description="Optional two-letter ISO country code to disambiguate, e.g. 'US' or 'IN'.",
    )
    limit: int = Field(
        3,
        ge=1,
        le=5,
        description="How many candidates to return.",
    )


class LocationCandidate(BaseModel):
    ref: str
    label: str
    latitude: float
    longitude: float
    admin1: str = ""
    country: str = ""


# --- get_forecast ---

class GetForecastArgs(BaseModel):
    location_ref: Optional[str] = Field(
        None,
        description=(
            "Reference returned by search_location. Preferred. Leave unset to pass "
            "latitude and longitude directly."
        ),
    )
    latitude: Optional[float] = Field(
        None, ge=-90.0, le=90.0, description="Latitude. Only used when location_ref is unset."
    )
    longitude: Optional[float] = Field(
        None, ge=-180.0, le=180.0, description="Longitude. Only used when location_ref is unset."
    )
    forecast_days: int = Field(
        2,
        ge=1,
        le=7,
        description=(
            "How many days of forecast to retrieve. 1-2 for 'right now' or 'today', "
            "2 for 'tomorrow', 5-7 when the user asks about the week."
        ),
    )
    time_window: str = Field(
        "today",
        description=(
            "Which hours the answer should cover. One of: now, today, this_evening, "
            "tomorrow, next_24h, next_48h, custom. Use 'now' for 'is it safe right "
            "now', 'this_evening' for 'tonight', 'tomorrow' for 'tomorrow'."
        ),
    )
    start_time: Optional[str] = Field(
        None,
        description="ISO-8601 start. Only with time_window='custom'.",
    )
    end_time: Optional[str] = Field(
        None,
        description="ISO-8601 end. Only with time_window='custom'.",
    )


class ForecastWindowPayload(BaseModel):
    name: str
    label: str = ""
    start: Optional[str] = None
    end: Optional[str] = None
    is_daytime_window: int = 0


# --- evaluate_policies ---

class EvaluatePoliciesArgs(BaseModel):
    forecast_ref: str = Field(
        ...,
        description=(
            "Reference returned by get_forecast. Facts are read from the server-side "
            "forecast by this reference; you cannot supply weather values yourself."
        ),
    )
    activity: str = Field(
        "unknown",
        description=(
            "Activity tag the user described. Call get_policy_catalog first if you "
            "are unsure which tag fits. Use 'unknown' if the activity is genuinely "
            "not in the taxonomy."
        ),
    )
    include_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Optional SOP ids to focus on. Narrows the result only; it cannot add a "
            "policy that did not match."
        ),
    )


class GetPolicyCatalogArgs(BaseModel):
    category: Optional[str] = Field(
        None, description="Filter by SOP category, e.g. 'exercise'. Omit for all."
    )
    activity: Optional[str] = Field(
        None, description="Filter to policies covering this activity tag."
    )


# --- end_turn ---
#
# Classifying why a turn ended is a judgement call, not a code branch. Handing
# it to the model as a constrained tool argument removes the hand-written
# branch-per-outcome nodes while keeping the set of outcomes server-validated.
#
# `llm_unavailable` and `unavailable` are deliberately absent: the model may not
# claim them. Only the graph can report that it could not do the work.

TerminalOutcome = Literal[
    "answered",
    "no_sop",
    "clarify",
    "out_of_scope",
    "location_failed",
    "weather_failed",
]

TERMINAL_OUTCOMES = (
    "answered",
    "no_sop",
    "clarify",
    "out_of_scope",
    "location_failed",
    "weather_failed",
)


class EndTurnArgs(BaseModel):
    outcome: TerminalOutcome = Field(
        ...,
        description=(
            "Why this turn ends. 'answered': policies matched and you are "
            "reporting them. 'no_sop': you looked up the conditions and no "
            "published policy covers this situation. 'clarify': you need the "
            "user to tell you something first. 'out_of_scope': the question is "
            "not about outdoor weather safety. 'location_failed': the place "
            "could not be resolved. 'weather_failed': live weather was "
            "unavailable."
        ),
    )
    message: str = Field(
        ...,
        max_length=4000,
        description=(
            "The reply to show the user. Required. Cite policy ids and only "
            "numbers from tool results."
        ),
    )


# --- Evaluated output shared with state ---

class ForecastBundle(BaseModel):
    """Server-side record of one resolved forecast, keyed by `ref` in state."""

    ref: str
    location_label: str
    latitude: float
    longitude: float
    window: Dict[str, Any]
    facts: Dict[str, Any]
    forecast_days: int = 2