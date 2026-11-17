"""Construction-only wrapper around spse.py.

Fetches the package list, keeps only construction packages (optionally by
minimum pagu and keyword), then downloads HTML and exports CSV/Excel for just
those. Output goes to output/konstruksi/<slug>/<tahun>/ so it never mixes with
full runs.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import spse

# Column holding "Jenis Pengadaan" and the short pagu text, per category.
COLS = {
    "tender": {"jenis": 8, "pagu": 4},
    "nontender": {"jenis": 6, "pagu": 4},
    "pencatatan": {"jenis": 5, "pagu": 3},
}

JENIS = {
    # Physical works: Pekerjaan Konstruksi (+ Terintegrasi)
    "pekerjaan": lambda j: j.startswith("pekerjaan konstruksi"),
    # Consultancy: Jasa Konsultansi ... Konstruksi (not Non Konstruksi)
    "konsultansi": lambda j: j.startswith("jasa konsultansi")
    and "konstruksi" in j and "non konstruksi" not in j,
}
JENIS["semua"] = lambda j: JENIS["pekerjaan"](j) or JENIS["konsultansi"](j)

UNIT = {"rb": 1e3, "jt": 1e6, "m": 1e9, "t": 1e12}


def pagu_short(value) -> float:
    """'143,8 M' -> 143.8e9; '47,3 Jt' -> 47.3e6."""
    m = re.match(r"\s*([\d.,]+)\s*([A-Za-z]*)", str(value or ""))
    if not m:
        return 0.0
    num = float(m.group(1).replace(".", "").replace(",", "."))
    return num * UNIT.get(m.group(2).lower(), 1)


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape paket konstruksi saja")
    p.add_argument("--agency", required=True)
    p.add_argument("--tipe", choices=sorted(COLS), default="tender")
    p.add_argument("--tahun", type=int)
    p.add_argument("--jenis", choices=sorted(JENIS), default="semua",
                   help="pekerjaan = kontraktor, konsultansi = konsultan")
    p.add_argument("--min-pagu", type=float, default=0,
                   help="Pagu minimum dalam miliar rupiah, mis. 1 = Rp1 M")
    p.add_argument("--kata", help="Kata kunci di nama paket, mis. jalan|irigasi")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=spse.DEFAULT_WORKERS)
    p.add_argument("--dry", action="store_true", help="Hanya hitung & tampilkan")
    p.add_argument("--no-excel", action="store_true")
    p.add_argument("--refresh", action="store_true",
                   help="Ambil ulang daftar paket dari server (abaikan cache)")
    a = p.parse_args()

    agency = spse.match_agency(spse.load_agencies(), a.agency)
    if agency is None:
        print(f"Instansi '{a.agency}' tidak ditemukan.")
        return 3
    tahun = spse.resolve_tahun(a.tahun)
    cols = COLS[a.tipe]

    full_dir = spse.run_dir(agency["slug"], tahun, a.tipe)
    rows = spse.scrape_json(agency["base"], agency["slug"], a.tipe, tahun, full_dir,
                            force=a.refresh)

    kata = re.compile(a.kata, re.I) if a.kata else None
    keep = []
    for r in rows:
        jenis = spse.clean_text(str(r[cols["jenis"]] or "")).lower()
        if not JENIS[a.jenis](jenis):
            continue
        if pagu_short(r[cols["pagu"]]) < a.min_pagu * 1e9:
            continue
        if kata and not kata.search(spse.strip_tags(str(r[1] or ""))):
            continue
        keep.append(r)

    print(f"{agency['slug']} {a.tipe} {tahun}: {len(rows)} paket -> "
          f"{len(keep)} paket konstruksi ({a.jenis})")
    if a.dry:
        for r in keep[:30]:
            print(f"  {r[0]}  {str(r[cols['pagu']]):>9}  "
                  f"{spse.strip_tags(str(r[1]))[:90]}")
        if len(keep) > 30:
            print(f"  ... dan {len(keep) - 30} lainnya")
        return 0
    if not keep:
        return 0

    root = Path("output/konstruksi")
    out_dir = spse.run_dir(agency["slug"], tahun, a.tipe, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "list.json").write_text(
        json.dumps({"data": keep}, ensure_ascii=False), encoding="utf-8")

    stats = spse.run_pipeline(agency, a.tipe, tahun, do_json=False,
                              workers=a.workers, excel=not a.no_excel,
                              limit=a.limit, root=root)
    print(f"Selesai: {stats['paket']} paket, {stats['csv_rows']} baris CSV")
    if stats.get("xlsx"):
        print(f"Excel: {stats['xlsx']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
