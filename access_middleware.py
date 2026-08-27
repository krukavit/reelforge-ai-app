from flask import request, redirect, make_response
import os

LANDING_URL = os.environ.get("LANDING_URL", "https://reelforge-landing-steel.vercel.app")
ACCESS_TOKEN = "rf2026free"
COOKIE_NAME = "rf_access"

def check_access():
    """Проверяет access-токен в URL или cookie. Разрешает /health и /outputs без проверки."""
    if request.path == "/health":
        return None
    if request.path.startswith("/outputs/"):
        return None

    token = request.args.get("access")
    if token == ACCESS_TOKEN:
        return None

    if request.cookies.get(COOKIE_NAME) == ACCESS_TOKEN:
        return None

    return redirect(f"{LANDING_URL}?redirected=1", code=302)

def set_access_cookie(response):
    """Ставит cookie, если в URL был правильный токен."""
    token = request.args.get("access")
    if token == ACCESS_TOKEN:
        response.set_cookie(COOKIE_NAME, ACCESS_TOKEN, max_age=60*60*24*30)
    return response
