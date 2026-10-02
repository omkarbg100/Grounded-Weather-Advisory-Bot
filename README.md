# Weather SOP Advisory Bot

A deterministic, SOP-grounded outdoor weather safety chatbot. Every reply is traceable to a written YAML policy. The LLM never decides advice or facts.

> 📘 **Full Architecture & Technical Documentation**: See [ARCH.md](ARCH.md) for complete system design, Mermaid sequence flowcharts, 17-node specifications, security guardrail details, and the verification loop mechanics.

> 📄 **YAML Policy Rationale**: Non-engineers can edit SOPs without touching code, diffs cleanly in git, and the schema is validated at server startup.

---

## Quick Architecture Summary

For full node-by-node details, see [ARCH.md](ARCH.md).

```
User Message → guard_input (PII & Injection Check) → extract_coords → parse_intent
                    │
                    ▼
          [Location Resolution] ──► fetch_weather (Open-Meteo) ──► build_facts
                                                                         │
                                                                         ▼
  compose_reply ◄── resolve_conflicts ◄── match_sops (YAML Rules) ◄──────┘
        │
        ▼
   verify_reply (Fact & SOP Citation Check) ──► finalize ──► Response
```

---

---

## Quick Start with Docker (Recommended)

Run both the FastAPI backend and React frontend with a single command:

```bash
# 1. Create your .env file
cp .env.example .env
# Fill in your GEMINI_API_KEY in .env

# 2. Build and start containers
docker compose up --build
```

- **Frontend App**: `http://localhost:5173` (or `http://localhost`)
- **Backend API**: `http://localhost:8000`
- **Interactive API Docs (Swagger)**: `http://localhost:8000/docs`

To stop containers:
```bash
docker compose down
```

---

## Setup & Running Locally (Without Docker)

### 1. Backend Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate       # Windows (or source .venv/bin/activate on Linux/Mac)
pip install -r requirements.txt

# Create .env file with your Gemini API Key
cp ../.env.example ../.env
# Edit ../.env and add your GEMINI_API_KEY from https://aistudio.google.com/

uvicorn app.main:app --reload
```

Backend server runs at: `http://localhost:8000`  
Health check: `http://localhost:8000/health`  
List active SOPs: `http://localhost:8000/sops`

### 2. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Open your browser at: `http://localhost:5173`

---

## Running Tests & Evaluation Suite

```bash
cd backend

# Unit & Integration Tests (16 tests, 100% pass)
python -m pytest tests/ -q

# Evaluation Suite Runner (Evaluates all 6 core PDF test cases)
python -m evals.eval_runner
```

---

## SOP Policy Format & Live Review Test

SOP policies are written in clean, declarative YAML files located in `backend/app/sops/`:

```yaml
- id: EXE-WIND-CYCLING-01
  category: outdoor_exercise
  title: High Wind Risk for Two-Wheelers
  severity: high                        # info | low | moderate | high | critical
  applies_to: ["cycling", "two_wheeler"]
  precedence: override                  # optional; bypasses normal severity ranking
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

### Adding a New SOP Live (Zero Code Changes):
1. Create a new `.yaml` file in `backend/app/sops/` (e.g. `SOP-SWIMMING-HEAT-01.yaml`).
2. Save the file and restart/reload the server.
3. The system automatically loads, validates, and registers the new policy with **zero Python code modifications**.

---

## Gemini API Configuration

The application uses the official **`google-genai`** Python SDK directly with `GEMINI_API_KEY`. No gateway or proxy required.

```env
GEMINI_API_KEY=AIza...
LLM_MODEL=gemini-flash-latest    # optional, defaults to gemini-flash-latest
LLM_TEMPERATURE=0.1
LLM_TIMEOUT=15.0
```

### Fallback Guarantee:
If the Gemini API key is missing, rate-limited (429), or offline, the system **never crashes**. It automatically falls back to deterministic keyword heuristics (`_fallback_structured`), allowing graph routing, SOP matching, and UI responses to complete successfully.

---

## Hard Requirement Compliance Checklist

| # | Requirement | Implementation / Location |
|---|---|---|
| 1 | LangGraph with real conditional branching | `graph/builder.py`, `graph/routing.py` |
| 2 | FastAPI backend, React+Vite frontend, Python 3.11+ | `app/main.py`, `frontend/` |
| 3 | Every reply traceable to SOP IDs | `engine/verifier.py`, `graph/nodes.py` |
| 4 | SOP change = YAML edit only | `app/sops/*.yaml`, `tools/sop_loader.py` |
| 5 | No guessing on weather/location failure | `graph/nodes.py: fail_weather, fail_location` |
| 6 | No invented advice when no SOP | `graph/nodes.py: respond_no_sop` |
| 7 | Numbers in reply verified against API payload | `engine/verifier.py: verify_reply` |
| 8 | Session memory via MemorySaver | `graph/builder.py: MemorySaver`, `thread_id=session_id` |
| 9 | API keys via `.env` only | `app/config.py`, `.gitignore` |
| 10 | Gemini API key configuration | `app/config.py`, `llm/client.py` |
