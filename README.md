# NALAR — Alur Solusi dan Panduan Menjalankan

NALAR membantu guru SMP kelas 7–8 memahami cara berpikir siswa melalui sesi penalaran singkat. Siswa menjelaskan alasannya, AI mengajukan pertanyaan pengarah tanpa memberikan jawaban akhir, lalu guru memperoleh laporan berbasis bukti dan peta miskonsepsi kelas untuk menentukan tindak lanjut pembelajaran.

Repositori ini berisi **backend FastAPI dan background worker**. Frontend dan layanan AI berada di repositori terpisah. Panduan ini mengikuti implementasi per 16 Oktober 2026; fitur orang tua sudah dihentikan.

## 1. Komponen solusi

| Komponen | Teknologi | Tanggung jawab |
|---|---|---|
| `Nalar_Frontend` | React, TypeScript, Vite | Antarmuka admin, guru, dan siswa |
| `Nalar_Backend` — API | Python 3.12+, FastAPI, asyncpg | Autentikasi, otorisasi, aturan bisnis, API, dan penulisan data aplikasi |
| `Nalar_Backend` — worker | Python, pgmq | Pengolahan materi, generasi misi, evaluasi, dan pekerjaan terjadwal |
| `Nalar_AI` | Python, FastAPI | Pemrosesan AI S1–S5; tidak mengakses database |
| Supabase | Postgres 17, Storage, Auth | Data, dokumen privat, kredensial, antrean pgmq, dan jadwal pg_cron |
| Redis | Redis | Sesi browser dan pembatasan permintaan autentikasi |

```text
Browser guru / siswa / admin
             |
     Frontend React + Vite
             |
       Backend FastAPI ------ Redis
          |       |
          |       +---------- AI service (interaksi S3)
          v
 Supabase Postgres / Storage / Auth
          |
       pgmq jobs
          |
        Worker -------------- AI service (pekerjaan latar belakang)
          |
          +------------------ Simpan hasil dan provenance AI
```

API dan worker memakai kode serta konfigurasi backend yang sama, tetapi berjalan sebagai dua proses. Pekerjaan AI yang panjang diproses melalui antrean; interaksi S3 saat sesi berlangsung tetap memerlukan respons langsung.

## 2. Alur penggunaan

1. **Admin menyiapkan sekolah.** Admin mengelola kelas, mata pelajaran, akun, dan penugasan guru. Guru dan siswa masuk melalui backend.
2. **Guru mengunggah materi.** PDF disimpan di bucket privat. Worker menjalankan S1 untuk mendeteksi bagian materi, mengekstrak konsep, dan menyusun kandidat miskonsepsi. Guru meninjau materi sebelum dipakai.
3. **Guru menyiapkan misi.** Guru membuat misi atau meminta S2 menyusun draf berdasarkan basis pengetahuan. Guru meninjau versi misi sebelum publikasi. Versi terpublikasi tidak dapat diubah.
4. **Guru membuka kegiatan.** Misi dipublikasikan ke kelas. Pada kegiatan langsung, siswa bergabung menggunakan kode dari guru.
5. **Siswa menjelaskan penalaran.** Backend menyimpan jawaban. S3 memilih pertanyaan lanjutan untuk menggali alasan siswa. Telemetri aktivitas menjadi bahan pemeriksaan integritas, bukan vonis otomatis.
6. **Worker mengevaluasi sesi.** Setelah sesi berakhir, pekerjaan evaluasi memanggil S4, menyimpan hasil dan bukti, serta menjalankan aturan integritas E1. Kunci idempotensi dan transisi status melindungi proses dari pengiriman ganda.
7. **Guru menentukan tindak lanjut.** Guru melihat laporan siswa, peta miskonsepsi, sinyal pendampingan, dan jejak perbaikan. Insight S5 serta rencana pembelajaran membantu menyiapkan intervensi berikutnya; hasil generasi disimpan. Jumlah siswa dan keanggotaan kelompok berasal dari database.

## 3. Prasyarat

- Python **3.12+** dan `uv`.
- Node.js **22.13+ pada seri 22, atau 24+**, serta npm.
- Docker Desktop aktif untuk Supabase lokal dan Redis.
- Ketiga repositori tersedia berdampingan untuk menjalankan solusi lengkap:

