"""
Tool layer tests.

Covers the schema inliner, argument validation and clamping, ref minting, and
the two invariants that matter most: the model cannot write facts, and the
model cannot introduce policy that did not match.
"""
import json

import pytest

from app import policy_config
from pydantic import ValidationError

from app.agent.schemas import SearchLocationArgs
from app.agent.tools import (
    EndTurnSignal,
    ToolContext,
    build_gemini_tools,
    build_tools,
    dispatch,
)
from app.config import SOPS_DIR
from app.llm.schema_utils import inline_schema
from app.tools.sop_loader import load_sops

from conftest import fake_forecast, fake_geocode

REGISTRY = load_sops(SOPS_DIR)

BOPAL = {
    "bhopal": [
        {
            "name": "Bhopal",
            "admin1": "Madhya Pradesh",
            "country": "India",
            "latitude": 23.2599,
            "longitude": 77.4126,
        }
    ],
    "springfield": [
        {
            "name": "Springfield",
            "admin1": "Illinois",
            "country": "United States",
            "latitude": 39.799,
            "longitude": -89.644,
        },
        {
            "name": "Springfield",
            "admin1": "Missouri",
            "country": "United States",
            "latitude": 37.2154,
            "longitude": -93.2983,
        },
    ],
}


def _fixture(name: str) -> dict:
    from app.config import BASE_DIR

    with open(BASE_DIR / "backend" / "evals" / "fixtures" / name, encoding="utf-8") as f:
        return json.load(f)


def _tool(name: str):
    return {tool.name: tool for tool in build_tools()}[name]


# --- Schema inliner ---

def test_inliner_resolves_refs_and_drops_unsupported_keywords():
    schema = {
        "$defs": {
            "Inner": {"type": "object", "properties": {"x": {"type": "integer"}}}
        },
        "type": "object",
        "title": "Root",
        "properties": {
            "inner": {"$ref": "#/$defs/Inner"},
            "note": {"type": "string", "default": None, "minLength": 2},
        },
        "required": ["inner"],
    }

    result = inline_schema(schema)

    assert "$defs" not in result and "$ref" not in result
    assert "title" not in result and "default" not in result
    assert "minLength" not in result
    assert result["properties"]["inner"]["properties"]["x"]["type"] == "integer"
    assert result["required"] == ["inner"]


def test_inliner_collapses_optional_to_nullable():
    schema = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    result = inline_schema(schema)
    assert result["properties"]["x"] == {"type": "string", "nullable": True}


def test_inliner_keeps_real_unions():
    schema = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    result = inline_schema(schema)
    assert len(result["properties"]["x"]["anyOf"]) == 2


def test_inliner_survives_a_dangling_ref():
    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}}
    assert inline_schema(schema)["properties"]["x"] == {"type": "object"}


def test_inliner_survives_recursive_models():
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    result = inline_schema(schema)
    assert result["properties"]["root"]["type"] == "object"


@pytest.mark.parametrize("tool", build_tools(), ids=lambda t: t.name)
def test_every_tool_declares_a_gemini_safe_schema(tool):
    declaration = tool.declaration()
    serialised = json.dumps(declaration.parameters_json_schema)
    for banned in ("$ref", "$defs", "anyOf", "title"):
        assert banned not in serialised, f"{tool.name} leaked {banned}"
    assert declaration.parameters_json_schema.get("type") == "object"


def test_build_gemini_tools_bundles_every_declaration():
    tool = build_gemini_tools()
    names = {d.name for d in tool.function_declarations}
    assert names == {
        "search_location",
        "get_forecast",
        "evaluate_policies",
        "get_policy_catalog",
        "end_turn",
    }


# --- Argument validation ---

def test_search_limit_bounds_are_enforced_twice():
    """The schema rejects outright; the config clamp is an independent net."""
    with pytest.raises(ValidationError):
        SearchLocationArgs(query="Bhopal", limit=99)

    assert SearchLocationArgs(query="Bhopal", limit=5).limit == 5
    assert policy_config.clamp_geocode_limit(99) == policy_config.geocode_config()["max_results"]
    assert policy_config.clamp_geocode_limit(0) == 1
    assert policy_config.clamp_geocode_limit(None) == policy_config.geocode_config()["default_results"]


