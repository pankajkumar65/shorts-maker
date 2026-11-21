"""Download, transcribe, and render helpers."""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import download_range_func

# Optional: YTDLP_COOKIES=/path/cookies.txt lets yt-dlp use a logged-in YouTube session. Servers in data centers are
# often asked to "confirm you're not a bot"; cookies from your own account are the usual workaround.
_YDL = yt_dlp.YoutubeDL
_cookie_copy = None


def _cookie_file():
    """Use a writable copy: yt-dlp saves cookies back on exit, and secret-file mounts are read-only."""
    global _cookie_copy
    src = os.getenv("YTDLP_COOKIES")
    if not src or not os.path.exists(src):
        return None
    if _cookie_copy is None:
        dst = Path(__file__).resolve().parent.parent / "work" / "bin" / "cookies.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        _cookie_copy = str(dst)
    return _cookie_copy


def _YoutubeDL(params=None, *args, **kwargs):
    params = dict(params or {})
    cf = _cookie_file()
    if cf:
        params.setdefault("cookiefile", cf)
    # YTDLP_PROXY=http://user:pass@host:port sends all YouTube traffic through a proxy in the right country.
    if os.getenv("YTDLP_PROXY"):
        params.setdefault("proxy", os.environ["YTDLP_PROXY"])
    return _YDL(params, *args, **kwargs)


yt_dlp.YoutubeDL = _YoutubeDL

def _ffmpeg_path() -> str:
    """yt-dlp only recognises a binary named ffmpeg[.exe], so copy the bundled one under that name."""
    src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if src.stem == "ffmpeg":
        return str(src)
    bin_dir = Path(__file__).resolve().parent.parent / "work" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dst = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not dst.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o755)
    return str(dst)


FFMPEG = _ffmpeg_path()
# yt-dlp's partial-download check looks for ffmpeg on PATH only.
os.environ["PATH"] = os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", "")
MAX_CLIP_SECONDS = 180

# Speed settings (set as environment variables to change):
#   ACCURATE_CUTS=1  -> frame-accurate cut points (slow: re-encodes the whole download). Default is fast keyframe cuts.
#   SHORTS_RES=1080  -> render 1080x1920 instead of 720x1280.
FAST_CUTS = os.getenv("ACCURATE_CUTS", "0") != "1"
OUT_W, OUT_H = (1080, 1920) if os.getenv("SHORTS_RES", "720") == "1080" else (720, 1280)

_whisper = None
_whisper_lock = threading.Lock()


_info_cache: dict[str, dict] = {}
_tr_cache: dict[str, list[dict]] = {}


def _slim_info(url: str) -> dict:
    """One yt-dlp lookup per URL; reused by Load, Suggest and caption fetching."""
    if url not in _info_cache:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        _info_cache[url] = {
            k: info.get(k)
            for k in ("id", "title", "duration", "thumbnail", "channel", "subtitles", "automatic_captions")
        }
    return _info_cache[url]


def video_info(url: str) -> dict:
    info = _slim_info(url)
    return {k: info.get(k) for k in ("id", "title", "duration", "thumbnail", "channel")}


def _fast_captions(info: dict) -> list[dict]:
    """Fetch the English caption track straight from its URL (no second yt-dlp run, no temp files)."""
    import requests

    for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        langs = [l for l in ("en", "en-orig") if l in source] + [l for l in source if l.startswith("en-") and l != "en-orig"]
        for lang in langs:
            for fmt in source[lang]:
                if fmt.get("ext") == "vtt" and fmt.get("url"):
                    proxy = os.getenv("YTDLP_PROXY")
                    r = requests.get(fmt["url"], timeout=15, proxies={"http": proxy, "https": proxy} if proxy else None)
                    r.raise_for_status()
                    tr = _parse_vtt_text(r.text)
                    if tr:
                        return tr
    return []


def prefetch_captions(url: str) -> None:
    """Warm the caption cache right after Load so Suggest clips is instant. Cheap path only, never Whisper."""
    try:
        if url not in _tr_cache:
            tr = _fast_captions(_slim_info(url))
            if tr:
                _tr_cache[url] = tr
    except Exception as e:
        print("Caption prefetch skipped:", e)


def quick_transcript(url: str) -> list[dict]:
    """Captions only (cached or direct fetch). Never falls back to Whisper, so it can't stall on long videos."""
    if url in _tr_cache:
        return _tr_cache[url]
    tr = _fast_captions(_slim_info(url))
    if tr:
        _tr_cache[url] = tr
    return tr


