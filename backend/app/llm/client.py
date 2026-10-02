"""
Gemini client.

Three call shapes:

* `complete_text`      - prose only.
* `complete_structured`- JSON against a Pydantic model.
* `ToolTurn` / `run_tool_turn` - the agent loop: hand the model the tool
  declarations, let it request tool calls, execute them, feed the results
  back, repeat.

The tool loop runs with Gemini's automatic function calling disabled so that
argument validation, ref minting and state writes all stay on this side of the
boundary. There is no client-owned fallback classifier: when the model is
unreachable the caller is told so and takes the deterministic path.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-flash-latest"


class LLMUnavailable(Exception):
    """Raised when the model cannot be reached. Callers fall back deterministically."""


def _get_client() -> Optional[genai.Client]:
    api_key = (
        GEMINI_API_KEY.strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as exc:
        logger.warning("[Gemini] Client init failed: %s", exc)
        return None


def is_available() -> bool:
    return _get_client() is not None


def get_active_model() -> str:
    """Active model id. `LLM_MODEL` may be namespaced as `models/<id>`."""
    if LLM_MODEL:
        return LLM_MODEL.split("/")[-1]
    return DEFAULT_MODEL


def _timeout_ms() -> int:
    return int(LLM_TIMEOUT * 1000)


def _http_options() -> types.HttpOptions:
    return types.HttpOptions(timeout=_timeout_ms())


# --- Prose ---

def complete_text(prompt: str, system_prompt: str = "") -> str:
    """
    Plain-text completion. Returns "" if the model is unreachable, so callers
    can substitute their own deterministic text.
    """
    client = _get_client()
    if not client:
        logger.warning("[Gemini] No API key configured.")
        return ""

    try:
        response = client.models.generate_content(
            model=get_active_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=LLM_TEMPERATURE,
                system_instruction=system_prompt or None,
                http_options=_http_options(),
            ),
        )
        return (response.text or "").strip()
    except Exception as exc:
        logger.warning("[Gemini] Text completion failed: %s", exc)
        return ""


# --- Structured ---

def complete_structured(prompt: str, response_model: Type[T]) -> Optional[T]:
    """
    Structured completion against a Pydantic model.

    Returns None on failure rather than a heuristic guess, so a caller can tell
    "the model said this" apart from "we made this up".
    """
    client = _get_client()
    if not client:
        logger.warning("[Gemini] No API key configured.")
        return None

    schema_json = json.dumps(response_model.model_json_schema(), indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond ONLY with a valid JSON object strictly matching this JSON Schema.\n"
        "Do NOT include markdown fences or extra text.\n"
        f"Schema:\n{schema_json}"
    )

    try:
        response = client.models.generate_content(
            model=get_active_model(),
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=LLM_TEMPERATURE,
                response_mime_type="application/json",
                http_options=_http_options(),
            ),
        )
        data = json.loads(_strip_markdown(response.text or ""))
        return response_model.model_validate(data)
    except Exception as exc:
        logger.warning("[Gemini] Structured completion failed: %s", exc)
        return None


# --- Tool-calling turn ---

class ToolTurn:
    """
    One exchange with the model.

    Exactly one of `text` / `function_calls` is populated: a turn either asks for
    tools or produces an answer.
    """

    __slots__ = ("text", "function_calls")

    def __init__(
        self,
        text: str = "",
        function_calls: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
    ):
        self.text = text
        self.function_calls = list(function_calls or [])

    @property
    def wants_tools(self) -> bool:
        return bool(self.function_calls)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.wants_tools:
            names = [name for name, _ in self.function_calls]
            return f"ToolTurn(function_calls={names})"
        return f"ToolTurn(text={self.text[:60]!r})"


def run_tool_turn(
    system_prompt: str,
    history: List[Any],
    tools: Optional[types.Tool] = None,
    temperature: Optional[float] = None,
) -> ToolTurn:
    """
    Ask the model for its next move.

    `history` is the full turn list, ending with the user's message. Tool
    results are already in it as function-response parts.

    Raises LLMUnavailable when the model cannot be reached, so the caller can
    abandon the agent and take the deterministic path.
    """
    client = _get_client()
    if not client:
        raise LLMUnavailable("No Gemini API key configured.")

    config = types.GenerateContentConfig(
        temperature=LLM_TEMPERATURE if temperature is None else temperature,
        system_instruction=system_prompt or None,
        tools=[tools] if tools is not None else None,
        # We execute tools ourselves so their arguments are validated and their
        # results are written into graph state.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        http_options=_http_options(),
    )

    try:
        response = client.models.generate_content(
            model=get_active_model(),
            contents=history,
            config=config,
        )
    except Exception as exc:
        raise LLMUnavailable(f"Gemini request failed: {exc}") from exc

    calls: List[Tuple[str, Dict[str, Any]]] = []
    for call in getattr(response, "function_calls", None) or []:
        args = call.args if isinstance(call.args, dict) else {}
        calls.append((call.name, dict(args or {})))

    if calls:
        return ToolTurn(function_calls=calls)

    return ToolTurn(text=(response.text or "").strip())


def append_model_turn(history: List[Any], turn: ToolTurn) -> None:
    """Record what the model said so the next turn has the full context."""
    parts: List[types.Part] = []
    if turn.text:
        parts.append(types.Part(text=turn.text))
    for name, args in turn.function_calls:
        parts.append(types.Part(function_call=types.FunctionCall(name=name, args=args)))
    history.append(types.Content(role="model", parts=parts))


def append_function_responses(
    history: List[Any],
    responses: List[Tuple[str, Dict[str, Any]]],
) -> None:
    """Append one function-response content per (name, payload) pair."""
    for name, payload in responses:
        history.append(
            types.Content(
                role="function",
                parts=[types.Part.from_function_response(name=name, response=payload)],
            )
        )


def user_turn(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()