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
gunicorn -w 1 -b 0.0.0.0:5000 bot:app
```
**PENTING: pakai `-w 1` (1 worker), JANGAN lebih.** Versi sebelumnya menyarankan `-w 2`, tapi ini
menyebabkan bug: state (lokasi terakhir, baris laporan terakhir) sekarang disimpan di file
`foto/state.json` yang di-lock supaya aman dipakai bareng banyak PROSES sekaligus -- tapi kalau
kamu perlu lebih dari 1 worker untuk skala trafik lebih besar, pastikan seluruh worker mount ke
`BASE_FOTO_DIR` yang SAMA (mis. lewat Railway Volume) supaya file state-nya benar-benar dibagi,
bukan masing-masing worker punya salinan disk terpisah.

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

## Fitur baru: Lokasi otomatis, Foto via reply, & tombol keyboard

Sheet punya 9 kolom: `No, Hari, Tanggal, Kegiatan, Deskripsi, Material, Jumlah, Lokasi, Gambar`.

1. **Kegiatan** — teks laporan (VN atau ketik) dicocokkan otomatis oleh AI ke salah satu dari 5 kategori resmi (EMERGENCY, INSPEKSI GARDU, PEMELIHARAAN, ROW, INSPEKSI JTM) berdasarkan konteks, bukan cuma kata pertama.
2. **Material & Jumlah** — dikenali dari kata kunci seperti "mengganti", "menggunakan", "sebanyak", "jumlah", atau pola "nama barang + angka + satuan".
3. **Lokasi** — otomatis terisi dari lokasi terakhir yang di-share user (tombol 📍, atau dari Mini App kamera), berupa `nama daerah (lat, lon)`.
4. **Gambar** — setelah bot membalas ringkasan laporan, **reply pesan ringkasan itu dengan foto**. Foto disimpan di server bot ini sendiri (folder `foto/sheet_gambar/`) dan di-serve lewat endpoint publik `/foto/...`, lalu disisipkan ke kolom Gambar di baris yang sama memakai formula `=IMAGE(url)` — otomatis menyesuaikan ukuran cell (mode fit-to-cell adalah default kalau parameter mode tidak diisi, supaya tidak kena masalah locale titik-koma vs koma pada formula Sheets).
5. **Tombol keyboard persisten** — setelah `/start`, muncul 2 tombol di bawah kolom chat:
   - 📍 **Bagikan Lokasi Saya** — 1 tap langsung share lokasi (tanpa buka menu attachment).
   - 🔄 **Mulai Kegiatan Baru** — reset kaitan "laporan terakhir" supaya foto berikutnya tidak salah nyangkut ke laporan lama, lalu bot siap terima laporan baru.

### Kenapa foto TIDAK diupload ke Google Drive?
Sempat dicoba pakai Google Drive, tapi service account **tidak punya storage quota sendiri** di Drive biasa (khusus akun Gmail non-Workspace, upload akan selalu gagal dengan error `storageQuotaExceeded`). Solusi resminya butuh Shared Drive (fitur Google Workspace) atau OAuth delegation — keduanya ribet buat setup kecil begini. Jadi foto di-host sendiri oleh backend ini (pola yang sama seperti fitur foto Mini App yang sudah ada sejak awal).

### Setup wajib untuk fitur foto: `PUBLIC_BASE_URL`
1. Buka project Railway kamu → tab **Settings** → cari domain publiknya (biasanya `https://nama-app-xxxx.up.railway.app`, atau domain custom kalau sudah di-set).
2. Tambahkan environment variable baru: `PUBLIC_BASE_URL` = domain tsb (tanpa slash `/` di akhir).
3. Redeploy.

**Catatan penting soal penyimpanan:** foto disimpan di disk container Railway. Kalau Railway kamu pakai **ephemeral filesystem** (default, tanpa Volume), foto bisa hilang saat redeploy/restart. Untuk penyimpanan permanen, tambahkan **Railway Volume** dan mount ke folder kerja bot (misal `/app/foto`) lewat tab **Volumes** di project Railway.

## Yang perlu disesuaikan lagi
- **Reverse geocoding**: contoh pakai Nominatim (gratis, ada rate limit). Untuk pemakaian production/banyak user, ganti ke Google Maps Geocoding API (berbayar tapi lebih stabil & akurat).
- **Daftar kegiatan**: edit array `KEGIATAN_LIST` di `index.html` dan `KEGIATAN_LABEL` di `bot.py` sesuai jenis kegiatan lapangan PLN yang kamu perlukan.
- **Autentikasi**: saat ini endpoint `/upload` terbuka publik. Untuk produksi, tambahkan validasi `initData` dari Telegram (`Telegram.WebApp.initData`) di backend agar hanya request dari bot kamu yang diterima.
- **Simpan ke galeri otomatis**: teknis WebView tidak mengizinkan tulis langsung ke galeri Android. Alur "kirim balik ke chat → Save to gallery" adalah cara paling stabil.
- **Kirim ke WhatsApp**: tombol share otomatis (`navigator.share`) bisa ditambahkan di Mini App untuk memicu share-sheet Android, tapi user tetap perlu 1 tap pilih WhatsApp (ini batasan OS, bukan batasan Telegram).
