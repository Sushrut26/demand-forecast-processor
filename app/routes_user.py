"""
User-facing routes: dashboard, license activation, job upload/run, status
polling, and result download.
"""

import io
from datetime import timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.jobs import (
    delete_ephemeral_after_download, enqueue_job, job_dir, load_output_bytes,
)
from app.licensing import validate_for_account
from app.models import Job, License, User, utcnow
from app.security import require_user
from app.templating import templates

router = APIRouter()


def _account_licenses_refresh(db: Session, account) -> None:
    """Flip any active/issued licenses to 'expired' when past their expiry."""
    changed = False
    for lic in account.licenses:
        if lic.status in ("issued", "active") and lic.is_expired():
            lic.status = "expired"
            changed = True
    if changed:
        db.commit()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.is_admin:
        return RedirectResponse("/admin", status_code=303)

    account = user.account
    if account is not None:
        _account_licenses_refresh(db, account)

    jobs = (
        db.query(Job)
        .filter(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(50)
        .all()
    )

    has_access = account.has_active_access() if account else False
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "jobs": jobs,
            "has_access": has_access,
            "max_upload_mb": settings.MAX_UPLOAD_MB,
        },
    )


@router.post("/activate")
def activate_license(
    request: Request,
    license_key: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = user.account
    if account is None:
        return RedirectResponse("/?msg=No+account+assigned.+Contact+support.", status_code=303)

    token = license_key.strip()
    is_valid, message, payload = validate_for_account(token, account.id)
    if not is_valid:
        return RedirectResponse(f"/?msg={message.replace(' ', '+')}", status_code=303)

    # Find the matching stored license (issued by admin) or create a record.
    lic = db.query(License).filter(License.account_id == account.id, License.key == token).first()
    from app.licensing import _parse_iso  # local import to avoid cycle at module load

    expires = _parse_iso(payload["expires_at"])
    if lic is None:
        issued = _parse_iso(payload["issued_at"])
        lic = License(
            account_id=account.id,
            key=token,
            customer_name=payload.get("customer_name", account.name),
            issued_at=issued,
            expires_at=expires,
            duration_days=max(0, (expires - issued).days),
            status="active",
            created_by="self-activation",
        )
        db.add(lic)

    lic.status = "active"
    lic.activated_at = utcnow()

    # Extend account access to the later of current or this token's expiry.
    current = account.access_until
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    account.access_until = expires if (current is None or expires > current) else current

    db.commit()
    return RedirectResponse("/?msg=License+activated+successfully.", status_code=303)


def _validate_upload(upload: UploadFile, raw: bytes) -> str:
    """Return an error message if the upload violates guardrails, else ''."""
    name = (upload.filename or "").lower()
    if not (name.endswith(".xlsx") or name.endswith(".xlsm")):
        return "Please upload an Excel file (.xlsx or .xlsm)."

    size_mb = len(raw) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_MB:
        return f"File is too large ({size_mb:.1f} MB). Maximum is {settings.MAX_UPLOAD_MB} MB."

    # Cheap dimension check with openpyxl read-only to protect memory.
    try:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows = ws.max_row or 0
        cols = ws.max_column or 0
        wb.close()
        if rows > settings.MAX_ROWS:
            return f"Too many rows ({rows}). Maximum is {settings.MAX_ROWS}."
        # First column is dates; the rest are category columns.
        if cols - 1 > settings.MAX_CATEGORY_COLUMNS:
            return f"Too many category columns ({cols - 1}). Maximum is {settings.MAX_CATEGORY_COLUMNS}."
    except Exception:
        return "The file could not be read as an Excel workbook."

    return ""


@router.post("/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = user.account
    if account is None or not account.has_active_access():
        return RedirectResponse(
            "/?msg=Your+license+has+expired.+Please+activate+a+new+key+to+run+jobs.",
            status_code=303,
        )

    raw = await file.read()
    error = _validate_upload(file, raw)
    if error:
        return RedirectResponse(f"/?msg={error.replace(' ', '+')}", status_code=303)

    # Create the job first so we have an id for the work directory.
    job = Job(
        account_id=account.id,
        user_id=user.id,
        original_filename=file.filename,
        status="queued",
        retention=bool(account.store_data),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    input_path = job_dir(job.id) / "input.xlsx"
    input_path.write_bytes(raw)
    job.input_ref = str(input_path)
    db.commit()

    enqueue_job(job.id)
    return RedirectResponse(f"/?msg=Job+%23{job.id}+queued.", status_code=303)


@router.get("/jobs/{job_id}/status")
def job_status(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "has_download": bool(job.output_ref) and job.status == "done",
        "error": bool(job.error),
    }


@router.get("/jobs/{job_id}/download")
def job_download(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        return RedirectResponse("/?msg=Job+not+found.", status_code=303)

    data = load_output_bytes(job)
    if data is None:
        return RedirectResponse("/?msg=Results+are+no+longer+available.", status_code=303)

    filename = f"forecast_results_job_{job.id}.zip"
    response = StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

    # For retention-OFF jobs, delete the artifact right after handing it over.
    if not job.retention:
        delete_ephemeral_after_download(job)
        job.output_ref = None
        db.commit()

    return response
