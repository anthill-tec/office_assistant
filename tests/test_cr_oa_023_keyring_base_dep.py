"""CR-OA-023 §S2 — `keyring` becomes a base dependency.

Verifies the NEW-contract AC before the GREEN phase lands it:

  §S2 the built wheel's `dist-info/METADATA` lists `keyring` in `Requires-Dist` WITHOUT
      an `extra ==` marker (a true base dependency, installed unconditionally), and NO
      `Provides-Extra: mail` remains carrying an extra-gated keyring requirement. The
      `[mail]` optional extra is retired entirely.

Today `pyproject.toml` declares `keyring>=24` only under
`[project.optional-dependencies] mail = ["keyring>=24"]` — an OPTIONAL extra. So the
built wheel's METADATA carries `Requires-Dist: keyring>=24; extra == "mail"` (extra-gated)
plus `Provides-Extra: mail`, and NOT an unconditional `Requires-Dist: keyring...` line.
Both assertions below MUST fail until CR-OA-023 §S2's GREEN phase moves `keyring` into
`[project] dependencies` and retires the `[mail]` extra.

Per CR-OA-023 §S2's own AC text, verification MUST be against the BUILT wheel's METADATA
(via `python -m build`), NOT by parsing `pyproject.toml` — reading pyproject.toml would
only prove intent, not what actually ships. This mirrors the CR-OA-018 §S5
`test_wheel_metadata_declares_gpl3` pattern in `tests/test_cr_oa_018_packaging.py`, except
the wheel is built ONCE in `setUpClass` (build is the slow part; ~seconds) and its METADATA
is reused across every test method here.
"""
import email
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class KeyringBaseDependencyTest(unittest.TestCase):
    """§S2 — the BUILT wheel's dist-info/METADATA declares keyring as a base dep."""

    @classmethod
    def setUpClass(cls):
        pytest.importorskip("build")

        cls.outdir = tempfile.mkdtemp(prefix="cr-oa-023-wheel-")
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", cls.outdir],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.outdir, ignore_errors=True)
            raise AssertionError(
                f"wheel build failed:\nstdout={result.stdout}\nstderr={result.stderr}"
            )

        wheels = glob.glob(os.path.join(cls.outdir, "*.whl"))
        if len(wheels) != 1:
            shutil.rmtree(cls.outdir, ignore_errors=True)
            raise AssertionError(
                f"expected exactly one built wheel in {cls.outdir}, found: {wheels}"
            )

        import zipfile

        with zipfile.ZipFile(wheels[0]) as zf:
            metadata_names = [
                n for n in zf.namelist() if n.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                shutil.rmtree(cls.outdir, ignore_errors=True)
                raise AssertionError(
                    "expected exactly one dist-info/METADATA entry, found: "
                    f"{metadata_names}"
                )
            cls.metadata_text = zf.read(metadata_names[0]).decode("utf-8")

        # Parse with email.parser like a real installer would, so "Requires-Dist"
        # extraction matches production behaviour rather than ad-hoc string slicing.
        cls.metadata_msg = email.message_from_string(cls.metadata_text)
        cls.requires_dist = cls.metadata_msg.get_all("Requires-Dist") or []
        cls.provides_extra = cls.metadata_msg.get_all("Provides-Extra") or []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "outdir", ""), ignore_errors=True)

    def test_keyring_is_base_requires_dist_without_extra_marker(self):
        """keyring must appear as an unconditional Requires-Dist (no `; extra == ...`)."""
        keyring_lines = [
            line for line in self.requires_dist if line.lower().startswith("keyring")
        ]
        self.assertTrue(
            keyring_lines,
            "expected at least one 'Requires-Dist: keyring...' line in the built "
            f"wheel METADATA; got Requires-Dist entries: {self.requires_dist}",
        )

        base_keyring_lines = [
            line for line in keyring_lines if "extra ==" not in line
        ]
        self.assertEqual(
            len(base_keyring_lines), 1,
            "expected exactly ONE unconditional (no 'extra ==' marker) "
            "'Requires-Dist: keyring' line — keyring must be a BASE dependency, not "
            f"gated behind an extra; got keyring Requires-Dist lines: {keyring_lines}",
        )
        self.assertRegex(
            base_keyring_lines[0],
            r"^Requires-Dist:\s*keyring",
            f"expected the base keyring requirement line to declare 'keyring', got: "
            f"{base_keyring_lines[0]!r}",
        )

    def test_mail_extra_is_retired_and_carries_no_keyring_requirement(self):
        """The `[mail]` extra is retired: no Provides-Extra: mail with a gated keyring dep."""
        mail_extra_present = any(
            extra.strip() == "mail" for extra in self.provides_extra
        )

        # PEP 508 marker quoting varies by build tool (hatchling emits single quotes:
        # `extra == 'mail'`; others may emit double quotes) — match either so the
        # assertion checks the semantic marker, not one specific quote style.
        keyring_gated_to_mail = any(
            line.lower().startswith("keyring")
            and "extra ==" in line
            and "mail" in line
            for line in self.requires_dist
        )

        self.assertFalse(
            mail_extra_present and keyring_gated_to_mail,
            "expected the '[mail]' extra to be retired with no keyring requirement "
            "gated behind it, but found Provides-Extra: mail "
            f"(present={mail_extra_present}) alongside an extra-gated keyring "
            f"Requires-Dist line (found={keyring_gated_to_mail}); "
            f"Provides-Extra entries: {self.provides_extra}; "
            f"Requires-Dist entries: {self.requires_dist}",
        )
        self.assertFalse(
            keyring_gated_to_mail,
            "expected NO 'Requires-Dist: keyring...; extra == \"mail\"'-style line "
            f"(any quote style) in the built wheel METADATA; got Requires-Dist "
            f"entries: {self.requires_dist}",
        )


class KeyringImportableSanityTest(unittest.TestCase):
    """Light guard: keyring is a real, importable package in the current venv."""

    def test_keyring_importable_in_current_environment(self):
        import keyring

        self.assertTrue(
            hasattr(keyring, "get_password"),
            "expected the 'keyring' package to expose 'get_password'",
        )


if __name__ == "__main__":
    unittest.main()
