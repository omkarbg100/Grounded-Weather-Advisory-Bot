from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from app.graph.state import GraphState
from app.graph.nodes import (
    guard_input,
    extract_coords,
    parse_intent,
    geocode_city_node,
    fetch_weather,
    build_facts,
    match_sops_node,
    resolve_conflicts_node,
    compose_reply,
    verify_reply_node,
    deterministic_reply,
    respond_no_scope,
    ask_clarification,
    respond_no_sop,
    fail_location,
    fail_weather,
    finalize,
)
from app.graph.routing import (
    route_after_parse_intent,
    route_after_geocode,
    route_after_fetch_weather,
    route_after_match_sops,
    route_after_verify,
)


def create_weather_sop_graph():
    builder = StateGraph(GraphState)

    # Add nodes
    builder.add_node("guard_input", guard_input)
    builder.add_node("extract_coords", extract_coords)
    builder.add_node("parse_intent", parse_intent)
    builder.add_node("geocode_city", geocode_city_node)
    builder.add_node("fetch_weather", fetch_weather)
    builder.add_node("build_facts", build_facts)
    builder.add_node("match_sops", match_sops_node)
    builder.add_node("resolve_conflicts", resolve_conflicts_node)
    builder.add_node("compose_reply", compose_reply)
    builder.add_node("verify_reply", verify_reply_node)
    builder.add_node("deterministic_reply", deterministic_reply)
    builder.add_node("respond_no_scope", respond_no_scope)
    builder.add_node("ask_clarification", ask_clarification)
    builder.add_node("respond_no_sop", respond_no_sop)
    builder.add_node("fail_location", fail_location)
    builder.add_node("fail_weather", fail_weather)
    builder.add_node("finalize", finalize)

    # Flow sequence
    builder.add_edge(START, "guard_input")
    builder.add_edge("guard_input", "extract_coords")
    builder.add_edge("extract_coords", "parse_intent")

    # Conditional routing after parse_intent
    builder.add_conditional_edges(
        "parse_intent",
        route_after_parse_intent,
        {
            "respond_no_scope": "respond_no_scope",
            "fetch_weather": "fetch_weather",
            "geocode_city": "geocode_city",
            "ask_clarification": "ask_clarification",
        }
    )

    # Conditional routing after geocode
    builder.add_conditional_edges(
        "geocode_city",
        route_after_geocode,
        {
            "fetch_weather": "fetch_weather",
            "fail_location": "fail_location",
        }
    )

    # Conditional routing after fetch_weather
    builder.add_conditional_edges(
        "fetch_weather",
        route_after_fetch_weather,
        {
            "build_facts": "build_facts",
            "fail_weather": "fail_weather",
        }
    )

    builder.add_edge("build_facts", "match_sops")

    # Conditional routing after match_sops
    builder.add_conditional_edges(
        "match_sops",
        route_after_match_sops,
        {
            "resolve_conflicts": "resolve_conflicts",
            "respond_no_sop": "respond_no_sop",
        }
    )

    builder.add_edge("resolve_conflicts", "compose_reply")
    builder.add_edge("compose_reply", "verify_reply")

    # Conditional routing after verify_reply
    builder.add_conditional_edges(
        "verify_reply",
        route_after_verify,
        {
            "finalize": "finalize",
            "compose_reply": "compose_reply",
            "deterministic_reply": "deterministic_reply",
        }
    )

    builder.add_edge("deterministic_reply", "finalize")
    builder.add_edge("respond_no_scope", END)
    builder.add_edge("ask_clarification", END)
    builder.add_edge("respond_no_sop", END)
    builder.add_edge("fail_location", END)
    builder.add_edge("fail_weather", END)
    builder.add_edge("finalize", END)

    # Memory Saver Checkpointer for thread session memory
    checkpointer = MemorySaver().with_allowlist([
        ("app.schemas", "IntentParseResult"),
        ("app.schemas", "LocationModel"),
        ("app.schemas", "SOPModel"),
    ])
    app = builder.compile(checkpointer=checkpointer)
    return app


# Singleton compiled graph instance
graph_app = create_weather_sop_graph()
