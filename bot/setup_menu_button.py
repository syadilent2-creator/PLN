"""
Script sekali-jalan untuk mengatur Menu Button bot agar membuka Mini App.
Jalankan setelah Mini App sudah di-deploy dan punya URL HTTPS.

Install:
    pip install requests python-dotenv

Jalankan:
    python setup_menu_button.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://url-miniapp-kamu.com/index.html")

resp = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
    json={
        "menu_button": {
            "type": "web_app",
            "text": "Buka Kamera",
            "web_app": {"url": MINIAPP_URL},
        }
    },
)

print(resp.status_code, resp.json())
