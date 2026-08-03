"""FastAPI app entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, oauth_routes, routes, ws_routes
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
# Declares its own full paths (/oauth/... and /api/auth/...), so no prefix.
app.include_router(oauth_routes.router)

# Paths that belong to the backend API surface. An unmatched path under one of
# these is a bug in the caller, not a client-side route, and must 404 as JSON —
# never fall through to the SPA. Before this guard existed, GET /api/typo
# returned index.html with status 200, so a client checking `res.ok` saw success
# and then failed parsing HTML as JSON. That is a genuinely awful thing to debug
# from inside an MCP tool.
_API_PREFIXES = ("api/", "ws/", "healthz", "oauth/")


# Serve the built React + Excalidraw frontend for any path that isn't an API or
# WebSocket route.  index.html is returned for unknown paths so client-side
# routing (React Router, if we add it) keeps working.
if os.path.isdir(_STATIC_DIR):
    # Asset filenames are content-hashed by Vite, so a changed file is a changed
    # URL and these can be cached forever.
    class _ImmutableStatic(StaticFiles):
        def file_response(self, *args, **kwargs):  # type: ignore[override]
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    app.mount("/assets", _ImmutableStatic(directory=os.path.join(_STATIC_DIR, "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # The routers above claim the real API/WS routes; anything under an API
        # prefix that reaches here is an unknown endpoint.
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such endpoint: /{full_path}")

        index_path = os.path.join(_STATIC_DIR, "index.html")
        if os.path.isfile(index_path):
            # The shell must NOT be cached. It names the hashed bundle, so a
            # stale index.html keeps a browser on old JavaScript indefinitely
            # after a deploy — which is how a client ended up posting image
            # elements at a backend that had already stopped accepting them.
            # media_type is explicit because this route's default response class
            # would otherwise announce JSON.
            return FileResponse(
                index_path,
                media_type="text/html",
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )
        # Signals that stage 1 of the Docker build didn't land. 503 rather than
        # 200 so a health probe or a deploy check can actually see it.
        return JSONResponse(
            {"ok": False, "detail": "whiteboard frontend not built"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
