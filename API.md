# SPSE Scraper API

HTTP API di atas `spse.py` dan `konstruksi.py`. Scrape berjalan sebagai job di
background, jadi request langsung kembali dan progresnya dipantau lewat
endpoint job. Kode scraper asli (`spse.py`) tidak diubah.

## Menjalankan

```bash
docker compose up -d --build        # build + jalankan di port 8080
docker compose logs -f              # lihat log server
docker compose down                 # hentikan
```

- API: `http://localhost:8080`
- Dokumentasi interaktif (Swagger, bisa dicoba langsung dari browser):
  `http://localhost:8080/docs`
- Tanpa Docker: `uv run uvicorn api.main:app --port 8080`

Hasil tersimpan di `output/` di host, sama seperti CLI. CLI tetap bisa dipakai:
`docker exec spse python spse.py ...`.

### Environment

| Variabel | Default | Arti |
|---|---|---|
| `SPSE_API_KEY` | kosong | Kalau diisi, semua endpoint kecuali `/health` butuh header `X-API-Key` |
| `SPSE_MAX_JOBS` | `2` | Jumlah job scrape yang berjalan bersamaan; sisanya antre |
| `SPSE_OUTPUT` | `output` | Root folder output |

Key disimpan di file `.env` (tidak ikut git), dibaca otomatis oleh
`docker compose`:

```bash
echo "SPSE_API_KEY=$(openssl rand -hex 24)" > .env
docker compose up -d
```

Di Swagger UI (`/docs`), klik tombol **Authorize**, isi key sekali, dan semua
request dari halaman itu otomatis membawa header `X-API-Key`.

## Endpoint

| Method | Path | Fungsi |
|---|---|---|
| GET | `/health` | Cek server hidup |
| GET | `/categories` | Daftar tipe dan apakah filter konstruksi tersedia |
| GET | `/agencies?q=surabaya` | Cari instansi (slug + nama) |
| GET | `/agencies/{slug-atau-nama}` | Resolve satu instansi |
| POST | `/preview` | Hitung dan tampilkan paket, tanpa download detail (cepat) |
| POST | `/jobs` | Mulai scrape di background (202 + objek job) |
| GET | `/jobs?status=running` | Daftar job |
| GET | `/jobs/{id}?log=50` | Status, progres, hasil, dan log terakhir |
| POST | `/jobs/{id}/cancel` | Batalkan job |
| GET | `/jobs/{id}/download/csv` | Unduh CSV hasil |
| GET | `/jobs/{id}/download/xlsx` | Unduh Excel hasil |
| GET | `/jobs/{id}/packages?q=&offset=&limit=` | Hasil sebagai JSON, satu entri per paket + peserta |

### Body `/preview` dan `/jobs`

```json
{
  "agency": "pu",
  "tipe": "tender",
  "tahun": 2026,
  "konstruksi": {
    "jenis": "pekerjaan",
    "min_pagu": 10,
    "kata": "gedung|fasad"
  },
  "refresh": false
}
```

| Field | Default | Arti |
|---|---|---|
| `agency` | wajib | Slug LPSE atau nama instansi |
| `tipe` | `tender` | `tender`, `nontender`, `pencatatan`, `swakelola`, `darurat` |
| `tahun` | tahun berjalan | Tahun anggaran |
| `konstruksi` | `null` | Isi untuk menyaring paket konstruksi saja; `null` = semua paket. Tidak tersedia untuk `swakelola` |
| `konstruksi.jenis` | `semua` | `pekerjaan` (kontraktor fisik), `konsultansi` (konsultan konstruksi), `semua` |
| `konstruksi.min_pagu` | `0` | Pagu minimum dalam miliar rupiah |
| `konstruksi.kata` | `null` | Regex kata kunci di nama paket (tidak peka huruf besar/kecil) |
| `refresh` | `false` | Ambil ulang daftar paket dari server; pakai untuk tahun berjalan |
| `max_items` | `50` | Khusus `/preview`: jumlah paket yang dikembalikan |
| `limit` | `0` | Khusus `/jobs`: batasi N paket (0 = semua) |
| `workers` | `8` | Khusus `/jobs`: paralel download HTML (1-16) |
| `excel` | `true` | Khusus `/jobs`: tulis `.xlsx` juga |
| `offline` | `false` | Khusus `/jobs`: export ulang dari data yang sudah ada, tanpa network |

### Status job

`queued` -> `running` -> `done` | `failed` | `cancelled`. Job yang sedang
berjalan saat server mati muncul sebagai `interrupted` setelah restart; kirim
ulang request yang sama dan scrape lanjut dari cache HTML.

## Contoh (curl)

