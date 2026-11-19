"""HTTP API around spse.py and konstruksi.py.

Run:  uvicorn api.main:app --host 0.0.0.0 --port 8080
Docs: http://localhost:8080/docs (interactive Swagger UI)

Environment:
  SPSE_OUTPUT        output root (default: output)
  SPSE_ALLOWED_IPS   IP whitelist (see api/access.py); empty = allow all
  SPSE_PROXY_HOPS    reverse proxies in front that append X-Forwarded-For (1)
  SPSE_CLIENT_IP_HEADER  optional trusted header holding the client IP
  SPSE_MAX_JOBS      concurrent scrape jobs (default: 2)
  SPSE_PUBLIC_URL    public base URL used in buka_url links (default: taken
                     from the request, honouring X-Forwarded-Proto/Host)
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

import konstruksi
import spse
from api.access import AccessPolicy
from api.jobs import JobManager

Tipe = Literal["tender", "nontender", "pencatatan", "swakelola", "darurat"]
Jenis = Literal["semua", "pekerjaan", "konsultansi"]
Status = Literal["semua", "berjalan", "selesai", "batal"]
Urut = Literal["terbaru", "pagu", "asli"]
MASKED_RE = re.compile(r"^Peserta \d+$")

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# -- request models ------------------------------------------------------------
class KonstruksiFilter(BaseModel):
    jenis: Jenis = Field("semua", description="pekerjaan = kontraktor fisik, "
                         "konsultansi = konsultan konstruksi, semua = keduanya")
    min_pagu: float = Field(0, ge=0, description="Pagu minimum, miliar rupiah")
    kata: str | None = Field(None, max_length=200,
                             description="Regex kata kunci di nama paket, mis. 'jalan|jembatan'")

    @field_validator("kata")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as err:
                raise ValueError(f"regex tidak valid: {err}") from err
        return value or None


class Target(BaseModel):
    agency: str = Field(..., description="Slug LPSE atau nama instansi, mis. 'pu'")
    tipe: Tipe = "tender"
    tahun: int | None = Field(None, ge=2000, le=2100,
                              description="Tahun anggaran (default: tahun berjalan)")
    konstruksi: KonstruksiFilter | None = Field(
        None, description="Isi untuk menyaring paket konstruksi saja")
    refresh: bool = Field(False, description="Ambil ulang daftar paket dari server")
    status: Status = Field("semua", description="berjalan = belum selesai dan tidak "
                           "batal/gagal; selesai; batal = batal atau gagal")
    urut: Urut = Field("terbaru", description="terbaru = paket terbaru dulu "
                       "(kode paket terbesar); pagu = pagu terbesar dulu; asli = urutan SPSE")


class PreviewRequest(Target):
    max_items: int = Field(50, ge=0, le=5000, description="Jumlah paket yang dikembalikan")


class JobRequest(Target):
    limit: int = Field(0, ge=0, description="Batasi N paket (0 = semua)")
    workers: int = Field(spse.DEFAULT_WORKERS, ge=1, le=16)
    excel: bool = True
    offline: bool = Field(False, description="Export ulang CSV/XLSX dari data yang "
                          "sudah ada, tanpa network (= --skip-json --skip-html)")


# -- Swagger examples --------------------------------------------------------------
# Without these, Swagger UI pre-fills every field with placeholders such as
# "kata": "string", which silently matches nothing. Each entry shows up in the
# Examples dropdown of /docs.
PREVIEW_EXAMPLES = {
    "hitung": {
        "summary": "Hitung semua paket (= --dry)",
        "value": {"agency": "jakarta", "tipe": "swakelola", "tahun": 2026},
    },
    "konstruksi": {
        "summary": "Hitung paket konstruksi",
        "value": {"agency": "pu", "tipe": "tender", "tahun": 2026,
                  "konstruksi": {"jenis": "semua"}},
    },
    "kontraktor_besar": {
        "summary": "Pekerjaan fisik, pagu >= Rp50 M",
        "value": {"agency": "pu", "tipe": "tender", "tahun": 2026,
                  "konstruksi": {"jenis": "pekerjaan", "min_pagu": 50}},
    },
    "kata_kunci": {
        "summary": "Tender gedung / fasad terbaru yang masih berjalan",
        "value": {"agency": "pu", "tipe": "tender", "tahun": 2026,
                  "konstruksi": {"kata": "gedung|fasad"}, "status": "berjalan",
                  "urut": "terbaru", "refresh": True, "max_items": 20},
    },
}

JOB_EXAMPLES = {
    "uji_kecil": {
        "summary": "Uji kecil: 5 paket (= --limit 5)",
        "value": {"agency": "kemkes", "tipe": "tender", "tahun": 2025, "limit": 5},
    },
    "lengkap": {
        "summary": "Scrape lengkap semua paket",
        "value": {"agency": "kemkes", "tipe": "tender", "tahun": 2025},
    },
    "konstruksi_gedung": {
        "summary": "Konstruksi: gedung, pekerjaan fisik",
        "value": {"agency": "pu", "tipe": "tender", "tahun": 2025,
                  "konstruksi": {"jenis": "pekerjaan", "kata": "gedung"}},
    },
    "konsultan": {
        "summary": "Konstruksi: peluang konsultan",
        "value": {"agency": "pu", "tipe": "tender", "tahun": 2025,
                  "konstruksi": {"jenis": "konsultansi"}},
    },
    "nontender_pemda": {
        "summary": "Pengadaan langsung konstruksi di pemda",
        "value": {"agency": "jatimprov", "tipe": "nontender", "tahun": 2025,
                  "konstruksi": {"jenis": "pekerjaan"}},
    },
    "offline": {
        "summary": "Export ulang tanpa network",
        "value": {"agency": "kemkes", "tipe": "tender", "tahun": 2025, "offline": True},
    },
}


# -- helpers -----------------------------------------------------------------------
def _resolve(agencies: list[dict], target: Target) -> tuple[dict, int]:
    agency = spse.match_agency(agencies, target.agency)
    if agency is None:
        raise HTTPException(404, f"Instansi '{target.agency}' tidak ditemukan")
    if target.konstruksi and target.tipe not in konstruksi.FILTERABLE:
        raise HTTPException(422, f"Filter konstruksi tidak tersedia untuk tipe "
                                 f"'{target.tipe}' (hanya {', '.join(konstruksi.FILTERABLE)}). "
                                 f"Hapus field 'konstruksi' atau isi null.")
    return agency, spse.resolve_tahun(target.tahun)


def _agency_out(agency: dict) -> dict:
    return {"slug": agency["slug"], "base": agency["base"], "names": agency["names"]}


def _num(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def original_link(row: dict) -> str | None:
    """The plain SPSE URL of a CSV row. The CSV's sumber_url already points at
    this API's /spse/ bounce page, so rebuild the original from slug/kategori/kode."""
    slug, kategori, kode = row.get("slug"), row.get("kategori"), row.get("kode_paket")
    if slug and kode and kategori in spse.CATEGORIES:
        return spse.entry_tab_url(f"{SPSE_ORIGIN}/{slug}", kategori, kode)
    return row.get("sumber_url") or None


