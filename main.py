#!/usr/bin/env python3
"""
main.py - Telegram bot using Google Generative Language (Gemini) REST API (v1beta).

Features:
- Uses x-goog-api-key header (as required by Gemini docs).
- Defaults to a hardcoded model via AI_MODEL env or discovers models once.
- Handles 429 (rate limit) with exponential backoff, Retry-After support and a simple circuit-breaker.
- Queues and retries requests in background to improve UX when API is rate-limited.
- Defensive extraction of text from various possible response shapes.
- Does NOT log API keys or include them in printed URLs.
- Simple to run on Render / Heroku / any container: set TELEGRAM_TOKEN, AI_API_KEY, optional AI_MODEL, PORT.

Env vars:
- TELEGRAM_TOKEN (required)
- AI_API_KEY (required)
- AI_MODEL (optional, e.g. "gemini-2.5-flash" or "gemini-2.5-pro")
- PORT (optional, default 8080)
"""

from flask import Flask, request
import requests
import os
import logging
import time
import traceback
import threading
import queue
from typing import Optional, Dict, Any, List

# -------------------- Configuration --------------------
APP_NAME = "telegram-gemini-bot"
AI_API_BASE = os.getenv("AI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL_ENV = os.getenv("AI_MODEL")  # e.g. "gemini-2.5-flash"
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN or not AI_API_KEY:
    raise RuntimeError("Environment variables TELEGRAM_TOKEN and AI_API_KEY must be set.")

TELEGRAM_SEND_URL = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_TOKEN}/sendMessage"
# Optional tuning
MODEL_DISCOVERY_TTL = 300  # seconds (cache models)
BACKGROUND_MAX_ATTEMPTS = int(os.getenv("BACKGROUND_MAX_ATTEMPTS", "4"))
BACKGROUND_BASE_BACKOFF = float(os.getenv("BACKGROUND_BASE_BACKOFF", "2.0"))
CIRCUIT_BREAKER_PAUSE = int(os.getenv("CIRCUIT_BREAKER_PAUSE", "60"))  # seconds

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(APP_NAME)

# -------------------- Globals --------------------
session = requests.Session()
_model_cache: Dict[str, Any] = {"models": [], "ts": 0}
_model_cache_lock = threading.Lock()

_last_rate_limit_time = 0.0
_last_rate_limit_lock = threading.Lock()

_retry_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()

app = Flask(__name__)


# -------------------- Utilities --------------------
class RateLimitError(Exception):
    def __init__(self, retry_after: Optional[int] = None, message: Optional[str] = None):
        super().__init__(message or "Rate limited")
        self.retry_after = retry_after


def safe_headers() -> Dict[str, str]:
    """Return headers for Gemini requests including x-goog-api-key."""
    return {
        "x-goog-api-key": AI_API_KEY,
        "Content-Type": "application/json"
    }


def send_telegram(chat_id: int, text: str) -> bool:
    """Send a message to Telegram. Returns True on success."""
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = session.post(TELEGRAM_SEND_URL, json=payload, timeout=10)
        try:
            j = r.json()
        except Exception:
            r.raise_for_status()
        if r.status_code != 200 or not j.get("ok", False):
            logger.error("Telegram API failed: %s %s", r.status_code, r.text)
            return False
        logger.info("Sent message to Telegram chat_id=%s message_id=%s", chat_id, j["result"].get("message_id"))
        return True
    except Exception:
        logger.exception("Exception while sending message to Telegram")
        return False


