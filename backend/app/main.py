import base64
import os
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import media

WORK = Path(__file__).resolve().parent.parent / "work"
WORK.mkdir(exist_ok=True)

app = FastAPI(title="Shorts Maker")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

# Optional password for a public deployment: set APP_PASSWORD and the browser asks for it once (any username).
APP_PASSWORD = os.getenv("APP_PASSWORD")

if APP_PASSWORD:

    @app.middleware("http")
    async def require_password(request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw.encode(), APP_PASSWORD.encode())
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Shorts Maker"'})
        return await call_next(request)

pool = ThreadPoolExecutor(max_workers=int(os.getenv("MAX_WORKERS", "2")))

# Load the Whisper model in the background at startup so the first clip doesn't wait for it.
def _warm_whisper():
    try:
        media._get_whisper()
    except Exception as e:
        print("Whisper preload skipped:", e)


if os.getenv("WARM_WHISPER", "1") == "1":  # set WARM_WHISPER=0 on low-memory hosts: the model then loads on first use
    threading.Thread(target=_warm_whisper, daemon=True).start()


# Delete finished clips after a few hours so a long-running server doesn't fill its disk.
def _cleanup_loop(max_age_hours: float = 6):
    while True:
        time.sleep(1800)
        cutoff = time.time() - max_age_hours * 3600
        for p in WORK.iterdir():
            if p.name == "bin":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except Exception:
                pass


threading.Thread(target=_cleanup_loop, daemon=True).start()
jobs: dict[str, dict] = {}
lock = threading.Lock()


def _set(job_id: str, **kw):
    with lock:
        jobs[job_id].update(kw)


class UrlIn(BaseModel):
    url: str


class SuggestIn(UrlIn):
    count: int = Field(5, ge=1, le=10)
    length: int = Field(45, ge=15, le=120)


class BatchIn(UrlIn):
    count: int = Field(10, ge=1, le=20)
    length: int = Field(30, ge=10, le=60)
    fit: str = Field("blur", pattern="^(crop|blur)$")
    captions: bool = True


class ClipIn(UrlIn):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    fit: str = Field("blur", pattern="^(crop|blur)$")
    captions: bool = True


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/info")
def info(body: UrlIn):
    try:
        meta = media.video_info(body.url)
        threading.Thread(target=media.prefetch_captions, args=(body.url,), daemon=True).start()
        return meta
    except Exception as e:
        raise HTTPException(400, f"Could not read video: {e}")


