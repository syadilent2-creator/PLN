"""
Backend Bot Telegram untuk Dokumentasi Kegiatan PLN
======================================================
Fungsi:
1. Menerima upload foto dari Mini App (multipart/form-data) -> endpoint /upload
2. [BARU] Menerima pesan suara (voice note) langsung di chat -> endpoint /telegram-webhook
   - Transkrip suara jadi teks (OpenAI Whisper)
   - Ekstrak kegiatan, deskripsi, material & jumlah (OpenAI GPT, JSON mode)
   - Cari baris kosong di Google Sheet, isi otomatis
   - Balas ke user dengan ringkasan (Hari, Tanggal, Lokasi, Kegiatan, Deskripsi, Material)
3. [BARU] Menerima share-location -> dipakai sebagai "lokasi terakhir" untuk laporan VN berikutnya

Install dependencies:
    pip install -r requirements.txt

Jalankan:
    python bot.py
"""

import io
import os
import json
import logging
import tempfile
import threading
import base64
import random
import string
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE_FOTO_DIR = "foto"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1vXFzN8nktmBmHUzN9KqkpaIBhHUSmoaXCXQOMb2Q2MY")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # isi mentah file JSON service account

# ID folder Google Drive tujuan simpan foto (opsional, isi via env var)
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")

# Scope Sheets + Drive (dibutuhkan untuk upload foto ke Google Drive)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TZ = ZoneInfo("Asia/Jakarta")
HARI_ID = ["SENIN", "SELASA", "RABU", "KAMIS", "JUM'AT", "SABTU", "MINGGU"]

# --- Konfigurasi watermark foto ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LOGO_ICON_PATH = os.path.join(BASE_DIR, "assets", "logo_pln_icon.png")
ULP_NAME = os.getenv("ULP_NAME", "ULP Benubenua")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Cache lokasi terakhir per user, in-memory: { user_id: {"lat":.., "lon":.., "nama":.., "waktu": datetime} }
LAST_LOCATION = {}

# Cache untuk fitur "reply dengan foto -> masuk kolom Gambar":
# - ROW_BY_MESSAGE: { message_id balasan ringkasan bot : nomor baris di sheet }
#   Dipakai kalau user reply LANGSUNG ke pesan ringkasan laporan tsb dengan foto.
# - LAST_ROW_BY_USER: { user_id : nomor baris terakhir yang ditulis }
#   Fallback kalau user kirim foto tanpa reply spesifik (dianggap untuk laporan terakhirnya).
ROW_BY_MESSAGE = {}
LAST_ROW_BY_USER = {}

KEGIATAN_LABEL = {
    "emergency": "EMERGENCY",
    "inspeksi_gardu": "INSPEKSI GARDU",
    "pemeliharaan": "PEMELIHARAAN",
    "row": "ROW",
    "inspeksi_jtm": "INSPEKSI JTM",
}

# --- Label tombol keyboard persisten (share lokasi + mulai kegiatan baru) ---
TOMBOL_LOKASI = "\U0001F4CD Bagikan Lokasi Saya"
TOMBOL_MULAI_ULANG = "\U0001F504 Mulai Kegiatan Baru"


def main_keyboard() -> dict:
    """Keyboard persisten (selalu tampil di chat) berisi tombol share-lokasi 1-tap
    dan tombol untuk mulai mencatat kegiatan baru kapan saja."""
    return {
        "keyboard": [
            [{"text": TOMBOL_LOKASI, "request_location": True}],
            [{"text": TOMBOL_MULAI_ULANG}],
        ],
        "resize_keyboard": True,
    }


