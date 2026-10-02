from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List

from app.schemas import ChatRequest, ChatResponse, SOPSummary, LocationModel
from app.graph.builder import graph_app
from app.graph.nodes import SOP_REGISTRY

from app.config import CORS_ORIGINS

app = FastAPI(
    title="Weather-SOP Advisory Bot API",
    description="Deterministic SOP-grounded weather safety advisory chatbot API",
    version="1.0.0"
)

# Configure CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "sops_loaded": len(SOP_REGISTRY.sops),
        "taxonomy": SOP_REGISTRY.taxonomy,
    }


@app.get("/sops", response_model=List[SOPSummary])
def list_sops():
    summaries = []
    for sop in SOP_REGISTRY.sops:
        advice_str = sop.advice or ""
        if not advice_str and sop.scoring:
            advice_str = " / ".join([b.advice for b in sop.scoring.bands])

        summaries.append(
            SOPSummary(
                id=sop.id,
                category=sop.category,
                title=sop.title,
                severity=sop.severity,
                applies_to=sop.applies_to,
                precedence=sop.precedence,
                advice_template=advice_str,
            )
        )
    return summaries


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if not req.session_id or not req.session_id.strip():
        raise HTTPException(status_code=400, detail="Session ID cannot be empty.")

    config = {"configurable": {"thread_id": req.session_id.strip()}}

    initial_input = {
        "session_id": req.session_id.strip(),
        "message": req.message.strip(),
    }

    try:
        final_state = graph_app.invoke(initial_input, config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Graph execution error: {str(e)}") from e

    # Extract response fields
    reply = final_state.get("reply", "No response generated.")
    outcome = final_state.get("outcome", "answered")

    ranking = final_state.get("ranking_result", {})
    sop_ids = ranking.get("all_sop_ids", [])
    if not sop_ids and final_state.get("matched_sops"):
        sop_ids = [s[0].id for s in final_state.get("matched_sops", [])]

    location = final_state.get("location")
    facts_used = final_state.get("facts", {})
    trace = final_state.get("trace", [])

    return ChatResponse(
        reply=reply,
        outcome=outcome,
        sop_ids=sop_ids,
        location=location,
        facts_used=facts_used,
        trace=trace,
    )

@app.get("/")
def root():
    return {
        "status": "healthy",
        "service": "weather-sop-advisory-bot",
        "docs": "/docs",
    }

