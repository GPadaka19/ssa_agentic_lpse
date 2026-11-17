# Panduan Run SPSE dari OrbStack (fokus konstruksi)

Container: `spse` (image `spse-scraper`), selalu hidup, hasil tersimpan di
folder `output/` di Mac. Semua perintah dijalankan dari Terminal Mac, atau dari
OrbStack: Containers > `spse` > Terminal (lalu hilangkan awalan
`docker exec spse`).

## 0. Kelola container

```bash
docker ps --filter name=spse        # cek status
docker start spse / docker stop spse
docker exec -it spse bash           # masuk shell container
```

## 1. Cari slug instansi

```bash
docker exec spse python spse.py --list-agencies | grep -i "jawa timur"
```

Instansi padat konstruksi: `pu` (Kementerian PU), `dephub` (Perhubungan),
`nasional` (Kementerian Perumahan, OIKN, dll.), `kemhan`, `jakarta`,
`jabarprov`, `jatengprov`, `jatimprov`, `surabaya`, `bandung`, serta LPSE
kabupaten/kota (`...kab`, `...kota`).

## 2. Paket konstruksi saja (`konstruksi.py`)

Filter dilakukan sebelum download HTML, jadi jauh lebih cepat.

| Opsi | Arti |
|---|---|
| `--tipe` | `tender` (default), `nontender`, `pencatatan` |
| `--jenis` | `semua` (default), `pekerjaan` (kontraktor fisik), `konsultansi` (konsultan konstruksi) |
| `--min-pagu N` | Pagu minimal N miliar rupiah |
| `--kata "a\|b"` | Kata kunci di nama paket (regex) |
| `--dry` | Hanya hitung dan tampilkan 30 paket pertama |
| `--limit N` | Ambil N paket saja (uji coba) |
| `--refresh` | Ambil ulang daftar paket dari server; pakai untuk tahun berjalan |

```bash
# Peta pasar: berapa paket konstruksi PU 2025?
docker exec spse python konstruksi.py --agency pu --tahun 2025 --dry

# Tender pekerjaan fisik >= Rp50 M
docker exec spse python konstruksi.py --agency pu --tahun 2025 --jenis pekerjaan --min-pagu 50

# Paket jalan & jembatan
docker exec spse python konstruksi.py --agency pu --tahun 2025 --kata "jalan|jembatan|preservasi"

# Irigasi, bendungan, sungai, pantai
docker exec spse python konstruksi.py --agency pu --tahun 2025 --kata "irigasi|bendung|sungai|pantai|banjir"

# Gedung: sekolah, rumah sakit, kantor
docker exec spse python konstruksi.py --agency pu --tahun 2025 --kata "gedung|sekolah|rumah sakit|madrasah"

# Peluang konsultan (perencanaan / supervisi / MK)
docker exec spse python konstruksi.py --agency pu --tahun 2025 --jenis konsultansi

# Paket kecil (pengadaan langsung) konstruksi di pemda
docker exec spse python konstruksi.py --agency jatimprov --tipe nontender --tahun 2025 --jenis pekerjaan

# Pantau tahun berjalan
docker exec spse python konstruksi.py --agency jakarta --tahun 2026 --jenis pekerjaan --dry
```

Hasil: `output/konstruksi/<slug>/<tahun>/<slug>_<tahun>_<tipe>.csv` dan `.xlsx`.

## 3. Data lengkap (`spse.py`, tanpa filter)

```bash
docker exec spse python spse.py --agency pu --tipe tender --tahun 2025 --dry
docker exec spse python spse.py --agency pu --tipe tender --tahun 2025
docker exec spse python spse.py --agency pu --tipe nontender --tahun 2025
docker exec spse python spse.py --agency jakarta --tipe swakelola --tahun 2026
docker exec spse python spse.py --agency pu --tipe darurat --tahun 2025
```

Hasil: `output/<slug>/<tahun>/<slug>_<tahun>_<tipe>.csv` dan `.xlsx`.

## 4. Banyak instansi sekaligus

```bash
for a in pu dephub jabarprov jatengprov jatimprov; do
  docker exec spse python konstruksi.py --agency $a --tahun 2025 --jenis pekerjaan --min-pagu 10
done
```

## 5. Analisis yang bisa dibuat dari kolom hasil

Kolom berguna: `satuan_kerja`, `jenis_pengadaan`, `metode_pengadaan`, `pagu`,
`hps`, `lokasi`, `sumber_dana`, `nama_pemenang`, `npwp`, `harga_kontrak`,
`nilai_pdn`, `nilai_realisasi`. Satu paket = satu baris per peserta.

- **Pemetaan pesaing**: siapa paling sering menang, di instansi/wilayah mana
  (`nama_pemenang`, `npwp`, `lokasi`).
- **Analisis harga penawaran**: rasio `harga_kontrak` terhadap `hps` untuk
  menakar diskon pemenang.
- **Pipeline peluang**: tender tahun berjalan per satker dan pagu.
- **Profil satker**: satker mana punya anggaran konstruksi terbesar.
- **Jumlah peserta per paket**: tingkat persaingan tiap jenis pekerjaan.

## Catatan

- Jalankan ulang perintah yang sama untuk melanjutkan run yang terhenti.
- Scrape penuh instansi besar bisa berjam-jam; mulai dengan `--dry` atau
  `--limit`.
- Gunakan secara wajar; jangan menaikkan `--workers` terlalu tinggi.
