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
from flask import Flask, request, render_template_string, send_from_directory, redirect, jsonify, session

from access_middleware import check_access, set_access_cookie

def admin_session_ok():
    """Проверяет текущую админскую сессию или одноразовый key из URL/form."""
    admin_key = os.getenv("ADMIN_RESET_TOKEN", "")
    provided_key = request.args.get("key", "") or request.form.get("key", "")
    if not admin_key:
        return False
    if session.get("admin_authenticated") is True:
        return True
    if provided_key and provided_key == admin_key:
        session["admin_authenticated"] = True
        session.permanent = True
        return True
    return False


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
                    free_entries_used INTEGER NOT NULL DEFAULT 0,
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

            cur.execute("""
                CREATE TABLE IF NOT EXISTS emails (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    ai_provider TEXT NOT NULL DEFAULT 'groq',
                    ai_model TEXT NOT NULL DEFAULT 'openai/gpt-oss-120b',
                    plan TEXT NOT NULL DEFAULT 'free',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                INSERT INTO app_settings (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
            """)


            # ============================================================
            # MARKETING PLATFORMS — реестр рекламных площадок
            # ============================================================
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketing_platforms (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    slug TEXT UNIQUE NOT NULL,
                    website_url TEXT NOT NULL,
                    api_url TEXT,
                    api_method TEXT DEFAULT 'POST',
                    automation_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                    auth_type TEXT,
                    badge_required BOOLEAN NOT NULL DEFAULT FALSE,
                    submission_status TEXT NOT NULL DEFAULT 'not_submitted',
                    review_status TEXT NOT NULL DEFAULT 'unknown',
                    publication_url TEXT,
                    last_error TEXT,
                    last_checked_at TIMESTAMPTZ,
                    last_submitted_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketing_submissions (
                    id SERIAL PRIMARY KEY,
                    platform_id INTEGER NOT NULL
                        REFERENCES marketing_platforms(id)
                        ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    request_data TEXT,
                    response_data TEXT,
                    publication_url TEXT,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Первая официально подключённая площадка.
            # PromptFrenzy автоматически проверил badge и смёржил PR #46.
            cur.execute("""
                INSERT INTO marketing_platforms (
                    name,
                    slug,
                    website_url,
                    api_url,
                    api_method,
                    automation_allowed,
                    auth_type,
                    badge_required,
                    submission_status,
                    review_status,
                    publication_url
                )
                VALUES (
                    'PromptFrenzy',
                    'promptfrenzy',
                    'https://www.promptfrenzy.com',
                    'https://www.promptfrenzy.com/api/directory/submit',
                    'POST',
                    TRUE,
                    'none',
                    TRUE,
                    'verified',
                    'merged',
                    'https://promptfrenzy.com/directory/reelforge-ai'
                )
                ON CONFLICT (slug) DO UPDATE SET
                    api_url = EXCLUDED.api_url,
                    api_method = EXCLUDED.api_method,
                    automation_allowed = EXCLUDED.automation_allowed,
                    auth_type = EXCLUDED.auth_type,
                    badge_required = EXCLUDED.badge_required,
                    submission_status = EXCLUDED.submission_status,
                    review_status = EXCLUDED.review_status,
                    publication_url = EXCLUDED.publication_url,
                    updated_at = NOW()
            """)

        # Безопасная миграция существующей базы:
        # добавляем счётчик бесплатных входов, если его ещё нет.
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS free_entries_used INTEGER NOT NULL DEFAULT 0
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS free_entries_limit INTEGER NOT NULL DEFAULT 3
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS unlimited_access BOOLEAN NOT NULL DEFAULT FALSE
            """)

        conn.commit()



def get_app_settings():
    """Возвращает глобальные настройки ReelForge AI."""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ai_provider, ai_model, plan
                FROM app_settings
                WHERE id = 1
            """)
            row = cur.fetchone()

    if not row:
        return {
            "ai_provider": "groq",
            "ai_model": "openai/gpt-oss-120b",
            "plan": "free",
        }

    return {
        "ai_provider": row[0],
        "ai_model": row[1],
        "plan": row[2],
    }


def save_app_settings(ai_provider, ai_model, plan):
    """Сохраняет глобальные настройки ReelForge AI."""
    allowed_providers = set(PROVIDER_CONFIG.keys())
    allowed_plans = {"free", "basic", "pro", "unlimited"}

    if ai_provider not in allowed_providers:
        raise ValueError("Некорректный AI provider")

    if plan not in allowed_plans:
        raise ValueError("Некорректный тариф")

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings
                    (id, ai_provider, ai_model, plan, updated_at)
                VALUES
                    (1, %s, %s, %s, NOW())
                ON CONFLICT (id)
                DO UPDATE SET
                    ai_provider = EXCLUDED.ai_provider,
                    ai_model = EXCLUDED.ai_model,
                    plan = EXCLUDED.plan,
                    updated_at = NOW()
            """, (ai_provider, ai_model, plan))

        conn.commit()

    print(
        f"[ADMIN] settings saved provider={ai_provider} "
        f"model={ai_model} plan={plan}",
        flush=True
    )


def ensure_user(user_key):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_key)
                VALUES (%s)
                ON CONFLICT (user_key) DO NOTHING
            """, (user_key,))
        conn.commit()



def get_or_create_user_by_email(email):
    email = (email or "").strip().lower()

    if not email:
        raise ValueError("Email не указан")

    user_key = "email:" + email

    ensure_user(user_key)

    return user_key


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



def consume_free_entry(user_key):
    """
    Атомарно списывает бесплатный вход пользователя.

    unlimited_access=True:
        доступ всегда разрешён.

    Иначе:
        доступ разрешён пока free_entries_used < free_entries_limit.
    """
    if not user_key:
        return False

    ensure_user(user_key)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET free_entries_used =
                    CASE
                        WHEN unlimited_access THEN free_entries_used
                        ELSE free_entries_used + 1
                    END
                WHERE user_key = %s
                  AND (
                      unlimited_access = TRUE
                      OR free_entries_used < free_entries_limit
                  )
                RETURNING
                    free_entries_used,
                    free_entries_limit,
                    unlimited_access
            """, (user_key,))

            row = cur.fetchone()
            conn.commit()

    if row:
        used, limit, unlimited = row

        print(
            f"[FREE] consumed user={user_key} "
            f"used={used} limit={limit} unlimited={unlimited}",
            flush=True
        )
        return True

    print(
        f"[FREE] LIMIT REACHED user={user_key}",
        flush=True
    )
    return False

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

app.secret_key = os.getenv("SECRET_KEY") or os.getenv("ADMIN_RESET_TOKEN", "")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True

try:
    init_db()
    print("POSTGRES INIT OK")
    print("[RUNTIME PATH]", os.environ.get("PATH"), flush=True)
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

UPLOAD_DIR = os.path.expanduser(os.environ.get("UPLOAD_DIR", "~/reelforge-test/uploads"))
OUTPUT_DIR = os.path.expanduser(os.environ.get("OUTPUT_DIR", "~/reelforge-test/outputs"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

_groq_client = None
_openai_client = None
PROVIDER_CONFIG = {
    "openai": {"name": "OpenAI", "key_env": "OPENAI_API_KEY", "base_url": None, "models": ["gpt-5.4-mini", "gpt-5.6-luna"]},
    "groq": {"name": "Groq", "key_env": "GROQ_API_KEY", "base_url": "https://api.groq.com/openai/v1", "models": ["openai/gpt-oss-120b"]},
    "gemini": {"name": "Google Gemini", "key_env": "GEMINI_API_KEY", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "models": ["gemini-2.5-flash", "gemini-2.5-pro"]},
    "cerebras": {"name": "Cerebras", "key_env": "CEREBRAS_API_KEY", "base_url": "https://api.cerebras.ai/v1", "models": ["gpt-oss-120b"]},
    "mistral": {"name": "Mistral", "key_env": "MISTRAL_API_KEY", "base_url": "https://api.mistral.ai/v1", "models": ["mistral-small-latest", "mistral-large-latest"]},
    "openrouter": {"name": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "base_url": "https://openrouter.ai/api/v1", "models": ["openrouter/free"]}
}
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

def get_available_providers():
    return {
        key: {**cfg, "connected": bool(os.getenv(cfg["key_env"]))}
        for key, cfg in PROVIDER_CONFIG.items()
    }


def get_provider_admin_info():
    result = {}
    for key, cfg in PROVIDER_CONFIG.items():
        api_key = os.getenv(cfg["key_env"], "")
        result[key] = {
            **cfg,
            "connected": bool(api_key),
            "key_mask": ("••••••••" + api_key[-4:]) if len(api_key) >= 4 else ""
        }
    return result


def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is not set")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def get_openai_client():
    global _openai_client

    if _openai_client is None:
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set")

        _openai_client = OpenAI(api_key=api_key)

    return _openai_client


def create_provider_client(provider):
    cfg = PROVIDER_CONFIG.get(provider)
    if not cfg:
        raise ValueError(f"Неизвестный AI provider: {provider}")

    api_key = os.getenv(cfg["key_env"])
    if not api_key:
        raise ValueError(f"{cfg['key_env']} environment variable is not set")

    from openai import OpenAI

    kwargs = {"api_key": api_key}
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]

    return OpenAI(**kwargs)


def get_ai_client():
    settings = get_app_settings()
    provider = settings.get("ai_provider", "groq")
    model = settings.get("ai_model", "openai/gpt-oss-120b")
    if provider not in PROVIDER_CONFIG:
        provider = "groq"
    cfg = PROVIDER_CONFIG[provider]
    if model not in cfg["models"]:
        model = cfg["models"][0]
    return create_provider_client(provider), model


# ============================================================

# ============================================================
# PEXELS — визуалы для PROMPT MODE
# ============================================================

def capture_website_screenshot(url, output_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        import shutil
        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        browser = p.chromium.launch(headless=True, executable_path=chromium_path) if chromium_path else p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.screenshot(path=output_path, full_page=False)
        browser.close()
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
        raise RuntimeError("Скриншот сайта не создан")
    return output_path

def fetch_website_text(url):
    import requests
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, urljoin

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Некорректная ссылка на сайт")

    queue = [url.strip()]
    visited = set()
    pages = []

    while queue and len(visited) < 5:
        current = queue.pop(0)
        if current in visited:
            continue

        try:
            response = requests.get(
                current,
                timeout=12,
                headers={"User-Agent": "ReelForgeAI/1.0"}
            )
            response.raise_for_status()
        except Exception:
            continue

        visited.add(current)
        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        text = " ".join(soup.stripped_strings)
        if text:
            pages.append(f"PAGE: {current}\n{text[:5000]}")

        if len(visited) < 5:
            priority_links = []
            other_links = []

            for link in soup.find_all("a", href=True):
                absolute = urljoin(current, link["href"])
                link_parsed = urlparse(absolute)
                if link_parsed.netloc != parsed.netloc:
                    continue
                if absolute in visited or absolute in queue:
                    continue

                path = link_parsed.path.lower()
                if any(
                    key in path
                    for key in (
                        "about", "product", "feature", "pricing",
                        "service", "solution", "blog", "contact"
                    )
                ):
                    priority_links.append(absolute)
                else:
                    other_links.append(absolute)

            queue.extend(priority_links)
            queue.extend(other_links)

    return "\n\n".join(pages)[:20000]


def search_pexels_media(query, per_page=8):
    """
    Ищет видео и фотографии Pexels по запросу.
    Видео возвращаются первыми, затем фотографии.
    """
    import requests

    api_key = os.getenv("PEXELS_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("PEXELS_API_KEY не установлен")

    query = (query or "").strip()
    if not query:
        return []

    headers = {
        "Authorization": api_key,
        "User-Agent": "ReelForgeAI/1.0",
    }

    results = []

    try:
        # Сначала видео
        video_response = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params={
                "query": query,
                "per_page": per_page,
                "orientation": "portrait",
            },
            timeout=20,
        )

        video_response.raise_for_status()
        video_data = video_response.json()

        for video in video_data.get("videos", []):
            files = video.get("video_files", [])

            # Предпочитаем вертикальные HD-файлы
            candidates = sorted(
                files,
                key=lambda x: (
                    0 if x.get("width", 0) < x.get("height", 0) else 1,
                    abs((x.get("width", 0) or 1) - 720),
                )
            )

            selected = None

            for vf in candidates:
                link = vf.get("link")
                width = vf.get("width", 0)
                height = vf.get("height", 0)

                if link and width and height:
                    selected = vf
                    break

            if selected:
                results.append({
                    "type": "video",
                    "url": selected["link"],
                    "width": selected.get("width", 0),
                    "height": selected.get("height", 0),
                    "title": query,
                    "source": "Pexels",
                    "page": video.get("url", ""),
                })

        # Затем фото как fallback
        photo_response = requests.get(
            "https://api.pexels.com/v1/search",
            headers=headers,
            params={
                "query": query,
                "per_page": per_page,
                "orientation": "portrait",
            },
            timeout=20,
        )

        photo_response.raise_for_status()
        photo_data = photo_response.json()

        for photo in photo_data.get("photos", []):
            src = photo.get("src", {})
            image_url = (
                src.get("large2x")
                or src.get("large")
                or src.get("original")
            )

            if image_url:
                results.append({
                    "type": "image",
                    "url": image_url,
                    "width": photo.get("width", 0),
                    "height": photo.get("height", 0),
                    "title": query,
                    "source": "Pexels",
                    "page": photo.get("url", ""),
                })

        print(
            f"[PEXELS SEARCH] query={query} "
            f"videos={sum(1 for x in results if x['type'] == 'video')} "
            f"images={sum(1 for x in results if x['type'] == 'image')}",
            flush=True,
        )

        return results

    except Exception as e:
        print(f"[PEXELS SEARCH] error query={query}: {e}", flush=True)
        return []


# PROMPT MODE — поиск визуалов через Wikimedia Commons
# ============================================================

def search_wikimedia_images(query, limit=6):
    """
    Ищет изображения в Wikimedia Commons без API-ключа.
    Возвращает список прямых URL изображений.
    """
    import requests

    query = (query or "").strip()
    if not query:
        return []

    url = "https://commons.wikimedia.org/w/api.php"

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": min(int(limit), 20),
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": 1280,
        "format": "json",
        "origin": "*",
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "ReelForgeAI/1.0"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        pages = data.get("query", {}).get("pages", {})

        results = []
        for page in pages.values():
            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue

            info = imageinfo[0]
            image_url = info.get("thumburl") or info.get("url")

            if image_url:
                results.append({
                    "title": page.get("title", ""),
                    "url": image_url,
                })

        return results

    except Exception as e:
        print(f"[PROMPT SEARCH] Wikimedia error: {e}", flush=True)
        return []




def download_pexels_media(url, output_path):
    """
    Скачивает медиафайл Pexels.
    """
    import requests

    headers = {
        "User-Agent": "ReelForgeAI/1.0",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=60,
        stream=True,
    )
    response.raise_for_status()

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    if not os.path.exists(output_path):
        raise RuntimeError("Pexels файл не создан")

    if os.path.getsize(output_path) < 1000:
        raise RuntimeError("Pexels файл слишком маленький")

    return output_path


def prepare_prompt_images(job_dir, scene_plan):
    """
    PROMPT MODE:
    Для каждой AI-сцены ищем Pexels.
    Видео имеют приоритет, фото используются как fallback.

    Важно:
    - Wikimedia не используется.
    - Каждый визуал выбирается отдельно для своей сцены.
    - Сохраняем информацию о типе media для дальнейшего монтажа.
    """

    scenes = scene_plan.get("scenes", [])

    if not scenes:
        raise RuntimeError("Нет сцен для поиска визуалов")

    downloaded = []
    used_urls = set()

    for index, scene in enumerate(scenes):
        query = str(scene.get("search", "")).strip()

        if not query:
            continue

        print(
            f"[PEXELS VISUAL] scene={index} search={query}",
            flush=True
        )

        results = search_pexels_media(query, per_page=8)

        if not results:
            print(
                f"[PEXELS VISUAL] no results scene={index}",
                flush=True
            )
            continue

        selected = None
        output_path = None

        # Видео идут первыми.
        # Если видео не скачивается — пробуем следующее.
        # После видео используем изображения.
        for candidate_no, item in enumerate(results, start=1):
            media_url = item.get("url")

            if not media_url or media_url in used_urls:
                continue

            media_type = item.get("type", "image")

            extension = ".mp4" if media_type == "video" else ".jpg"

            candidate_path = os.path.join(
                job_dir,
                f"media{len(downloaded):03d}{extension}"
            )

            print(
                f"[PEXELS VISUAL] try scene={index} "
                f"candidate={candidate_no}/{len(results)} "
                f"type={media_type}",
                flush=True
            )

            try:
                download_pexels_media(
                    media_url,
                    candidate_path
                )

                selected = item
                output_path = candidate_path
                break

            except Exception as e:
                print(
                    f"[PEXELS VISUAL] candidate failed "
                    f"scene={index} candidate={candidate_no}: {e}",
                    flush=True
                )

                try:
                    if os.path.exists(candidate_path):
                        os.remove(candidate_path)
                except Exception:
                    pass

        if not selected:
            print(
                f"[PEXELS VISUAL] all candidates failed "
                f"scene={index}",
                flush=True
            )
            continue

        used_urls.add(selected["url"])

        print(
            f"[PEXELS VISUAL] SELECTED scene={index} "
            f"type={selected.get('type')} "
            f"title={selected.get('title', '')}",
            flush=True
        )

        downloaded.append({
            "path": output_path,
            "caption": scene.get("caption", ""),
            "source": "Pexels",
            "url": selected.get("url", ""),
            "type": selected.get("type", "image"),
            "scene_index": index,
            "search": query,
        })

    if not downloaded:
        raise RuntimeError(
            "Не удалось найти визуалы Pexels для ролика"
        )

    print(
        f"[PEXELS VISUAL] COMPLETE media={len(downloaded)} "
        f"videos={sum(1 for x in downloaded if x.get('type') == 'video')} "
        f"images={sum(1 for x in downloaded if x.get('type') == 'image')}",
        flush=True
    )

    return downloaded


def generate_prompt_scene_plan(topic, website_text=""):
    """
    Создаёт редактируемый план Prompt Mode.

    План НЕ является готовыми титрами.
    Поле caption используется как описание/текст сцены
    и показывается пользователю для редактирования.
    """

    client, ai_model = get_ai_client()

    user_prompt = (topic or "").strip()
    if not user_prompt:
        raise ValueError("Пустой промт")

    response = client.chat.completions.create(
        model=ai_model,
        messages=[
            {
                "role": "system",
                "content": """Ты — режиссёр коротких вертикальных видео ReelForge AI.

