"""
Background job queue and worker.

A single in-process worker thread drains a FIFO queue and runs one forecast job
at a time. Running jobs serially (rather than a process pool) keeps memory
bounded on small free-tier instances while preserving the engine's full,
unchanged model search.

Retention:
    - store_data ON  -> input workbook and output zip are persisted to the
      configured storage backend; refs are storage keys.
    - store_data OFF -> files live only under the ephemeral work dir; refs are
      local paths, and both input and output are deleted right after the user
      downloads (with a TTL sweep as a safety net).
"""

import json
import queue
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.db import SessionLocal
from app.forecast_engine import process_workbook
from app.models import Job, utcnow
from app.storage import get_persistent_storage

_job_queue: "queue.Queue[int]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _work_root() -> Path:
    root = Path(settings.WORK_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_dir(job_id: int) -> Path:
    d = _work_root() / f"job_{job_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue_job(job_id: int) -> None:
    """Add a job to the processing queue and ensure the worker is running."""
    ensure_worker()
    _job_queue.put(job_id)


def ensure_worker() -> None:
    """Start the single background worker thread once."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker_loop, name="forecast-worker", daemon=True)
        thread.start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            _run_job(job_id)
        except Exception:
            # Never let the worker die; record the failure on the job.
            _mark_error(job_id, traceback.format_exc())
        finally:
            _job_queue.task_done()


def _mark_error(job_id: int, error_text: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "error"
            job.error = error_text[-8000:]
            job.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def _run_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return

        job.status = "running"
        db.commit()

        work = job_dir(job_id)
        # For a freshly queued job, input_ref is always the local upload path
        # (set by the upload route before enqueueing).
        input_path = Path(job.input_ref)
        output_dir = work / "output"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        summary = process_workbook(
            input_path=str(input_path),
            output_dir=str(output_dir),
            workers=settings.FORECAST_WORKERS,
        )

        # Bundle the output folder into a single zip for delivery.
        zip_base = work / "forecast_results"
        zip_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=str(output_dir)))

        if job.retention:
            storage = get_persistent_storage()
            input_key = f"account_{job.account_id}/job_{job_id}/input.xlsx"
            output_key = f"account_{job.account_id}/job_{job_id}/forecast_results.zip"
            storage.save_file(input_key, str(input_path))
            storage.save_file(output_key, str(zip_path))
            job.input_ref = input_key
            job.output_ref = output_key
            # Persisted copies are safe; clear the local work dir.
            shutil.rmtree(work, ignore_errors=True)
        else:
            # Ephemeral: keep only the output zip locally; drop the raw input
            # and the unzipped output folder immediately.
            job.output_ref = str(zip_path)
            job.input_ref = None
            try:
                input_path.unlink(missing_ok=True)
            except Exception:
                pass
            shutil.rmtree(output_dir, ignore_errors=True)

        job.summary_json = json.dumps(summary, default=str)
        job.status = "done"
        job.finished_at = utcnow()
        db.commit()

    finally:
        db.close()


def load_output_bytes(job: Job) -> Optional[bytes]:
    """Return the output zip bytes for a completed job, or None if unavailable."""
    if job.status != "done" or not job.output_ref:
        return None

    if job.retention:
        storage = get_persistent_storage()
        return storage.load_bytes(job.output_ref)

    path = Path(job.output_ref)
    if not path.exists():
        return None
    return path.read_bytes()


def delete_ephemeral_after_download(job: Job) -> None:
    """For retention-OFF jobs, remove the output zip once it has been fetched."""
    if job.retention or not job.output_ref:
        return
    try:
        Path(job.output_ref).unlink(missing_ok=True)
    except Exception:
        pass
    shutil.rmtree(job_dir(job.id), ignore_errors=True)


def sweep_expired_ephemeral() -> int:
    """
    Safety net: delete ephemeral (retention-OFF) job artifacts older than the
    configured TTL, in case a user never downloads them. Returns count removed.
    """
    ttl = timedelta(minutes=settings.EPHEMERAL_TTL_MINUTES)
    cutoff = datetime.now(timezone.utc) - ttl
    removed = 0
    db = SessionLocal()
    try:
        stale = (
            db.query(Job)
            .filter(Job.retention.is_(False))
            .filter(Job.status == "done")
            .filter(Job.finished_at.isnot(None))
            .all()
        )
        for job in stale:
            finished = job.finished_at
            if finished is None:
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if finished < cutoff and job.output_ref:
                if Path(job.output_ref).exists():
                    delete_ephemeral_after_download(job)
                    job.output_ref = None
                    removed += 1
        if removed:
            db.commit()
    finally:
        db.close()
    return removed