def summarize_csv(path: Path) -> list[dict]:
    """Collapse the one-row-per-participant CSV into one entry per package."""
    packages: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            kode = row.get("kode_paket") or ""
            pkg = packages.get(kode)
            if pkg is None:
                pkg = packages[kode] = {
                    "kode_paket": kode,
                    "nama_paket": row.get("nama_paket"),
                    "instansi": row.get("instansi"),
                    "satuan_kerja": row.get("satuan_kerja"),
                    "jenis_pengadaan": row.get("jenis_pengadaan"),
                    "metode_pengadaan": row.get("metode_pengadaan"),
                    "tahap": row.get("tahap") or None,
                    "pagu_num": _num(row.get("pagu_num", "")),
                    "hps_num": _num(row.get("hps_num", "")),
                    "lokasi": row.get("lokasi"),
                    "sumber_dana": row.get("sumber_dana"),
                    "tanggal_pembuatan": row.get("tanggal_pembuatan_iso") or None,
                    "sumber_url": original_link(row),
                    "jumlah_peserta": 0,
                    "nama_disamarkan": False,
                    "peserta": [],
                }
            nama = (row.get("nama_pemenang") or "").strip()
            if not nama:
                continue
            pkg["jumlah_peserta"] += 1
            if MASKED_RE.match(nama):
                pkg["nama_disamarkan"] = True
                continue
            pkg["peserta"].append({
                "nama": nama,
                "npwp": row.get("npwp") or None,
                "harga_kontrak_num": _num(row.get("harga_kontrak_num", "")),
            })
    return list(packages.values())