# ======================================================================
# 1. UPLOAD FOTO DARI MINI APP (fitur lama, tidak berubah)
# ======================================================================

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
        provinsi = request.form.get("provinsi", "")
        photo = request.files.get("photo")

        if not photo:
            return jsonify({"status": "error", "message": "Foto tidak ditemukan"}), 400
        if not user_id:
            return jsonify({"status": "error", "message": "user_id tidak ditemukan"}), 400

        now = datetime.now(TZ)
        hari = HARI_ID[now.weekday()]
        tanggal_folder = now.strftime("%Y-%m-%d")
        waktu_file = now.strftime("%H%M%S")

        folder = os.path.join(BASE_FOTO_DIR, kegiatan, tanggal_folder)
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, f"{waktu_file}_{user_id}.jpg")
        photo.save(filepath)
        logger.info(f"Foto disimpan: {filepath}")

        # Tempel watermark PLN (logo, ULP, jam/hari/tanggal, daerah, koordinat) ke foto
        try:
            tambah_watermark(
                filepath, now=now, hari=hari, lat=lat, lon=lon,
                kecamatan=kecamatan, kota=kota, provinsi=provinsi,
            )
        except Exception:
            logger.exception("Gagal menambahkan watermark, foto tetap dikirim tanpa watermark")

        label = KEGIATAN_LABEL.get(kegiatan, kegiatan)
        alamat = ", ".join(filter(None, [jalan, kelurahan]))
        wilayah = ", ".join(filter(None, [f"Kec. {kecamatan}" if kecamatan else "", kota]))
        
        # Simpan lokasi terbaru ke LAST_LOCATION agar dipakai untuk VN/teks Telegram berikutnya
        nama_lokasi = ", ".join(filter(None, [alamat, wilayah])) or "Lokasi tidak diketahui"
        try:
            u_id = int(user_id) if str(user_id).isdigit() else user_id
            LAST_LOCATION[u_id] = {
                "lat": lat,
                "lon": lon,
                "nama": nama_lokasi,
                "waktu": now
            }
            LAST_LOCATION[str(user_id)] = LAST_LOCATION[u_id]
            logger.info(f"Lokasi user {user_id} disimpan dari Mini App: {nama_lokasi}")
        except Exception:
            logger.exception("Gagal menyimpan lokasi dari Mini App")

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
    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": f},
            timeout=30,
        )
    if not resp.ok:
        logger.error(f"Gagal kirim ke Telegram: {resp.text}")


