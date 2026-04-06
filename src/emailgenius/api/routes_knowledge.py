from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

router = APIRouter()

@router.post("/api/profiles")
async def create_profile():
    return {"status": "created"}

@router.post("/api/knowledge/ingest")
async def ingest_knowledge(file: UploadFile = File(...)):
    # Calculate hash and ingest into vector db asynchronously
    return {"status": "ingested"}