# -- SPSE link bounce -------------------------------------------------------------
SPSE_ORIGIN = spse.SPSE_ORIGIN

# SPSE answers "Akses Ditolak" to any detail page requested without a Referer
# header, which is exactly what a click from Excel, a chat message or the
# address bar sends. A 302 would not help: browsers keep the (empty) original
# Referer across redirects. A page that navigates with JavaScript, however,
# makes the browser send this page's origin as Referer, which SPSE accepts.
BOUNCE_PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="referrer" content="origin">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Membuka SPSE</title>
<style>body{{font-family:system-ui,sans-serif;margin:3rem auto;max-width:40rem;
padding:0 1rem;color:#222}}a{{color:#0b57d0;word-break:break-all}}</style>
</head><body>
<p>Membuka halaman SPSE...</p>
<p>Kalau tidak terbuka otomatis, klik: <a href="{href}">{href}</a></p>
<script>window.location.replace({target});</script>
</body></html>
"""


def spse_target(path: str, query: str = "") -> str:
    target = f"{SPSE_ORIGIN}/{path.lstrip('/')}"
    return f"{target}?{query}" if query else target


def public_base(request: Request) -> str:
    """Public origin for links. Behind a TLS-terminating proxy the app itself
    sees plain http, so prefer the proxy's X-Forwarded-Proto/Host."""
    if os.environ.get("SPSE_PUBLIC_URL"):
        return os.environ["SPSE_PUBLIC_URL"].rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    return f"{proto or request.url.scheme}://{host or request.url.netloc}"


def buka_url(request: Request, sumber_url: str | None) -> str | None:
    """https://spse.inaproc.id/x -> <this api>/spse/x (opens with a Referer)."""
    return spse.buka_link(sumber_url or "", public_base(request)) or None


# -- job runner --------------------------------------------------------------------
def make_runner(root: Path, agencies: list[dict], manager: JobManager):
    def runner(params: dict, log, progress, cancel) -> dict:
        req = JobRequest(**params)
        agency, tahun = _resolve(agencies, req)
        slug, tipe = agency["slug"], req.tipe
        with manager.dataset_lock(slug, tahun, tipe):
            if req.konstruksi or req.status != "semua" or (req.limit and req.urut != "asli"):
                f = req.konstruksi
                return konstruksi.run_filtered(
                    agency, tipe, tahun, jenis=f.jenis if f else None,
                    min_pagu=f.min_pagu if f else 0, kata=f.kata if f else None,
                    status=req.status, urut=req.urut,
                    limit=req.limit, workers=req.workers,
                    excel=req.excel, refresh=req.refresh,
                    offline=req.offline, root=root,
                    cancel=cancel, progress=progress, log=log)

            out_dir = spse.run_dir(slug, tahun, tipe, root)
            if req.offline:
                rows = konstruksi.load_cached_rows(out_dir)
            else:
                rows = spse.scrape_json(agency["base"], slug, tipe, tahun, out_dir,
                                        force=req.refresh, log=log)
            stats = spse.run_pipeline(
                agency, tipe, tahun, do_json=False, do_html=not req.offline,
                workers=req.workers,
                excel=req.excel, limit=req.limit, root=root,
                cancel=cancel, progress=progress, log=log)
            csv_path = out_dir.parent / f"{slug}_{tahun}_{tipe}.csv"
            stats.update(total=len(rows), cancelled=cancel.is_set(),
                         csv=str(csv_path) if csv_path.exists() else None)
            return stats
    return runner


# -- app -----------------------------------------------------------------------------
def create_app(root: Path | str | None = None, allowed_ips: str | None = None,
               proxy_hops: int | None = None, client_ip_header: str | None = None,
               max_jobs: int | None = None, agency_csv: Path | str | None = None) -> FastAPI:
    root = Path(root or os.environ.get("SPSE_OUTPUT", "output"))
    access = AccessPolicy.from_env(allowed_ips, proxy_hops, client_ip_header)
    print(f"SPSE API: {access.describe()}", flush=True)
    max_jobs = max_jobs or int(os.environ.get("SPSE_MAX_JOBS", "2"))
    agencies = spse.load_agencies(agency_csv or spse.AGENCY_CSV)
    manager = JobManager(root, max_concurrent=max_jobs)
    runner = make_runner(root, agencies, manager)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(
        title="SPSE Scraper API",
        version="0.1.0",
        description="Scrape data pengadaan SPSE (tender, non tender, pencatatan, "
                    "swakelola, darurat) per instansi dan tahun, dengan filter "
                    "paket konstruksi. Scrape berjalan sebagai job di background.",
        lifespan=lifespan,
        # Swagger UI and ReDoc, both behind the IP whitelist like every non-public path.
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.manager = manager

    app.state.access = access

    @app.middleware("http")
    async def ip_whitelist(request: Request, call_next):
        """Every path except the public ones (/, /health, /whoami, /spse/...)
        requires a whitelisted client IP; this includes /docs."""
        if access.enabled and not access.is_public(request.url.path):
            ip = access.client_ip(request.client.host if request.client else None,
                                  request.headers)
            if not access.allows(ip):
                return JSONResponse({"detail": f"IP {ip or '?'} tidak diizinkan"},
                                    status_code=403)
        return await call_next(request)

    @app.get("/whoami", tags=["meta"])
    def whoami(request: Request) -> dict:
        """Your IP as the server sees it and whether it is whitelisted. Use it
        to check SPSE_ALLOWED_IPS / SPSE_PROXY_HOPS after a deploy."""
        peer = request.client.host if request.client else None
        ip = access.client_ip(peer, request.headers)
        return {"ip": ip, "allowed": access.allows(ip),
                "whitelist_enabled": access.enabled, "peer": peer,
                "x_forwarded_for": request.headers.get("x-forwarded-for")}

    @app.get("/", tags=["meta"])
    def index() -> dict:  # not "root": that name is the output folder here
        # Looks like FastAPI's own 404 body, but answers 200 on purpose.
        return {"detail": "Not Found"}

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/spse/{path:path}", tags=["tautan"], response_class=HTMLResponse)
    def buka_spse(path: str, request: Request) -> HTMLResponse:
        """Buka halaman SPSE dengan Referer, supaya tidak "Akses Ditolak".

        Ganti `https://spse.inaproc.id/` di link paket dengan
        `<alamat api>/spse/`. Terbuka untuk semua IP, supaya bisa diklik dari
        Excel atau chat; tujuannya selalu spse.inaproc.id, jadi tidak bisa dipakai untuk
        mengalihkan ke situs lain."""
        target = spse_target(path, request.url.query)
        return HTMLResponse(BOUNCE_PAGE.format(
            href=html.escape(target, quote=True),
            target=json.dumps(target).replace("</", "<\\/")))

    r = APIRouter()

    @r.get("/categories", tags=["meta"])
    def categories() -> list[dict]:
        return [{"tipe": k, "label": v["label"],
                 "filter_konstruksi": k in konstruksi.FILTERABLE}
                for k, v in spse.CATEGORIES.items()]

    @r.get("/agencies", tags=["instansi"])
    def list_agencies(q: str | None = Query(None, description="Cari di slug atau nama"),
                      limit: int = Query(50, ge=1, le=1000)) -> dict:
        found = agencies
        if q:
            needle = q.lower()
            found = [a for a in agencies
                     if needle in " ".join([a["slug"], *a["names"]]).lower()]
        return {"total": len(found), "items": [_agency_out(a) for a in found[:limit]]}

    @r.get("/agencies/{query}", tags=["instansi"])
    def get_agency(query: str) -> dict:
        agency = spse.match_agency(agencies, query)
        if agency is None:
            raise HTTPException(404, f"Instansi '{query}' tidak ditemukan")
        return _agency_out(agency)

    @r.post("/preview", tags=["scrape"])
    def preview(req: Annotated[PreviewRequest, Body(openapi_examples=PREVIEW_EXAMPLES)],
                request: Request) -> dict:
        """Hitung dan tampilkan paket tanpa mengunduh detail (cepat)."""
        agency, tahun = _resolve(agencies, req)
        slug = agency["slug"]
        with manager.dataset_lock(slug, tahun, req.tipe):
            rows = spse.scrape_json(agency["base"], slug, req.tipe, tahun,
                                    spse.run_dir(slug, tahun, req.tipe, root),
                                    force=req.refresh, log=lambda *a: None)
        f = req.konstruksi
        matched = konstruksi.filter_rows(
            rows, req.tipe, jenis=f.jenis if f else None,
            min_pagu=f.min_pagu if f else 0, kata=f.kata if f else None,
            status=req.status)
        matched = konstruksi.sort_rows(matched, req.tipe, req.urut)

        def with_links(pkg: dict) -> dict:
            pkg["sumber_url"] = spse.entry_tab_url(agency["base"], req.tipe, pkg["kode"])
            pkg["buka_url"] = buka_url(request, pkg["sumber_url"])
            return pkg

        return {
            "agency": _agency_out(agency), "tipe": req.tipe, "tahun": tahun,
            "total": len(rows), "matched": len(matched),
            "packages": [with_links(konstruksi.row_summary(row, req.tipe))
                         for row in matched[:req.max_items]],
        }

    @r.post("/jobs", status_code=202, tags=["jobs"])
    def create_job(req: Annotated[JobRequest, Body(openapi_examples=JOB_EXAMPLES)]) -> dict:
        """Mulai scrape di background. Pantau lewat GET /jobs/{id}."""
        agency, tahun = _resolve(agencies, req)
        params = req.model_dump()
        params.update(agency=agency["slug"], tahun=tahun)
        return manager.submit(params, runner).to_dict()

    @r.get("/jobs", tags=["jobs"])
    def list_jobs(status: str | None = None) -> list[dict]:
        return [j.to_dict(log_lines=0) for j in manager.list(status)]

    def _job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Job tidak ditemukan")
        return job

    @r.get("/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str, log: int = Query(50, ge=0, le=500)) -> dict:
        return _job(job_id).to_dict(log_lines=log)

    @r.post("/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str) -> dict:
        _job(job_id)
        return manager.cancel(job_id).to_dict()

    def _result_file(job_id: str, key: str) -> Path:
        job = _job(job_id)
        path = (job.result or {}).get(key)
        if not path or not Path(path).exists():
            raise HTTPException(404, f"File {key} belum tersedia (status: {job.status})")
        return Path(path)

    @r.get("/jobs/{job_id}/download/{fmt}", tags=["jobs"])
    def download(job_id: str, fmt: Literal["csv", "xlsx"]) -> FileResponse:
        path = _result_file(job_id, fmt)
        return FileResponse(path, filename=path.name)

    @r.get("/jobs/{job_id}/packages", tags=["jobs"])
    def packages(job_id: str, request: Request,
                 q: str | None = Query(None, description="Cari di nama paket / satker / peserta"),
                 urut: Urut = Query("terbaru", description="terbaru = tanggal pembuatan "
                                    "terbaru dulu; pagu = pagu terbesar dulu; asli = urutan file"),
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)) -> dict:
        """Hasil job sebagai JSON: satu entri per paket beserta pesertanya."""
        items = summarize_csv(_result_file(job_id, "csv"))
        if urut == "terbaru":
            items.sort(key=lambda p: (p["tanggal_pembuatan"] or "",
                                      int(p["kode_paket"]) if p["kode_paket"].isdigit() else 0),
                       reverse=True)
        elif urut == "pagu":
            items.sort(key=lambda p: p["pagu_num"] or 0, reverse=True)
        if q:
            needle = q.lower()
            items = [p for p in items if needle in " ".join(
                [p["nama_paket"] or "", p["satuan_kerja"] or "",
                 *(x["nama"] for x in p["peserta"])]).lower()]
        page = items[offset:offset + limit]
        for p in page:
            p["buka_url"] = buka_url(request, p["sumber_url"])
        return {"total": len(items), "offset": offset, "items": page}

    app.include_router(r)
    return app


app = create_app()
