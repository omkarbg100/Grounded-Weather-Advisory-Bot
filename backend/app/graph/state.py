from typing import Any, Dict, List, Optional, TypedDict

from app.agent.schemas import ForecastBundle
from app.schemas import LocationModel


class GraphState(TypedDict, total=False):
    """
    Agent state.

    Fields split into three groups:

    * **Input**: what the user sent, and what the guardrails made of it.
    * **Refs**: server-issued handles. The model can only reach a location or a
      forecast through these, which is what stops it authoring facts.
    * **Engine output**: the deterministic results the verifier checks against.

    `location`, `facts`, `matched_sops` and `ranking_result` keep the names the
    API response and the offline fallback path already use.
    """

    # --- input ---
    session_id: str
    message: str
    sanitized_message: str
    injection_flag: bool

    # --- server-issued refs ---
    location_refs: Dict[str, LocationModel]
    forecast_refs: Dict[str, ForecastBundle]
    forecast_ref: str
    time_window: str

    # --- engine output ---
    location: Optional[LocationModel]
    location_note: str
    coords_candidate: Optional[Any]
    raw_weather: Optional[Dict[str, Any]]
    facts: Dict[str, Any]
    activity: str
    matched_sops: List[Any]
    ranking_result: Dict[str, Any]

    # --- reply ---
    reply: str
    verify_passed: bool
    verify_retries: int
    verify_errors: List[str]
    outcome: str

    # --- bookkeeping ---
    trace: List[str]
    error_message: Optional[str]