# -------------------- Model discovery & selection --------------------
def list_models() -> List[str]:
    """List models (cached). Returns list of model name strings such as 'models/gemini-2.5-flash'."""
    now = time.time()
    with _model_cache_lock:
        if _model_cache["models"] and now - _model_cache["ts"] < MODEL_DISCOVERY_TTL:
            return _model_cache["models"]

    url = f"{AI_API_BASE}/models"
    try:
        r = session.get(url, headers=safe_headers(), timeout=10)
        if r.status_code != 200:
            logger.error("ListModels failed: %s %s", r.status_code, r.text)
            r.raise_for_status()
        data = r.json()
        items = data.get("models") or data.get("model") or []
        result = []
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "name" in m:
                    result.append(m["name"])
        with _model_cache_lock:
            _model_cache["models"] = result
            _model_cache["ts"] = time.time()
        logger.info("Discovered %d models (cached for %ds)", len(result), MODEL_DISCOVERY_TTL)
        return result
    except Exception:
        logger.exception("Failed to list models")
        return []


def choose_model() -> str:
    """Return the model identifier to use in URL (e.g., 'gemini-2.5-flash')."""
    # Prefer explicit env
    if AI_MODEL_ENV:
        # allow passing either full name 'models/gemini-2.5-flash' or short 'gemini-2.5-flash'
        return AI_MODEL_ENV.split("/")[-1]

    models = list_models()
    if not models:
        # fallback to a conservative default short name; user may override via AI_MODEL env
        logger.warning("No models discovered, falling back to 'gemini-2.5-flash'. Set AI_MODEL env to override.")
        return "gemini-2.5-flash"

    # prefer gemini or bison or text
    for name in models:
        ln = name.lower()
        if "gemini" in ln or "bison" in ln or "text" in ln:
            return name.split("/")[-1]
    # otherwise first
    return models[0].split("/")[-1]


# -------------------- AI call helpers --------------------
def ai_post(url: str, payload: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    """
    POST to Gemini endpoint with x-goog-api-key header.
    Raises RateLimitError on 429 (with retry_after if provided).
    Raises requests.HTTPError for other non-200.
    """
    headers = safe_headers()
    r = session.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code == 429:
        retry_after = None
        try:
            ra = r.headers.get("Retry-After")
            if ra:
                retry_after = int(float(ra))
        except Exception:
            retry_after = None
        logger.error("AI API 429: %s", r.text)
        raise RateLimitError(retry_after=retry_after)
    if r.status_code != 200:
        logger.error("AI API returned %s: %s", r.status_code, r.text)
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        # If cannot parse JSON, raise
        logger.exception("AI API returned non-JSON response")
        raise


def extract_text_from_response(data: Dict[str, Any]) -> Optional[str]:
    """Defensive extraction of human-readable text from multiple response shapes."""
    parts = []

    # 1) candidates -> content -> parts -> text
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for cand in candidates:
            content = cand.get("content") if isinstance(cand, dict) else None
            if isinstance(content, dict):
                cont_parts = content.get("parts") or []
                for p in cont_parts:
                    if isinstance(p, dict) and "text" in p:
                        parts.append(p["text"])
                    elif isinstance(p, str):
                        parts.append(p)

    # 2) outputs -> content -> parts/text (some responses use outputs/content array)
    outputs = data.get("outputs")
    if isinstance(outputs, list):
        for out in outputs:
            content = out.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(item["text"])
                    elif isinstance(item, str):
                        parts.append(item)

    # 3) top-level content
    content = data.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and "text" in c:
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)

    # 4) candidates with nested content.parts (alternate shape)
    if not parts and isinstance(candidates, list):
        for cand in candidates:
            content = cand.get("content")
            if isinstance(content, dict):
                nested_parts = content.get("parts") or []
                for np in nested_parts:
                    if isinstance(np, dict) and "text" in np:
                        parts.append(np["text"])

    # 5) fallback: extract longest string anywhere (last resort)
    if not parts:
        def find_strings(obj):
            results = []
            if isinstance(obj, str):
                results.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    results.extend(find_strings(v))
            elif isinstance(obj, list):
                for i in obj:
                    results.extend(find_strings(i))
            return results
        all_strings = find_strings(data)
        if all_strings:
            all_strings.sort(key=len, reverse=True)
            parts.append(all_strings[0])

    if not parts:
        return None
    # join and trim
    reply = "\n\n".join(p.strip() for p in parts if p and p.strip())
    if len(reply) > 4000:
        reply = reply[:3996] + "..."
    return reply


