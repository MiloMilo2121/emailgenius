from __future__ import annotations

from fastapi import APIRouter
from prometheus_client import Counter, Histogram, generate_latest
from fastapi.responses import Response

router = APIRouter()

LLM_REQUESTS_TOTAL = Counter("llm_requests_total", "Total LLM requests")
TOOL_LATENCY_SECONDS = Histogram("tool_latency_seconds", "Latency of OSINT/LLM tools")

@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")
