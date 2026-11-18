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
    with TestClient(create_app(root=tmp_path, allowed_ips="")) as c:
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
    resp = client.get("/")
    assert resp.status_code == 200 and resp.json() == {"detail": "Not Found"}
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
    assert [p["kode"] for p in body["packages"]] == ["5", "1"]   # terbaru dulu


def test_preview_status_and_order(client):
    body = client.post("/preview", json={
        "agency": "pu", "konstruksi": {}, "status": "berjalan"}).json()
    assert [p["kode"] for p in body["packages"]] == ["1"]
    body = client.post("/preview", json={"agency": "pu", "urut": "asli"}).json()
    assert [p["kode"] for p in body["packages"]] == ["1", "2", "3", "4", "5"]
    body = client.post("/preview", json={"agency": "pu", "urut": "pagu", "max_items": 2}).json()
    assert [p["kode"] for p in body["packages"]] == ["5", "1"]


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
    by_kode = {p["kode_paket"]: p for p in pk["items"]}
    gedung, jalan = by_kode["1"], by_kode["5"]
    assert [p["kode_paket"] for p in pk["items"]] == ["5", "1"]   # terbaru dulu
    assert gedung["jumlah_peserta"] == 2
    assert [p["nama"] for p in gedung["peserta"]] == ["PT Beton Jaya", "PT Baja Abadi"]
    assert jalan["nama_disamarkan"] is True and jalan["peserta"] == []
    assert gedung["buka_url"] == "http://testserver/spse/pu/lelang/1/pengumumanlelang"


    hit = client.get(f"/jobs/{job['id']}/packages", params={"q": "baja"}).json()
    assert hit["total"] == 1

    assert [j["id"] for j in client.get("/jobs").json()] == [job["id"]]
    assert client.get("/jobs/nope").status_code == 404


def test_offline_job_skips_network(client, tmp_path, monkeypatch):
    # No cache yet: the job fails with a clear message instead of scraping.
    job = wait_job(client, client.post("/jobs", json={
        "agency": "pu", "tahun": 2026, "offline": True,
        "konstruksi": {}}).json()["id"])
    assert job["status"] == "failed" and "cache" in job["error"]

    list_dir = tmp_path / "pu" / "2026" / "tender"
    list_dir.mkdir(parents=True)
    (list_dir / "list.json").write_text(json.dumps({"data": ROWS}))

    def boom(*a, **k):
        raise AssertionError("offline job touched the network")
    monkeypatch.setattr(spse, "scrape_json", boom)
    monkeypatch.setattr(spse, "scrape_html", boom)
    job = wait_job(client, client.post("/jobs", json={
        "agency": "pu", "tahun": 2026, "offline": True,
        "konstruksi": {"jenis": "pekerjaan"}}).json()["id"])
    assert job["status"] == "done", job
    assert job["result"]["paket"] == 2


def test_spse_bounce_page(tmp_path, network):
    with TestClient(create_app(root=tmp_path, allowed_ips="34.50.127.37")) as c:
        # Open to every IP: links are clicked from Excel / chat.
        resp = c.get("/spse/pu/lelang/10181495000/pengumumanlelang")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        target = "https://spse.inaproc.id/pu/lelang/10181495000/pengumumanlelang"
        assert f'window.location.replace("{target}")' in resp.text
        assert 'content="origin"' in resp.text
        # Query strings (pencatatan / darurat links) survive.
        resp = c.get("/spse/pu/pencatatan/pengumumannonspk?id=123")
        assert "pengumumannonspk?id=123" in resp.text
        # The target host is fixed; a path cannot point the browser elsewhere.
        resp = c.get("/spse//evil.example/x")
        assert 'replace("https://spse.inaproc.id/' in resp.text
        assert '"https://evil' not in resp.text


def test_buka_url_in_results(client):
    body = client.post("/preview", json={"agency": "pu", "max_items": 1,
                                         "urut": "asli"}).json()
    pkg = body["packages"][0]
    assert pkg["sumber_url"] == "https://spse.inaproc.id/pu/lelang/1/pengumumanlelang"
    assert pkg["buka_url"] == "http://testserver/spse/pu/lelang/1/pengumumanlelang"


ALLOWED = "100.80.64.57, 34.50.127.37 43.157.228.227,10.20.0.0/16"


def via_proxy(ip):
    """What one reverse proxy sends: the real client appended last."""
    return {"X-Forwarded-For": ip}


def test_ip_whitelist(tmp_path, network):
    with TestClient(create_app(root=tmp_path, allowed_ips=ALLOWED)) as c:
        # Public paths stay open to anyone.
        for path in ("/", "/health", "/whoami", "/spse/pu/lelang/1/pengumumanlelang"):
            assert c.get(path, headers=via_proxy("8.8.8.8")).status_code == 200, path
        # Everything else, /docs included, needs a whitelisted IP.
        for path in ("/categories", "/jobs", "/docs", "/openapi.json"):
            resp = c.get(path, headers=via_proxy("8.8.8.8"))
            assert resp.status_code == 403, path
            assert "8.8.8.8" in resp.json()["detail"]
        assert c.get("/categories", headers=via_proxy("34.50.127.37")).status_code == 200
        assert c.get("/categories", headers=via_proxy("100.80.64.57")).status_code == 200
        assert c.get("/categories", headers=via_proxy("10.20.5.6")).status_code == 200  # CIDR
        assert c.post("/preview", json={"agency": "pu"},
                      headers=via_proxy("8.8.8.8")).status_code == 403


