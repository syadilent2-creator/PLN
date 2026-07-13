# Bot Telegram Dokumentasi Kegiatan PLN (Mini App)

Project ini terdiri dari 2 bagian:
- `miniapp/index.html` — halaman kamera (dibuka di dalam Telegram sebagai Mini App)
- `bot/bot.py` — backend penerima upload foto + pengirim balik ke chat Telegram

## Alur kerja

1. User buka bot Telegram → tekan tombol menu "Buka Kamera"
2. Mini App terbuka → pilih kegiatan → tekan "AMBIL FOTO"
3. Kamera aktif, lokasi diambil otomatis, watermark (jam/tanggal/lokasi/logo PLN) ditempel ke foto
4. Foto dikirim ke server (`bot.py`) → disimpan ke folder `foto/{kegiatan}/{tanggal}/`
5. Server kirim balik foto ke chat Telegram user
6. User tinggal tekan foto di chat → menu titik tiga → **Save to gallery**, lalu **Share → WhatsApp**

## Langkah setup

### 1. Buat bot di Telegram
1. Chat `@BotFather` → `/newbot` → ikuti instruksi → simpan `BOT_TOKEN`.

### 2. Deploy backend (`bot/`)
Bisa pakai VPS, Railway, Render, atau Fly.io (yang penting HTTPS).

```bash
cd bot
pip install -r requirements.txt
cp .env.example .env   # isi BOT_TOKEN
python bot.py
```

Untuk production, jalankan dengan gunicorn di belakang nginx/Caddy (biar dapat HTTPS otomatis):
```bash
gunicorn -w 2 -b 0.0.0.0:5000 bot:app
```

### 3. Deploy frontend (`miniapp/`)
Upload folder `miniapp/` ke hosting statis HTTPS apapun, misalnya:
- Vercel (`vercel deploy`)
- Netlify (drag & drop folder)
- GitHub Pages

Sebelum deploy, edit di `index.html`:
```js
const SERVER_URL = "https://server-kamu.com/upload"; // ganti sesuai URL backend kamu
```

Taruh juga file `pln-logo.png` (logo PLN) di folder `miniapp/` untuk ditampilkan di header dan watermark.

### 4. Hubungkan Mini App ke tombol menu bot
Edit `bot/setup_menu_button.py`, isi `MINIAPP_URL` dengan URL hasil deploy Mini App, lalu jalankan:
```bash
python setup_menu_button.py
```

Sekarang tombol menu (ikon di sebelah kolom chat, seperti "View Menu" di gambar referensimu) akan membuka Mini App kamera ini.

### 5. Uji coba
Buka bot di Telegram → tekan tombol menu → izinkan akses kamera & lokasi → ambil foto kegiatan → cek folder `foto/{kegiatan}/{tanggal}/` di server, dan cek chat Telegram menerima foto baliknya.

## Yang perlu disesuaikan lagi
- **Reverse geocoding**: contoh pakai Nominatim (gratis, ada rate limit). Untuk pemakaian production/banyak user, ganti ke Google Maps Geocoding API (berbayar tapi lebih stabil & akurat).
- **Daftar kegiatan**: edit array `KEGIATAN_LIST` di `index.html` dan `KEGIATAN_LABEL` di `bot.py` sesuai jenis kegiatan lapangan PLN yang kamu perlukan.
- **Autentikasi**: saat ini endpoint `/upload` terbuka publik. Untuk produksi, tambahkan validasi `initData` dari Telegram (`Telegram.WebApp.initData`) di backend agar hanya request dari bot kamu yang diterima.
- **Simpan ke galeri otomatis**: teknis WebView tidak mengizinkan tulis langsung ke galeri Android. Alur "kirim balik ke chat → Save to gallery" adalah cara paling stabil.
- **Kirim ke WhatsApp**: tombol share otomatis (`navigator.share`) bisa ditambahkan di Mini App untuk memicu share-sheet Android, tapi user tetap perlu 1 tap pilih WhatsApp (ini batasan OS, bukan batasan Telegram).
