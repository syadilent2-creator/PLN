"""
Daftarkan webhook Telegram ke backend Railway kamu, supaya bot bisa
menerima pesan suara & share-location langsung di chat (bukan lewat Mini App).

Jalankan SEKALI setelah backend sudah live di Railway:
    py setup_voice_webhook.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
BACKEND_URL = os.getenv("BACKEND_URL", "https://pln-production-fd9d.up.railway.app")

webhook_url = f"{BACKEND_URL.rstrip('/')}/telegram-webhook"

resp = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    json={"url": webhook_url, "allowed_updates": ["message"]},
)
print(resp.status_code, resp.json())

# Cek status webhook
info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo").json()
print("\nStatus webhook saat ini:")
print(info)