def generate_with_model(message: str, chosen_model: Optional[str] = None) -> str:
    """
    Try several endpoints for the chosen model.
    Raises RateLimitError on 429.
    Raises RuntimeError if no text extracted.
    """
    model_for_url = chosen_model or choose_model()
    # ensure short name
    model_for_url = model_for_url.split("/")[-1]

    # trim very long inputs to reduce quota usage
    if len(message) > 3000:
        message = message[:3000] + "\n\n...[trimmed]"

    # 1) generateContent
    url1 = f"{AI_API_BASE}/models/{model_for_url}:generateContent"
    payload1 = {"contents": [{"parts": [{"text": message}]}]}
    try:
        data1 = ai_post(url1, payload1)
        text = extract_text_from_response(data1)
        if text:
            return text
    except RateLimitError:
        raise
    except requests.HTTPError:
        logger.debug("generateContent not supported or HTTP error, falling back", exc_info=True)
    except Exception:
        logger.debug("generateContent unexpected error, falling back", exc_info=True)

    # 2) generateText
    url2 = f"{AI_API_BASE}/models/{model_for_url}:generateText"
    payload2 = {"prompt": {"text": message}}
    try:
        data2 = ai_post(url2, payload2)
        text = extract_text_from_response(data2)
        if text:
            return text
    except RateLimitError:
        raise
    except requests.HTTPError:
        logger.debug("generateText not supported or HTTP error", exc_info=True)
    except Exception:
        logger.debug("generateText unexpected error", exc_info=True)

    # 3) generic generate
    url3 = f"{AI_API_BASE}/models/{model_for_url}:generate"
    payload3 = {"input": message}
    try:
        data3 = ai_post(url3, payload3)
        text = extract_text_from_response(data3)
        if text:
            return text
    except RateLimitError:
        raise
    except Exception:
        logger.debug("generate unexpected error", exc_info=True)

    raise RuntimeError("AI returned no extractable text")


# -------------------- Background queue & worker --------------------
def queue_request_for_background(chat_id: int, message: str):
    job = {
        "chat_id": chat_id,
        "message": message,
        "attempts_left": BACKGROUND_MAX_ATTEMPTS,
        "next_try": time.time()
    }
    _retry_q.put(job)
    logger.info("Queued background job for chat_id=%s attempts=%s", chat_id, BACKGROUND_MAX_ATTEMPTS)


def background_worker():
    global _last_rate_limit_time
    while True:
        try:
            job = _retry_q.get(timeout=1)
        except queue.Empty:
            continue

        try:
            now = time.time()
            if job["next_try"] > now:
                # not yet time
                _retry_q.put(job)
                time.sleep(0.5)
                _retry_q.task_done()
                continue

            # circuit-breaker: postpone if recently rate-limited globally
            with _last_rate_limit_lock:
                if time.time() - _last_rate_limit_time < CIRCUIT_BREAKER_PAUSE:
                    job["next_try"] = time.time() + CIRCUIT_BREAKER_PAUSE
                    _retry_q.put(job)
                    logger.info("Circuit open: postponing job for chat_id=%s", job["chat_id"])
                    _retry_q.task_done()
                    time.sleep(0.5)
                    continue

            logger.info("Background attempt chat_id=%s attempts_left=%s", job["chat_id"], job["attempts_left"])
            try:
                reply = generate_with_model(job["message"])
                sent = send_telegram(job["chat_id"], reply)
                if sent:
                    logger.info("Background job success for chat_id=%s", job["chat_id"])
                    _retry_q.task_done()
                    continue
                else:
                    logger.warning("Background job: telegram send failed for chat_id=%s", job["chat_id"])
            except RateLimitError as e:
                logger.warning("Background rate-limited; retry_after=%s", e.retry_after)
                with _last_rate_limit_lock:
                    _last_rate_limit_time = time.time()
                # schedule next try respecting Retry-After
                wait = e.retry_after if e.retry_after else BACKGROUND_BASE_BACKOFF
                job["attempts_left"] -= 1
                job["next_try"] = time.time() + max(1, wait)
                if job["attempts_left"] > 0:
                    _retry_q.put(job)
                else:
                    send_telegram(job["chat_id"], "در حال حاضر سرویس هوش مصنوعی در دسترس نیست. لطفاً بعداً تلاش کنید.")
            except Exception as e:
                logger.exception("Background generation failed for chat_id=%s", job["chat_id"])
                job["attempts_left"] -= 1
                if job["attempts_left"] > 0:
                    delay = BACKGROUND_BASE_BACKOFF ** (BACKGROUND_MAX_ATTEMPTS - job["attempts_left"] + 1)
                    job["next_try"] = time.time() + delay
                    _retry_q.put(job)
                else:
                    send_telegram(job["chat_id"], "متأسفم، در پردازش درخواست شما مشکلی پیش آمد. لطفاً بعداً تلاش کنید.")
        finally:
            _retry_q.task_done()


