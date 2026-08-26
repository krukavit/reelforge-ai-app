import os
from flask import Flask, request, render_template_string


app = Flask(__name__)

# Ленивая инициализация клиента Groq
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

# HTML-шаблон главной страницы
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
        button { background: #007bff; color: white; padding: 12px 24px; border: none; cursor: pointer; font-size: 16px; margin-top: 10px; }
        button:hover { background: #0056b3; }
    </style>
</head>
<body>
    <h1>ReelForge AI</h1>
    <p>Введи тему — сгенерирую сценарий для Reels</p>
    <form action="/generate" method="post">
        <textarea name="topic" placeholder="Например: 5 лайфхаков для продуктивности..."></textarea><br>
        <button type="submit">Сгенерировать</button>
    </form>
</body>
</html>
"""

# HTML-шаблон результата
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
    </style>
</head>
<body>
    <h1>Готово!</h1>
    <div class="result">{{ script }}</div>
    <a href="/">← Сгенерировать ещё</a>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/generate", methods=["POST"])
def generate():
    topic = request.form.get("topic", "")
    if not topic:
        return "Введи тему!", 400

    try:
        client = get_groq_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ты — эксперт по созданию вирусных сценариев для Instagram Reels. Пиши коротко, с хуком, с призывом к действию. Используй эмодзи."},
                {"role": "user", "content": f"Напиши сценарий для Reels на тему: {topic}"}
            ],
            max_tokens=500
        )
        script = response.choices[0].message.content
        return render_template_string(RESULT_HTML, script=script)
    except Exception as e:
        return f"Ошибка: {str(e)}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

@app.route("/health")
def health():
    return "OK", 200