Преврати запрос пользователя в редактируемый план визуального Reels.

Правила:
1. Строго сохраняй основную тему пользователя.
2. Если пользователь указал длительность — используй её.
3. Если длительность не указана — используй примерно 40 секунд.
4. Создай 6–10 сцен.
5. Для каждой сцены создай короткое описание происходящего на экране на русском языке.
6. Поля title и caption должны быть только на русском языке.
6. Это описание НЕ является титром и НЕ будет показываться поверх видео.
7. Для каждой сцены создай точный поисковый запрос на английском языке.
8. Поисковый запрос должен описывать реальный визуальный объект, место, человека, действие или событие.
9. Не придумывай несуществующие факты.
10. Не добавляй объяснения.
11. Верни ТОЛЬКО валидный JSON.

Формат:
{
  "title": "название ролика",
  "duration": 40,
  "use_captions": false,
  "use_voiceover": false,
  "scenes": [
    {
      "caption": "описание сцены",
      "search": "English visual search query"
    }
  ]
}"""
            },
            {
                "role": "user",
                "content": (user_prompt + "\n\nДАННЫЕ САЙТА:\n" + website_text).strip()
            }
        ],
        max_completion_tokens=1400
    )

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        plan = json.loads(raw)
    except Exception as e:
        print(f"[PROMPT PLAN] JSON ERROR: {e}", flush=True)
        print(f"[PROMPT PLAN] RAW: {raw}", flush=True)
        raise RuntimeError("AI вернул некорректный план сцен")

    scenes = plan.get("scenes")

    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("AI не создал сцены")

    clean_scenes = []

    for scene in scenes[:12]:
        if not isinstance(scene, dict):
            continue

        caption = str(scene.get("caption", "")).strip()
        search = str(scene.get("search", "")).strip()

        if caption and search:
            clean_scenes.append({
                "caption": caption[:300],
                "search": search[:300],
            })

    if not clean_scenes:
        raise RuntimeError("Не удалось получить корректные сцены")

    try:
        duration = int(
            plan.get(
                "duration",
                parse_target_duration(topic)
            )
        )
    except Exception:
        duration = parse_target_duration(topic)

    duration = max(5, min(duration, 180))

    result = {
        "title": str(plan.get("title", "")).strip()[:200],
        "duration": duration,
        "use_captions": bool(plan.get("use_captions", False)),
        "use_voiceover": bool(plan.get("use_voiceover", False)),
        "scenes": clean_scenes,
    }

    print(
        f"[PROMPT PLAN] scenes={len(clean_scenes)} duration={duration}",
        flush=True
    )

    return result

def download_prompt_image(url, output_path):
    """
    Надёжная загрузка изображения для prompt-mode.
    Wikimedia может временно отвечать 429.
    """
    import requests
    import time

    headers = {
        "User-Agent": (
            "ReelForgeAI/1.0 "
            "(https://reelforge-landing-steel.vercel.app/; "
            "ReelForgeAI image fetcher)"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,*/*",
    }

    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=30,
                stream=True,
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "5")

                try:
                    wait = min(int(retry_after), 20)
                except Exception:
                    wait = 5

                print(
                    f"[PROMPT DOWNLOAD] 429 attempt={attempt + 1}/3 "
                    f"wait={wait}s",
                    flush=True,
                )

                response.close()
                time.sleep(wait)
                continue

            response.raise_for_status()

            content_type = (
                response.headers.get("content-type", "")
                .lower()
            )

            if not content_type.startswith("image/"):
                raise RuntimeError(
                    f"URL не содержит изображение: {content_type}"
                )

            with open(output_path, "wb") as f:
                for chunk in response.iter_content(
                    chunk_size=64 * 1024
                ):
                    if chunk:
                        f.write(chunk)

            if (
                not os.path.exists(output_path)
                or os.path.getsize(output_path) < 1000
            ):
                raise RuntimeError(
                    "Изображение скачалось некорректно"
                )

            return output_path

        except Exception as e:
            last_error = e

            print(
                f"[PROMPT DOWNLOAD] error attempt={attempt + 1}/3: {e}",
                flush=True,
            )

            if attempt < 2:
                time.sleep(2)

    raise RuntimeError(
        f"Не удалось скачать изображение: {last_error}"
    )


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
.website-input{width:100%;padding:15px;border-radius:14px;background:#0b0b13;color:#fff;border:1px solid #252533;font-size:15px;outline:none;transition:.2s;box-sizing:border-box;}
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
    <h2>🤖 Reels по промту</h2>
    <p class="card-desc">
        Вставь ссылку на сайт или опиши идею.
        AI изучит страницу, подготовит сценарий, найдёт визуалы и соберёт вертикальный Reels.
    </p>

    <form action="/prepare_prompt" method="post">
        <label>🔗 Ссылка на сайт <span style="color:#666">(необязательно)</span></label>
        <input
            type="url"
            name="website_url"
            placeholder="https://example.com"
            class="website-input"
        >

        <label>✨ Что создать? <span style="color:#666">(можно оставить пустым)</span></label>
        <textarea
            name="topic"
            placeholder="Например: Сделай динамичный Reels о продукте, выдели главные преимущества"
        >{{ generated_script|default("") }}</textarea>

        <button class="btn" type="submit">
            🎬 Создать Reels
        </button>
    </form>
</div>

<div class="card">
    <h2>📸 Reels из скриншотов</h2>
    <p class="card-desc">Загрузи изображения — ReelForge добавит субтитры, монтаж и музыку.</p>

    <form action="/create_video" method="post" enctype="multipart/form-data">

        <label>🎯 Тема ролика</label>
        <textarea name="topic" placeholder="Например: Как пользоваться нашим приложением">{{ generated_script|default("") }}</textarea>

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
        <textarea name="topic" placeholder="Например: Обзор продукта за 30 секунд">{{ generated_script|default("") }}</textarea>

        <label>🎞️ Видео-куски</label>
        <input class="file" type="file" name="videos" multiple accept="video/*">
        <p style="color:#666;font-size:13px;margin-top:6px;">
            Максимум: 100 MB на одно видео и 300 MB на весь проект.
        </p>

        <label>🎵 Музыка <span style="color:#666">(необязательно)</span></label>
        <input class="file" type="file" name="music" accept="audio/*">

        
        {{ upload_progress_html|safe }}
        

        

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

    const timer = document.getElementById("timer");
    const status = document.getElementById("statusText");

    let seconds = 0;

    if (window.reelForgeTimer) {
        clearInterval(window.reelForgeTimer);
    }

    window.reelForgeTimer = setInterval(() => {
        seconds++;

        const m = String(Math.floor(seconds / 60)).padStart(2, "0");
        const sec = String(seconds % 60).padStart(2, "0");

        if (timer) timer.textContent = m + ":" + sec;
    }, 1000);

    if (status) {
        status.textContent = "Подготавливаем загрузку...";
    }

    document.querySelectorAll(".step").forEach(x => {
        x.classList.remove("active");
    });

    const first = document.getElementById("s1");
    if (first) first.classList.add("active");
}

async function uploadVideoForm(form) {
    if (form.dataset.uploading === "1") {
        console.warn("[UPLOAD] duplicate submit ignored");
        return;
    }

    form.dataset.uploading = "1";

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
                "\\n\\nМаксимальный размер одного видео — 100 MB." +
                "\\nРазмер этого файла — " +
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
            "\\n\\nМаксимальный общий размер видео — 300 MB." +
            "\\nСейчас выбрано — " +
            (totalVideoSize / 1024 / 1024).toFixed(1) + " MB."
        );
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = "⏳ Загружаем видео...";
    }

    showUploadProgress();

    updateUploadProgress(
        2,
        "📋 Подготовка загрузки",
        "Выбрано видео: <b>" + files.length + "</b><br>" +
        "Общий размер: <b>" +
        (totalVideoSize / 1024 / 1024).toFixed(1) +
        " MB</b>"
    );

    showLoading();

    try {
        // Создаём сессию загрузки
        const startData = new URLSearchParams();
        startData.append("topic", topicInput ? topicInput.value : "");
        startData.append("total", String(files.length));

        let startResponse;
        try {
            startResponse = await fetch("/start_video_upload", {
                method: "POST",
                body: startData
            });
        } catch (e) {
            console.error("[FETCH ERROR] /start_video_upload", e);
            throw new Error("Не удалось связаться с сервером: /start_video_upload — " + e.message);
        }

        if (!startResponse.ok) {
            throw new Error("Не удалось начать загрузку");
        }

        const startResult = await startResponse.json();
        console.log("[UPLOAD-DEBUG] startResult OK");
        const jobId = startResult.job_id;

        updateUploadProgress(
            5,
            "📤 Загрузка видео",
            "Сессия создана.<br>Подготавливаем файлы..."
        );

        // Загружаем каждое видео небольшими частями.
        // Это предотвращает Failed to fetch на больших multipart-запросах.
        const VIDEO_CHUNK_SIZE = 4 * 1024 * 1024;
        console.log("[UPLOAD-DEBUG] entering video chunks");

        for (let i = 0; i < files.length; i++) {
        console.log("[UPLOAD-DEBUG] VIDEO LOOP START", i + 1, "/", files.length);
            const file = files[i];

            const totalChunks = Math.ceil(
                file.size / VIDEO_CHUNK_SIZE
            );

            if (btn) {
                btn.innerHTML =
                    `⏳ Видео ${i + 1} из ${files.length}`;
            }

            for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
                const start = chunkIndex * VIDEO_CHUNK_SIZE;
                const end = Math.min(
                    start + VIDEO_CHUNK_SIZE,
                    file.size
                );

                const blob = file.slice(start, end);

                const fd = new FormData();

                fd.append("job_id", jobId);
                fd.append("index", String(i));
                fd.append("chunk_index", String(chunkIndex));
                fd.append("total_chunks", String(totalChunks));
                fd.append("video", blob, file.name);

                const chunkProgress =
                    5 +
                    (
                        (
                            i +
                            ((chunkIndex + 1) / totalChunks)
                        ) /
                        files.length
                    ) * 65;

                updateUploadProgress(
                    chunkProgress,
                    "📤 Загружаем видео " +
                        (i + 1) +
                        " из " +
                        files.length,
                    "Файл: <b>" + file.name + "</b><br>" +
                    "Часть: <b>" +
                        (chunkIndex + 1) +
                        " / " +
                        totalChunks +
                    "</b><br>" +
                    "Размер: <b>" +
                        (file.size / 1024 / 1024).toFixed(1) +
                        " MB</b>"
                );

                console.log("[UPLOAD-DEBUG] BEFORE FETCH chunk", chunkIndex + 1, "/", totalChunks);

                let response;

                try {
                    response = await fetch("/upload_video_part", {
                        method: "POST",
                        body: fd
                    });
                } catch (e) {
                    console.error(
                        "[FETCH ERROR] /upload_video_part",
                        e
                    );

                    throw new Error(
                        "Не удалось загрузить видео " +
                        (i + 1) +
                        ", часть " +
                        (chunkIndex + 1) +
                        " из " +
                        totalChunks +
                        ": /upload_video_part — " +
                        (e.message || "Failed to fetch")
                    );
                }

                console.log(
                    "[UPLOAD-DEBUG] RESPONSE chunk",
                    chunkIndex + 1,
                    "/",
                    totalChunks,
                    "status=",
                    response.status,
                    "ok=",
                    response.ok
                );

                if (!response.ok) {
                    let message =
                        "Ошибка загрузки видео " +
                        (i + 1) +
                        ", часть " +
                        (chunkIndex + 1);

                    try {
                        const data = await response.json();

                        if (data.error) {
                            message = data.error;
                        }
                    } catch (_) {}

                    throw new Error(message);
                }

                console.log(
                    "[UPLOAD-DEBUG] CHUNK OK, next=",
                    chunkIndex + 2,
                    "/",
                    totalChunks
                );
            }

            updateUploadProgress(
                5 +
                    (((i + 1) / files.length) * 65),
                "✅ Видео " +
                    (i + 1) +
                    " из " +
                    files.length +
                    " загружено",
                "Файл: <b>" +
                    file.name +
                    "</b><br>" +
                "Размер: <b>" +
                    (file.size / 1024 / 1024).toFixed(1) +
                    " MB</b><br>" +
                "Продолжение загрузки..."
            );
        }

        updateUploadProgress(
            72,
            "🎵 Проверка музыки",
            "Видео загружены.<br>Проверяем выбранную фоновую музыку..."
        );

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

                updateUploadProgress(
                    72 + (((i + 1) / totalChunks) * 18),
                    "🎵 Загружаем музыку " + (i + 1) + " из " + totalChunks,
                    "Файл: <b>" + musicFile.name + "</b><br>" +
                    "Часть: <b>" + (i + 1) + " / " + totalChunks + "</b><br>" +
                    "Размер: <b>" +
                    (musicFile.size / 1024 / 1024).toFixed(1) +
                    " MB</b>"
                );

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
        updateUploadProgress(
            92,
            "🎬 Запускаем монтаж",
            "Все файлы загружены.<br>" +
            "Передаём проект ReelForge AI на обработку..."
        );

        if (btn) btn.innerHTML = "🎬 Запускаем монтаж...";

        const finishData = new URLSearchParams();
        finishData.append("job_id", jobId);

        let finishResponse;
        try {
            finishResponse = await fetch("/finish_video_upload", {
                method: "POST",
                body: finishData
            });
        } catch (e) {
            console.error("[FETCH ERROR] /finish_video_upload", e);
            throw new Error("Не удалось завершить загрузку: /finish_video_upload — " + e.message);
        }

        if (!finishResponse.ok) {
            let message = "Не удалось запустить обработку";
            try {
                const data = await finishResponse.json();
                if (data.error) message = data.error;
            } catch (_) {}
            throw new Error(message);
        }

        updateUploadProgress(
            97,
            "⚙️ Монтаж выполняется",
            "Видео загружены.<br>" +
            "AI создаёт субтитры и собирает Reels.<br>" +
            "<b>Не закрывайте страницу.</b>"
        );

        setTimeout(() => {
            window.location.href =
                "/status/" + jobId + "?access=rf2026free";
        }, 500);

    } catch (error) {
        console.error(error);
        alert("Ошибка загрузки: " + error.message);
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = "🚀 Собрать Reels из видео";
        }
        form.dataset.uploading = "0";
    }
}

document.querySelectorAll("form").forEach(form => {
    form.addEventListener("submit", event => {
        if (form.id === "videoUploadForm") {
            event.preventDefault();

            // Показываем прогресс МГНОВЕННО при нажатии кнопки.
            showUploadProgress();
            updateUploadProgress(
                1,
                "📋 Подготовка загрузки",
                "Проверяем выбранные видео..."
            );

            // Затем запускаем асинхронную загрузку.
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


<div style="text-align:center;margin:24px 0">
  <a href="https://www.promptfrenzy.com/directory"
     target="_blank"
     rel="dofollow"
     title="Featured on PromptFrenzy AI Directory">
    <img src="https://www.promptfrenzy.com/badges/directory-mono-light.svg"
         alt="Featured on PromptFrenzy AI Directory"
         width="220"
         height="44"
         loading="lazy">
  </a>
</div>

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
            max-width: 980px;
            margin: 0 auto;
            padding: 12px 10px 24px;
        }

        .logo {
            text-align: center;
            font-size: 23px;
            font-weight: 800;
            margin-bottom: 12px;
        }

        .logo span {
            color: #a855f7;
        }

        .success {
            text-align: center;
            margin-bottom: 8px;
        }

        .check {
            width: 46px;
            height: 46px;
            margin: 0 auto 7px;
            border-radius: 50%;
            background: #22c55e;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 27px;
        }

        h1 {
            margin: 0 0 3px;
            font-size: 25px;
        }

        .subtitle {
            margin: 0;
            color: #9ca3af;
            font-size: 15px;
        }

        .video-card {
            width: 100%;
            background: #111118;
            border: 1px solid #272733;
            border-radius: 14px;
            padding: 4px;
            margin-top: 8px;
            overflow: hidden;
        }

        video {
            display: block;
            width: 100%;
            height: auto;
            max-height: 90vh;
            border-radius: 10px;
            background: #000;
            object-fit: contain;
        }

        .actions {
            display: flex;
            flex-direction: row;
            gap: 5px;
            margin-top: 3px;
        }

        .button {
            display: block;
            width: 100%;
            padding: 6px 8px;
            min-height: 34px;
            border-radius: 8px;
            text-align: center;
            text-decoration: none;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.2;
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
                padding-top: 28px;
            }

            .video-card {
                padding: 8px;
            }

            .actions {
                flex-direction: row;
                gap: 6px;
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

        {% if video_url %}
        <div style="
            margin-top:3px;
            margin-bottom:3px;
            border-radius:12px;
            overflow:hidden;
            border:1px solid #3a3a4a;
            background:#111118;
        ">
            <a href="/" style="
                display:flex;
                align-items:center;
                justify-content:center;
                gap:10px;
                min-height:58px;
                padding:8px 12px;
                color:#fff;
                text-decoration:none;
            ">
                <div style="
                    font-size:34px;
                    line-height:1;
                ">🐱</div>

                <div style="text-align:left;">
                    <div style="
                        font-size:16px;
                        font-weight:800;
                    ">ReelForge AI</div>

                    <div style="
                        font-size:12px;
                        color:#c4b5fd;
                        margin-top:2px;
                    ">👆 Нажми на котика</div>
                </div>
            </a>

            <a href="/static/reelforge-banner.svg"
               download="ReelForge-AI-banner.svg"
               style="
                   display:block;
                   text-align:center;
                   padding:5px;
                   border-top:1px solid #292936;
                   color:#d8b4fe;
                   font-size:11px;
                   font-weight:700;
                   text-decoration:none;
               ">
                ⬇ Скачать баннер
            </a>
        </div>
        {% endif %}

        {% if topic %}
        <div class="script result-editor" style="
            margin-top:2px;
            padding:12px;
            background:#111118;
            border:1px solid #3a3a4a;
            border-radius:14px;
        ">
            <div style="
                font-size:19px;
                font-weight:800;
                margin-bottom:5px;
            ">
                ✏️ Редактор новой версии
            </div>


            <form action="/create_reel_from_prompt" method="post">

                <div class="result-editor-options" style="
                    display:flex;
                    flex-wrap:wrap;
                    gap:6px;
                    margin-bottom:9px;
                ">
                    <label style="
                        display:inline-flex;
                        align-items:center;
                        gap:6px;
                        padding:7px 9px;
                        margin:0;
                        background:#08080e;
                        border:1px solid #30303b;
                        border-radius:9px;
                        cursor:pointer;
                        flex:1 1 180px;
                    ">
                        <input
                            type="checkbox"
                            name="editor_captions"
                            value="1"
                            style="width:18px;height:18px;"
                            {% if 'титр' in topic.lower() or 'субтитр' in topic.lower() or 'captions' in topic.lower() or 'subtitles' in topic.lower() %}checked{% endif %}
                        >
                        <span style="font-size:14px;font-weight:700;">
                            📝 Титры / текст
                        </span>
                    </label>

                    <label style="
                        display:inline-flex;
                        align-items:center;
                        gap:6px;
                        padding:7px 9px;
                        margin:0;
                        background:#08080e;
                        border:1px solid #30303b;
                        border-radius:9px;
                        cursor:pointer;
                        flex:1 1 180px;
                    ">
                        <input
                            type="checkbox"
                            name="editor_voiceover"
                            value="1"
                            style="width:18px;height:18px;"
                            {% if 'голос за кадром' in topic.lower() or 'озвуч' in topic.lower() or 'voice-over' in topic.lower() or 'voiceover' in topic.lower() or 'narration' in topic.lower() %}checked{% endif %}
                        >
                        <span style="font-size:14px;font-weight:700;">
                            🎤 Голос за кадром
                        </span>
                    </label>
                </div>

                <div style="
                    display:flex;
                    align-items:center;
                    gap:8px;
                    margin:0 0 7px;
                ">
                    <div style="
                        font-size:13px;
                        font-weight:700;
                        white-space:nowrap;
                    ">
                        📍 Положение титров
                    </div>

                    <select
                        name="editor_subtitle_position"
                        style="
                            flex:1;
                            padding:7px 9px;
                            border-radius:8px;
                            border:1px solid #30303b;
                            background:#08080e;
                            color:#fff;
                            font-size:13px;
                        "
                    >
                        <option value="bottom">Снизу</option>
                        <option value="top">Сверху</option>
                    </select>
                </div>

                <div style="
                    margin-bottom:5px;
                    font-size:13px;
                    font-weight:700;
                ">
                    📝 Промт
                </div>

                <input type="hidden" name="website_url" value="{{ website_url }}">

                <textarea
                    name="topic"
                    required
                    style="
                        width:100%;
                        min-height:500px;
                        padding:12px;
                        border-radius:11px;
                        border:1px solid #3a3a4a;
                        background:#08080e;
                        color:#fff;
                        font-size:15px;
                        line-height:1.5;
                        resize:vertical;
                        outline:none;
                        display:block;
                    "
                >{{ topic }}</textarea>

                <button
                    class="button download"
                    type="submit"
                    style="
                        width:100%;
                        border:0;
                        cursor:pointer;
                        margin-top:9px;
                        font-size:15px;
                        padding:10px 12px;
                        border-radius:9px;
                    "
                >
                    🔄 Создать новую версию
                </button>

            </form>
        </div>
        {% endif %}

        {% if script %}
        <div class="script">{{ script }}</div>

        <div class="actions" style="margin-top:20px;">
            <form action="/prepare_reel" method="post" style="margin:0;">
                <input type="hidden" name="website_url" value="{{ website_url }}">

                <textarea name="script" style="display:none;">{{ script }}</textarea>
                <button class="button download" type="submit" style="border:0;cursor:pointer;">
                    🎬 Создать Reels из этого сценария
                </button>
            </form>
        </div>
        {% endif %}

    </main>
</body>
</html>
"""

