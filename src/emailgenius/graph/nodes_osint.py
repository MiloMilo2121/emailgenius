from __future__ import annotations

import asyncio
from .state import CompanyState
from ..search import discover_company_and_news
from ..enrichment import build_enrichment_dossier_sync

async def node_discover_company(state: CompanyState) -> dict:
    company = state["company"]
    if not company.website:
        site_query, site_candidates, news_results, news_query, selected_site = await asyncio.to_thread(
            discover_company_and_news,
            company_name=company.company_name,
            city=company.location,
            site_max_results=10,
            news_max_results=8,
        )
        if selected_site:
            company.website = selected_site.url
    return {"company": company}

async def node_enrich_company(state: CompanyState) -> dict:
    company = state["company"]
    contact = state.get("contact")
    
    dossier, discovered_website = await asyncio.to_thread(
        build_enrichment_dossier_sync,
        company=company,
        contact=contact,
        headless=True,
        max_extra_pages=0,
        snapshot_timeout_ms=18000,
    )
    if discovered_website and not company.website:
        company.website = discovered_website
        
    return {"dossier": dossier, "company": company}
