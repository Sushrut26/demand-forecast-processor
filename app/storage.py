"""
Pluggable persistent storage for retention-ON accounts.

When an account's `store_data` flag is ON, a job's input workbook and output zip
are persisted here and remain downloadable. When it is OFF, files never reach
this layer — they live only in the ephemeral work directory during the job and
are deleted immediately after download (handled in app.jobs).

Backend selection:
    - Supabase Storage when SUPABASE_URL + SUPABASE_SERVICE_KEY are configured.
    - A local directory otherwise (handy for development).
"""

from pathlib import Path
from typing import Optional

from app.config import settings


class LocalStorage:
    """Filesystem-backed persistent storage (development / self-hosted)."""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def save_file(self, key: str, source_path: str) -> str:
        dest = self.base / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(source_path).read_bytes())
        return key

    def load_bytes(self, key: str) -> Optional[bytes]:
        path = self.base / key
        if not path.exists():
            return None
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self.base / key
        if path.exists():
            path.unlink()


class SupabaseStorage:
    """Supabase Storage bucket backend."""

    def __init__(self, url: str, service_key: str, bucket: str) -> None:
        from supabase import create_client

        self.client = create_client(url, service_key)
        self.bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self.client.storage.create_bucket(self.bucket, options={"public": False})
        except Exception:
            # Already exists (or insufficient perms to create) — proceed.
            pass

    def save_file(self, key: str, source_path: str) -> str:
        data = Path(source_path).read_bytes()
        storage = self.client.storage.from_(self.bucket)
        try:
            storage.upload(
                path=key,
                file=data,
                file_options={"upsert": "true", "content-type": "application/octet-stream"},
            )
        except Exception:
            # Fallback for client versions with a different signature.
            storage.upload(key, data)
        return key

    def load_bytes(self, key: str) -> Optional[bytes]:
        try:
            return self.client.storage.from_(self.bucket).download(key)
        except Exception:
            return None

    def delete(self, key: str) -> None:
        try:
            self.client.storage.from_(self.bucket).remove([key])
        except Exception:
            pass


def get_persistent_storage():
    """Return the configured persistent storage backend (singleton-ish)."""
    if settings.storage_configured:
        return SupabaseStorage(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
            settings.SUPABASE_BUCKET,
        )
    return LocalStorage(settings.LOCAL_STORAGE_DIR)
