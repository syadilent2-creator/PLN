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

import os
import json
import logging
import tempfile
import threading
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE_FOTO_DIR = "foto"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1vXFzN8nktmBmHUzN9KqkpaIBhHUSmoaXCXQOMb2Q2MY")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # isi mentah file JSON service account

TZ = ZoneInfo("Asia/Jakarta")
HARI_ID = ["SENIN", "SELASA", "RABU", "KAMIS", "JUM'AT", "SABTU", "MINGGU"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Cache lokasi terakhir per user, in-memory: { user_id: {"lat":.., "lon":.., "nama":.., "waktu": datetime} }
LAST_LOCATION = {}

KEGIATAN_LABEL = {
    "emergency": "EMERGENCY",
    "inspeksi_gardu": "INSPEKSI GARDU",
    "pemeliharaan": "PEMELIHARAAN",
    "row": "ROW",
    "inspeksi_jtm": "INSPEKSI JTM",
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
        photo = request.files.get("photo")

        if not photo:
            return jsonify({"status": "error", "message": "Foto tidak ditemukan"}), 400
        if not user_id:
            return jsonify({"status": "error", "message": "user_id tidak ditemukan"}), 400

        now = datetime.now(TZ)
        tanggal_folder = now.strftime("%Y-%m-%d")
        waktu_file = now.strftime("%H%M%S")

        folder = os.path.join(BASE_FOTO_DIR, kegiatan, tanggal_folder)
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, f"{waktu_file}_{user_id}.jpg")
        photo.save(filepath)
        logger.info(f"Foto disimpan: {filepath}")

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
        if teks.startswith("/"):
            if teks == "/start":
                kirim_pesan(
                    chat_id,
                    "Halo! Kirim *pesan suara* ATAU *ketik teks* untuk mencatat laporan kegiatan.\n\n"
                    "Contoh ketik: \"Inspeksi gardu, cek kondisi trafo aman, "
                    "ganti isolator 3 buah\"\n\n"
                    "Sebelum mulai (sekali per shift), share lokasi kamu lewat "
                    "\U0001F4CE > Location, supaya laporan mencantumkan lokasi kerja."
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
    kirim_pesan(chat_id, f"Lokasi tersimpan: {nama}\nAkan dipakai untuk laporan suara berikutnya.")


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

        url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent"
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

    # 1. Ekstraksi terstruktur -> JSON (GPT)
    data = ekstrak_laporan(teks)

    # 2. Tulis ke Google Sheet
    now = datetime.now(TZ)
    hari = HARI_ID[now.weekday()]
    tanggal = now.strftime("%d-%m-%Y")
    baris = tulis_ke_sheet(hari, tanggal, data)

    # 3. Balas ringkasan ke user
    u_id = int(user_id) if str(user_id).isdigit() else user_id
    loc = LAST_LOCATION.get(u_id) or LAST_LOCATION.get(str(u_id))
    lokasi_teks = loc["nama"] if loc else "(belum ada lokasi, share lokasi dulu)"

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
        f"{sumber_teks}"
    )
    kirim_pesan(chat_id, balasan)


def download_telegram_file(file_id: str) -> str:
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15)
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    fd, local_path = tempfile.mkstemp(suffix=".oga")
    os.close(fd)
    with requests.get(file_url, stream=True, timeout=30) as resp:
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    return local_path


def ekstrak_laporan(teks: str) -> dict:
    """Minta Gemini mengubah transkrip bebas jadi field terstruktur."""
    kategori = ", ".join(KEGIATAN_LABEL.values())
    prompt = f"""Kamu adalah asisten pencatatan laporan lapangan PLN. Tugasmu adalah mengekstrak teks laporan menjadi format JSON terstruktur.

Aturan Ekstraksi Laporan:
1. Kalimat atau bagian pertama dari teks menjelaskan "kegiatan". Kamu WAJIB mencocokkan dan memilih salah satu dari kategori resmi berikut (tulis persis sama):
   - EMERGENCY
   - INSPEKSI GARDU
   - PEMELIHARAAN
   - ROW
   - INSPEKSI JTM
   Pilih yang paling sesuai.

2. Kalimat atau bagian kedua menjelaskan "deskripsi" detail dari kegiatan tersebut. Ambil HANYA bagian kedua ini sebagai deskripsi (jangan masukkan kalimat pertama atau ketiga).

3. Kalimat atau bagian ketiga menjelaskan "material" yang digunakan beserta "jumlah" nya. Pisahkan dengan jelas antara nama material dan jumlahnya.

Contoh Ekstraksi:
Teks: "Inspeksi gardu, cek kondisi trafo aman, ganti isolator 3 buah"
Output JSON:
{{
  "kegiatan": "INSPEKSI GARDU",
  "deskripsi": "cek kondisi trafo aman",
  "material": [
    {{
      "nama": "isolator",
      "jumlah": "3 buah"
    }}
  ]
}}

Teks Laporan: \"\"\"{teks}\"\"\"

Balas HANYA dengan JSON valid tanpa formatting markdown seperti ```json atau penjelas lainnya. Format JSON harus persis seperti ini:
{{
  "kegiatan": "PILIH SALAH SATU: EMERGENCY / INSPEKSI GARDU / PEMELIHARAAN / ROW / INSPEKSI JTM",
  "deskripsi": "deskripsi kegiatan dari kalimat/bagian kedua",
  "material": [
    {{
      "nama": "nama material dari kalimat/bagian ketiga",
      "jumlah": "jumlah + satuan dari kalimat/bagian ketiga"
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

    try:
        url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent"
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
    except Exception:
        logger.exception("Gagal parse JSON dari Gemini")
        return {"kegiatan": "", "deskripsi": teks, "material": []}


def get_sheet():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)


def tulis_ke_sheet(hari: str, tanggal: str, data: dict) -> int:
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

    ws.update(
        f"A{target_row}:G{target_row}",
        [[no_val, hari_val, tanggal_val, kegiatan_val, data.get("deskripsi", ""), material_val, jumlah_val]],
    )
    return target_row


def kirim_pesan(chat_id, teks: str):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": teks, "parse_mode": "Markdown"},
        timeout=15,
    )


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
