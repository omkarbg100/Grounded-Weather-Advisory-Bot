import re
from typing import Dict, Any, Tuple
from app.graph.state import GraphState
from app.engine.location import parse_coordinates, resolve_location_decision
from app.engine.facts import derive_facts
from app.engine.matcher import match_sops
from app.engine.ranking import resolve_conflicts
from app.engine.verifier import verify_reply as check_reply
from app.tools.sop_loader import load_sops
from app.tools.weather import get_weather, WeatherUnavailable
from app.tools.geocode import geocode_city, LocationNotFound
from app.llm.client import complete_structured, complete_text
from app.llm.prompts import build_intent_parser_prompt, build_compose_reply_prompt
from app.schemas import IntentParseResult, LocationModel

# Preload SOP registry once
SOP_REGISTRY = load_sops()

# --- Security Guardrails: Injection Patterns & PII Scrubbing ---

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"override\s+sops?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"unfiltered\s+ai", re.IGNORECASE),
    re.compile(r"bypass\s+rules", re.IGNORECASE),
    re.compile(r"forget\s+instructions", re.IGNORECASE),
    re.compile(r"<\s*\|?\s*im_start\s*\|?\s*>", re.IGNORECASE),
]

PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"), "[SENSITIVE_ID_REDACTED]"),
]


def _scrub_pii(text: str) -> str:
    """Scrub PII (emails, phone numbers, credit card / national IDs) from user message."""
    scrubbed = text
    for pattern, replacement in PII_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


def guard_input(state: GraphState) -> Dict[str, Any]:
    """
    Security Middleware Node:
    1. Sanitizes non-printable characters.
    2. Scrubs PII (emails, phone numbers, sensitive IDs).
    3. Scans for prompt injection / jailbreak attempts.
    """
    trace = state.get("trace", []) + ["guard_input"]
    msg = state.get("message", "")[:500]

    # Clean control characters except newline/tab
    sanitized = "".join(ch for ch in msg if ch >= " " or ch in "\n\r\t").strip()

    # PII Scrubbing Middleware
    sanitized = _scrub_pii(sanitized)

    # Prompt Injection Guardrail Middleware
    injection_flag = any(pat.search(sanitized) for pat in INJECTION_PATTERNS)

    return {
        "sanitized_message": sanitized,
        "injection_flag": injection_flag,
        "trace": trace,
    }