def test_forecast_days_are_clamped_to_the_configured_range():
    assert policy_config.clamp_forecast_days(999) == policy_config.forecast_days_bounds()[1]
    assert policy_config.clamp_forecast_days(-5) == policy_config.forecast_days_bounds()[0]
    assert policy_config.clamp_forecast_days(None) == 2


def test_dispatch_rejects_out_of_range_arguments_without_calling_the_handler(monkeypatch):
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(_tool("search_location"), {"query": "Bhopal", "limit": 500}, ctx)
    assert result.is_error
    assert "rejected the arguments" in result.payload["error"]


def test_dispatch_reports_a_missing_required_field(monkeypatch):
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(_tool("search_location"), {}, ctx)
    assert result.is_error
    assert "query" in result.payload["error"]


def test_unknown_tool_is_rejected_by_the_registry(monkeypatch):
    assert "query_the_web" not in {t.name for t in build_tools()}


# --- search_location ---

def test_search_location_mints_a_ref_and_records_the_location(monkeypatch):
    fake_geocode(monkeypatch, BOPAL)
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(_tool("search_location"), {"query": "Bhopal"}, ctx)

    assert not result.is_error
    candidate = result.payload["candidates"][0]
    assert candidate["ref"] in ctx.locations
    assert ctx.locations[candidate["ref"]].label.startswith("Bhopal")
    assert result.updates["location"].source == "geocoded"


def test_search_location_returns_all_candidates_for_the_model_to_choose(monkeypatch):
    fake_geocode(monkeypatch, BOPAL)
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(_tool("search_location"), {"query": "Springfield"}, ctx)

    labels = [c["label"] for c in result.payload["candidates"]]
    assert len(labels) == 2
    assert any("Illinois" in label for label in labels)
    assert any("Missouri" in label for label in labels)


def test_search_location_reports_an_unresolvable_place(monkeypatch):
    fake_geocode(monkeypatch, BOPAL)
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(_tool("search_location"), {"query": "Atlantis"}, ctx)

    assert result.is_error
    assert "Atlantis" in result.payload["error"]
    assert "hint" in result.payload


# --- get_forecast ---

def test_get_forecast_returns_facts_the_model_cannot_edit(monkeypatch):
    fake_geocode(monkeypatch, BOPAL)
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))

    ctx = ToolContext(registry=REGISTRY)
    dispatch(_tool("search_location"), {"query": "Bhopal"}, ctx)
    ref = list(ctx.locations)[0]

    result = dispatch(
        _tool("get_forecast"), {"location_ref": ref, "time_window": "today"}, ctx
    )

    assert not result.is_error
    assert result.payload["forecast_ref"] in ctx.forecasts
    assert result.payload["facts"]["wind_kmh"] == 12.0
    assert "sustained wind speed: 12.0 km/h" in result.payload["facts_summary"]

    # The facts live in the server-side bundle, and that is what evaluate reads.
    bundle = ctx.forecasts[result.payload["forecast_ref"]]
    assert bundle.facts == result.payload["facts"]


def test_get_forecast_rejects_an_unknown_ref(monkeypatch):
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(_tool("get_forecast"), {"location_ref": "loc_made_up"}, ctx)

    assert result.is_error
    assert "loc_made_up" in result.payload["error"]


def test_get_forecast_rejects_an_unknown_window(monkeypatch):
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(
        _tool("get_forecast"),
        {"latitude": 23.26, "longitude": 77.41, "time_window": "next_decade"},
        ctx,
    )

    assert result.is_error
    assert "next_decade" in result.payload["error"]


def test_get_forecast_requires_both_bounds_for_a_custom_window(monkeypatch):
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(
        _tool("get_forecast"),
        {"latitude": 23.26, "longitude": 77.41, "time_window": "custom"},
        ctx,
    )

    assert result.is_error
    assert "start_time" in result.payload["error"]


