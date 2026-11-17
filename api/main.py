"""HTTP API around spse.py and konstruksi.py.

Run:  uvicorn api.main:app --host 0.0.0.0 --port 8080
Docs: http://localhost:8080/docs (interactive Swagger UI)

Environment:
  SPSE_OUTPUT        output root (default: output)
  SPSE_API_KEY       if set, every endpoint except /health needs X-API-Key
  SPSE_MAX_JOBS      concurrent scrape jobs (default: 2)
"""
from __future__ import annotations

import csv
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

import konstruksi
import spse
from api.jobs import JobManager

Tipe = Literal["tender", "nontender", "pencatatan", "swakelola", "darurat"]
Jenis = Literal["semua", "pekerjaan", "konsultansi"]
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


class PreviewRequest(Target):
    max_items: int = Field(50, ge=0, le=5000, description="Jumlah paket yang dikembalikan")


class JobRequest(Target):
    limit: int = Field(0, ge=0, description="Batasi N paket (0 = semua)")
    workers: int = Field(spse.DEFAULT_WORKERS, ge=1, le=16)
    excel: bool = True


# -- helpers -----------------------------------------------------------------------
def _resolve(agencies: list[dict], target: Target) -> tuple[dict, int]:
    agency = spse.match_agency(agencies, target.agency)
    if agency is None:
        raise HTTPException(404, f"Instansi '{target.agency}' tidak ditemukan")
    if target.konstruksi and target.tipe not in konstruksi.FILTERABLE:
        raise HTTPException(422, f"Filter konstruksi tidak tersedia untuk tipe "
                                 f"'{target.tipe}' (hanya {', '.join(konstruksi.FILTERABLE)})")
    return agency, spse.resolve_tahun(target.tahun)


def _agency_out(agency: dict) -> dict:
    return {"slug": agency["slug"], "base": agency["base"], "names": agency["names"]}


def _num(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


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
                    "sumber_url": row.get("sumber_url"),
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


# -- job runner --------------------------------------------------------------------
def make_runner(root: Path, agencies: list[dict], manager: JobManager):
    def runner(params: dict, log, progress, cancel) -> dict:
        req = JobRequest(**params)
        agency, tahun = _resolve(agencies, req)
        slug, tipe = agency["slug"], req.tipe
        with manager.dataset_lock(slug, tahun, tipe):
            if req.konstruksi:
                f = req.konstruksi
                return konstruksi.run_filtered(
                    agency, tipe, tahun, jenis=f.jenis, min_pagu=f.min_pagu,
                    kata=f.kata, limit=req.limit, workers=req.workers,
                    excel=req.excel, refresh=req.refresh, root=root,
                    cancel=cancel, progress=progress, log=log)

            out_dir = spse.run_dir(slug, tahun, tipe, root)
            rows = spse.scrape_json(agency["base"], slug, tipe, tahun, out_dir,
                                    force=req.refresh, log=log)
            stats = spse.run_pipeline(
                agency, tipe, tahun, do_json=False, workers=req.workers,
                excel=req.excel, limit=req.limit, root=root,
                cancel=cancel, progress=progress, log=log)
            csv_path = out_dir.parent / f"{slug}_{tahun}_{tipe}.csv"
            stats.update(total=len(rows), cancelled=cancel.is_set(),
                         csv=str(csv_path) if csv_path.exists() else None)
            return stats
    return runner


# -- app -----------------------------------------------------------------------------
def create_app(root: Path | str | None = None, api_key: str | None = None,
               max_jobs: int | None = None, agency_csv: Path | str | None = None) -> FastAPI:
    root = Path(root or os.environ.get("SPSE_OUTPUT", "output"))
    api_key = api_key if api_key is not None else os.environ.get("SPSE_API_KEY") or None
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
    )
    app.state.manager = manager

    # Declared as a security scheme so Swagger UI (/docs) shows an Authorize
    # button that fills the header for every request.
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                              description="Isi dengan nilai SPSE_API_KEY")

    def require_key(x_api_key: str | None = Security(key_header)) -> None:
        if api_key and not secrets.compare_digest(x_api_key or "", api_key):
            raise HTTPException(401, "X-API-Key salah atau tidak ada")

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    r = APIRouter(dependencies=[Depends(require_key)])

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
    def preview(req: PreviewRequest) -> dict:
        """Hitung dan tampilkan paket tanpa mengunduh detail (cepat)."""
        agency, tahun = _resolve(agencies, req)
        slug = agency["slug"]
        with manager.dataset_lock(slug, tahun, req.tipe):
            rows = spse.scrape_json(agency["base"], slug, req.tipe, tahun,
                                    spse.run_dir(slug, tahun, req.tipe, root),
                                    force=req.refresh, log=lambda *a: None)
        matched = rows
        if req.konstruksi:
            f = req.konstruksi
            matched = konstruksi.filter_rows(rows, req.tipe, f.jenis, f.min_pagu, f.kata)
        return {
            "agency": _agency_out(agency), "tipe": req.tipe, "tahun": tahun,
            "total": len(rows), "matched": len(matched),
            "packages": [konstruksi.row_summary(row, req.tipe)
                         for row in matched[:req.max_items]],
        }

    @r.post("/jobs", status_code=202, tags=["jobs"])
    def create_job(req: JobRequest) -> dict:
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
    def packages(job_id: str,
                 q: str | None = Query(None, description="Cari di nama paket / satker / peserta"),
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)) -> dict:
        """Hasil job sebagai JSON: satu entri per paket beserta pesertanya."""
        items = summarize_csv(_result_file(job_id, "csv"))
        if q:
            needle = q.lower()
            items = [p for p in items if needle in " ".join(
                [p["nama_paket"] or "", p["satuan_kerja"] or "",
                 *(x["nama"] for x in p["peserta"])]).lower()]
        return {"total": len(items), "offset": offset,
                "items": items[offset:offset + limit]}

    app.include_router(r)
    return app


app = create_app()
