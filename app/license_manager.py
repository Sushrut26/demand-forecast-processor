"""
Desktop license manager: local encrypted license storage, tamper-resistant
time lookup, and signed-token activation/status checks.

Deliberately has no GUI dependency (no tkinter) so it can be unit tested and
reused independently of the Tkinter shell in desktop_app.py.

Licensing model: the app ships with only a *public* Ed25519 key baked in
(PUBLIC_KEY_PEM below). An admin holds the matching *private* key and issues
signed, time-limited tokens with tools/generate_license.py (default validity:
2 days, configurable per key). A customer activates a token; it's verified
locally and, once accepted, stored encrypted on disk. On every launch the
license is re-checked against a tamper-resistant time source (a world-time
API, falling back to the system clock when offline) so a rolled-back system
clock cannot extend access. When a key lapses, the customer is blocked until
an admin issues a fresh one.
"""

import base64
import hashlib
import json
import os
import platform
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests import RequestException

from app.licensing import is_expired, parse_iso, verify_with_public_key_pem


class LicenseManager:
    PRODUCT_CODE = "DEMAND_FORECAST_PROCESSOR"

    # Public key only — pairs with the private key held by whoever runs
    # tools/generate_license.py. Generate a real pair once with
    # tools/make_signing_key.py, keep the private half secret, and paste the
    # public half here before shipping the app. A LICENSE_PUBLIC_KEY env var
    # override is supported for local development/testing so this file does
    # not need to be edited to try out a fresh key pair.
    _DEFAULT_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAUjoF1GeTIg6ljXtSRGyW24JqU06iqCGwdsVN8ODw5E8=
