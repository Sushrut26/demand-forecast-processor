"""
SQLAlchemy ORM models.

The database is the source of truth for identity, licensing and data-retention.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    """
    A customer account. Multiple users can belong to one account. The
    data-retention flag and license access window are account-level and can
    only be changed by an admin.
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    # store_data is admin-only; locked (read-only) in the user UI.
    store_data = Column(Boolean, nullable=False, default=False)
    # access_until is derived from the latest activated license.
    access_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    users = relationship("User", back_populates="account", cascade="all, delete-orphan")
    licenses = relationship("License", back_populates="account", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="account", cascade="all, delete-orphan")

    def has_active_access(self) -> bool:
        if self.access_until is None:
            return False
        au = self.access_until
        if au.tzinfo is None:
            au = au.replace(tzinfo=timezone.utc)
        return au > utcnow()


class User(Base):
    """A login identity. Role is 'admin' or 'user'."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    account = relationship("Account", back_populates="users")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class License(Base):
    """
    A time-limited license key (a signed Ed25519 token). Issued by an admin for
    an account with a configurable duration; expires and is renewed by issuing a
    new key.
    """
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    key = Column(Text, nullable=False)
    customer_name = Column(String(255), nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    duration_days = Column(Integer, nullable=False)
    # status: 'issued' (not yet activated), 'active', 'expired', 'revoked'
    status = Column(String(20), nullable=False, default="issued")
    activated_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(255), nullable=True)

    account = relationship("Account", back_populates="licenses")

    def is_expired(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= utcnow()


class Job(Base):
    """A forecast run. Files are only persisted when retention was ON at run time."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    original_filename = Column(String(512), nullable=True)
    # status: 'queued', 'running', 'done', 'error'
    status = Column(String(20), nullable=False, default="queued")
    input_ref = Column(String(1024), nullable=True)
    output_ref = Column(String(1024), nullable=True)
    summary_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    # Snapshot of account.store_data at run time.
    retention = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    account = relationship("Account", back_populates="jobs")
    user = relationship("User")
