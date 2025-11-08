from flask import Flask, request
import requests, os

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

@app.route('/', methods=['GET'])
def home():
    return '✅ Bot is running!'

@app.route('/', methods=['POST'])
def webhook():
    data = request.get_json()
    chat_id = data['message']['chat']['id']
    text = data['message']['text']

    reply = ask_ai(text)
    send_message(chat_id, reply)
    return 'ok'

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def ask_ai(message):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": message}]}
        ]
    }
    res = requests.post(url, json=payload)
    data = res.json()
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except:
        return "❌ خطا در دریافت پاسخ از هوش مصنوعی."