def test_forged_forwarded_for_is_ignored(tmp_path, network):
    with TestClient(create_app(root=tmp_path, allowed_ips=ALLOWED)) as c:
        # Attacker sends "X-Forwarded-For: <whitelisted>"; the proxy appends the
        # real address, which is what counts.
        forged = {"X-Forwarded-For": "34.50.127.37, 8.8.8.8"}
        assert c.get("/categories", headers=forged).status_code == 403
        who = c.get("/whoami", headers=forged).json()
        assert who["ip"] == "8.8.8.8" and who["allowed"] is False
        # No forwarding header: the direct peer (TestClient's "testclient") is
        # not an IP, so it is refused rather than waved through.
        assert c.get("/categories").status_code == 403


def test_proxy_hops_and_client_ip_header(tmp_path, network):
    two = {"X-Forwarded-For": "8.8.8.8, 34.50.127.37, 172.16.0.9"}
    with TestClient(create_app(root=tmp_path, allowed_ips=ALLOWED, proxy_hops=2)) as c:
        assert c.get("/categories", headers=two).status_code == 200
    with TestClient(create_app(root=tmp_path, allowed_ips=ALLOWED, proxy_hops=1)) as c:
        assert c.get("/categories", headers=two).status_code == 403
    with TestClient(create_app(root=tmp_path, allowed_ips=ALLOWED,
                               client_ip_header="CF-Connecting-IP")) as c:
        assert c.get("/categories", headers={"CF-Connecting-IP": "43.157.228.227"}).status_code == 200
        assert c.get("/categories", headers={"CF-Connecting-IP": "8.8.8.8"}).status_code == 403


def test_whitelist_disabled_allows_all(client):
    assert client.get("/categories", headers=via_proxy("8.8.8.8")).status_code == 200
    assert client.get("/whoami").json()["whitelist_enabled"] is False


def test_invalid_whitelist_entry_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="100.80.64"):
        create_app(root=tmp_path, allowed_ips="100.80.64")


def test_buka_url_uses_forwarded_proto(client):
    body = client.post("/preview", json={"agency": "pu", "max_items": 1, "urut": "asli"},
                       headers={"X-Forwarded-Proto": "https",
                                "X-Forwarded-Host": "data-spse.latindo.ai"}).json()
    assert body["packages"][0]["buka_url"] == \
        "https://data-spse.latindo.ai/spse/pu/lelang/1/pengumumanlelang"


def test_swagger_examples_are_valid(client):
    # Every dropdown example in /docs must pass validation as-is.
    from api.main import JOB_EXAMPLES, PREVIEW_EXAMPLES, JobRequest, PreviewRequest
    for ex in PREVIEW_EXAMPLES.values():
        PreviewRequest(**ex["value"])
    for ex in JOB_EXAMPLES.values():
        JobRequest(**ex["value"])
    spec = client.get("/openapi.json").json()
    body = spec["paths"]["/jobs"]["post"]["requestBody"]["content"]["application/json"]
    assert "uji_kecil" in body["examples"]


def test_cancel_running_job_reports_cancelling(tmp_path):
    import threading
    manager = JobManager(tmp_path)
    started, release = threading.Event(), threading.Event()

    def runner(params, log, progress, cancel):
        started.set()
        release.wait(5)
        return {}

    job = manager.submit({}, runner)
    assert started.wait(5)
    assert manager.cancel(job.id).status == "cancelling"
    release.set()
    deadline = time.time() + 5
    while job.status == "cancelling" and time.time() < deadline:
        time.sleep(0.02)
    assert job.status == "cancelled"
    manager.shutdown()


def test_running_job_becomes_interrupted_after_restart(tmp_path):
    jobs_dir = tmp_path / "_jobs"
    jobs_dir.mkdir()
    (jobs_dir / "abc.json").write_text(json.dumps({
        "id": "abc", "status": "running", "params": {}, "created_at": 1.0}))
    manager = JobManager(tmp_path)
    assert manager.get("abc").status == "interrupted"
    manager.shutdown()


def test_packages_rebuild_original_link_from_bounce_csv(tmp_path):
    from api.main import summarize_csv
    path = tmp_path / "x.csv"
    write_csv(path, [{"slug": "pu", "kategori": "tender", "kode_paket": "77",
                   "sumber_url": "https://data-spse.latindo.ai/spse/pu/lelang/77/pengumumanlelang"}])
    [pkg] = summarize_csv(path)
    assert pkg["sumber_url"] == "https://spse.inaproc.id/pu/lelang/77/pengumumanlelang"
