import os
import subprocess
import uuid
import glob
from flask import Flask, request, render_template_string, send_from_directory

app = Flask(__name__)

UPLOAD_DIR = "/tmp/uploads"
OUTPUT_DIR = "/tmp/outputs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

_groq_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is not set")
        _groq_client = Groq(api_key=api_key)
    return _groq_client

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
        input[type=file] { margin-top: 10px; }
        button { background: #007bff; color: white; padding: 12px 24px; border: none; cursor: pointer; font-size: 16px; margin-top: 10px; }
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

    <p>Собери видео Reels из своих скриншотов</p>
    <form action="/create_video" method="post" enctype="multipart/form-data">
        <textarea name="topic" placeholder="Тема ролика (для сценария)..."></textarea><br>
        <input type="file" name="images" multiple accept="image/*"><br>
        <button type="submit">Собрать видео</button>
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

def build_slideshow_video(image_dir, output_path, seconds_per_image=3):
    images = sorted(glob.glob(os.path.join(image_dir, "img*")))
    if not images:
        raise ValueError("Нет изображений для сборки видео")

    list_path = os.path.join(image_dir, "list.txt")
    with open(list_path, "w") as f:
        for img in images:
            f.write(f"file '{img}'\n")
            f.write(f"duration {seconds_per_image}\n")
        f.write(f"file '{images[-1]}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r", "30",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1500:])

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

    if not files:
        return "Загрузи хотя бы одно изображение!", 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename)[1] or ".jpg"
        f.save(os.path.join(job_dir, f"img{i:03d}{ext}"))

    script = None
    if topic:
        try:
            script = generate_script(topic)
        except Exception as e:
            script = f"(Не удалось сгенерировать сценарий: {e})"

    output_filename = f"{job_id}.mp4"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    try:
        build_slideshow_video(job_dir, output_path, seconds_per_image=3)
    except Exception as e:
        return f"Ошибка сборки видео: {str(e)}", 500

    video_url = f"/outputs/{output_filename}"
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
