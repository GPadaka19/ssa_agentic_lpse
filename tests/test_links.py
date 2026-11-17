"""buka_url: exported links go through the API bounce page, clickable in Excel."""
import csv

from openpyxl import load_workbook

import spse

SUMBER = "https://spse.inaproc.id/pu/lelang/10181495000/pengumumanlelang"


def test_buka_link_default_domain(monkeypatch):
    monkeypatch.delenv("SPSE_PUBLIC_URL", raising=False)
    assert spse.buka_link(SUMBER) == \
        "https://data-spse.latindo.ai/spse/pu/lelang/10181495000/pengumumanlelang"


def test_buka_link_env_and_explicit(monkeypatch):
    monkeypatch.setenv("SPSE_PUBLIC_URL", "http://localhost:8080/")
    assert spse.buka_link(SUMBER).startswith("http://localhost:8080/spse/pu/")
    assert spse.buka_link(SUMBER, "https://x.test").startswith("https://x.test/spse/")
    q = "https://spse.inaproc.id/pu/pencatatan/pengumumannonspk?id=1"
    assert spse.buka_link(q).endswith("/spse/pu/pencatatan/pengumumannonspk?id=1")


def test_buka_link_ignores_non_spse():
    assert spse.buka_link("") == ""
    assert spse.buka_link("https://example.com/x") == ""


def test_build_rows_fills_buka_url(monkeypatch):
    monkeypatch.delenv("SPSE_PUBLIC_URL", raising=False)
    detail = {"fields": {}, "named_tables": {}, "tables": [], "tabs": []}
    [row] = spse.build_rows(detail, slug="pu", nama_instansi="PU", kategori="tender",
                            tahun=2026, paket_id="10181495000", sumber_url=SUMBER)
    assert row["sumber_url"] == SUMBER
    assert row["buka_url"].startswith("https://data-spse.latindo.ai/spse/")
    assert spse.CSV_COLUMNS.index("buka_url") == spse.CSV_COLUMNS.index("sumber_url") + 1


def test_excel_buka_url_is_a_hyperlink(tmp_path):
    link = spse.buka_link(SUMBER, "https://data-spse.latindo.ai")
    csv_path = tmp_path / "pu_2026_tender.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="|")
        writer.writerow(["kode_paket", "sumber_url", "buka_url"])
        writer.writerow(["10181495000", SUMBER, link])
        writer.writerow(["2", SUMBER, ""])

    xlsx = spse.export_excel(csv_path, log=lambda *a: None)
    rows = list(load_workbook(xlsx)["data"].values)
    assert rows[0] == ("kode_paket", "sumber_url", "buka_url")
    assert rows[1][2] == f'=HYPERLINK("{link}","{link}")'
    assert rows[1][1] == SUMBER          # original link left as plain text
    assert rows[2][2] is None            # empty link stays empty
