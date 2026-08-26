import os
import re
import json
import subprocess
import uuid
import glob
import shutil
from flask import Flask, request, render_template_string, send_from_directory

app = Flask(__name__)

UPLOAD_DIR = "/tmp/uploads"
OUTPUT_DIR = "/tmp/outputs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

_groq_client = None
_font_path = None

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
<html>
<head>
    <meta charset="UTF-8">
    <title>ReelForge AI</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
        h1 { color: #333; }
        textarea { width: 100%; height: 120px; padding: 10px; font-size: 16px; }
        input[type=file] { margin-top: 10px; display: block; }
        label { display: block; margin-top: 15px; font-weight: bold; }
        button { background: #007bff; color: white; padding: 12px 24px; border: none; cursor: pointer; font-size: 16px; margin-top: 15px; }
        button:hover { background: #0056b3; }
        hr { margin: 30px 0; }
    </style>
</head>
<body>
    <h1>ReelForge AI</h1>

    <p>Введи тему — сгенерирую сценарий для Reels</p>
    <form action="/generate" method="post">
        <textarea name="topic" placeholder="Например: 5 лайфхаков для продуктивности..."></textarea><br>
        <button type="submit">Сгенерировать сценарий</button>
    </form>

    <hr>

    <p>Собери видео Reels из своих скриншотов (с субтитрами и музыкой)</p>
    <form action="/create_video" method="post" enctype="multipart/form-data">
        <label>Тема ролика (для сценария и субтитров)</label>
        <textarea name="topic" placeholder="Тема ролика..."></textarea>

        <label>Скриншоты (можно несколько)</label>
        <input type="file" name="images" multiple accept="image/*">

        <label>Музыка (необязательно, mp3/wav)</label>
        <input type="file" name="music" accept="audio/*">

        <button type="submit">Собрать видео из скриншотов</button>
    </form>

    <hr>

    <p>Собери Reels из видео-кусков (система сама выберет лучшие моменты и смонтирует)</p>
    <form action="/create_reel_from_videos" method="post" enctype="multipart/form-data">
        <label>Тема ролика (для сценария и субтитров)</label>
        <textarea name="topic" placeholder="Тема ролика..."></textarea>

        <label>Видео-куски (можно несколько)</label>
        <input type="file" name="videos" multiple accept="video/*">

        <label>Музыка (необязательно, mp3/wav)</label>
        <input type="file" name="music" accept="audio/*">

        <button type="submit">Собрать Reels из видео</button>
    </form>
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
    clip_durations = []

    for i, vp in enumerate(video_paths):
        clip_path = os.path.join(clip_dir, f"clip{i:03d}.mp4")
        length = extract_auto_clip(vp, clip_path, clip_seconds=4)
        clip_paths.append(clip_path)
        clip_durations.append(length)

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

    script = None
    captions = None
    if topic:
        try:
            script = generate_script(topic)
        except Exception as e:
            script = f"(Не удалось сгенерировать сценарий: {e})"
        try:
            captions = generate_captions(topic, len(files))
        except Exception:
            captions = None

    silent_filename = f"{job_id}_silent.mp4"
    silent_path = os.path.join(OUTPUT_DIR, silent_filename)

    try:
        build_slideshow_video(job_dir, silent_path, captions=captions, seconds_per_image=3)
    except Exception as e:
        return f"Ошибка сборки видео: {str(e)}", 500

    final_filename = f"{job_id}.mp4"
    final_path = os.path.join(OUTPUT_DIR, final_filename)

    if music_path:
        try:
            mux_music(silent_path, music_path, final_path)
        except Exception:
            shutil.copy(silent_path, final_path)
    else:
        shutil.copy(silent_path, final_path)

    video_url = f"/outputs/{final_filename}"
    return render_template_string(RESULT_HTML, script=script, video_url=video_url)

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

    script = None
    captions = None
    if topic:
        try:
            script = generate_script(topic)
        except Exception as e:
            script = f"(Не удалось сгенерировать сценарий: {e})"
        try:
            captions = generate_captions(topic, len(files))
        except Exception:
            captions = None

    silent_filename = f"{job_id}_silent.mp4"
    silent_path = os.path.join(OUTPUT_DIR, silent_filename)

    try:
        build_reel_from_videos(video_paths, silent_path, captions=captions)
    except Exception as e:
        return f"Ошибка сборки видео: {str(e)}", 500

    final_filename = f"{job_id}.mp4"
    final_path = os.path.join(OUTPUT_DIR, final_filename)

    if music_path:
        try:
            mux_music(silent_path, music_path, final_path)
        except Exception:
            shutil.copy(silent_path, final_path)
    else:
        shutil.copy(silent_path, final_path)

    video_url = f"/outputs/{final_filename}"
    return render_template_string(RESULT_HTML, script=script, video_url=video_url)

@app.route("/outputs/<path:filename>")
def outputs(filename):
    return send_from_directory(OUTPUT_DIR, filename)

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
# rebuild trigger
