from flask import Flask, request
import requests
import os
import logging
import time

app = Flask(__name__)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

if not TELEGRAM_TOKEN or not AI_API_KEY:
    raise ValueError("توکن تلگرام یا کلید API هوش مصنوعی تنظیم نشده‌اند!")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
AI_API_URL = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent?key={AI_API_KEY}"

# Simple retry decorator for network calls
def retry_request(func, retries=3, backoff=1, *args, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.warning("Attempt %d failed: %s", attempt, e)
            if attempt == retries:
                raise
            time.sleep(backoff * attempt)

@app.route("/", methods=["GET"])
def index():
    return "✅ Bot is running!"

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    logging.info("📩 پیام دریافتی از تلگرام: %s", data)

    # Basic validation: Telegram may send updates that do not include 'message' (e.g., callback_query)
    if not data:
        logging.warning("⚠️ بدنه درخواست خالی یا فرمت JSON نامعتبر است.")
        return "Invalid request", 400

    message = data.get("message")
    if not message:
        logging.info("⚠️ این آپدیت پیام معمولی نیست، نادیده گرفته شد.")
        return "ok"  # we acknowledge non-message updates to avoid retries

    text = message.get("text")
    chat = message.get("chat")
    if not text or not chat:
        logging.warning("⚠️ ساختار پیام نامعتبره یا پیام متنی نیست")
        # Optionally notify the user that only text is supported
        chat_id = chat.get("id") if chat else None
        if chat_id:
            send_message(chat_id, "متأسفم، من فقط پیام‌های متنی را پردازش می‌کنم.")
        return "Invalid message format", 400

    chat_id = chat["id"]

    try:
        reply = ask_ai(text)
    except Exception as e:
        logging.exception("❌ خطا در فراخوانی هوش مصنوعی:")
        reply = "متأسفم، در دریافت پاسخ از سرویس هوش مصنوعی مشکلی پیش آمد. لطفاً بعداً تلاش کنید."

    send_message(chat_id, reply)
    return "ok"

def send_message(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    def do_post():
        res = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        # Telegram returns 200 even for application-level errors; check ok field
        try:
            j = res.json()
        except Exception:
            res.raise_for_status()
        if res.status_code != 200 or not j.get("ok", False):
            raise RuntimeError(f"Telegram error: {res.status_code} - {res.text}")
        return j

    try:
        result = retry_request(do_post, retries=3, backoff=1)
        logging.info("📤 پاسخ ارسال‌شده به تلگرام: %s", result)
    except Exception as e:
        logging.exception("❌ خطا در ارسال پیام به تلگرام: %s", e)

def ask_ai(message):
    """
    Calls the Generative Language API and returns a text reply.
    This function is defensive about response shapes because the API
    may return different JSON structures depending on model/version.
    """
    payload = {
        # Keep user's original style but also include a prompt wrapper to be robust
        "contents": [
            {"parts": [{"text": message}]}
        ],
        # Additional fields you may tune:
        # "temperature": 0.3,
        # "candidate_count": 1,
    }

    def do_post():
        res = requests.post(AI_API_URL, json=payload, timeout=15)
        if res.status_code != 200:
            # try to surface the API error for logs
            try:
                logging.error("AI API returned %s: %s", res.status_code, res.text)
            except Exception:
                logging.error("AI API returned non-200 status: %s", res.status_code)
            res.raise_for_status()
        return res.json()

    data = retry_request(do_post, retries=2, backoff=1)

    # Try multiple known response shapes to extract text
    reply_parts = []

    # 1) v1-like outputs -> data["outputs"][...]["content"] -> list of parts with "text"
    outputs = data.get("outputs")
    if outputs and isinstance(outputs, list):
        for out in outputs:
            content = out.get("content")
            if isinstance(content, list):
                for c in content:
                    # parts might be dicts with "text"
                    if isinstance(c, dict) and "text" in c:
                        reply_parts.append(c["text"])
                    elif isinstance(c, str):
                        reply_parts.append(c)
    # 2) candidates -> might include "output" or "content"
    if not reply_parts:
        candidates = data.get("candidates")
        if candidates and isinstance(candidates, list):
            for cand in candidates:
                # cand might have "output"
                if "output" in cand and isinstance(cand["output"], str):
                    reply_parts.append(cand["output"])
                # or nested content
                content = cand.get("content")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and "text" in c:
                            reply_parts.append(c["text"])
                        elif isinstance(c, str):
                            reply_parts.append(c)
    # 3) content directly at top-level
    if not reply_parts:
        content = data.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and "text" in c:
                    reply_parts.append(c["text"])
                elif isinstance(c, str):
                    reply_parts.append(c)

    # 4) fallback: try to pretty-print whole JSON (not ideal, but better than empty)
    if not reply_parts:
        # Try to find any string values in the response (last resort)
        def extract_strings(obj):
            found = []
            if isinstance(obj, str):
                found.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    found.extend(extract_strings(v))
            elif isinstance(obj, list):
                for item in obj:
                    found.extend(extract_strings(item))
            return found

        strings = extract_strings(data)
        # pick the longest string (heuristic)
        if strings:
            strings.sort(key=len, reverse=True)
            reply_parts.append(strings[0])

    if not reply_parts:
        raise RuntimeError("Couldn't extract text from AI response")

    # Join parts, strip excessive whitespace
    reply = "\n\n".join(p.strip() for p in reply_parts if p and p.strip())
    # Keep reply short if extremely long (Telegram message length limits)
    if len(reply) > 4000:
        reply = reply[:3996] + "..."

    logging.info("🤖 پاسخ تولیدشده از هوش مصنوعی: %s", reply)
    return reply

if __name__ == "__main__":
    # For local testing only. Use a production WSGI server (gunicorn/uvicorn) when deploying.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
