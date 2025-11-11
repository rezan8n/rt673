from flask import Flask, request
import requests
import os
import logging
import time
import traceback

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

if not TELEGRAM_TOKEN or not AI_API_KEY:
    raise ValueError("توکن تلگرام یا کلید API هوش مصنوعی تنظیم نشده‌اند!")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
AI_BASE = "https://generativelanguage.googleapis.com/v1"

session = requests.Session()

def retry_request(func, retries=3, backoff=1):
    for attempt in range(1, retries + 1):
        try:
            return func()
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
    try:
        data = request.get_json(silent=True)
        logging.info("📩 پیام دریافتی از تلگرام: %s", data)

        if not data:
            logging.warning("⚠️ بدنه درخواست خالی یا فرمت JSON نامعتبر است.")
            return "Invalid request", 400

        message = data.get("message")
        if not message:
            logging.info("⚠️ این آپدیت پیام معمولی نیست، نادیده گرفته شد.")
            return "ok"

        text = message.get("text")
        chat = message.get("chat")
        if not text or not chat:
            logging.warning("⚠️ ساختار پیام نامعتبره یا پیام متنی نیست")
            chat_id = chat.get("id") if chat else None
            if chat_id:
                send_message(chat_id, "متأسفم، من فقط پیام‌های متنی را پردازش می‌کنم.")
            return "Invalid message format", 400

        chat_id = chat["id"]

        try:
            reply = ask_ai(text)
        except Exception:
            logging.exception("❌ خطا در فراخوانی هوش مصنوعی:")
            reply = "متأسفم، در دریافت پاسخ از سرویس هوش مصنوعی مشکلی پیش آمد. لطفاً بعداً تلاش کنید."

        send_message(chat_id, reply)
        return "ok"
    except Exception as e:
        logging.error("Unhandled exception in webhook: %s\n%s", e, traceback.format_exc())
        return "server error", 500

def send_message(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    def do_post():
        res = session.post(TELEGRAM_API_URL, json=payload, timeout=10)
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
    except Exception:
        logging.exception("❌ خطا در ارسال پیام به تلگرام")

def list_models():
    url = f"{AI_BASE}/models"
    def do_get():
        res = session.get(url, params={"key": AI_API_KEY}, timeout=10)
        if res.status_code != 200:
            logging.error("ListModels returned %s: %s", res.status_code, res.text)
            res.raise_for_status()
        return res.json()
    return retry_request(do_get, retries=2, backoff=1)

def try_generate_with_url(url, payload):
    def do_post():
        res = session.post(url, params={"key": AI_API_KEY}, json=payload, timeout=15)
        if res.status_code != 200:
            logging.error("AI API returned %s: %s", res.status_code, res.text)
            res.raise_for_status()
        return res.json()
    return retry_request(do_post, retries=1, backoff=1)

def extract_text_from_response(data):
    reply_parts = []
    outputs = data.get("outputs")
    if outputs and isinstance(outputs, list):
        for out in outputs:
            content = out.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and "text" in c:
                        reply_parts.append(c["text"])
                    elif isinstance(c, str):
                        reply_parts.append(c)
    if not reply_parts:
        candidates = data.get("candidates")
        if candidates and isinstance(candidates, list):
            for cand in candidates:
                if isinstance(cand, dict):
                    if "output" in cand and isinstance(cand["output"], str):
                        reply_parts.append(cand["output"])
                    content = cand.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and "text" in c:
                                reply_parts.append(c["text"])
                            elif isinstance(c, str):
                                reply_parts.append(c)
    if not reply_parts:
        content = data.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and "text" in c:
                    reply_parts.append(c["text"])
                elif isinstance(c, str):
                    reply_parts.append(c)
    if not reply_parts:
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
        if strings:
            strings.sort(key=len, reverse=True)
            reply_parts.append(strings[0])
    if not reply_parts:
        return None
    reply = "\n\n".join(p.strip() for p in reply_parts if p and p.strip())
    if len(reply) > 4000:
        reply = reply[:3996] + "..."
    return reply

def ask_ai(message):
    models_info = list_models()
    model_names = []
    if isinstance(models_info, dict):
        items = models_info.get("models") or models_info.get("model") or []
        if isinstance(items, list):
            for m in items:
                name = m.get("name") if isinstance(m, dict) else None
                if name:
                    model_names.append(name)
    logging.info("Available models (sample): %s", model_names[:20])

    if not model_names:
        raise RuntimeError("No models found from ListModels")

    # choose model (full name from list, we will strip "models/" when building URL)
    chosen_full = None
    for name in model_names:
        ln = name.lower()
        if "bison" in ln or "gemini" in ln or "text" in ln:
            chosen_full = name
            break
    if not chosen_full:
        chosen_full = model_names[0]

    # If chosen_full is like 'models/gemini-2.5-flash', extract trailing part for URL
    chosen_for_url = chosen_full.split("/")[-1] if "/" in chosen_full else chosen_full
    logging.info("Selected model (full): %s ; using for calls: %s", chosen_full, chosen_for_url)

    errors = []
    # try generateContent
    url1 = f"{AI_BASE}/models/{chosen_for_url}:generateContent"
    payload1 = {"contents": [{"parts": [{"text": message}]}]}
    try:
        data = try_generate_with_url(url1, payload1)
        text = extract_text_from_response(data)
        if text:
            logging.info("🤖 پاسخ تولیدشده از هوش مصنوعی (via generateContent)")
            return text
    except Exception as e:
        errors.append(("generateContent", str(e)))
        logging.debug("generateContent failed for %s: %s", chosen_for_url, e)

    # try generateText
    url2 = f"{AI_BASE}/models/{chosen_for_url}:generateText"
    payload2 = {"prompt": {"text": message}}
    try:
        data = try_generate_with_url(url2, payload2)
        text = extract_text_from_response(data)
        if text:
            logging.info("🤖 پاسخ تولیدشده از هوش مصنوعی (via generateText)")
            return text
    except Exception as e:
        errors.append(("generateText", str(e)))
        logging.debug("generateText failed for %s: %s", chosen_for_url, e)

    # try generic generate
    url3 = f"{AI_BASE}/models/{chosen_for_url}:generate"
    payload3 = {"input": message}
    try:
        data = try_generate_with_url(url3, payload3)
        text = extract_text_from_response(data)
        if text:
            logging.info("🤖 پاسخ تولیدشده از هوش مصنوعی (via generate)")
            return text
    except Exception as e:
        errors.append(("generate", str(e)))
        logging.debug(":generate failed for %s: %s", chosen_for_url, e)

    logging.error("All generation attempts failed. Attempts: %s", errors)
    raise RuntimeError("AI generation failed; see server logs for details")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
