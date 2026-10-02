from typing import Literal

from app.graph.state import GraphState

# The graph has one real decision left: did the reply pass verification, and if
# not, is there repair budget. Everything that used to be a branch here (which
# location source to use, whether the message was in scope, whether a policy
# matched) is a tool argument the model supplies and the engine validates.


def route_after_guard(state: GraphState) -> Literal["agent_loop"]:
    # A flagged injection is not a request to reason about, so the model is
    # given the refusal instruction rather than the user's text. Everything else
    # goes to the loop, including greetings and out-of-scope questions: the
    # model decides those via end_turn.
    return "agent_loop"


def route_after_agent(state: GraphState) -> Literal["verify_reply", "deterministic_reply"]:
    if state.get("outcome") == "llm_unavailable":
        return "deterministic_reply"
    return "verify_reply"


def route_after_verify(
    state: GraphState,
) -> Literal["finalize", "repair_reply", "deterministic_reply"]:
    if state.get("verify_passed"):
        return "finalize"

    from app import policy_config

    attempts = int(policy_config.agent_config().get("verify_repair_attempts", 1))
    if int(state.get("verify_retries", 0)) <= attempts:
        return "repair_reply"
    return "deterministic_reply"