UPLOAD_PROGRESS_HTML = """
<div id="uploadProgress" style="
    display:none;
    position:fixed;
    inset:0;
    z-index:99999;
    background:rgba(7,7,13,.96);
    align-items:center;
    justify-content:center;
    padding:20px;
">
    <div style="
        width:min(520px,100%);
        background:#111118;
        border:1px solid #2d2d3a;
        border-radius:22px;
        padding:28px;
        color:#fff;
        text-align:center;
        box-shadow:0 20px 70px rgba(0,0,0,.55);
    ">
        <div style="
            font-size:25px;
            font-weight:800;
            margin-bottom:10px;
        ">
            🚀 ReelForge AI
        </div>

        <div id="progressStage" style="
            font-size:18px;
            font-weight:700;
            margin-bottom:18px;
        ">
            Подготовка...
        </div>

        <div style="
            width:100%;
            height:16px;
            background:#272733;
            border-radius:20px;
            overflow:hidden;
            margin-bottom:10px;
        ">
            <div id="progressBar" style="
                width:0%;
                height:100%;
                background:#a855f7;
                border-radius:20px;
                transition:width .3s ease;
            "></div>
        </div>

        <div id="progressPercent" style="
            font-size:16px;
            font-weight:700;
            color:#c4c4d0;
            margin-bottom:18px;
        ">
            0%
        </div>

        <div id="progressDetails" style="
            font-size:14px;
            line-height:1.7;
            color:#9ca3af;
        ">
            Подготавливаем загрузку...
        </div>
    </div>
</div>

<script>
function showUploadProgress() {
    const box = document.getElementById("uploadProgress");

    if (box) {
        box.style.display = "flex";
    }

    document.body.style.overflow = "hidden";
}

function updateUploadProgress(percent, stage, details) {
    const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));

    const bar = document.getElementById("progressBar");
    const percentEl = document.getElementById("progressPercent");
    const stageEl = document.getElementById("progressStage");
    const detailsEl = document.getElementById("progressDetails");

    if (bar) bar.style.width = safePercent + "%";
    if (percentEl) percentEl.textContent = Math.round(safePercent) + "%";
    if (stageEl) stageEl.textContent = stage || "";
    if (detailsEl) detailsEl.innerHTML = details || "";

    const status = document.getElementById("statusText");
    if (status) status.textContent = stage || "";

    const oldProgress = document.querySelector(".progress-bar");
    if (oldProgress) {
        oldProgress.style.animation = "none";
        oldProgress.style.width = safePercent + "%";
    }

    let activeStep = 0;

    if (safePercent >= 92) {
        activeStep = 4;
    } else if (safePercent >= 80) {
        activeStep = 3;
    } else if (safePercent >= 65) {
        activeStep = 2;
    }

    const steps = ["s1", "s2", "s3", "s4", "s5"];

    steps.forEach((id, index) => {
        const el = document.getElementById(id);
        if (el) {
            el.classList.toggle("active", index === activeStep);
        }
    });
}
</script>
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
    client, ai_model = get_ai_client()
    user_prompt = (topic or "").strip()

    if user_prompt:
        task = f"""ЗАДАНИЕ ПОЛЬЗОВАТЕЛЯ:
{user_prompt}

Создай готовый сценарий Instagram Reels строго по основной идее этого задания.
Если пользователь указал конкретные требования — соблюдай их.
Если какие-то параметры не указаны — выбери их самостоятельно.
Не меняй основную тему пользователя.
"""
    else:
        task = """ПОЛЬЗОВАТЕЛЬ НЕ УКАЗАЛ ЗАДАНИЕ.

Создай полностью автоматически готовый сценарий Instagram Reels.
Сам выбери интересную тему, сильный хук, структуру, сцены, стиль и призыв к действию.
Продолжительность — примерно 40 секунд.
"""

    response = client.chat.completions.create(
        model=ai_model,
        messages=[
            {
                "role": "system",
                "content": """Ты — профессиональный сценарист Instagram Reels.

Правила:
1. Если пользователь дал задание — следуй его основной идее.
2. Никогда не заменяй тему пользователя своей темой.
3. Всё, что пользователь не указал, выбирай самостоятельно.
4. Если указана длительность — соблюдай её.
5. Создавай сильный хук в начале.
6. Разбивай сценарий на логичные сцены.
7. Пиши конкретно и динамично.
8. Сценарий должен быть пригоден для создания субтитров.
9. Не копируй пользовательский промт как готовый текст видео.
10. Верни только сценарий без объяснений."""
            },
            {
                "role": "user",
                "content": task
            }
        ],
        max_completion_tokens=800
    )

    return response.choices[0].message.content


def generate_voiceover_text(scene_plan):
    """
    Создаёт текст для закадрового голоса на основе готового
    плана сцен Prompt Mode.

    Важно:
    - не использует исходный пользовательский промт напрямую;
    - использует только готовый план сцен;
    - возвращает обычный текст без markdown.
    """
    scenes = scene_plan.get("scenes", []) if isinstance(scene_plan, dict) else []

    if not scenes:
        return ""

    scene_text = "\n".join(
        str(scene.get("caption", "")).strip()
        for scene in scenes
        if str(scene.get("caption", "")).strip()
    )

    if not scene_text:
        return ""

    client, ai_model = get_ai_client()

    response = client.chat.completions.create(
        model=ai_model,
        messages=[
            {
                "role": "system",
                "content": """Ты — профессиональный сценарист voice-over для Instagram Reels.

На вход ты получаешь готовые описания сцен.

Создай короткий естественный текст для закадрового голоса.

Правила:
1. Используй только информацию, содержащуюся в сценах.
2. Не придумывай факты.
3. Текст должен звучать естественно при озвучке.
4. Пиши динамично и коротко.
5. Не используй markdown.
6. Не добавляй заголовок или пояснения.
7. Верни только готовый текст voice-over."""
            },
            {
                "role": "user",
                "content": f"""ГОТОВЫЕ СЦЕНЫ:

{scene_text}

Создай единый текст для voice-over этого Reels."""
            }
        ],
        max_completion_tokens=700
    )

    text = (response.choices[0].message.content or "").strip()

    text = re.sub(r"^```(?:text)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    return text

def generate_captions(topic, count):
    client, ai_model = get_ai_client()
    try:
        response = client.chat.completions.create(
            model=ai_model,
            messages=[
                {"role": "system", "content": """Ты — редактор субтитров для Instagram Reels.

На вход ты получаешь ГОТОВЫЙ сценарий, созданный другим AI по заданию пользователя.

Твоя задача:
1. Взять смысл ИМЕННО из этого сценария.
2. Создать короткие субтитры, соответствующие сценам сценария.
3. Вернуть ровно указанное количество фраз.
4. Каждая фраза — максимум 6 слов.
5. Не добавлять новую информацию, которой нет в сценарии.
6. Не использовать исходный промт пользователя.
7. Не писать пояснения, заголовки или markdown.

Отвечай ТОЛЬКО валидным JSON-массивом строк."""},
                {"role": "user", "content": f"""ГОТОВЫЙ СЦЕНАРИЙ REELS:

{topic}

