from typing import TypedDict, Optional, Tuple, Dict, Any, List
from app.schemas import LocationModel, IntentParseResult, SOPModel


class GraphState(TypedDict, total=False):
    session_id: str
    message: str
    sanitized_message: str
    injection_flag: bool
    coords_candidate: Optional[Tuple[float, float]]
    intent: Optional[IntentParseResult]
    city_text: Optional[str]
    time_ref: str
    activity: str
    location: Optional[LocationModel]
    location_note: str
    raw_weather: Optional[Dict[str, Any]]
    facts: Dict[str, Any]
    matched_sops: List[Tuple[SOPModel, str]]
    ranking_result: Dict[str, Any]
    reply: str
    verify_retries: int
    outcome: str
    trace: List[str]
    error_message: Optional[str]
