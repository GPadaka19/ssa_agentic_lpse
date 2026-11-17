"""Construction filter and filtered pipeline (network mocked)."""
import csv
from pathlib import Path

import pytest

import konstruksi
import spse


def tender_row(kode, nama, jenis, pagu, status="Tender Sudah Selesai"):
    row = [None] * 16
    row[0], row[1], row[2], row[3], row[4], row[8] = (
        kode, nama, "Kementerian PU", status, pagu, f"{jenis} - TA 2026")
    return row


ROWS = [
    tender_row("1", "Pembangunan Gedung Kantor", "Pekerjaan Konstruksi", "143,8 M",
               "Penetapan Pemenang"),
    tender_row("2", "Supervisi Gedung Kantor", "Jasa Konsultansi Badan Usaha Konstruksi", "3,3 M"),
    tender_row("3", "Konsultan Keuangan", "Jasa Konsultansi Badan Usaha Non Konstruksi", "1 M"),
    tender_row("4", "Pengadaan Laptop", "Pengadaan Barang", "500 Jt"),
    tender_row("5", "Preservasi Jalan <span class='badge'>Tender Ulang</span>",
               "Pekerjaan Konstruksi Terintegrasi", "1,6 T", "Tender Batal"),
]


@pytest.mark.parametrize("text,expected", [
    ("143,8 M", 143.8e9), ("47,3 Jt", 47.3e6), ("1,6 T", 1.6e12),
    ("350 Jt", 350e6), ("", 0.0), (None, 0.0), ("abc", 0.0),
])
def test_pagu_short(text, expected):
    assert konstruksi.pagu_short(text) == pytest.approx(expected)


def ids(rows):
    return [r[0] for r in rows]


def test_filter_jenis():
    assert ids(konstruksi.filter_rows(ROWS, "tender")) == ["1", "2", "5"]
    assert ids(konstruksi.filter_rows(ROWS, "tender", "pekerjaan")) == ["1", "5"]
    assert ids(konstruksi.filter_rows(ROWS, "tender", "konsultansi")) == ["2"]


def test_filter_min_pagu_and_kata():
    assert ids(konstruksi.filter_rows(ROWS, "tender", min_pagu=100)) == ["1", "5"]
    assert ids(konstruksi.filter_rows(ROWS, "tender", kata="gedung")) == ["1", "2"]
    # badge HTML in the name is stripped before matching
    assert ids(konstruksi.filter_rows(ROWS, "tender", kata="badge")) == []


def test_filter_rejects_swakelola():
    with pytest.raises(ValueError):
        konstruksi.filter_rows(ROWS, "swakelola")


def test_filter_key():
    assert konstruksi.filter_key() == "konstruksi"
    assert konstruksi.filter_key(None, status="berjalan") == "paket_berjalan"
    assert (konstruksi.filter_key("pekerjaan", 50, "fasad|gedung")
            == "konstruksi_pekerjaan_min50M_fasad-gedung")


def test_row_summary_strips_badge():
    s = konstruksi.row_summary(ROWS[4], "tender")
    assert s["nama"] == "Preservasi Jalan Tender Ulang"
    assert s["jenis"] == "Pekerjaan Konstruksi Terintegrasi"
    assert s["pagu_num"] == pytest.approx(1.6e12)


@pytest.fixture
def mocked_network(monkeypatch):
    seen = {}

    def fake_scrape_json(base, slug, kategori, tahun, out_dir, force=False, log=print):
        seen["force"] = force
        return ROWS

    def fake_scrape_html(base, kategori, tahun, ids, out_dir, workers=8,
                         cancel=None, progress=None, log=print):
        # A package from an earlier, different filter is already cached.
        for paket_id in [*ids, "999"]:
            (out_dir / paket_id).mkdir(parents=True, exist_ok=True)
        return {"files": len(ids), "failed": []}

    def fake_export_csv(packages_dir, out_path, **kwargs):
        seen["exported"] = sorted(p.name for p in Path(packages_dir).iterdir())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("x", encoding="utf-8")
        return len(seen["exported"])

    monkeypatch.setattr(spse, "scrape_json", fake_scrape_json)
    monkeypatch.setattr(spse, "scrape_html", fake_scrape_html)
    monkeypatch.setattr(spse, "export_csv", fake_export_csv)
    monkeypatch.setattr(spse, "export_excel", lambda *a, **k: None)
    return seen


AGENCY = {"slug": "pu", "base": "https://spse.inaproc.id/pu", "names": ["PU"]}


def test_run_filtered_exports_only_selected(tmp_path, mocked_network):
    stats = konstruksi.run_filtered(AGENCY, "tender", 2026, jenis="pekerjaan",
                                    refresh=True, root=tmp_path, log=lambda *a: None)
    assert mocked_network["force"] is True
    assert mocked_network["exported"] == ["1", "5"]           # not the cached 999
    assert stats["total"] == 5 and stats["paket"] == 2
    assert stats["csv"].endswith("konstruksi/pu/2026/pu_2026_tender_konstruksi_pekerjaan.csv")
    # HTML lands in the shared cache, not a per-filter copy
    assert (tmp_path / "pu" / "2026" / "tender" / "html" / "1").is_dir()


def test_run_filtered_no_match(tmp_path, mocked_network):
    stats = konstruksi.run_filtered(AGENCY, "tender", 2026, kata="tidakada",
                                    root=tmp_path, log=lambda *a: None)
    assert stats["paket"] == 0 and stats["csv"] is None


def test_filter_status():
    assert ids(konstruksi.filter_rows(ROWS, "tender", status="berjalan")) == ["1"]
    assert ids(konstruksi.filter_rows(ROWS, "tender", status="batal")) == ["5"]
    assert ids(konstruksi.filter_rows(ROWS, "tender", status="selesai")) == ["2"]


def test_filter_without_jenis_works_for_any_category():
    assert len(konstruksi.filter_rows(ROWS, "tender", jenis=None)) == 5
    swakelola = [["9", "Rehab Gedung", "DKI", "66 Jt", "5", "2026", "5", "Paket Sudah Selesai"]]
    assert ids(konstruksi.filter_rows(swakelola, "swakelola", jenis=None, kata="gedung")) == ["9"]


def test_sort_rows():
    rows = [tender_row("10095208000", "a", "Pekerjaan Konstruksi", "1 M"),
            tender_row("10181495000", "b", "Pekerjaan Konstruksi", "5 M"),
            tender_row("4711064", "c", "Pekerjaan Konstruksi", "9 M")]
    assert ids(konstruksi.sort_rows(rows, "tender", "terbaru")) == ["10181495000", "10095208000", "4711064"]
    assert ids(konstruksi.sort_rows(rows, "tender", "pagu")) == ["4711064", "10181495000", "10095208000"]
    assert ids(konstruksi.sort_rows(rows, "tender", "asli")) == ["10095208000", "10181495000", "4711064"]


def test_row_summary_status():
    s = konstruksi.row_summary(ROWS[0], "tender")
    assert s["status"] == "Penetapan Pemenang" and s["status_grup"] == "berjalan"
