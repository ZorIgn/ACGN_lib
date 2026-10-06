"""Generate an installation secret without replacing existing encryption keys."""

import os
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

MIN_SECRET_LENGTH = 32
PLACEHOLDERS = {
    "longstring",
    "generate-a-random-secret-before-starting",
    "ifx7bdUWo5EwC2NQNihjRjOrW00Cdv5Y",
}


def installation_secret(
    configured: str | None,
    path: Path,
    *,
    existing_database: Path | None = None,
    external_database: bool = False,
) -> str:
    """Keep explicit keys; atomically publish a private, persistent fallback."""
    if configured:
        if configured in PLACEHOLDERS:
            message = (
                "SECRET is a public placeholder. Back up and migrate encrypted "
                "credentials before replacing this key."
            )
            raise ImproperlyConfigured(message)
        return configured
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if external_database or (
            existing_database is not None
            and existing_database.exists()
            and existing_database.stat().st_size > 0
        ):
            message = (
                "SECRET is missing for an existing or external database. Restore the "
                "previous key and migrate encrypted credentials "
                "before generating a replacement."
            )
            raise ImproperlyConfigured(message)
        fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.flush()
                os.fsync(handle.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            Path(temporary).unlink()
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < MIN_SECRET_LENGTH or value in PLACEHOLDERS:
        message = "Invalid persisted SECRET; restore its backup."
        raise ImproperlyConfigured(message)
    return value
