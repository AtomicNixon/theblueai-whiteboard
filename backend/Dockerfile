# Whiteboard backend
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "asyncpg>=0.30.0" \
                "pydantic>=2.9.0" "pydantic-settings>=2.6.0" "httpx>=0.27.0" \
                "websockets>=13.0" "python-multipart>=0.0.12"

COPY app ./app

# Copy the built frontend into the image so the backend can serve it.
# Build the frontend first (cd frontend && npm ci && npm run build) then build
# the backend image from the repo root with the dist folder in place.
COPY dist ./static

EXPOSE 8092
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
