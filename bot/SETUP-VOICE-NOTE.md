# Setup Fitur Voice Note (VN) -> Google Sheets

Fitur ini terpisah dari Mini App kamera. User cukup kirim **pesan suara** ke bot,
bot akan transkrip -> ekstrak info -> tulis otomatis ke Google Sheet.

## Cara pakai (dari sisi user)

1. Sekali di awal shift: tekan tombol \U0001F4CE (attach) di Telegram -> **Location** ->
   **Share My Current Location** (sekali kirim, bukan live location). Bot akan balas
   konfirmasi lokasi tersimpan.
2. Kirim pesan suara (tekan & tahan tombol mic), jelaskan kegiatan, deskripsi,
   dan material yang dipakai beserta jumlahnya. Contoh:
   > "Hari ini inspeksi gardu di Jalan Rungkut Industri, ganti isolator 3 buah
   > dan kabel NYY 2 meter, kondisi aman terkendali."
3. Bot balas ringkasan (Hari, Tanggal, Lokasi, Kegiatan, Deskripsi, Material) dan
   baris itu otomatis muncul di spreadsheet.

Catatan penting: Telegram **tidak** menyisipkan GPS otomatis ke file voice note.
Makanya langkah 1 (share location manual) perlu dilakukan minimal sekali per shift;
lokasi ini disimpan sementara di memori server dan dipakai untuk laporan-laporan
berikutnya sampai kamu share lokasi baru.

## Cara setup (dari sisi kamu, sekali saja)

### 1. Buat Google Service Account (supaya bot bisa nulis ke Sheet)

1. Buka [console.cloud.google.com](https://console.cloud.google.com) -> buat project baru
   (atau pakai yang sudah ada).
2. Di search bar, cari **"Google Sheets API"** -> klik **Enable**.
3. Buka menu **APIs & Services -> Credentials** -> **Create Credentials** ->
   **Service Account**.
4. Isi nama bebas (misal `pln-bot-sheets`), klik **Create and Continue** ->
   **Done** (skip bagian role/akses opsional).
5. Klik service account yang baru dibuat -> tab **Keys** -> **Add Key** ->
   **Create new key** -> pilih **JSON** -> download.
6. Buka file JSON itu dengan text editor, **copy semua isinya** (satu baris utuh).

### 2. Share spreadsheet ke service account

1. Buka file JSON tadi, cari field `"client_email"` (formatnya seperti
   `pln-bot-sheets@nama-project.iam.gserviceaccount.com`).
2. Buka spreadsheet kamu -> klik **Share** (kanan atas) -> paste email itu ->
   kasih akses **Editor** -> Send.

### 3. Buat API key OpenAI (untuk transkrip suara)

1. Buka [platform.openai.com/api-keys](https://platform.openai.com/api-keys) -> login/daftar.
2. Klik **Create new secret key**, copy key-nya (`sk-...`).
3. Isi saldo minimal (Billing -> Add payment method) — biayanya sangat kecil,
   Whisper ~$0.006/menit audio + GPT-4o-mini ~$0.0002/laporan.

### 4. Set environment variables di Railway

Buka project Railway -> service `bot` -> tab **Variables** -> tambahkan:

| Key | Value |
|---|---|
| `OPENAI_API_KEY` | `sk-...` dari langkah 3 |
| `SPREADSHEET_ID` | `1vXFzN8nktmBmHUzN9KqkpaIBhHUSmoaXCXQOMb2Q2MY` |
| `SHEET_NAME` | `Sheet1` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | isi mentah file JSON dari langkah 1 (satu baris utuh) |
| `BACKEND_URL` | URL Railway kamu, misal `https://pln-production-fd9d.up.railway.app` |

### 5. Push kode baru ke GitHub

Ganti `bot/bot.py` dan `bot/requirements.txt` di repo dengan versi baru.
Railway otomatis redeploy (karena auto-deploy sudah aktif).

### 6. Daftarkan webhook Telegram

Di komputermu (folder `bot/`, dengan `.env` lokal yang sudah berisi `BOT_TOKEN`
dan `BACKEND_URL`):
```
py setup_voice_webhook.py
```
Harus muncul `200 {'ok': True, 'result': True, ...}`.

> Catatan: mendaftarkan webhook untuk voice note ini terpisah dari menu button
> Mini App — keduanya bisa jalan bersamaan tanpa saling mengganggu.

### 7. Uji coba

1. Buka chat bot -> ketik `/start`, cek balasan instruksi muncul.
2. Share lokasi -> cek balasan "Lokasi tersimpan: ...".
3. Kirim voice note -> cek balasan ringkasan laporan muncul dalam beberapa detik,
   dan baris baru muncul di spreadsheet.

## Batasan & hal yang perlu disesuaikan lagi

- **Penyimpanan lokasi in-memory**: kalau server Railway restart/redeploy, cache
  lokasi hilang dan user perlu share lokasi ulang. Untuk versi lebih permanen,
  bisa dipindah ke database kecil (SQLite/Redis) — tinggal bilang kalau mau
  saya bantu upgrade ini.
- **Multi-user bersamaan**: kalau banyak petugas pakai bot yang sama, tiap
  laporan tetap tertulis ke baris kosong berikutnya secara berurutan (aman,
  tidak akan tertimpa), tapi tidak ada penanda "siapa yang lapor" di sheet saat
  ini — bisa ditambahkan kolom "Petugas" kalau perlu.
- **Akurasi ekstraksi AI**: kalau user bicara terlalu singkat/ambigu, hasil
  Kegiatan/Material bisa kurang tepat — user tetap bisa edit manual langsung di
  spreadsheet kalau ada yang meleset.
