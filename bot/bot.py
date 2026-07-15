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
import re
import tempfile
import threading
import time
import base64
import random
import string
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOTMU")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BASE_FOTO_DIR = "foto"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent"
# (connect_timeout, read_timeout) detik. Transkrip audio & ekstraksi JSON kadang butuh
# waktu lebih dari 30 detik terutama untuk VN yang agak panjang, jadi dinaikkan + ada retry.
GEMINI_TIMEOUT = (15, 150)
GEMINI_MAX_RETRY = 6
GEMINI_RETRY_BACKOFF = [5, 10, 20, 35, 60]  # jeda (detik) sebelum percobaan ke-2 dan ke-3


def panggil_gemini(payload: dict):
    """Panggil Gemini dengan retry yang lebih kuat"""
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    last_error = None
    
    for attempt in range(1, GEMINI_MAX_RETRY + 1):
        if attempt > 1:
            delay = GEMINI_RETRY_BACKOFF[min(attempt-2, len(GEMINI_RETRY_BACKOFF)-1)]
            time.sleep(delay + random.uniform(0, 3))  # jitter
            
        try:
            res = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=GEMINI_TIMEOUT)
            res.raise_for_status()
            return res
        except requests.Timeout as e:
            last_error = e
            logger.warning(f"Gemini timeout (attempt {attempt}/{GEMINI_MAX_RETRY})")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else None
            if status and status < 500 and status not in (429, 503):
                raise
            last_error = e
            logger.warning(f"Gemini error {status} (attempt {attempt})")
        except Exception as e:
            last_error = e
            logger.warning(f"Gemini connection error (attempt {attempt})")
    
    raise last_error


def pesan_error_gemini(e: Exception) -> str:
    if isinstance(e, requests.Timeout):
        return "Server AI lambat. Coba lagi dalam 30 detik."
    if isinstance(e, requests.HTTPError):
        status = e.response.status_code if e.response else None
        if status in (503, 429):
            return "Server AI sedang sibuk. Coba lagi sebentar atau ketik manual."
        if status in (401, 403):
            return "API Key Gemini bermasalah. Hubungi admin."
        return f"Server AI gangguan (kode {status})."
    return "Gagal memproses laporan. Coba lagi nanti."
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1vXFzN8nktmBmHUzN9KqkpaIBhHUSmoaXCXQOMb2Q2MY")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # isi mentah file JSON service account

# URL publik backend ini sendiri (Railway/dst), dipakai supaya Google Sheets bisa
# mengambil foto yang di-reply user lewat formula IMAGE(). WAJIB diisi untuk fitur
# foto -> kolom Gambar. Railway biasanya kasih domain seperti https://xxxx.up.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Scope Sheets biasa saja (fitur foto sekarang di-host sendiri, bukan lewat Google Drive,
# jadi tidak butuh scope Drive / kena masalah "service account tidak punya storage quota")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TZ = ZoneInfo("Asia/Jakarta")
HARI_ID = ["SENIN", "SELASA", "RABU", "KAMIS", "JUM'AT", "SABTU", "MINGGU"]

# --- Konfigurasi watermark foto ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LOGO_ICON_PATH = os.path.join(BASE_DIR, "assets", "logo_pln_icon.png")
ULP_NAME = os.getenv("ULP_NAME", "ULP Benubenua")

import fcntl  # untuk file-lock lintas proses (aman dipakai bareng beberapa worker gunicorn)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ======================================================================

@app.route("/test-gemini", methods=["GET"])
def test_gemini():
    if not GEMINI_API_KEY:
        return jsonify({"status": "error", "message": "GEMINI_API_KEY belum diisi"}), 400
    
    try:
        payload = {
            "contents": [{"parts": [{"text": "Halo, balas dengan kata OK saja."}]}]
        }
        res = panggil_gemini(payload)
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({
            "status": "success", 
            "model": "gemini-1.5-flash",
            "response": text
        })
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": str(e)
        }), 500

# ======================================================================
# STATE BERSAMA (lokasi terakhir, baris laporan terakhir, dsb) -- DISIMPAN DI FILE,
# BUKAN DICT DI MEMORI.
# ======================================================================
# Kenapa? Kalau backend dijalankan dengan lebih dari 1 worker (mis. "gunicorn -w 2"),
# setiap worker adalah PROSES TERPISAH dengan memorinya sendiri-sendiri. Dict biasa
# ("LAST_LOCATION = {}") TIDAK dibagi antar proses, jadi kalau share-location ditangani
# worker #1 tapi laporan suara berikutnya ditangani worker #2, worker #2 tidak akan
# tahu soal lokasi yang baru saja disimpan -> kolom Lokasi kosong secara acak/tidak
# konsisten. Menyimpan state ini ke file di disk (dengan file-lock) membuat semua
# worker baca-tulis dari sumber yang sama, dan sebagai bonus juga tetap ada walau
# satu worker/proses di-restart.
STATE_FILE = os.path.join(BASE_FOTO_DIR, "state.json")
_STATE_KOSONG = {"lokasi": {}, "baris_terakhir": {}, "baris_by_pesan": {}}