Создай ровно {count} коротких субтитров по этому сценарию.
Формат: ["фраза1", "фраза2", "..."]"""}
            ],
            max_completion_tokens=400
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
    # Не выводим исходный промт пользователя на видео.
    # Если AI не смог создать captions — оставляем видео без текста.
    return []

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


def parse_video_instructions(topic):
    """
    Извлекает технические требования из пользовательского промта.

    Возвращает:
      crop_mode
      subtitle_position
      use_captions
      use_voiceover
      effects
    """
    text = (topic or "").lower().strip()

    instructions = {
        "crop_mode": "pad",
        "subtitle_position": "bottom",
        "use_captions": False,
        "use_voiceover": False,
        "effects": [],
    }

    # ============================================================
    # КАДРИРОВАНИЕ
    # ============================================================

    crop_patterns = [
        r"\bcrop\b",
        r"\bcover\b",
        r"обрез(?:ать|ка|ь)",
        r"заполнить\s+кадр",
        r"без\s+ч[её]рных\s+полос",
    ]

    pad_patterns = [
        r"\bpad\b",
        r"полос(?:ы|ами|ах)",
        r"не\s+обрез(?:ать|ай|ать)",
        r"сохранить\s+весь\s+кадр",
    ]

    if any(re.search(pattern, text) for pattern in pad_patterns):
        instructions["crop_mode"] = "pad"
    elif any(re.search(pattern, text) for pattern in crop_patterns):
        instructions["crop_mode"] = "cover"

    # ============================================================
    # ПОЗИЦИЯ ТИТРОВ
    # ============================================================

    subtitle_top_patterns = [
        r"subtitle\s+top",
        r"subtitles\s+top",
        r"субтитр(?:ы|ов)?\s+(?:сверху|вверху|наверху)",
        r"текст\s+(?:сверху|вверху|наверху)",
    ]

    subtitle_bottom_patterns = [
        r"subtitle\s+bottom",
        r"subtitles\s+bottom",
        r"субтитр(?:ы|ов)?\s+(?:снизу|внизу)",
        r"текст\s+(?:снизу|внизу)",
    ]

    if any(re.search(pattern, text) for pattern in subtitle_top_patterns):
        instructions["subtitle_position"] = "top"
    elif any(re.search(pattern, text) for pattern in subtitle_bottom_patterns):
        instructions["subtitle_position"] = "bottom"

    # ============================================================
    # ТИТРЫ / СУБТИТРЫ
    # ============================================================

    captions_enable_patterns = [
        r"\bс\s+титрами\b",
        r"\bс\s+субтитрами\b",
        r"\bдобав(?:ь|ить)\s+титр",
        r"\bдобав(?:ь|ить)\s+субтитр",
        r"\bпокажи\s+текст\b",
        r"\bтекст\s+на\s+экране\b",
        r"\bтекст\s+поверх\s+видео\b",
        r"\bcaptions?\b",
        r"\bsubtitles?\b",
        r"\bon[- ]screen\s+text\b",
        r"\btext\s+on\s+screen\b",
    ]

    captions_disable_patterns = [
        r"\bбез\s+титров\b",
        r"\bбез\s+субтитров\b",
        r"\bне\s+добавляй\s+титры\b",
        r"\bне\s+добавляй\s+субтитры\b",
        r"\bбез\s+текста\s+на\s+экране\b",
        r"\bno\s+captions?\b",
        r"\bno\s+subtitles?\b",
        r"\bno\s+on[- ]screen\s+text\b",
    ]

    captions_requested = any(
        re.search(pattern, text)
        for pattern in captions_enable_patterns
    )

    captions_forbidden = any(
        re.search(pattern, text)
        for pattern in captions_disable_patterns
    )

    if captions_requested and not captions_forbidden:
        instructions["use_captions"] = True

    # ============================================================
    # ГОЛОС ЗА КАДРОМ / VOICE-OVER
    # ============================================================

    voice_enable_patterns = [
        r"голос\s+за\s+кадром",
        r"озвучк(?:а|у|ой)",
        r"озвуч(?:ь|ить|ка)",
        r"диктор",
        r"дикторская\s+озвучка",
        r"закадров(?:ый|ая|ое)\s+голос",
        r"voice[- ]?over",
        r"voiceover",
        r"narration",
        r"narrator",
        r"spoken\s+voice",
    ]

    voice_disable_patterns = [
        r"без\s+голоса",
        r"без\s+озвучки",
        r"без\s+голоса\s+за\s+кадром",
        r"не\s+добавляй\s+голос",
        r"не\s+добавляй\s+озвучку",
        r"no\s+voice[- ]?over",
        r"no\s+voiceover",
        r"no\s+narration",
    ]

    voice_requested = any(
        re.search(pattern, text)
        for pattern in voice_enable_patterns
    )

    voice_forbidden = any(
        re.search(pattern, text)
        for pattern in voice_disable_patterns
    )

    if voice_requested and not voice_forbidden:
        instructions["use_voiceover"] = True

    print(
        "[INSTRUCTIONS] "
        + "crop_mode=" + instructions["crop_mode"]
        + " subtitle_position=" + instructions["subtitle_position"]
        + " use_captions=" + str(instructions["use_captions"])
        + " use_voiceover=" + str(instructions["use_voiceover"])
        + " effects=" + str(instructions["effects"]),
        flush=True,
    )

    return instructions


def get_scale_vf(crop_mode="pad"):
    """
    Вертикальный формат 720x1280.

    pad   = сохраняем весь кадр, добавляем полосы.
    cover = заполняем весь кадр, обрезаем лишнее.
    """
    if crop_mode == "cover":
        vf = (
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,"
            "format=yuv420p"
        )
    else:
        vf = (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            "format=yuv420p"
        )

    print(
        "[CROP] mode=" + crop_mode + " vf=" + vf,
        flush=True,
    )

    return vf


def get_video_duration(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def parse_scene_effects(topic, count):
    """
    Разбирает сценарий по временным сценам и определяет эффекты
    отдельно для каждой сцены.

    Например:
      0:00-0:03 ... зум-ин
      0:04-0:08 ... зум-аут
    """
    text = topic or ""
    scenes = []

    # Ищем временные интервалы вида 0:00-0:03
    pattern = re.compile(
        r'(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})(.*?)(?=\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}|$)',
        re.S
    )

    for m in pattern.finditer(text):
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        desc = m.group(5).lower()

        effects = []

        if re.search(r"зум[- ]?ин|zoom[- ]?in|приближ", desc):
            effects.append("zoom_in")

        if re.search(r"зум[- ]?аут|zoom[- ]?out|отъезж|отдал", desc):
            effects.append("zoom_out")

        if re.search(r"световой всплеск|вспышк|flash", desc):
            effects.append("flash")

        scenes.append({
            "start": start,
            "end": end,
            "effects": effects,
        })

    # Если временных сцен нет — сохраняем старое поведение.
    if not scenes:
        global_effects = []
        low = text.lower()

        if re.search(r"зум[- ]?ин|zoom[- ]?in|приближ", low):
            global_effects.append("zoom_in")

        if re.search(r"зум[- ]?аут|zoom[- ]?out|отъезж|отдал", low):
            global_effects.append("zoom_out")

        if re.search(r"световой всплеск|вспышк|flash", low):
            global_effects.append("flash")

        return [
            {"start": 0, "end": 0, "effects": global_effects}
            for _ in range(count)
        ]

    # Привязываем сцены к исходным видео.
    result = []

    for i in range(count):
        if i < len(scenes):
            result.append(scenes[i])
        else:
            result.append({
                "start": scenes[-1]["end"],
                "end": scenes[-1]["end"],
                "effects": [],
            })

    print(
        "[SCENES] " +
        str(result),
        flush=True,
    )

    return result


def extract_auto_clip(
    src_path,
    out_path,
    clip_seconds=4,
    instructions=None,
    scene_effects=None,
):
    """
    Универсальный клип для PROMPT AUTO MIX.

    Поддерживает:
    - MP4/MOV/WebM и другие видео;
    - JPG/JPEG/PNG/WebP изображения.

    Изображение превращается в вертикальный видеоклип
    с плавным zoom-in/zoom-out.
    """

    instructions = instructions or {}

    effects = (
        scene_effects
        if scene_effects is not None
        else instructions.get("effects", [])
    )

    ext = os.path.splitext(src_path)[1].lower()

    # ============================================================
    # IMAGE -> VIDEO
    # ============================================================

    if ext in (".jpg", ".jpeg", ".png", ".webp", ".avif"):

        vf_parts = [
            get_scale_vf(instructions.get("crop_mode", "pad"))
        ]

        # Плавное движение по изображению.
        if "zoom_out" in effects and "zoom_in" not in effects:
            zoom_expr = (
                "if(lte(on,1),1.30,"
                "max(1.30-0.30*on/(24*"
                f"{clip_seconds}),1.0))"
            )
        else:
            zoom_expr = (
                "min(1.0+0.30*on/(24*"
                f"{clip_seconds}),1.30)"
            )

        vf_parts.append(
            "zoompan="
            f"z='{zoom_expr}':"
            "x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':"
            f"d=1:s=720x1280:fps=24"
        )

        if "flash" in effects:
            vf_parts.append(
                "eq=brightness='if(lt(t,0.35),"
                "0.65*(1-t/0.35),0)'"
            )

        vf = ",".join(vf_parts)

        print(
            f"[IMAGE CLIP] src={src_path} duration={clip_seconds} "
            f"effects={effects}",
            flush=True,
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", src_path,
            "-t", str(clip_seconds),
            "-vf", vf,
            "-r", "24",
            "-an",
            "-preset", "ultrafast",
            "-threads", "1",
            "-pix_fmt", "yuv420p",
            out_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"[IMAGE CLIP exit code {result.returncode}] "
                + result.stderr[-1500:]
            )

        return clip_seconds

    # ============================================================
    # VIDEO -> VIDEO
    # ============================================================

    duration = get_video_duration(src_path)

    if duration <= 0:
        raise RuntimeError(
            f"Не удалось определить длительность видео: {src_path}"
        )

    vf_parts = [
        get_scale_vf(instructions.get("crop_mode", "pad"))
    ]

    has_zoom_in = "zoom_in" in effects
    has_zoom_out = "zoom_out" in effects

    if has_zoom_in and has_zoom_out:
        vf_parts.append(
            "zoompan="
            "z='if(lte(on,frames*0.5),"
            "min(1+on/(frames*0.5)*0.35,1.35),"
            "max(1.35-(on-frames*0.5)/(frames*0.5)*0.35,1.0))':"
            "x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':"
            "d=1:s=720x1280:fps=24"
        )

    elif has_zoom_in:
        vf_parts.append(
            "zoompan="
            "z='min(zoom+0.002,1.35)':"
            "x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':"
            "d=1:s=720x1280:fps=24"
        )

    elif has_zoom_out:
        vf_parts.append(
            "zoompan="
            "z='if(lte(zoom,1.0),1.35,"
            "max(zoom-0.002,1.0))':"
            "x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':"
            "d=1:s=720x1280:fps=24"
        )

    if "flash" in effects:
        vf_parts.append(
            "eq=brightness='if(lt(t,0.35),"
            "0.65*(1-t/0.35),0)'"
        )

    vf = ",".join(vf_parts)

    print(
        f"[VIDEO CLIP] src={src_path} duration={clip_seconds} "
        f"effects={effects}",
        flush=True,
    )

    if duration < clip_seconds:
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", src_path,
            "-t", str(clip_seconds),
            "-vf", vf,
            "-r", "24",
            "-an",
            "-preset", "ultrafast",
            "-threads", "1",
            "-pix_fmt", "yuv420p",
            out_path,
        ]
    else:
        start = duration * 0.2

        if start + clip_seconds > duration:
            start = max(0, duration - clip_seconds)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", src_path,
            "-t", str(clip_seconds),
            "-vf", vf,
            "-r", "24",
            "-an",
            "-preset", "ultrafast",
            "-threads", "1",
            "-pix_fmt", "yuv420p",
            out_path,
        ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"[VIDEO CLIP exit code {result.returncode}] "
            + result.stderr[-1500:]
        )

    return clip_seconds


def build_reel_from_videos(
    video_paths,
    output_path,
    captions=None,
    target_duration=40,
    instructions=None,
):
    clip_dir = os.path.dirname(output_path)
    clip_paths = []

    clip_seconds = max(1, target_duration / max(1, len(video_paths)))

    # Разбираем сценарий на отдельные сцены.
    scene_data = parse_scene_effects(
        (instructions or {}).get("_topic", ""),
        len(video_paths),
    )

    for i, vp in enumerate(video_paths):
        clip_path = os.path.join(clip_dir, f"clip{i:03d}.mp4")

        scene = scene_data[i] if i < len(scene_data) else {}
        scene_effects = scene.get("effects", [])

        print(
            f"[SCENE {i}] "
            f"start={scene.get('start')} "
            f"end={scene.get('end')} "
            f"effects={scene_effects}",
            flush=True,
        )

        extract_auto_clip(
            vp,
            clip_path,
            clip_seconds=clip_seconds,
            instructions=instructions,
            scene_effects=scene_effects,
        )
        clip_paths.append(clip_path)

    font_path = get_font_path()
    if captions and font_path:
        for i, clip_path in enumerate(clip_paths):
            cap = captions[i] if i < len(captions) else ""
            if not cap:
                continue
            safe_cap = escape_drawtext(cap)
            tagged_path = clip_path.replace(".mp4", "_cap.mp4")

            subtitle_position = (instructions or {}).get(
                "subtitle_position",
                "bottom",
            )

            if subtitle_position == "top":
                subtitle_y = "120"
            else:
                subtitle_y = "h-320"

            vf = (
                f"drawtext=fontfile='{font_path}':text='{safe_cap}':"
                f"fontsize=42:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:"
                f"x=(w-text_w)/2:y={subtitle_y}"
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


def select_prompt_music(topic=""):
    """
    Автоматически выбирает бесплатную музыку из публичного
    Free To Use API без API key и регистрации.
    """
    import requests
    import os
    import random

    music_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static",
        "music"
    )
    os.makedirs(music_dir, exist_ok=True)

    text = (topic or "").lower()

    if any(x in text for x in (
        "страш", "ужас", "тайн", "мистик",
        "dark", "horror", "mystery", "scary", "suspense"
    )):
        preferred = ["dark", "suspense", "mysterious", "cinematic", "ambient"]

    elif any(x in text for x in (
        "путеше", "путешеств", "travel", "road",
        "приключ", "adventure", "nature", "природ"
    )):
        preferred = ["travel", "adventure", "cinematic", "nature", "inspiring"]

    elif any(x in text for x in (
        "мотива", "успех", "спорт", "energy",
        "motivational", "fitness", "success"
    )):
        preferred = ["motivational", "energy", "inspiring", "electronic"]

    elif any(x in text for x in (
        "груст", "печал", "sad", "emotional",
        "love", "любов"
    )):
        preferred = ["emotional", "piano", "sad", "calm"]

    elif any(x in text for x in (
        "спокой", "релакс", "медита", "calm",
        "relax", "meditation"
    )):
        preferred = ["calm", "ambient", "relaxing", "piano"]

    elif any(x in text for x in (
        "эпич", "герой", "войн", "epic",
        "hero", "cinematic"
    )):
        preferred = ["epic", "cinematic", "dramatic", "inspiring"]

    else:
        preferred = ["inspiring", "cinematic", "ambient", "electronic"]

    print(
        f"[PROMPT MUSIC] preferred={preferred}",
        flush=True
    )

    api_url = "https://api.freetouse.com/v3/music/tracks/all"

    try:
        response = requests.get(
            api_url,
            params={
                "limit": 50,
                "order": "random"
            },
            timeout=20
        )

        response.raise_for_status()

        payload = response.json()

        tracks = payload.get("data", [])

        print(
            f"[PROMPT MUSIC] API tracks={len(tracks)}",
            flush=True
        )

        # Только бесплатные треки с MP3.
        free_tracks = [
            track for track in tracks
            if not track.get("is_premium", True)
            and track.get("files", {}).get("mp3")
        ]

        print(
            f"[PROMPT MUSIC] free tracks={len(free_tracks)}",
            flush=True
        )

        if not free_tracks:
            print(
                "[PROMPT MUSIC] no free tracks",
                flush=True
            )
            return None

        # Сначала пытаемся подобрать трек по жанру/тегам.
        scored = []

        for track in free_tracks:
            text_parts = [
                str(track.get("title", "")),
                str(track.get("genre", "")),
                str(track.get("lyrics", "")),
            ]

            for item in track.get("tags", []) or []:
                text_parts.append(str(item))

            for item in track.get("categories", []) or []:
                if isinstance(item, dict):
                    text_parts.append(str(item.get("name", "")))
                else:
                    text_parts.append(str(item))

            haystack = " ".join(text_parts).lower()

            score = 0

            for word in preferred:
                if word.lower() in haystack:
                    score += 3

            scored.append((score, track))

        scored.sort(
            key=lambda x: x[0],
            reverse=True
        )

        best_score = scored[0][0]

        if best_score > 0:
            candidates = [
                track
                for score, track in scored
                if score == best_score
            ]
            track = random.choice(candidates)
        else:
            track = random.choice(free_tracks)

        title = str(
            track.get("title", "background_music")
        )

        track_id = str(
            track.get("id", "unknown")
        )

        mp3_url = track.get(
            "files", {}
        ).get("mp3")

        if not mp3_url:
            print(
                "[PROMPT MUSIC] selected track has no mp3",
                flush=True
            )
            return None

        safe_name = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "_",
            title
        ).strip("_")[:60] or "background_music"

        music_path = os.path.join(
            music_dir,
            f"{safe_name}_{track_id}.mp3"
        )

        # Используем уже скачанный файл.
        if (
            os.path.exists(music_path)
            and os.path.getsize(music_path) > 10000
        ):
            print(
                f"[PROMPT MUSIC] cached={music_path}",
                flush=True
            )
            return music_path

        print(
            f"[PROMPT MUSIC] downloading "
            f"title={title} "
            f"genre={track.get('genre')} "
            f"score={best_score}",
            flush=True
        )

        audio = requests.get(
            mp3_url,
            timeout=60
        )

        audio.raise_for_status()

        with open(music_path, "wb") as f:
            f.write(audio.content)

        size = os.path.getsize(music_path)

        if size < 10000:
            os.remove(music_path)
            raise RuntimeError(
                "Downloaded music file is too small"
            )

        print(
            f"[PROMPT MUSIC] COMPLETE "
            f"title={title} "
            f"size={size} "
            f"artist={track.get('artists')}",
            flush=True
        )

        return music_path

    except Exception as e:
        print(
            f"[PROMPT MUSIC] ERROR: {e}",
            flush=True
        )
        return None



def generate_voiceover_audio(text, output_path):
    """
    Создаёт MP3 voice-over через gTTS.
    """
    text = (text or "").strip()

    if not text:
        raise RuntimeError("VOICEOVER TEXT IS EMPTY")

    try:
        from gtts import gTTS

        print(
            f"[VOICEOVER] GENERATING text_length={len(text)}",
            flush=True
        )

        tts = gTTS(
            text=text,
            lang="ru",
            slow=False,
        )

        tts.save(output_path)

        if not os.path.exists(output_path):
            raise RuntimeError("VOICEOVER FILE NOT CREATED")

        size = os.path.getsize(output_path)

        if size < 1000:
            raise RuntimeError(
                f"VOICEOVER FILE TOO SMALL: {size}"
            )

        print(
            f"[VOICEOVER] COMPLETE output={output_path} size={size}",
            flush=True
        )

        return output_path

    except Exception as e:
        print(
            f"[VOICEOVER] ERROR: {e}",
            flush=True
        )
        raise

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

def mux_original_audio(video_path, source_video_path, output_path):
    if not os.path.exists(video_path):
        raise RuntimeError(f"VIDEO NOT FOUND: {video_path}")
    if not source_video_path or not os.path.exists(source_video_path):
        raise RuntimeError(f"SOURCE VIDEO NOT FOUND: {source_video_path}")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", source_video_path,
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]

    print(f"[ORIGINAL AUDIO] source={source_video_path}", flush=True)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"[ORIGINAL AUDIO exit code {result.returncode}] "
            + result.stderr[-1500:]
        )

    if not os.path.exists(output_path):
        raise RuntimeError("ORIGINAL AUDIO OUTPUT NOT CREATED")

def add_website_watermark(video_path, output_path, website_url):
    if not website_url:
        shutil.copy(video_path, output_path)
        return
    host = re.sub(r"^https?://", "", website_url).split("/")[0]
    host = escape_drawtext(host)
    font_path = get_font_path()
    cmd = ["ffmpeg","-y","-i",video_path,"-vf",f"drawtext=fontfile='{font_path}':text='{host}':fontsize=28:fontcolor=white@0.9:box=1:boxcolor=black@0.5:boxborderw=8:x=(w-text_w)/2:y=h-70","-c:a","copy","-movflags","+faststart",output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Website watermark error: " + result.stderr[-1000:])

def process_video_job(
    job_id,
    job_dir,
    files_meta,
    music_path,
    topic,
    mode,
    preset_captions=None,
    target_duration_override=None,
    use_captions=None,
    use_voiceover=False,
    voiceover_text=None, website_url=None,
):
    print(f"[JOB {job_id}] START mode={mode} files={len(files_meta)} topic={bool(topic)}", flush=True)
    try:
        script = None
        captions = None
        instructions = parse_video_instructions(topic)
        instructions["_topic"] = topic or ""

        # В PROMPT MODE используем субтитры,
        # которые были созданы AI для сцен.
        if mode == "prompt":
            instructions["_prompt_mode"] = True

        # PROMPT MODE:
        # Пользовательский промт является главным источником
        # требований к ролику.
        if mode == "prompt":
            script = None

            if use_captions is True:
                if preset_captions:
                    captions = list(preset_captions)[:len(files_meta)]
                else:
                    captions = []
                print(
                    f"[AI CAPTIONS] PROMPT MODE ENABLED count={len(captions)}",
                    flush=True
                )
            else:
                captions = []
                print(
                    "[AI CAPTIONS] PROMPT MODE DISABLED",
                    flush=True
                )

            print(
                f"[AI VOICEOVER] enabled={bool(use_voiceover)}",
                flush=True
            )

        # Остальные режимы работают по старой логике.
        else:
            # AI создаёт сценарий.
            try:
                script = generate_script(topic)
                print(
                    f"[AI SCRIPT] generated length={len(script or '')}",
                    flush=True
                )
            except Exception as e:
                print(f"[AI SCRIPT] ERROR: {e}", flush=True)
                script = None

            if preset_captions:
                captions = list(preset_captions)[:len(files_meta)]
                print(
                    f"[AI CAPTIONS] using preset count={len(captions)}",
                    flush=True
                )
            elif instructions.get("use_captions") and script:
                try:
                    captions = generate_captions(
                        script,
                        len(files_meta)
                    )
                    print(
                        f"[AI CAPTIONS] generated count={len(captions or [])}",
                        flush=True
                    )
                except Exception as e:
                    print(f"[AI CAPTIONS] ERROR: {e}", flush=True)
                    captions = []
            else:
                captions = []
                print(
                    "[AI CAPTIONS] disabled by user instructions",
                    flush=True
                )

        silent_path = os.path.join(OUTPUT_DIR, f"{job_id}_silent.mp4")

        # ============================================================
        # VOICE-OVER
        # ============================================================
        voiceover_path = None

        if mode == "prompt" and use_voiceover:
            try:
                if not voiceover_text:
                    raise RuntimeError("VOICEOVER TEXT NOT PROVIDED")

                voiceover_path = os.path.join(
                    job_dir,
                    "voiceover.mp3"
                )

                generate_voiceover_audio(
                    voiceover_text,
                    voiceover_path
                )

                print(
                    f"[AI VOICEOVER] READY path={voiceover_path}",
                    flush=True
                )

            except Exception as e:
                print(
                    f"[AI VOICEOVER] ERROR: {e}",
                    flush=True
                )
                raise

        if target_duration_override:
            target_duration = float(target_duration_override)
            print(
                f"[JOB {job_id}] target_duration from PROMPT PLAN={target_duration}",
                flush=True
            )
        else:
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
                target_duration=target_duration,
                instructions=instructions,
            )

        print(f"[JOB {job_id}] RENDER COMPLETE silent={silent_path}", flush=True)
        final_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")

        # ============================================================
        # AUDIO MIX
        # video + background music + voice-over
        # ============================================================
        if voiceover_path and music_path:
            print(
                f"[AUDIO MIX] video={silent_path} "
                f"music={music_path} "
                f"voiceover={voiceover_path}",
                flush=True
            )

            cmd = [
                "ffmpeg", "-y",
                "-i", silent_path,
                "-stream_loop", "-1", "-i", music_path,
                "-i", voiceover_path,

                "-filter_complex",
                "[1:a]volume=0.22[music];"
                "[2:a]volume=1.0[voice];"
                "[music][voice]amix=inputs=2:duration=first:dropout_transition=2[aout]",

                "-map", "0:v:0",
                "-map", "[aout]",

                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                final_path,
            ]

            print(
                f"[AUDIO MIX] FFMPEG START: {cmd}",
                flush=True
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                print(
                    f"[AUDIO MIX] FFMPEG ERROR: {result.stderr[-3000:]}",
                    flush=True
                )
                raise RuntimeError(
                    f"[AUDIO MIX exit code {result.returncode}] "
                    + result.stderr[-1000:]
                )

            if not os.path.exists(final_path):
                raise RuntimeError(
                    "AUDIO MIX OUTPUT NOT CREATED"
                )

            print(
                f"[AUDIO MIX] COMPLETE output={final_path} "
                f"size={os.path.getsize(final_path)}",
                flush=True
            )

        elif voiceover_path:
            print(
                f"[AUDIO MIX] voiceover only={voiceover_path}",
                flush=True
            )

            cmd = [
                "ffmpeg", "-y",
                "-i", silent_path,
                "-i", voiceover_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                final_path,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"[VOICEOVER MIX exit code {result.returncode}] "
                    + result.stderr[-1000:]
                )

        elif music_path:
            mux_music(
                silent_path,
                music_path,
                final_path
            )

        else:
            source_audio = files_meta[0] if files_meta else None
            if source_audio:
                mux_original_audio(
                    silent_path,
                    source_audio,
                    final_path
                )
            else:
                shutil.copy(
                    silent_path,
                    final_path
                )

        if website_url:
            watermarked_path = os.path.join(job_dir, "watermarked.mp4")
            add_website_watermark(final_path, watermarked_path, website_url)
            shutil.move(watermarked_path, final_path)
            print(f"[WEBSITE WATERMARK] added={website_url}", flush=True)
        print(f"[JOB {job_id}] DONE final={final_path}", flush=True)
        set_job(
            job_id,
            status="done",
            script=script,
            topic=topic, website_url=website_url,
            video_url=f"{BACKEND_URL}/outputs/{job_id}.mp4"
        )
    except Exception as e:
        import traceback
        print(f"VIDEO JOB ERROR [{job_id}]: {e}", flush=True)
        traceback.print_exc()
        set_job(job_id, status="error", error=str(e))

@app.route("/")
def index():
    script = request.args.get("script", "")
    return render_template_string(
        INDEX_HTML,
        upload_progress_html=UPLOAD_PROGRESS_HTML,
        generated_script=script
    )

@app.route("/prepare_reel", methods=["POST"])
def prepare_reel():
    script = request.form.get("script", "")
    return render_template_string(
        INDEX_HTML,
        upload_progress_html=UPLOAD_PROGRESS_HTML,
        generated_script=script
    )

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



PROMPT_PREVIEW_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ReelForge AI — Подготовка промта</title>
    <style>
        * { box-sizing: border-box; }

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

        .card {
            background: #111118;
            border: 1px solid #272733;
            border-radius: 20px;
            padding: 22px;
        }

        h1 {
            margin: 0 0 10px;
            font-size: 28px;
        }

        .desc {
            color: #9ca3af;
            line-height: 1.5;
            margin-bottom: 22px;
        }

        textarea {
            width: 100%;
            min-height: 260px;
            padding: 15px;
            border-radius: 14px;
            border: 1px solid #30303b;
            background: #08080e;
            color: #fff;
            font-size: 16px;
            line-height: 1.55;
            resize: vertical;
            outline: none;
        }

        textarea:focus {
            border-color: #a855f7;
        }

        .button {
            display: block;
            width: 100%;
            margin-top: 14px;
            padding: 16px 20px;
            border: 0;
            border-radius: 13px;
            background: #a855f7;
            color: #fff;
            font-size: 17px;
            font-weight: 700;
            cursor: pointer;
        }

        .back {
            background: #1b1b24;
            border: 1px solid #30303b;
        }

        .editor-options {
            margin-top: 12px;
            padding: 10px 12px;
            border-radius: 12px;
            background: #0c0c13;
            border: 1px solid #272733;
        }

        .option-title {
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 7px;
        }

        .option-row {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            margin: 3px 18px 3px 0;
            cursor: pointer;
            vertical-align: middle;
        }

        .option-row span {
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }

        .option-row b {
            display: inline;
            font-size: 13px;
        }

        .option-row small {
            display: none;
        }

        .option-row input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: #a855f7;
            flex-shrink: 0;
        }

        .subtitle-position {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-top: 5px;
            padding-top: 7px;
            border-top: 1px solid #20202a;
            color: #d1d1d8;
            font-size: 13px;
        }

        .position-title {
            margin-right: 3px;
            font-weight: 600;
        }

        .subtitle-position label {
            margin-right: 8px;
            cursor: pointer;
        }

        .subtitle-position input {
            accent-color: #a855f7;
        }

        .original {

            margin-top: 20px;
            padding: 14px;
            border-radius: 12px;
            background: #0c0c13;
            color: #9ca3af;
            font-size: 14px;
            line-height: 1.5;
        }
    </style>
</head>
<body>
<main class="container">

    <div class="logo">ReelForge <span>AI</span></div>

    <div class="card">
        <h1>✏️ Проверь промт перед созданием</h1>

        <div class="desc">
            AI подготовил промт для будущего Reels.
            Ты можешь изменить его перед генерацией.
            Видео, музыка и визуалы пока не создаются.
        </div>

        <form action="/create_reel_from_prompt" method="post">
              {% if website_url %}<div style="margin-bottom:12px;color:#a855f7;font-size:14px;">🌐 Сайт: {{ website_url }}</div>{% endif %}
              <input type="hidden" name="website_url" value="{{ website_url }}">
            <textarea name="topic" required>{{ prepared_prompt }}</textarea>

            <div class="editor-options">
                <div class="option-title">⚙️ Настройки видео</div>

                <label class="option-row">
                    <span>
                        <b>Титры на экране</b>
                        <small>Добавить текст поверх видео</small>
                    </span>
                    <input type="checkbox" name="editor_captions" value="1">
                </label>

                <label class="option-row">
                    <span>
                        <b>Голос за кадром</b>
                        <small>AI создаст озвучку по сценам</small>
                    </span>
                    <input type="checkbox" name="editor_voiceover" value="1">
                </label>

                <div class="subtitle-position">
                    <div class="position-title">Положение титров</div>

                    <label>
                        <input type="radio" name="editor_subtitle_position" value="bottom" checked>
                        Снизу
                    </label>

                    <label>
                        <input type="radio" name="editor_subtitle_position" value="top">
                        Сверху
                    </label>
                </div>
            </div>


            <button class="button" type="submit">
                🎬 Создать Reels
            </button>
        </form>

        <form action="/" method="get">
            <button class="button back" type="submit">
                ← Изменить исходную идею
            </button>
        </form>

        <div class="original">
            <b>Исходная идея:</b><br>
            {{ original_topic }}
        </div>
    </div>

</main>
</body>
</html>
"""


