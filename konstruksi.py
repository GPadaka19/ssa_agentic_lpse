"""Construction-only wrapper around spse.py.

Fetches the package list, keeps only construction packages (optionally by
minimum pagu and keyword), then downloads HTML and exports CSV/Excel for just
those packages.

HTML detail pages share the regular cache (output/<slug>/<tahun>/<tipe>/html),
so a package is downloaded once no matter how many filters select it. Each
filter combination gets its own CSV/XLSX under
output/konstruksi/<slug>/<tahun>/, so results of different filters never mix.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import spse

# Column holding "Jenis Pengadaan" and the short pagu text, per category.
# swakelola rows carry no jenis column, so it cannot be filtered.
COLS = {
    "tender": {"jenis": 8, "pagu": 4},
    "nontender": {"jenis": 6, "pagu": 4},
    "pencatatan": {"jenis": 5, "pagu": 3},
    "darurat": {"jenis": 6, "pagu": 3},
}
FILTERABLE = sorted(COLS)

# Short pagu text and current stage/status, for every category.
PAGU_COL = {"tender": 4, "nontender": 4, "pencatatan": 3, "swakelola": 3, "darurat": 3}
STATUS_COL = {"tender": 3, "nontender": 3, "pencatatan": 8, "swakelola": 7, "darurat": 7}
STATUS_CHOICES = ["semua", "berjalan", "selesai", "batal"]
URUT_CHOICES = ["terbaru", "pagu", "asli"]

JENIS = {
    # Physical works: Pekerjaan Konstruksi (+ Terintegrasi)
    "pekerjaan": lambda j: j.startswith("pekerjaan konstruksi"),
    # Consultancy: Jasa Konsultansi ... Konstruksi (not Non Konstruksi)
    "konsultansi": lambda j: j.startswith("jasa konsultansi")
    and "konstruksi" in j and "non konstruksi" not in j,
}
JENIS["semua"] = lambda j: JENIS["pekerjaan"](j) or JENIS["konsultansi"](j)
JENIS_CHOICES = sorted(JENIS)

UNIT = {"rb": 1e3, "jt": 1e6, "m": 1e9, "t": 1e12}


def pagu_short(value) -> float:
    """'143,8 M' -> 143.8e9; '47,3 Jt' -> 47.3e6; unparseable -> 0."""
    m = re.match(r"\s*([\d.,]+)\s*([A-Za-z]*)", str(value or ""))
    if not m:
        return 0.0
    try:
        num = float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0
    return num * UNIT.get(m.group(2).lower(), 1)


def _cell(row: list, index: int | None) -> str:
    if index is None or index >= len(row) or row[index] is None:
        return ""
    return spse.clean_text(spse.strip_tags(str(row[index])))


def status_group(text: str) -> str:
    """Bucket the SPSE stage text: batal (cancelled/failed), selesai, berjalan."""
    t = text.lower()
    if "batal" in t or "gagal" in t:
        return "batal"
    if "selesai" in t:
        return "selesai"
    return "berjalan"


def _kode_num(row: list, kategori: str) -> int:
    kode = _cell(row, spse.CATEGORIES[kategori]["id_index"])
    return int(kode) if kode.isdigit() else 0


def sort_rows(rows: list, kategori: str, urut: str = "terbaru") -> list:
    """terbaru: highest kode first (SPSE codes are issued sequentially, so this
    is newest-created first); pagu: largest pagu first; asli: server order."""
    if urut == "terbaru":
        return sorted(rows, key=lambda r: _kode_num(r, kategori), reverse=True)
    if urut == "pagu":
        return sorted(rows, key=lambda r: pagu_short(_cell(r, PAGU_COL[kategori])),
                      reverse=True)
    return list(rows)


def row_summary(row: list, kategori: str) -> dict:
    """Short, JSON-friendly view of one list.json row."""
    id_index = spse.CATEGORIES[kategori]["id_index"]
    pagu_text = _cell(row, PAGU_COL[kategori])
    status = _cell(row, STATUS_COL[kategori])
    return {
        "kode": _cell(row, id_index),
        "nama": _cell(row, 1),
        "instansi": _cell(row, 2),
        "jenis": _cell(row, COLS.get(kategori, {}).get("jenis")).split(" - TA")[0] or None,
        "pagu": pagu_text,
        "pagu_num": pagu_short(pagu_text) or None,
        "status": status or None,
        "status_grup": status_group(status) if status else None,
    }


def filter_rows(rows: list, kategori: str, jenis: str | None = "semua",
                min_pagu: float = 0.0, kata: str | None = None,
                status: str = "semua") -> list:
    """Keep matching rows. jenis=None skips the construction check (any
    category); min_pagu is in miliar rupiah; kata is a regex; status is one of
    STATUS_CHOICES."""
    if jenis is not None and kategori not in COLS:
        raise ValueError(f"kategori '{kategori}' tidak punya kolom jenis pengadaan")
    pattern = re.compile(kata, re.I) if kata else None
    keep = []
    for row in rows:
        if jenis is not None and not JENIS[jenis](_cell(row, COLS[kategori]["jenis"]).lower()):
            continue
        if min_pagu and pagu_short(_cell(row, PAGU_COL[kategori])) < min_pagu * 1e9:
            continue
        if pattern and not pattern.search(_cell(row, 1)):
            continue
        if status != "semua" and status_group(_cell(row, STATUS_COL[kategori])) != status:
            continue
        keep.append(row)
    return keep


def filter_key(jenis: str | None = "semua", min_pagu: float = 0.0,
               kata: str | None = None, status: str = "semua") -> str:
    """Filename-safe name for one filter combination."""
    parts = ["konstruksi" if jenis is not None else "paket"]
    if jenis not in (None, "semua"):
        parts.append(jenis)
    if status != "semua":
        parts.append(status)
    if min_pagu:
        parts.append(f"min{min_pagu:g}M")
    if kata:
        slug = re.sub(r"[^a-z0-9]+", "-", kata.lower()).strip("-")[:40]
        if slug:
            parts.append(slug)
    return "_".join(parts)


def load_cached_rows(full_dir: Path) -> list:
    """Rows from an earlier list.json; raises FileNotFoundError if none."""
    cache = full_dir / "list.json"
    if not cache.exists():
        raise FileNotFoundError(f"belum ada cache {cache}; jalankan sekali tanpa offline")
    return json.loads(cache.read_text(encoding="utf-8"))["data"]


def run_filtered(agency: dict, kategori: str, tahun: int, jenis: str | None = "semua",
                 min_pagu: float = 0.0, kata: str | None = None,
                 status: str = "semua", urut: str = "terbaru", limit: int = 0,
                 workers: int = spse.DEFAULT_WORKERS, excel: bool = True,
                 refresh: bool = False, offline: bool = False,
                 root: Path = spse.OUTPUT_ROOT,
                 cancel=None, progress=None, log=print) -> dict:
    """List -> filter -> download HTML -> CSV/XLSX for the matching packages.

    offline=True uses the cached list.json and only the HTML already on disk,
    so it re-exports without touching the network. Rows are ordered by urut
    before limit applies, so limit=5 with urut=terbaru takes the 5 newest.
    """
    base, slug = agency["base"], agency["slug"]
    nama = agency["names"][0] if agency["names"] else slug
    full_dir = spse.run_dir(slug, tahun, kategori, root)
    html_dir = full_dir / "html"

    if offline:
        rows = load_cached_rows(full_dir)
        log(f"offline: memakai {len(rows)} baris dari list.json")
    else:
        rows = spse.scrape_json(base, slug, kategori, tahun, full_dir,
                                force=refresh, log=log)
    keep = sort_rows(filter_rows(rows, kategori, jenis, min_pagu, kata, status),
                     kategori, urut)
    ids = spse.extract_ids(keep, kategori)
    if limit:
        ids = ids[:limit]
    log(f"{slug} {kategori} {tahun}: {len(rows)} paket -> {len(ids)} paket terpilih")

    stats = {"total": len(rows), "paket": len(ids), "files": 0,
             "csv_rows": 0, "csv": None, "xlsx": None, "cancelled": False}
    if not ids:
        return stats

    if not offline:
        html_progress = (lambda d, t: progress(d, t, "paket HTML")) if progress else None
        html_stats = spse.scrape_html(base, kategori, tahun, ids, html_dir,
                                      workers=workers, cancel=cancel,
                                      progress=html_progress, log=log)
        stats["files"] = html_stats["files"]
    if cancel is not None and cancel.is_set():
        stats["cancelled"] = True
        return stats

    out_dir = root / "konstruksi" / slug / str(tahun)
    csv_path = out_dir / f"{slug}_{tahun}_{kategori}_{filter_key(jenis, min_pagu, kata, status)}.csv"
    csv_progress = (lambda d, t: progress(d, t, "paket CSV")) if progress else None
    # export_csv walks every folder in a directory; give it a throwaway view
    # holding only the selected packages so other cached packages stay out.
    with tempfile.TemporaryDirectory() as tmp:
        view = Path(tmp)
        for paket_id in ids:
            src = html_dir / str(paket_id)
            if src.is_dir():
                (view / str(paket_id)).symlink_to(src.resolve(), target_is_directory=True)
        stats["csv_rows"] = spse.export_csv(view, csv_path, slug=slug,
                                            nama_instansi=nama, kategori=kategori,
                                            tahun=tahun, base=base, log=log,
                                            progress=csv_progress)
    stats["csv"] = str(csv_path)
    if excel:
        xlsx_progress = (lambda d, t: progress(d, t, "baris Excel")) if progress else None
        xlsx = spse.export_excel(csv_path, log=log, progress=xlsx_progress)
        stats["xlsx"] = str(xlsx) if xlsx else None
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape paket konstruksi saja")
    p.add_argument("--agency", required=True)
    p.add_argument("--tipe", choices=FILTERABLE, default="tender")
    p.add_argument("--tahun", type=int)
    p.add_argument("--jenis", choices=JENIS_CHOICES, default="semua",
                   help="pekerjaan = kontraktor, konsultansi = konsultan")
    p.add_argument("--min-pagu", type=float, default=0,
                   help="Pagu minimum dalam miliar rupiah, mis. 1 = Rp1 M")
    p.add_argument("--kata", help="Kata kunci di nama paket, mis. jalan|irigasi")
    p.add_argument("--status", choices=STATUS_CHOICES, default="semua",
                   help="berjalan = tender belum selesai/batal")
    p.add_argument("--urut", choices=URUT_CHOICES, default="terbaru")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=spse.DEFAULT_WORKERS)
    p.add_argument("--dry", action="store_true", help="Hanya hitung & tampilkan")
    p.add_argument("--no-excel", action="store_true")
    p.add_argument("--offline", action="store_true",
                   help="Export ulang dari cache tanpa network")
    p.add_argument("--refresh", action="store_true",
                   help="Ambil ulang daftar paket dari server (abaikan cache)")
    a = p.parse_args()

    agency = spse.match_agency(spse.load_agencies(), a.agency)
    if agency is None:
        print(f"Instansi '{a.agency}' tidak ditemukan.")
        return 3
    tahun = spse.resolve_tahun(a.tahun)

    if a.dry:
        full_dir = spse.run_dir(agency["slug"], tahun, a.tipe)
        rows = spse.scrape_json(agency["base"], agency["slug"], a.tipe, tahun,
                                full_dir, force=a.refresh)
        keep = sort_rows(filter_rows(rows, a.tipe, a.jenis, a.min_pagu, a.kata,
                                     a.status), a.tipe, a.urut)
        print(f"{agency['slug']} {a.tipe} {tahun}: {len(rows)} paket -> "
              f"{len(keep)} paket konstruksi ({a.jenis})")
        for row in keep[:30]:
            s = row_summary(row, a.tipe)
            print(f"  {s['kode']}  {s['pagu']:>9}  {s['status_grup'] or '':8}  {s['nama'][:80]}")
        if len(keep) > 30:
            print(f"  ... dan {len(keep) - 30} lainnya")
        return 0

    stats = run_filtered(agency, a.tipe, tahun, a.jenis, a.min_pagu, a.kata,
                         status=a.status, urut=a.urut, limit=a.limit, workers=a.workers,
                         excel=not a.no_excel, refresh=a.refresh,
                         offline=a.offline)
    print(f"Selesai: {stats['paket']} paket, {stats['csv_rows']} baris CSV")
    for key in ("csv", "xlsx"):
        if stats.get(key):
            print(f"{key.upper()}: {stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
