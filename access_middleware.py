from flask import request, redirect
import os

LANDING_URL = os.environ.get(
    "LANDING_URL",
    "https://reelforge-landing-steel.vercel.app"
)

ACCESS_TOKEN = "rf2026free"

COOKIE_NAME = "rf_access"
COUNT_COOKIE_NAME = "rf_access_count"

MAX_FREE_ENTRIES = 3


def check_access():
    """
    Бесплатный доступ: 3 входа.
    Счётчик увеличивается только при входе на главную /
    с правильным access-токеном.

    Запросы /status, /generate, /create_video,
    /create_reel_from_videos и /outputs/* не расходуют вход.
    """

    if request.path == "/health":
        return None

    if request.path.startswith("/outputs/"):
        return None

    if request.path == "/payment":
        return None

    try:
        count = int(request.cookies.get(COUNT_COOKIE_NAME, "0"))
    except (TypeError, ValueError):
        count = 0

    token = request.args.get("access")
    has_cookie = request.cookies.get(COOKIE_NAME) == ACCESS_TOKEN

    # Вход по правильной ссылке.
    # Только "/" с access считается новым бесплатным входом.
    if token == ACCESS_TOKEN:
        if request.path == "/":
            if count < MAX_FREE_ENTRIES:
                return None
            return redirect("/payment", code=302)

        # access-токен на внутренних URL разрешаем,
        # но НЕ увеличиваем счётчик.
        return None

    # Уже авторизованный пользователь.
    if has_cookie:
        if count < MAX_FREE_ENTRIES:
            return None
        return redirect("/payment", code=302)

    return redirect(f"{LANDING_URL}?redirected=1", code=302)


def set_access_cookie(response):
    """
    Ставит cookie и увеличивает счётчик ТОЛЬКО при
    настоящем входе на главную страницу с access-токеном.
    """

    token = request.args.get("access")

    # Считаем только /?access=rf2026free
    if token == ACCESS_TOKEN and request.path == "/":

        try:
            count = int(request.cookies.get(COUNT_COOKIE_NAME, "0"))
        except (TypeError, ValueError):
            count = 0

        new_count = min(count + 1, MAX_FREE_ENTRIES)

        response.set_cookie(
            COOKIE_NAME,
            ACCESS_TOKEN,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="Lax"
        )

        response.set_cookie(
            COUNT_COOKIE_NAME,
            str(new_count),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="Lax"
        )

    return response
