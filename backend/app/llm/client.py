"""
Gemini Direct LLM Client.

Uses official google-genai SDK directly with GEMINI_API_KEY (no gateways or proxies).
Falls back to deterministic keyword heuristics if offline or quota exceeded.
"""
import json
import logging
import os
import re
from typing import Type, TypeVar, Optional
from pydantic import BaseModel
from google import genai
from google.genai import types

from app.config import (
    GEMINI_API_KEY,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-flash-latest"


def _get_client() -> Optional[genai.Client]:
    api_key = GEMINI_API_KEY.strip() or os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.warning(f"[Gemini] Client init failed: {e}")
        return None


def get_active_model() -> str:
    """Return active Gemini model identifier."""
    if LLM_MODEL:
        return LLM_MODEL.split("/")[-1]
    return DEFAULT_MODEL


def complete_structured(prompt: str, response_model: Type[T]) -> T:
    """
    Produce structured Pydantic object using direct Gemini API.
    Falls back to deterministic keyword heuristics if API call fails.
    """
    client = _get_client()
    if not client:
        logger.warning("[Gemini] No API key available. Using deterministic fallback.")
        return _fallback_structured(prompt, response_model)

    model = get_active_model()
    schema_json = json.dumps(response_model.model_json_schema(), indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond ONLY with a valid JSON object strictly matching this JSON Schema.\n"
        "Do NOT include markdown fences or extra text.\n"
        f"Schema:\n{schema_json}"
    )

    try:
        timeout_ms = int(LLM_TIMEOUT * 1000)
        config = types.GenerateContentConfig(
            temperature=LLM_TEMPERATURE,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        response = client.models.generate_content(
            model=model,
            contents=full_prompt,
            config=config,
        )
        text = response.text or ""
        clean_text = _strip_markdown(text)
        data = json.loads(clean_text)
        return response_model.model_validate(data)
    except Exception as e:
        logger.warning(f"[Gemini] Structured completion failed: {e}. Using deterministic fallback.")
        return _fallback_structured(prompt, response_model)


def complete_text(prompt: str, system_prompt: str = "") -> str:
    """
    Produce plain-text completion using direct Gemini API.
    """
    client = _get_client()
    if not client:
        return ""

    model = get_active_model()
    try:
        timeout_ms = int(LLM_TIMEOUT * 1000)
        config = types.GenerateContentConfig(
            temperature=LLM_TEMPERATURE,
            system_instruction=system_prompt if system_prompt else None,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.warning(f"[Gemini] Text completion failed: {e}.")
        return ""


def _strip_markdown(text: str) -> str:
    """Remove ```json ... ``` fences from output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _fallback_structured(prompt: str, response_model: Type[T]) -> T:
    """
    Deterministic keyword-heuristic fallback when LLM is offline or quota exceeded.
    Ensures graph routing and deterministic SOP matching remain 100% functional.
    """
    user_msg = prompt
    if 'User message: "' in prompt:
        try:
            user_msg = prompt.split('User message: "')[1].split('"')[0]
        except IndexError:
            pass

    session_act = _extract_quoted(prompt, "reuse session activity '")
    session_city = _extract_quoted(prompt, "reuse session city '")

    msg_lower = user_msg.lower()
    words = set(msg_lower.split())

    # 1. Intent type
    intent_type = "advice"
    if words & {"hi", "hello", "hey", "greetings"}:
        intent_type = "smalltalk"
    elif words & {"movie", "recipe", "crypto", "stock", "stocks", "recipes", "weather"}:
        intent_type = "out_of_scope"
    elif "what about" in msg_lower or "evening" in msg_lower or "this morning" in msg_lower:
        intent_type = "followup"

    # 2. Activity
    activity_map = [
        (["cycl", "pedal", "bike", "bicycle"],              "cycling"),
        (["run", "jog"],                                     "running"),
        (["picnic"],                                         "picnic"),
        (["kid", "toddler", "child", "children"],           "children_outdoors"),
        (["elderly", "senior"],                              "elderly_outdoors"),
        (["dog", "pet", "puppy", "pup"],                    "dog_walk"),
        (["drive", "driving", "trip"],                       "travel"),
        (["commute", "office"],                              "commute"),
        (["walk"],                                           "walking"),
    ]
    activity = "unknown"
    for keywords, act in activity_map:
        if any(kw in msg_lower for kw in keywords):
            activity = act
            break

    if activity == "unknown" and session_act and session_act not in ("unknown", "None"):
        activity = session_act

    # 3. City text
    city_text: Optional[str] = None
    city_match = re.search(r"\bin\s+([A-Z][a-z]{2,})", user_msg)
    if city_match:
        city_text = city_match.group(1)
    elif not city_text and session_city and session_city not in ("None", "") and not session_city.startswith("Coordinates"):
        city_text = session_city

    # 4. Time reference
    time_ref = "unspecified"
    if "evening" in msg_lower or "tonight" in msg_lower or "night" in msg_lower:
        time_ref = "this_evening"
    elif "tomorrow" in msg_lower:
        time_ref = "tomorrow"
    elif "today" in msg_lower or "now" in msg_lower:
        time_ref = "today"

    return response_model.model_validate({
        "intent_type": intent_type,
        "activity": activity,
        "city_text": city_text,
        "time_ref": time_ref,
    })


def _extract_quoted(text: str, marker: str) -> Optional[str]:
    if marker in text:
        try:
            return text.split(marker)[1].split("'")[0]
        except IndexError:
            pass
    return None
