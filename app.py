import os
import re
import uuid
import shutil
import asyncio
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ClipAI Mobile", version="0.1.0")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

JOBS = {}
MODEL = None

def run_cmd(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or "Media processing failed")
    return result

def get_duration(path: Path) -> float:
    result = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("Could not read video duration.")

def get_model():
    global MODEL
    if MODEL is None:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "base")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        MODEL = WhisperModel(model_name, device=device, compute_type=compute_type)
    return MODEL

def transcribe(path: Path, language: str):
    model = get_model()
    lang = None if language == "auto" else language
    segments, info = model.transcribe(
        str(path), language=lang, beam_size=3, vad_filter=True,
        word_timestamps=False
    )
    rows = []
    for seg in segments:
        txt = (seg.text or "").strip()
        if txt:
            rows.append({"start": float(seg.start), "end": float(seg.end), "text": txt})
    return rows, getattr(info, "language", None)

def viral_score(text: str, duration: float) -> float:
    """MVP scoring: transcript hooks, emotional words, questions, and speech density."""
    low = text.lower()
    score = 0.0
    hooks = [
        "लेकिन", "क्योंकि", "कैसे", "कभी", "सच", "गलती", "ध्यान", "जरूरी",
        "secret", "mistake", "why", "how", "best", "worst", "never", "always",
        "shocking", "amazing", "important", "problem", "solution", "truth",
        "लेकिन", "सोचो", "याद", "मत", "देखो", "सुनो", "समझो"
    ]
    emotion = [
        "amazing", "love", "hate", "crazy", "insane", "wow", "great", "fail",
        "जीत", "हार", "प्यार", "कमाल", "खतरनाक", "जबरदस्त", "शानदार",
        "मुश्किल", "आसान", "गलत", "सही", "हैरान"
    ]
    score += min(3.0, sum(1 for w in hooks if w in low) * 0.55)
    score += min(2.0, sum(1 for w in emotion if w in low) * 0.45)
    score += min(1.2, text.count("?") * 0.4 + text.count("!") * 0.25)
    words = len(re.findall(r"\S+", text))
    density = words / max(duration, 1)
    score += min(1.5, density * 0.12)
    if 15 <= duration <= 45:
        score += 0.8
    if len(text) > 80:
        score += 0.4
    return round(score, 2)

def build_candidates(segments, duration, min_s, max_s):
    if not segments:
        return []
    target = min(max_s, max(min_s, 30))
    starts = []
    next_allowed = -1.0
    for seg in segments:
        if seg["start"] >= next_allowed:
            starts.append(seg["start"])
            next_allowed = seg["start"] + max(7.0, target * 0.35)

    candidates = []
    for start in starts:
        end_limit = min(duration, start + max_s)
        chosen = [s for s in segments if s["end"] > start and s["start"] < end_limit]
        if not chosen:
            continue
        end = min(end_limit, max(s["end"] for s in chosen))
        actual = end - start
        if actual < min_s:
            continue
        text = " ".join(s["text"] for s in chosen)
        candidates.append({
            "start": max(0.0, start), "end": end, "duration": actual,
            "text": text, "score": viral_score(text, actual), "segments": chosen
        })

    # Prefer high-scoring clips and avoid returning almost identical sections.
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for c in candidates:
        overlaps = False
        for old in selected:
            intersection = max(0.0, min(c["end"], old["end"]) - max(c["start"], old["start"]))
            smaller = min(c["duration"], old["duration"])
            if smaller > 0 and intersection / smaller > 0.45:
                overlaps = True
                break
        if not overlaps:
            selected.append(c)
        if len(selected) >= 5:
            break
    return sorted(selected, key=lambda x: x["start"])

def emoji_for(text: str) -> str:
    t = text.lower()
    mapping = [
        (["love", "प्यार", "दिल"], " ❤️"),
        (["money", "पैसा", "कमाई", "rich"], " 💰"),
        (["secret", "राज", "सच"], " 🤫"),
        (["wow", "amazing", "कमाल", "जबरदस्त"], " 🔥"),
        (["problem", "मुश्किल", "गलती", "mistake"], " ⚠️"),
        (["win", "जीत", "success", "सफल"], " 🏆"),
        (["question", "क्यों", "कैसे", "why", "how"], " 🤔"),
        (["time", "समय"], " ⏳"),
    ]
    for words, emoji in mapping:
        if any(w in t for w in words):
            return emoji
    return ""

def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"

def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")