_worker_thread = threading.Thread(target=background_worker, daemon=True)
_worker_thread.start()


# -------------------- Flask endpoints --------------------
@app.route("/", methods=["GET"])
def root_get():
    return f"{APP_NAME} ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint. Expect raw update JSON from Telegram."""
    try:
        update = request.get_json(silent=True)
        logger.info("Incoming Telegram update: %s", {"update_id": update.get("update_id") if isinstance(update, dict) else None})
        if not update:
            logger.warning("Empty or invalid JSON received")
            return "bad request", 400

        message = update.get("message")
        if not message:
            # ignore non-message updates for now
            return "ok", 200

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text")
        if not chat_id or not text:
            logger.warning("Non-text or malformed message received, ignoring")
            if chat_id:
                send_telegram(chat_id, "متأسفم، فقط پیام‌های متنی را پردازش می‌کنم.")
            return "ok", 200

        # Quick circuit-breaker: if recently rate-limited globally, queue and notify user
        with _last_rate_limit_lock:
            recent_rl = (time.time() - _last_rate_limit_time) < CIRCUIT_BREAKER_PAUSE

        if recent_rl:
            queue_request_for_background(chat_id, text)
            send_telegram(chat_id, "درخواست شما صف‌بندی شد و به محض در دسترس شدن سرویس پاسخ داده خواهد شد.")
            return "ok", 200

        # Fast-path: try to get immediate response
        try:
            reply = generate_with_model(text)
            send_telegram(chat_id, reply)
            return "ok", 200
        except RateLimitError as e:
            logger.warning("Immediate call rate-limited, scheduling background retry (retry_after=%s)", e.retry_after)
            with _last_rate_limit_lock:
                _last_rate_limit_time = time.time()
            queue_request_for_background(chat_id, text)
            send_telegram(chat_id, "در حال حاضر محدودیت مصرف API وجود دارد؛ درخواست شما صف‌بندی شد و بعداً پاسخ داده می‌شود.")
            return "ok", 200
        except Exception as e:
            logger.exception("Immediate generation failed, queuing for background")
            queue_request_for_background(chat_id, text)
            send_telegram(chat_id, "درخواست شما ثبت شد و در صورت امکان پس از چند تلاش دوباره پاسخ داده خواهد شد.")
            return "ok", 200

    except Exception:
        logger.exception("Unhandled exception in webhook")
        return "server error", 500


# -------------------- CLI / entrypoint --------------------
if __name__ == "__main__":
    # When running locally, user can set TELEGRAM webhook to https://.../webhook
    logger.info("Starting %s on port %s (model=%s)", APP_NAME, PORT, AI_MODEL_ENV or "<auto>")
    app.run(host="0.0.0.0", port=PORT)
