"""
Golden evaluation harness.

Runs the scenarios in `cases.yaml` through the real graph and asserts the
invariants that must hold no matter what the model does:

  * every cited SOP id exists in the registry
  * every cited SOP id actually matched the facts (no invented policy)
  * every number in the reply is supported by the facts or the cited advice
  * a refusal outcome carries no numbers at all
  * an injection attempt never yields the forbidden ids

Two modes:

  live    the model drives the tool loop; this is the real measurement
  offline no API key; the deterministic fallback answers instead

The mode is recorded in the report rather than hidden. An offline run
exercises policy matching and verification but proves nothing about the
model, and the report says so.
"""
import json
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest import mock

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent import tools as agent_tools
from app.config import SOPS_DIR
from app.engine.verifier import (
    SOP_ID_REGEX,
    extract_numbers_from_text,
    verify_reply,
)
from app.graph.builder import create_weather_sop_graph
from app.tools.geocode import LocationNotFound
from app.tools.sop_loader import load_sops
from app.tools.weather import WeatherUnavailable

REGISTRY = load_sops(SOPS_DIR)
REGISTRY_IDS = {s.id for s in REGISTRY.sops}

RUNS_PER_CASE = 3


def load_fixture(name: str) -> Dict[str, Any]:
    path = BACKEND_DIR / "evals" / "fixtures" / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture file missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cases() -> List[Dict[str, Any]]:
    with open(BACKEND_DIR / "evals" / "cases.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def llm_available() -> bool:
    from app.llm import client as llm_client

    try:
        return bool(llm_client.is_available())
    except Exception:
        return False


# --- Per-case environment ---

@contextmanager
def offline_llm() -> Iterator[None]:
    """Make every model call fail, as an unreachable API would.

    Needed because an API key can be present while the quota is exhausted, in
    which case `is_available()` is true but nothing can succeed. Patching the
    loop is what actually stops the network calls.
    """
    from app.agent import loop as agent_loop
    from app.llm import client as llm_client

    def unreachable(system_prompt, history, tools=None, temperature=None):
        raise llm_client.LLMUnavailable("offline eval")

    with mock.patch.object(agent_loop, "run_tool_turn", unreachable):
        yield


@contextmanager
def case_environment(
    case: Dict[str, Any], fixture: Optional[Dict[str, Any]], live: bool
) -> Iterator[None]:
    """Point the tool layer at fixtures or at simulated outages."""
    with ExitStack() as stack:
        if not live:
            stack.enter_context(offline_llm())

        if case.get("mock_weather_fail"):
            def boom(*args, **kwargs):
                raise WeatherUnavailable("Simulated weather API outage.")

            stack.enter_context(
                mock.patch.object(agent_tools, "get_weather", boom)
            )

        if case.get("mock_geocode_empty"):
            def no_city(name, country_code=None, limit=None):
                raise LocationNotFound(f"No location found for '{name}'.")

            stack.enter_context(
                mock.patch.object(agent_tools, "geocode_search", no_city)
            )

        if fixture is not None:
            stack.enter_context(
                mock.patch.object(agent_tools, "get_weather", lambda *a, **k: fixture)
            )
            stack.enter_context(
                mock.patch.object(
                    agent_tools,
                    "geocode_search",
                    lambda name, country_code=None, limit=None: [
                        {
                            "name": "Bhopal",
                            "admin1": "Madhya Pradesh",
                            "country": "India",
                            "latitude": 23.2599,
                            "longitude": 77.4126,
                        }
                    ],
                )
            )
        yield


# --- Result ---

@dataclass
class CaseResult:
    name: str
    what_we_check: str
    pass_criteria: str
    passes: int = 0
    skipped: bool = False
    notes: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    fallback_runs: int = 0

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.passes == RUNS_PER_CASE:
            return "PASS"
        return "PARTIAL" if self.passes else "FAIL"


def check_case(case: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    """Return a list of invariant violations; empty means the run passed."""
    failures: List[str] = []
    reply = state.get("reply") or ""
    outcome = state.get("outcome") or ""
    expected = case.get("expected") or {}
    ranking = state.get("ranking_result") or {}
    matched_ids = set(ranking.get("all_sop_ids") or [])
    cited = set(SOP_ID_REGEX.findall(reply))

    if not reply.strip():
        failures.append("empty reply")

    # 1. Every cited id exists.
    invented = cited - REGISTRY_IDS
    if invented:
        failures.append(f"cited non-existent SOP id(s): {sorted(invented)}")

    # 2. Every cited id actually matched.
    unmatched_citations = cited - matched_ids
    if unmatched_citations:
        failures.append(f"cited SOP id(s) that did not match: {sorted(unmatched_citations)}")

    # 3. Forbidden ids never appear.
    for forbidden in expected.get("forbidden_ids") or []:
        if forbidden in reply or forbidden in " ".join(sorted(matched_ids)):
            failures.append(f"leaked forbidden id: {forbidden}")

    # 4. Numbers are supported by the facts or the published advice.
    facts = state.get("facts") or {}
    matched_sops = [item[0] for item in (state.get("matched_sops") or [])]
    is_valid, errors = verify_reply(
        reply=reply,
        facts=facts,
        matched_sops=matched_sops,
        allowed_sop_ids=sorted(matched_ids),
        rendered_advice=[ranking.get("combined_advice")] if ranking.get("combined_advice") else [],
    )
    if not is_valid:
        failures.append(f"verifier rejected reply: {errors}")

    # 5. A refusal carries no numbers.
    if outcome in {"weather_failed", "location_failed", "llm_unavailable", "unavailable"}:
        if extract_numbers_from_text(reply):
            failures.append("honest-failure reply contains numbers")

    # 6. Expected outcome, where the fixture makes it deterministic.
    if "outcome" in expected and outcome != expected["outcome"]:
        failures.append(f"outcome {outcome!r}, expected {expected['outcome']!r}")

    if "sop_ids" in expected:
        for wanted in expected["sop_ids"]:
            if wanted not in matched_ids:
                failures.append(f"expected SOP {wanted} did not match")

    if "location_source" in expected:
        location = state.get("location")
        actual = location.source if location else None
        if actual != expected["location_source"]:
            failures.append(f"location source {actual!r}, expected {expected['location_source']!r}")

    return failures


def run_case(case: Dict[str, Any], live: bool) -> CaseResult:
    result = CaseResult(
        name=case["name"],
        what_we_check=case.get("what_we_check", ""),
        pass_criteria=case.get("pass_criteria", ""),
    )

    if case.get("requires_model") and not live:
        result.skipped = True
        result.notes.append("needs the model to call a tool; skipped offline")
        return result

    fixture = load_fixture(case["fixture"]) if case.get("fixture") else None
    session_id = case.get("session_id") or f"eval_{case['name']}"
    config = {"configurable": {"thread_id": session_id}}

    for _ in range(RUNS_PER_CASE):
        # A fresh graph per run: no state, no checkpoints, no carry-over.
        graph = create_weather_sop_graph()

        with case_environment(case, fixture, live):
            if case.get("requires_prerequisite"):
                graph.invoke(
                    {
                        "message": "Is it good to cycle in Bhopal?",
                        "session_id": session_id,
                        "activity": "cycling",
                        "time_window": "today",
                        "raw_weather": fixture or {},
                    },
                    config=config,
                )

            state_input: Dict[str, Any] = {
                "message": case["input"],
                "session_id": session_id,
            }
            if live:
                # Only the message. Locating, timing and activity are the
                # model's job now.
                pass
            else:
                # Offline the fallback needs the engine inputs up front, since
                # no model will ever call the tools.
                state_input["activity"] = case.get("activity")
                if fixture is not None:
                    state_input["raw_weather"] = fixture
                    state_input["time_window"] = case.get("time_window", "today")

            try:
                state = graph.invoke(state_input, config=config)
            except Exception as exc:  # noqa: BLE001 - a crash is a failure
                result.notes.append(f"exception: {exc}")
                continue

            if "deterministic_reply" in (state.get("trace") or []):
                result.fallback_runs += 1

            failures = check_case(case, state)
            if failures:
                result.notes.extend(failures)
            else:
                result.passes += 1

    result.notes = sorted(set(result.notes))
    return result


def run_evaluations(mode: Optional[str] = None) -> List[CaseResult]:
    live = llm_available() if mode is None else (mode == "live")
    cases = load_cases()

    print(f"=== EVALUATION SUITE ({'live model' if live else 'offline / no API key'}) ===")

    results = []
    for case in cases:
        result = run_case(case, live)
        results.append(result)
        suffix = ""
        if result.skipped:
            suffix = "  (needs a live model)"
        elif result.fallback_runs:
            suffix = f"  ({result.fallback_runs}/{RUNS_PER_CASE} via deterministic fallback)"
        print(f"  {result.name:<32} {result.passes}/{RUNS_PER_CASE} {result.status}{suffix}")
        for note in result.notes:
            print(f"      - {note}")

    write_report(results, live)
    return results


def write_report(results: List[CaseResult], live: bool) -> None:
    scored = [r for r in results if not r.skipped]
    total = sum(r.passes for r in scored)
    possible = len(scored) * RUNS_PER_CASE
    skipped = len(results) - len(scored)
    out = BACKEND_DIR / "evals" / "RESULTS.md"

    with open(out, "w", encoding="utf-8") as f:
        f.write("# Evaluation Results\n\n")
        if live:
            f.write("Run with the live model driving the tool loop.\n\n")
        else:
            f.write(
                "> **No usable API key, so this run used the deterministic fallback.** "
                "It measures policy matching, citation validity and numeric faithfulness, "
                "which are engine properties. It does not measure the model's tool "
                "selection, parameter choice or phrasing. Re-run with a key to evaluate "
                "the agent itself.\n\n"
            )

        if skipped:
            f.write(
                f"{skipped} scenario(s) were skipped because they can only be observed "
                "when the model drives a tool call; they are listed as `SKIP`, not as passes.\n\n"
            )

        f.write(f"**{total}/{possible} runs passed** across {len(scored)} scored scenarios.\n\n")

        f.write("| Scenario | What is checked | Result | Fallback | Notes |\n")
        f.write("| --- | --- | --- | --- | --- |\n")
        for r in results:
            notes = "; ".join(r.notes) if r.notes else "clean"
            shown = "-" if r.skipped else f"{r.passes}/{RUNS_PER_CASE}"
            f.write(
                f"| `{r.name}` | {r.what_we_check} | **{shown}** `{r.status}` "
                f"| {r.fallback_runs}/{RUNS_PER_CASE} | {notes} |\n"
            )

        f.write(
            "\n## Invariants checked on every run\n\n"
            "1. The reply is non-empty.\n"
            "2. Every cited SOP id exists in the registry.\n"
            "3. Every cited SOP id actually matched the derived facts.\n"
            "4. No forbidden id from an injection case appears anywhere.\n"
            "5. The grounding verifier accepts every number in the reply.\n"
            "6. A failure outcome carries no numbers at all.\n"
            "7. Expected outcome, matched SOPs and location source, where the fixture pins them.\n"
        )

    print(f"\nReport written to {out}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode not in (None, "live", "offline"):
        print("usage: run_evals.py [live|offline]")
        raise SystemExit(2)
    run_evaluations(mode)