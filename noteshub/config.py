"""Small, explicit process configuration; no default account/password."""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cookie_secure: bool = True
    max_upload_bytes: int = 2 * 1024 * 1024
    preview_seconds: int = 3600
    session_seconds: int = 12 * 3600

    @classmethod
    def from_env(cls):
        secure = os.environ.get("NOTESHUB_COOKIE_SECURE", "true").lower()
        if secure not in {"true", "false"}:
            raise ValueError("NOTESHUB_COOKIE_SECURE 只能是 true 或 false")
        return cls(
            Path(os.environ.get("NOTESHUB_DATA_DIR", "data")).resolve(),
            cookie_secure=secure == "true",
        )