-----END PUBLIC KEY-----"""
    PUBLIC_KEY_PEM = os.environ.get("LICENSE_PUBLIC_KEY", "").strip() or _DEFAULT_PUBLIC_KEY_PEM

    def __init__(self):
        # Local obfuscation key for the on-disk license file. This is not a
        # security boundary (the signature check is); it just keeps casual
        # editing of license.dat from succeeding.
        self._k1, self._k2, self._k3, self._k4, self._k5 = "Gd@rj", "Opt#", "2025!", "fCsT", "pR"

        if getattr(sys, "frozen", False):
            app_dir = Path(sys.executable).parent.resolve()
        else:
            app_dir = Path(__file__).resolve().parent.parent
        self.app_dir = app_dir

        if platform.system() == "Windows":
            appdata = os.environ.get("LOCALAPPDATA", "")
            self.license_dir = (
                Path(appdata) / "DemandForecastProcessor" / "data"
                if appdata
                else app_dir / "forecast_appdata"
            )
        else:
            self.license_dir = app_dir / "forecast_appdata"

        self.license_file = self.license_dir / "license.dat"
        self.license_log = str(self.license_dir.parent / "app.log")
        self.allow_offline_mode = True

        self.time_apis = [
            {"url": "https://worldtimeapi.org/api/timezone/Asia/Kolkata", "parser": self._parse_worldtime},
            {"url": "https://timeapi.io/api/Time/current/zone?timeZone=Asia/Kolkata", "parser": self._parse_timeapi},
        ]

    # --- logging -----------------------------------------------------------

    def _log_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            Path(self.license_log).parent.mkdir(parents=True, exist_ok=True)
            with open(self.license_log, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {message}\n")
        except OSError as e:
            print(f"Warning: Could not write to license log: {e}")

    # --- local encrypted storage --------------------------------------------

    def _canonical_json(self, value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _get_encryption_key(self) -> bytes:
        return (self._k1 + self._k2 + self._k3 + self._k4 + self._k5).encode()

    def _integrity_pepper(self) -> str:
        return f"{self.PRODUCT_CODE}:{hashlib.sha256(self.PUBLIC_KEY_PEM.encode()).hexdigest()}"

    def _calculate_integrity(self, data_dict: dict) -> str:
        data_copy = {k: v for k, v in data_dict.items() if not k.startswith("_")}
        data_str = self._canonical_json(data_copy)
        return hashlib.sha256((data_str + self._integrity_pepper()).encode()).hexdigest()[:16]

    def _encrypt_data(self, data_dict: dict) -> "str | None":
        try:
            payload = dict(data_dict)
            payload["_integrity"] = self._calculate_integrity(payload)
            json_str = self._canonical_json(payload)
            compressed = zlib.compress(json_str.encode())
            key = self._get_encryption_key()
            encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(compressed))
            return base64.b64encode(encrypted).decode()
        except (TypeError, ValueError, zlib.error) as e:
            self._log_event(f"ERROR: Encryption failed - {e}")
            return None

    def _decrypt_data(self, encoded_str: str):
        try:
            encrypted = base64.b64decode(encoded_str.encode())
            key = self._get_encryption_key()
            decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(encrypted))
            data_dict = json.loads(zlib.decompress(decrypted).decode())

            stored_integrity = data_dict.pop("_integrity", None)
            if stored_integrity != self._calculate_integrity(data_dict):
                self._log_event("ERROR: License file tampered - integrity check failed")
                return None, "License file has been modified or corrupted."

            return data_dict, None
        except (ValueError, json.JSONDecodeError, zlib.error) as e:
            self._log_event(f"ERROR: Decryption failed - {e}")
            return None, f"License validation failed: {e}"

    def _save_to_file(self, encrypted_data: str) -> bool:
        try:
            self.license_dir.mkdir(parents=True, exist_ok=True)
            with open(self.license_file, "w", encoding="utf-8") as f:
                f.write(encrypted_data)
            self._log_event(f"License data saved to: {self.license_file}")
            return True
        except OSError as e:
            self._log_event(f"ERROR: Failed to save license file - {e}")
            return False

    def _load_from_file(self) -> "str | None":
        try:
            if self.license_file.exists():
                return self.license_file.read_text(encoding="utf-8")
        except OSError as e:
            self._log_event(f"ERROR: Failed to load license file - {e}")
        return None

    def save_license_data(self, data_dict: dict) -> bool:
        encrypted = self._encrypt_data(data_dict)
        if not encrypted:
            return False
        return self._save_to_file(encrypted)

    def load_license_data(self):
        encrypted = self._load_from_file()
        if not encrypted:
            return None, None
        return self._decrypt_data(encrypted)

    # --- tamper-resistant time ----------------------------------------------

    def _parse_worldtime(self, response) -> datetime:
        data = response.json()
        return datetime.fromisoformat(data["datetime"].replace("Z", "+00:00")).astimezone(timezone.utc)

    def _parse_timeapi(self, response) -> datetime:
        data = response.json()
        naive = datetime.strptime(data["dateTime"].split(".")[0], "%Y-%m-%dT%H:%M:%S")
        # timeapi.io returns IST wall-clock time for this zone query; treat as such.
        return naive.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)

    def get_current_datetime(self) -> "tuple[datetime | None, str | None, str]":
        """Real current time (UTC) from a world-time API, falling back to the
        local system clock if offline. Returns (datetime, error, source), where
        source is 'api' or 'offline' — callers should only trust the system
        clock's ordering (for rollback detection) when source == 'offline',
        since an API-sourced time is authoritative regardless of local clock."""
        for api in self.time_apis:
            try:
                response = requests.get(api["url"], timeout=10)
                response.raise_for_status()
                dt = api["parser"](response)
                self._log_event(f"Time fetched from API: {dt.isoformat()} ({api['url']})")
                return dt, None, "api"
            except (RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                self._log_event(f"WARNING: Time API failed for {api['url']} - {e}")

        if self.allow_offline_mode:
            dt = datetime.now(timezone.utc)
            self._log_event(f"Offline mode - using system time: {dt.isoformat()}")
            return dt, None, "offline"

        self._log_event("ERROR: All time APIs failed and offline mode disabled")
        return None, "Network error: Unable to verify the current date/time.", "offline"

    # --- license activation & status ----------------------------------------

    def activate_license(self, customer_name: str, license_token: str):
        """Verify + store a signed license token. Returns (success, message)."""
        self._log_event(f"License activation attempt for: {customer_name}")

        is_valid, message, payload = verify_with_public_key_pem(license_token, self.PUBLIC_KEY_PEM)
        if not is_valid:
            self._log_event(f"Activation failed for {customer_name}: {message}")
            return False, message

        if payload.get("customer_name", "").strip().upper() != customer_name.strip().upper():
            return False, "Customer name does not match the license token."

        current_time, error, _source = self.get_current_datetime()
        if error:
            return False, error

        if is_expired(payload, current_time):
            self._log_event(f"Activation rejected — token already expired for {customer_name}")
            return False, "This license key has already expired. Please request a new key."

        data = {
            "customer_name": payload["customer_name"],
            "signed_license_token": license_token.strip(),
            "expires_at": payload["expires_at"],
            "activation_date": current_time.isoformat(),
            "last_check": current_time.isoformat(),
            "product_code": self.PRODUCT_CODE,
        }

        if self.save_license_data(data):
            self._log_event(f"License activated for {customer_name}, expires {payload['expires_at']}")
            return True, "License activated successfully!"

        return False, "Failed to save the license locally."

    def check_license_status(self):
        """
        Verify the locally stored license is present, authentic, and unexpired.

        Returns (has_access: bool, message: str, days_left: float | None).
        """
        self._log_event("=" * 60)
        self._log_event("Checking license status")

        data, error = self.load_license_data()
        if error:
            return False, error, None
        if data is None:
            return False, "Not activated. Please enter your license key.", None

        token = data.get("signed_license_token")
        if not token:
            return False, "Stored license is incomplete. Please re-activate.", None

        is_valid, message, payload = verify_with_public_key_pem(token, self.PUBLIC_KEY_PEM)
        if not is_valid:
            self._log_event(f"Stored license failed verification: {message}")
            return False, f"{message} Please contact support for a new key.", None

        current_time, error, source = self.get_current_datetime()
        if error:
            return False, error, None

        last_check = data.get("last_check")
        if last_check and source == "offline":
            # Only meaningful when we're relying on the local system clock —
            # an API-sourced time is authoritative regardless of local drift.
            if current_time < parse_iso(last_check) - timedelta(hours=1):
                self._log_event("ERROR: System time manipulation detected (system clock rolled back)")
                return False, "System clock manipulation detected. Please sync your system time and try again.", None

        data["last_check"] = current_time.isoformat()
        self.save_license_data(data)

        if is_expired(payload, current_time):
            expires_at = parse_iso(payload["expires_at"])
            self._log_event(f"License expired at {expires_at.isoformat()}")
            return (
                False,
                f"Your license expired on {expires_at.strftime('%Y-%m-%d %H:%M')} UTC. "
                "Please request a new key to continue.",
                None,
            )

        expires_at = parse_iso(payload["expires_at"])
        remaining = expires_at - current_time
        days_left = remaining.total_seconds() / 86400
        customer = payload.get("customer_name", "Licensed User")

        if days_left >= 1:
            msg = f"Licensed to {customer} — {int(days_left)}d {int((days_left % 1) * 24)}h remaining"
        else:
            hours_left = remaining.total_seconds() / 3600
            msg = f"Licensed to {customer} — {hours_left:.1f}h remaining. Renew soon."

        self._log_event(f"License valid: {msg}")
        return True, msg, days_left