def _baca_tulis_state(mutator=None):
    """Baca file state, jalankan mutator(state) untuk memodifikasi (opsional), lalu
    tulis balik kalau ada mutator. Pakai file-lock exclusive supaya aman dipakai
    beberapa worker/proses sekaligus tanpa saling menimpa."""
    os.makedirs(BASE_FOTO_DIR, exist_ok=True)
    with open(STATE_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()
            try:
                state = json.loads(raw) if raw.strip() else dict(_STATE_KOSONG)
            except json.JSONDecodeError:
                state = dict(_STATE_KOSONG)
            for k, v in _STATE_KOSONG.items():
                state.setdefault(k, dict(v))

            if mutator is not None:
                mutator(state)
                f.seek(0)
                f.truncate()
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            return state
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def simpan_lokasi(user_id, lat, lon, nama):
    def _mut(state):
        state["lokasi"][str(user_id)] = {
            "lat": lat, "lon": lon, "nama": nama,
            "waktu": datetime.now(TZ).isoformat(),
        }
    _baca_tulis_state(_mut)


def ambil_lokasi(user_id) -> Optional[dict]:
    state = _baca_tulis_state()
    return state["lokasi"].get(str(user_id))


def simpan_baris_terakhir(user_id, baris: int):
    def _mut(state):
        state["baris_terakhir"][str(user_id)] = baris
    _baca_tulis_state(_mut)


def ambil_baris_terakhir(user_id) -> Optional[int]:
    state = _baca_tulis_state()
    return state["baris_terakhir"].get(str(user_id))


def hapus_baris_terakhir(user_id):
    def _mut(state):
        state["baris_terakhir"].pop(str(user_id), None)
    _baca_tulis_state(_mut)


def simpan_baris_by_pesan(message_id, baris: int):
    def _mut(state):
        state["baris_by_pesan"][str(message_id)] = baris
    _baca_tulis_state(_mut)


def ambil_baris_by_pesan(message_id) -> Optional[int]:
    if message_id is None:
        return None
    state = _baca_tulis_state()
    return state["baris_by_pesan"].get(str(message_id))

KEGIATAN_LABEL = {
    "emergency": "EMERGENCY",
    "inspeksi_gardu": "INSPEKSI GARDU",
    "pemeliharaan": "PEMELIHARAAN",
    "row": "ROW",
    "inspeksi_jtm": "INSPEKSI JTM",
    "perbaikan": "PERBAIKAN",
}

# ======================================================================
# PENCOCOKAN KEGIATAN BERDASARKAN KATA KUNCI (deterministik, bukan tebakan AI)
# ======================================================================
# Analoginya seperti admin manusia yang scan kalimat laporan dan langsung tahu
# ini masuk kategori apa dari kata yang benar-benar disebut. Kalau ada kata
# kunci yang cocok, hasil ini akan MENANG dibanding tebakan AI.
#
# "primer" = kata yang hampir selalu berarti kategori itu (bobot besar), sedangkan
# "sekunder" = kata konteks pendukung yang bisa juga muncul di kategori lain
# (bobot kecil). Ini penting supaya kata seperti "PHPTR" (sekunder utk INSPEKSI GARDU)
# tidak mengalahkan kata "pemeliharaan" (primer utk PEMELIHARAAN) kalau dua-duanya
# muncul dalam satu laporan.
KEGIATAN_KEYWORDS = {
    "EMERGENCY": {
        "primer": ["emergency", "darurat"],
        "sekunder": [
            "gangguan mendadak", "padam mendadak", "padam total", "black out",
            "blackout", "tumbang", "roboh", "kebakaran", "terbakar", "longsor",
            "kecelakaan", "gangguan tiba-tiba", "putus mendadak", "trip mendadak",
            "konslet", "korsleting",
        ],
    },
    "ROW": {
        "primer": ["row", "right of way", "pemangkasan", "penebangan"],
        "sekunder": [
            "memangkas", "pangkas pohon", "tebang", "menebang", "vegetasi",
            "ranting", "jalur kabel", "lintasan kabel", "pohon mengganggu",
            "pohon",
        ],
    },
    "INSPEKSI GARDU": {
        "primer": [
            "inspeksi gardu", "cek gardu", "periksa gardu", "patroli gardu",
            "inspeksi trafo",
        ],
        "sekunder": [
            "kondisi gardu", "cek trafo", "periksa trafo", "kondisi trafo",
            "phptr", "kubikel", "panel hubung", "gardu distribusi", "box gardu",
            "gardu", "trafo",
        ],
    },
    "INSPEKSI JTM": {
        "primer": [
            "inspeksi jtm", "cek jtm", "periksa jtm", "patroli jtm",
            "inspeksi tiang",
        ],
        "sekunder": [
            "patroli jaringan", "cek tiang", "periksa tiang", "kondisi tiang",
            "jaringan tegangan menengah", "andongan", "cek jaringan",
            "periksa jaringan", "tiang", "jtm", "kawat", "konduktor",
        ],
    },
    "PEMELIHARAAN": {
        "primer": ["pemeliharaan", "perawatan", "maintenance"],
        "sekunder": [
            "preventif", "penggantian rutin", "pembersihan", "bersihkan",
            "mengencangkan", "kencangkan baut",
        ],
    },
    "PERBAIKAN": {
        "primer": [
            "perbaikan", "memperbaiki", "diperbaiki", "reparasi", "perbaiki",
        ],
        "sekunder": ["servis", "betulkan"],
    },
}
BOBOT_PRIMER = 3
BOBOT_SEKUNDER = 1
# Urutan prioritas kalau ada skor kata kunci yang seri (kategori lebih spesifik menang)
KEGIATAN_PRIORITY = [
    "EMERGENCY", "ROW", "PERBAIKAN", "INSPEKSI GARDU", "INSPEKSI JTM", "PEMELIHARAAN",
]


def deteksi_kegiatan_dari_kata_kunci(teks: str) -> Optional[str]:
    """Cocokkan teks laporan ke salah satu dari 6 kategori resmi berdasarkan
    kata/kalimat yang benar-benar muncul di deskripsi (bukan tebakan bebas AI).
    Return None kalau tidak ada kata kunci yang cocok sama sekali."""
    teks_lower = teks.lower()
    skor = {kat: 0 for kat in KEGIATAN_PRIORITY}
    for kat, grup in KEGIATAN_KEYWORDS.items():
        for kw in grup["primer"]:
            if kw in teks_lower:
                skor[kat] += BOBOT_PRIMER
        for kw in grup["sekunder"]:
            if kw in teks_lower:
                skor[kat] += BOBOT_SEKUNDER
    skor_tertinggi = max(skor.values())
    if skor_tertinggi == 0:
        return None
    kandidat = [kat for kat, s in skor.items() if s == skor_tertinggi]
    for kat in KEGIATAN_PRIORITY:  # tie-break: kategori paling spesifik menang
        if kat in kandidat:
            return kat
    return kandidat[0]


# ======================================================================
# PENCOCOKAN MATERIAL & JUMLAH BERDASARKAN POLA KATA (heuristik, deterministik)
# ======================================================================
# Seperti manusia yang membaca laporan: setelah nemu kata kerja pemakaian/penggantian,
# cari angkanya (baik ditulis digit "3" maupun kata "tiga"), lalu ambil nama barang
# di SEBELUM atau SESUDAH angka itu tergantung urutan kalimatnya -- karena orang bicara
# bebas, bisa "ganti isolator 3 buah" ATAU "mengganti tiga NH fuse yang rusak".
_KATA_KERJA_MATERIAL_SET = {
    "mengganti", "penggantian", "ganti", "memasang", "pemasangan", "pasang",
    "menggunakan", "gunakan", "menambahkan", "tambah",
}
_SATUAN_SET = {
    "buah", "pcs", "pc", "unit", "meter", "mtr", "m", "set", "batang", "btg",
    "roll", "lembar", "butir", "liter", "ltr", "box",
}
# Kata yang menandai "berhenti di sini" saat mencari nama barang (biasanya masuk ke
# info lain: lokasi, alasan, dsb) supaya nama barang tidak "kebablasan" ikut ke situ.
_STOPWORD_BATAS = {
    "di", "ke", "pada", "untuk", "karena", "dan", "serta", "akibat",
    "sehingga", "agar", "supaya", "dari", "oleh", "dengan", "saat", "ketika",
}
# Kata pengisi yang dilewati (bukan bagian nama barang ataupun batas)
_FILLER = {"sebanyak", "yang", "tersebut", "nya", "itu", "ini"}
# Kata kondisi yang sering nempel SETELAH nama barang, bukan bagian nama barang itu sendiri
_KONDISI = {
    "rusak", "aus", "bermasalah", "hilang", "hangus", "pecah", "retak",
    "bocor", "putus", "terbakar",
}
ANGKA_KATA = {
    "satu": "1", "dua": "2", "tiga": "3", "empat": "4", "lima": "5",
    "enam": "6", "tujuh": "7", "delapan": "8", "sembilan": "9", "sepuluh": "10",
}
_POLA_ANGKA_KATA = re.compile(r"\b(" + "|".join(ANGKA_KATA.keys()) + r")\b", re.IGNORECASE)


def _normalisasi_angka_kata(teks: str) -> str:
    """Ubah angka dalam bentuk kata ("tiga") jadi digit ("3") supaya bisa dideteksi,
    karena hasil transkrip suara sering menulis angka sebagai kata, bukan digit."""
    return _POLA_ANGKA_KATA.sub(lambda m: ANGKA_KATA[m.group(0).lower()], teks)


def deteksi_material_regex(teks: str) -> list:
    """Cari pasangan (nama material, jumlah) dari kata kerja pemakaian/penggantian +
    angka (digit maupun kata bilangan) yang menyertainya, di urutan manapun kalimat
    itu ditulis. Deterministik, tidak bergantung ke AI."""
    teks_norm = _normalisasi_angka_kata(teks)
    kata_list = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", teks_norm)
    n = len(kata_list)
    hasil = []
    seen = set()

    i = 0
    while i < n:
        if kata_list[i].lower() not in _KATA_KERJA_MATERIAL_SET:
            i += 1
            continue

        batas = min(i + 9, n)  # jangkauan pencarian angka dibatasi ~8 kata setelah kata kerja
        j = i + 1
        while j < batas and kata_list[j].lower() not in _KATA_KERJA_MATERIAL_SET:
            if not kata_list[j].isdigit():
                j += 1
                continue

            idx_angka = j
            satuan = ""
            idx_setelah = j + 1
            if idx_setelah < n and kata_list[idx_setelah].lower() in _SATUAN_SET:
                satuan = kata_list[idx_setelah].lower()
                idx_setelah += 1

            # Coba cari nama barang SEBELUM angka dulu (pola: "ganti isolator 3 buah")
            nama_kata = []
            k = idx_angka - 1
            while k > i and len(nama_kata) < 4:
                w = kata_list[k]
                wl = w.lower()
                if wl in _STOPWORD_BATAS:
                    break
                if wl in _FILLER:
                    k -= 1
                    continue
                nama_kata.insert(0, w)
                k -= 1
            nama = " ".join(nama_kata).strip()

            # Kalau tidak ketemu (angka nempel langsung ke kata kerja), cari SETELAH
            # angka/satuan (pola: "mengganti tiga NH fuse yang rusak")
            if not nama:
                nama_kata = []
                k2 = idx_setelah
                while k2 < batas and len(nama_kata) < 4:
                    w = kata_list[k2]
                    wl = w.lower()
                    if wl in _STOPWORD_BATAS or wl in _KATA_KERJA_MATERIAL_SET:
                        break
                    if wl in _FILLER or wl in _KONDISI:
                        k2 += 1
                        continue
                    nama_kata.append(w)
                    k2 += 1
                nama = " ".join(nama_kata).strip()

            if nama:
                key = nama.lower()
                if key not in seen:
                    seen.add(key)
                    jumlah = f"{kata_list[idx_angka]} {satuan}".strip() if satuan else kata_list[idx_angka]
                    hasil.append({"nama": nama, "jumlah": jumlah})
            break  # 1 angka per kata kerja sudah cukup, lanjut cari kata kerja berikutnya

        i += 1

    return hasil

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
@app.route("/test-gemini", methods=["GET"])
def test_gemini():
    try:
        payload = {"contents": [{"parts": [{"text": "Test: balas dengan kata 'OK'"}]}]}
        res = panggil_gemini(payload)
        return jsonify({"status": "ok", "text": res.json()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
# ======================================================================

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
        
        # Simpan lokasi terbaru (state di file, dibagi semua worker) agar dipakai
        # untuk VN/teks Telegram berikutnya
        nama_lokasi = ", ".join(filter(None, [alamat, wilayah])) or "Lokasi tidak diketahui"
        try:
            simpan_lokasi(user_id, lat, lon, nama_lokasi)
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
            hapus_baris_terakhir(user_id)
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
                    "membagikan lokasi kerja kamu, supaya laporan otomatis mencantumkan lokasi. "
                    "Kalau tombol itu tidak bereaksi (kadang terjadi di Telegram Web/browser), "
                    "ketik manual: `/lokasi nama tempat kamu`\n\n"
                    "Kalau mau memulai catatan kegiatan yang baru kapan saja, tap tombol "
                    "\U0001F504 Mulai Kegiatan Baru.",
                    reply_markup=main_keyboard(),
                )
            elif teks.startswith("/lokasi"):
                nama_lokasi = teks[len("/lokasi"):].strip()
                if not nama_lokasi:
                    kirim_pesan(
                        chat_id,
                        "Format: `/lokasi nama tempat kamu`\n"
                        "Contoh: `/lokasi Gardu Induk Rungkut, Surabaya`",
                    )
                else:
                    simpan_lokasi(user_id, None, None, nama_lokasi)
                    kirim_pesan(
                        chat_id,
                        f"Lokasi tersimpan (manual): {nama_lokasi}\n"
                        "Akan dipakai otomatis untuk laporan-laporan berikutnya.",
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

    simpan_lokasi(user_id, lat, lon, nama)
    kirim_pesan(
        chat_id,
        f"Lokasi tersimpan: {nama}\nAkan dipakai otomatis untuk laporan-laporan berikutnya.",
        reply_markup=main_keyboard(),
    )


def proses_voice_note(user_id, chat_id, file_id):
    try:
        if not GEMINI_API_KEY:
            kirim_pesan(chat_id, "GEMINI_API_KEY belum diset.")
            return

        audio_path = download_telegram_file(file_id)

        with open(audio_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode("utf-8")
        os.remove(audio_path)

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "audio/ogg", "data": audio_data}},
                    {"text": "Transkripsikan rekaman suara ini ke dalam teks bahasa Indonesia secara lengkap dan akurat. Jangan tambahkan komentar lain."}
                ]
            }]
        }

        res = panggil_gemini(payload)
        teks = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        if not teks:
            kirim_pesan(chat_id, "Suara tidak terdeteksi dengan jelas.")
            return

        proses_dan_simpan_laporan(user_id, chat_id, teks, sumber="suara")

    except Exception as e:
        logger.exception("Gagal proses voice note")
        kirim_pesan(chat_id, pesan_error_gemini(e))