def extract_coords(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["extract_coords"]
    msg = state.get("sanitized_message", "")
    coords = parse_coordinates(msg)
    return {
        "coords_candidate": coords,
        "trace": trace,
    }


def parse_intent(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["parse_intent"]
    msg = state.get("sanitized_message", "")
    sess_activity = state.get("activity")
    sess_location = state.get("location")
    sess_city = sess_location.label if (sess_location and sess_location.source == "geocoded") else None

    prompt = build_intent_parser_prompt(
        user_message=msg,
        taxonomy=SOP_REGISTRY.taxonomy,
        session_activity=sess_activity,
        session_city=sess_city
    )

    intent_res = complete_structured(prompt, IntentParseResult)

    activity = intent_res.activity
    city_text = intent_res.city_text
    time_ref = intent_res.time_ref

    # Clean up city_text if model mistakenly echoed coordinate label
    if city_text and ("Coordinates" in city_text or "(" in city_text):
        city_text = None

    # Handle follow-ups: reuse session state if unsupplied
    if intent_res.intent_type == "followup":
        if not activity or activity == "unknown":
            activity = sess_activity or "unknown"
        # Only set city_text from session if location came from geocoding (has a real city name).
        # Coordinate-based sessions must NOT inject label as city_text (would break geocode routing).
        if not city_text and sess_location and sess_location.source == "geocoded":
            city_text = sess_city

    result = {
        "intent": intent_res,
        "activity": activity,
        "city_text": city_text,
        "time_ref": time_ref,
        "trace": trace,
    }
    # For follow-up with no city_text: carry session location into state so
    # route_after_parse_intent can detect it at priority-3 and go to fetch_weather.
    if intent_res.intent_type == "followup" and sess_location and not city_text:
        result["location"] = sess_location
    return result


def geocode_city_node(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["geocode_city"]
    city_text = state.get("city_text")
    if not city_text:
        return {"location": None, "error_message": "No city text to geocode", "trace": trace}

    try:
        geo_res = geocode_city(city_text)
        loc, note = resolve_location_decision(
            coords_candidate=None,
            city_text=city_text,
            session_location=None,
            geocoded_result=geo_res
        )
        return {
            "location": loc,
            "location_note": note,
            "trace": trace,
        }
    except LocationNotFound as e:
        return {
            "location": None,
            "error_message": str(e),
            "trace": trace,
        }


def fetch_weather(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["fetch_weather"]
    
    coords_cand = state.get("coords_candidate")
    loc = state.get("location")

    # Resolve location
    final_loc, note = resolve_location_decision(
        coords_candidate=coords_cand,
        city_text=state.get("city_text") if not coords_cand else None,
        session_location=loc if loc and loc.source == "session" else None,
        geocoded_result={"lat": loc.lat, "lon": loc.lon, "name": loc.label} if loc and loc.source == "geocoded" else None
    )

    # Check if raw_weather was pre-injected (e.g. during mock evals)
    # None means explicitly simulate failure; dict means use the fixture
    if "raw_weather" in state and state["raw_weather"] is not None:
        return {
            "location": final_loc or loc,
            "location_note": note,
            "trace": trace
        }

    if not final_loc:
        return {
            "error_message": "No valid location to fetch weather.",
            "trace": trace
        }

    try:
        raw_weather = get_weather(final_loc.lat, final_loc.lon)
        return {
            "location": final_loc,
            "location_note": note,
            "raw_weather": raw_weather,
            "trace": trace,
        }
    except WeatherUnavailable as e:
        return {
            "error_message": str(e),
            "trace": trace,
        }


def build_facts(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["build_facts"]
    raw_w = state.get("raw_weather", {})
    time_ref = state.get("time_ref", "unspecified")
    facts = derive_facts(raw_w, time_ref=time_ref)
    return {
        "facts": facts,
        "trace": trace,
    }


def match_sops_node(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["match_sops"]
    activity = state.get("activity", "unknown")
    facts = state.get("facts", {})
    matched = match_sops(SOP_REGISTRY, activity, facts)
    return {
        "matched_sops": matched,
        "trace": trace,
    }


def resolve_conflicts_node(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["resolve_conflicts"]
    matched = state.get("matched_sops", [])
    ranking = resolve_conflicts(matched)
    return {
        "ranking_result": ranking,
        "trace": trace,
    }


def compose_reply(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["compose_reply"]
    sanitized_q = state.get("sanitized_message", "")
    ranking = state.get("ranking_result", {})
    combined_advice = ranking.get("combined_advice", "")
    loc = state.get("location")
    loc_label = loc.label if loc else "unspecified location"
    facts = state.get("facts", {})

    facts_summary = ", ".join([f"{k}: {v}" for k, v in facts.items()])

    prompt = build_compose_reply_prompt(
        sanitized_question=sanitized_q,
        sop_advice_text=combined_advice,
        location_label=loc_label,
        facts_summary=facts_summary
    )

    reply_text = complete_text(prompt)
    if not reply_text:
        # If LLM produces empty text, fallback to deterministic advice
        reply_text = combined_advice

    return {
        "reply": reply_text,
        "trace": trace,
    }


def verify_reply_node(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["verify_reply"]
    reply = state.get("reply", "")
    facts = state.get("facts", {})
    matched_tuples = state.get("matched_sops", [])
    matched_sops = [item[0] for item in matched_tuples]
    ranking = state.get("ranking_result", {})
    allowed_ids = ranking.get("all_sop_ids", [])

    is_valid, errors = check_reply(reply, facts, matched_sops, allowed_ids)
    retries = state.get("verify_retries", 0) + 1

    return {
        "verify_retries": retries,
        "verify_passed": is_valid,
        "trace": trace,
    }


def deterministic_reply(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["deterministic_reply"]
    ranking = state.get("ranking_result", {})
    combined_advice = ranking.get("combined_advice", "")
    loc = state.get("location")
    loc_label = loc.label if loc else ""
    loc_note = state.get("location_note", "")

    reply_text = f"Safety Advisory for {loc_label}:\n\n{combined_advice}"
    if loc_note:
        reply_text += f"\n\n[{loc_note}]"

    return {
        "reply": reply_text,
        "trace": trace,
    }


# Configurable System Response Templates
DEFAULT_SYSTEM_RESPONSES = {
    "out_of_scope": "I am a weather safety advisory bot. I can only answer questions regarding outdoor activity safety based on live weather policies.",
    "ask_clarification": "Please provide a location (city name or lat/lon coordinates) so I can evaluate weather safety advice for your activity.",
    "no_sop_template": "We don't have guidance for that activity under the current weather conditions in {location}.",
    "fail_location_template": "I couldn't resolve the location '{city}'. Please specify a valid city or coordinates.",
    "fail_weather": "I couldn't retrieve live weather data right now, so I can't give safety advice. Please try again shortly.",
}


def respond_no_scope(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["respond_no_scope"]
    return {
        "reply": DEFAULT_SYSTEM_RESPONSES["out_of_scope"],
        "outcome": "out_of_scope",
        "trace": trace,
    }


def ask_clarification(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["ask_clarification"]
    return {
        "reply": DEFAULT_SYSTEM_RESPONSES["ask_clarification"],
        "outcome": "clarify",
        "trace": trace,
    }


def respond_no_sop(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["respond_no_sop"]
    loc = state.get("location")
    loc_label = loc.label if loc else "your location"
    reply_text = DEFAULT_SYSTEM_RESPONSES["no_sop_template"].format(location=loc_label)
    return {
        "reply": reply_text,
        "outcome": "no_sop",
        "trace": trace,
    }


def fail_location(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["fail_location"]
    city = state.get("city_text", "specified city")
    reply_text = DEFAULT_SYSTEM_RESPONSES["fail_location_template"].format(city=city)
    return {
        "reply": reply_text,
        "outcome": "location_failed",
        "trace": trace,
    }


def fail_weather(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["fail_weather"]
    return {
        "reply": DEFAULT_SYSTEM_RESPONSES["fail_weather"],
        "outcome": "weather_failed",
        "trace": trace,
    }


def finalize(state: GraphState) -> Dict[str, Any]:
    trace = state.get("trace", []) + ["finalize"]
    outcome = state.get("outcome", "answered")

    result = {
        "outcome": outcome,
        "trace": trace,
    }
    # Preserve location and activity unmodified so session memory works naturally
    # via LangGraph's MemorySaver — the state is restored as-is on next invocation.
    loc = state.get("location")
    activity = state.get("activity")
    if loc:
        result["location"] = loc
    if activity:
        result["activity"] = activity
    return result
