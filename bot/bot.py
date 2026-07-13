"""
Backend Bot Telegram untuk Dokumentasi Kegiatan PLN
======================================================
Fungsi:
1. Menerima upload foto dari Mini App (multipart/form-data)
2. Menyimpan foto ke folder lokal: foto/{kegiatan}/{tanggal}/
3. Mengirim balik foto tersebut ke chat Telegram user
   (agar user bisa "Save to gallery" & forward ke WhatsApp langsung dari Telegram)

Install dependencies:
    pip install flask python-telegram-bot python-dotenv

Jalankan:
    python bot.py
"""

import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE_FOTO_DIR = "foto"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

KEGIATAN_LABEL = {
    "inspeksi": "Inspeksi",
    "gangguan": "Gangguan",
    "pemeliharaan": "Pemeliharaan Gardu",
    "row": "ROW",
    "pemutusan": "Pemutusan",
    "inspeksi_tiang": "Inspeksi Tiang",
    "temuan": "Temuan",
}


@app.route("/upload", methods=["POST"])
def upload():
    try:
        kegiatan = request.form.get("kegiatan", "lainnya")
        user_id = request.form.get("user_id")
        lat = request.form.get("lat", "-")
        lon = request.form.get("lon", "-")
        jalan = request.form.get("jalan", "")
        kelurahan = request.form.get("kelurahan", "")
        kecamatan = request.form.get("kecamatan", "")
        kota = request.form.get("kota", "")
        photo = request.files.get("photo")

        if not photo:
            return jsonify({"status": "error", "message": "Foto tidak ditemukan"}), 400
        if not user_id:
            return jsonify({"status": "error", "message": "user_id tidak ditemukan"}), 400

        now = datetime.now()
        tanggal_folder = now.strftime("%Y-%m-%d")
        waktu_file = now.strftime("%H%M%S")

        # 1. Simpan ke folder lokal berdasarkan kegiatan & tanggal
        folder = os.path.join(BASE_FOTO_DIR, kegiatan, tanggal_folder)
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, f"{waktu_file}_{user_id}.jpg")
        photo.save(filepath)
        logger.info(f"Foto disimpan: {filepath}")

        # 2. Kirim balik foto ke chat Telegram user
        label = KEGIATAN_LABEL.get(kegiatan, kegiatan)
        alamat = ", ".join(filter(None, [jalan, kelurahan]))
        wilayah = ", ".join(filter(None, [f"Kec. {kecamatan}" if kecamatan else "", kota]))
        caption_lines = [label, f"{now.strftime('%d %b %Y, %H:%M')} WIB"]
        if alamat:
            caption_lines.append(alamat)
        if wilayah:
            caption_lines.append(wilayah)
        caption_lines.append(f"Koordinat: {lat}, {lon}")
        caption = "\n".join(caption_lines)
        kirim_foto_ke_telegram(user_id, filepath, caption)

        return jsonify({"status": "ok", "path": filepath})

    except Exception as e:
        logger.exception("Upload gagal")
        return jsonify({"status": "error", "message": str(e)}), 500


def kirim_foto_ke_telegram(chat_id: str, filepath: str, caption: str):
    """Kirim foto ke chat user via Telegram Bot API."""
    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": f},
            timeout=30,
        )
    if not resp.ok:
        logger.error(f"Gagal kirim ke Telegram: {resp.text}")


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    # Untuk production, gunakan gunicorn/uwsgi + reverse proxy HTTPS (nginx / Caddy)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
