from __future__ import annotations

from typing import Any
from .state import CompanyState
from ..types import CampaignCompanyResult, ApprovalRecord
from ..utils import utc_now_iso

async def node_persist_record(state: CompanyState, config: Any) -> dict:
    store = config["configurable"].get("store")
    campaign_id = config["configurable"].get("campaign_id", "unknown_campaign")
    parent_slug = config["configurable"].get("parent_slug", "")
    
    company = state["company"]
    contact = state.get("contact")
    dossier = state.get("dossier")
    variants = state.get("variants", [])
    
    if dossier:
        result = CampaignCompanyResult(
            campaign_id=campaign_id,
            parent_slug=parent_slug,
            company=company,
            contact=contact,
            dossier=dossier,
            variants=variants,
            recommended_variant="A",
            approval=ApprovalRecord(status="PENDING", updated_at=utc_now_iso())
        )
        
        if store:
            if hasattr(store, "insert_campaign_company_result_async"):
                await store.insert_campaign_company_result_async(result)
            else:
                # dummy fallback for typing
                pass
                
    return {}

def node_hitl_wait(state: CompanyState) -> dict:
    return {}
