"""Background job runner for long scrapes.

Jobs run in a small thread pool. State lives in memory and is mirrored to
<root>/_jobs/<id>.json so the job list survives a restart; jobs that were
still queued or running when the process died come back as "interrupted"
(rerunning them resumes from the HTML cache).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

ACTIVE = ("queued", "running", "cancelling")
LOG_KEEP = 500


def _now() -> float:
    return round(time.time(), 3)


class Job:
    def __init__(self, params: dict, job_id: str | None = None):
        self.id = job_id or uuid.uuid4().hex[:12]
        self.params = params
        self.status = "queued"
        self.created_at = _now()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.progress = {"done": 0, "total": 0, "label": ""}
        self.log: deque[str] = deque(maxlen=LOG_KEEP)
        self.result: dict | None = None
        self.error: str | None = None
        self.cancel = threading.Event()

    def to_dict(self, log_lines: int = 50) -> dict:
        lines = list(self.log)[-log_lines:] if log_lines else []
        return {
            "id": self.id,
            "status": self.status,
            "params": self.params,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "result": self.result,
            "error": self.error,
            "log": lines,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        job = cls(data["params"], job_id=data["id"])
        job.status = data["status"]
        job.created_at = data["created_at"]
        job.started_at = data.get("started_at")
        job.finished_at = data.get("finished_at")
        job.progress = data.get("progress") or job.progress
        job.result = data.get("result")
        job.error = data.get("error")
        job.log.extend(data.get("log") or [])
        return job


# runner(params, log, progress, cancel) -> result dict
Runner = Callable[[dict, Callable, Callable, threading.Event], dict]


class JobManager:
    def __init__(self, root: Path, max_concurrent: int = 2):
        self.dir = Path(root) / "_jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._dataset_locks: defaultdict[tuple, threading.Lock] = defaultdict(threading.Lock)
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent,
                                        thread_name_prefix="spse-job")
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        for path in sorted(self.dir.glob("*.json")):
            try:
                job = Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                continue
            if job.status in ACTIVE:
                job.status = "interrupted"
                job.finished_at = job.finished_at or _now()
                self._persist(job)
            self.jobs[job.id] = job

    def _persist(self, job: Job) -> None:
        path = self.dir / f"{job.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(log_lines=LOG_KEEP), ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    # -- locking -----------------------------------------------------------
    def dataset_lock(self, slug: str, tahun: int, tipe: str) -> threading.Lock:
        """One lock per list.json so two jobs never write the same cache."""
        with self._lock:
            return self._dataset_locks[(slug, tahun, tipe)]

    # -- public API --------------------------------------------------------
    def submit(self, params: dict, runner: Runner) -> Job:
        job = Job(params)
        with self._lock:
            self.jobs[job.id] = job
        self._persist(job)
        self._pool.submit(self._run, job, runner)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, status: str | None = None) -> list[Job]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j for j in jobs if status is None or j.status == status]

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.status in ACTIVE:
            job.cancel.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = _now()
            elif job.status == "running":
                # Requests already in flight must finish (or time out, up to
                # ~60 s) before the worker stops; show that it was heard.
                job.status = "cancelling"
            job.log.append("pembatalan diminta, menunggu request yang sedang berjalan")
            self._persist(job)
        return job

    def shutdown(self) -> None:
        for job in self.jobs.values():
            job.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- worker ------------------------------------------------------------
    def _run(self, job: Job, runner: Runner) -> None:
        if job.cancel.is_set():
            return
        job.status = "running"
        job.started_at = _now()
        self._persist(job)

        def log(*parts) -> None:
            job.log.append(" ".join(str(p) for p in parts))

        def progress(done: int, total: int, label: str = "paket") -> None:
            job.progress = {"done": done, "total": total, "label": label}

        try:
            job.result = runner(job.params, log, progress, job.cancel)
            job.status = "cancelled" if job.cancel.is_set() else "done"
        except Exception as err:  # surface any failure on the job, never crash the pool
            job.status = "failed"
            job.error = f"{type(err).__name__}: {err}"
            log(f"gagal: {job.error}")
        job.finished_at = _now()
        self._persist(job)
