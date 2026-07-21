"""
Admin routes: provision accounts and their first user, toggle the locked
data-retention flag, issue and revoke time-limited license keys, and review
global job history.
"""

from datetime import timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.licensing import generate_token
from app.models import Account, Job, License, User, utcnow
from app.security import hash_password, require_admin
from app.templating import templates

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.created_at.desc()).all()
    recent_jobs = db.query(Job).order_by(Job.created_at.desc()).limit(25).all()
    return templates.TemplateResponse(
        "admin/home.html",
        {
            "request": request,
            "admin": admin,
            "accounts": accounts,
            "recent_jobs": recent_jobs,
            "default_license_days": settings.DEFAULT_LICENSE_DAYS,
        },
    )


@router.post("/accounts")
def create_account(
    name: str = Form(...),
    store_data: str = Form("off"),
    user_email: str = Form(...),
    user_password: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = name.strip()
    email = user_email.strip().lower()

    if db.query(Account).filter(Account.name == name).first():
        return RedirectResponse("/admin?msg=Account+name+already+exists.", status_code=303)
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse("/admin?msg=A+user+with+that+email+already+exists.", status_code=303)

    account = Account(name=name, store_data=(store_data == "on"))
    db.add(account)
    db.flush()

    user = User(
        account_id=account.id,
        email=email,
        password_hash=hash_password(user_password),
        role="user",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return RedirectResponse(f"/admin/accounts/{account.id}?msg=Account+created.", status_code=303)


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(
    account_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        return RedirectResponse("/admin?msg=Account+not+found.", status_code=303)

    jobs = db.query(Job).filter(Job.account_id == account_id).order_by(Job.created_at.desc()).limit(50).all()
    licenses = sorted(account.licenses, key=lambda l: l.issued_at, reverse=True)
    return templates.TemplateResponse(
        "admin/account.html",
        {
            "request": request,
            "admin": admin,
            "account": account,
            "jobs": jobs,
            "licenses": licenses,
            "default_license_days": settings.DEFAULT_LICENSE_DAYS,
        },
    )


@router.post("/accounts/{account_id}/store-data")
def toggle_store_data(
    account_id: int,
    store_data: str = Form("off"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        return RedirectResponse("/admin?msg=Account+not+found.", status_code=303)
    account.store_data = (store_data == "on")
    db.commit()
    return RedirectResponse(
        f"/admin/accounts/{account_id}?msg=Data+retention+updated.", status_code=303
    )


@router.post("/accounts/{account_id}/users")
def add_user(
    account_id: int,
    user_email: str = Form(...),
    user_password: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        return RedirectResponse("/admin?msg=Account+not+found.", status_code=303)

    email = user_email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse(
            f"/admin/accounts/{account_id}?msg=Email+already+in+use.", status_code=303
        )

    db.add(User(
        account_id=account.id,
        email=email,
        password_hash=hash_password(user_password),
        role="user",
        is_active=True,
    ))
    db.commit()
    return RedirectResponse(f"/admin/accounts/{account_id}?msg=User+added.", status_code=303)


@router.post("/accounts/{account_id}/licenses")
def generate_license(
    account_id: int,
    duration_days: int = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        return RedirectResponse("/admin?msg=Account+not+found.", status_code=303)

    duration = max(0, int(duration_days))
    token, issued, expires = generate_token(
        account_id=account.id,
        customer_name=account.name,
        duration_days=duration,
    )

    lic = License(
        account_id=account.id,
        key=token,
        customer_name=account.name,
        issued_at=issued,
        expires_at=expires,
        duration_days=duration,
        status="issued",
        created_by=admin.email,
    )
    db.add(lic)

    # Issuing a key immediately grants the account access up to its expiry
    # (the customer still needs the key string to use the app elsewhere, but
    # for the web platform the admin has already enabled them).
    current = account.access_until
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    account.access_until = expires if (current is None or expires > current) else current

    db.commit()
    db.refresh(lic)
    return RedirectResponse(
        f"/admin/accounts/{account_id}?new_key={lic.id}&msg=License+key+generated.",
        status_code=303,
    )


@router.post("/licenses/{license_id}/revoke")
def revoke_license(
    license_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lic = db.get(License, license_id)
    if lic is None:
        return RedirectResponse("/admin?msg=License+not+found.", status_code=303)

    lic.status = "revoked"
    account = lic.account

    # Recompute account access from remaining non-revoked, unexpired licenses.
    best = None
    for other in account.licenses:
        if other.id == lic.id or other.status == "revoked":
            continue
        exp = other.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > utcnow() and (best is None or exp > best):
            best = exp
    account.access_until = best

    db.commit()
    return RedirectResponse(
        f"/admin/accounts/{account.id}?msg=License+revoked.", status_code=303
    )
