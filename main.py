from flask import Flask, request
import requests, os

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

@app.route('/', methods=['GET'])
def index():
    return '✅ Bot is running!'

@app.route('/', methods=['POST'])
def webhook():
    data = request.get_json()
    print("📩 پیام دریافتی از تلگرام:", data)

    if not data or 'message' not in data or 'chat' not in data['message']:
        print("⚠️ ساختار پیام نامعتبره")
        return 'Invalid message format', 400

    chat_id = data['message']['chat']['id']
    text = data['message'].get('text', '')

    if not text:
        print("⚠️ پیام متنی دریافت نشد")
        return 'No text message', 200

    reply = ask_ai(text)
    send_message(chat_id, reply)
    return 'ok'

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": chat_id, "text": text})
    print("📤 پاسخ ارسال‌شده به تلگرام:", res.status_code, res.text)

def ask_ai(message):
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro-latest:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": message}]}
        ]
    }
    try:
        res = requests.post(url, json=payload)
        data = res.json()
        print("🔍 پاسخ خام Gemini:", data)

        if 'candidates' in data:
            return data['candidates'][0]['content']['parts'][0]['text']
        elif 'error' in data:
            return f"❌ خطا از سمت Gemini: {data['error'].get('message', 'خطای ناشناخته')}"
        else:
            return f"❌ پاسخ نامعتبر از Gemini: {data}"
    except Exception as e:
        return f"❌ خطای ارتباط با Gemini: {e}"
