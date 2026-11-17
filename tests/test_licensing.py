"""Tests for signed license token generation and validation (app/licensing.py)."""

from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing import (
    _PUBLIC_KEY, generate_token, is_expired, validate_customer, verify_token,
    verify_with_public_key_pem,
)

_PUBLIC_KEY_PEM = _PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")


def test_token_roundtrip_valid():
    token, issued, expires = generate_token(customer_name="Acme", duration_days=2)
    ok, msg, payload = verify_token(token)
    assert ok, msg
    assert payload["customer_name"] == "Acme"
    assert payload["product_code"]


def test_default_duration_used_when_omitted():
    token, issued, expires = generate_token(customer_name="Acme")
    assert (expires - issued).days == 2  # settings.DEFAULT_LICENSE_DAYS


def test_validate_customer_rejects_wrong_name():
    token, _, _ = generate_token(customer_name="Acme", duration_days=2)
    ok, msg, _ = validate_customer(token, customer_name="Wrong Co")
    assert not ok
    assert "does not match" in msg.lower()


def test_validate_customer_rejects_expired():
    past = datetime.now(timezone.utc) - timedelta(days=5)
    token, _, _ = generate_token(customer_name="Acme", duration_days=1, issued_at=past)
    ok, msg, _ = validate_customer(token, customer_name="Acme")
    assert not ok
    assert "expired" in msg.lower()


def test_validate_customer_accepts_valid_unexpired():
    token, _, _ = generate_token(customer_name="Acme", duration_days=2)
    ok, msg, payload = validate_customer(token, customer_name="acme")  # case-insensitive
    assert ok, msg
    assert payload["customer_name"] == "Acme"


def test_is_expired_uses_supplied_now():
    token, issued, expires = generate_token(customer_name="Acme", duration_days=2)
    _, _, payload = verify_token(token)
    assert not is_expired(payload, now=issued)
    assert is_expired(payload, now=expires + timedelta(seconds=1))


def test_tampered_token_rejected():
    token, _, _ = generate_token(customer_name="Acme", duration_days=2)
    payload_b64, sig_b64 = token.split(".", 1)
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig_b64
    ok, _, _ = verify_token(tampered)
    assert not ok


def test_verify_with_public_key_pem_matches_verify_token():
    """This is the path the shipped desktop app actually uses: verification via
    an explicit public key PEM, with no private key or settings involved."""
    token, _, _ = generate_token(customer_name="Acme", duration_days=2)
    ok, msg, payload = verify_with_public_key_pem(token, _PUBLIC_KEY_PEM)
    assert ok, msg
    assert payload["customer_name"] == "Acme"


def test_verify_with_public_key_pem_rejects_wrong_key():
    token, _, _ = generate_token(customer_name="Acme", duration_days=2)
    other_public_pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    ok, _, _ = verify_with_public_key_pem(token, other_public_pem)
    assert not ok
