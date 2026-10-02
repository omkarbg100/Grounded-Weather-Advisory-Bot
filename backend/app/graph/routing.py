from typing import Literal
from app.graph.state import GraphState


def route_after_parse_intent(
    state: GraphState,
) -> Literal["respond_no_scope", "fetch_weather", "geocode_city", "ask_clarification"]:
    intent = state.get("intent")
    if intent and intent.intent_type in ["smalltalk", "out_of_scope"]:
        return "respond_no_scope"

    # Priority 1: Valid coordinates
    if state.get("coords_candidate") is not None:
        return "fetch_weather"

    # Priority 2: City text
    if state.get("city_text"):
        return "geocode_city"

    # Priority 3: Session last location
    loc = state.get("location")
    if loc is not None:
        return "fetch_weather"

    # Priority 4: Ask clarification
    return "ask_clarification"


def route_after_geocode(state: GraphState) -> Literal["fetch_weather", "fail_location"]:
    if state.get("location") is not None:
        return "fetch_weather"
    return "fail_location"


def route_after_fetch_weather(state: GraphState) -> Literal["build_facts", "fail_weather"]:
    # If there's an explicit error, fail
    if state.get("error_message"):
        return "fail_weather"
    # raw_weather present and non-None means success
    raw = state.get("raw_weather")
    if raw is not None:
        return "build_facts"
    return "fail_weather"


def route_after_match_sops(state: GraphState) -> Literal["resolve_conflicts", "respond_no_sop"]:
    matched = state.get("matched_sops", [])
    if matched and len(matched) > 0:
        return "resolve_conflicts"
    return "respond_no_sop"


def route_after_verify(state: GraphState) -> Literal["finalize", "compose_reply", "deterministic_reply"]:
    if state.get("verify_passed", False):
        return "finalize"
    if state.get("verify_retries", 0) < 1:
        return "compose_reply"
    return "deterministic_reply"
