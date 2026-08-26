from flask import request, redirect
import os

LANDING_URL = os.environ.get("LANDING_URL", "https://reelforge-landing-steel.vercel.app")
ACCESS_TOKEN = "rf2026free"

def check_access():
    """Проверяет access-токен в URL. Разрешает /health и /outputs без проверки."""
    if request.path == "/health":
        return None
    if request.path.startswith("/outputs/"):
        return None
    token = request.args.get("access")
    if token != ACCESS_TOKEN:
        return redirect(f"{LANDING_URL}?redirected=1", code=302)
    return None
