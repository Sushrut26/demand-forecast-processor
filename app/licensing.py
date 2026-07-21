"""
Server-side licensing using Ed25519 signed tokens.

This evolves the desktop app's approach: keys remain self-contained and
tamper-proof (signed with a private key held only by the vendor), but the
server now owns time and account state, so there is no machine binding, no
world-time lookups, and no encrypted local file.

Token format (unchanged from the desktop app):  <payload_b64url>.<signature_b64url>
where payload is a JSON object:
    {
        "account_id": int,
        "customer_name": str,
        "issued_at": ISO-8601,
        "expires_at": ISO-8601,
        "product_code": str
    }

The admin signs tokens; anyone can verify with the derived public key. Access
is granted only while `expires_at` is in the future (checked against server
time).
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
    local testing, but such keys do not survive a restart, so production MUST
    set LICENSE_SIGNING_KEY (see tools/make_signing_key.py).
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
# Signing (admin) and verifying (activation / enforcement)
# ---------------------------------------------------------------------------

def generate_token(
    account_id: int,
    customer_name: str,
    duration_days: int,
    issued_at: Optional[datetime] = None,
) -> Tuple[str, datetime, datetime]:
    """
    Create a signed license token.

    Returns (token, issued_at, expires_at).
    """
    issued = issued_at or datetime.now(timezone.utc)
    expires = issued + timedelta(days=duration_days)

    payload = {
        "account_id": int(account_id),
        "customer_name": customer_name.strip(),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "product_code": settings.PRODUCT_CODE,
    }

    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _PRIVATE_KEY.sign(payload_bytes)

    token = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
    return token, issued, expires


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def verify_token(token: str) -> Tuple[bool, str, Optional[dict]]:
    """
    Verify a token's signature and product code.

    Returns (is_valid, message, payload). Does NOT check expiry — callers decide
    how to treat an expired-but-authentic token.
    """
    try:
        payload_b64, signature_b64 = token.strip().split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature_bytes = _b64url_decode(signature_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"Invalid license token format: {e}", None

    try:
        _PUBLIC_KEY.verify(signature_bytes, payload_bytes)
    except InvalidSignature:
        return False, "License token signature is invalid", None

    if payload.get("product_code") != settings.PRODUCT_CODE:
        return False, "License token is for a different product", None

    return True, "License token is valid", payload


def validate_for_account(token: str, account_id: int) -> Tuple[bool, str, Optional[dict]]:
    """
    Full activation-time validation: signature + product + account match + not
    expired.
    """
    is_valid, message, payload = verify_token(token)
    if not is_valid:
        return False, message, None

    if int(payload.get("account_id", -1)) != int(account_id):
        return False, "This key was issued for a different account.", None

    expires = _parse_iso(payload["expires_at"])
    if expires <= datetime.now(timezone.utc):
        return False, "This key has already expired. Please request a new key.", None

    return True, "License token is valid", payload


def format_key_for_display(token: str) -> str:
    """The full token is what the customer pastes; returned as-is."""
    return token
