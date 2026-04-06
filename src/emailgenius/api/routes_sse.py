from __future__ import annotations

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

@router.get("/api/campaigns/{campaign_id}/stream")
async def stream_campaign_events(campaign_id: str, request: Request):
    async def event_generator():
        # async for event in app.astream(..., stream_mode="updates"):
        #     yield {"data": event}
        yield {"data": "stream_started"}
        
    return EventSourceResponse(event_generator())
