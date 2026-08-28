import os
import re
import json
import subprocess
import uuid
import glob
import shutil
import threading
import time
import psycopg
from flask import Flask, request, render_template_string, send_from_directory, redirect, jsonify

from access_middleware import check_access, set_access_cookie


# =========================
# PostgreSQL
# =========================

def db_connect():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL не задан")
    return psycopg.connect(database_url)


def init_db():
    """Создаёт таблицы ReelForge AI при запуске приложения."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    user_key TEXT UNIQUE NOT NULL,
                    videos_balance INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_key TEXT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    videos INTEGER NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    confirmed_at TIMESTAMPTZ
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id SERIAL PRIMARY KEY,
                    user_key TEXT NOT NULL,
                    job_id TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

        conn.commit()


def ensure_user(user_key):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_key)
                VALUES (%s)
                ON CONFLICT (user_key) DO NOTHING
            """, (user_key,))
        conn.commit()


def get_user_balance(user_key):
    ensure_user(user_key)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT videos_balance FROM users WHERE user_key = %s",
                (user_key,)
            )
            row = cur.fetchone()

    return row[0] if row else 0


def add_videos(user_key, amount):
    ensure_user(user_key)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET videos_balance = videos_balance + %s
                WHERE user_key = %s
            """, (amount, user_key))
        conn.commit()


def create_payment(user_key, amount_rub=10, videos=10):
    ensure_user(user_key)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO payments (user_key, amount_rub, videos, status)
                VALUES (%s, %s, %s, 'pending')
                RETURNING id
            """, (user_key, amount_rub, videos))

            payment_id = cur.fetchone()[0]

        conn.commit()

    return payment_id


def confirm_payment(payment_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_key, videos, status
                FROM payments
                WHERE id = %s
                FOR UPDATE
            """, (payment_id,))

            row = cur.fetchone()

            if not row:
                return False, "Платёж не найден"

            user_key, videos, status = row

            if status == "confirmed":
                return False, "Платёж уже подтверждён"

            cur.execute("""
                UPDATE payments
                SET status = 'confirmed',
                    confirmed_at = NOW()
                WHERE id = %s
            """, (payment_id,))

            cur.execute("""
                UPDATE users
                SET videos_balance = videos_balance + %s
                WHERE user_key = %s
            """, (videos, user_key))

        conn.commit()

    return True, "Платёж подтверждён"


def consume_video(user_key, job_id):
    ensure_user(user_key)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT videos_balance
                FROM users
                WHERE user_key = %s
                FOR UPDATE
            """ , (user_key,))

            row = cur.fetchone()

            if not row or row[0] <= 0:
                return False

            cur.execute("""
                UPDATE users
                SET videos_balance = videos_balance - 1
                WHERE user_key = %s
            """, (user_key,))

            cur.execute("""
                INSERT INTO usage (user_key, job_id)
                VALUES (%s, %s)
                ON CONFLICT (job_id) DO NOTHING
            """, (user_key, job_id))

        conn.commit()

    return True

app = Flask(__name__)

try:
    init_db()
    print("POSTGRES INIT OK")
except Exception as e:
    print(f"DATABASE INIT WARNING: {e}")

BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://web-production-3e349.up.railway.app"
)

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

# Upload limits
MAX_VIDEO_SIZE = 100 * 1024 * 1024       # 100 MB на одно видео
MAX_PROJECT_SIZE = 300 * 1024 * 1024     # 300 MB на все видео проекта
MAX_UPLOAD_REQUEST = 105 * 1024 * 1024   # небольшой запас multipart

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

    <form id="videoUploadForm" action="/create_reel_from_videos" method="post" enctype="multipart/form-data">

        <label>🎯 Тема ролика</label>
        <textarea name="topic" placeholder="Например: Обзор продукта за 30 секунд"></textarea>

        <label>🎞️ Видео-куски</label>
        <input class="file" type="file" name="videos" multiple accept="video/*">
        <p style="color:#666;font-size:13px;margin-top:6px;">
            Максимум: 100 MB на одно видео и 300 MB на весь проект.
        </p>

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
function showLoading() {
    const loading = document.getElementById("loading");
    if (loading) loading.style.display = "flex";

    let seconds = 0;
    const timer = document.getElementById("timer");
    const status = document.getElementById("statusText");

    setInterval(() => {
        seconds++;
        const m = String(Math.floor(seconds / 60)).padStart(2, "0");
        const sec = String(seconds % 60).padStart(2, "0");
        if (timer) timer.textContent = m + ":" + sec;
    }, 1000);

    const messages = [
        ["s1", "Загружаем материалы..."],
        ["s2", "Анализируем контент..."],
        ["s3", "Создаём сценарий..."],
        ["s4", "Монтируем видео..."],
        ["s5", "Финальный рендер..."]
    ];

    messages.forEach((item, i) => {
        setTimeout(() => {
            document.querySelectorAll(".step").forEach(x => x.classList.remove("active"));
            const step = document.getElementById(item[0]);
            if (step) step.classList.add("active");
            if (status) status.textContent = item[1];
        }, (i + 1) * 5000);
    });
}