def download_section(url: str, start: float, end: float, out_dir: Path, fit: str = "blur") -> Path:
    """Download only [start, end] of the video as an mp4."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Blur layout shows the video at output width only, so 480p is plenty; crop layout needs more height.
    h = 480 if fit == "blur" else 720
    def attempt(fast: bool):
        # Fast cuts copy streams without re-encoding, so prefer codecs the mp4 container accepts (H.264 + AAC).
        # The accurate path re-encodes, so any codec works there.
        fmt = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
            if fast else ""
        ) + f"bv*[height<={h}]+ba/b[height<={h}]/b"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ffmpeg_location": os.path.dirname(FFMPEG),
            "format": fmt,
            "merge_output_format": "mp4",
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": not fast,
            "outtmpl": str(out_dir / "source.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:
        attempt(FAST_CUTS)
    except Exception as e:
        if not FAST_CUTS:
            raise
        print("Fast cut failed, retrying with accurate cuts:", e)
        for f in out_dir.glob("source.*"):
            f.unlink(missing_ok=True)
        attempt(False)
    files = sorted(out_dir.glob("source.*"))
    if not files:
        raise RuntimeError("Download produced no file")
    return files[0]


def _get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel(os.getenv("WHISPER_MODEL", "tiny"), compute_type="int8", cpu_threads=os.cpu_count() or 4)
    return _whisper


def transcribe(media: Path) -> list[dict]:
    """Return word-level segments: [{start, end, text}]."""
    import numpy as np

    # Decode with our own ffmpeg (16 kHz mono float32) so we don't depend on PyAV's decoder.
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("audio decode failed: " + proc.stderr.decode(errors="ignore")[-400:])
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    model = _get_whisper()
    segments, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True, beam_size=1, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"start": w.start, "end": w.end, "text": w.word.strip()})
    return words


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def write_srt(words: list[dict], path: Path, per_chunk: int = 3) -> bool:
    if not words:
        return False
    lines = []
    for i in range(0, len(words), per_chunk):
        chunk = words[i : i + per_chunk]
        text = " ".join(w["text"] for w in chunk).upper()
        lines.append(f"{i // per_chunk + 1}\n{_ts(chunk[0]['start'])} --> {_ts(chunk[-1]['end'])}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def render_vertical(src: Path, out: Path, fit: str, srt: Path | None) -> None:
    """Render 1080x1920. fit = 'crop' (center crop) or 'blur' (blurred background)."""
    W, H = OUT_W, OUT_H
    sw, sh = W // 10, H // 10
    if fit == "blur":
        vf = (
            "[0:v]split[a][b];"
            f"[a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur=3:2,scale={W}:{H}:flags=bilinear[bg];"
            f"[b]scale={W}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        vf = f"[0:v]scale=-2:{H},crop={W}:{H}"
    if srt:
        style = "FontName=Arial,FontSize=14,Bold=1,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,BorderStyle=1,Outline=3,Alignment=2,MarginV=260"
        vf += f",subtitles={srt.name}:force_style='{style}'"
    vf += "[v]"
    cmd = [
        FFMPEG, "-y", "-i", src.name,
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-threads", "0",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ]
    # Run inside the job dir so filter paths stay relative (avoids Windows path escaping).
    proc = subprocess.run(cmd, cwd=src.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr[-800:])


# ---------- AI highlight suggestions ----------

def fetch_transcript(url: str, work: Path) -> list[dict]:
    """Return [{start, text}]: cached/fast caption fetch first, then the slower yt-dlp + Whisper path."""
    if url in _tr_cache:
        return _tr_cache[url]
    try:
        tr = _fast_captions(_slim_info(url))
        if tr:
            _tr_cache[url] = tr
            return tr
    except Exception as e:
        print("Fast captions failed, using fallback:", e)
    tr = _fetch_transcript_slow(url, work)
    if tr:
        _tr_cache[url] = tr
    return tr


def _fetch_transcript_slow(url: str, work: Path) -> list[dict]:
    """Return [{start, text}] using YouTube captions if available, else Whisper on audio."""
    work.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "writeautomaticsub": True, "writesubtitles": True, "subtitleslangs": ["en", "en-orig"], "ignoreerrors": True,
        "subtitlesformat": "vtt", "outtmpl": str(work / "cap.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    vtts = list(work.glob("cap*.vtt"))
    if vtts:
        return _parse_vtt(vtts[0])
    # Fallback: audio -> whisper
    aopts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "ffmpeg_location": os.path.dirname(FFMPEG),
        "format": "ba[abr<=64]/ba/b", "outtmpl": str(work / "audio.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(aopts) as ydl:
        ydl.download([url])
    audio = next(work.glob("audio.*"))
    return [{"start": w["start"], "text": w["text"]} for w in transcribe(audio)]


_TIME = re.compile(r"(\d+):(\d+):(\d+)\.(\d+) -->")


def _parse_vtt(path: Path) -> list[dict]:
    return _parse_vtt_text(path.read_text(encoding="utf-8", errors="ignore"))


def _parse_vtt_text(raw: str) -> list[dict]:
    out, last_text, cur = [], None, None
    for line in raw.splitlines():
        m = _TIME.match(line)
        if m:
            h, mi, s, ms = map(int, m.groups())
            cur = h * 3600 + mi * 60 + s + ms / 1000
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if cur is not None and text and text != last_text and "-->" not in text and not text.startswith(("WEBVTT", "Kind:", "Language:")):
            out.append({"start": cur, "text": text})
            last_text = text
    return out


def suggest_clips(transcript: list[dict], duration: float, count: int = 5, length: int = 45) -> list[dict]:
    """Pick highlight windows. Uses Claude if ANTHROPIC_API_KEY is set, else a heuristic."""
    if not transcript:
        return []
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return _suggest_llm(transcript, duration, count, length)
        except Exception as e:  # fall back rather than fail the whole job
            print("LLM suggestion failed, using heuristic:", e)
    return _suggest_heuristic(transcript, duration, count, length)


def _suggest_heuristic(transcript, duration, count, length):
    # Rank non-overlapping windows by speech density plus excitement markers.
    hot = re.compile(r"\b(secret|never|always|biggest|mistake|best|worst|how to|why|amazing|crazy|important|truth)\b", re.I)
    step = 15
    scored = []
    t = 0.0
    while t + length <= (duration or t + length):
        chunk = [c for c in transcript if t <= c["start"] < t + length]
        score = len(chunk) + 3 * sum(bool(hot.search(c["text"])) for c in chunk) + 2 * sum("?" in c["text"] for c in chunk)
        scored.append((score, t))
        t += step
    scored.sort(reverse=True)
    picks = []
    for score, s in scored:
        if all(abs(s - p["start"]) >= length for p in picks):
            picks.append({"start": s, "end": s + length, "title": f"Highlight at {int(s // 60)}:{int(s % 60):02d}", "reason": "High speech density / hook words"})
        if len(picks) >= count:
            break
    return sorted(picks, key=lambda p: p["start"])


def _compact(transcript: list[dict], bucket: int = 12) -> str:
    """Merge caption lines into ~12s buckets so the prompt is several times smaller (faster, cheaper)."""
    out, cur, buf = [], None, []
    for c in transcript:
        b = int(c["start"] // bucket) * bucket
        if cur is None:
            cur = b
        if b != cur:
            out.append(f"[{cur}] " + " ".join(buf))
            cur, buf = b, []
        buf.append(c["text"])
    if buf:
        out.append(f"[{cur}] " + " ".join(buf))
    return "\n".join(out)


def _suggest_llm(transcript, duration, count, length):
    import json

    import anthropic

    lines = _compact(transcript)[:60000]
    prompt = (
        f"Below is a timestamped transcript (seconds) of a {int(duration or 0)}s YouTube video. "
        f"Pick the {count} best self-contained moments for YouTube Shorts, each {length - 15}-{length + 15} seconds, "
        "starting on a strong hook and ending at a natural stop. Clips must not overlap. "
        'Keep each title under 8 words and each reason under 12 words. '
        'Reply with ONLY a JSON array: [{"start": number, "end": number, "title": string, "reason": string}].\n\n' + lines
    )
    # Haiku is much faster than Sonnet for this picking task; set HIGHLIGHT_MODEL=claude-sonnet-5-5 for max quality.
    client = anthropic.Anthropic(timeout=45, max_retries=1)
    msg = client.messages.create(
        model=os.getenv("HIGHLIGHT_MODEL", "claude-haiku-5-5"), max_tokens=1000, messages=[{"role": "user", "content": prompt}]
    )
    text = msg.content[0].text
    data = json.loads(text[text.index("[") : text.rindex("]") + 1])
    clean = []
    for d in data:
        s, e = float(d["start"]), float(d["end"])
        e = min(e, s + MAX_CLIP_SECONDS)
        if duration:
            e = min(e, duration)
        if e - s >= 5:
            clean.append({"start": s, "end": e, "title": d.get("title", ""), "reason": d.get("reason", "")})
    return clean[:count]