```text
Nalar/
├── Nalar_Backend/
├── Nalar_AI/
└── Nalar_Frontend/
```

Contoh berikut menggunakan **PowerShell**. Jalankan setiap layanan di terminal terpisah. Gunakan hostname `localhost` secara konsisten pada browser/frontend/backend agar cookie sesi tidak terpisah antar-host.

## 4. Menyiapkan backend dan database lokal

Dari direktori `Nalar_Backend`:

```powershell
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npx supabase start
npx supabase status
```

Ambil URL dan kunci **lokal** dari keluaran Supabase, kemudian isi `.env` backend:

```dotenv
NALAR_ENV=local
NALAR_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
NALAR_SUPABASE_URL=http://127.0.0.1:54321
NALAR_SUPABASE_SERVICE_ROLE_KEY=<service-role-key-lokal>
NALAR_SUPABASE_ANON_KEY=<anon-key-lokal>
NALAR_SUPABASE_JWT_SECRET=<jwt-secret-lokal>
NALAR_REDIS_URL=redis://127.0.0.1:6379
NALAR_AI_BASE_URL=http://127.0.0.1:8000
NALAR_AI_SERVICE_KEY=<kunci-bersama-backend-dan-ai>
NALAR_CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
NALAR_SEED_PASSWORD=<password-demo-minimal-8-karakter>
```

Ganti seluruh placeholder. Service role dan kunci AI tidak boleh dimasukkan ke frontend. Jangan gunakan kredensial produksi untuk panduan lokal ini.

Jalankan Redis:

```powershell
docker run --detach --name nalar-redis --publish 127.0.0.1:6379:6379 redis:7-alpine
```

Jika container sudah dibuat, gunakan `docker start nalar-redis`.

Untuk database lokal baru yang boleh dikosongkan, terapkan ulang seluruh migration:

```powershell
npx supabase db reset --local
```

**Reset menghapus data database lokal.** Jangan jalankan pada database berisi data yang perlu dipertahankan. Panduan ini tidak memerlukan migration ke Supabase hosted.

## 5. Menjalankan layanan AI

Di terminal baru, dari direktori `Nalar_AI`:

```powershell
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Untuk mencoba integrasi lokal tanpa provider berbayar, isi `.env` milik **Nalar_AI**:

```dotenv
NALAR_AI_ENVIRONMENT=local
NALAR_AI_SERVICE_KEY=<kunci-yang-sama-dengan-backend>
NALAR_AI_LLM_PROVIDER=fake
```

```powershell
uv run uvicorn nalar_ai.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Mode `fake` menyediakan keluaran simulasi untuk pengembangan; hasilnya tidak membuktikan mutu pedagogis AI. Untuk AI nyata, ikuti konfigurasi provider di `.env.example` repositori AI, termasuk kredensial dan batas biaya. Permintaan AI nyata dapat dikenai biaya.

## 6. Menyiapkan data demo

Dari direktori `Nalar_Backend`, setelah Supabase aktif dan `.env` terisi:

```powershell
uv run python scripts/seed_demo.py
```

Seed menyiapkan sekolah demo, kelas 8A, mata pelajaran IPA, 32 siswa, basis pengetahuan, dan misi **Gaya dan Gerak**. Seed memakai identitas data tetap agar dapat dijalankan ulang. Perintah menulis ke database dan Supabase Auth yang ditunjuk `.env`; pastikan keduanya mengarah ke lokal.

| Peran | Akun demo |
|---|---|
| Admin sekolah | `admin@demo.nalar.id` |
| Guru | `guru@demo.nalar.id` |
| Siswa | `siswa01@demo.nalar.id` sampai `siswa32@demo.nalar.id` |

Password akun demo baru mengikuti `NALAR_SEED_PASSWORD`. Seed dasar tidak memanggil AI; opsi `--warm` menghubungi AI untuk memvalidasi context pack.

## 7. Menjalankan API dan worker

Terminal API, dari `Nalar_Backend`:

```powershell
uv run uvicorn nalar.bootstrap.app:create_app --factory --host 127.0.0.1 --port 8080 --reload
```

Terminal worker, dari `Nalar_Backend`:

```powershell
uv run python -m nalar.bootstrap.worker
```