# ekstrak_laporan dengan fallback kuat
def ekstrak_laporan(teks: str) -> dict:
    kegiatan_kw = deteksi_kegiatan_dari_kata_kunci(teks)
    material_regex = deteksi_material_regex(teks)

    hasil = {"kegiatan": kegiatan_kw or "LAINNYA", "material": list(material_regex)}

    # Coba pakai Gemini
    try:
        # (prompt tetap sama seperti kode asli kamu)
        prompt = f"""... [masukkan prompt lengkap kamu di sini] ..."""   # copy prompt panjang dari kode asli

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }

        res = panggil_gemini(payload)
        content = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Bersihkan markdown
        if content.startswith("```"):
            content = "\n".join(content.splitlines()[1:-1]).strip()

        ai_result = json.loads(content)

        # Prioritaskan hasil keyword
        if kegiatan_kw:
            hasil["kegiatan"] = kegiatan_kw
        elif ai_result.get("kegiatan"):
            ai_keg = ai_result["kegiatan"].strip().upper()
            if ai_keg in KEGIATAN_LABEL.values():
                hasil["kegiatan"] = ai_keg

        # Tambah material dari AI
        nama_sudah_ada = {m["nama"].strip().lower() for m in hasil["material"]}
        for m in ai_result.get("material", []):
            nama = (m.get("nama") or "").strip()
            if nama and nama.lower() not in nama_sudah_ada:
                hasil["material"].append({"nama": nama, "jumlah": m.get("jumlah", "-")})
                nama_sudah_ada.add(nama.lower())

    except Exception as e:
        logger.warning(f"Gemini gagal, pakai fallback keyword only: {e}")

    return hasil