def test_get_forecast_falls_back_to_the_session_location(monkeypatch):
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))
    from app.schemas import LocationModel

    ctx = ToolContext(registry=REGISTRY)
    ctx.session_location = LocationModel(
        lat=23.2599, lon=77.4126, label="Bhopal, India", source="session"
    )

    result = dispatch(_tool("get_forecast"), {"time_window": "today"}, ctx)

    assert not result.is_error
    assert result.payload["location"] == "Bhopal, India"


def test_get_forecast_reports_a_weather_outage(monkeypatch):
    from app.agent import tools as agent_tools
    from app.tools import weather as weather_module

    def boom(*args, **kwargs):
        raise weather_module.WeatherUnavailable("forecast service down")

    monkeypatch.setattr(agent_tools, "get_weather", boom)
    ctx = ToolContext(registry=REGISTRY)

    result = dispatch(
        _tool("get_forecast"), {"latitude": 23.26, "longitude": 77.41}, ctx
    )

    assert result.is_error
    assert "down" in result.payload["error"]


# --- evaluate_policies ---

def _with_forecast(monkeypatch) -> tuple:
    """A ToolContext that already holds one forecast, and its ref."""
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))
    ctx = ToolContext(registry=REGISTRY)
    dispatch(_tool("get_forecast"), {"latitude": 23.2599, "longitude": 77.4126}, ctx)
    return ctx, list(ctx.forecasts)[0]


def test_evaluate_policies_returns_ranked_matches_with_reasons(monkeypatch):
    ctx, ref = _with_forecast(monkeypatch)

    result = dispatch(
        _tool("evaluate_policies"), {"forecast_ref": ref, "activity": "cycling"}, ctx
    )

    assert not result.is_error
    assert result.payload["primary_sop_id"] == "EXE-MILD-CONDITIONS-01"
    matched = result.payload["matched"][0]
    assert matched["why"], "a match must explain which condition triggered it"
    assert any("temp_c" in reason for reason in matched["why"])


def test_evaluate_policies_rejects_an_unknown_activity(monkeypatch):
    ctx, ref = _with_forecast(monkeypatch)

    result = dispatch(
        _tool("evaluate_policies"), {"forecast_ref": ref, "activity": "yoga"}, ctx
    )

    assert result.is_error
    assert "yoga" in result.payload["error"]
    assert "cycling" in result.payload["available_activities"]


def test_evaluate_policies_cannot_introduce_an_unmatched_policy(monkeypatch):
    """include_ids narrows; it cannot make a non-matching SOP appear."""
    ctx, ref = _with_forecast(monkeypatch)

    result = dispatch(
        _tool("evaluate_policies"),
        {"forecast_ref": ref, "activity": "cycling", "include_ids": ["SIT-RAIN-SYSTEM-01"]},
        ctx,
    )

    assert not result.is_error
    assert result.payload["matched"] == []
    assert "SIT-RAIN-SYSTEM-01" not in result.payload["all_sop_ids"]


def test_evaluate_policies_rejects_a_forged_forecast_ref(monkeypatch):
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(
        _tool("evaluate_policies"),
        {"forecast_ref": "fc_invented", "activity": "cycling"},
        ctx,
    )
    assert result.is_error
    assert "fc_invented" in result.payload["error"]


# --- end_turn ---

def test_end_turn_halts_the_run_and_reports_the_outcome():
    ctx = ToolContext(registry=REGISTRY)
    with pytest.raises(EndTurnSignal) as caught:
        dispatch(
            _tool("end_turn"),
            {"outcome": "clarify", "message": "Which city are you in?"},
            ctx,
        )
    assert caught.value.outcome == "clarify"
    assert caught.value.updates["outcome"] == "clarify"


def test_end_turn_rejects_an_outcome_outside_the_enum():
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(_tool("end_turn"), {"outcome": "vibes", "message": "hi"}, ctx)
    assert result.is_error
    assert "outcome" in result.payload["error"]


def test_end_turn_requires_a_non_empty_message():
    ctx = ToolContext(registry=REGISTRY)
    result = dispatch(_tool("end_turn"), {"outcome": "answered", "message": "   "}, ctx)
    assert result.is_error
    assert "empty" in result.payload["error"]