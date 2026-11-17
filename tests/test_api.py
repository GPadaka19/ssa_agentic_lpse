"""HTTP API (network mocked)."""
import csv
import json
import time

import pytest
from fastapi.testclient import TestClient

import spse
from api.jobs import JobManager
from api.main import create_app
from tests.test_konstruksi import ROWS


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=spse.CSV_COLUMNS, delimiter="|",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def network(monkeypatch):
    def fake_scrape_json(base, slug, kategori, tahun, out_dir, force=False, log=print):
        return ROWS

    def fake_scrape_html(base, kategori, tahun, ids, out_dir, workers=8,
                         cancel=None, progress=None, log=print):
        for paket_id in ids:
            (out_dir / paket_id).mkdir(parents=True, exist_ok=True)
        if progress:
            progress(len(ids), len(ids))
        return {"files": len(ids), "failed": []}

    def fake_export_csv(packages_dir, out_path, **kwargs):
        write_csv(out_path, [
            {"kode_paket": "1", "nama_paket": "Pembangunan Gedung Kantor",
             "satuan_kerja": "SATKER A", "pagu_num": "143800000000.0",
             "nama_pemenang": "PT Beton Jaya", "npwp": "01.234",
             "harga_kontrak_num": "120000000000.0",
             "sumber_url": "https://spse.inaproc.id/pu/lelang/1/pengumumanlelang"},
            {"kode_paket": "1", "nama_paket": "Pembangunan Gedung Kantor",
             "nama_pemenang": "PT Baja Abadi"},
            {"kode_paket": "5", "nama_paket": "Preservasi Jalan",
             "nama_pemenang": "Peserta 1"},
            {"kode_paket": "5", "nama_paket": "Preservasi Jalan",
             "nama_pemenang": "Peserta 2"},
        ])
        return 4

    monkeypatch.setattr(spse, "scrape_json", fake_scrape_json)
    monkeypatch.setattr(spse, "scrape_html", fake_scrape_html)
    monkeypatch.setattr(spse, "export_csv", fake_export_csv)
    monkeypatch.setattr(spse, "export_excel", lambda *a, **k: None)


@pytest.fixture
def client(tmp_path, network):
    with TestClient(create_app(root=tmp_path, api_key="")) as c:
        yield c


def wait_job(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_health_and_meta(client):
    assert client.get("/health").json() == {"status": "ok"}
    tipes = {c["tipe"]: c["filter_konstruksi"] for c in client.get("/categories").json()}
    assert tipes["tender"] is True and tipes["swakelola"] is False


def test_agencies(client):
    found = client.get("/agencies", params={"q": "pekerjaan umum"}).json()
    assert any(a["slug"] == "pu" for a in found["items"])
    assert client.get("/agencies/pu").json()["base"] == "https://spse.inaproc.id/pu"
    assert client.get("/agencies/tidak-ada-sama-sekali").status_code == 404


def test_preview_filtered(client):
    body = client.post("/preview", json={
        "agency": "pu", "tahun": 2026,
        "konstruksi": {"jenis": "pekerjaan", "kata": "gedung|jalan"}}).json()
    assert body["total"] == 5 and body["matched"] == 2
    assert [p["kode"] for p in body["packages"]] == ["1", "5"]


def test_preview_validation(client):
    assert client.post("/preview", json={"agency": "pu", "tipe": "swakelola",
                                         "konstruksi": {}}).status_code == 422
    assert client.post("/preview", json={"agency": "pu",
                                         "konstruksi": {"kata": "(["}}).status_code == 422
    assert client.post("/preview", json={"agency": "zzzz-none"}).status_code == 404


def test_job_lifecycle(client):
    resp = client.post("/jobs", json={"agency": "pu", "tahun": 2026,
                                      "konstruksi": {"jenis": "pekerjaan"}})
    assert resp.status_code == 202
    job = wait_job(client, resp.json()["id"])
    assert job["status"] == "done", job
    assert job["result"]["paket"] == 2
    assert job["params"]["agency"] == "pu"

    csv_resp = client.get(f"/jobs/{job['id']}/download/csv")
    assert csv_resp.status_code == 200 and "PT Beton Jaya" in csv_resp.text
    assert client.get(f"/jobs/{job['id']}/download/xlsx").status_code == 404

    pk = client.get(f"/jobs/{job['id']}/packages").json()
    assert pk["total"] == 2
    gedung, jalan = pk["items"]
    assert gedung["jumlah_peserta"] == 2
    assert [p["nama"] for p in gedung["peserta"]] == ["PT Beton Jaya", "PT Baja Abadi"]
    assert jalan["nama_disamarkan"] is True and jalan["peserta"] == []

    hit = client.get(f"/jobs/{job['id']}/packages", params={"q": "baja"}).json()
    assert hit["total"] == 1

    assert [j["id"] for j in client.get("/jobs").json()] == [job["id"]]
    assert client.get("/jobs/nope").status_code == 404


def test_api_key(tmp_path, network):
    with TestClient(create_app(root=tmp_path, api_key="rahasia")) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/categories").status_code == 401
        assert c.get("/categories", headers={"X-API-Key": "rahasia"}).status_code == 200


def test_running_job_becomes_interrupted_after_restart(tmp_path):
    jobs_dir = tmp_path / "_jobs"
    jobs_dir.mkdir()
    (jobs_dir / "abc.json").write_text(json.dumps({
        "id": "abc", "status": "running", "params": {}, "created_at": 1.0}))
    manager = JobManager(tmp_path)
    assert manager.get("abc").status == "interrupted"
    manager.shutdown()
