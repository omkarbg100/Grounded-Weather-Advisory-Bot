"""
Test doubles.

The agent loop is only as trustworthy as its tests, and a live Gemini call is
neither free nor deterministic. `ScriptedModel` replays a fixed sequence of
turns (tool calls, then text) so tests assert on the exact tool sequence and
the resulting state without touching the network.

Refs are minted server-side, so a script cannot hard-code them. `Ref("...")`
stands in for a value taken from an earlier tool result and is resolved at call
time from what the dispatcher actually returned.
"""
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.llm import client as llm_client


class Ref:
    """
    A placeholder for a value the server minted, e.g. Ref("get_forecast",
    "forecast_ref"). Resolved from the recorded tool payloads at call time.
    """

    __slots__ = ("tool", "path")

    def __init__(self, tool: str, *path: Any):
        self.tool = tool
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Ref({self.tool}, {self.path})"


def _resolve(value: Any, payloads: List[Tuple[str, Dict[str, Any]]]) -> Any:
    if isinstance(value, Ref):
        for tool, payload in reversed(payloads):
            if tool != value.tool:
                continue
            found: Any = payload
            for key in value.path:
                if isinstance(key, int):
                    found = found[key]
                else:
                    found = (found or {}).get(key)
            return found
        raise AssertionError(f"No recorded result for {value.tool} in tool history")

    if isinstance(value, dict):
        return {k: _resolve(v, payloads) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, payloads) for v in value]
    return value


class ScriptedTurn:
    """One model turn: either tool calls, or final text."""

    def __init__(
        self,
        calls: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        text: str = "",
    ):
        self.calls = list(calls or [])
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.calls:
            return f"ScriptedTurn(calls={[c[0] for c in self.calls]})"
        return f"ScriptedTurn(text={self.text[:40]!r})"


class ScriptedModel:
    """
    Replays a fixed list of turns.

    Every turn is consumed in order. When the script runs out the model raises
    LLMUnavailable, the same signal a real outage produces, so the deterministic
    fallback path gets exercised too.
    """

    def __init__(self, script: List[ScriptedTurn]):
        self.script = list(script)
        self.index = 0
        self.payloads: List[Tuple[str, Dict[str, Any]]] = []
        self.seen_system_prompts: List[str] = []

        # What the repair_reply node's free-text re-prompt should return.
        # None means "no usable repair", which drives the graph to the
        # deterministic fallback; set it to test a successful repair.
        self.repair_text: Optional[str] = None
        self.repair_prompts: List[str] = []

    @property
    def exhausted(self) -> bool:
        return self.index >= len(self.script)

    def record(self, tool_name: str, payload: Dict[str, Any]) -> None:
        self.payloads.append((tool_name, payload))

    def __call__(self, system_prompt, history, tools=None, temperature=None):
        self.seen_system_prompts.append(system_prompt)
        if self.exhausted:
            raise llm_client.LLMUnavailable("ScriptedModel exhausted")

        turn = self.script[self.index]
        self.index += 1

        if not turn.calls:
            return llm_client.ToolTurn(text=turn.text)

        resolved = [
            (name, _resolve(args, self.payloads)) for name, args in turn.calls
        ]
        return llm_client.ToolTurn(function_calls=resolved)


@pytest.fixture
def scripted_model(monkeypatch):
    """
    Install a ScriptedModel in place of the real Gemini client.

    Patches the names the agent loop imported directly, not the client module,
    so the substitution actually takes effect.
    """
    def install(script: List[ScriptedTurn]) -> ScriptedModel:
        from app.agent import loop as agent_loop
        from app.agent import tools as agent_tools
        from app.graph import nodes as graph_nodes

        model = ScriptedModel(script)
        real_dispatch = agent_tools.dispatch

        def recording_dispatch(tool, raw_args, ctx):
            result = real_dispatch(tool, raw_args, ctx)
            model.record(tool.name, result.payload)
            return result

        def scripted_complete_text(prompt, system_prompt=None, **kwargs):
            model.repair_prompts.append(prompt)
            return model.repair_text or ""

        # The loop imported these names directly, so the substitution has to
        # happen on the loop module, not the tools module.
        monkeypatch.setattr(agent_loop, "run_tool_turn", model)
        monkeypatch.setattr(agent_loop, "dispatch", recording_dispatch)
        # repair_reply calls complete_text; leave it scripted so no test
        # reaches the network.
        monkeypatch.setattr(graph_nodes, "complete_text", scripted_complete_text)
        return model

    return install


@pytest.fixture
def offline_llm(monkeypatch):
    """Force every model call to fail, as if the API were unreachable."""
    from app.agent import loop as agent_loop
    from app.graph import nodes as graph_nodes

    def unavailable(system_prompt, history, tools=None, temperature=None):
        raise llm_client.LLMUnavailable("offline (test)")

    monkeypatch.setattr(agent_loop, "run_tool_turn", unavailable)
    monkeypatch.setattr(graph_nodes, "complete_text", lambda *a, **k: "")
    monkeypatch.setattr(llm_client, "is_available", lambda: False)


@pytest.fixture(autouse=True)
def _clear_tool_caches():
    """Weather and geocode results are memoised; tests must not share them."""
    from app.tools import geocode, weather

    weather.clear_cache()
    geocode.clear_cache()
    yield
    weather.clear_cache()
    geocode.clear_cache()


def fake_geocode(monkeypatch, results: Dict[str, List[Dict[str, Any]]]):
    """Replace the geocoder with a lookup table."""
    from app.agent import tools as agent_tools
    from app.tools import geocode as geocode_module

    def search(name, country_code=None, limit=None):
        candidates = (results or {}).get(str(name).strip().lower())
        if not candidates:
            raise geocode_module.LocationNotFound(f"No location found for '{name}'.")
        return list(candidates)[: (limit or 3)]

    monkeypatch.setattr(geocode_module, "geocode_search", search)
    monkeypatch.setattr(agent_tools, "geocode_search", search)


def fake_forecast(monkeypatch, payload: Dict[str, Any]):
    """Replace the forecaster with a fixed payload."""
    from app.agent import tools as agent_tools
    from app.tools import weather as weather_module

    def get_weather(latitude, longitude, forecast_days=None, timezone=None):
        return payload

    monkeypatch.setattr(weather_module, "get_weather", get_weather)
    monkeypatch.setattr(agent_tools, "get_weather", get_weather)