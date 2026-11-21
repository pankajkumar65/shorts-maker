# Shorts Maker

Paste a long YouTube URL, pick a segment (manually or via AI highlights), get a 9:16 MP4 with optional burned-in captions.

## Run

Backend (http://localhost:8000):

    cd backend
    .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

Frontend (http://localhost:5173):

    cd frontend
    npm run dev

## Setup from scratch

    cd backend
    uv venv --python 3.14 .venv
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt

    cd ../frontend
    npm install

## Config (backend/.env.example, set as environment variables)

- ANTHROPIC_API_KEY: optional, enables LLM-picked highlights (otherwise a heuristic is used)
- WHISPER_MODEL: tiny / base / small (default base), used for captions

## Deploy (Docker)

One container serves the website and the API on one port.

    docker build -t shorts-maker .
    docker run -p 8000:8000 -e APP_PASSWORD=choose-a-password shorts-maker

Open http://localhost:8000. On a host such as Render, Railway or Fly.io, point it at this repo's Dockerfile and set the variables below.

Server settings (environment variables):

- APP_PASSWORD: asks for this password in the browser (any username). Set it on any public server.
- ANTHROPIC_API_KEY: optional, enables AI-picked highlights. HIGHLIGHT_MODEL changes the model.
- WHISPER_MODEL: tiny (default in the image) / base / small
- YTDLP_COOKIES: optional path to a cookies.txt file, for when YouTube blocks the server's address
- ACCURATE_CUTS=1: frame-accurate cut points (slower). SHORTS_RES=1080: render 1080x1920.

Use at least 2 CPUs and 2 GB of memory. Finished clips are deleted after 6 hours.

Only clip videos you own or have permission to reuse.
