import os
import re
import json
import subprocess
import uuid
import glob
import shutil
import threading
import time
from flask import Flask, request, render_template_string, send_from_directory

from access_middleware import check_access, set_access_cookie
app = Flask(__name__)

@app.before_request
def require_access():
    response = check_access()
    if response:
        return response

@app.after_request
def apply_access_cookie(response):
    return set_access_cookie(response)

UPLOAD_DIR = "/tmp/uploads"
OUTPUT_DIR = "/tmp/outputs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

_groq_client = None
_font_path = None

JOBS = {}
JOBS_LOCK = threading.Lock()

def set_job(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {})
        JOBS[job_id].update(kwargs)

def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))

def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is not set")
        _groq_client = Groq(api_key=api_key)
    return _groq_client

def get_font_path():
    global _font_path
    if _font_path is not None:
        return _font_path
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "DejaVu Sans"],
            capture_output=True, text=True, timeout=5
        )
        path = result.stdout.strip()
        if path and os.path.exists(path):
            _font_path = path
            return _font_path
    except Exception:
        pass
    _font_path = ""
    return _font_path

INDEX_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ReelForge AI — Создание Reels</title>

<style>
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
    margin:0;
    font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
    background:
      radial-gradient(circle at 10% 0%,rgba(168,85,247,.18),transparent 32%),
      radial-gradient(circle at 90% 10%,rgba(236,72,153,.14),transparent 30%),
      #07070d;
    color:#fff;
    min-height:100vh;
}
.container{max-width:900px;margin:auto;padding:20px}
.header{
    display:flex;align-items:center;justify-content:space-between;
    padding:10px 0 35px;
}
.logo{
    font-size:25px;font-weight:900;
    background:linear-gradient(90deg,#a855f7,#ec4899);
    -webkit-background-clip:text;background-clip:text;color:transparent;
}
.badge{
    padding:7px 12px;border-radius:999px;
    background:rgba(168,85,247,.12);
    border:1px solid rgba(168,85,247,.25);
    color:#c084fc;font-size:12px;
}
.hero{text-align:center;padding:25px 0 35px}
.hero h1{
    font-size:clamp(38px,8vw,68px);
    line-height:1.02;margin:0 0 18px;font-weight:900;
}
.gradient{
    background:linear-gradient(90deg,#a855f7,#ec4899);
    -webkit-background-clip:text;background-clip:text;color:transparent;
}
.hero p{color:#9ca3af;font-size:17px;line-height:1.6;max-width:650px;margin:0 auto}
.free{
    display:inline-block;margin-bottom:20px;padding:8px 15px;
    border-radius:999px;background:rgba(168,85,247,.10);
    border:1px solid rgba(168,85,247,.25);color:#d8b4fe;font-size:14px;
}
.card{
    background:rgba(17,17,27,.82);
    border:1px solid rgba(255,255,255,.08);
    border-radius:24px;padding:25px;margin:18px 0;
    box-shadow:0 20px 60px rgba(0,0,0,.28);
    backdrop-filter:blur(14px);
}
.card h2{margin:0 0 8px;font-size:21px}
.card-desc{margin:0 0 20px;color:#8b8f9b;font-size:14px;line-height:1.5}
label{display:block;margin:18px 0 8px;font-weight:700;font-size:14px}
textarea{
    width:100%;min-height:105px;resize:vertical;
    background:#0b0b13;color:#fff;border:1px solid #252533;
    border-radius:14px;padding:15px;font-size:15px;outline:none;
    transition:.2s;
}
textarea:focus{border-color:#a855f7;box-shadow:0 0 0 3px rgba(168,85,247,.12)}
.file{
    width:100%;padding:14px;border:1px dashed #39394a;
    border-radius:14px;background:#0b0b13;color:#9ca3af;
}
input[type=file]::file-selector-button{
    background:linear-gradient(90deg,#7c3aed,#db2777);
    color:#fff;border:0;border-radius:9px;padding:9px 13px;
    margin-right:10px;font-weight:700;
}
.btn{
    width:100%;border:0;border-radius:14px;padding:15px;
    margin-top:20px;color:white;font-size:16px;font-weight:800;
    cursor:pointer;
    background:linear-gradient(90deg,#9333ea,#ec4899);
    box-shadow:0 10px 30px rgba(168,85,247,.22);
    transition:.2s;
}
.btn:hover{transform:translateY(-1px);box-shadow:0 14px 35px rgba(168,85,247,.3)}
.btn:disabled{opacity:.65;cursor:wait;transform:none}
.divider{height:1px;background:rgba(255,255,255,.07);margin:30px 0}
.features{
    display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:25px
}
.feature{
    text-align:center;padding:16px 10px;border-radius:16px;
    background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.06);
}
.feature b{display:block;font-size:18px;margin-bottom:5px}
.feature span{color:#858997;font-size:12px}
.footer{text-align:center;color:#555967;font-size:12px;padding:35px 0}

.overlay{
    display:none;position:fixed;inset:0;z-index:9999;
    background:rgba(4,4,9,.94);backdrop-filter:blur(10px);
    align-items:center;justify-content:center;padding:25px;
}
.loader-box{
    width:min(440px,100%);text-align:center;
    background:#11111b;border:1px solid #29293a;border-radius:26px;
    padding:30px;box-shadow:0 25px 80px rgba(0,0,0,.55);
}
.spinner{
    width:58px;height:58px;margin:0 auto 20px;
    border:5px solid #29293a;border-top-color:#c026d3;
    border-right-color:#9333ea;border-radius:50%;
    animation:spin .8s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.loader-box h2{margin:0 0 8px}
.loader-box p{color:#8b8f9b;margin:0 0 20px}
.timer{font-size:28px;font-weight:900;margin:15px 0}
.progress{
    height:8px;background:#252533;border-radius:99px;overflow:hidden;margin:20px 0
}
.progress-bar{
    width:15%;height:100%;
    background:linear-gradient(90deg,#9333ea,#ec4899);
    border-radius:99px;animation:progress 12s ease-in-out infinite;
}
@keyframes progress{
    0%{width:10%}35%{width:38%}65%{width:67%}90%{width:88%}100%{width:94%}
}
.steps{text-align:left;margin-top:20px}
.step{
    display:flex;gap:10px;align-items:center;
    color:#555967;padding:8px 0;font-size:14px;
}
.step.active{color:#fff}
.dot{
    width:9px;height:9px;border-radius:50%;background:#343444;flex:none
}
.step.active .dot{background:#c026d3;box-shadow:0 0 12px #c026d3}

@media(max-width:600px){
    .container{padding:14px}
    .header{padding-bottom:20px}
    .hero{padding:20px 0}
    .hero h1{font-size:43px}
    .hero p{font-size:15px}
    .card{padding:19px;border-radius:20px}
    .features{grid-template-columns:1fr}
}
</style>
</head>

<body>

<div class="container">

<header class="header">
    <div class="logo">ReelForge AI</div>
    <div class="badge">⚡ AI VIDEO</div>
</header>

<section class="hero">
    <div class="free">🚀 3 видео бесплатно — без карты</div>
    <h1>Создавай <span class="gradient">Reels</span><br>за минуты</h1>
    <p>Генерируй сценарии и собирай вертикальные видео для TikTok, Instagram Reels и YouTube Shorts.</p>

    <div class="features">
        <div class="feature"><b>🤖 AI</b><span>Умный сценарий</span></div>
        <div class="feature"><b>🎬 9:16</b><span>Формат Reels</span></div>
        <div class="feature"><b>⚡ Быстро</b><span>Автоматический монтаж</span></div>
    </div>
</section>

<div class="card">
    <h2>🧠 Сгенерировать сценарий</h2>
    <p class="card-desc">Введи тему — AI подготовит готовый сценарий для короткого ролика.</p>

    <form action="/generate" method="post">
        <textarea name="topic" placeholder="Например: 5 лайфхаков для продуктивности..."></textarea>
        <button class="btn" type="submit">✨ Сгенерировать сценарий</button>
    </form>
</div>

<div class="card">
    <h2>📸 Reels из скриншотов</h2>
    <p class="card-desc">Загрузи изображения — ReelForge добавит субтитры, монтаж и музыку.</p>

    <form action="/create_video" method="post" enctype="multipart/form-data">

        <label>🎯 Тема ролика</label>
        <textarea name="topic" placeholder="Например: Как пользоваться нашим приложением"></textarea>

        <label>🖼️ Скриншоты</label>
        <input class="file" type="file" name="images" multiple accept="image/*">

        <label>🎵 Музыка <span style="color:#666">(необязательно)</span></label>
        <input class="file" type="file" name="music" accept="audio/*">

        <button class="btn" type="submit">🎬 Собрать Reels из скриншотов</button>
    </form>
</div>

<div class="card">
    <h2>🎥 Reels из видео</h2>
    <p class="card-desc">Загрузи несколько видео — система сама выберет лучшие моменты и соберёт ролик.</p>

    <form action="/create_reel_from_videos" method="post" enctype="multipart/form-data">

        <label>🎯 Тема ролика</label>
        <textarea name="topic" placeholder="Например: Обзор продукта за 30 секунд"></textarea>

        <label>🎞️ Видео-куски</label>
        <input class="file" type="file" name="videos" multiple accept="video/*">

        <label>🎵 Музыка <span style="color:#666">(необязательно)</span></label>
        <input class="file" type="file" name="music" accept="audio/*">

        <button class="btn" type="submit">🚀 Собрать Reels из видео</button>
    </form>
</div>

<div class="footer">
    ReelForge AI • Создание коротких видео с помощью AI
</div>

</div>

<div class="overlay" id="loading">
    <div class="loader-box">
        <div class="spinner"></div>
        <h2>Создаём твой Reels 🚀</h2>
        <p id="statusText">Подготавливаем файлы...</p>

        <div class="timer" id="timer">00:00</div>

        <div class="progress">
            <div class="progress-bar"></div>
        </div>

        <div class="steps">
            <div class="step active" id="s1"><span class="dot"></span> Загружаем материалы</div>
            <div class="step" id="s2"><span class="dot"></span> Анализируем контент</div>
            <div class="step" id="s3"><span class="dot"></span> Создаём сценарий</div>
            <div class="step" id="s4"><span class="dot"></span> Монтируем видео</div>
            <div class="step" id="s5"><span class="dot"></span> Финальный рендер</div>
        </div>
    </div>
</div>

<script>
document.querySelectorAll("form").forEach(form=>{
    form.addEventListener("submit",()=>{
        const btn=form.querySelector("button");
        if(btn){
            btn.disabled=true;
            btn.innerHTML="⏳ Создаём видео...";
        }

        document.getElementById("loading").style.display="flex";

        let seconds=0;
        const timer=document.getElementById("timer");
        const status=document.getElementById("statusText");

        setInterval(()=>{
            seconds++;
            const m=String(Math.floor(seconds/60)).padStart(2,"0");
            const s=String(seconds%60).padStart(2,"0");
            timer.textContent=m+":"+s;
        },1000);

        const messages=[
            ["s1","Загружаем материалы..."],
            ["s2","Анализируем контент..."],
            ["s3","Создаём сценарий..."],
            ["s4","Монтируем видео..."],
            ["s5","Финальный рендер..."]
        ];

        messages.forEach((item,i)=>{
            setTimeout(()=>{
                document.querySelectorAll(".step").forEach(x=>x.classList.remove("active"));
                document.getElementById(item[0]).classList.add("active");
                status.textContent=item[1];
            },(i+1)*5000);
        });
    });
});
</script>

</body>
</html>
"""

RESULT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>ReelForge AI — Результат</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
        h1 { color: #333; }
        .result { background: #f4f4f4; padding: 20px; border-radius: 8px; white-space: pre-wrap; }
        a { display: inline-block; margin-top: 20px; color: #007bff; }
        video { width: 100%; margin-top: 20px; border-radius: 8px; }
    </style>
</head>
<body>
    <h1>Готово!</h1>
    {% if script %}
    <div class="result">{{ script }}</div>
    {% endif %}
    {% if video_url %}
    <video controls src="{{ video_url }}"></video><br>
    <a href="{{ video_url }}" download>⬇ Скачать видео</a><br>
    {% endif %}
    <a href="/">← Сгенерировать ещё</a>
</body>
</html>
"""

PROCESSING_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="4">
    <title>ReelForge AI — Обработка</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; text-align: center; }
        .spinner { font-size: 48px; }
        p { color: #555; }
    </style>
</head>
<body>
    <div class="spinner">⏳</div>
    <h2>Собираю видео...</h2>
    <p>Это может занять минуту-две. Страница обновится автоматически.</p>
</body>
</html>
"""

ERROR_HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Ошибка</title></head>
<body style="font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
    <h2>Ошибка сборки видео</h2>
    <pre style="white-space: pre-wrap; background:#fee; padding:15px; border-radius:8px;">{{ error }}</pre>
    <a href="/">← Назад</a>
</body>
</html>
"""

def generate_script(topic):
    client = get_groq_client()
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": "Ты — эксперт по созданию вирусных сценариев для Instagram Reels. Пиши коротко, с хуком, с призывом к действию. Используй эмодзи."},
            {"role": "user", "content": f"Напиши сценарий для Reels на тему: {topic}"}
        ],
        max_tokens=500
    )
    return response.choices[0].message.content

def generate_captions(topic, count):
    client = get_groq_client()
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "Ты пишешь короткие субтитры для видео Reels. Отвечай ТОЛЬКО валидным JSON-массивом строк, без пояснений и markdown."},
                {"role": "user", "content": f"Дай ровно {count} коротких фраз (до 6 слов каждая) как субтитры для видео на тему: {topic}. Формат: [\"фраза1\", \"фраза2\", ...]"}
            ],
            max_tokens=400
        )
        text = response.choices[0].message.content.strip()
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        captions = json.loads(text)
        if isinstance(captions, list) and len(captions) > 0:
            while len(captions) < count:
                captions.append(captions[-1])
            return [str(c) for c in captions[:count]]
    except Exception:
        pass
    return [topic[:40] if topic else ""] * count

def escape_drawtext(text):
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\u2019")
    text = text.replace("%", "\\%")
    return text

def build_slideshow_video(image_dir, output_path, captions=None, seconds_per_image=3):
    images = sorted(glob.glob(os.path.join(image_dir, "img*")))
    if not images:
        raise ValueError("Нет изображений для сборки видео")

    list_path = os.path.join(image_dir, "list.txt")
    with open(list_path, "w") as f:
        for img in images:
            f.write(f"file '{img}'\n")
            f.write(f"duration {seconds_per_image}\n")
        f.write(f"file '{images[-1]}'\n")

    vf_chain = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

    font_path = get_font_path()
    if captions and font_path:
        for i, cap in enumerate(captions):
            start = i * seconds_per_image
            end = start + seconds_per_image
            safe_cap = escape_drawtext(cap)
            vf_chain += (
                f",drawtext=fontfile='{font_path}':text='{safe_cap}':"
                f"fontsize=42:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:"
                f"x=(w-text_w)/2:y=h-220:enable='between(t,{start},{end})'"
            )

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-vf", vf_chain,
        "-r", "24",
        "-preset", "ultrafast",
        "-threads", "1",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"[exit code {result.returncode}] " + result.stderr[-1000:])

def get_video_duration(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0

def extract_auto_clip(src_path, out_path, clip_seconds=4):
    duration = get_video_duration(src_path)
    if duration <= 0:
        raise RuntimeError(f"Не удалось определить длительность видео: {src_path}")

    if duration <= clip_seconds:
        start = 0
        length = duration
    else:
        start = duration * 0.2
        if start + clip_seconds > duration:
            start = max(0, duration - clip_seconds)
        length = clip_seconds

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", src_path,
        "-t", str(length),
        "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r", "24",
        "-an",
        "-preset", "ultrafast",
        "-threads", "1",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"[exit code {result.returncode}] " + result.stderr[-1000:])
    return length

def build_reel_from_videos(video_paths, output_path, captions=None):
    clip_dir = os.path.dirname(output_path)
    clip_paths = []

    for i, vp in enumerate(video_paths):
        clip_path = os.path.join(clip_dir, f"clip{i:03d}.mp4")
        extract_auto_clip(vp, clip_path, clip_seconds=4)
        clip_paths.append(clip_path)

    font_path = get_font_path()
    if captions and font_path:
        for i, clip_path in enumerate(clip_paths):
            cap = captions[i] if i < len(captions) else ""
            if not cap:
                continue
            safe_cap = escape_drawtext(cap)
            tagged_path = clip_path.replace(".mp4", "_cap.mp4")
            vf = (
                f"drawtext=fontfile='{font_path}':text='{safe_cap}':"
                f"fontsize=42:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:"
                f"x=(w-text_w)/2:y=h-220"
            )
            cmd = [
                "ffmpeg", "-y", "-i", clip_path,
                "-vf", vf,
                "-r", "24", "-preset", "ultrafast", "-threads", "1", "-an",
                tagged_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                clip_paths[i] = tagged_path

    list_path = os.path.join(clip_dir, "clips_list.txt")
    with open(list_path, "w") as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        cmd2 = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-r", "24", "-preset", "ultrafast", "-threads", "1",
            output_path
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True)
        if result2.returncode != 0:
            raise RuntimeError(f"[exit code {result2.returncode}] " + result2.stderr[-1000:])

def mux_music(video_path, music_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", music_path,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"[exit code {result.returncode}] " + result.stderr[-1000:])

def process_video_job(job_id, job_dir, files_meta, music_path, topic, mode):
    try:
        script = None
        captions = None
        if topic:
            try:
                script = generate_script(topic)
            except Exception as e:
                script = f"(Не удалось сгенерировать сценарий: {e})"
            try:
                captions = generate_captions(topic, len(files_meta))
            except Exception:
                captions = None

        silent_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")

        if mode == "images":
            build_slideshow_video(job_dir, silent_path, captions=captions, seconds_per_image=3)
        else:
            build_reel_from_videos(files_meta, silent_path, captions=captions)

        final_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
        if music_path:
            mux_music(silent_path, music_path, final_path)
        else:
            shutil.copy(silent_path, final_path)

        set_job(job_id, status="done", script=script, video_url=f"/outputs/{job_id}.mp4")
    except Exception as e:
        set_job(job_id, status="error", error=str(e))

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/generate", methods=["POST"])
def generate():
    topic = request.form.get("topic", "")
    if not topic:
        return "Введи тему!", 400
    try:
        script = generate_script(topic)
        return render_template_string(RESULT_HTML, script=script, video_url=None)
    except Exception as e:
        return f"Ошибка: {str(e)}", 500

@app.route("/create_video", methods=["POST"])
def create_video():
    topic = request.form.get("topic", "")
    files = request.files.getlist("images")
    files = [f for f in files if f and f.filename]
    music_file = request.files.get("music")

    if not files:
        return "Загрузи хотя бы одно изображение!", 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename)[1] or ".jpg"
        f.save(os.path.join(job_dir, f"img{i:03d}{ext}"))

    music_path = None
    if music_file and music_file.filename:
        m_ext = os.path.splitext(music_file.filename)[1] or ".mp3"
        music_path = os.path.join(job_dir, f"music{m_ext}")
        music_file.save(music_path)

    set_job(job_id, status="processing")
    t = threading.Thread(target=process_video_job, args=(job_id, job_dir, files, music_path, topic, "images"))
    t.daemon = True
    t.start()

    return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url=/status/{job_id}"></body>'))

@app.route("/create_reel_from_videos", methods=["POST"])
def create_reel_from_videos():
    topic = request.form.get("topic", "")
    files = request.files.getlist("videos")
    files = [f for f in files if f and f.filename]
    music_file = request.files.get("music")

    if not files:
        return "Загрузи хотя бы одно видео!", 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    video_paths = []
    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename)[1] or ".mp4"
        vp = os.path.join(job_dir, f"src{i:03d}{ext}")
        f.save(vp)
        video_paths.append(vp)

    music_path = None
    if music_file and music_file.filename:
        m_ext = os.path.splitext(music_file.filename)[1] or ".mp3"
        music_path = os.path.join(job_dir, f"music{m_ext}")
        music_file.save(music_path)

    set_job(job_id, status="processing")
    t = threading.Thread(target=process_video_job, args=(job_id, job_dir, video_paths, music_path, topic, "videos"))
    t.daemon = True
    t.start()

    return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url=/status/{job_id}"></body>'))

@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if not job:
        return "Задача не найдена (возможно, сервер перезапустился)", 404

    if job.get("status") == "processing":
        return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url=/status/{job_id}"></body>'))
    elif job.get("status") == "error":
        return render_template_string(ERROR_HTML, error=job.get("error", "неизвестная ошибка"))
    elif job.get("status") == "done":
        return render_template_string(RESULT_HTML, script=job.get("script"), video_url=job.get("video_url"))
    return "Неизвестный статус задачи", 500

@app.route("/outputs/<path:filename>")
def outputs(filename):
    return send_from_directory(OUTPUT_DIR, filename)

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
