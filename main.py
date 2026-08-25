"""
ReelForge AI — MVP
Бэкенд: загрузка видео, AI-нарезка, генерация субтитров, создание Reels
"""

import os
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
import openai
from dotenv import load_dotenv

# Загружаем env
load_dotenv()

# Конфигурация
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("output")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Инициализация OpenAI
if OPENAI_API_KEY:
    openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None

app = FastAPI(title="ReelForge AI", version="1.0.0")

# Статические файлы
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

# ============ МОДЕЛИ ДАННЫХ ============

class VideoJob(BaseModel):
    job_id: str
    status: str
    original_filename: str
    created_at: str
    reels: list = []
    error: Optional[str] = None

# Хранилище задач (в MVP — в памяти)
jobs = {}

# ============ ФРОНТЕНД ============

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/result/{job_id}", response_class=HTMLResponse)
async def result_page(request: Request, job_id: str):
    return templates.TemplateResponse("result.html", {"request": request, "job_id": job_id})

# ============ API ENDPOINTS ============

@app.post("/api/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    num_reels: int = Form(5),
    language: str = Form("ru")
):
    job_id = str(uuid.uuid4())[:8]
    
    allowed = {'.mp4', '.mov', '.avi', '.mkv', '.webm'}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported format: {ext}")
    
    upload_path = UPLOAD_DIR / f"{job_id}{ext}"
    with open(upload_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    jobs[job_id] = VideoJob(
        job_id=job_id,
        status="pending",
        original_filename=file.filename,
        created_at=datetime.now().isoformat(),
        reels=[]
    )
    
    background_tasks.add_task(process_video, job_id, str(upload_path), num_reels, language)
    
    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Video uploaded. Processing started.",
        "check_url": f"/api/status/{job_id}"
    }

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    
    job = jobs[job_id]
    response = {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "reels": job.reels
    }
    if job.error:
        response["error"] = job.error
    
    return response

@app.get("/api/download/{job_id}/{reel_index}")
async def download_reel(job_id: str, reel_index: int):
    reel_path = OUTPUT_DIR / f"{job_id}_reel_{reel_index}.mp4"
    if not reel_path.exists():
        raise HTTPException(404, "Reel not found")
    
    return FileResponse(
        path=str(reel_path),
        filename=f"reelforge_reel_{reel_index}.mp4",
        media_type="video/mp4"
    )


# ============ ОБРАБОТКА ВИДЕО ============

def process_video(job_id: str, video_path: str, num_reels: int, language: str):
    try:
        jobs[job_id].status = "processing"
        print(f"[{job_id}] Starting processing: {video_path}")
        
        duration = get_video_duration(video_path)
        print(f"[{job_id}] Duration: {duration}s")
        
        audio_path = str(UPLOAD_DIR / f"{job_id}_audio.wav")
        extract_audio(video_path, audio_path)
        print(f"[{job_id}] Audio extracted")
        
        if openai_client:
            segments = transcribe_with_whisper(audio_path, language)
        else:
            segments = create_dummy_segments(duration, num_reels)
        
        print(f"[{job_id}] Transcription: {len(segments)} segments")
        
        best_moments = find_best_moments(segments, num_reels, duration)
        print(f"[{job_id}] Best moments: {len(best_moments)}")
        
        reels = []
        for i, moment in enumerate(best_moments):
            reel_path = OUTPUT_DIR / f"{job_id}_reel_{i}.mp4"
            cut_video_segment(video_path, str(reel_path), moment["start"], moment["end"])
            
            hook = generate_hook(moment["text"])
            reels.append({
                "index": i,
                "start": moment["start"],
                "end": moment["end"],
                "text": moment["text"][:100] + "..." if len(moment["text"]) > 100 else moment["text"],
                "download_url": f"/api/download/{job_id}/{i}",
                "hook": hook
            })
        
        jobs[job_id].status = "completed"
        jobs[job_id].reels = reels
        print(f"[{job_id}] Done! {len(reels)} Reels created")
        
    except Exception as e:
        jobs[job_id].status = "failed"
        jobs[job_id].error = str(e)
        print(f"[{job_id}] Error: {e}")

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def get_video_duration(video_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())

def extract_audio(video_path: str, audio_path: str):
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
           audio_path]
    subprocess.run(cmd, capture_output=True)

def transcribe_with_whisper(audio_path: str, language: str) -> list:
    with open(audio_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=language,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
    
    segments = []
    for seg in transcript.segments:
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip()
        })
    return segments

def create_dummy_segments(duration: float, num_reels: int) -> list:
    segment_length = min(60, duration / num_reels)
    segments = []
    for i in range(num_reels):
        start = i * segment_length
        end = min(start + segment_length, duration)
        segments.append({
            "start": start,
            "end": end,
            "text": f"Segment {i+1}: {start:.1f}s - {end:.1f}s"
        })
    return segments

def find_best_moments(segments: list, num_reels: int, total_duration: float) -> list:
    scored = []
    for seg in segments:
        duration = seg["end"] - seg["start"]
        if duration < 5:
            continue
        text_density = len(seg["text"]) / max(duration, 1)
        scored.append({
            **seg,
            "score": text_density,
            "duration": duration
        })
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    
    selected = []
    used_ranges = []
    
    for seg in scored:
        if len(selected) >= num_reels:
            break
        
        overlaps = False
        for used in used_ranges:
            if not (seg["end"] <= used[0] or seg["start"] >= used[1]):
                overlaps = True
                break
        
        if not overlaps and seg["duration"] >= 15 and seg["duration"] <= 90:
            hook = generate_hook(seg["text"])
            selected.append({
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "hook": hook
            })
            used_ranges.append((seg["start"], seg["end"]))
    
    if len(selected) < num_reels:
        step = total_duration / num_reels
        for i in range(num_reels - len(selected)):
            start = i * step
            end = min(start + 30, total_duration)
            selected.append({
                "start": start,
                "end": end,
                "text": "",
                "hook": "Watch till the end! 👇"
            })
    
    return selected[:num_reels]

def generate_hook(text: str) -> str:
    if not openai_client or len(text) < 10:
        hooks = [
            "This changes everything... 🔥",
            "Nobody talks about this 👀",
            "Secret they hide 💡",
            "Watch till the end! 👇",
            "I was shocked 😱"
        ]
        import random
        return random.choice(hooks)
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You create catchy hooks for Instagram Reels. A hook is 3-7 words that make the viewer watch till the end. Use emojis. Reply with only the hook, nothing else."},
                {"role": "user", "content": f"Create a hook for this text: {text[:200]}"}
            ],
            max_tokens=30
        )
        return response.choices[0].message.content.strip()
    except:
        return "Watch till the end! 👇"

def cut_video_segment(input_path: str, output_path: str, start: float, end: float):
    duration = end - start
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
           "-i", input_path, "-c", "copy", output_path]
    subprocess.run(cmd, capture_output=True)

# ============ ЗАПУСК ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

