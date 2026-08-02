"""FastAPI app entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, routes, ws_routes
from .config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("whiteboard")

# Path to the built frontend.  In the Docker image this lives at /app/static.
# For local dev the Vite dev server on :5173 serves the frontend directly,
# so this mount only matters in production / Docker.
_STATIC_DIR = os.environ.get("WB_STATIC_DIR", "/app/static")


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

# Serve the built React + Excalidraw frontend for any path that isn't an API or
# WebSocket route.  index.html is returned for unknown paths so client-side
# routing (React Router, if we add it) keeps working.
if os.path.isdir(_STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(_STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # API/WebSocket are handled by the routers above; this only catches
        # unmatched paths.  Always serve index.html so the React app boots.
        index_path = os.path.join(_STATIC_DIR, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return {"ok": False, "detail": "whiteboard frontend not built"}