async function uploadVideoForm(form) {
    const btn = form.querySelector("button");
    const filesInput = form.querySelector('input[name="videos"]');
    const topicInput = form.querySelector('textarea[name="topic"]');
    const musicInput = form.querySelector('input[name="music"]');

    const files = Array.from(filesInput.files || []);

    const MAX_VIDEO_SIZE = 100 * 1024 * 1024;
    const MAX_PROJECT_SIZE = 300 * 1024 * 1024;

    if (!files.length) {
        alert("Выбери хотя бы одно видео!");
        return;
    }

    // Проверяем размер каждого видео до начала загрузки.
    for (const file of files) {
        if (file.size > MAX_VIDEO_SIZE) {
            alert(
                "Видео слишком большое: " + file.name +
                "\n\nМаксимальный размер одного видео — 100 MB." +
                "\nРазмер этого файла — " +
                (file.size / 1024 / 1024).toFixed(1) + " MB."
            );
            return;
        }
    }

    // Проверяем общий размер проекта до начала загрузки.
    const totalVideoSize = files.reduce(
        (sum, file) => sum + file.size,
        0
    );

    if (totalVideoSize > MAX_PROJECT_SIZE) {
        alert(
            "Проект слишком большой." +
            "\n\nМаксимальный общий размер видео — 300 MB." +
            "\nСейчас выбрано — " +
            (totalVideoSize / 1024 / 1024).toFixed(1) + " MB."
        );
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = "⏳ Загружаем видео...";
    }

    showLoading();

    try {
        // Создаём сессию загрузки
        const startData = new URLSearchParams();
        startData.append("topic", topicInput ? topicInput.value : "");
        startData.append("total", String(files.length));

        const startResponse = await fetch("/start_video_upload", {
            method: "POST",
            body: startData
        });

        if (!startResponse.ok) {
            throw new Error("Не удалось начать загрузку");
        }

        const startResult = await startResponse.json();
        const jobId = startResult.job_id;

        // Загружаем КАЖДОЕ видео отдельным HTTP-запросом
        for (let i = 0; i < files.length; i++) {
            if (btn) btn.innerHTML = `⏳ Загружаем видео ${i + 1} из ${files.length}...`;

            const fd = new FormData();
            fd.append("job_id", jobId);
            fd.append("index", String(i));
            fd.append("video", files[i], files[i].name);

            const response = await fetch("/upload_video_part", {
                method: "POST",
                body: fd
            });

            if (!response.ok) {
                let message = "Ошибка загрузки видео " + (i + 1);
                try {
                    const data = await response.json();
                    if (data.error) message = data.error;
                } catch (_) {}
                throw new Error(message);
            }
        }

        // Музыка загружается небольшими частями, чтобы избежать
        // Gunicorn NoMoreData на больших multipart-запросах.
        const musicInputEl = form.querySelector('input[type="file"][name="music"]');
        const musicFile =
            musicInputEl &&
            musicInputEl.files &&
            musicInputEl.files.length
                ? musicInputEl.files[0]
                : null;

        console.log("[MUSIC-CLIENT] input=", musicInputEl);
        console.log("[MUSIC-CLIENT] file=", musicFile);
        console.log(
            "[MUSIC-CLIENT] name=",
            musicFile ? musicFile.name : "NONE",
            "size=",
            musicFile ? musicFile.size : 0,
            "type=",
            musicFile ? musicFile.type : "NONE"
        );

        if (musicFile) {
            if (btn) {
                btn.innerHTML =
                    "⏳ Музыка: " + musicFile.name +
                    " (" + Math.round(musicFile.size / 1024 / 1024) + " MB)";
            }
            const CHUNK_SIZE = 4 * 1024 * 1024;
            const totalChunks = Math.ceil(musicFile.size / CHUNK_SIZE);

            for (let i = 0; i < totalChunks; i++) {
                if (btn) {
                    btn.innerHTML =
                        `⏳ Загружаем музыку ${i + 1} из ${totalChunks}...`;
                }

                const start = i * CHUNK_SIZE;
                const end = Math.min(start + CHUNK_SIZE, musicFile.size);
                const blob = musicFile.slice(start, end);

                const musicFd = new FormData();
                musicFd.append("job_id", jobId);
                musicFd.append("index", String(i));
                musicFd.append("total", String(totalChunks));
                musicFd.append("name", musicFile.name);
                musicFd.append("chunk", blob, musicFile.name + ".part");

                let musicResponse;

                try {
                    musicResponse = await fetch("/upload_video_music_part", {
                        method: "POST",
                        body: musicFd
                    });
                } catch (e) {
                    throw new Error(
                        `Ошибка загрузки музыки, часть ${i + 1} из ${totalChunks}: ` +
                        (e.message || "Failed to fetch")
                    );
                }

                if (!musicResponse.ok) {
                    let message =
                        `Ошибка загрузки музыки, часть ${i + 1} из ${totalChunks}`;

                    try {
                        const data = await musicResponse.json();
                        if (data.error) message = data.error;
                    } catch (_) {}

                    throw new Error(message);
                }
            }
        }

        // После полной загрузки запускаем уже существующий рендер
        if (btn) btn.innerHTML = "🎬 Запускаем монтаж...";

        const finishData = new URLSearchParams();
        finishData.append("job_id", jobId);

        const finishResponse = await fetch("/finish_video_upload", {
            method: "POST",
            body: finishData
        });

        if (!finishResponse.ok) {
            let message = "Не удалось запустить обработку";
            try {
                const data = await finishResponse.json();
                if (data.error) message = data.error;
            } catch (_) {}
            throw new Error(message);
        }

        window.location.href =
            "/status/" + jobId + "?access=rf2026free";

    } catch (error) {
        console.error(error);
        alert("Ошибка загрузки: " + error.message);
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = "🚀 Собрать Reels из видео";
        }
    }
}

