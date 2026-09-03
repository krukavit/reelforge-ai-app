from flask import request, redirect
import os
import psycopg

LANDING_URL = os.environ.get(
    "LANDING_URL",
    "https://reelforge-landing-steel.vercel.app"
)

ACCESS_TOKEN = "rf2026free"

COOKIE_NAME = "rf_access"
USER_COOKIE_NAME = "rf_user_key"

MAX_FREE_ENTRIES = 3


def db_connect():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL не задан")
    return psycopg.connect(database_url)


def get_user_access(user_key):
    if not user_key:
        return None

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        free_entries_used,
                        free_entries_limit,
                        unlimited_access,
                        blocked
                    FROM users
                    WHERE user_key = %s
                """, (user_key,))

                row = cur.fetchone()

        if not row:
            return None

        return {
            "used": row[0],
            "limit": row[1],
            "unlimited": bool(row[2]),
            "blocked": bool(row[3]),
        }

    except Exception as e:
        print(f"[ACCESS] DB ERROR: {e}", flush=True)
        return None


def increment_free_entries(user_key):
    if not user_key:
        return False

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET free_entries_used = free_entries_used + 1
                    WHERE user_key = %s
                      AND free_entries_used < %s
                    RETURNING free_entries_used
                """, (user_key, MAX_FREE_ENTRIES))

                row = cur.fetchone()

            conn.commit()

        if row:
            print(
                f"[ACCESS] free entry used user={user_key} count={row[0]}",
                flush=True
            )
            return True

        return False

    except Exception as e:
        print(f"[ACCESS] DB ERROR increment: {e}", flush=True)
        return False


def check_access():
    """
    Контроль доступа через PostgreSQL.

    - Админские маршруты всегда доступны middleware.
    - unlimited_access=True -> безлимит.
    - Иначе используется индивидуальный free_entries_limit.
    """

    if request.path == "/health":
        return None

    # Публичная страница для AI-каталогов и автоматической проверки.
    if request.path in ("/directory", "/promptfrenzy"):
        return None

    if request.path.startswith("/outputs/"):
        return None

    # Загрузки должны проходить до проверки генерации.
    if request.path in (
        "/start_video_upload",
        "/upload_video_part",
        "/upload_video_music",
        "/upload_video_music_part",
        "/finish_video_upload",
    ):
        return None

    # Оплата и email доступны без обычного доступа.
    if request.path in (
        "/payment",
        "/collect-email",
    ):
        return None

    # ============================================================
    # ADMIN — НИКОГДА НЕ ОТПРАВЛЯЕМ НА PAYMENT
    # ============================================================
    if request.path == "/admin" or request.path.startswith("/admin/"):
        return None

    user_key = request.cookies.get(USER_COOKIE_NAME)

    # Без email/user_key пользователь не получает доступ.
    if not user_key:
        return redirect(f"{LANDING_URL}?redirected=1", code=302)

    access = get_user_access(user_key)

    if access is None:
        return "Ошибка проверки доступа", 500

    if access["blocked"]:
        return "Доступ заблокирован", 403

    # Безлимитный пользователь.
    if access["unlimited"]:
        return None

    # Индивидуальный лимит бесплатных генераций.
    if access["used"] >= access["limit"]:
        return redirect("/payment", code=302)

    return None


def set_access_cookie(response):
    """
    Сохраняем только служебный access-cookie.
    Реальный счётчик находится в PostgreSQL.
    """

    user_key = request.cookies.get(USER_COOKIE_NAME)

    if user_key:
        response.set_cookie(
            COOKIE_NAME,
            ACCESS_TOKEN,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax"
        )

    return response