def make_ass(clip, workdir: Path, style: str):
    if style == "neon":
        primary, outline, back = "&H00FFFF&", "&H00101010&", "&H80000000&"
    elif style == "clean":
        primary, outline, back = "&H00FFFFFF&", "&H00000000&", "&H70000000&"
    else:
        primary, outline, back = "&H0000FFFF&", "&H00000000&", "&H70000000&"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default, Noto Sans, 62,{primary},&H000000FF&,{outline},{back},-1,0,1,5,2,2,70,70,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for seg in clip["segments"]:
        start = max(0.0, seg["start"] - clip["start"])
        end = max(start + 0.25, seg["end"] - clip["start"])
        txt = (seg["text"] + emoji_for(seg["text"])).strip()
        txt = textwrap.fill(txt, width=34)
        txt = ass_escape(txt).replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{txt}\n")
    ass_path = workdir / "captions.ass"
    ass_path.write_text("".join(lines), encoding="utf-8")
    return ass_path

def render_clip(source: Path, clip, index: int, job_id: str, style: str):
    workdir = TEMP_DIR / f"{job_id}_{index}"
    workdir.mkdir(parents=True, exist_ok=True)
    ass_path = make_ass(clip, workdir, style)
    filename = f"{job_id}_clip_{index}.mp4"
    output = OUTPUT_DIR / filename

    # 9:16 centre crop, 1080x1920 output. Punchy/neon styles add a mild colour boost.
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if style in ("punchy", "neon"):
        vf += ",eq=saturation=1.15:contrast=1.05"
    vf += ",subtitles=captions.ass"
    run_cmd([
        "ffmpeg", "-y", "-ss", f"{clip['start']:.3f}", "-i", str(source),
        "-t", f"{clip['duration']:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(output)
    ], cwd=str(workdir))
    shutil.rmtree(workdir, ignore_errors=True)
    return {
        "url": f"/outputs/{filename}",
        "start": round(clip["start"], 1),
        "duration": round(clip["duration"], 1),
        "score": clip["score"],
        "title": f"AI Clip {index}"
    }

async def process_job(job_id: str, source: Path, language: str, min_s: int, max_s: int, max_clips: int, style: str):
    try:
        JOBS[job_id]["status"] = "transcribing"
        duration = get_duration(source)
        segments, detected_language = await asyncio.to_thread(transcribe, source, language)
        if not segments:
            raise RuntimeError("Speech was not detected. Try a video with clear spoken audio.")
        JOBS[job_id]["status"] = "finding_highlights"
        candidates = build_candidates(segments, duration, min_s, max_s)
        if not candidates:
            # If the video is shorter than the selected minimum, use the full spoken section.
            start = segments[0]["start"]
            end = min(duration, segments[-1]["end"])
            if end - start >= 5:
                txt = " ".join(s["text"] for s in segments)
                candidates = [{
                    "start": start, "end": end, "duration": end-start,
                    "text": txt, "score": viral_score(txt, end-start), "segments": segments
                }]
        candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)[:max_clips]
        if not candidates:
            raise RuntimeError("Could not find a suitable clip. Try a longer video.")
        results = []
        for i, clip in enumerate(candidates, 1):
            JOBS[job_id]["status"] = f"rendering_{i}_of_{len(candidates)}"
            results.append(await asyncio.to_thread(render_clip, source, clip, i, job_id, style))
        JOBS[job_id].update({
            "status": "completed", "clips": results,
            "detected_language": detected_language or "unknown",
            "message": "Your clips are ready."
        })
    except Exception as exc:
        JOBS[job_id].update({"status": "failed", "error": str(exc)})
    finally:
        try:
            source.unlink(missing_ok=True)
        except Exception:
            pass

@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "app": "ClipAI Mobile"}

@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    language: str = Form("auto"),
    min_seconds: int = Form(15),
    max_seconds: int = Form(45),
    max_clips: int = Form(3),
    caption_style: str = Form("punchy"),
):
    if not video.filename:
        raise HTTPException(400, "Please choose a video.")
    ext = Path(video.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        raise HTTPException(400, "Supported formats: MP4, MOV, M4V, WEBM, MKV.")
    if not (5 <= min_seconds <= 90 and 10 <= max_seconds <= 120 and min_seconds <= max_seconds):
        raise HTTPException(400, "Choose a valid clip duration.")
    if not (1 <= max_clips <= 5):
        raise HTTPException(400, "Choose between 1 and 5 clips.")
    if language not in {"auto", "hi", "en"}:
        raise HTTPException(400, "Invalid language.")
    if caption_style not in {"punchy", "clean", "neon"}:
        raise HTTPException(400, "Invalid caption style.")

    job_id = uuid.uuid4().hex[:12]
    source = TEMP_DIR / f"{job_id}{ext}"
    with source.open("wb") as f:
        while True:
            chunk = await video.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    JOBS[job_id] = {"status": "queued", "clips": []}
    background_tasks.add_task(process_job, job_id, source, language, min_seconds, max_seconds, max_clips, caption_style)
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found. It may have expired after a server restart.")
    return job
