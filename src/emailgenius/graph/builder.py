from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from .state import CompanyState
from .nodes_osint import node_discover_company, node_enrich_company
from .nodes_llm import node_retrieve_marketing, node_generate_copy
from .nodes_db import node_persist_record, node_hitl_wait

def build_workflow():
    workflow = StateGraph(CompanyState)
    
    workflow.add_node("discover", node_discover_company)
    workflow.add_node("enrich", node_enrich_company)
    workflow.add_node("retrieve", node_retrieve_marketing)
    workflow.add_node("llm", node_generate_copy)
    workflow.add_node("hitl_wait", node_hitl_wait)
    workflow.add_node("persist", node_persist_record)
    
    workflow.add_edge(START, "discover")
    workflow.add_edge("discover", "enrich")
    workflow.add_edge("enrich", "retrieve")
    workflow.add_edge("retrieve", "llm")
    workflow.add_edge("llm", "hitl_wait")
    workflow.add_edge("hitl_wait", "persist")
    workflow.add_edge("persist", END)
    
    app = workflow.compile()
    return app
