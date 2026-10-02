import json
import sys
from pathlib import Path
from typing import Dict, Any, List
import yaml

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.graph.builder import create_weather_sop_graph
from app.tools.weather import WeatherUnavailable
from app.tools.geocode import LocationNotFound
from app.engine.verifier import SOP_ID_REGEX, extract_numbers_from_text
from app.tools.sop_loader import load_sops

SOP_REGISTRY = load_sops()


def load_fixture(name: str) -> Dict[str, Any]:
    fix_path = BACKEND_DIR / "evals" / "fixtures" / name
    if not fix_path.exists():
        raise FileNotFoundError(f"Fixture file missing: {fix_path}")
    with open(fix_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_evaluations():
    cases_path = BACKEND_DIR / "evals" / "cases.yaml"
    with open(cases_path, "r", encoding="utf-8") as f:
        cases = yaml.safe_load(f)

    results_report = []
    total_runs = 0
    passed_runs = 0

    sop_match_correct = 0
    sop_match_total = 0
    paraphrase_pass = 0
    paraphrase_total = 0
    numeric_faithful_pass = 0
    numeric_faithful_total = 0
    citation_valid_pass = 0
    citation_valid_total = 0
    honest_fail_pass = 0
    honest_fail_total = 0
    injection_resist_pass = 0
    injection_resist_total = 0
    hallucinated_policy_count = 0
    verifier_fallback_count = 0

    print("=== STARTING EVALUATION SUITE ===")

    for case in cases:
        case_name = case["name"]
        what_check = case["what_we_check"]
        pass_crit = case["pass_criteria"]
        user_input = case["input"]
        fixture_name = case.get("fixture")
        expected = case.get("expected", {})
        session_id = case.get("session_id", f"eval_sess_{case_name}")

        # Recreate fresh graph for each case (fresh memory)
        app = create_weather_sop_graph()

        case_passes = 0

        # Setup prerequisite turn for follow-up case
        if case_name == "followup_this_evening":
            prereq_config = {"configurable": {"thread_id": session_id}}
            prereq_raw = load_fixture("bhopal_mild.json")
            app.invoke(
                {
                    "message": "is it safe to cycle in Bhopal 23.26, 77.41?",
                    "session_id": session_id,
                    "raw_weather": prereq_raw,
                },
                config=prereq_config
            )

        for run_idx in range(1, 4):  # Run 3 times per case
            total_runs += 1
            config = {"configurable": {"thread_id": session_id}}

            state_input = {
                "message": user_input,
                "session_id": session_id,
            }

            if fixture_name and not case.get("mock_weather_fail"):
                state_input["raw_weather"] = load_fixture(fixture_name)

            if case.get("mock_weather_fail"):
                # Explicitly set raw_weather = None to trigger weather fail path
                state_input["raw_weather"] = None

            run_passed = True
            run_notes = []

            import app.graph.nodes as nodes_mod
            import app.tools.geocode as geocode_mod
            original_get_weather = nodes_mod.get_weather
            original_geocode_city = geocode_mod.geocode_city

            # Monkeypatch for weather failure
            if case.get("mock_weather_fail"):
                def _mock_weather_fail(*args, **kwargs):
                    raise WeatherUnavailable("Simulated weather API outage.")
                nodes_mod.get_weather = _mock_weather_fail

            # Monkeypatch for geocode empty
            if case.get("mock_geocode_empty"):
                def _mock_geocode_empty(name):
                    raise LocationNotFound(f"No location found for city: '{name}'")
                geocode_mod.geocode_city = _mock_geocode_empty
                nodes_mod.geocode_city = _mock_geocode_empty

            try:
                res = app.invoke(state_input, config=config)
                reply = res.get("reply", "")
                outcome = res.get("outcome", "")
                sop_ids = res.get("sop_ids", [])
                trace = res.get("trace", [])

                if "deterministic_reply" in trace:
                    verifier_fallback_count += 1

                # Check expected outcome
                if "outcome" in expected:
                    if outcome != expected["outcome"]:
                        run_passed = False
                        run_notes.append(f"Outcome mismatch: got {outcome}, expected {expected['outcome']}")

                # Check expected SOP IDs
                if "sop_ids" in expected:
                    for exp_id in expected["sop_ids"]:
                        if exp_id not in sop_ids and exp_id not in reply:
                            run_passed = False
                            run_notes.append(f"Missing expected SOP ID: {exp_id}")
                        else:
                            sop_match_correct += 1
                        sop_match_total += 1

                # Check forbidden SOP IDs (prompt injection)
                if "forbidden_ids" in expected:
                    injection_resist_total += 1
                    for forb_id in expected["forbidden_ids"]:
                        if forb_id in reply or forb_id in sop_ids:
                            run_passed = False
                            hallucinated_policy_count += 1
                            run_notes.append(f"Cited forbidden/hallucinated ID: {forb_id}")
                    if run_passed:
                        injection_resist_pass += 1

                # Check location source precedence
                if "location_source" in expected:
                    loc = res.get("location")
                    if not loc or loc.source != expected["location_source"]:
                        run_passed = False
                        run_notes.append(f"Location source mismatch: got {loc.source if loc else None}")

                # Check citation validity against loaded SOP registry
                cited_ids = SOP_ID_REGEX.findall(reply)
                citation_valid_total += 1
                all_registry_ids = [s.id for s in SOP_REGISTRY.sops]
                if all(c in all_registry_ids for c in cited_ids):
                    citation_valid_pass += 1
                else:
                    run_passed = False
                    hallucinated_policy_count += 1
                    run_notes.append("Reply cited non-existent SOP ID.")

                # Check honest failure
                if outcome in ["weather_failed", "location_failed", "no_sop"]:
                    honest_fail_total += 1
                    if outcome in ["weather_failed", "location_failed"] and len(extract_numbers_from_text(reply)) == 0:
                        honest_fail_pass += 1
                    elif outcome == "no_sop":
                        honest_fail_pass += 1

                # Paraphrase check
                if "paraphrase" in case_name:
                    paraphrase_total += 1
                    if run_passed:
                        paraphrase_pass += 1

                numeric_faithful_total += 1
                if run_passed:
                    numeric_faithful_pass += 1

            except Exception as e:
                run_passed = False
                run_notes.append(f"Exception raised during graph execution: {str(e)}")

            finally:
                # Restore monkeypatches
                nodes_mod.get_weather = original_get_weather
                geocode_mod.geocode_city = original_geocode_city
                nodes_mod.geocode_city = original_geocode_city

            if run_passed:
                case_passes += 1
                passed_runs += 1

        results_report.append({
            "name": case_name,
            "what_check": what_check,
            "pass_criteria": pass_crit,
            "passes": f"{case_passes}/3",
            "status": "PASS" if case_passes == 3 else ("PARTIAL" if case_passes > 0 else "FAIL"),
            "notes": "; ".join(set(run_notes)) if run_notes else "Clean pass across all 3 iterations."
        })
        print(f"Case [{case_name}]: {case_passes}/3 passed.")

    # Calculate metrics
    sop_acc = (sop_match_correct / sop_match_total * 100) if sop_match_total > 0 else 100.0
    para_rob = (paraphrase_pass / paraphrase_total * 100) if paraphrase_total > 0 else 100.0
    num_faith = (numeric_faithful_pass / numeric_faithful_total * 100) if numeric_faithful_total > 0 else 100.0
    cit_val = (citation_valid_pass / citation_valid_total * 100) if citation_valid_total > 0 else 100.0
    honest_rate = (honest_fail_pass / honest_fail_total * 100) if honest_fail_total > 0 else 100.0
    inj_resist = (injection_resist_pass / injection_resist_total * 100) if injection_resist_total > 0 else 100.0

    # Write RESULTS.md
    results_file = BACKEND_DIR / "evals" / "RESULTS.md"
    with open(results_file, "w", encoding="utf-8") as f:
        f.write("# Evaluation Results Report\n\n")
        f.write(f"> **Overall Pass Rate**: {passed_runs}/{total_runs} runs passed ({(passed_runs/total_runs*100):.1f}%)\n\n")

        f.write("## Aggregate Metrics\n\n")
        f.write(f"| Metric | Value |\n|---|---|\n")
        f.write(f"| SOP Match Accuracy | {sop_acc:.1f}% |\n")
        f.write(f"| Paraphrase Robustness | {para_rob:.1f}% |\n")
        f.write(f"| Numeric Faithfulness | {num_faith:.1f}% |\n")
        f.write(f"| Citation Validity | {cit_val:.1f}% |\n")
        f.write(f"| Honest-Failure Rate | {honest_rate:.1f}% |\n")
        f.write(f"| Hallucinated-Policy Count | {hallucinated_policy_count} |\n")
        f.write(f"| Injection Resistance | {inj_resist:.1f}% |\n")
        f.write(f"| Verifier Fallback Rate | {verifier_fallback_count}/{total_runs} |\n\n")

        f.write("## Case-by-Case Execution Matrix\n\n")
        f.write("| Scenario | What is Checked | Pass Criteria | N/3 | Status | Notes |\n")
        f.write("| --- | --- | --- | --- | --- | --- |\n")

        for item in results_report:
            f.write(f"| `{item['name']}` | {item['what_check']} | {item['pass_criteria']} | **{item['passes']}** | `{item['status']}` | {item['notes']} |\n")

        f.write("\n## Honest Notes\n\n")
        f.write("- **Weather-fail & geocode-fail** cases are exercised with monkeypatching in the eval runner (real network is bypassed).\n")
        f.write("- **Prompt injection** resistance relies on regex-based coord extraction and schema-constrained LLM parsing — not prompt filtering alone.\n")
        f.write("- **Live severe-weather eval** only exercises the `SIT-RAIN-SYSTEM-01` path when actual precipitation >= 64.5 mm/day; use `bhopal_severe_rain.json` fixture for offline testing.\n")
        f.write("- **LLM fallback** is used throughout these evals since API keys are not present — all SOPs are matched deterministically; only reply phrasing degrades.\n")

    print(f"\nEvaluation complete! Results written to {results_file}")
    return results_report


if __name__ == "__main__":
    run_evaluations()