@app.route("/prepare_prompt", methods=["POST"])
def prepare_prompt():
    topic = request.form.get("topic", "").strip()
    website_url = request.form.get("website_url", "").strip()
    if not topic and not website_url:
        return "Введи идею для создания Reels!", 400

    try:
        website_text = fetch_website_text(website_url) if website_url else ""
        client, ai_model = get_ai_client()

        response = client.chat.completions.create(
            model=ai_model,
            messages=[
                {
                    "role": "system",
                    "content": """Ты — профессиональный режиссёр коротких вертикальных видео ReelForge AI.

Пользователь дал сырую идею для Reels.
Если предоставлены данные сайта, используй их как основной источник информации. Определи ключевые страницы и содержание сайта, выбери наиболее полезные факты о продукте или компании и на их основе создай сценарий Reels. Не выдумывай сведения, которых нет в данных сайта. Если данных недостаточно, используй только то, что удалось получить.

Твоя задача — превратить её в один понятный, подробный и редактируемый промт для генерации видео.

Правила:
1. Сохраняй главную идею пользователя.
2. Если пользователь указал длительность — сохрани её.
3. Если длительность не указана — предложи около 40 секунд.
4. Опиши визуальный стиль, атмосферу, последовательность сцен и динамику монтажа.
5. Укажи, какие реальные визуалы нужно искать.
6. Не придумывай несуществующие факты.
7. Если пользователь явно попросил голос за кадром, voice-over, озвучку или narration — обязательно сохрани это требование в готовом промте.
8. Если пользователь явно попросил титры, субтитры или текст на экране — обязательно сохрани это требование в готовом промте.
9. Если пользователь не просил голос за кадром — не добавляй его самостоятельно.
10. Если пользователь не просил титры или субтитры — не добавляй их самостоятельно.
11. Фоновую музыку можно использовать автоматически, если пользователь не указал другое.
12. Никогда не удаляй и не ослабляй явные требования пользователя.
13. Верни только готовый промт, без пояснений и без кавычек.

Промт должен быть написан естественным человеческим языком и готов для дальнейшего редактирования пользователем."""
                },
                {
                    "role": "user",
                    "content": (topic + "\n\nДанные с сайта:\n" + website_text).strip()
                }
            ],
            max_completion_tokens=1000
        )

        prepared_prompt = (
            response.choices[0].message.content or ""
        ).strip()

        if not prepared_prompt:
            raise RuntimeError("AI не создал подготовленный промт")

        print(
            f"[PROMPT PREVIEW] generated length={len(prepared_prompt)}",
            flush=True
        )

        return render_template_string(
            PROMPT_PREVIEW_HTML,
            prepared_prompt=prepared_prompt,
            original_topic=topic, website_url=website_url
        )

    except Exception as e:
        import traceback
        print(f"[PROMPT PREVIEW] ERROR: {e}", flush=True)
        traceback.print_exc()
        return f"Ошибка подготовки промта: {e}", 500


@app.route("/create_reel_from_prompt", methods=["POST"])
def create_reel_from_prompt():
    topic = request.form.get("topic", "").strip()
    website_url = request.form.get("website_url", "").strip()
    website_text = fetch_website_text(website_url) if website_url else ""

    # Настройки редактора Prompt Mode.
    # Чекбоксы явно определяют, нужны ли титры и voice-over.
    editor_use_captions = request.form.get("editor_captions") == "1"
    editor_use_voiceover = request.form.get("editor_voiceover") == "1"
    editor_subtitle_position = request.form.get(
        "editor_subtitle_position",
        "bottom"
    )

    if editor_subtitle_position not in ("top", "bottom"):
        editor_subtitle_position = "bottom"

    if not topic:
        return "Введи промт для создания Reels!", 400

    # Используем ту же систему доступа/бесплатных генераций,
    # что и существующие режимы.
    user_key = request.cookies.get("rf_user_key")

    if not user_key:
        return "Требуется email", 401

    if not consume_free_entry(user_key):
        return redirect("/payment", code=302)

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    set_job(
        job_id,
        status="processing",
        mode="prompt",
        topic=topic, website_url=website_url,
        editor_use_captions=editor_use_captions,
        editor_use_voiceover=editor_use_voiceover,
        editor_subtitle_position=editor_subtitle_position,
    )

    def run_prompt_job():
        try:
            print(
                f"[PROMPT JOB {job_id}] START topic={topic}",
                flush=True
            )

            # 1. AI создаёт план сцен.
            scene_plan = generate_prompt_scene_plan(topic, website_text)

            # Настройки редактора имеют приоритет над тем,
            # что AI предположил в плане.
            scene_plan["use_captions"] = editor_use_captions
            scene_plan["use_voiceover"] = editor_use_voiceover

            if editor_use_captions:
                topic_for_render = (
                    topic
                    + "\n\n"
                    + f"Титры на экране: включены. "
                    f"Положение титров: {editor_subtitle_position}."
                )
            else:
                topic_for_render = (
                    topic
                    + "\n\n"
                    + "Титры на экране: выключены."
                )

            if editor_use_voiceover:
                topic_for_render += (
                    "\nVoice-over: включён."
                )
            else:
                topic_for_render += (
                    "\nVoice-over: выключен."
                )

            print(
                f"[EDITOR FLAGS] "
                f"captions={editor_use_captions} "
                f"voiceover={editor_use_voiceover} "
                f"subtitle_position={editor_subtitle_position}",
                flush=True
            )

            set_job(
                job_id,
                prompt_plan=scene_plan,
            )

            print(
                f"[PROMPT JOB {job_id}] PLAN "
                f"scenes={len(scene_plan.get('scenes', []))} "
                f"duration={scene_plan.get('duration')}",
                flush=True
            )

            # 2. Автоматически подбираем бесплатную фоновую музыку
            # по содержанию промта.
            music_path = None
            try:
                music_path = select_prompt_music(topic)
                print(
                    f"[PROMPT MUSIC] selected={music_path}",
                    flush=True
                )
            except Exception as e:
                print(
                    f"[PROMPT MUSIC] ERROR: {e}",
                    flush=True
                )

            website_screenshot = None
            if website_url:
                website_screenshot = os.path.join(job_dir, "website_screenshot.png")
                capture_website_screenshot(website_url, website_screenshot)
                print(f"[WEBSITE SCREENSHOT] saved={website_screenshot}", flush=True)
            visual_files = prepare_prompt_images(
                job_dir,
                scene_plan
            )
            if website_screenshot:
                visual_files.insert(0, {"path": website_screenshot, "type": "image", "caption": "", "source": "Website", "url": website_url, "scene_index": -1})

            # 3. PROMPT AUTO MIX:
            # сохраняем ВСЕ сцены — видео и изображения —
            # в исходном порядке сцен.
            #
            # Видео будут монтироваться как видео.
            # Изображения будут превращаться в короткие видеоклипы.

            media_files = [item["path"] for item in visual_files]

            print(
                f"[PROMPT MEDIA] MIX total={len(media_files)} "
                f"videos={sum(1 for x in visual_files if x.get('type') == 'video')} "
                f"images={sum(1 for x in visual_files if x.get('type') == 'image')}",
                flush=True
            )

            # PROMPT MODE:
            # Передаём в рендер реальные требования пользователя.
            #
            # Если AI определил титры — используем текст caption
            # из каждой сцены как текст на экране.
            prompt_captions = None
            if scene_plan.get("use_captions"):
                prompt_captions = [
                    str(scene.get("caption", "")).strip()
                    for scene in scene_plan.get("scenes", [])
                ]

            prompt_use_captions = bool(scene_plan.get("use_captions", False))
            prompt_use_voiceover = bool(scene_plan.get("use_voiceover", False))

            prompt_voiceover_text = None

            if prompt_use_voiceover:
                prompt_voiceover_text = generate_voiceover_text(scene_plan)
                print(
                    f"[PROMPT VOICEOVER TEXT] length={len(prompt_voiceover_text or '')}",
                    flush=True
                )

            print(
                f"[PROMPT RENDER FLAGS] "
                f"use_captions={prompt_use_captions} "
                f"use_voiceover={prompt_use_voiceover} "
                f"captions={len(prompt_captions or [])}",
                flush=True
            )

            process_video_job(
                job_id,
                job_dir,
                media_files,
                music_path,
                topic_for_render,
                "prompt",
                preset_captions=prompt_captions,
                target_duration_override=scene_plan.get("duration"),
                use_captions=prompt_use_captions,
                use_voiceover=prompt_use_voiceover,
                voiceover_text=prompt_voiceover_text,
                website_url=website_url,
            )


        except Exception as e:
            import traceback

            print(
                f"[PROMPT JOB {job_id}] ERROR: {e}",
                flush=True
            )
            traceback.print_exc()

            set_job(
                job_id,
                status="error",
                error=str(e),
            )

    t = threading.Thread(
        target=run_prompt_job,
        daemon=True
    )
    t.start()

    return render_template_string(
        PROCESSING_HTML.replace(
            "</body>",
            f'<meta http-equiv="refresh" content="4;url={BACKEND_URL}/status/{job_id}?access=rf2026free"></body>'
        )
    )


