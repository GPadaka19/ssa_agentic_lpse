# Prompt: Tender fasad / gedung terbaru

Prompt tugas untuk AI executive assistant PT Ardhana Utama Sejahtera. Pakai
bersama `AI_ASSISTANT.md` (panduan API). Bisa dikirim manual atau dijadwalkan
rutin (mis. setiap pagi hari kerja).

Variabel yang bisa diganti sebelum dikirim:

| Variabel | Default |
|---|---|
| `{{TAHUN}}` | tahun berjalan |
| `{{LPSE}}` | `pu, nasional, jakarta, jabarprov, jatengprov, jatimprov, dephub, surabaya` |
| `{{KATA}}` | `fasad\|facade\|curtain ?wall\|cladding\|gedung` |
| `{{MIN_PAGU}}` | `0` (miliar rupiah) |
| `{{JUMLAH}}` | `15` |

---

## Prompt

Cari tender pemerintah terbaru yang terkait pekerjaan fasad dan gedung, lalu
laporkan ke saya dalam bentuk ringkas.

Gunakan SPSE Scraper API sesuai panduan `AI_ASSISTANT.md`.

Langkah:

1. Untuk setiap LPSE di daftar ini: {{LPSE}}, panggil `POST /preview` dengan
   body:

   ```json
   {
     "agency": "<slug>",
     "tipe": "tender",
     "tahun": {{TAHUN}},
     "konstruksi": {"jenis": "semua", "min_pagu": {{MIN_PAGU}}, "kata": "{{KATA}}"},
     "status": "berjalan",
     "urut": "terbaru",
     "refresh": true,
     "max_items": 50
   }
   ```

   `status: "berjalan"` membuang tender yang sudah selesai, batal, atau gagal.
   `refresh: true` wajib supaya tender yang baru tayang hari ini ikut.
   Kalau satu LPSE error, catat dan lanjut ke LPSE berikutnya.

2. Gabungkan semua `packages`, buang duplikat berdasarkan `kode`, lalu
   urutkan dari `kode` terbesar (paling baru dibuat). Ambil {{JUMLAH}} teratas.

3. Kalau hasilnya 0, ulangi langkah 1 dengan `tipe: "nontender"`, lalu
   laporkan bahwa yang ditemukan adalah paket pengadaan langsung (nilai kecil).

4. Untuk paket teratas (dahulukan yang masih bisa diikuti, lihat langkah 5)
   yang paling relevan dengan fasad (nama memuat fasad,
   facade, curtain wall, atau cladding; kalau tidak ada, 5 paket dengan pagu
   terbesar), ambil detailnya dengan `POST /jobs`:

   ```json
   {
     "agency": "<slug>",
     "tipe": "tender",
     "tahun": {{TAHUN}},
     "konstruksi": {"kata": "{{KATA}}"},
     "status": "berjalan",
     "urut": "terbaru",
     "limit": 10
   }
   ```

   Pantau `GET /jobs/<id>` setiap 10-15 detik sampai `done`, lalu ambil
   `GET /jobs/<id>/packages?urut=terbaru` untuk HPS, lokasi, satuan kerja,
   tanggal pembuatan, dan jumlah peserta. Satu job per LPSE yang punya
   paket terpilih; jangan lebih dari 2 job berjalan bersamaan.

5. Kelompokkan setiap paket berdasarkan field `status` (tahap saat ini).
   `status: "berjalan"` di API hanya berarti belum selesai dan tidak batal,
   jadi masih mencakup tahap akhir:

   | Kelompok | Tahap yang memuat kata |
   |---|---|
   | **Masih bisa diikuti** | Pengumuman, Pendaftaran, Download Dokumen, Pemberian Penjelasan, Upload / Pemasukan Dokumen Penawaran |
   | **Evaluasi** | Pembukaan, Evaluasi, Pembuktian, Klarifikasi, Negosiasi |
   | **Hampir selesai** | Penetapan Pemenang, Pengumuman Pemenang, Masa Sanggah, Surat Penunjukan, Penandatanganan Kontrak |
   | **Tidak jelas** | Tidak Ada Jadwal, atau tahap lain di luar daftar |

6. Susun laporan dengan format di bawah. Paket "Masih bisa diikuti"
   didahulukan, lalu Evaluasi, lalu sisanya.

Format laporan:

**Ringkasan** (2-3 kalimat): berapa tender fasad/gedung yang masih berjalan
di LPSE yang dicek, berapa yang **masih bisa diikuti**, berapa yang benar-benar
terkait fasad, dan satu paket paling menarik (nilai terbesar atau paling
relevan).

**Tabel tender terbaru** (maks {{JUMLAH}} baris, terbaru di atas):

| No | Kelompok | Kode | Nama paket | Instansi / LPSE | Pagu | HPS | Lokasi | Tahap | Peserta | Link |
|---|---|---|---|---|---|---|---|---|---|---|

- Pagu dan HPS dalam format Rp166,3 M. Kosongkan HPS / Lokasi / Peserta kalau
  paket itu tidak masuk langkah 4.
- Tandai paket yang terkait langsung fasad (fasad, facade, curtain wall,
  cladding) dengan "[FASAD]" di awal nama.
- Tahap diambil dari field `status` (mis. "Pengumuman Pascakualifikasi",
  "Penetapan Pemenang").

**Catatan**:
- LPSE yang dicek dan jumlah hasil per LPSE, termasuk yang 0 atau error.
- Waktu pengambilan data (tanggal dan jam).
- Nama peserta belum dibuka SPSE selama tender berjalan; jangan sebutkan
  "Peserta 1", "Peserta 2" sebagai nama perusahaan.
- Kolom Link berisi `buka_url` dari hasil API (jangan `sumber_url`, karena
  link asli SPSE menampilkan "Akses Ditolak" kalau diklik dari luar SPSE).

Jangan mengarang paket, angka, atau nama perusahaan yang tidak ada di hasil
API. Kalau ada yang tidak pasti, tulis "belum tersedia".

Tawarkan di akhir: file Excel lengkap (`GET /jobs/<id>/download/xlsx`) atau
memperluas pencarian ke LPSE lain / tahun sebelumnya untuk analisis pesaing.
