from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    agent_loop,
    deterministic_reply,
    finalize,
    guard_input,
    repair_reply,
    verify_reply_node,
)
from app.graph.routing import route_after_agent, route_after_guard, route_after_verify
from app.graph.state import GraphState


def create_weather_sop_graph():
    """
    Build the advisory graph.

        guard_input -> agent_loop -> verify_reply -> finalize
                            |             |   ^
                            |             |   | (repair, once)
                            v             v   |
                       deterministic_reply <-+

    The previous graph had seventeen nodes and eleven conditional edges, all of
    which encoded policy in Python. The model now supplies tool arguments and
    the single remaining router checks only whether the reply was grounded.
    """
    builder = StateGraph(GraphState)

    builder.add_node("guard_input", guard_input)
    builder.add_node("agent_loop", agent_loop)
    builder.add_node("verify_reply", verify_reply_node)
    builder.add_node("repair_reply", repair_reply)
    builder.add_node("deterministic_reply", deterministic_reply)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "guard_input")

    builder.add_conditional_edges(
        "guard_input",
        route_after_guard,
        {"agent_loop": "agent_loop"},
    )

    builder.add_conditional_edges(
        "agent_loop",
        route_after_agent,
        {
            "verify_reply": "verify_reply",
            "deterministic_reply": "deterministic_reply",
        },
    )

    builder.add_conditional_edges(
        "verify_reply",
        route_after_verify,
        {
            "finalize": "finalize",
            "repair_reply": "repair_reply",
            "deterministic_reply": "deterministic_reply",
        },
    )

    builder.add_edge("repair_reply", "verify_reply")
    builder.add_edge("deterministic_reply", "finalize")
    builder.add_edge("finalize", END)

    # Pydantic models stored in state must survive checkpoint serialisation.
    #
    # `MemorySaver.with_allowlist` is a no-op against the default serializer:
    # the default msgpack policy is "allow everything, warn once per type", so
    # the allowlist never takes effect and a future LangGraph will start
    # blocking these types outright. Passing an explicit list to the serializer
    # constructor is what actually registers them.
    serializer = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("app.schemas", "LocationModel"),
            ("app.schemas", "SOPModel"),
            ("app.agent.schemas", "ForecastBundle"),
        ]
    )
    return builder.compile(checkpointer=MemorySaver(serde=serializer))


# Singleton compiled graph instance
graph_app = create_weather_sop_graph()