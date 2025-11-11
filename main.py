from flask import Flask, request
import requests, os

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

if not TELEGRAM_TOKEN or not AI_API_KEY:
    raise ValueError("توکن تلگرام یا کلید API هوش مصنوعی تنظیم نشده‌اند!")

@app.route('/', methods=['GET'])
def index():
    return '✅ Bot is running!'

@app.route('/', methods=['POST'])
def webhook():
    data = request.get_json()
    print("📩 پیام دریافتی از تلگرام:", data)

    if not data or 'message' not in data or 'text' not in data['message'] or 'chat' not in data['message']:
        print("⚠️ ساختار پیام نامعتبره یا پیام متنی نیست")
        return 'Invalid message format', 400

    chat_id = data['message']['chat']['id']
    text = data['message']['text']

    reply = ask_ai(text)
    send_message(chat_id, reply)
    return 'ok'

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        if res.status_code != 200:
            print(f"❌ خطا در ارسال پیام به تلگرام: {res.status_code} - {res.text}")
        else:
            print("📤 پاسخ ارسال‌شده به تلگرام:", res.status_code, res.text)
    except Exception as e:
        print(f"❌ خطا در ارسال پیام به تلگرام: {e}")

def ask_ai(message):
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": message}]}
        ]
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
