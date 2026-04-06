from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class ApprovalRequest(BaseModel):
    approved_variant: str
    notes: str | None = None

@router.post("/api/campaigns/{campaign_id}/companies/{company_key}/approve")
async def approve_company(campaign_id: str, company_key: str, data: ApprovalRequest):
    # Update state in DB
    # app.aupdate_state(config, {"approval": ...}, as_node="hitl_wait")
    return {"status": "approved", "variant": data.approved_variant}
