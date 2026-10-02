# Weather SOP Advisory Bot

A deterministic, SOP-grounded outdoor weather safety chatbot. Every reply is traceable to a written YAML policy. The LLM decides *parameters* — place, time window, activity. It never decides advice or facts.

> 📘 Full design, trust boundary and verification rules: [ARCH.md](ARCH.md).

---

## How it works

```
User message
   │
   ▼
guard_input        PII redacted, control chars stripped, injection detected
   │
   ▼
agent_loop         Gemini picks tools and arguments   ← the only non-deterministic part
   │   search_location → get_forecast → evaluate_policies → end_turn
   ▼
verify_reply       every cited SOP matched; every number supported by facts or published advice
   │
   ├─ fails ─► repair_reply (once) ─► verify_reply
   │              │ still fails
   │              ▼
   │       deterministic_reply      published advice, verbatim
   ▼
finalize           settle the outcome
```

The model supplies the place, the time window and the activity. The server supplies the facts, the refs and the policy. The model cannot write a weather value into the conversation, and it cannot introduce a policy that did not match — `evaluate_policies` requires a server-minted `forecast_ref`, and its `include_ids` argument can only narrow the result, never widen it.

If Gemini is unreachable the graph degrades to the deterministic path and says so (`llm_unavailable`) rather than inventing data to fill the gap.

### The five tools

| Tool | Purpose |
|---|---|
| `search_location` | Resolve a named place to coordinates; returns every candidate with a server-minted `location_ref` for the model to choose from |
| `get_forecast` | Fetch conditions and derive the numeric facts; needs a `location_ref`, or the user's own coordinates |
| `evaluate_policies` | Match the YAML SOPs against a `forecast_ref` — the only source of guidance |
| `get_policy_catalog` | List activity tags and SOP ids, for when the request doesn't map cleanly |
| `end_turn` | Report the outcome and message; a validated enum, not a free-form verdict |

---

## Quick Start with Docker

```bash
cp .env.example .env      # add your GEMINI_API_KEY
docker compose up --build
```

- **Frontend**: `http://localhost:5173` · **Backend**: `http://localhost:8000` · **Swagger**: `http://localhost:8000/docs`

Stop with `docker compose down`.

---

## Running Locally

**Backend** (Python 3.11+):

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows; source .venv/bin/activate on Linux/Mac
pip install -r requirements.txt

cp ../.env.example ../.env    # add GEMINI_API_KEY from https://aistudio.google.com/
uvicorn app.main:app --reload
```

Health: `http://localhost:8000/health` · Policies: `http://localhost:8000/sops`

**Frontend**:

```bash
cd frontend
npm install
npm run dev
```

---

## Tests and Evaluation

```bash
cd backend

# 109 tests, no network access
python -m pytest tests/ -q

# Golden scenarios from evals/cases.yaml
python evals/run_evals.py offline   # deterministic path; no API key needed
python evals/run_evals.py live      # model drives the tool loop
```

The suite never calls Gemini. `ScriptedModel` replays a fixed tool-call sequence, and `Ref("get_forecast", "forecast_ref")` placeholders are resolved against what the dispatcher actually returned — a test cannot hard-code a value the server is supposed to mint.

`run_evals.py` asserts the invariants that must hold whatever the model does: cited SOP ids exist, cited SOP ids actually matched, every number is supported, failure replies carry no numbers, and injection attempts leak nothing. 11 of the 15 scenarios are scorable offline; the other 4 depend on the model issuing a tool call and are marked `requires_model: true`, reported as `SKIP` rather than quietly counted as passes.

---

## Writing Policies

Policies live in `backend/app/sops/` as YAML — `exercise.yaml`, `General.yml`, `leisure.yaml`, `situational.yaml`, `travel.yaml`, `vulnerable.yaml`:

```yaml
- id: EXE-WIND-CYCLING-01
  category: exercise
  title: High Wind Risk for Two-Wheelers and Cycling
  severity: high                        # info | low | moderate | high | critical
  applies_to: ["cycling", "cycling_commute", "two_wheeler"]
  precedence: override                  # optional; wins ahead of severity
  when:
    any:
      - fact: wind_kmh
        op: ">="
        value: 35.0
      - fact: max_gust_kmh
        op: ">="
        value: 45.0
  advice: "HIGH WIND WARNING: Sustained wind speeds of {wind_kmh} km/h and gusts up to {max_gust_kmh} km/h make cycling unsafe."
  tags: ["cycling", "wind", "safety"]
```

Supported operators: `>`, `>=`, `<`, `<=`, `==`, `between`, `in`. `severity` order is
`critical, high, moderate, low, info` (see `policy_config.yaml`). An SOP may name
only facts that exist in the fact registry, or it is rejected at load time.

Add a file and restart — no Python changes. Engine-level knobs are YAML too: `app/policy_config.yaml` holds severity ordering, fact definitions, time-window boundaries, coordinate bounds, request limits and agent limits; `app/guardrails/patterns.yaml` holds redaction and injection patterns; `app/guardrails/templates.yaml` holds every canned user-facing string.

---

## Configuration

```env
GEMINI_API_KEY=AIza...
LLM_MODEL=gemini-flash-latest     # optional; accepts "models/<id>" too
LLM_TEMPERATURE=0.1
LLM_TIMEOUT=15.0
CORS_ORIGINS=http://localhost:5173
```

Requires `google-genai>=2.20.0` — the tool declarations use `parameters_json_schema`, which earlier releases do not provide.

If the key is missing, rate-limited or offline the service still responds: it returns published advice quoted verbatim and reports the outcome honestly.

---

## Requirements

| # | Requirement | Where |
|---|---|---|
| 1 | LangGraph with real conditional branching | `app/graph/builder.py`, `app/graph/routing.py` |
| 2 | FastAPI backend, React+Vite frontend, Python 3.11+ | `app/main.py`, `frontend/` |
| 3 | Every reply traceable to SOP ids | `app/engine/verifier.py` |
| 4 | A policy change is a YAML edit | `app/sops/*.yaml`, `app/tools/sop_loader.py` |
| 5 | No guessing when weather or location fails | tool error results → `weather_failed` / `location_failed` |
| 6 | No invented advice when no SOP matches | `evaluate_policies` returns empty → `no_sop` |
| 7 | Numbers verified against derived facts | `app/engine/verifier.py` |
| 8 | Session memory | `MemorySaver`, `thread_id=session_id` |
| 9 | Keys via `.env` only | `app/config.py`, `.gitignore` |
| 10 | Gemini configured through the SDK | `app/config.py`, `app/llm/client.py` |

---

## Current state

The deterministic engine, the tool layer, the guardrails, the graph and the test suite are complete and verified offline (109 tests, 15 eval scenarios).

**The live model path has not yet been exercised end to end.** Everything above was validated against a scripted model because the Gemini free-tier quota was exhausted (`429 RESOURCE_EXHAUSTED`) during development. `python evals/run_evals.py live` is the outstanding check; expect to iterate on tool-argument quality once it can run.


by Omkar Gaikwad