def _new_job(kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with lock:
        jobs[job_id] = {"id": job_id, "kind": kind, "status": "queued", "step": "Queued", "error": None, "result": None}
    return job_id


@app.post("/api/suggest")
def suggest(body: SuggestIn):
    job_id = _new_job("suggest")

    def run():
        try:
            _set(job_id, status="running", step="Reading video")
            meta = media.video_info(body.url)
            _set(job_id, step="Getting transcript")
            tr = media.fetch_transcript(body.url, WORK / job_id)
            _set(job_id, step="Picking highlights")
            clips = media.suggest_clips(tr, meta.get("duration") or 0, body.count, body.length)
            _set(job_id, status="done", step="Done", result={"clips": clips})
        except Exception as e:
            _set(job_id, status="error", step="Failed", error=str(e))
        finally:
            shutil.rmtree(WORK / job_id, ignore_errors=True)

    pool.submit(run)
    return {"id": job_id}


@app.post("/api/clips")
def create_clip(body: ClipIn):
    if body.end <= body.start:
        raise HTTPException(400, "End must be after start")
    if body.end - body.start > media.MAX_CLIP_SECONDS:
        raise HTTPException(400, f"Clip can be at most {media.MAX_CLIP_SECONDS}s")
    return {"id": _submit_clip(body.url, body.start, body.end, body.fit, body.captions)}


def _submit_clip(url: str, start: float, end: float, fit: str, captions: bool) -> str:
    """Queue one clip job (used by single clips and by batches) and return its job id."""
    job_id = _new_job("clip")

    def run():
        d = WORK / job_id
        try:
            timings = {"download": 0.0, "captions": 0.0, "render": 0.0}
            t = time.time()
            _set(job_id, status="running", step="Downloading section")
            src = media.download_section(url, start, end, d, fit)
            timings["download"] = time.time() - t
            srt = None
            if captions:
                t = time.time()
                _set(job_id, step="Transcribing for captions")
                words = media.transcribe(src)
                srt_path = d / "captions.srt"
                if media.write_srt(words, srt_path):
                    srt = srt_path
                timings["captions"] = time.time() - t
            t = time.time()
            _set(job_id, step="Rendering 9:16")
            media.render_vertical(src, d / "short.mp4", fit, srt)
            timings["render"] = time.time() - t
            timings = {k: round(v, 1) for k, v in timings.items()}
            print(f"[clip {job_id}] download {timings['download']}s, captions {timings['captions']}s, render {timings['render']}s")
            _set(job_id, status="done", step="Done", result={"file": f"/api/clips/{job_id}/file", "timings": timings})
        except Exception as e:
            _set(job_id, status="error", step="Failed", error=str(e))

    pool.submit(run)
    return job_id


def _plan_windows(picks: list[dict], count: int, length: int, duration: float) -> list[tuple[float, float]]:
    """Exactly `length`-second, non-overlapping windows: AI picks first, then gaps filled from the start."""
    out: list[tuple[float, float]] = []
    for c in sorted(picks, key=lambda c: c["start"]):
        s = max(0.0, float(c["start"]))
        e = s + length
        if duration and e > duration:
            e = float(duration)
            s = max(0.0, e - length)
        if all(e <= a or s >= b for a, b in out):
            out.append((s, e))
    t = 0.0
    while len(out) < count and (not duration or t + length <= duration):
        if all(t + length <= a or t >= b for a, b in out):
            out.append((t, t + length))
        t += length
    if not out and duration:
        out.append((0.0, min(float(duration), float(length))))
    return sorted(out)[:count]


@app.post("/api/batch")
def create_batch(body: BatchIn):
    """One click: pick the best moments and make a Short from each."""
    job_id = _new_job("batch")
    _set(job_id, clips=[])

    def snapshot(children, windows):
        out = []
        for i, (cid, (s, e)) in enumerate(zip(children, windows), start=1):
            with lock:
                j = dict(jobs[cid])
            out.append({
                "id": cid, "index": i, "start": s, "end": e,
                "status": j["status"], "step": j["step"], "error": j["error"],
                "file": (j["result"] or {}).get("file"),
            })
        return out

    def run():
        try:
            _set(job_id, status="running", step="Finding the best moments")
            duration = media.video_info(body.url).get("duration") or 0
            try:
                picks = media.suggest_clips(media.quick_transcript(body.url), duration, body.count, body.length)
            except Exception as e:
                print("Batch highlight search failed, splitting evenly:", e)
                picks = []
            windows = _plan_windows(picks, body.count, body.length, duration)
            if not windows:
                raise RuntimeError("Could not plan any clips for this video")
            children = [_submit_clip(body.url, s, e, body.fit, body.captions) for s, e in windows]
            _set(job_id, step="Creating clips", clips=snapshot(children, windows))
            while True:
                snap = snapshot(children, windows)
                _set(job_id, clips=snap)
                if all(c["status"] in ("done", "error") for c in snap):
                    break
                time.sleep(0.5)
            _set(job_id, status="done", step="Done", result={"clips": snap})
        except Exception as e:
            _set(job_id, status="error", step="Failed", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return {"id": job_id}


@app.get("/api/batch/{job_id}/zip")
def batch_zip(job_id: str):
    j = jobs.get(job_id)
    if not j or j["kind"] != "batch" or j["status"] != "done":
        raise HTTPException(404, "Not ready")
    zpath = WORK / f"{job_id}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for c in j["result"]["clips"]:
            p = WORK / c["id"] / "short.mp4"
            if c["status"] == "done" and p.exists():
                z.write(p, f"short-{c['index']:02d}.mp4")
    return FileResponse(zpath, media_type="application/zip", filename="shorts.zip")


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    return j


@app.get("/api/clips/{job_id}/file")
def clip_file(job_id: str):
    path = WORK / job_id / "short.mp4"
    if not job_id.isalnum() or not path.exists():
        raise HTTPException(404, "Not ready")
    return FileResponse(path, media_type="video/mp4", filename="short.mp4")


# In production the built frontend sits in ./static and is served from the same port. Must stay last.
STATIC = Path(__file__).resolve().parent.parent / "static"
if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="web")
