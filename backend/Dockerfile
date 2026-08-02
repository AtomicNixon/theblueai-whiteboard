# Whiteboard — frontend build + backend runtime in one image.
#
# BUILD CONTEXT IS THE REPO ROOT, not backend/:
#     docker build -t whiteboard:latest -f backend/Dockerfile .
#
# The frontend is built inside stage 1 rather than copied in from the host.
# That matters because frontend/dist is gitignored — a git-based deploy never
# carries it, so any Dockerfile that expects a prebuilt dist on disk silently
# depends on someone remembering to run `npm run build` and stage the output by
# hand. Building it here makes `git pull && docker compose up -d --build` a
# complete deploy.

# ---- Stage 1: build the React + Excalidraw frontend --------------------------
FROM node:22-slim AS frontend

WORKDIR /build

# Dependencies first, so a source-only change doesn't re-run npm ci.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build          # tsc -b && vite build -> /build/dist

# ---- Stage 2: backend runtime ------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "asyncpg>=0.30.0" \
                "pydantic>=2.9.0" "pydantic-settings>=2.6.0" "httpx>=0.27.0" \
                "websockets>=13.0" "python-multipart>=0.0.12"

COPY backend/app ./app

# app/main.py serves this directory as the SPA (WB_STATIC_DIR, default
# /app/static). If it's absent the backend still runs and the API works —
# it just returns {"ok": false, "detail": "whiteboard frontend not built"}
# for page requests, which is a useful signal that stage 1 didn't land.
COPY --from=frontend /build/dist ./static

EXPOSE 8092
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
