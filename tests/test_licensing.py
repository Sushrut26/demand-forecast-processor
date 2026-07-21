"""Tests for signed license token generation and validation."""

from datetime import datetime, timedelta, timezone

from app.licensing import generate_token, validate_for_account, verify_token


def test_token_roundtrip_valid():
    token, issued, expires = generate_token(account_id=7, customer_name="Acme", duration_days=2)
    ok, msg, payload = verify_token(token)
    assert ok, msg
    assert payload["account_id"] == 7
    assert payload["customer_name"] == "Acme"


def test_validate_for_account_rejects_wrong_account():
    token, _, _ = generate_token(account_id=1, customer_name="Acme", duration_days=2)
    ok, msg, _ = validate_for_account(token, account_id=2)
    assert not ok
    assert "different account" in msg.lower()


def test_validate_for_account_rejects_expired():
    past = datetime.now(timezone.utc) - timedelta(days=5)
    token, _, _ = generate_token(account_id=1, customer_name="Acme", duration_days=1, issued_at=past)
    ok, msg, _ = validate_for_account(token, account_id=1)
    assert not ok
    assert "expired" in msg.lower()


def test_tampered_token_rejected():
    token, _, _ = generate_token(account_id=1, customer_name="Acme", duration_days=2)
    payload_b64, sig_b64 = token.split(".", 1)
    # Flip a character in the payload.
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig_b64
    ok, _, _ = verify_token(tampered)
    assert not ok
