from __future__ import annotations

import asyncio
from typing import Any
from .state import CompanyState

async def node_retrieve_marketing(state: CompanyState, config: Any) -> dict:
    store = config["configurable"].get("store")
    llm = config["configurable"].get("llm")
    parent_slug = config["configurable"].get("parent_slug")
    
    company = state["company"]
    dossier = state.get("dossier")
    
    if store and llm and parent_slug and dossier:
        query = f"{company.company_name} {dossier.site_summary}"
        embeddings = await asyncio.to_thread(llm.embed_texts, [query])
        if embeddings:
            results = await asyncio.to_thread(
                store.search_knowledge_chunks,
                parent_slug=parent_slug,
                kind="marketing",
                query_embedding=embeddings[0],
                top_k=6
            )
            snippets = [str(item.get("content") or "") for item in results]
            dossier.evidence.extend(snippets)
            
    return {"dossier": dossier}

async def node_generate_copy(state: CompanyState, config: Any) -> dict:
    llm = config["configurable"].get("llm")
    parent = config["configurable"].get("parent")
    company = state["company"]
    contact = state.get("contact")
    dossier = state.get("dossier")
    
    variants = []
    # Example logic bridging to existing generation
    if llm and dossier:
        # In agent engine this uses engine.generate_sequence, for now returning empty variants
        pass
        
    return {"variants": variants}
