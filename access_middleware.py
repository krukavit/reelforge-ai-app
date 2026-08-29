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


def get_free_entries_used(user_key):
    if not user_key:
        return None

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT free_entries_used
                    FROM users
                    WHERE user_key = %s
                """, (user_key,))
                row = cur.fetchone()

        return row[0] if row else None

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
    Бесплатный доступ контролируется через PostgreSQL.

    Пользователь должен сначала указать email.
    После этого email получает постоянный user_key.

    Бесплатные входы: 3 на пользователя.
    """

    if request.path == "/health":
        return None

    if request.path.startswith("/outputs/"):
        return None

    if request.path in (
        "/start_video_upload",
        "/upload_video_part",
        "/upload_video_music",
        "/upload_video_music_part",
        "/finish_video_upload",
    ):
        return None

    if request.path == "/payment":
        return None

    if request.path == "/collect-email":
        return None

    if request.path == "/admin/reset-free":
        return None

    if request.path == "/admin/users":
        return None

    user_key = request.cookies.get(USER_COOKIE_NAME)

    # Без email/user_key пользователь не получает доступ к приложению.
    if not user_key:
        return redirect(f"{LANDING_URL}?redirected=1", code=302)

    used = get_free_entries_used(user_key)

    # Пользователь существует, но база недоступна.
    if used is None:
        return "Ошибка проверки доступа", 500

    # Если бесплатные входы закончились — оплата.
    if used >= MAX_FREE_ENTRIES:
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