def proses_dan_simpan_laporan(user_id, chat_id, teks: str, sumber: str):
    """Inti bersama: ekstraksi AI -> tulis ke sheet -> balas ke user.
    Dipakai baik untuk laporan dari voice note maupun teks ketikan."""

    # 1. Ekstraksi terstruktur -> JSON (GPT), untuk ambil kegiatan & material saja
    data = ekstrak_laporan(teks)
    # Deskripsi selalu pakai teks lengkap apa adanya (bukan potongan hasil AI)
    data["deskripsi"] = teks

    # 2. Ambil lokasi terakhir user (dari share-location / Mini App) SEBELUM tulis ke sheet,
    #    supaya kolom Lokasi langsung terisi nama daerah + titik koordinat.
    loc = ambil_lokasi(user_id)
    lokasi_teks = loc["nama"] if loc else "(belum ada lokasi)"

    # 3. Tulis ke Google Sheet
    now = datetime.now(TZ)
    hari = HARI_ID[now.weekday()]
    tanggal = now.strftime("%d-%m-%Y")
    waktu = now.strftime("%H:%M")
    tanggal_waktu = f"{tanggal} {waktu}"  # kolom C sheet sekarang "Tanggal & Waktu" digabung
    baris = tulis_ke_sheet(hari, tanggal_waktu, data, loc=loc)

    # Simpan baris ini sebagai "laporan terakhir" user, dipakai kalau nanti user
    # reply pesan ringkasan di bawah ini dengan foto -> foto masuk ke baris yang sama.
    simpan_baris_terakhir(user_id, baris)

    # 4. Balas ringkasan ke user
    material_teks = "\n".join(
        f"  \u2022 {m['nama']} - {m['jumlah']}" for m in data.get("material", [])
    ) or "  -"

    sumber_teks = "_Dari pesan suara_" if sumber == "suara" else "_Dari teks ketikan_"
    balasan = (
        f"*Laporan tersimpan* (baris {baris})\n\n"
        f"Hari/Tanggal: {hari}, {tanggal}\n"
        f"Jam: {waktu} WIB\n"
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
        simpan_baris_by_pesan(sent_message_id, baris)
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
# 3. [BARU] FOTO SEBAGAI REPLY -> HOST SENDIRI DI BACKEND -> KOLOM GAMBAR
# ======================================================================
# Kenapa tidak upload ke Google Drive? Karena Drive milik service account TIDAK
# punya storage quota sendiri (khusus akun Gmail biasa/non-Workspace, ini akan
# selalu gagal dengan error "storageQuotaExceeded"). Solusinya: foto disimpan di
# disk server ini sendiri (folder foto/sheet_gambar/) lalu di-serve lewat endpoint
# publik /foto/<path>, dan URL itu yang dipakai formula IMAGE() di Google Sheets.

FOTO_SHEET_DIR = os.path.join(BASE_FOTO_DIR, "sheet_gambar")


@app.route("/foto/<path:filename>", methods=["GET"])
def serve_foto(filename):
    """Serve foto yang sudah disimpan supaya bisa diakses publik oleh formula
    IMAGE() di Google Sheets (Sheets butuh URL yang bisa diakses tanpa login,
    dengan Content-Type gambar yang eksplisit dan tanpa perlu autentikasi)."""
    resp = send_from_directory(BASE_FOTO_DIR, filename, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def cari_baris_laporan_terakhir() -> Optional[int]:
    """Fallback terakhir kalau state di file (baris_by_pesan/baris_terakhir) kosong
    -- misal user kirim foto tanpa laporan sebelumnya sama sekali.
    Cari langsung dari sheet: baris terakhir yang kolom Deskripsi-nya sudah terisi."""
    try:
        ws = get_sheet()
        semua = ws.get_all_values()
        for i in range(len(semua), 1, -1):
            row = semua[i - 1]
            deskripsi_cell = row[4] if len(row) > 4 else ""
            if deskripsi_cell.strip():
                return i
    except Exception:
        logger.exception("Gagal cari baris laporan terakhir sbg fallback foto")
    return None


def proses_foto_laporan(user_id, chat_id, file_id, reply_msg_id):
    """Handle foto yang dikirim user (biasanya sebagai reply ke ringkasan laporan).
    Foto disimpan di server ini sendiri lalu disisipkan ke kolom Gambar (kolom I)
    pada baris yang sesuai, memakai formula IMAGE(url) supaya ukurannya otomatis
    menyesuaikan ukuran cell."""
    if not PUBLIC_BASE_URL:
        kirim_pesan(
            chat_id,
            "Fitur foto belum aktif: env var PUBLIC_BASE_URL belum diisi di server "
            "(isi dengan URL publik bot ini, misal https://nama-app.up.railway.app). "
            "Hubungi admin bot.",
        )
        return

    local_path = None
    try:
        # Tentukan baris tujuan, 3 lapis fallback:
        # 1. Baris dari pesan spesifik yang di-reply (paling akurat).
        # 2. Laporan terakhir milik user ini (state di file, dibagi semua worker).
        # 3. Kalau state kosong (belum pernah ada laporan sama sekali) -> cari langsung
        #    dari sheet: baris terakhir yang sudah terisi.
        target_row = ambil_baris_by_pesan(reply_msg_id)
        if target_row is None:
            target_row = ambil_baris_terakhir(user_id)
        if target_row is None:
            target_row = cari_baris_laporan_terakhir()

        if target_row is None:
            kirim_pesan(
                chat_id,
                "Belum ada laporan (teks/suara) untuk dikaitkan dengan foto ini. "
                "Kirim laporan kegiatan dulu, baru reply pesan ringkasannya dengan foto.",
            )
            return

        kirim_pesan(chat_id, f"Menerima foto, menyimpan ke baris {target_row}...")

        # Download dari Telegram + upload/tulis ke Sheet dibungkus retry ringan,
        # supaya kalau server baru saja "bangun" dari cold start (permintaan pertama
        # kadang lebih lambat/gagal), percobaan ke-2 otomatis dicoba tanpa user perlu
        # kirim ulang foto secara manual.
        percobaan_terakhir_error = None
        for percobaan in range(1, 3):
            try:
                local_path = download_telegram_file(file_id, suffix=".jpg")

                os.makedirs(FOTO_SHEET_DIR, exist_ok=True)
                nama_file = f"baris{target_row}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
                tujuan = os.path.join(FOTO_SHEET_DIR, nama_file)

                # Kompres/resize foto sebelum disimpan: Google Sheets IMAGE() punya batas
                # ukuran file, dan foto asli dari kamera HP kadang cukup besar (beberapa MB).
                # Diperkecil ke maks 1280px sisi terpanjang + kualitas JPEG 85% supaya jauh
                # di bawah batas dan lebih cepat di-fetch oleh Sheets.
                with Image.open(local_path) as img:
                    img = img.convert("RGB")  # jaga-jaga kalau ada channel alpha/mode aneh
                    img.thumbnail((1280, 1280))
                    img.save(tujuan, format="JPEG", quality=85, optimize=True)
                os.remove(local_path)
                local_path = None  # sudah diproses & dihapus, tidak perlu dihapus lagi di finally

                url_gambar = f"{PUBLIC_BASE_URL}/foto/sheet_gambar/{nama_file}"

                ws = get_sheet()
                ws.update(
                    f"I{target_row}:I{target_row}",
                    [[f'=IMAGE("{url_gambar}")']],
                    value_input_option="USER_ENTERED",
                )
                percobaan_terakhir_error = None
                break
            except Exception as e:
                percobaan_terakhir_error = e
                logger.warning(f"Gagal proses foto (percobaan {percobaan}/2): {e}")
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
                    local_path = None
                time.sleep(2)

        if percobaan_terakhir_error is not None:
            raise percobaan_terakhir_error

        kirim_pesan(chat_id, f"Foto tersimpan di kolom Gambar, baris {target_row}.")
    except Exception as e:
        logger.exception("Gagal proses foto laporan")
        kirim_pesan(chat_id, f"Gagal menyimpan foto: {e}")
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


def ekstrak_laporan(teks: str) -> dict:
    """Ekstrak kegiatan & material dari teks laporan.
    Strategi 2 lapis (seperti admin manusia yang teliti):
    1. Deteksi berdasarkan kata kunci/pola kata yang PERSIS muncul di teks (deterministik,
       lewat deteksi_kegiatan_dari_kata_kunci & deteksi_material_regex).
    2. AI (Gemini) dipakai untuk melengkapi/menormalkan kasus yang tidak tertangkap
       kata kunci (nama material dengan ejaan bebas, kalimat ambigu, dsb).
    Kalau kata kunci ketemu, hasil kata kunci itu yang dipakai (menang atas tebakan AI),
    supaya kegiatan konsisten sesuai kata yang benar-benar disebut di laporan."""

    kegiatan_kw = deteksi_kegiatan_dari_kata_kunci(teks)
    material_regex = deteksi_material_regex(teks)

    kategori = ", ".join(KEGIATAN_LABEL.values())
    prompt = f"""Kamu adalah asisten pencatatan laporan lapangan PLN yang sangat teliti, selayaknya petugas admin manusia yang membaca laporan dari VN (voice note) maupun teks ketikan lalu memindahkannya ke tabel Excel/Spreadsheet. Tugasmu mengekstrak teks laporan menjadi JSON terstruktur berisi "kegiatan" dan "material" saja.

========================================
ATURAN 1 - MENENTUKAN "kegiatan"
========================================
Baca keseluruhan konteks laporan (bukan cuma kata pertama), lalu cocokkan ke SALAH SATU dari 6 kategori resmi berikut (tulis persis sama, huruf besar semua):

- EMERGENCY
  Ciri-ciri: gangguan mendadak/darurat, padam tiba-tiba, jaringan putus/roboh akibat pohon tumbang/longsor/kecelakaan, kebakaran, kondisi berbahaya yang perlu ditangani segera di luar jadwal rutin.

- INSPEKSI GARDU
  Ciri-ciri: mengecek/memeriksa/patroli kondisi GARDU DISTRIBUSI, trafo, PHPTR (Panel Hubung Bagi Tegangan Rendah), kubikel, box gardu. Kata kunci: "inspeksi gardu", "cek gardu", "cek trafo", "kondisi gardu".

- PEMELIHARAAN
  Ciri-ciri: kegiatan perawatan/pemeliharaan TERJADWAL (preventif) yang memang sudah direncanakan rutin, membersihkan, mengencangkan baut/klem, penggantian komponen sebagai bagian pemeliharaan berkala. Kata kunci: "pemeliharaan", "perawatan", "penggantian rutin".

- ROW
  Ciri-ciri: Right of Way — pemangkasan/penebangan pohon atau vegetasi yang mendekati/mengganggu jaringan listrik, pembersihan jalur/lintasan kabel. Kata kunci: "ROW", "pemangkasan pohon", "vegetasi", "penebangan".

- INSPEKSI JTM
  Ciri-ciri: mengecek/memeriksa/patroli kondisi JARINGAN TEGANGAN MENENGAH (JTM) di LUAR gardu — tiang listrik, kawat/konduktor, isolator di jaringan, andongan kawat. Kata kunci: "inspeksi JTM", "patroli jaringan", "cek tiang", "cek jaringan".

- PERBAIKAN
  Ciri-ciri: memperbaiki/mereparasi komponen atau peralatan yang RUSAK/bermasalah, TAPI bukan kondisi darurat/berbahaya (itu masuk EMERGENCY) dan bukan bagian dari jadwal pemeliharaan rutin (itu masuk PEMELIHARAAN). Biasanya berupa laporan perbaikan atas keluhan/temuan kerusakan spesifik. Kata kunci: "perbaikan", "memperbaiki", "diperbaiki", "reparasi", "rusak diperbaiki".

Jika laporan menyebut kombinasi (misal inspeksi SEKALIGUS ganti komponen), pilih kategori berdasarkan TUJUAN UTAMA kunjungan (inspeksi rutin gardu yang berujung ganti komponen kecil tetap INSPEKSI GARDU; penggantian terjadwal skala pemeliharaan masuk PEMELIHARAAN; perbaikan atas kerusakan yang dilaporkan/ditemukan di luar jadwal rutin masuk PERBAIKAN; kondisi darurat/berbahaya masuk EMERGENCY).
{f'PETUNJUK: hasil pencocokan kata kunci otomatis menunjukkan kategori "{kegiatan_kw}" — pakai ini kecuali konteks laporan jelas-jelas bertentangan.' if kegiatan_kw else ''}

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
  "kegiatan": "PILIH SALAH SATU: EMERGENCY / INSPEKSI GARDU / PEMELIHARAAN / ROW / INSPEKSI JTM / PERBAIKAN",
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

    hasil = {"kegiatan": kegiatan_kw or "", "material": list(material_regex)}
    try:
        res = panggil_gemini(payload)
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

        ai_result = json.loads(content)

        # --- Kegiatan: kata kunci deterministik MENANG kalau ada, AI cuma fallback ---
        ai_kegiatan = (ai_result.get("kegiatan") or "").strip().upper()
        if kegiatan_kw:
            hasil["kegiatan"] = kegiatan_kw
        elif ai_kegiatan in KEGIATAN_LABEL.values():
            hasil["kegiatan"] = ai_kegiatan
        else:
            hasil["kegiatan"] = ai_kegiatan or ""

        # --- Material: gabungkan hasil regex (pasti akurat) + tambahan dari AI
        #     (untuk material yang tidak tertangkap pola regex, misal ejaan bebas dari VN) ---
        nama_sudah_ada = {m["nama"].strip().lower() for m in hasil["material"]}
        for m in ai_result.get("material", []):
            nama = (m.get("nama") or "").strip()
            if nama and nama.lower() not in nama_sudah_ada:
                hasil["material"].append({"nama": nama, "jumlah": m.get("jumlah", "-")})
                nama_sudah_ada.add(nama.lower())

        return hasil
    except requests.Timeout:
        logger.warning("Gemini API timeout saat ekstraksi, pakai hasil deteksi kata kunci saja")
        return hasil
    except requests.HTTPError as e:
        # Log body respons Gemini biar kelihatan pesan error aslinya (mis. 400/404 karena
        # endpoint/model/API key salah), bukan cuma "gagal parse JSON"
        body = e.response.text if e.response is not None else "(tidak ada respons)"
        status = e.response.status_code if e.response is not None else "?"
        logger.error(f"Gemini API error {status}: {body}")
        return hasil
    except Exception:
        logger.exception("Gagal parse JSON dari Gemini, pakai hasil deteksi kata kunci saja")
        return hasil


def get_sheet():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)


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


def tulis_ke_sheet(hari: str, tanggal_waktu: str, data: dict, loc: Optional[dict] = None) -> int:
    """Cari baris pertama yang kolom Deskripsi (E) masih kosong, isi di situ.
    Kolom C sekarang "Tanggal & Waktu" digabung (mis. "14-07-2026 16:33").
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
    tanggal_val = cell(2) or tanggal_waktu
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