Worker harus tetap berjalan agar pengolahan materi, generasi misi, evaluasi, dan pekerjaan antrean selesai.

| Layanan | Alamat lokal |
|---|---|
| Backend API | `http://localhost:8080/api/v1` |
| Swagger backend | `http://localhost:8080/docs` |
| AI service | `http://127.0.0.1:8000` |
| Swagger AI | `http://127.0.0.1:8000/docs` |
| Supabase API | `http://127.0.0.1:54321` |
| Postgres | `127.0.0.1:54322` |

Periksa backend:

```powershell
Invoke-RestMethod http://localhost:8080/health
Invoke-RestMethod http://localhost:8080/health/ready
```

`/health` mengembalikan `status: ok`. `/health/ready` melaporkan kondisi database dan AI; status HTTP 200 ditentukan oleh ketersediaan database. Periksa nilai `ai` karena HTTP 200 tidak menjamin AI tersedia. Kedua endpoint berada di luar prefix `/api/v1`.

## 8. Menjalankan frontend

Di terminal baru, dari direktori `Nalar_Frontend`:

```powershell
npm ci
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
```

Isi `.env.local` frontend:

```dotenv
VITE_API_BASE_URL=http://localhost:8080/api/v1
```

```powershell
npm run dev -- --host localhost --port 5173
```

Buka `http://localhost:5173`, lalu masuk menggunakan akun demo. Isi alamat API secara eksplisit agar frontend menggunakan backend lokal. Jika port frontend berubah, sesuaikan `NALAR_CORS_ORIGINS` dan mulai ulang backend.

Untuk mencoba alur utama: masuk sebagai guru, pilih misi demo, publikasikan ke kelas 8A, buka kegiatan langsung, lalu masuk sebagai siswa di profil browser lain. Gunakan kode bergabung, kerjakan sesi hingga selesai, kemudian kembali ke akun guru untuk melihat hasil setelah worker selesai mengevaluasi.

## 9. Keamanan dan ketahanan

- Backend memeriksa lingkup akses setiap use case; objek di luar lingkup pengguna mengembalikan `404` (NFR-S9).
- Respons siswa tidak memuat rubrik, jawaban acuan, skor, atau flag internal (NFR-S8). Payload AI memakai ID dan teks tanpa nama, NISN, atau email (AI-8).
- Sesi browser memakai cookie HttpOnly. Permintaan tulis wajib membawa `Origin` yang diizinkan dan header `X-Nalar-CSRF: 1`; klien browser juga harus mengirim cookie.
- Setiap pemanggilan AI menyimpan provenance pada keberhasilan maupun kegagalan (AI-6). Timeout, retry, dan fallback ditangani sesuai jenis operasi.
- Kunci alami, deduplikasi pekerjaan, dan pembaruan status bersyarat menjaga pemrosesan aman terhadap retry dan pesan ganda (NFR-R1–R3).
- Materi berada di bucket privat. Backend menegakkan otorisasi meskipun koneksi service role melewati RLS.

## 10. Pemeriksaan pengembangan

