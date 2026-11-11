"""Public packaging and runtime-only setup must preserve private installations."""

import importlib
import io
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
release = importlib.import_module("build_release")
installer = importlib.import_module("install")


class ReleaseTests(unittest.TestCase):
    def test_archive_is_deterministic_contains_frontend_and_excludes_private_data(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            target = release.build_release(directory)
            first = target.read_bytes()
            release.build_release(directory)
            self.assertEqual(first, target.read_bytes())
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                self.assertIn("frontend/panel.js", names)
                self.assertIn("brand/icon.png", names)
                self.assertIn("manifest.json", names)
                self.assertFalse(
                    any(
                        n.startswith(("custom_components/", ".storage/", ".personal_secretary/"))
                        or n.endswith(("auth.json", ".bundle", ".pyc"))
                        for n in names
                    )
                )

    def test_runtime_only_preserves_managed_component_and_existing_authorization(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            private = root / ".personal_secretary"
            (private / "bin").mkdir(parents=True)
            (private / "codex-home").mkdir()
            (private / "bin/codex").write_bytes(b"existing executable")
            (private / "codex-home/auth.json").write_text("existing-private-auth")
            managed = root / "custom_components/personal_secretary/manifest.json"
            managed.parent.mkdir(parents=True)
            managed.write_text("managed-by-marketplace")
            argv = [
                "install.py",
                "--runtime-only",
                "--reuse-login",
                "--ha-config",
                str(root),
                "--codex-home",
                str(root / "not-present"),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(installer.shutil, "which", return_value=None),
            ):
                installer.main()
            self.assertEqual(managed.read_text(), "managed-by-marketplace")
            self.assertEqual(
                (private / "codex-home/auth.json").read_text(), "existing-private-auth"
            )

    def test_failed_auth_copy_does_not_leave_empty_cache(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            private = root / ".personal_secretary"
            (private / "bin").mkdir(parents=True)
            (private / "bin/codex").write_bytes(b"existing executable")
            argv = [
                "install.py",
                "--runtime-only",
                "--reuse-login",
                "--ha-config",
                str(root),
                "--codex-home",
                str(root / "missing"),
            ]
            with patch.object(sys, "argv", argv), self.assertRaises(FileNotFoundError):
                installer.main()
            self.assertFalse((private / "codex-home/auth.json").exists())
