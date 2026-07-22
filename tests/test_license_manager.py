"""Tests for the desktop LicenseManager (app/license_manager.py): local
encrypted storage, activation, and expiry-aware status checks. The
tamper-resistant time source is monkeypatched so these tests never hit the
real world-time APIs.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.license_manager import LicenseManager


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_signed_token(private_key, customer_name, duration_days, issued_at=None):
    issued = issued_at or datetime.now(timezone.utc)
    expires = issued + timedelta(days=duration_days)
    payload = {
        "customer_name": customer_name,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "product_code": LicenseManager.PRODUCT_CODE,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    token = f"{_b64url(payload_bytes)}.{_b64url(signature)}"
    return token, issued, expires


def make_manager(tmp_path, private_key) -> LicenseManager:
    mgr = LicenseManager()
    mgr.PUBLIC_KEY_PEM = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    mgr.license_dir = tmp_path / "license"
    mgr.license_file = mgr.license_dir / "license.dat"
    mgr.license_log = str(tmp_path / "app.log")
    return mgr


@pytest.fixture
def keypair():
    return Ed25519PrivateKey.generate()


def test_not_activated_before_any_key(tmp_path, keypair):
    mgr = make_manager(tmp_path, keypair)
    has_access, message, days_left = mgr.check_license_status()
    assert not has_access
    assert "not activated" in message.lower()
    assert days_left is None


def test_activate_then_status_reports_remaining_time(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    token, issued, expires = make_signed_token(keypair, "Acme", duration_days=2)

    now = issued + timedelta(hours=1)
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (now, None, "api"))

    ok, msg = mgr.activate_license("Acme", token)
    assert ok, msg

    has_access, message, days_left = mgr.check_license_status()
    assert has_access
    assert days_left is not None and days_left > 1.5
    assert "Acme" in message


def test_activate_rejects_already_expired_token(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    past = datetime.now(timezone.utc) - timedelta(days=5)
    token, issued, expires = make_signed_token(keypair, "Acme", duration_days=1, issued_at=past)

    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (datetime.now(timezone.utc), None, "api"))
    ok, msg = mgr.activate_license("Acme", token)
    assert not ok
    assert "expired" in msg.lower()

    # Nothing should have been persisted.
    data, _ = mgr.load_license_data()
    assert data is None


def test_activate_rejects_customer_name_mismatch(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    token, issued, expires = make_signed_token(keypair, "Acme", duration_days=2)
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (issued, None, "api"))

    ok, msg = mgr.activate_license("Wrong Co", token)
    assert not ok
    assert "does not match" in msg.lower()


def test_activate_rejects_token_signed_by_different_key(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    other_key = Ed25519PrivateKey.generate()
    token, issued, expires = make_signed_token(other_key, "Acme", duration_days=2)
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (issued, None, "api"))

    ok, msg = mgr.activate_license("Acme", token)
    assert not ok


def test_status_transitions_to_expired_after_time_passes(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    issued = datetime.now(timezone.utc)
    token, issued, expires = make_signed_token(keypair, "Acme", duration_days=1, issued_at=issued)

    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (issued + timedelta(hours=1), None, "api"))
    ok, msg = mgr.activate_license("Acme", token)
    assert ok, msg

    # Renewal not issued yet; time now moves past expiry.
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (expires + timedelta(hours=1), None, "api"))
    has_access, message, days_left = mgr.check_license_status()
    assert not has_access
    assert "expired" in message.lower()
    assert days_left is None


def test_renewal_restores_access_after_expiry(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    issued = datetime.now(timezone.utc)
    old_token, issued, old_expires = make_signed_token(keypair, "Acme", duration_days=1, issued_at=issued)

    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (issued + timedelta(hours=1), None, "api"))
    assert mgr.activate_license("Acme", old_token)[0]

    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (old_expires + timedelta(hours=1), None, "api"))
    assert not mgr.check_license_status()[0]

    # Admin issues a fresh key; customer activates it.
    new_token, new_issued, new_expires = make_signed_token(
        keypair, "Acme", duration_days=2, issued_at=old_expires + timedelta(hours=1)
    )
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (new_issued, None, "api"))
    ok, msg = mgr.activate_license("Acme", new_token)
    assert ok, msg

    has_access, message, days_left = mgr.check_license_status()
    assert has_access
    assert days_left > 1.5


def test_tampered_local_file_is_rejected(tmp_path, keypair, monkeypatch):
    mgr = make_manager(tmp_path, keypair)
    token, issued, expires = make_signed_token(keypair, "Acme", duration_days=2)
    monkeypatch.setattr(mgr, "get_current_datetime", lambda: (issued, None, "api"))
    assert mgr.activate_license("Acme", token)[0]

    # Corrupt the on-disk file directly.
    mgr.license_file.write_text("not-valid-encrypted-data", encoding="utf-8")

    has_access, message, days_left = mgr.check_license_status()
    assert not has_access
    assert days_left is None
