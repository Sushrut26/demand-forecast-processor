"""
Database engine and session management (SQLAlchemy).

Uses the DATABASE_URL from settings. Defaults to a local SQLite file for
zero-setup development; set DATABASE_URL to the Supabase / Postgres connection
string in production.
"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

Base = declarative_base()

_connect_args = {}
if settings.is_sqlite:
    # SQLite lives on a file path; make sure the parent directory exists.
    db_path = settings.DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Called on application startup."""
    # Import models so they are registered on the Base metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
