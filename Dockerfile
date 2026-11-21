# Shorts Maker: one container serves the website and the API on a single port.

# ---- Stage 1: build the frontend ----
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: backend + built frontend ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    WHISPER_MODEL=tiny

# Fonts so burned-in captions render (Liberation Sans stands in for Arial).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fontconfig fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to read YouTube reliably.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# Bake the caption model into the image so the first clip isn't slow.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', compute_type='int8')"

COPY backend/app ./app
COPY --from=web /web/dist ./static

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
