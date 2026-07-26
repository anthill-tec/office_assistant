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

    def _with_block_values(self, step):
        with_block = step.get("with") if isinstance(step, dict) else None
        return with_block if isinstance(with_block, dict) else {}

    def test_ci_workflow_exists_and_builds_tests_gates(self):
        """§S6/§S7 — two-tier, git-flow publish model.

        Three jobs are required:
          - `test` (runs on push): builds the wheel, runs pytest, runs the
            Model-B release gate.
          - a test-publish job gated to `release/*` branches, whose publish
            step targets TestPyPI.
          - a production publish job gated to a version tag, with
            `permissions.id-token: write`, an `environment` named `pypi`
            (OIDC trusted-publisher scoping), and a
            `pypa/gh-action-pypi-publish` step.

        Manual-reviewer / required-approval gating is a repo setting, not
        YAML — deliberately NOT asserted here.
        """
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

        job_blobs = {
            name: self._all_step_strings(job.get("steps") if isinstance(job, dict) else None)
            for name, job in jobs.items()
        }

        # --- job 1: `test` — builds the wheel, runs pytest, runs the release gate ---
        test_job = None
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            blob = job_blobs[job_name]
            builds_wheel = "build" in blob and ("-m build" in blob or "python -m build" in blob)
            runs_pytest = "pytest" in blob
            runs_release_gate = "skill-release-gate.py" in blob
            if builds_wheel and runs_pytest and runs_release_gate:
                test_job = job_name
                break
        self.assertIsNotNone(
            test_job,
            "expected a `test` job whose steps build the wheel (python -m build), run "
            f"pytest, AND invoke skill-release-gate.py; jobs found: {job_blobs}",
        )

        # --- job 2: test-publish — gated to release/* branches, publishes to TestPyPI ---
        test_publish_job = None
        for job_name, job in jobs.items():
            if not isinstance(job, dict) or job_name == test_job:
                continue
            job_if = job.get("if", "")
            gated_to_release_branch = isinstance(job_if, str) and "refs/heads/release/" in job_if
            if not gated_to_release_branch:
                continue
            steps = job.get("steps") or []
            targets_testpypi = False
            for step in steps:
                if not isinstance(step, dict):
                    continue
                with_vals = self._with_block_values(step)
                repo_url = with_vals.get("repository-url") or with_vals.get("repository_url")
                if isinstance(repo_url, str) and "test.pypi.org" in repo_url:
                    targets_testpypi = True
                    break
                for key in ("run", "uses", "name"):
                    val = step.get(key)
                    if isinstance(val, str) and "testpypi" in val.lower():
                        targets_testpypi = True
                        break
                if targets_testpypi:
                    break
            if targets_testpypi:
                test_publish_job = job_name
                break
        self.assertIsNotNone(
            test_publish_job,
            "expected a test-publish job gated to release/* branches (job 'if' containing "
            "'refs/heads/release/') whose publish step targets TestPyPI (a "
            "with.repository-url/repository_url containing 'test.pypi.org', or a step "
            f"referencing 'testpypi'); jobs found: {list(jobs.keys())}",
        )

        # --- job 3: production publish — gated to a version tag, OIDC-scoped ---
        production_job = None
        for job_name, job in jobs.items():
            if not isinstance(job, dict) or job_name in (test_job, test_publish_job):
                continue
            job_if = job.get("if", "")
            gated_to_tag = isinstance(job_if, str) and "refs/tags/" in job_if
            if not gated_to_tag:
                continue
            permissions = job.get("permissions") or {}
            has_id_token_write = (
                isinstance(permissions, dict) and permissions.get("id-token") == "write"
            )
            environment = job.get("environment")
            env_name = (
                environment.get("name")
                if isinstance(environment, dict)
                else environment
            )
            environment_is_pypi = env_name == "pypi"
            blob = job_blobs[job_name]
            uses_gh_action_pypi_publish = "pypa/gh-action-pypi-publish" in blob
            if has_id_token_write and environment_is_pypi and uses_gh_action_pypi_publish:
                production_job = job_name
                break
        self.assertIsNotNone(
            production_job,
            "expected a production publish job gated to a version tag (job 'if' containing "
            "'refs/tags/') with permissions.id-token: write, an environment named 'pypi' "
            "(OIDC trusted-publisher scoping), and a pypa/gh-action-pypi-publish step; "
            f"jobs found: { {name: jobs[name] for name in jobs} }",
        )


if __name__ == "__main__":
    unittest.main()
