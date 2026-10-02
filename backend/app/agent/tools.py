"""
The agent's tool surface.

Four tools, and the model chooses among them and supplies their arguments:

    search_location  -> candidates, each with a server-issued ref
    get_forecast     -> a forecast_ref plus the facts derived from it
    evaluate_policies-> the ranked SOP matches for a forecast_ref
    get_policy_catalog -> the SOP index, for taxonomy discovery

Two invariants hold across all four:

* **The model cannot write facts.** `evaluate_policies` reads its facts through
  a `forecast_ref` the server minted in `get_forecast`. Whatever the model
  puts in `evaluate_policies` cannot change a weather value.
* **The model cannot invent policy.** `evaluate_policies` runs the YAML rule
  engine. `include_ids` only narrows an already-matched set. The model never
  authors, ranks, or selects a SOP.

Refs are stable hashes of their inputs, so calling `get_forecast` twice for the
same place and window reuses the same reference instead of re-fetching.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from google.genai import types
from pydantic import BaseModel, ValidationError

from app import policy_config
from app.agent.schemas import (
    EndTurnArgs,
    EvaluatePoliciesArgs,
    ForecastBundle,
    GetForecastArgs,
    GetPolicyCatalogArgs,
    LocationCandidate,
    SearchLocationArgs,
    ToolError,
    ToolResult,
)
from app.engine import policies as policy_engine
from app.engine.fact_registry import describe_facts
from app.engine.location import parse_coordinates, validate_lat_lon
from app.engine.timewindow import WindowResolutionError, resolve_window
from app.llm.schema_utils import inline_schema
from app.schemas import LocationModel
from app.tools.geocode import LocationNotFound, geocode_search
from app.tools.weather import WeatherUnavailable, get_weather

logger = logging.getLogger(__name__)


def _ref(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:10]}"


class EndTurnSignal(Exception):
    """
    Raised by the `end_turn` handler to stop the agent loop.

    Carrying the terminal classification as an exception rather than a return
    value keeps the `Tool` handler signature uniform while making the halt
    impossible to ignore.
    """

    def __init__(self, outcome: str, reply: str, updates: Dict[str, Any]):
        super().__init__(f"end_turn: {outcome}")
        self.outcome = outcome
        self.reply = reply
        self.updates = updates


# --- Session-scoped resource store ---
#
# Refs minted here are the only way the model reaches engine state. The store is
# rebuilt per request from graph state, so nothing leaks between sessions.


@dataclass
class ToolContext:
    """Per-request scratch space shared by the tool handlers."""

    locations: Dict[str, LocationModel] = field(default_factory=dict)
    forecasts: Dict[str, ForecastBundle] = field(default_factory=dict)
    registry: Any = None

    # Location already established earlier in the conversation. get_forecast
    # falls back to it when the model supplies no coordinates, which is what
    # makes "what about tomorrow?" work without re-resolving the city.
    session_location: Optional[LocationModel] = None

    # When set, get_forecast returns this payload instead of calling the API.
    # This is the seam evals and tests use to pin conditions; it lives here
    # rather than inside the weather node so there is no branch in the graph
    # that only exists for tests.
    forecast_override: Optional[Dict[str, Any]] = None

    def remember_location(self, location: LocationModel) -> str:
        ref = _ref(
            "loc",
            f"{location.lat:.4f}",
            f"{location.lon:.4f}",
            location.label,
        )
        self.locations[ref] = location
        return ref

    def remember_forecast(self, bundle: ForecastBundle) -> str:
        self.forecasts[bundle.ref] = bundle
        return bundle.ref


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: Type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], ToolResult]

    def declaration(self) -> types.FunctionDeclaration:
        raw_schema = self.args_model.model_json_schema()
        description = raw_schema.get("description") or self.description
        return types.FunctionDeclaration(
            name=self.name,
            description=description,
            parameters_json_schema=inline_schema(raw_schema),
        )

    def validate(self, raw_args: Optional[Dict[str, Any]]) -> BaseModel:
        try:
            return self.args_model.model_validate(raw_args or {})
        except ValidationError as exc:
            raise ToolError(_format_validation_error(self.name, exc)) from exc


def _format_validation_error(tool_name: str, exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        problems.append(f"{location}: {error.get('msg')}")
    detail = "; ".join(problems)
    return f"{tool_name} rejected the arguments ({detail}). Correct them and retry."


# --- Handlers ---


def _search_location(args: SearchLocationArgs, ctx: ToolContext) -> ToolResult:
    try:
        candidates = geocode_search(
            args.query,
            country_code=args.country_code,
            limit=args.limit,
        )
    except LocationNotFound as exc:
        return ToolResult.fail(
            str(exc),
            hint="Ask the user which place they mean, or try a broader query.",
        )

    rendered: List[Dict[str, Any]] = []
    for candidate in candidates:
        label = ", ".join(
            part for part in (
                candidate["name"],
                candidate["admin1"],
                candidate["country"],
            ) if part
        )
        location = LocationModel(
            lat=candidate["latitude"],
            lon=candidate["longitude"],
            label=label,
            source="geocoded",
        )
        ref = ctx.remember_location(location)
        rendered.append(
            LocationCandidate(
                ref=ref,
                label=label,
                latitude=candidate["latitude"],
                longitude=candidate["longitude"],
                admin1=candidate["admin1"],
                country=candidate["country"],
            ).model_dump()
        )

    return ToolResult.ok(
        {
            "query": args.query,
            "candidates": rendered,
            "resolved": len(rendered) == 1,
        },
        location=ctx.locations[rendered[0]["ref"]],
        location_note=(
            f"Geocoded location: {rendered[0]['label']}."
            if len(rendered) == 1
            else f"Found {len(rendered)} places matching '{args.query}'; awaiting disambiguation."
        ),
    )


def _get_forecast(args: GetForecastArgs, ctx: ToolContext) -> ToolResult:
    location, note = _resolve_forecast_location(args, ctx)
    if isinstance(location, ToolResult):
        return location

    window_name = args.time_window
    if window_name not in policy_config.window_names():
        return ToolResult.fail(
            f"Unknown time_window '{window_name}'. Use one of "
            f"{policy_config.window_names()}."
        )
    if args.start_time and args.end_time:
        window_name = "custom"
    elif window_name == "custom":
        return ToolResult.fail(
            "time_window='custom' requires both start_time and end_time in ISO-8601."
        )

    days = policy_config.clamp_forecast_days(args.forecast_days)

    if ctx.forecast_override is not None:
        raw = ctx.forecast_override
    else:
        try:
            raw = get_weather(location.lat, location.lon, forecast_days=days)
        except WeatherUnavailable as exc:
            return ToolResult.fail(
                str(exc),
                hint="Tell the user live weather is unavailable and offer to retry.",
            )

    from app.engine.facts import derive_facts  # local import avoids a cycle at module load

    try:
        window = resolve_window(
            list((raw.get("hourly") or {}).get("time") or []),
            window_name,
            args.start_time,
            args.end_time,
        )
    except WindowResolutionError as exc:
        return ToolResult.fail(str(exc))

    facts = derive_facts(raw, window=window)

    bundle = ForecastBundle(
        ref=_ref("fc", f"{location.lat:.4f}", f"{location.lon:.4f}", window_name,
                window.start_index, window.end_index, days),
        location_label=location.label,
        latitude=location.lat,
        longitude=location.lon,
        window=window.as_dict(),
        facts=facts,
        forecast_days=days,
    )
    ref = ctx.remember_forecast(bundle)

    return ToolResult.ok(
        {
            "forecast_ref": ref,
            "location": location.label,
            "coordinates": {"latitude": location.lat, "longitude": location.lon},
            "window": bundle.window,
            "facts": facts,
            "facts_summary": describe_facts(facts),
        },
        location=location,
        location_note=note,
        forecast_ref=ref,
        raw_weather=raw,
        facts=facts,
        time_window=window_name,
    )


def _resolve_forecast_location(
    args: GetForecastArgs,
    ctx: ToolContext,
):
    """Returns (LocationModel, note) or a ToolResult describing the failure."""

    if args.location_ref:
        location = ctx.locations.get(args.location_ref)
        if location is None:
            return (
                ToolResult.fail(
                    f"Unknown location_ref '{args.location_ref}'.",
                    hint="Call search_location first and use a ref it returned.",
                ),
                "",
            )
        return location, f"Using resolved location: {location.label}."

    if args.latitude is None or args.longitude is None:
        if args.latitude is None and args.longitude is None:
            # No coordinates at all: reuse whatever the conversation already
            # resolved, which is how a follow-up inherits its city.
            if ctx.session_location is not None:
                return ctx.session_location, (
                    f"Reusing this conversation's location: "
                    f"{ctx.session_location.label}."
                )

        # A last-ditch parse is worth one attempt: users often paste bare
        # coordinate pairs, and the model's arguments may have dropped them.
        coerced = parse_coordinates(args.location_ref or "")
        if coerced is None:
            return (
                ToolResult.fail(
                    "Provide either location_ref, both latitude and longitude, or "
                    "ask the user where they are.",
                    hint="Use search_location to resolve a place name.",
                ),
                "",
            )
        args.latitude, args.longitude = coerced

    if not validate_lat_lon(args.latitude, args.longitude):
        return (
            ToolResult.fail(
                f"Coordinates out of range: ({args.latitude}, {args.longitude})."
            ),
            "",
        )

    location = LocationModel(
        lat=args.latitude,
        lon=args.longitude,
        label=f"Coordinates ({args.latitude:.2f}, {args.longitude:.2f})",
        source="coordinates",
    )
    note = f"Using provided coordinates ({args.latitude:.2f}, {args.longitude:.2f})."
    return location, note


def _evaluate_policies(args: EvaluatePoliciesArgs, ctx: ToolContext) -> ToolResult:
    bundle = ctx.forecasts.get(args.forecast_ref)
    if bundle is None:
        return ToolResult.fail(
            f"Unknown forecast_ref '{args.forecast_ref}'.",
            hint="Call get_forecast first and use the forecast_ref it returned.",
        )

    if ctx.registry is None:
        return ToolResult.fail("No SOP registry is loaded.", hint="This is a server error.")

    if not policy_engine.is_valid_activity(args.activity, ctx.registry):
        return ToolResult.fail(
            f"Unknown activity tag '{args.activity}'.",
            available_activities=ctx.registry.taxonomy,
            hint="Call get_policy_catalog to see valid tags, then retry.",
        )

    evaluation = policy_engine.evaluate(
        ctx.registry,
        args.activity,
        bundle.facts,
        include_ids=args.include_ids,
    )

    payload = policy_engine.to_tool_payload(evaluation)
    payload["location"] = bundle.location_label
    payload["window"] = bundle.window.get("name")

    if not evaluation:
        payload["note"] = (
            f"No published policy matched activity '{evaluation.activity}' under these "
            "conditions. Say so plainly rather than guessing."
        )

    return ToolResult.ok(
        payload,
        activity=evaluation.activity,
        matched_sops=evaluation.legacy_matched,
        ranking_result=evaluation.ranking,
    )


def _get_policy_catalog(args: GetPolicyCatalogArgs, ctx: ToolContext) -> ToolResult:
    if ctx.registry is None:
        return ToolResult.fail("No SOP registry is loaded.")

    category = (args.category or "").strip().lower() or None
    activity = (args.activity or "").strip().lower() or None

    entries = []
    for sop in ctx.registry.sops:
        if category and sop.category.lower() != category:
            continue
        if activity and "*" not in sop.applies_to and activity not in {
            tag.lower() for tag in sop.applies_to
        }:
            continue
        entries.append(
            {
                "id": sop.id,
                "title": sop.title,
                "category": sop.category,
                "severity": sop.severity,
                "applies_to": list(sop.applies_to),
            }
        )

    return ToolResult.ok(
        {
            "activities": list(ctx.registry.taxonomy),
            "categories": sorted({sop.category for sop in ctx.registry.sops}),
            "policies": entries,
        }
    )


def _end_turn(args: EndTurnArgs, ctx: ToolContext) -> ToolResult:
    """
    Record the terminal classification and the user-facing reply.

    Writes `reply` and `outcome` into state and raises `EndTurnSignal`, which the
    loop catches to stop the run.
    """
    message = args.message.strip()
    if not message:
        return ToolResult.fail("message cannot be empty.")
    raise EndTurnSignal(
        outcome=args.outcome,
        reply=message,
        updates={"reply": message, "outcome": args.outcome},
    )


# --- Registry ---


def build_tools() -> List[Tool]:
    return [
        Tool(
            name="search_location",
            description=(
                "Find the coordinates for a place the user named. Returns ranked "
                "candidates; use one of the returned refs in get_forecast. If several "
                "places match and the user's context does not disambiguate them, ask "
                "which one they mean instead of guessing."
            ),
            args_model=SearchLocationArgs,
            handler=_search_location,
        ),
        Tool(
            name="get_forecast",
            description=(
                "Retrieve live weather for a resolved location and derive the numeric "
                "facts used by the safety policies. Returns a forecast_ref that "
                "evaluate_policies reads. Choose time_window to match the user's "
                "question: 'now', 'today', 'this_evening', 'tomorrow', 'next_24h' or "
                "'next_48h'."
            ),
            args_model=GetForecastArgs,
            handler=_get_forecast,
        ),
        Tool(
            name="evaluate_policies",
            description=(
                "Check the official safety policies against a forecast. Returns every "
                "policy that matched, ranked, with the exact published advice and the "
                "conditions that triggered it. This is the only source of safety "
                "guidance; never state advice from memory."
            ),
            args_model=EvaluatePoliciesArgs,
            handler=_evaluate_policies,
        ),
        Tool(
            name="get_policy_catalog",
            description=(
                "List the published policies and the valid activity tags. Use it to "
                "pick the right activity tag for evaluate_policies, or to answer "
                "questions about what guidance exists."
            ),
            args_model=GetPolicyCatalogArgs,
            handler=_get_policy_catalog,
        ),
        Tool(
            name="end_turn",
            description=(
                "Finish and reply to the user. Call this exactly once, as your last "
                "action, with the outcome that describes why you are stopping and the "
                "message to show them."
            ),
            args_model=EndTurnArgs,
            handler=_end_turn,
        ),
    ]


def tool_names() -> List[str]:
    return [tool.name for tool in build_tools()]


def build_gemini_tools() -> types.Tool:
    """A single Gemini Tool bundling every function declaration."""
    return types.Tool(function_declarations=[tool.declaration() for tool in build_tools()])


def dispatch(
    tool: Tool,
    raw_args: Optional[Dict[str, Any]],
    ctx: ToolContext,
) -> ToolResult:
    """
    Validate arguments and run a handler.

    Argument errors and unexpected handler failures both come back as error
    results rather than exceptions, so one bad tool call does not abort the run.

    `EndTurnSignal` is deliberately not caught: it is the loop's stop condition.
    """
    try:
        args = tool.validate(raw_args)
    except ToolError as exc:
        logger.info("Tool %s rejected arguments: %s", tool.name, exc)
        return ToolResult.fail(str(exc))

    try:
        return tool.handler(args, ctx)
    except EndTurnSignal:
        raise
    except ToolError as exc:
        return ToolResult.fail(str(exc))
    except Exception as exc:  # defensive: never let a tool kill the graph
        logger.exception("Tool %s raised", tool.name)
        return ToolResult.fail(
            f"{tool.name} failed unexpectedly: {exc}",
            hint="Report the failure rather than guessing at the data.",
        )


def registry_from_state(state: Dict[str, Any], registry: Any) -> ToolContext:
    """Rebuild a ToolContext from graph state so refs survive across nodes."""
    ctx = ToolContext(registry=registry)
    for ref, location in (state.get("location_refs") or {}).items():
        if isinstance(location, LocationModel):
            ctx.locations[ref] = location
    for ref, bundle in (state.get("forecast_refs") or {}).items():
        if isinstance(bundle, ForecastBundle):
            ctx.forecasts[ref] = bundle
    ctx.forecast_override = state.get("raw_weather")
    location = state.get("location")
    if isinstance(location, LocationModel):
        ctx.session_location = location
    return ctx