@app.route("/create_video", methods=["POST"])
def create_video():
    topic = request.form.get("topic", "")
    files = request.files.getlist("images")
    files = [f for f in files if f and f.filename]
    music_file = request.files.get("music")

    if not files:
        return "Загрузи хотя бы одно изображение!", 400

    # Бесплатный вход списывается только после успешной проверки файлов.
    user_key = request.cookies.get("rf_user_key")
    if not user_key:
        return "Требуется email", 401

    if not consume_free_entry(user_key):
        return redirect("/payment", code=302)

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
        topic=topic, website_url=website_url,
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

    try:
        index = int(request.form.get("index", "0"))
        chunk_index = int(request.form.get("chunk_index", "0"))
        total_chunks = int(request.form.get("total_chunks", "1"))
    except Exception:
        return jsonify({"error": "Некорректные параметры загрузки видео"}), 400

    if (
        not job_id
        or not video
        or not video.filename
        or index < 0
        or chunk_index < 0
        or total_chunks <= 0
        or chunk_index >= total_chunks
    ):
        return jsonify({"error": "Некорректная часть видео"}), 400

    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ext = os.path.splitext(video.filename)[1] or ".mp4"

    # Временная папка конкретного видео.
    video_chunk_dir = os.path.join(
        job_dir,
        f"video{index:03d}_chunks"
    )
    os.makedirs(video_chunk_dir, exist_ok=True)

    chunk_path = os.path.join(
        video_chunk_dir,
        f"part{chunk_index:05d}"
    )

    started = time.time()

    print(
        f"[UPLOAD-PART] START job={job_id} "
        f"video={index} chunk={chunk_index+1}/{total_chunks} "
        f"name={video.filename} "
        f"content_length={request.content_length}",
        flush=True
    )

    video.save(chunk_path)

    chunk_size = os.path.getsize(chunk_path)

    print(
        f"[UPLOAD-PART] SAVED job={job_id} "
        f"video={index} chunk={chunk_index+1}/{total_chunks} "
        f"size={chunk_size} "
        f"elapsed={time.time()-started:.2f}s",
        flush=True
    )

    # Пока не последняя часть — просто подтверждаем.
    if chunk_index != total_chunks - 1:
        return jsonify({
            "ok": True,
            "complete": False,
            "index": index,
            "chunk_index": chunk_index,
            "size": chunk_size
        })

    # Последняя часть: проверяем, что все части на месте.
    missing = []

    for i in range(total_chunks):
        expected = os.path.join(
            video_chunk_dir,
            f"part{i:05d}"
        )

        if not os.path.exists(expected):
            missing.append(i)

    if missing:
        return jsonify({
            "error": f"Не хватает частей видео: {missing}"
        }), 400

    video_path = os.path.join(
        job_dir,
        f"src{index:03d}{ext}"
    )

    # Собираем исходное видео строго по порядку.
    with open(video_path, "wb") as out:
        for i in range(total_chunks):
            part = os.path.join(
                video_chunk_dir,
                f"part{i:05d}"
            )

            with open(part, "rb") as src:
                shutil.copyfileobj(src, out)

    # Удаляем временные части.
    for i in range(total_chunks):
        part = os.path.join(
            video_chunk_dir,
            f"part{i:05d}"
        )

        try:
            os.remove(part)
        except OSError:
            pass

    try:
        os.rmdir(video_chunk_dir)
    except OSError:
        pass

    size = os.path.getsize(video_path)

    # Серверная проверка полного файла.
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
        f"[UPLOAD-PART] COMPLETE job={job_id} "
        f"video={index} chunks={total_chunks} "
        f"size={size}",
        flush=True
    )

    return jsonify({
        "ok": True,
        "complete": True,
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

    # Бесплатный вход списывается только после полной проверки
    # загруженных видео и непосредственно перед запуском рендера.
    user_key = request.cookies.get("rf_user_key")
    if not user_key:
        return jsonify({"error": "Требуется email"}), 401

    if not consume_free_entry(user_key):
        return jsonify({
            "error": "Бесплатные входы закончились",
            "redirect": "/payment"
        }), 402

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
        return render_template_string(
            RESULT_HTML,
            script=job.get("script"),
            topic=job.get("topic", ""),
            video_url=job.get("video_url")
        )
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


@app.route("/admin")
def admin_dashboard():
    """Главная админ-панель ReelForge AI."""
    if not admin_session_ok():
        return "Доступ запрещён", 403

    html = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>ReelForge AI — Admin</title>

<style>
*{box-sizing:border-box}

body{
    margin:0;
    background:#07070d;
    color:#fff;
    font-family:Arial,sans-serif;
}

.container{
    max-width:850px;
    margin:auto;
    padding:28px 16px 60px;
}

h1{
    margin:0 0 8px;
    font-size:30px;
}

.subtitle{
    color:#999;
    margin-bottom:28px;
}

.admin-grid{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:16px;
}

.admin-card{
    display:block;
    text-decoration:none;
    color:#fff;
    background:#11111a;
    border:1px solid #292936;
    border-radius:18px;
    padding:25px 22px;
    min-height:150px;
    transition:.15s;
}

.admin-card:hover{
    border-color:#7c3aed;
    background:#161321;
    transform:translateY(-2px);
}

.admin-icon{
    font-size:34px;
    margin-bottom:14px;
}

.admin-title{
    font-size:20px;
    font-weight:800;
    margin-bottom:7px;
}

.admin-desc{
    color:#999;
    font-size:14px;
    line-height:1.45;
}

@media(max-width:650px){
    .admin-grid{
        grid-template-columns:1fr;
    }

    .admin-card{
        min-height:125px;
    }
}
</style>
</head>

<body>

<div class="container">

<h1>⚙️ ReelForge AI</h1>

<div class="subtitle">
Центр управления проектом
</div>

<div class="admin-grid">

<a class="admin-card"
   href="/admin/users">
    <div class="admin-icon">👥</div>
    <div class="admin-title">Пользователи</div>
    <div class="admin-desc">
        Пользователи, балансы, бесплатные генерации и доступ.
    </div>
</a>

<a class="admin-card"
   href="/admin/settings">
    <div class="admin-icon">🤖</div>
    <div class="admin-title">AI и модели</div>
    <div class="admin-desc">
        OpenAI, Groq и выбор используемой AI-модели.
    </div>
</a>

<a class="admin-card"
   href="/admin/plans">
    <div class="admin-icon">💳</div>
    <div class="admin-title">Тарифы</div>
    <div class="admin-desc">
        Free, Basic, Pro и Unlimited.
    </div>
</a>

<a class="admin-card"
   href="/admin/security">
    <div class="admin-icon">🔐</div>
    <div class="admin-title">Безопасность</div>
    <div class="admin-desc">
        Ключи, доступ администратора и настройки безопасности.
    </div>
</a>

<a class="admin-card"
   href="/admin/marketing">
    <div class="admin-icon">📣</div>
    <div class="admin-title">Маркетинговые площадки</div>
    <div class="admin-desc">
        PromptFrenzy и другие каталоги, API, публикации и статусы размещения.
    </div>
</a>

</div>

</div>

<script>
(function () {
    const providerInputs = document.querySelectorAll('input[name="ai_provider"]');
    const modelSelect = document.querySelector('select[name="ai_model"]');

    if (!providerInputs.length || !modelSelect) return;

    const allOptions = Array.from(modelSelect.options);

    function updateModels() {
        const provider = document.querySelector(
            'input[name="ai_provider"]:checked'
        )?.value;

        if (!provider) return;

        const current = modelSelect.value;

        allOptions.forEach(option => {
            const isOpenAI =
                option.value === "gpt-5.4-mini" ||
                option.value === "gpt-5.6-luna";

            const isGroq =
                option.value === "openai/gpt-oss-120b";

            const allowed =
                (provider === "openai" && isOpenAI) ||
                (provider === "groq" && isGroq);

            option.hidden = !allowed;
            option.disabled = !allowed;
        });

        if (
            provider === "openai" &&
            (current === "gpt-5.4-mini" || current === "gpt-5.6-luna")
        ) {
            modelSelect.value = current;
        } else if (provider === "groq") {
            modelSelect.value = "openai/gpt-oss-120b";
        } else {
            modelSelect.value = "gpt-5.4-mini";
        }

        const selected =
            modelSelect.options[modelSelect.selectedIndex];

        if (!selected || selected.disabled) {
            modelSelect.value =
                provider === "groq"
                    ? "openai/gpt-oss-120b"
                    : "gpt-5.4-mini";
        }
    }

    providerInputs.forEach(input => {
        input.addEventListener("change", updateModels);
    });

    updateModels();
})();
</script>

</body>
</html>
"""

    return render_template_string(
        html,
    )



@app.route("/admin/marketing")
def admin_marketing():
    """Управление маркетинговыми площадками."""

    if not admin_session_ok():
        return "Доступ запрещён", 403

    platforms = []

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        name,
                        slug,
                        website_url,
                        api_url,
                        api_method,
                        automation_allowed,
                        auth_type,
                        badge_required,
                        submission_status,
                        review_status,
                        publication_url,
                        last_error,
                        last_checked_at,
                        last_submitted_at
                    FROM marketing_platforms
                    ORDER BY id
                """)
                platforms = cur.fetchall()
    except Exception as e:
        print(f"[ADMIN] MARKETING ERROR: {e}", flush=True)
        return f"Ошибка загрузки площадок: {e}", 500

    cards = ""

    for row in platforms:
        (
            pid,
            name,
            slug,
            website_url,
            api_url,
            api_method,
            automation_allowed,
            auth_type,
            badge_required,
            submission_status,
            review_status,
            publication_url,
            last_error,
            last_checked_at,
            last_submitted_at,
        ) = row

        automation = "ВКЛ" if automation_allowed else "ВЫКЛ"
        badge = "Да" if badge_required else "Нет"

        error_html = ""
        if last_error:
            error_html = f'<div class="error"><b>Ошибка:</b> {last_error}</div>'

        publication_html = ""
        if publication_url:
            publication_html = (
                f'<a href="{publication_url}" target="_blank" rel="noopener">'
                f'Открыть публикацию</a>'
            )

        cards += f"""
        <section class="platform">
          <div class="platform-head">
            <div>
              <h2>{name}</h2>
              <div class="slug">{slug}</div>
            </div>
            <div class="status">
              Публикация: {submission_status} · Проверка: {review_status}
            </div>
          </div>

          <div class="grid">
            <div><b>Сайт</b><br>
              <a href="{website_url}" target="_blank" rel="noopener">
                {website_url}
              </a>
            </div>

            <div><b>API</b><br>
              {api_method or "—"} {api_url or "—"}
            </div>

            <div><b>Автоматизация</b><br>{automation}</div>
            <div><b>Badge</b><br>{badge}</div>
            <div><b>Авторизация</b><br>{auth_type or "—"}</div>
            <div><b>ID</b><br>{pid}</div>
          </div>

          {error_html}

          <div class="actions">
            <a class="button"
               href="/admin/marketing/edit/{pid}">
              ✏️ Редактировать
            </a>

            <a class="button secondary"
               href="/admin/marketing/check/{pid}">
              🔎 Проверить
            </a>

            {publication_html}
          </div>
        </section>
        """

    html = f"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReelForge AI — Маркетинговые площадки</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#07070d;color:#fff;font-family:Arial,sans-serif}}
.container{{max-width:950px;margin:auto;padding:28px 16px 60px}}
.top{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:28px}}
h1{{margin:0;font-size:30px}}
a{{color:#a78bfa}}
.platform{{background:#11111a;border:1px solid #292936;border-radius:18px;padding:22px;margin-bottom:18px}}
.platform-head{{display:flex;justify-content:space-between;gap:15px;align-items:center}}
.platform h2{{margin:0 0 4px}}
.slug{{color:#888;font-size:13px}}
.status{{background:#191326;border:1px solid #51328c;border-radius:999px;padding:8px 12px;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:20px;color:#bbb;font-size:14px;line-height:1.5}}
.grid b{{color:#fff}}
.actions{{display:flex;gap:10px;margin-top:22px;flex-wrap:wrap}}
.button{{display:inline-block;padding:10px 15px;border-radius:10px;background:#7c3aed;color:#fff;text-decoration:none;font-weight:700}}
.button.secondary{{background:#22222e}}
.error{{margin-top:18px;padding:12px;border-radius:10px;background:#32151a;color:#ffb4b4}}
.back{{color:#aaa;text-decoration:none}}
@media(max-width:650px){{
  .grid{{grid-template-columns:1fr}}
  .platform-head{{align-items:flex-start;flex-direction:column}}
}}
</style>
</head>
<body>
<div class="container">

  <div class="top">
    <div>
      <h1>📣 Маркетинговые площадки</h1>
      <div style="color:#999;margin-top:6px">
        Управление каталогами, API и публикациями
      </div>
    </div>

    </div>

  {cards or '<div class="platform">Площадки пока не добавлены.</div>'}

  <div style="margin-top:20px">
      <a class="button" href="/admin/marketing/add">
        ➕ Добавить площадку
      </a>
  </div>

  <div style="margin-top:16px">
    <a class="back" href="/admin">
      ← Админка
    </a>
  </div>

</div>
</body>
</html>
"""

    return html




@app.route("/admin/marketing/check/<int:platform_id>")
def admin_marketing_check(platform_id):
    """Проверка доступности сайта/API маркетинговой площадки."""

    if not admin_session_ok():
        return "Доступ запрещён", 403

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT name, website_url, api_url, api_method
                    FROM marketing_platforms
                    WHERE id=%s
                """, (platform_id,))
                platform = cur.fetchone()

        if not platform:
            return "Площадка не найдена", 404

        name, website_url, api_url, api_method = platform
        import urllib.request

        target = website_url
        status_code = None
        error = None

        try:
            req = urllib.request.Request(
                target,
                method="HEAD",
                headers={"User-Agent": "ReelForge-AI-Marketing-Checker/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                status_code = response.getcode()
        except Exception:
            try:
                req = urllib.request.Request(
                    target,
                    method="GET",
                    headers={"User-Agent": "ReelForge-AI-Marketing-Checker/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    status_code = response.getcode()
            except Exception as e:
                error = str(e)

        review_status = (
            "reachable"
            if status_code and 200 <= status_code < 400
            else "error"
        )

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE marketing_platforms
                    SET review_status=%s,
                        last_error=%s,
                        last_checked_at=NOW(),
                        updated_at=NOW()
                    WHERE id=%s
                """, (
                    review_status,
                    error,
                    platform_id
                ))
            conn.commit()

        status_text = (
            f"HTTP {status_code}"
            if status_code
            else f"Ошибка: {error or 'неизвестная ошибка'}"
        )

        return render_template_string("""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка площадки</title>
<style>
body{margin:0;background:#07070d;color:#fff;font-family:Arial,sans-serif}
.container{max-width:700px;margin:auto;padding:30px 16px}
.card{margin-top:20px;padding:24px;background:#11111a;border:1px solid #292936;border-radius:18px}
a{color:#a78bfa;text-decoration:none}
.status{margin-top:18px;padding:16px;border-radius:12px;background:#191326}
</style>
</head>
<body>
<div class="container">
<a href="/admin/marketing">← Маркетинговые площадки</a>
<h1>🔎 Проверка площадки</h1>
<div class="card">
<h2>{{ name }}</h2>
<p>Проверяем: <b>{{ target }}</b></p>
<div class="status">
Статус: <b>{{ status_text }}</b><br>
Результат: <b>{{ review_status }}</b>
</div>
</div>
</div>
</body>
</html>
""",
            name=name,
            target=target,
            status_text=status_text,
            review_status=review_status
        )

    except Exception as e:
        print(f"[ADMIN] MARKETING CHECK ERROR: {e}", flush=True)
        return f"Ошибка проверки: {e}", 500



def fetch_catalog_metadata(url):
    """Получает название, описание и OG image с сайта."""
    import urllib.request
    from html import unescape

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ReelForge-AI-Catalog/1.0"}
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read(300000).decode("utf-8", errors="ignore")

    def meta(name):
        pattern = (
            r'<meta[^>]+(?:name|property)=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\']([^"\']*)["\']'
        )
        match = re.search(pattern, html, re.I)
        return unescape(match.group(1)).strip() if match else None

    title = re.search(
        r'<title[^>]*>(.*?)</title>',
        html,
        re.I | re.S
    )

    name = None
    if title:
        name = re.sub(r'\s+', ' ', title.group(1)).strip()

    return {
        "name": name,
        "slug": re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-"),
        "description": meta("description"),
        "logo_url": meta("og:image"),
    }



@app.route("/admin/marketing/catalog-preview")
def admin_marketing_catalog_preview():
    """Возвращает метаданные сайта для автозаполнения формы."""
    if not admin_session_ok():
        return jsonify({"error": "Доступ запрещён"}), 403

    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL не указан"}), 400

    try:
        data = fetch_catalog_metadata(url)
        return jsonify(data)
    except Exception as e:
        print(f"[ADMIN] CATALOG PREVIEW ERROR: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/admin/marketing/add", methods=["GET", "POST"])
def admin_marketing_add():
    """Добавление новой маркетинговой площадки."""

    if not admin_session_ok():
        return "Доступ запрещён", 403

    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip()
            website_url = request.form.get("website_url", "").strip()
            api_url = request.form.get("api_url", "").strip() or None
            api_method = request.form.get("api_method", "POST").strip().upper()
            auth_type = request.form.get("auth_type", "").strip() or None
            automation_allowed = request.form.get("automation_allowed") == "1"
            badge_required = request.form.get("badge_required") == "1"

            description = request.form.get("description", "").strip() or None
            category = request.form.get("category", "").strip() or None
            tags = request.form.get("tags", "").strip() or None
            pricing = request.form.get("pricing", "").strip() or None
            logo_url = request.form.get("logo_url", "").strip() or None
            badge_url = request.form.get("badge_url", "").strip() or None

            # Автозаполнение каталожных данных с сайта.
            if website_url:
                try:
                    catalog = fetch_catalog_metadata(website_url)

                    if not description:
                        description = catalog.get("description")

                    if not logo_url:
                        logo_url = catalog.get("logo_url")

                except Exception as catalog_error:
                    print(
                        f"[ADMIN] CATALOG AUTOFILL ERROR: {catalog_error}",
                        flush=True
                    )

            if not name or not slug or not website_url:
                return "Ошибка: Name, Slug и Website URL обязательны.", 400

            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO marketing_platforms
                        (name, slug, website_url, api_url, api_method,
                         automation_allowed, auth_type, badge_required,
                         description, category, tags, pricing, logo_url, badge_url)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        name, slug, website_url, api_url, api_method,
                        automation_allowed, auth_type, badge_required,
                        description, category, tags, pricing, logo_url, badge_url
                    ))
                conn.commit()

            return redirect(
                "/admin/marketing"
            )

        except Exception as e:
            print(f"[ADMIN] MARKETING ADD ERROR: {e}", flush=True)
            return f"Ошибка добавления: {e}", 500

    return render_template_string("""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReelForge AI — Добавить площадку</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#07070d;color:#fff;font-family:Arial,sans-serif}
.container{max-width:700px;margin:auto;padding:28px 16px 60px}
a{color:#a78bfa;text-decoration:none}
.card{margin-top:20px;padding:24px;background:#11111a;border:1px solid #292936;border-radius:18px}
label{display:block;margin-top:16px;margin-bottom:7px;font-weight:700}
input,select{width:100%;padding:12px;border-radius:10px;border:1px solid #393946;background:#090910;color:#fff}
.check{display:flex;gap:10px;align-items:center}
.check input{width:auto}
button{margin-top:22px;padding:12px 18px;border:0;border-radius:10px;background:#7c3aed;color:#fff;font-weight:700}
</style>
</head>
<body>
<div class="container">
<a href="/admin/marketing">← Маркетинговые площадки</a>
<h1>➕ Добавить площадку</h1>

<div class="card">
<form method="post">
<label>Название</label>
<input name="name" required placeholder="Например: Product Hunt">

<label>Slug</label>
<input name="slug" required placeholder="product-hunt">

<label>Website URL</label>
<div style="display:flex;gap:10px;align-items:center">
<input id="website_url" name="website_url" type="url" required placeholder="https://example.com">
<button type="button" onclick="loadCatalogData(this)">🔎 Подтянуть данные</button>
</div>

<label>API URL</label>
<input name="api_url" type="url" placeholder="https://example.com/api/...">

<label>Описание для каталога</label>
<textarea name="description" rows="4" placeholder="Короткое описание ReelForge AI для каталога"></textarea>

<label>Категория</label>
<input name="category" placeholder="video-generation">

<label>Теги</label>
<input name="tags" placeholder="video, reels, ai-video">

<label>Pricing</label>
<input name="pricing" placeholder="freemium">

<label>Logo URL</label>
<input name="logo_url" type="url" placeholder="https://example.com/logo.png">

<label>Badge URL</label>
<input name="badge_url" type="url" placeholder="https://example.com/badge.svg">

<label>API Method</label>
<select name="api_method">
<option>POST</option>
<option>GET</option>
<option>PUT</option>
<option>PATCH</option>
</select>

<label>Auth type</label>
<input name="auth_type" placeholder="none / api_key / oauth">

<label class="check">
<input type="checkbox" name="automation_allowed" value="1">
Разрешить автоматизацию
</label>

<label class="check">
<input type="checkbox" name="badge_required" value="1">
Требуется badge
</label>

<button type="submit">Сохранить площадку</button>
</form>
<script>
async function loadCatalogData(button) {
    const input = document.getElementById("website_url");
    const url = input ? input.value.trim() : "";

    if (!url) {
        alert("Сначала укажи Website URL");
        return;
    }

    button.disabled = true;
    button.textContent = "⏳ Загружаем...";

    try {
        const response = await fetch(
            "/admin/marketing/catalog-preview?url=" + encodeURIComponent(url)
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Не удалось получить данные");
        }

        const description = document.querySelector(
            'textarea[name="description"]'
        );
        const logo = document.querySelector(
            'input[name="logo_url"]'
        );

        if (description && data.description) {
            description.value = data.description;
        }

        if (logo && data.logo_url) {
            logo.value = data.logo_url;
        }

        if (data.name) document.querySelector('input[name="name"]').value = data.name;
        if (data.slug) document.querySelector('input[name="slug"]').value = data.slug;
        alert("Данные каталога загружены");
    } catch (error) {
        alert("Ошибка: " + error.message);
    } finally {
        button.disabled = false;
        button.textContent = "🔎 Подтянуть данные";
    }
}
</script>

</div>
</div>
</body>
</html>
""")


@app.route("/admin/marketing/edit/<int:platform_id>", methods=["GET", "POST"])
def admin_marketing_edit(platform_id):
    """Редактирование маркетинговой площадки."""

    if not admin_session_ok():
        return "Доступ запрещён", 403

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                if request.method == "POST":
                    cur.execute("""
                        UPDATE marketing_platforms
                        SET name=%s,
                            slug=%s,
                            website_url=%s,
                            api_url=%s,
                            api_method=%s,
                            automation_allowed=%s,
                            auth_type=%s,
                            badge_required=%s,
                            submission_status=%s,
                            review_status=%s,
                            publication_url=%s,
                            last_error=%s,
                            description=%s,
                            category=%s,
                            tags=%s,
                            pricing=%s,
                            logo_url=%s,
                            badge_url=%s,
                            updated_at=NOW()
                        WHERE id=%s
                    """, (
                        request.form.get("name", "").strip(),
                        request.form.get("slug", "").strip(),
                        request.form.get("website_url", "").strip(),
                        request.form.get("api_url", "").strip() or None,
                        request.form.get("api_method", "POST").strip().upper(),
                        request.form.get("automation_allowed") == "1",
                        request.form.get("auth_type", "").strip() or None,
                        request.form.get("badge_required") == "1",
                        request.form.get("submission_status", "not_submitted").strip(),
                        request.form.get("review_status", "unknown").strip(),
                        request.form.get("publication_url", "").strip() or None,
                        request.form.get("last_error", "").strip() or None,
                        request.form.get("description", "").strip() or None,
                        request.form.get("category", "").strip() or None,
                        request.form.get("tags", "").strip() or None,
                        request.form.get("pricing", "").strip() or None,
                        request.form.get("logo_url", "").strip() or None,
                        request.form.get("badge_url", "").strip() or None,
                        platform_id
                    ))
                    conn.commit()

                    return redirect(
                        "/admin/marketing"
                    )

                cur.execute("""
                    SELECT id,name,slug,website_url,api_url,api_method,
                           automation_allowed,auth_type,badge_required,
                           submission_status,review_status,publication_url,
                           last_error,description,category,tags,pricing,
                           logo_url,badge_url
                    FROM marketing_platforms
                    WHERE id=%s
                """, (platform_id,))
                platform = cur.fetchone()

        if not platform:
            return "Площадка не найдена", 404

        (
            pid,name,slug,website_url,api_url,api_method,
            automation_allowed,auth_type,badge_required,
            submission_status,review_status,publication_url,last_error,
            description,category,tags,pricing,logo_url,badge_url
        ) = platform

        return render_template_string("""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReelForge AI — Редактирование</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#07070d;color:#fff;font-family:Arial,sans-serif}
.container{max-width:760px;margin:auto;padding:28px 16px 60px}
a{color:#a78bfa;text-decoration:none}
.card{margin-top:20px;padding:24px;background:#11111a;border:1px solid #292936;border-radius:18px}
label{display:block;margin-top:15px;margin-bottom:7px;font-weight:700}
input,select{width:100%;padding:12px;border-radius:10px;border:1px solid #393946;background:#090910;color:#fff}
.check{display:flex;gap:10px;align-items:center}
.check input{width:auto}
button{margin-top:22px;padding:12px 18px;border:0;border-radius:10px;background:#7c3aed;color:#fff;font-weight:700}
</style>
</head>
<body>
<div class="container">
<a href="/admin/marketing">← Маркетинговые площадки</a>
<h1>✏️ {{ name }}</h1>

<div class="card">
<form method="post">
<label>Название</label>
<input name="name" value="{{ name }}" required>

<label>Slug</label>
<input name="slug" value="{{ slug }}" required>

<label>Website URL</label>
<input name="website_url" value="{{ website_url }}" required>

<label>API URL</label>
<input name="api_url" value="{{ api_url or '' }}">

<label>API Method</label>
<select name="api_method">
{% for method in ["POST","GET","PUT","PATCH"] %}
<option value="{{ method }}" {% if api_method == method %}selected{% endif %}>{{ method }}</option>
{% endfor %}
</select>

<label>Auth type</label>
<input name="auth_type" value="{{ auth_type or '' }}">

<label>Статус отправки</label>
<input name="submission_status" value="{{ submission_status }}">

<label>Статус проверки</label>
<input name="review_status" value="{{ review_status }}">

<label>Publication URL</label>
<input name="publication_url" value="{{ publication_url or '' }}">

<label>Описание для каталога</label>
<textarea name="description" rows="4">{{ description or '' }}</textarea>

<label>Категория</label>
<input name="category" value="{{ category or '' }}">

<label>Теги</label>
<input name="tags" value="{{ tags or '' }}">

<label>Pricing</label>
<input name="pricing" value="{{ pricing or '' }}">

<label>Logo URL</label>
<input name="logo_url" value="{{ logo_url or '' }}">

<label>Badge URL</label>
<input name="badge_url" value="{{ badge_url or '' }}">

<label>Последняя ошибка</label>
<input name="last_error" value="{{ last_error or '' }}">

<label class="check">
<input type="checkbox" name="automation_allowed" value="1" {% if automation_allowed %}checked{% endif %}>
Разрешить автоматизацию
</label>

<label class="check">
<input type="checkbox" name="badge_required" value="1" {% if badge_required %}checked{% endif %}>
Требуется badge
</label>

<button type="submit">💾 Сохранить изменения</button>
</form>
</div>
</div>
</body>
</html>
""",
            pid=pid,
            name=name,
            slug=slug,
            website_url=website_url,
            api_url=api_url,
            api_method=api_method,
            automation_allowed=automation_allowed,
            auth_type=auth_type,
            badge_required=badge_required,
            submission_status=submission_status,
            review_status=review_status,
            publication_url=publication_url,
            last_error=last_error,
            description=description,
            category=category,
            tags=tags,
            pricing=pricing,
            logo_url=logo_url,
            badge_url=badge_url
        )

    except Exception as e:
        print(f"[ADMIN] MARKETING EDIT ERROR: {e}", flush=True)
        return f"Ошибка редактирования: {e}", 500


@app.route("/admin/marketing/delete/<int:platform_id>", methods=["POST"])
def admin_marketing_delete(platform_id):
    """Удаление маркетинговой площадки."""

    if not admin_session_ok():
        return "Доступ запрещён", 403

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM marketing_platforms WHERE id=%s",
                    (platform_id,)
                )
            conn.commit()

        return redirect(
            "/admin/marketing"
        )

    except Exception as e:
        print(f"[ADMIN] MARKETING DELETE ERROR: {e}", flush=True)
        return f"Ошибка удаления: {e}", 500


@app.route("/admin/plans")
def admin_plans():
    """Раздел тарифов админ-панели."""
    if not admin_session_ok():
        return "Доступ запрещён", 403

    return render_template_string("""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReelForge AI — Тарифы</title>
<style>
body{
    margin:0;
    background:#07070d;
    color:#fff;
    font-family:Arial,sans-serif;
}
.container{
    max-width:850px;
    margin:auto;
    padding:30px 16px 60px;
}
a{
    color:#a78bfa;
    text-decoration:none;
}
.card{
    margin-top:20px;
    padding:24px;
    background:#11111a;
    border:1px solid #292936;
    border-radius:18px;
}
h1{margin:20px 0 8px}
p{color:#999}
</style>
</head>
<body>
<div class="container">
<a href="/admin">← Админ-панель</a>
<h1>💳 Тарифы</h1>
<div class="card">
<p>Раздел тарифов подготовлен.</p>
<p>Здесь следующим этапом настроим Free / Basic / Pro / Unlimited.</p>
</div>
</div>
</body>
</html>
""")


@app.route("/admin/security")
def admin_security():
    """Раздел безопасности админ-панели."""
    if not admin_session_ok():
        return "Доступ запрещён", 403

    return render_template_string("""
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReelForge AI — Безопасность</title>
<style>
body{
    margin:0;
    background:#07070d;
    color:#fff;
    font-family:Arial,sans-serif;
}
.container{
    max-width:850px;
    margin:auto;
    padding:30px 16px 60px;
}
a{
    color:#a78bfa;
    text-decoration:none;
}
.card{
    margin-top:20px;
    padding:24px;
    background:#11111a;
    border:1px solid #292936;
    border-radius:18px;
}
h1{margin:20px 0 8px}
p{color:#999;line-height:1.5}
</style>
</head>
<body>
<div class="container">
<a href="/admin">← Админ-панель</a>
<h1>🔐 Безопасность</h1>
<div class="card">
<p>Раздел безопасности подготовлен.</p>
<p>Здесь следующим этапом проверим админ-доступ, API-ключи, токены и защиту служебных маршрутов.</p>
</div>
</div>
</body>
</html>
""")


@app.route("/admin/reset-free")
def admin_reset_free():
    """
    Сброс бесплатных входов текущего пользователя.
    Доступ только по ADMIN_RESET_TOKEN.
    """

    if not admin_session_ok():
        return "Доступ запрещён", 403

    user_key = request.cookies.get("rf_user_key")

    if not user_key:
        return "Пользователь не определён", 400

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET free_entries_used = 0
                    WHERE user_key = %s
                    RETURNING user_key, free_entries_used
                """, (user_key,))

                row = cur.fetchone()

            conn.commit()

        if not row:
            return "Пользователь не найден", 404

        print(
            f"[ADMIN] free counter reset user={user_key}",
            flush=True
        )

        return redirect("/")

    except Exception as e:
        print(f"[ADMIN] RESET ERROR: {e}", flush=True)
        return "Ошибка сброса счётчика", 500



@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    """Настройки AI-провайдера, модели и тарифа ReelForge AI."""
    if not admin_session_ok():
        return "Доступ запрещён", 403

    try:
        if request.method == "POST":
            ai_provider = (request.form.get("ai_provider") or "groq").strip().lower()
            ai_model = (request.form.get("ai_model") or "").strip()
            plan = (request.form.get("plan") or "free").strip().lower()

            allowed_models = {
                key: set(cfg["models"])
                for key, cfg in PROVIDER_CONFIG.items()
            }

            if ai_provider not in PROVIDER_CONFIG:
                return "Некорректный AI provider", 400

            # Сервер сам выбирает допустимую модель для выбранного провайдера.
            # Это защищает от старого значения select в браузере.
            if ai_model not in allowed_models[ai_provider]:
                ai_model = PROVIDER_CONFIG[ai_provider]["models"][0]

            if plan not in {"free", "basic", "pro", "unlimited"}:
                return "Некорректный тариф", 400

            save_app_settings(ai_provider, ai_model, plan)

            return redirect("/admin/settings?saved=1")

        settings = get_app_settings()
        saved = request.args.get("saved") == "1"

        html = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>ReelForge AI — Settings</title>

<style>
*{box-sizing:border-box}

body{
    margin:0;
    background:#07070d;
    color:#fff;
    font-family:Arial,sans-serif;
}

.container{
    max-width:850px;
    margin:auto;
    padding:30px 16px 60px;
}

h1{
    margin:0 0 8px;
}

.subtitle{
    color:#999;
    margin-bottom:25px;
}

.card{
    background:#11111a;
    border:1px solid #292936;
    border-radius:16px;
    padding:22px;
    margin-bottom:18px;
}

.card h2{
    margin:0 0 16px;
    font-size:19px;
}

.options{
    display:grid;
    grid-template-columns:repeat(2,1fr);
    gap:12px;
}

.option{
    position:relative;
}

.provider-switch{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
    margin-top:10px;
}

.provider-option{
    position:relative;
}

.provider-option input{
    position:absolute;
    opacity:0;
    pointer-events:none;
}

.provider-option label{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    padding:13px 15px;
    border:1px solid #333;
    border-radius:11px;
    cursor:pointer;
    background:#0c0c14;
    transition:.15s;
}

.provider-option input:checked + label{
    border-color:#7c3aed;
    background:#1b132d;
    box-shadow:0 0 0 1px #7c3aed;
}

.provider-name{
    font-size:16px;
    font-weight:700;
}

.provider-desc{
    color:#999;
    font-size:12px;
}

.option input{
    position:absolute;
    opacity:0;
}

.option label{
    display:block;
    padding:18px;
    border:1px solid #333;
    border-radius:12px;
    cursor:pointer;
    background:#0c0c14;
}

.option input:checked + label{
    border-color:#7c3aed;
    background:#1b132d;
    box-shadow:0 0 0 1px #7c3aed;
}

.option-title{
    font-size:17px;
    font-weight:bold;
}

.option-desc{
    color:#999;
    font-size:13px;
    margin-top:5px;
}

select{
    width:100%;
    padding:14px;
    border-radius:10px;
    border:1px solid #333;
    background:#0c0c14;
    color:white;
    font-size:15px;
}

button{
    width:100%;
    padding:15px;
    border:0;
    border-radius:11px;
    background:#7c3aed;
    color:white;
    font-size:16px;
    font-weight:bold;
    cursor:pointer;
}

.status{
    padding:14px;
    border-radius:10px;
    background:#09251d;
    border:1px solid #065f46;
    color:#6ee7b7;
    margin-bottom:18px;
}

.back{
    display:inline-block;
    color:#a78bfa;
    text-decoration:none;
    margin-bottom:20px;
}

@media(max-width:650px){
    .options{
        grid-template-columns:1fr;
    }
}
</style>
</head>

<body>

<div class="container">

<a class="back"
   href="/admin/users">
   ← Назад к пользователям
</a>

<h1>⚙️ ReelForge AI — Настройки</h1>

<div class="subtitle">
Глобальные настройки AI и тарифного режима
</div>

{% if saved %}
<div class="status">
✅ Настройки успешно сохранены
</div>
{% endif %}

<form method="post">



<div class="card">

<h2>🤖 AI-провайдер</h2>

<div class="provider-switch">
{% for key, provider in providers.items() %}
    <div class="provider-option">
        <input id="provider_{{ key }}" type="radio" name="ai_provider" value="{{ key }}" {% if settings.ai_provider == key %}checked{% endif %}>
        <label for="provider_{{ key }}">
            <span>
                <span class="provider-name">{{ provider.name }}</span>
                <span class="provider-desc">
                    {{ "🟢 Подключён" if provider.connected else "⚪ Не подключён" }}
                    {% if provider_info[key].key_mask %}
                        · {{ provider_info[key].key_mask }}
                    {% endif %}
                </span>
            </span>
        </label>
    </div>
{% endfor %}
</div>

</div>

<div class="card">

<h2>🧠 Модель</h2>

<select name="ai_model" id="ai_model">
{% for key, provider in providers.items() %}
    {% for model in provider.models %}
    <option value="{{ model }}" data-provider="{{ key }}" {% if settings.ai_provider == key and settings.ai_model == model %}selected{% endif %}>
        {{ provider.name }} — {{ model }}
    </option>
    {% endfor %}
{% endfor %}
</select>

</div>

<div class="card">

<h2>💳 Тарифный режим</h2>

<div class="options">

<div class="option">
<input id="plan_free" type="radio" name="plan" value="free"
{% if settings.plan == "free" %}checked{% endif %}>
<label for="plan_free">
<div class="option-title">Free</div>
<div class="option-desc">Бесплатный режим</div>
</label>
</div>

<div class="option">
<input id="plan_basic" type="radio" name="plan" value="basic"
{% if settings.plan == "basic" %}checked{% endif %}>
<label for="plan_basic">
<div class="option-title">Basic</div>
<div class="option-desc">Базовый платный тариф</div>
</label>
</div>

<div class="option">
<input id="plan_pro" type="radio" name="plan" value="pro"
{% if settings.plan == "pro" %}checked{% endif %}>
<label for="plan_pro">
<div class="option-title">Pro</div>
<div class="option-desc">Расширенный тариф</div>
</label>
</div>

<div class="option">
<input id="plan_unlimited" type="radio" name="plan" value="unlimited"
{% if settings.plan == "unlimited" %}checked{% endif %}>
<label for="plan_unlimited">
<div class="option-title">Unlimited</div>
<div class="option-desc">Без ограничения генераций</div>
</label>
</div>

</div>

</div>

<button type="submit">
💾 Сохранить настройки
</button>

</form>

<script>
(function(){
    const providers = document.querySelectorAll('input[name="ai_provider"]');
    const modelSelect = document.getElementById("ai_model");

    function updateModels(){
        const selected = document.querySelector('input[name="ai_provider"]:checked');
        if (!selected || !modelSelect) return;

        const provider = selected.value;
        let first = null;

        Array.from(modelSelect.options).forEach(function(option){
            const visible = option.dataset.provider === provider;
            option.hidden = !visible;
            if (visible && first === null) first = option;
        });

        const current = modelSelect.options[modelSelect.selectedIndex];
        if (!current || current.dataset.provider !== provider){
            if (first) modelSelect.value = first.value;
        }
    }

    providers.forEach(function(input){
        input.addEventListener("change", updateModels);
    });

    updateModels();
})();
</script>

</div>

</body>
</html>
"""

        return render_template_string(
            html,
            settings=settings,
            saved=saved,
            providers=get_available_providers(),
            provider_info=get_provider_admin_info()
        )

    except Exception as e:
        print(f"[ADMIN] SETTINGS ERROR: {e}", flush=True)
        return "Ошибка настроек", 500


@app.route("/admin/users")
def admin_users():
    """
    Полноценная админ-панель пользователей ReelForge AI.
    """

    if not admin_session_ok():
        return "Доступ запрещён", 403

    search = (request.args.get("search") or "").strip().lower()

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                if search:
                    cur.execute("""
                        SELECT
                            id,
                            user_key,
                            videos_balance,
                            free_entries_used,
                            free_entries_limit,
                            unlimited_access,
                            created_at
                        FROM users
                        WHERE LOWER(user_key) LIKE %s
                        ORDER BY id DESC
                    """, (f"%{search}%",))
                else:
                    cur.execute("""
                        SELECT
                            id,
                            user_key,
                            videos_balance,
                            free_entries_used,
                            free_entries_limit,
                            unlimited_access,
                            created_at
                        FROM users
                        ORDER BY id DESC
                    """)

                rows = cur.fetchall()

        html = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>ReelForge AI — Admin</title>

<style>
*{box-sizing:border-box}

body{
    margin:0;
    background:#07070d;
    color:#fff;
    font-family:Arial,sans-serif;
}

.container{
    max-width:1200px;
    margin:auto;
    padding:25px 16px 50px;
}

h1{
    margin:0 0 8px;
}

.subtitle{
    color:#999;
    margin-bottom:25px;
}

.search{
    display:flex;
    gap:10px;
    margin-bottom:20px;
}

.search input{
    flex:1;
    padding:13px 15px;
    border-radius:10px;
    border:1px solid #333;
    background:#11111a;
    color:white;
    font-size:15px;
}

button,.btn{
    border:0;
    border-radius:9px;
    padding:10px 13px;
    color:white;
    text-decoration:none;
    cursor:pointer;
    display:inline-block;
    font-size:14px;
}

.search button{
    background:#7c3aed;
}

table{
    width:100%;
    border-collapse:collapse;
    background:#11111a;
    border-radius:14px;
    overflow:hidden;
}

th,td{
    padding:13px 10px;
    border-bottom:1px solid #292936;
    text-align:left;
    vertical-align:middle;
}

th{
    color:#a78bfa;
    font-size:13px;
}

.email{
    word-break:break-all;
}

.actions{
    display:flex;
    flex-wrap:wrap;
    gap:6px;
}

.plus{
    background:#2563eb;
}

.reset{
    background:#7c3aed;
}

.on{
    background:#059669;
}

.off{
    background:#dc2626;
}

.unlimited{
    color:#34d399;
    font-weight:bold;
}

.normal{
    color:#c4b5fd;
}

@media(max-width:800px){
    table{
        font-size:12px;
    }

    th,td{
        padding:9px 6px;
    }

    .actions{
        flex-direction:column;
    }
}
</style>
</head>

<body>

<div class="container">

<h1>⚡ ReelForge AI — Админ-панель</h1>

<div class="subtitle">
Управление бесплатными генерациями и доступом пользователей
</div>

<form class="search" method="get" action="/admin/users">
    <input
    >

    <input
        name="search"
        placeholder="Поиск по email..."
        value="{{ search }}"
    >

    <button type="submit">🔎 Найти</button>
</form>

<table>
<thead>
<tr>
    <th>ID</th>
    <th>Email / User</th>
    <th>Оплачено</th>
    <th>Free</th>
    <th>Статус</th>
    <th>Действия</th>
</tr>
</thead>

<tbody>

{% for row in rows %}

<tr>

<td>{{ row[0] }}</td>

<td class="email">{{ row[1] }}</td>

<td>{{ row[2] }}</td>

<td>
    {% if row[5] %}
        <span class="unlimited">∞ Безлимит</span>
    {% else %}
        {{ row[3] }} / {{ row[4] }}
    {% endif %}
</td>

<td>
    {% if row[5] %}
        <span class="unlimited">ACTIVE</span>
    {% else %}
        <span class="normal">Обычный</span>
    {% endif %}
</td>

<td>

<div class="actions">

<a class="btn plus"
   href="/admin/add-free?user_key={{ row[1] }}&amount=3">
   +3
</a>

<a class="btn off"
   href="/admin/add-free?user_key={{ row[1] }}&amount=-3">
   −3
</a>

<a class="btn reset"
   href="/admin/reset-user?user_key={{ row[1] }}">
   Сброс
</a>

{% if row[5] %}

<a class="btn off"
   href="/admin/unlimited?user_key={{ row[1] }}&value=0">
   ∞ Выкл
</a>

{% else %}

<a class="btn on"
   href="/admin/unlimited?user_key={{ row[1] }}&value=1">
   ∞ Вкл
</a>

{% endif %}

</div>

</td>

</tr>

{% endfor %}

</tbody>
</table>

</div>

</body>
</html>
"""

        return render_template_string(
            html,
            rows=rows,
            search=search
        )

    except Exception as e:
        print(f"[ADMIN] USERS ERROR: {e}", flush=True)
        return "Ошибка загрузки пользователей", 500


@app.route("/admin/add-free")
def admin_add_free():
    """Добавляет бесплатные генерации пользователю."""
    user_key = request.args.get("user_key", "").strip()

    try:
        amount = int(request.args.get("amount", "3"))
    except ValueError:
        return "Некорректное количество", 400

    if not admin_session_ok():
        return "Доступ запрещён", 403

    if not user_key:
        return "user_key не указан", 400

    amount = max(-1000, min(amount, 1000))

    if amount == 0:
        return redirect("/admin/users")

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET free_entries_limit =
                        GREATEST(
                            free_entries_used,
                            free_entries_limit + %s
                        )
                    WHERE user_key = %s
                    RETURNING user_key, free_entries_limit
                """, (amount, user_key))

                row = cur.fetchone()
                conn.commit()

        if not row:
            return "Пользователь не найден", 404

        print(
            f"[ADMIN] add_free user={user_key} amount={amount} "
            f"new_limit={row[1]}",
            flush=True
        )

        return redirect("/admin/users")

    except Exception as e:
        print(f"[ADMIN] ADD FREE ERROR: {e}", flush=True)
        return "Ошибка добавления бесплатных генераций", 500


@app.route("/admin/unlimited")
def admin_unlimited():
    """Включает или выключает безлимитный доступ."""
    user_key = request.args.get("user_key", "").strip()
    value = request.args.get("value", "0") == "1"

    if not admin_session_ok():
        return "Доступ запрещён", 403

    if not user_key:
        return "user_key не указан", 400

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET unlimited_access = %s
                    WHERE user_key = %s
                    RETURNING user_key, unlimited_access
                """, (value, user_key))

                row = cur.fetchone()
                conn.commit()

        if not row:
            return "Пользователь не найден", 404

        print(
            f"[ADMIN] unlimited user={user_key} value={value}",
            flush=True
        )

        return redirect("/admin/users")

    except Exception as e:
        print(f"[ADMIN] UNLIMITED ERROR: {e}", flush=True)
        return "Ошибка изменения доступа", 500


@app.route("/admin/reset-user")
def admin_reset_user():
    """
    Сброс бесплатного счётчика конкретного пользователя.
    """
    user_key = request.args.get("user_key", "").strip()

    if not admin_session_ok():
        return "Доступ запрещён", 403

    if not user_key:
        return "user_key не указан", 400

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET free_entries_used = 0
                    WHERE user_key = %s
                    RETURNING user_key
                """, (user_key,))

                row = cur.fetchone()

            conn.commit()

        if not row:
            return "Пользователь не найден", 404

        print(
            f"[ADMIN] reset user={user_key}",
            flush=True
        )

        return redirect("/admin/users")

    except Exception as e:
        print(f"[ADMIN] RESET USER ERROR: {e}", flush=True)
        return "Ошибка сброса пользователя", 500


@app.route("/collect-email", methods=["GET"])
def collect_email():
    email = (request.args.get("email") or "").strip().lower()

    if not email:
        return "Email не указан", 400

    if "@" not in email or "." not in email:
        return "Введите корректный email", 400

    try:
        user_key = get_or_create_user_by_email(email)

        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO emails (email)
                    VALUES (%s)
                    ON CONFLICT (email) DO NOTHING
                """, (email,))
            conn.commit()

        print(
            f"[EMAIL] collected email={email} user_key={user_key}",
            flush=True
        )

    except Exception as e:
        print(f"[EMAIL] ERROR: {e}", flush=True)
        return "Ошибка сохранения email", 500

    response = redirect("/")

    response.set_cookie(
        "rf_user_key",
        user_key,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax"
    )

    response.set_cookie(
        "rf_email",
        email,
        max_age=60 * 60 * 24 * 365,
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

    response.set_cookie(
        "rf_access_count",
        "0",
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="Lax"
    )

    return response



@app.route("/directory", methods=["GET"])
def directory_page():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ReelForge AI — AI Reel & Short Video Creator</title>
  <meta name="description" content="ReelForge AI creates short-form videos and Reels from your ideas, images, screenshots and video clips.">
</head>
<body>
  <main style="max-width:900px;margin:60px auto;padding:24px;font-family:Arial,sans-serif">
    <h1>ReelForge AI</h1>
    <h2>AI Reel & Short Video Creator</h2>
    <p>Create short-form videos from ideas, images, screenshots and video clips.</p>
    <p>ReelForge AI is designed for creators who want to turn content into ready-to-publish Reels quickly.</p>
    <p><strong>Category:</strong> AI Video Generation / Content Creation</p>
    <p><strong>Website:</strong>
      <a href="https://reelforge-landing-steel.vercel.app">ReelForge AI</a>
    </p>

    <div style="text-align:center;margin:40px 0">
      <a href="https://www.promptfrenzy.com/directory"
         target="_blank"
         rel="dofollow"
         title="Featured on PromptFrenzy AI Directory">
        <img src="https://www.promptfrenzy.com/badges/directory-mono-light.svg"
             alt="Featured on PromptFrenzy AI Directory"
             width="220"
             height="44"
             loading="lazy">
      </a>
    </div>
  </main>
</body>
</html>
"""

@app.route("/promptfrenzy")
def promptfrenzy_badge():
    return """<!doctype html><html><head><meta charset="utf-8"><title>ReelForge AI — PromptFrenzy</title></head><body><main style="max-width:900px;margin:60px auto;padding:24px;font-family:Arial,sans-serif"><h1>ReelForge AI</h1><p>AI-powered tool for creating ready-to-publish Reels quickly.</p><p><a href="https://www.promptfrenzy.com/directory" target="_blank" rel="noopener" title="Featured on PromptFrenzy AI Directory"><img src="https://www.promptfrenzy.com/badges/directory.svg" alt="Featured on PromptFrenzy AI Directory" width="220" height="44"></a></p></main></body></html>"""

@app.route("/admin/ai-provider-test")
def admin_ai_provider_test():
    if not admin_session_ok():
        return jsonify({"ok": False, "error": "Доступ запрещён"}), 403

    provider = (request.args.get("provider") or "").strip().lower()

    if provider not in PROVIDER_CONFIG:
        return jsonify({"ok": False, "error": "Некорректный AI provider"}), 400

    try:
        client = create_provider_client(provider)
        model = PROVIDER_CONFIG[provider]["models"][0]
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK"}],
            max_completion_tokens=5
        )
        return jsonify({
            "ok": True,
            "provider": provider,
            "model": model,
            "response": response.choices[0].message.content
        })
    except Exception as e:
        print(f"[ADMIN] PROVIDER TEST ERROR provider={provider}: {e}", flush=True)
        return jsonify({
            "ok": False,
            "provider": provider,
            "error": str(e)
        }), 500


@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
