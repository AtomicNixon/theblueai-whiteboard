"""FastAPI app entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, routes, ws_routes
from .config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("whiteboard")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_pool()
    log.info("whiteboard backend ready on port %s", settings.port)
    yield


app = FastAPI(title="whiteboard", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_url, "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": "0.1.0"}


app.include_router(routes.router, prefix="/api")
app.include_router(ws_routes.router)
