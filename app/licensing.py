"""
Ed25519 signed, time-limited license tokens for the desktop app.

Token format:  <payload_b64url>.<signature_b64url>
where payload is a JSON object:
    {
        "customer_name": str,
        "issued_at": ISO-8601,
        "expires_at": ISO-8601,
        "product_code": str
    }

Whoever holds the private key (LICENSE_SIGNING_KEY, generated once by
tools/make_signing_key.py) can mint tokens with tools/generate_license.py. The
desktop app only ever needs the public key (derived from the private key here,
or embedded directly in the shipped app) to verify a token's authenticity.
Verifying a signature only proves the token is authentic and unmodified; expiry
is checked separately by the caller against a tamper-resistant time source (see
desktop_app.py's world-time lookup), since local system clocks can't be trusted.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from app.config import settings


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def _load_private_key() -> Ed25519PrivateKey:
    """
    Load the Ed25519 private key from settings.LICENSE_SIGNING_KEY.

    Accepts a PEM string (possibly with escaped newlines) or a base64-encoded
    PEM. Falls back to a freshly generated ephemeral key when unset — fine for
    local testing, but such keys do not survive a restart, so issuing real
    licenses MUST set LICENSE_SIGNING_KEY (see tools/make_signing_key.py).
    """
    raw = settings.LICENSE_SIGNING_KEY.strip()
    if not raw:
        return Ed25519PrivateKey.generate()

    pem = raw.replace("\\n", "\n")
    if "BEGIN" not in pem:
        # Assume it's base64 of the PEM bytes.
        try:
            pem = base64.b64decode(raw).decode("utf-8")
        except Exception:
            pass

    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


# Loaded once at import; the signing key does not change during a process.
_PRIVATE_KEY = _load_private_key()
_PUBLIC_KEY: Ed25519PublicKey = _PRIVATE_KEY.public_key()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# ---------------------------------------------------------------------------
# Signing (admin tool) and verifying (the desktop app)
# ---------------------------------------------------------------------------

def generate_token(
    customer_name: str,
    duration_days: Optional[int] = None,
    issued_at: Optional[datetime] = None,
) -> Tuple[str, datetime, datetime]:
    """
    Create a signed license token.

    duration_days defaults to settings.DEFAULT_LICENSE_DAYS (2) when omitted —
    this is the admin-configurable renewal period.

    Returns (token, issued_at, expires_at).
    """
    if duration_days is None:
        duration_days = settings.DEFAULT_LICENSE_DAYS

    issued = issued_at or datetime.now(timezone.utc)
    expires = issued + timedelta(days=duration_days)

    payload = {
        "customer_name": customer_name.strip(),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "product_code": settings.PRODUCT_CODE,
    }

    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _PRIVATE_KEY.sign(payload_bytes)

    token = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
    return token, issued, expires


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _verify_with_key(token: str, public_key: Ed25519PublicKey) -> Tuple[bool, str, Optional[dict]]:
    """Signature + product-code check against an explicit public key. Does NOT check expiry."""
    try:
        payload_b64, signature_b64 = token.strip().split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature_bytes = _b64url_decode(signature_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"Invalid license token format: {e}", None

    try:
        public_key.verify(signature_bytes, payload_bytes)
    except InvalidSignature:
        return False, "License token signature is invalid", None

    if payload.get("product_code") != settings.PRODUCT_CODE:
        return False, "License token is for a different product", None

    return True, "License token is valid", payload


def verify_token(token: str) -> Tuple[bool, str, Optional[dict]]:
    """
    Verify a token's signature and product code using this process's key pair
    (loaded from LICENSE_SIGNING_KEY). Intended for the admin/CLI/test context
    where signing and verifying happen with the same key. The shipped desktop
    app should use verify_with_public_key_pem instead, since it must never load
    a private key.

    Returns (is_valid, message, payload). Does NOT check expiry — callers decide
    how to treat an expired-but-authentic token (see is_expired below).
    """
    return _verify_with_key(token, _PUBLIC_KEY)


def verify_with_public_key_pem(token: str, public_key_pem: str) -> Tuple[bool, str, Optional[dict]]:
    """
    Verify a token's signature and product code using an explicit public key
    PEM string, with no dependency on any private key or settings. This is what
    a distributed client (the desktop app) should call: it embeds only the
    public key, never the private key used to mint tokens.
    """
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    except Exception as e:
        return False, f"Invalid public key configured in the app: {e}", None

    return _verify_with_key(token, public_key)


def is_expired(payload: dict, now: Optional[datetime] = None) -> bool:
    """True if the token's expires_at is at or before `now` (defaults to real time)."""
    now = now or datetime.now(timezone.utc)
    expires = parse_iso(payload["expires_at"])
    return expires <= now


def validate_customer(token: str, customer_name: str, now: Optional[datetime] = None) -> Tuple[bool, str, Optional[dict]]:
    """
    Full activation-time validation: signature + product + customer name match +
    not expired (against `now`, which callers should source from a
    tamper-resistant clock rather than the local system clock).
    """
    is_valid, message, payload = verify_token(token)
    if not is_valid:
        return False, message, None

    if payload.get("customer_name", "").strip().upper() != customer_name.strip().upper():
        return False, "Customer name does not match the license token.", None

    if is_expired(payload, now):
        return False, "This key has already expired. Please request a new key.", None

    return True, "License token is valid", payload
