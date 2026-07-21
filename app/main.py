"""
Application entry point.

Wires together auth, user, and admin routers; seeds the bootstrap admin on
first startup; and launches the background forecast worker.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import SessionLocal, init_db
from app.jobs import ensure_worker
from app.models import User
from app.security import hash_password

app = FastAPI(title=settings.APP_NAME)

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def seed_admin() -> None:
    """Create the bootstrap admin user from env if no admin exists yet."""
    db = SessionLocal()
    try:
        existing_admin = db.query(User).filter(User.role == "admin").first()
        if existing_admin is not None:
            return
        admin = User(
            account_id=None,
            email=settings.ADMIN_EMAIL.strip().lower(),
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    seed_admin()
    ensure_worker()


@app.exception_handler(HTTPException)
async def auth_redirect_handler(request: Request, exc: HTTPException):
    """Send unauthenticated browser requests to the login page."""
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# Routers are imported after app creation to avoid circular imports.
from app.auth import router as auth_router  # noqa: E402
from app.routes_admin import router as admin_router  # noqa: E402
from app.routes_user import router as user_router  # noqa: E402

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(user_router)
