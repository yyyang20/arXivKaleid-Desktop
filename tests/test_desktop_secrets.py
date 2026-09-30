from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from desktop import pipeline, secrets
from test_desktop_pipeline import IsolatedDesktopTest


class DesktopSecretTests(IsolatedDesktopTest):
    def test_missing_secret_does_not_create_or_read_other_state(self):
        with patch("desktop.secrets._dpapi") as crypt:
            self.assertEqual(secrets.SecretStore().load(), "")
            crypt.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assert_no_analysis()

    def test_only_ciphertext_is_written_and_reloaded_without_logs(self):
        output = io.StringIO()
        writes = []
        original = os.replace

        def replace(source, target):
            writes.append(source.read_bytes())
            return original(source, target)

        with patch("desktop.secrets._dpapi", side_effect=[b"synthetic-ciphertext", b"fake-key-only-for-tests"]) as crypt, patch("desktop.secrets.os.replace", side_effect=replace), redirect_stdout(output), redirect_stderr(output):
            store = secrets.SecretStore()
            store.save("fake-key-only-for-tests")
            self.assertEqual(store.load(), "fake-key-only-for-tests")
        path = pipeline.runtime_path("config", "secret.dat")
        self.assertEqual(path, self.root / ".desktop-runtime/config/secret.dat")
        self.assertEqual(path.read_bytes(), b"synthetic-ciphertext")
        self.assertEqual(writes, [b"synthetic-ciphertext"])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(crypt.call_args_list[1].kwargs, {"decrypt": True})
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [path])

    def test_failed_save_preserves_previous_ciphertext_and_safe_errors(self):
        store = secrets.SecretStore()
        with patch("desktop.secrets._dpapi", return_value=b"old-ciphertext"):
            store.save("fake-old")
        for target in ("desktop.secrets._dpapi", "desktop.secrets.os.replace"):
            with patch("desktop.secrets._dpapi", return_value=b"new-ciphertext"), patch(target, side_effect=RuntimeError("fake-key-private-detail")):
                with self.assertRaises(secrets.SecretError) as caught:
                    store.save("fake-new")
            self.assertNotIn("private-detail", str(caught.exception))
            self.assertEqual(pipeline.runtime_path("config", "secret.dat").read_bytes(), b"old-ciphertext")
        self.assertEqual(len(list(pipeline.runtime_path("config").iterdir())), 1)

    def test_corrupt_secret_safe_error(self):
        path = pipeline.runtime_path("config", "secret.dat")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"bad-data")
        with patch("desktop.secrets._dpapi", side_effect=RuntimeError("fake-private-detail")):
            with self.assertRaises(secrets.SecretError) as caught:
                secrets.SecretStore().load()
        self.assertNotIn("private-detail", str(caught.exception))

    def test_windows_dpapi_round_trip_empty_value_and_corruption(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI requires Windows")
        store = secrets.SecretStore()
        for value in ("fake-key-for-windows-test-测试", ""):
            store.save(value)
            self.assertEqual(secrets.SecretStore().load(), value)
            if value:
                self.assertNotIn(value.encode(), pipeline.runtime_path("config", "secret.dat").read_bytes())
        with self.assertRaises(secrets.SecretError):
            secrets._dpapi(b"invalid-dpapi-ciphertext", decrypt=True)
