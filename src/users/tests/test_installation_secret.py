"""Installation secrets stay stable across workers and reject public keys."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.persistent_secret import installation_secret


class InstallationSecretTest(SimpleTestCase):
    """Cover persistence, permissions, concurrency and safe migration refusal."""

    def test_installation_key_persists_and_is_private(self):
        """All concurrent initializers receive the same private persisted key."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db" / ".app-secret"
            with ThreadPoolExecutor(max_workers=16) as pool:
                values = list(
                    pool.map(lambda _: installation_secret(None, path), range(32))
                )
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(installation_secret(None, path), values[0])
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertEqual(
                installation_secret("existing-private-key", path),
                "existing-private-key",
            )

    def test_public_placeholder_rejected_without_silent_rotation(self):
        """Reject known public keys without generating an incompatible replacement."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".secret"
            for value in ("longstring", "ifx7bdUWo5EwC2NQNihjRjOrW00Cdv5Y"):
                with (
                    self.subTest(value=value),
                    self.assertRaisesMessage(ImproperlyConfigured, "migrate encrypted"),
                ):
                    installation_secret(value, path)
                self.assertFalse(path.exists())

    def test_missing_key_for_existing_database_does_not_rotate(self):
        """An old database without its key requires explicit restoration/migration."""
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / ".app-secret"
            database = Path(directory) / "db.sqlite3"
            database.write_bytes(b"existing database")
            with self.assertRaisesMessage(
                ImproperlyConfigured, "Restore the previous key"
            ):
                installation_secret(None, key, existing_database=database)
            self.assertFalse(key.exists())
            with self.assertRaisesMessage(ImproperlyConfigured, "external database"):
                installation_secret(None, key, external_database=True)
            self.assertFalse(key.exists())
            key.write_text("existing-private-key-which-must-survive")
            self.assertEqual(
                installation_secret(None, key, existing_database=database),
                "existing-private-key-which-must-survive",
            )
