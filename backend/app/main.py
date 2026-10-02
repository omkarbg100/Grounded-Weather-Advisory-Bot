from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.graph.builder import graph_app
from app.graph.nodes import SOP_REGISTRY
from app.schemas import ChatRequest, ChatResponse, SOPSummary

app = FastAPI(
    title="Weather-SOP Advisory Bot API",
    description=(
        "SOP-grounded weather safety advisory chatbot. The model chooses tool "
        "arguments; all safety policy is evaluated deterministically."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    from app.llm.client import is_available

    return {
        "status": "ok",
        "sops_loaded": len(SOP_REGISTRY.sops),
        "taxonomy": SOP_REGISTRY.taxonomy,
        "llm_available": is_available(),
    }


@app.get("/sops", response_model=List[SOPSummary])
def list_sops():
    summaries = []
    for sop in SOP_REGISTRY.sops:
        advice_str = sop.advice or ""
        if not advice_str and sop.scoring:
            advice_str = " / ".join([band.advice for band in sop.scoring.bands])

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
    initial_input: Dict[str, Any] = {
        "session_id": req.session_id.strip(),
        "message": req.message.strip(),
    }

    try:
        final_state = graph_app.invoke(initial_input, config=config)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Graph execution error: {exc}"
        ) from exc

    return ChatResponse(
        reply=final_state.get("reply", ""),
        outcome=final_state.get("outcome", "unavailable"),
        sop_ids=_sop_ids(final_state),
        location=final_state.get("location"),
        facts_used=final_state.get("facts") or {},
        trace=final_state.get("trace") or [],
    )


def _sop_ids(state: Dict[str, Any]) -> List[str]:
    ranking = state.get("ranking_result") or {}
    ids = ranking.get("all_sop_ids") or []
    if ids:
        return list(ids)
    return [sop.id for sop, _ in (state.get("matched_sops") or [])]


@app.get("/")
def root():
    return {
        "status": "healthy",
        "service": "weather-sop-advisory-bot",
        "docs": "/docs",
    }