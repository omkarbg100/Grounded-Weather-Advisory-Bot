# Weather SOP Advisory Bot

A deterministic, SOP-grounded outdoor weather safety chatbot. Every reply is traceable to a written YAML policy. The LLM decides *parameters*; it never decides advice or facts.

> 📘 **Full Architecture**: see [ARCH.md](ARCH.md) for the trust boundary, the six-node graph, the tool contract, and the verification rules.

---

## How it works

```
User message
   │
   ▼
guard_input      PII redacted, injection detected
   │
   ▼
agent_loop       Gemini chooses tools and arguments  ← the only non-deterministic part
   │  search_location → get_forecast → evaluate_policies → end_turn
   ▼
verify_reply     every cited SOP matched, every number supported
   │        │
   │        └─ fails ─► repair_reply (once) ─► verify_reply
   │                     │ still fails
   │                     ▼
   │              deterministic_reply   published advice, verbatim
   ▼
finalize         settle the outcome
```

The model supplies the place, the time window and the activity. The server supplies the facts, the refs and the policy. It cannot write a weather value into the conversation, and it cannot introduce a policy that did not match — `evaluate_policies` takes a server-minted `forecast_ref`, and an `include_ids` argument can only narrow the result.

If Gemini is unreachable the graph degrades to the deterministic path rather than failing, and says so.

---

## Quick Start with Docker

```bash
cp .env.example .env      # add your GEMINI_API_KEY
docker compose up --build
```

- **Frontend**: `http://localhost:5173`
- **Backend API**: `http://localhost:8000`
- **Swagger**: `http://localhost:8000/docs`

Stop with `docker compose down`.

---

## Running Locally

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows; source .venv/bin/activate on Linux/Mac
pip install -r requirements.txt

cp ../.env.example ../.env    # add GEMINI_API_KEY from https://aistudio.google.com/
uvicorn app.main:app --reload
```

Health: `http://localhost:8000/health` · Policies: `http://localhost:8000/sops`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

---

## Tests and Evaluation

```bash
cd backend

# 109 tests, no network access required
python -m pytest tests/ -q

# Golden scenarios from evals/cases.yaml
python evals/run_evals.py offline   # deterministic path; no key needed
python evals/run_evals.py live      # model drives the tool loop
```

The test suite never calls Gemini. `ScriptedModel` replays a fixed tool-call sequence, and `Ref("get_forecast", "forecast_ref")` placeholders are resolved against what the dispatcher actually returned — so a test cannot hard-code a value the server is supposed to mint.

`run_evals.py` checks invariants that must hold whatever the model does: cited SOP ids exist, cited SOP ids actually matched, every number is supported, failure replies carry no numbers, and injection attempts leak nothing. Scenarios that can only be observed when the model calls a tool are marked `requires_model: true` and reported as `SKIP` offline rather than quietly counted as passes.

---

## Writing Policies

Policies live in `backend/app/sops/` as YAML:

```yaml
- id: EXE-WIND-CYCLING-01
  category: outdoor_exercise
  title: High Wind Risk for Two-Wheelers
  severity: high                        # info | low | moderate | high | critical
  applies_to: ["cycling", "two_wheeler"]
  precedence: override                  # optional; wins ahead of severity
  when:
    any:
      - fact: wind_kmh
        op: ">="
        value: 35.0
      - fact: max_gust_kmh
        op: ">="
        value: 45.0
  advice: "HIGH WIND WARNING: {wind_kmh} km/h wind and gusts up to {max_gust_kmh} km/h."
  tags: ["cycling", "wind"]
```

Add a file and restart. No Python changes. Thresholds in the engine itself — severity ordering, fact definitions, time-window boundaries, agent limits, guardrail patterns — are in `backend/app/policy_config.yaml` and the `app/guardrails/*.yaml` files.

---

## Configuration

```env
GEMINI_API_KEY=AIza...
LLM_MODEL=gemini-flash-latest     # optional
LLM_TEMPERATURE=0.1
LLM_TIMEOUT=15.0
```

Requires `google-genai>=2.20.0`: the tool declarations use `parameters_json_schema`, which earlier releases do not provide.

If the key is missing, rate-limited or offline the service still responds — it returns published advice quoted verbatim and reports the outcome as `llm_unavailable`.

---

## Requirements

| # | Requirement | Where |
|---|---|---|
| 1 | LangGraph with real conditional branching | `app/graph/builder.py`, `app/graph/routing.py` |
| 2 | FastAPI backend, React+Vite frontend, Python 3.11+ | `app/main.py`, `frontend/` |
| 3 | Every reply traceable to SOP ids | `app/engine/verifier.py` |
| 4 | A policy change is a YAML edit | `app/sops/*.yaml`, `app/tools/sop_loader.py` |
| 5 | No guessing when weather or location fails | tool error results → `weather_failed` / `location_failed` |
| 6 | No invented advice when no SOP matches | `evaluate_policies` returns empty; `no_sop` |
| 7 | Numbers verified against derived facts | `app/engine/verifier.py` |
| 8 | Session memory | `MemorySaver`, `thread_id=session_id` |
| 9 | Keys via `.env` only | `app/config.py`, `.gitignore` |
| 10 | Gemini configured through the SDK | `app/config.py`, `app/llm/client.py` |