def _font(name: str, size: int):
    """Load font dari folder fonts/. Fallback ke font bawaan PIL kalau file tidak ada."""
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def _generate_kode_foto(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def tambah_watermark(photo_path: str, now: datetime, hari: str, lat, lon,
                      kecamatan: str = "", kota: str = "", provinsi: str = "") -> str:
    """Tempel watermark PLN (logo, nama ULP, jam/hari/tanggal, daerah, koordinat)
    langsung ke pixel foto (bukan cuma caption Telegram). Overwrite file di photo_path.
    Return kode_foto yang di-generate (untuk referensi/log kalau perlu)."""
    base = Image.open(photo_path).convert("RGBA")
    W, H = base.size

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Gradient gelap di bagian bawah biar teks kebaca jelas di foto apapun
    band_h = int(H * 0.34)
    grad = Image.new("L", (1, band_h), color=0)
    for i in range(band_h):
        grad.putpixel((0, i), int(220 * (i / band_h)))
    grad = grad.resize((W, band_h))
    black = Image.new("RGBA", (W, band_h), (0, 0, 0, 255))
    black.putalpha(grad)
    overlay.paste(black, (0, H - band_h), black)

    pad = int(W * 0.045)
    y = H - band_h + pad

    # Logo + teks "PLN"
    logo_size = int(W * 0.11)
    try:
        logo = Image.open(LOGO_ICON_PATH).convert("RGBA").resize((logo_size, logo_size))
        overlay.paste(logo, (pad, y), logo)
    except Exception:
        logger.warning(f"Logo watermark tidak ditemukan di {LOGO_ICON_PATH}")

    f_pln = _font("DejaVuSans-Bold.ttf", int(W * 0.065))
    draw.text((pad + logo_size + int(W * 0.025), y + logo_size // 2 - int(W * 0.033)),
              "PLN", font=f_pln, fill=(255, 255, 255, 255))
    y += logo_size + int(H * 0.02)

    # Nama ULP
    f_ulp = _font("DejaVuSans-Bold.ttf", int(W * 0.05))
    draw.text((pad, y), ULP_NAME.upper(), font=f_ulp, fill=(255, 255, 255, 255))
    y += int(W * 0.08)

    # Jam | tanggal + hari
    f_time = _font("DejaVuSans-Bold.ttf", int(W * 0.085))
    f_date = _font("DejaVuSans.ttf", int(W * 0.045))
    waktu = now.strftime("%H:%M")
    tanggal = now.strftime("%d %B %Y")
    draw.text((pad, y), waktu, font=f_time, fill=(255, 255, 255, 255))
    tw = draw.textlength(waktu, font=f_time)
    lx = pad + tw + int(W * 0.025)
    draw.line([(lx, y + int(W * 0.005)), (lx, y + int(W * 0.08))], fill=(255, 255, 255, 160), width=3)
    draw.text((lx + int(W * 0.02), y + int(W * 0.005)), tanggal, font=f_date, fill=(255, 255, 255, 255))
    draw.text((lx + int(W * 0.02), y + int(W * 0.005) + int(W * 0.05)), hari, font=f_date, fill=(255, 255, 255, 230))
    y += int(W * 0.14)

    # Daerah (kecamatan/kabupaten, provinsi)
    bagian = [b for b in [kecamatan, kota, provinsi] if b]
    daerah = ", ".join(bagian) if bagian else "Lokasi tidak diketahui"
    f_daerah = _font("DejaVuSans.ttf", int(W * 0.04))
    draw.text((pad, y), daerah, font=f_daerah, fill=(255, 255, 255, 255))
    y += int(W * 0.062)

    # Koordinat
    f_koor = _font("DejaVuSans.ttf", int(W * 0.033))
    draw.text((pad, y), f"Koordinat: {lat}, {lon}", font=f_koor, fill=(230, 230, 230, 255))

    # Kode foto (referensi unik, pojok kanan bawah)
    kode = _generate_kode_foto()
    f_kode = _font("DejaVuSans.ttf", int(W * 0.026))
    kode_txt = f"Kode Foto: {kode}"
    kw = draw.textlength(kode_txt, font=f_kode)
    draw.text((W - kw - pad, H - pad // 2 - int(W * 0.03)), kode_txt, font=f_kode, fill=(255, 255, 255, 190))

    hasil = Image.alpha_composite(base, overlay).convert("RGB")
    hasil.save(photo_path, quality=92)
    return kode


# ======================================================================
# 2. [BARU] WEBHOOK TELEGRAM - tangkap voice note & share-location
# ======================================================================

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Telegram akan POST setiap update (pesan baru) ke sini."""
    update = request.get_json(silent=True) or {}
    msg = update.get("message")

    if not msg:
        return jsonify({"ok": True})  # abaikan update jenis lain

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]

    # --- Share-location: simpan sebagai lokasi terakhir user ini ---
    if "location" in msg:
        loc = msg["location"]
        threading.Thread(target=proses_location, args=(user_id, chat_id, loc)).start()
        return jsonify({"ok": True})

    # --- Foto (biasanya sebagai reply ke ringkasan laporan): masuk ke kolom Gambar ---
    if "photo" in msg:
        # Telegram kirim beberapa resolusi, ambil yang paling besar (elemen terakhir)
        file_id = msg["photo"][-1]["file_id"]
        reply_to = msg.get("reply_to_message") or {}
        reply_msg_id = reply_to.get("message_id")
        threading.Thread(
            target=proses_foto_laporan, args=(user_id, chat_id, file_id, reply_msg_id)
        ).start()
        return jsonify({"ok": True})

    # --- Voice note: proses transkrip + ekstraksi + tulis ke sheet ---
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        # Balas cepat dulu biar Telegram tidak timeout/retry,
        # proses berat dikerjakan di background thread.
        kirim_pesan(chat_id, "Menerima laporan suara, sedang diproses...")
        threading.Thread(target=proses_voice_note, args=(user_id, chat_id, file_id)).start()
        return jsonify({"ok": True})

    # --- Teks biasa (bukan command): anggap laporan yang diketik manual ---
    if "text" in msg:
        teks = msg["text"].strip()

        # Tombol "Mulai Kegiatan Baru" (dikirim sebagai teks biasa oleh keyboard Telegram)
        if teks == TOMBOL_MULAI_ULANG:
            LAST_ROW_BY_USER.pop(user_id, None)
            kirim_pesan(
                chat_id,
                "Oke, siap menerima laporan kegiatan baru \u2705\n\n"
                "Kirim *pesan suara* atau *ketik teks* laporannya sekarang.",
                reply_markup=main_keyboard(),
            )
            return jsonify({"ok": True})

        if teks.startswith("/"):
            if teks == "/start":
                kirim_pesan(
                    chat_id,
                    "Halo! Kirim *pesan suara* ATAU *ketik teks* untuk mencatat laporan kegiatan.\n\n"
                    "Contoh ketik: \"Inspeksi gardu, cek kondisi trafo aman, "
                    "ganti isolator 3 buah\"\n\n"
                    "Sebelum mulai (sekali per shift), tap tombol \U0001F4CD di bawah untuk "
                    "membagikan lokasi kerja kamu, supaya laporan otomatis mencantumkan lokasi.\n\n"
                    "Kalau mau memulai catatan kegiatan yang baru kapan saja, tap tombol "
                    "\U0001F504 Mulai Kegiatan Baru.",
                    reply_markup=main_keyboard(),
                )
            return jsonify({"ok": True})

        kirim_pesan(chat_id, "Menerima laporan teks, sedang diproses...")
        threading.Thread(target=proses_laporan_teks, args=(user_id, chat_id, teks)).start()
        return jsonify({"ok": True})

    return jsonify({"ok": True})


def proses_location(user_id, chat_id, loc):
    lat, lon = loc["latitude"], loc["longitude"]
    nama = "Lokasi tidak diketahui"
    try:
        res = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "accept-language": "id", "zoom": 18},
            headers={"User-Agent": "pln-rpa-bot"},
            timeout=10,
        )
        addr = res.json().get("address", {})
        bagian = [
            addr.get("road") or addr.get("pedestrian"),
            addr.get("village") or addr.get("suburb"),
            f"Kec. {addr.get('city_district')}" if addr.get("city_district") else None,
            addr.get("city") or addr.get("county"),
        ]
        nama = ", ".join(filter(None, bagian)) or nama
    except Exception:
        logger.exception("Gagal reverse geocode lokasi")

    LAST_LOCATION[user_id] = {"lat": lat, "lon": lon, "nama": nama, "waktu": datetime.now(TZ)}
    kirim_pesan(
        chat_id,
        f"Lokasi tersimpan: {nama}\nAkan dipakai otomatis untuk laporan-laporan berikutnya.",
        reply_markup=main_keyboard(),
    )


def proses_voice_note(user_id, chat_id, file_id):
    try:
        if not GEMINI_API_KEY:
            kirim_pesan(chat_id, "GEMINI_API_KEY belum diset di server. Hubungi admin bot.")
            return

        # 1. Download file voice dari Telegram
        audio_path = download_telegram_file(file_id)

        # 2. Transkrip suara -> teks (Gemini)
        with open(audio_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode("utf-8")
        os.remove(audio_path)

        payload = {
            "contents": [{
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "audio/ogg",
                            "data": audio_data
                        }
                    },
                    {
                        "text": "Transkripsikan rekaman suara ini ke dalam teks bahasa Indonesia secara lengkap dan akurat. Jangan tambahkan komentar lain."
                    }
                ]
            }]
        }

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
        headers = {"x-goog-api-key": GEMINI_API_KEY}
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        res_json = res.json()
        teks = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()

        if not teks:
            kirim_pesan(chat_id, "Maaf, suara tidak bisa dikenali. Coba rekam ulang lebih jelas.")
            return

        proses_dan_simpan_laporan(user_id, chat_id, teks, sumber="suara")

    except Exception as e:
        logger.exception("Gagal proses voice note")
        kirim_pesan(chat_id, f"Gagal memproses laporan: {e}")


def proses_laporan_teks(user_id, chat_id, teks):
    try:
        if not GEMINI_API_KEY:
            kirim_pesan(chat_id, "GEMINI_API_KEY belum diset di server. Hubungi admin bot.")
            return
        proses_dan_simpan_laporan(user_id, chat_id, teks, sumber="teks")
    except Exception as e:
        logger.exception("Gagal proses laporan teks")
        kirim_pesan(chat_id, f"Gagal memproses laporan: {e}")


def proses_dan_simpan_laporan(user_id, chat_id, teks: str, sumber: str):
    """Inti bersama: ekstraksi AI -> tulis ke sheet -> balas ke user.
    Dipakai baik untuk laporan dari voice note maupun teks ketikan."""

    # 1. Ekstraksi terstruktur -> JSON (GPT), untuk ambil kegiatan & material saja
    data = ekstrak_laporan(teks)
    # Deskripsi selalu pakai teks lengkap apa adanya (bukan potongan hasil AI)
    data["deskripsi"] = teks

    # 2. Ambil lokasi terakhir user (dari share-location / Mini App) SEBELUM tulis ke sheet,
    #    supaya kolom Lokasi langsung terisi nama daerah + titik koordinat.
    u_id = int(user_id) if str(user_id).isdigit() else user_id
    loc = LAST_LOCATION.get(u_id) or LAST_LOCATION.get(str(u_id))
    lokasi_teks = loc["nama"] if loc else "(belum ada lokasi)"

    # 3. Tulis ke Google Sheet
    now = datetime.now(TZ)
    hari = HARI_ID[now.weekday()]
    tanggal = now.strftime("%d-%m-%Y")
    baris = tulis_ke_sheet(hari, tanggal, data, loc=loc)

    # Simpan baris ini sebagai "laporan terakhir" user, dipakai kalau nanti user
    # reply pesan ringkasan di bawah ini dengan foto -> foto masuk ke baris yang sama.
    LAST_ROW_BY_USER[user_id] = baris

    # 4. Balas ringkasan ke user
    material_teks = "\n".join(
        f"  \u2022 {m['nama']} - {m['jumlah']}" for m in data.get("material", [])
    ) or "  -"

    sumber_teks = "_Dari pesan suara_" if sumber == "suara" else "_Dari teks ketikan_"
    balasan = (
        f"*Laporan tersimpan* (baris {baris})\n\n"
        f"Hari/Tanggal: {hari}, {tanggal}\n"
        f"Lokasi: {lokasi_teks}\n"
        f"Kegiatan: {data.get('kegiatan', '-')}\n"
        f"Deskripsi: {data.get('deskripsi', '-')}\n"
        f"Material:\n{material_teks}\n\n"
        f"{sumber_teks}\n\n"
        f"\U0001F4F7 _Balas (reply) pesan ini dengan foto kegiatan untuk mengisi kolom Gambar._"
    )
    hasil_kirim = kirim_pesan(chat_id, balasan)

    # Catat message_id balasan ini -> baris, supaya kalau user reply pesan ini dengan
    # foto, kita tahu persis baris mana yang harus diisi kolom Gambar-nya.
    try:
        sent_message_id = hasil_kirim.json()["result"]["message_id"]
        ROW_BY_MESSAGE[sent_message_id] = baris
    except Exception:
        logger.exception("Gagal mencatat message_id -> baris untuk fitur reply foto")

    # Kalau lokasi belum tercatat, langsung susulkan tombol share-location 1-tap
    if not loc:
        kirim_tombol_minta_lokasi(chat_id)


def download_telegram_file(file_id: str, suffix: str = ".oga") -> str:
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    # Pakai ekstensi asli dari Telegram kalau ada, biar file foto/audio konsisten
    ext = os.path.splitext(file_path)[1] or suffix
    fd, local_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    with requests.get(file_url, stream=True, timeout=30) as resp:
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    return local_path


# ======================================================================
# 3. FOTO SEBAGAI REPLY -> UPLOAD KE GOOGLE DRIVE -> EMBED KE CELL
# ======================================================================
# Foto di-upload ke Google Drive menggunakan service account (persistent,
# tidak bergantung filesystem Railway yang ephemeral).
# Gambar disisipkan langsung ke dalam cell kolom I menggunakan Sheets API
# batchUpdate dengan tipe `image` — otomatis menyesuaikan ukuran cell.


def get_google_creds() -> Credentials:
    """Credentials dengan scope Sheets + Drive."""
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SCOPES)


def upload_ke_drive(foto_bytes: bytes, nama_file: str) -> str:
    """
    Upload bytes foto ke Google Drive menggunakan service account.
    File dibuat publik (anyone reader) agar bisa dirender oleh Sheets API.
    Kembalikan URL langsung gambar.
    """
    creds     = get_google_creds()
    drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    file_meta = {"name": nama_file, "mimeType": "image/jpeg"}
    if DRIVE_FOLDER_ID:
        file_meta["parents"] = [DRIVE_FOLDER_ID]

    media  = MediaIoBaseUpload(io.BytesIO(foto_bytes), mimetype="image/jpeg", resumable=False)
    result = drive_svc.files().create(
        body=file_meta, media_body=media, fields="id"
    ).execute()
    file_id = result["id"]

    # Izinkan siapa saja melihat file
    drive_svc.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    url = f"https://drive.google.com/uc?export=view&id={file_id}"
    logger.info(f"Foto diunggah ke Drive: {url}")
    return url


def sisipkan_gambar_ke_cell(baris: int, foto_url: str):
    """
    Sisipkan gambar LANGSUNG ke dalam cell I{baris} menggunakan Sheets API batchUpdate.
    Tipe `image` pada userEnteredValue membuat gambar tampil di dalam cell
    dan otomatis menyesuaikan ukuran cell (bukan overlay, bukan formula).
    """
    creds      = get_google_creds()
    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Ambil sheetId berdasarkan SHEET_NAME
    meta = sheets_svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in meta["sheets"]
        if s["properties"]["title"] == SHEET_NAME
    )

    body = {
        "requests": [{
            "updateCells": {
                "rows": [{
                    "values": [{
                        "userEnteredValue": {
                            "image": {
                                "sourceUri": foto_url,
                                "altText":   "Foto lapangan"
                            }
                        }
                    }]
                }],
                "fields": "userEnteredValue.image",
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    baris - 1,   # 0-indexed
                    "endRowIndex":      baris,
                    "startColumnIndex": 8,            # kolom I (0-indexed)
                    "endColumnIndex":   9
                }
            }
        }]
    }

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body=body
    ).execute()
    logger.info(f"Gambar berhasil disisipkan ke cell I{baris}")


def proses_foto_laporan(user_id, chat_id, file_id, reply_msg_id):
    """
    Handle foto yang dikirim user (biasanya sebagai reply ke ringkasan laporan).
    Alur:
      1. Download bytes foto dari Telegram
      2. Upload ke Google Drive (persistent, publik)
      3. Sisipkan gambar langsung ke cell I pada baris yang sesuai
         menggunakan Sheets API — gambar menyesuaikan ukuran cell otomatis.
    """
    local_bytes = None
    try:
        # Tentukan baris tujuan: prioritas baris dari pesan yang di-reply,
        # fallback ke laporan terakhir user ini kalau reply tidak spesifik/tidak ada.
        target_row = ROW_BY_MESSAGE.get(reply_msg_id) if reply_msg_id else None
        if target_row is None:
            target_row = LAST_ROW_BY_USER.get(user_id)

        if target_row is None:
            kirim_pesan(
                chat_id,
                "Belum ada laporan (teks/suara) untuk dikaitkan dengan foto ini. "
                "Kirim laporan kegiatan dulu, baru reply pesan ringkasannya dengan foto.",
            )
            return

        kirim_pesan(chat_id, f"📤 Menerima foto, sedang diunggah ke Drive...")

        # 1. Download bytes dari Telegram
        local_path = download_telegram_file(file_id, suffix=".jpg")
        with open(local_path, "rb") as f:
            local_bytes = f.read()
        os.remove(local_path)

        # 2. Upload ke Google Drive
        nama_file = f"baris{target_row}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
        foto_url  = upload_ke_drive(local_bytes, nama_file)

        # 3. Sisipkan gambar ke cell I (bukan formula, tapi nilai gambar langsung)
        sisipkan_gambar_ke_cell(target_row, foto_url)

        kirim_pesan(chat_id, f"🖼️ Foto berhasil disisipkan ke kolom Gambar, baris {target_row}.")

    except Exception as e:
        logger.exception("Gagal proses foto laporan")
        kirim_pesan(chat_id, f"❌ Gagal menyimpan foto: {e}")


def ekstrak_laporan(teks: str) -> dict:
    """Minta Gemini mengubah transkrip bebas jadi field terstruktur."""
    kategori = ", ".join(KEGIATAN_LABEL.values())
    prompt = f"""Kamu adalah asisten pencatatan laporan lapangan PLN yang sangat teliti, selayaknya petugas admin manusia yang membaca laporan dari VN (voice note) maupun teks ketikan lalu memindahkannya ke tabel Excel/Spreadsheet. Tugasmu mengekstrak teks laporan menjadi JSON terstruktur berisi "kegiatan" dan "material" saja.

========================================
ATURAN 1 - MENENTUKAN "kegiatan"
========================================
Baca keseluruhan konteks laporan (bukan cuma kata pertama), lalu cocokkan ke SALAH SATU dari 5 kategori resmi berikut (tulis persis sama, huruf besar semua):

- EMERGENCY
  Ciri-ciri: gangguan mendadak/darurat, padam tiba-tiba, jaringan putus/roboh akibat pohon tumbang/longsor/kecelakaan, kebakaran, perbaikan darurat di luar jadwal rutin.

- INSPEKSI GARDU
  Ciri-ciri: mengecek/memeriksa/patroli kondisi GARDU DISTRIBUSI, trafo, PHPTR (Panel Hubung Bagi Tegangan Rendah), kubikel, box gardu. Kata kunci: "inspeksi gardu", "cek gardu", "cek trafo", "kondisi gardu".

- PEMELIHARAAN
  Ciri-ciri: kegiatan perawatan/pemeliharaan terjadwal (preventif), mengganti komponen yang aus/rusak secara rutin (bukan darurat), membersihkan, mengencangkan baut/klem, mengganti fuse/isolator/komponen sebagai bagian pemeliharaan berkala. Kata kunci: "pemeliharaan", "perawatan", "penggantian rutin".

- ROW
  Ciri-ciri: Right of Way — pemangkasan/penebangan pohon atau vegetasi yang mendekati/mengganggu jaringan listrik, pembersihan jalur/lintasan kabel. Kata kunci: "ROW", "pemangkasan pohon", "vegetasi", "penebangan".

- INSPEKSI JTM
  Ciri-ciri: mengecek/memeriksa/patroli kondisi JARINGAN TEGANGAN MENENGAH (JTM) di LUAR gardu — tiang listrik, kawat/konduktor, isolator di jaringan, andongan kawat. Kata kunci: "inspeksi JTM", "patroli jaringan", "cek tiang", "cek jaringan".

Jika laporan menyebut kombinasi (misal inspeksi SEKALIGUS ganti komponen), pilih kategori berdasarkan TUJUAN UTAMA kunjungan (inspeksi rutin gardu yang berujung ganti komponen kecil tetap INSPEKSI GARDU; sedangkan penggantian terjadwal skala pemeliharaan masuk PEMELIHARAAN).

========================================
ATURAN 2 - MENENTUKAN "material" dan "jumlah"
========================================
Seperti manusia yang membaca laporan, kenali material & jumlahnya dari KATA KUNCI berikut yang biasa muncul di VN maupun teks ketikan (tidak harus selalu di kalimat ketiga, bisa di mana saja):
  - Kata kerja penanda material dipakai: "ganti"/"mengganti"/"penggantian", "pasang"/"memasang"/"pemasangan", "gunakan"/"menggunakan", "tambah"/"menambahkan".
  - Kata penanda jumlah: "sebanyak", "jumlah", angka + satuan langsung setelah nama barang (contoh: "isolator 3 buah", "fuse 2 pcs", "kabel 5 meter").
Ambil PASANGAN nama barang + jumlah + satuannya secara utuh. Jika ada beberapa material dalam satu laporan, masukkan semua sebagai item terpisah dalam array "material". Jika jumlah tidak disebutkan eksplisit, isi jumlah dengan "-".

Contoh Ekstraksi:
Teks: "Inspeksi gardu, cek kondisi trafo aman, ganti isolator 3 buah"
Output JSON:
{{
  "kegiatan": "INSPEKSI GARDU",
  "material": [
    {{
      "nama": "isolator",
      "jumlah": "3 buah"
    }}
  ]
}}

Teks: "Melakukan pemeliharaan pada PHPTR dengan mengganti NH fuse sebanyak 3 buah dan menggunakan kabel schoen 2 pcs"
Output JSON:
{{
  "kegiatan": "PEMELIHARAAN",
  "material": [
    {{"nama": "NH fuse", "jumlah": "3 buah"}},
    {{"nama": "kabel schoen", "jumlah": "2 pcs"}}
  ]
}}

Teks Laporan: \"\"\"{teks}\"\"\"

Balas HANYA dengan JSON valid tanpa formatting markdown seperti ```json atau penjelas lainnya. Format JSON harus persis seperti ini:
{{
  "kegiatan": "PILIH SALAH SATU: EMERGENCY / INSPEKSI GARDU / PEMELIHARAAN / ROW / INSPEKSI JTM",
  "material": [
    {{
      "nama": "nama material yang disebutkan",
      "jumlah": "jumlah + satuan"
    }}
  ]
}}
Jika tidak ada material yang digunakan, isi "material": []."""

    payload = {
        "contents": [{
            "parts": [
                {
                    "text": prompt
                }
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    res = None
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
        headers = {"x-goog-api-key": GEMINI_API_KEY}
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        res_json = res.json()
        content = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Bersihkan pembungkus markdown ```json ... ``` jika ada
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
            
        return json.loads(content)
    except requests.HTTPError:
        # Log body respons Gemini biar kelihatan pesan error aslinya (mis. 400/404 karena
        # endpoint/model/API key salah), bukan cuma "gagal parse JSON"
        body = res.text if res is not None else "(tidak ada respons)"
        status = res.status_code if res is not None else "?"
        logger.error(f"Gemini API error {status}: {body}")
        return {"kegiatan": "", "deskripsi": teks, "material": []}
    except Exception:
        logger.exception("Gagal parse JSON dari Gemini")
        return {"kegiatan": "", "deskripsi": teks, "material": []}


def get_sheet():
    creds = get_google_creds()
    gc    = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def format_lokasi(loc: Optional[dict]) -> str:
    """Gabungkan nama daerah + titik koordinat jadi satu teks untuk kolom Lokasi.
    Contoh: 'Jl. Ahmad Yani, Kec. Sawahan, Surabaya (-7.290000, 112.730000)'"""
    if not loc:
        return ""
    nama = loc.get("nama") or "Lokasi tidak diketahui"
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is None or lon is None:
        return nama
    return f"{nama} ({lat}, {lon})"


def tulis_ke_sheet(hari: str, tanggal: str, data: dict, loc: Optional[dict] = None) -> int:
    """Cari baris pertama yang kolom Deskripsi (E) masih kosong, isi di situ.
    Kembalikan nomor baris yang ditulis."""
    ws = get_sheet()
    semua = ws.get_all_values()  # termasuk header di baris 1

    target_row = None
    for i, row in enumerate(semua[1:], start=2):  # mulai dari baris 2
        deskripsi_cell = row[4] if len(row) > 4 else ""
        if not deskripsi_cell.strip():
            target_row = i
            break
    if target_row is None:
        target_row = len(semua) + 1  # kalau semua penuh, tambah baris baru di akhir

    existing = semua[target_row - 1] if target_row - 1 < len(semua) else []

    def cell(idx):
        return existing[idx].strip() if idx < len(existing) else ""

    no_val = cell(0) or str(target_row - 1)
    hari_val = cell(1) or hari
    tanggal_val = cell(2) or tanggal
    kegiatan_val = cell(3) or data.get("kegiatan", "")

    material_list = data.get("material", [])
    material_val = "\n".join(m.get("nama", "") for m in material_list)
    jumlah_val = "\n".join(m.get("jumlah", "") for m in material_list)
    lokasi_val = cell(7) or format_lokasi(loc)

    ws.update(
        f"A{target_row}:H{target_row}",
        [[no_val, hari_val, tanggal_val, kegiatan_val, data.get("deskripsi", ""),
          material_val, jumlah_val, lokasi_val]],
    )
    return target_row


def kirim_pesan(chat_id, teks: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": teks, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)


def kirim_tombol_minta_lokasi(chat_id):
    """Ingatkan user share lokasi kalau belum tercatat untuk shift ini.
    Tombolnya sama dengan keyboard persisten (main_keyboard) supaya tombol
    'Mulai Kegiatan Baru' tetap ada juga."""
    kirim_pesan(
        chat_id,
        "Lokasi kerja belum tercatat untuk shift ini. Tap tombol \U0001F4CD di bawah untuk "
        "membagikan lokasi (cukup sekali per shift, lokasi ini akan dipakai otomatis "
        "untuk laporan-laporan berikutnya).",
        reply_markup=main_keyboard(),
    )


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
