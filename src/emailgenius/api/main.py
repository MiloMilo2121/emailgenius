from __future__ import annotations

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from .routes_campaign import router as campaign_router
from .routes_sse import router as sse_router
from .routes_approval import router as approval_router
from .routes_knowledge import router as knowledge_router
from .metrics import router as metrics_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup AsyncConnectionPool
    db_url = os.environ.get("EMAILGENIUS_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/emailgenius")
    pool = AsyncConnectionPool(conninfo=db_url, open=False)
    await pool.open(wait=True)
    app.state.pool = pool
    yield
    await pool.close()

app = FastAPI(title="EmailGenius Agentic API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaign_router)
app.include_router(sse_router)
app.include_router(approval_router)
app.include_router(knowledge_router)
app.include_router(metrics_router)