Dari direktori backend:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run pytest
```

Tes integrasi memerlukan Supabase lokal dengan migration terbaru. `NALAR_TEST_DATABASE_URL` dapat diarahkan ke database uji lokal terpisah; jangan arahkan ke produksi. Hentikan worker aplikasi saat menjalankan tes integrasi pada database yang sama agar tidak mengambil pesan tes. Tes memakai AI palsu dan tidak memanggil provider berbayar.

Untuk tes unit yang tidak membutuhkan database:

```powershell
uv run pytest tests/unit -q
```

Kontrak backend ada di `openapi.json`; kontrak AI yang dipin ada di `contracts/nalar-ai.openapi.json`. Setelah perubahan route atau schema backend, jalankan `uv run python scripts/export_openapi.py`.

## 11. Pemecahan masalah

| Gejala | Pemeriksaan |
|---|---|
| Supabase tidak dapat dimulai | Pastikan Docker Desktop aktif dan port 54321/54322 tidak dipakai layanan lain. |
| Login gagal atau sesi tidak bertahan | Periksa Redis, kunci Supabase, password seed, pengiriman cookie, dan konsistensi hostname browser/API. |
| `CSRF_REJECTED` | Periksa `Origin`, `NALAR_CORS_ORIGINS`, dan header `X-Nalar-CSRF: 1`. Swagger memerlukan header yang sama untuk operasi tulis. |
| Pekerjaan terus menunggu | Periksa terminal worker, koneksi database, dan status pekerjaan melalui API jobs. |
| AI tidak tersedia | Periksa port 8000, URL AI, kesamaan service key, dan konfigurasi provider. |
| Tabel atau constraint tidak ditemukan | Pastikan migration terbaru diterapkan pada database lokal yang digunakan. |
| Port 8000 sudah dipakai | Gunakan 8080 untuk backend; 8000 dialokasikan untuk AI dalam panduan ini. |

## 12. Struktur backend dan deployment

```text
src/nalar/
├── domain/           Aturan penalaran, status, peta miskonsepsi, dan E1
├── application/      Use case dan port layanan
├── infrastructure/   Adapter database, AI, autentikasi, storage, dan antrean
├── presentation/     Router HTTP, schema, middleware, dan konsumen worker
└── bootstrap/        Settings, dependency injection, entry point API/worker
supabase/migrations/  Riwayat perubahan schema SQL
supabase/seed/        Materi dan data demo
contracts/           Kontrak AI
scripts/             Seed dan pembangkitan kontrak
tests/               Unit, integrasi, dan kontrak
```

Pada Railway, API dan worker berjalan sebagai layanan terpisah dari Docker image yang sama. Dockerfile memakai port dari variabel `PORT` dengan default 8080; perintah worker adalah `python -m nalar.bootstrap.worker`. Konfigurasi AI memakai alamat private network. Database hosted memakai session-mode pooler port 5432; untuk transaction pooler, atur `NALAR_DB_STATEMENT_CACHE_SIZE=0`.

Siapkan schema sebelum merilis kode yang bergantung padanya. Panduan lokal ini tidak menjalankan deployment atau perubahan database hosted. Target latensi dan kapasitas perlu dibuktikan dengan pengukuran pada lingkungan tujuan.

## 13. Environment staging — Railway

Konfigurasi berikut dipisahkan untuk layanan **API `Nalar_Backend`** dan **worker**. Masukkan melalui tab Variables pada layanan Railway yang sesuai. Ganti placeholder secret dengan nilai dari pengelola secret; contoh ini tidak berisi kredensial infrastruktur aktif.

### API — Nalar_Backend

```dotenv
NALAR_ENV="staging"
NALAR_DATABASE_URL="postgresql://postgres.jhqehdxyxlvndjskalwq:<DATABASE_PASSWORD>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
NALAR_SUPABASE_URL="https://jhqehdxyxlvndjskalwq.supabase.co"
NALAR_SUPABASE_SERVICE_ROLE_KEY="<SUPABASE_SERVICE_ROLE_KEY>"
NALAR_SUPABASE_ANON_KEY="<SUPABASE_ANON_KEY>"
NALAR_AI_BASE_URL="http://nalarai.railway.internal:8080"
NALAR_AI_SERVICE_KEY="<AI_SERVICE_KEY>"
NALAR_CORS_ORIGINS="[\"http://localhost:5173\",\"https://nalar-academy.vercel.app\"]"
NALAR_DB_POOL_MAX_SIZE="8"
NALAR_REDIS_URL="rediss://default:<REDIS_PASSWORD>@great-garfish-321431.upstash.io:6379"
NALAR_DEFAULT_PHASE="D"
NALAR_KB_BUILD_STALE_AFTER_S="3600"
NALAR_ACCOUNT_EMAIL_ENABLED="true"
NALAR_ACCOUNT_EMAIL_ACTIVATION_URL="https://nalar-academy.vercel.app/activate"
NALAR_MISSION_REVISION_ENABLED="true"
NALAR_REFERENCE_AI_DRAFT_ENABLED="true"
```

Jika mengisi `NALAR_CORS_ORIGINS` melalui kolom nilai tunggal Railway, gunakan JSON langsung: `["http://localhost:5173","https://nalar-academy.vercel.app"]`. Bentuk kutip dan escape di atas ditujukan untuk format dotenv/Raw Editor.

Alamat `nalarai.railway.internal` hanya dapat diakses dari jaringan privat Railway. Konfigurasi staging ini tidak dapat langsung menggantikan konfigurasi lokal pada bagian 4–8. Cookie staging memakai atribut Secure; pengujian browser staging harus memakai HTTPS dan konfigurasi domain/cookie yang sesuai. Entri CORS localhost saja tidak menjamin sesi browser lintas situs dapat dipakai.

### Worker

Referensi `${{Nalar_Backend.…}}` dibaca oleh Railway dari layanan API; sintaks ini tidak diekspansi oleh file `.env` lokal. Nama layanan API harus persis `Nalar_Backend`, atau sesuaikan referensinya.

```dotenv
NALAR_ENV="${{Nalar_Backend.NALAR_ENV}}"
NALAR_DATABASE_URL="${{Nalar_Backend.NALAR_DATABASE_URL}}"
NALAR_SUPABASE_URL="${{Nalar_Backend.NALAR_SUPABASE_URL}}"
NALAR_SUPABASE_SERVICE_ROLE_KEY="${{Nalar_Backend.NALAR_SUPABASE_SERVICE_ROLE_KEY}}"
NALAR_SUPABASE_ANON_KEY="${{Nalar_Backend.NALAR_SUPABASE_ANON_KEY}}"
NALAR_AI_BASE_URL="${{Nalar_Backend.NALAR_AI_BASE_URL}}"
NALAR_AI_SERVICE_KEY="${{Nalar_Backend.NALAR_AI_SERVICE_KEY}}"
NALAR_REDIS_URL="${{Nalar_Backend.NALAR_REDIS_URL}}"
NALAR_CORS_ORIGINS="[\"https://nalar-academy.vercel.app\"]"
NALAR_DB_POOL_MAX_SIZE="4"
NALAR_ACCOUNT_EMAIL_ENABLED="true"
NALAR_ACCOUNT_EMAIL_ACTIVATION_URL="https://nalar-academy.vercel.app/activate"
NALAR_ACCOUNT_EMAIL_AUTH_COOLDOWN_S="1800"
NALAR_ACCOUNT_EMAIL_DAILY_LIMIT="200"
NALAR_ACCOUNT_EMAIL_HOURLY_LIMIT="20"
NALAR_ACCOUNT_EMAIL_LINK_LIFETIME_S="3600"
NALAR_ACCOUNT_EMAIL_SENDER_SPACING_S="1"
NALAR_MISSION_REVISION_ENABLED="true"
NALAR_REFERENCE_AI_DRAFT_ENABLED="true"
```

Worker memakai pool maksimum 4 koneksi; API memakai 8. Hitung total koneksi berdasarkan jumlah replika kedua layanan. Flag revisi misi dan draf referensi AI memerlukan migration serta layanan AI yang kompatibel. Email aktivasi memerlukan konfigurasi pengiriman pada Supabase Auth; flag `true` tidak membuktikan email berhasil terkirim.

## 14. Akun uji staging

Buka [NALAR](https://nalar-academy.vercel.app), lalu masuk menggunakan salah satu akun berikut. Daftar ini diberikan oleh pengelola untuk UAT; keberhasilan login dan penugasan peran belum diverifikasi pada sesi dokumentasi ini. Akun ini berbeda dari akun seed lokal pada bagian 6.

| Peran | Email | Password |
|---|---|---|
| Platform admin | `platform.uat@uat.nalar.id` | `Uji-Nalar-2026!` |
| Admin sekolah | `kepsek.uat@uat.nalar.id` | `Uji-Nalar-2026!` |
| Guru | `bryanthanaya4@gmail.com` | `teacher123` |
| Siswa | `bryanthanaya2@gmail.com` | `student123` |

Gunakan platform admin untuk pengelolaan tingkat platform, admin sekolah untuk administrasi sekolah, guru untuk menyiapkan dan memantau pembelajaran, serta siswa untuk mengikuti sesi. Hak akses aktual mengikuti peran dan penugasan yang tersimpan pada database.

**Bagian akun uji ini hanya untuk distribusi terbatas.** Hapus password dari salinan README yang dipublikasikan dan bagikan kredensial melalui kanal privat. Rotasi secret infrastruktur yang telah dibagikan melalui chat; perbarui Railway dan layanan terkait setelah rotasi.