document.querySelectorAll("form").forEach(form => {
    form.addEventListener("submit", event => {
        if (form.id === "videoUploadForm") {
            event.preventDefault();
            uploadVideoForm(form);
            return;
        }

        const btn = form.querySelector("button");
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = "⏳ Создаём видео...";
        }

        showLoading();
    });
});
</script>

</body>
</html>
"""

RESULT_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ReelForge AI — Ваш Reels готов</title>
    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            background: #07070d;
            color: #fff;
            font-family: Arial, sans-serif;
        }

        .container {
            width: 100%;
            max-width: 760px;
            margin: 0 auto;
            padding: 28px 18px 40px;
        }

        .logo {
            text-align: center;
            font-size: 25px;
            font-weight: 800;
            margin-bottom: 32px;
        }

        .logo span {
            color: #a855f7;
        }

        .success {
            text-align: center;
            margin-bottom: 24px;
        }

        .check {
            width: 64px;
            height: 64px;
            margin: 0 auto 16px;
            border-radius: 50%;
            background: #22c55e;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
        }

        h1 {
            margin: 0 0 8px;
            font-size: 30px;
        }

        .subtitle {
            margin: 0;
            color: #9ca3af;
            font-size: 16px;
        }

        .video-card {
            background: #111118;
            border: 1px solid #272733;
            border-radius: 18px;
            padding: 10px;
            margin-top: 24px;
            overflow: hidden;
        }

        video {
            display: block;
            width: 100%;
            max-height: 75vh;
            border-radius: 12px;
            background: #000;
        }

        .actions {
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 20px;
        }

        .button {
            display: block;
            width: 100%;
            padding: 16px 20px;
            border-radius: 12px;
            text-align: center;
            text-decoration: none;
            font-size: 17px;
            font-weight: 700;
        }

        .download {
            background: #a855f7;
            color: #fff;
        }

        .download:active {
            background: #9333ea;
        }

        .new-video {
            background: #1b1b24;
            color: #fff;
            border: 1px solid #30303b;
        }

        .script {
            margin-top: 24px;
            padding: 18px;
            background: #111118;
            border: 1px solid #272733;
            border-radius: 14px;
            white-space: pre-wrap;
            color: #d1d5db;
            line-height: 1.5;
        }

        @media (min-width: 600px) {
            .container {
                padding-top: 45px;
            }

            .actions {
                flex-direction: row;
            }

            .button {
                flex: 1;
            }
        }
    </style>
</head>
<body>
    <main class="container">

        <div class="logo">ReelForge <span>AI</span></div>

        <section class="success">
            <div class="check">✓</div>
            <h1>Ваш Reels готов!</h1>
            <p class="subtitle">Видео успешно создано</p>
        </section>

        {% if video_url %}
        <div class="video-card">
            <video controls playsinline preload="metadata" src="{{ video_url }}"></video>
        </div>

        <div class="actions">
            <a class="button download" href="{{ video_url }}" download>
                ⬇ Скачать видео
            </a>

            <a class="button new-video" href="/">
                ＋ Создать ещё один
            </a>
        </div>
        {% endif %}

        {% if script %}
        <div class="script">{{ script }}</div>
        {% endif %}

    </main>
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

def parse_target_duration(topic):
    """Определяет длительность ролика из промпта. По умолчанию 40 секунд."""
    text = (topic or "").lower().strip()

    # MM:SS
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        return max(5, min(int(m.group(1)) * 60 + int(m.group(2)), 180))

    # минуты
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:минут(?:а|ы)?|мин\.?|m)\b', text)
    if m:
        seconds = float(m.group(1).replace(",", ".")) * 60
        return max(5, min(int(round(seconds)), 180))

    # секунды
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:секунд(?:а|ы)?|сек\.?|сек|s)\b', text)
    if m:
        return max(5, min(int(round(float(m.group(1).replace(",", ".")))), 180))

    return 40


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

    if duration < clip_seconds:
        # Зацикливаем короткое видео до требуемой длительности.
        start = 0
        length = clip_seconds
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", src_path,
            "-t", str(length),
            "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-r", "24",
            "-an",
            "-preset", "ultrafast",
            "-threads", "1",
            out_path
        ]
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

def build_reel_from_videos(video_paths, output_path, captions=None, target_duration=40):
    clip_dir = os.path.dirname(output_path)
    clip_paths = []

    clip_seconds = max(1, target_duration / max(1, len(video_paths)))

    for i, vp in enumerate(video_paths):
        clip_path = os.path.join(clip_dir, f"clip{i:03d}.mp4")
        extract_auto_clip(vp, clip_path, clip_seconds=clip_seconds)
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
    if not os.path.exists(video_path):
        raise RuntimeError(f"VIDEO NOT FOUND: {video_path}")

    if not music_path or not os.path.exists(music_path):
        raise RuntimeError(f"MUSIC NOT FOUND: {music_path}")

    video_size = os.path.getsize(video_path)
    music_size = os.path.getsize(music_path)

    print(
        f"[MUSIC] video={video_path} size={video_size} "
        f"music={music_path} size={music_size}",
        flush=True
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", music_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]

    print(f"[MUSIC] FFMPEG START: {cmd}", flush=True)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[MUSIC] FFMPEG ERROR: {result.stderr[-3000:]}", flush=True)
        raise RuntimeError(
            f"[exit code {result.returncode}] " + result.stderr[-1000:]
        )

    if not os.path.exists(output_path):
        raise RuntimeError(f"MUSIC OUTPUT NOT CREATED: {output_path}")

    print(
        f"[MUSIC] COMPLETE output={output_path} "
        f"size={os.path.getsize(output_path)}",
        flush=True
    )

def process_video_job(job_id, job_dir, files_meta, music_path, topic, mode):
    print(f"[JOB {job_id}] START mode={mode} files={len(files_meta)} topic={bool(topic)}", flush=True)
    try:
        script = None
        captions = None
        if topic:
            target_duration = parse_target_duration(topic)
            ai_topic = (
                f"{topic}\n\n"
                f"ВАЖНО: создай сценарий для Reels примерно на "
                f"{target_duration} секунд. "
                f"Рассчитай текст и сцены под эту длительность."
            )

            try:
                script = generate_script(ai_topic)
            except Exception as e:
                script = f"(Не удалось сгенерировать сценарий: {e})"
            try:
                captions = generate_captions(ai_topic, len(files_meta))
            except Exception:
                captions = None

        silent_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")

        target_duration = parse_target_duration(topic)
        print(f"[JOB {job_id}] target_duration={target_duration}", flush=True)
        print(f"[JOB {job_id}] START RENDER mode={mode}", flush=True)

        if mode == "images":
            seconds_per_image = target_duration / max(1, len(files_meta))
            build_slideshow_video(
                job_dir,
                silent_path,
                captions=captions,
                seconds_per_image=seconds_per_image
            )
        else:
            build_reel_from_videos(
                files_meta,
                silent_path,
                captions=captions,
                target_duration=target_duration
            )

        print(f"[JOB {job_id}] RENDER COMPLETE silent={silent_path}", flush=True)
        final_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
        if music_path:
            mux_music(silent_path, music_path, final_path)
        else:
            shutil.copy(silent_path, final_path)

        print(f"[JOB {job_id}] DONE final={final_path}", flush=True)
        set_job(job_id, status="done", script=script, video_url=f"{BACKEND_URL}/outputs/{job_id}.mp4")
    except Exception as e:
        import traceback
        print(f"VIDEO JOB ERROR [{job_id}]: {e}", flush=True)
        traceback.print_exc()
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

    return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url={BACKEND_URL}/status/{job_id}?access=rf2026free"></body>'))


@app.route("/start_video_upload", methods=["POST"])
def start_video_upload():
    topic = request.form.get("topic", "")
    try:
        total = int(request.form.get("total", "0"))
    except Exception:
        total = 0

    if total <= 0:
        return jsonify({"error": "Нет видео для загрузки"}), 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    set_job(
        job_id,
        status="uploading",
        topic=topic,
        total=total,
        video_paths=[]
    )

    print(
        f"[UPLOAD-SESSION] START job={job_id} total={total}",
        flush=True
    )

    return jsonify({"job_id": job_id})


@app.route("/upload_video_part", methods=["POST"])
def upload_video_part():
    job_id = request.form.get("job_id", "")
    video = request.files.get("video")

    if not job_id or not video or not video.filename:
        return jsonify({"error": "Видео не получено"}), 400

    # Защита от слишком большого HTTP multipart-запроса.
    if request.content_length and request.content_length > MAX_UPLOAD_REQUEST:
        return jsonify({
            "error": (
                "Видео слишком большое. "
                "Максимальный размер одного видео — 100 MB."
            )
        }), 413

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    try:
        index = int(request.form.get("index", "0"))
    except Exception:
        index = 0

    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ext = os.path.splitext(video.filename)[1] or ".mp4"
    video_path = os.path.join(job_dir, f"src{index:03d}{ext}")

    started = time.time()
    print(
        f"[UPLOAD-PART] START job={job_id} index={index} "
        f"name={video.filename} content_length={request.content_length}",
        flush=True
    )

    video.save(video_path)

    size = os.path.getsize(video_path)

    # Серверная проверка фактического размера файла.
    if size > MAX_VIDEO_SIZE:
        try:
            os.remove(video_path)
        except OSError:
            pass

        return jsonify({
            "error": (
                "Видео слишком большое. "
                "Максимальный размер одного видео — 100 MB. "
                f"Размер файла: {size / 1024 / 1024:.1f} MB."
            )
        }), 413

    with JOBS_LOCK:
        current = JOBS.setdefault(job_id, {})
        paths = current.setdefault("video_paths", [])
        paths = [x for x in paths if x != video_path]
        paths.append(video_path)
        paths.sort()
        current["video_paths"] = paths

    print(
        f"[UPLOAD-PART] SAVED job={job_id} index={index} "
        f"size={size} elapsed={time.time()-started:.2f}s",
        flush=True
    )

    return jsonify({
        "ok": True,
        "index": index,
        "size": size
    })


@app.route("/upload_video_music", methods=["POST"])
def upload_video_music():
    job_id = request.form.get("job_id", "")
    music = request.files.get("music")

    if not job_id:
        return jsonify({"error": "Нет job_id"}), 400

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    if not music or not music.filename:
        return jsonify({"ok": True, "skipped": True})

    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ext = os.path.splitext(music.filename)[1] or ".mp3"
    music_path = os.path.join(job_dir, f"music{ext}")

    music.save(music_path)

    set_job(job_id, music_path=music_path)

    print(
        f"[UPLOAD-MUSIC] SAVED job={job_id} "
        f"size={os.path.getsize(music_path)}",
        flush=True
    )

    return jsonify({"ok": True})


@app.route("/upload_video_music_part", methods=["POST"])
def upload_video_music_part():
    job_id = request.form.get("job_id", "")
    chunk = request.files.get("chunk")

    try:
        index = int(request.form.get("index", "0"))
        total = int(request.form.get("total", "0"))
    except Exception:
        return jsonify({"error": "Некорректные параметры части музыки"}), 400

    if not job_id or not chunk or total <= 0 or index < 0 or index >= total:
        return jsonify({"error": "Некорректная часть музыки"}), 400

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = request.form.get("name", "music.mp3")
    ext = os.path.splitext(original_name)[1] or ".mp3"

    part_path = os.path.join(job_dir, f"music.part{index:04d}")

    started = time.time()
    print(
        f"[UPLOAD-MUSIC-PART] START job={job_id} "
        f"index={index}/{total} "
        f"name={original_name} content_length={request.content_length}",
        flush=True
    )

    chunk.save(part_path)

    size = os.path.getsize(part_path)

    print(
        f"[UPLOAD-MUSIC-PART] SAVED job={job_id} "
        f"index={index}/{total} size={size} "
        f"elapsed={time.time()-started:.2f}s",
        flush=True
    )

    if index == total - 1:
        music_path = os.path.join(job_dir, f"music{ext}")

        missing = []
        for i in range(total):
            expected = os.path.join(job_dir, f"music.part{i:04d}")
            if not os.path.exists(expected):
                missing.append(i)

        if missing:
            return jsonify({
                "error": f"Не хватает частей музыки: {missing}"
            }), 400

        with open(music_path, "wb") as out:
            for i in range(total):
                part = os.path.join(job_dir, f"music.part{i:04d}")
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, out)

        for i in range(total):
            part = os.path.join(job_dir, f"music.part{i:04d}")
            try:
                os.remove(part)
            except OSError:
                pass

        final_size = os.path.getsize(music_path)
        set_job(job_id, music_path=music_path)

        print(
            f"[UPLOAD-MUSIC] COMPLETE job={job_id} "
            f"parts={total} size={final_size}",
            flush=True
        )

        return jsonify({
            "ok": True,
            "complete": True,
            "size": final_size
        })

    return jsonify({
        "ok": True,
        "complete": False,
        "index": index,
        "size": size
    })


@app.route("/finish_video_upload", methods=["POST"])
def finish_video_upload():
    job_id = request.form.get("job_id", "")

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    video_paths = list(job.get("video_paths", []))
    total = int(job.get("total", 0))

    if len(video_paths) != total:
        return jsonify({
            "error": f"Загружено {len(video_paths)} из {total} видео"
        }), 400

    # Проверяем общий размер проекта перед запуском рендера.
    total_video_bytes = 0

    for video_path in video_paths:
        if not os.path.exists(video_path):
            return jsonify({
                "error": "Один из загруженных файлов не найден."
            }), 400

        size = os.path.getsize(video_path)

        if size > MAX_VIDEO_SIZE:
            return jsonify({
                "error": (
                    "Видео превышает лимит 100 MB: "
                    + os.path.basename(video_path)
                )
            }), 413

        total_video_bytes += size

    if total_video_bytes > MAX_PROJECT_SIZE:
        return jsonify({
            "error": (
                "Общий размер проекта превышает лимит 300 MB. "
                f"Сейчас: {total_video_bytes / 1024 / 1024:.1f} MB."
            )
        }), 413

    topic = job.get("topic", "")
    music_path = job.get("music_path")

    print(
        f"[UPLOAD-SESSION] COMPLETE job={job_id} "
        f"files={len(video_paths)} "
        f"total_bytes={sum(os.path.getsize(v) for v in video_paths)}",
        flush=True
    )

    set_job(job_id, status="processing")

    t = threading.Thread(
        target=process_video_job,
        args=(job_id, os.path.join(UPLOAD_DIR, job_id),
              video_paths, music_path, topic, "videos")
    )
    t.daemon = True
    t.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/create_reel_from_videos", methods=["POST"])
def create_reel_from_videos():
    upload_started = time.time()
    print(f"[UPLOAD] START content_length={request.content_length}", flush=True)

    topic = request.form.get("topic", "")
    files = request.files.getlist("videos")
    files = [f for f in files if f and f.filename]
    music_file = request.files.get("music")

    print(f"[UPLOAD] PARSED files={len(files)} elapsed={time.time()-upload_started:.2f}s", flush=True)

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
        print(
            f"[UPLOAD] SAVED file={i} name={f.filename} "
            f"size={os.path.getsize(vp)} bytes elapsed={time.time()-upload_started:.2f}s",
            flush=True
        )

    music_path = None
    if music_file and music_file.filename:
        m_ext = os.path.splitext(music_file.filename)[1] or ".mp3"
        music_path = os.path.join(job_dir, f"music{m_ext}")
        music_file.save(music_path)

    print(
        f"[UPLOAD] COMPLETE job={job_id} files={len(video_paths)} "
        f"total_bytes={sum(os.path.getsize(v) for v in video_paths)} "
        f"elapsed={time.time()-upload_started:.2f}s",
        flush=True
    )

    set_job(job_id, status="processing")
    t = threading.Thread(target=process_video_job, args=(job_id, job_dir, video_paths, music_path, topic, "videos"))
    t.daemon = True
    t.start()

    return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url={BACKEND_URL}/status/{job_id}?access=rf2026free"></body>'))

@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if not job:
        return "Задача не найдена (возможно, сервер перезапустился)", 404

    if job.get("status") == "processing":
        return render_template_string(PROCESSING_HTML.replace("</body>", f'<meta http-equiv="refresh" content="4;url={BACKEND_URL}/status/{job_id}?access=rf2026free"></body>'))
    elif job.get("status") == "error":
        return render_template_string(ERROR_HTML, error=job.get("error", "неизвестная ошибка"))
    elif job.get("status") == "done":
        return render_template_string(RESULT_HTML, script=job.get("script"), video_url=job.get("video_url"))
    return "Неизвестный статус задачи", 500

@app.route("/outputs/<path:filename>")
def outputs(filename):
    return send_from_directory(OUTPUT_DIR, filename)

@app.route("/payment")
def payment():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ReelForge AI — Оплата</title>
        <style>
            body {
                margin: 0;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: #07070d;
                color: white;
                font-family: Arial, sans-serif;
                text-align: center;
            }
            .box {
                padding: 40px 25px;
                max-width: 420px;
            }
            h1 {
                font-size: 32px;
                margin-bottom: 15px;
            }
            p {
                color: #9ca3af;
                font-size: 17px;
                line-height: 1.5;
            }
            .badge {
                display: inline-block;
                margin-bottom: 20px;
                padding: 8px 14px;
                border-radius: 999px;
                background: #9333ea;
                font-weight: bold;
            }
        </style>
    </head>
    <body>
        <div class="box">
            <div class="badge">ReelForge AI</div>
            <h1>Бесплатные входы закончились 🎬</h1>
            <p>Здесь позже будет страница оплаты.</p>
        </div>
    </body>
    </html>
    """

@app.route("/admin/reset-free")
def admin_reset_free():
    admin_key = os.getenv("ADMIN_RESET_TOKEN", "")
    provided_key = request.args.get("key", "")

    if not admin_key or provided_key != admin_key:
        return "Доступ запрещён", 403

    response = redirect("/?access=rf2026free")
    response.set_cookie(
        "rf_access_count",
        "0",
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="Lax"
    )
    response.set_cookie(
        "rf_access",
        "rf2026free",
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="Lax"
    )
    return response


@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
