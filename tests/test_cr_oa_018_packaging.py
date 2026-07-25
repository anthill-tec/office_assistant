"""CR-OA-018 §S5/§S6 — GPL-3.0 license + PyPI packaging + CI.

Verifies the NEW-contract ACs before licensing/CI land:

  §S5 a top-level `LICENSE` file carrying the full GNU GPLv3 text, and the built wheel's
      `dist-info/METADATA` declaring that license (either a `License-Expression:
      GPL-3.0-or-later` line or the classic `License :: OSI Approved :: GNU General
      Public License v3 or later (GPLv3+)` classifier).
  §S6 a GitHub Actions workflow under `.github/workflows/` that (a) triggers on push,
      (b) has a job whose steps build the wheel, run pytest, AND invoke the Model-B
      release gate (`skill-release-gate.py`), and (c) has a job that publishes to PyPI
      gated on a version tag.

Today NEITHER a top-level `LICENSE` file NOR any `.github/workflows/*.yml` exist, and
`pyproject.toml` declares no license/classifiers at all — every test below MUST fail
until CR-OA-018 §S5/§S6's GREEN phase lands the license file, pyproject license metadata,
and the workflow.

`test_wheel_metadata_declares_gpl3` actually BUILDS the wheel via `python -m build` (this
is the only trustworthy way to verify what ships in `METADATA` — reading pyproject.toml
would only prove intent, not packaging correctness) and is therefore slow.
"""
import glob
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LICENSE_PATH = os.path.join(ROOT, "LICENSE")
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")


class LicenseFileTest(unittest.TestCase):
    """§S5 — a top-level LICENSE file carries the full GPLv3 text."""

    def test_license_file_is_gpl3(self):
        self.assertTrue(
            os.path.isfile(LICENSE_PATH),
            f"expected a top-level LICENSE file at {LICENSE_PATH}",
        )
        with open(LICENSE_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(
            "GNU GENERAL PUBLIC LICENSE", text,
            "LICENSE must contain the GPL header text 'GNU GENERAL PUBLIC LICENSE'",
        )
        self.assertIn(
            "Version 3", text,
            "LICENSE must declare 'Version 3'",
        )


class WheelMetadataTest(unittest.TestCase):
    """§S5 — the BUILT wheel's dist-info/METADATA declares GPL-3.0-or-later."""

    def test_wheel_metadata_declares_gpl3(self):
        pytest.importorskip("build")

        with tempfile.TemporaryDirectory() as outdir:
            result = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", outdir],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(
                result.returncode, 0,
                f"wheel build failed:\nstdout={result.stdout}\nstderr={result.stderr}",
            )

            wheels = glob.glob(os.path.join(outdir, "*.whl"))
            self.assertEqual(
                len(wheels), 1,
                f"expected exactly one built wheel in {outdir}, found: {wheels}",
            )

            with zipfile.ZipFile(wheels[0]) as zf:
                metadata_names = [
                    n for n in zf.namelist()
                    if n.endswith(".dist-info/METADATA")
                ]
                self.assertEqual(
                    len(metadata_names), 1,
                    f"expected exactly one dist-info/METADATA entry, found: {metadata_names}",
                )
                metadata_text = zf.read(metadata_names[0]).decode("utf-8")

        declares_expression = "License-Expression: GPL-3.0-or-later" in metadata_text
        declares_classifier = (
            "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)"
            in metadata_text
        )
        self.assertTrue(
            declares_expression or declares_classifier,
            "expected built wheel METADATA to declare GPL-3.0-or-later via either a "
            "'License-Expression: GPL-3.0-or-later' line or the "
            "'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)' "
            f"classifier; got METADATA:\n{metadata_text}",
        )


class CIWorkflowTest(unittest.TestCase):
    """§S6 — a GitHub Actions workflow authored (parse-validated, not executed)."""

    def setUp(self):
        yaml = pytest.importorskip("yaml")
        self.yaml = yaml
        files = sorted(
            glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml"))
            + glob.glob(os.path.join(WORKFLOWS_DIR, "*.yaml"))
        )
        self.assertTrue(
            files,
            f"expected at least one GitHub Actions workflow file under {WORKFLOWS_DIR}",
        )
        self.workflow_path = files[0]
        with open(self.workflow_path, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)
        self.assertIsInstance(
            self.doc, dict,
            f"expected {self.workflow_path} to parse as a YAML mapping, got {type(self.doc)}",
        )

    def _on_section(self):
        # PyYAML (YAML 1.1) parses the bare key `on:` as the boolean True, so accept
        # either spelling rather than pinning one.
        if "on" in self.doc:
            return self.doc["on"]
        if True in self.doc:
            return self.doc[True]
        return None

    def _all_step_strings(self, steps):
        """Flatten every textual field of a steps list into one search blob."""
        blob = []
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            for key in ("run", "uses", "name"):
                val = step.get(key)
                if isinstance(val, str):
                    blob.append(val)
            with_block = step.get("with")
            if isinstance(with_block, dict):
                for val in with_block.values():
                    if isinstance(val, str):
                        blob.append(val)
        return "\n".join(blob)

    def test_ci_workflow_exists_and_builds_tests_gates(self):
        on_section = self._on_section()
        self.assertIsNotNone(
            on_section,
            f"expected an 'on:' trigger section in {self.workflow_path}, got keys: {list(self.doc.keys())}",
        )
        if isinstance(on_section, dict):
            triggers_on_push = "push" in on_section
        elif isinstance(on_section, list):
            triggers_on_push = "push" in on_section
        else:
            triggers_on_push = on_section == "push"
        self.assertTrue(
            triggers_on_push,
            f"expected the workflow to trigger on push, got 'on': {on_section!r}",
        )

        jobs = self.doc.get("jobs")
        self.assertIsInstance(
            jobs, dict,
            f"expected a 'jobs' mapping in {self.workflow_path}, got {jobs!r}",
        )
        self.assertTrue(jobs, f"expected at least one job in {self.workflow_path}")

        build_test_gate_job = None
        for job_name, job in jobs.items():
            blob = self._all_step_strings(job.get("steps")) if isinstance(job, dict) else ""
            builds_wheel = "build" in blob and ("-m build" in blob or "python -m build" in blob)
            runs_pytest = "pytest" in blob
            runs_release_gate = "skill-release-gate.py" in blob
            if builds_wheel and runs_pytest and runs_release_gate:
                build_test_gate_job = job_name
                break
        self.assertIsNotNone(
            build_test_gate_job,
            "expected a job whose steps build the wheel (python -m build), run pytest, "
            "AND invoke skill-release-gate.py; jobs found: "
            f"{ {name: self._all_step_strings(j.get('steps') if isinstance(j, dict) else None) for name, j in jobs.items()} }",
        )

        publish_job = None
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            blob = self._all_step_strings(job.get("steps")).lower()
            publishes_to_pypi = "pypi" in blob
            if not publishes_to_pypi:
                continue
            job_if = job.get("if", "")
            gated_by_job_if = isinstance(job_if, str) and "refs/tags/" in job_if
            on_push_tags = isinstance(on_section, dict) and isinstance(on_section.get("push"), dict) and bool(
                on_section["push"].get("tags")
            )
            if gated_by_job_if or on_push_tags:
                publish_job = job_name
                break
        self.assertIsNotNone(
            publish_job,
            "expected a job that publishes to PyPI gated on a version tag "
            "(either job-level 'if' referencing refs/tags/, or 'on: push: tags:'); "
            f"jobs found: {list(jobs.keys())}",
        )


if __name__ == "__main__":
    unittest.main()