```bash
B=http://localhost:8080

# Berapa tender pekerjaan konstruksi >= Rp100 M di PU tahun ini?
curl -s -X POST $B/preview -H 'Content-Type: application/json' \
  -d '{"agency":"pu","konstruksi":{"jenis":"pekerjaan","min_pagu":100},"refresh":true}'

# Mulai scrape tender gedung PU 2025
curl -s -X POST $B/jobs -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"kata":"gedung"}}'
# -> {"id":"b14559526b90","status":"queued",...}

curl -s $B/jobs/b14559526b90                       # progres
curl -s "$B/jobs/b14559526b90/packages?q=nindya"   # hasil JSON
curl -OJ $B/jobs/b14559526b90/download/xlsx        # unduh Excel
```

## Padanan perintah CLI

Siapkan dulu variabel berikut di terminal (key ada di `.env`):

```bash
B=http://localhost:8080
K=$(sed -n 's/^SPSE_API_KEY=//p' .env)
```

| CLI | API |
|---|---|
| `spse.py --list-agencies` | `GET /agencies?limit=1000` |
| `spse.py ... --dry` | `POST /preview` |
| `spse.py --agency A --tipe T --tahun Y` | `POST /jobs` |
| `spse.py ... --limit 5` | `POST /jobs` dengan `"limit": 5` |
| `spse.py ... --skip-json --skip-html` | `POST /jobs` dengan `"offline": true` |
| `spse.py ... --no-excel` | `POST /jobs` dengan `"excel": false` |
| `konstruksi.py ... --jenis/--min-pagu/--kata` | field `"konstruksi": {...}` di `/preview` atau `/jobs` |
| `konstruksi.py ... --refresh` | `"refresh": true` |

```bash
# Daftar instansi
curl -s -H "X-API-Key: $K" "$B/agencies?limit=1000"

# Cek jumlah paket saja
curl -s -X POST $B/preview -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"jakarta","tipe":"swakelola","tahun":2026}'

# Scrape lengkap (CSV + xlsx masuk ke ./output)
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"kemkes","tipe":"tender","tahun":2025}'

# Uji kecil dulu: 5 paket
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"kemkes","tipe":"tender","tahun":2025,"limit":5}'

# Export ulang tanpa network
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"kemkes","tipe":"tender","tahun":2025,"offline":true}'
```

Konstruksi:

```bash
# Hitung paket konstruksi + tampilkan daftar
curl -s -X POST $B/preview -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{}}'

# Pekerjaan fisik (kontraktor) dengan pagu >= Rp50 M
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"jenis":"pekerjaan","min_pagu":50}}'

# Per bidang lewat kata kunci
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"kata":"jalan|jembatan|preservasi"}}'
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"kata":"irigasi|bendung|sungai|banjir"}}'
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"kata":"gedung|sekolah|rumah sakit"}}'

# Peluang konsultan (perencanaan, supervisi, MK)
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"pu","tahun":2025,"konstruksi":{"jenis":"konsultansi"}}'

# Paket kecil (pengadaan langsung) di pemda
curl -s -X POST $B/jobs -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"agency":"jatimprov","tipe":"nontender","tahun":2025,"konstruksi":{"jenis":"pekerjaan"}}'
```

Setiap `POST /jobs` mengembalikan `id`. Lanjutkan dengan:

```bash
curl -s -H "X-API-Key: $K" $B/jobs/<id>                      # progres
curl -s -H "X-API-Key: $K" $B/jobs/<id>/packages             # hasil JSON
curl -s -OJ -H "X-API-Key: $K" $B/jobs/<id>/download/xlsx    # unduh Excel
```

## Bentuk hasil `/jobs/{id}/packages`

```json
{
  "total": 4,
  "offset": 0,
  "items": [{
    "kode_paket": "10074821000",
    "nama_paket": "Pembangunan Gedung Menara Fakultas Teknik ...",
    "satuan_kerja": "...", "jenis_pengadaan": "Pekerjaan Konstruksi",
    "pagu_num": 1.2e11, "hps_num": 1.1e11, "lokasi": "...",
    "sumber_url": "https://spse.inaproc.id/pu/lelang/10074821000/pengumumanlelang",
    "jumlah_peserta": 1,
    "nama_disamarkan": false,
    "peserta": [{"nama": "PT. NINDYA KARYA (Persero)", "npwp": "...", "harga_kontrak_num": null}]
  }]
}
```

`nama_disamarkan: true` berarti SPSE masih menampilkan peserta sebagai
"Peserta 1", "Peserta 2", ... (tender belum selesai); nama asli baru muncul
setelah evaluasi.

## Catatan

- `sumber_url` menolak dibuka langsung ("Akses Ditolak") kalau browser tidak
  mengirim header `Referer`. Buka dari halaman web lain, atau cari kode paket
  di halaman daftar SPSE.
- Satu `list.json` per instansi/tahun/tipe dikunci per job, jadi dua job untuk
  data yang sama berjalan bergantian, bukan bersamaan.
- HTML detail paket di-cache di `output/<slug>/<tahun>/<tipe>/html/` dan
  dipakai bersama oleh semua job, dengan atau tanpa filter.
