from flask import request, redirect, make_response
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
    Разрешает первые 3 входа по access-токену.
    После 3 входов отправляет пользователя на страницу оплаты.
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

    if token == ACCESS_TOKEN:
        if count < MAX_FREE_ENTRIES:
            return None
        return redirect("/payment", code=302)

    if request.cookies.get(COOKIE_NAME) == ACCESS_TOKEN:
        if count < MAX_FREE_ENTRIES:
            return None
        return redirect("/payment", code=302)

    return redirect(f"{LANDING_URL}?redirected=1", code=302)


def set_access_cookie(response):
    """
    Сохраняет access cookie и увеличивает счётчик входов.
    """

    token = request.args.get("access")

    # Новый вход по правильному токену
    if token == ACCESS_TOKEN:

        try:
            count = int(request.cookies.get(COUNT_COOKIE_NAME, "0"))
        except (TypeError, ValueError):
            count = 0

        # Увеличиваем счётчик только до максимума
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
