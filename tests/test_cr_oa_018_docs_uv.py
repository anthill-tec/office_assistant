"""CR-OA-018 §S7 — docs point at `uv tool install`, README documents the SQLite-default /
Mongo-opt-in backend story, and the built wheel actually installs + runs via `uv tool
install` end to end.

Today README.md / scripts/README.md / skills/vidushi-oa/SKILL.md do not mention `uv tool
install vidushi-oa` at all, and README.md does not yet document SQLite as the
zero-config default backend with Mongo as an opt-in `[mongo]` extra — every doc-text
assertion below MUST fail until CR-OA-018 §S7's GREEN phase lands the doc updates.

`test_uv_tool_install_yields_voa` actually builds the wheel and does a real, ISOLATED
`uv tool install` (its own UV_TOOL_DIR/UV_TOOL_BIN_DIR under a tempdir — it must never
touch the developer's real `~/.local` uv tool state) to prove the packaged CLI is
actually installable and runnable end to end, not just that the docs claim it is.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
README_PATH = os.path.join(ROOT, "README.md")
SCRIPTS_README_PATH = os.path.join(ROOT, "scripts", "README.md")
SKILL_MD_PATH = os.path.join(ROOT, "skills", "vidushi-oa", "SKILL.md")

UV_INSTALL_SNIPPET = "uv tool install vidushi-oa"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class DocsMentionUvToolInstallTest(unittest.TestCase):
    """§S7 — README.md, scripts/README.md, and the skill's SKILL.md each document the
    `uv tool install vidushi-oa` install path."""

    def test_readme_mentions_uv_tool_install(self):
        self.assertTrue(
            os.path.isfile(README_PATH), f"expected {README_PATH} to exist"
        )
        text = _read(README_PATH)
        self.assertIn(
            UV_INSTALL_SNIPPET, text,
            f"expected README.md to document '{UV_INSTALL_SNIPPET}'",
        )

    def test_scripts_readme_mentions_uv_tool_install(self):
        self.assertTrue(
            os.path.isfile(SCRIPTS_README_PATH),
            f"expected {SCRIPTS_README_PATH} to exist",
        )
        text = _read(SCRIPTS_README_PATH)
        self.assertIn(
            UV_INSTALL_SNIPPET, text,
            f"expected scripts/README.md to document '{UV_INSTALL_SNIPPET}'",
        )

    def test_skill_md_mentions_uv_tool_install(self):
        self.assertTrue(
            os.path.isfile(SKILL_MD_PATH), f"expected {SKILL_MD_PATH} to exist"
        )
        text = _read(SKILL_MD_PATH)
        self.assertIn(
            UV_INSTALL_SNIPPET, text,
            f"expected skills/vidushi-oa/SKILL.md to document '{UV_INSTALL_SNIPPET}'",
        )


class ReadmeBackendStoryTest(unittest.TestCase):
    """§S7 — README.md documents SQLite as the zero-config default backend and Mongo as
    an opt-in `[mongo]` extra."""

    def test_readme_documents_sqlite_default_and_mongo_extra(self):
        self.assertTrue(
            os.path.isfile(README_PATH), f"expected {README_PATH} to exist"
        )
        text = _read(README_PATH)

        mentions_sqlite_default = (
            "SQLite is the default" in text
            or "sqlite is the default" in text
            or "default backend is SQLite" in text
            or "defaults to SQLite" in text
            or "defaults to sqlite" in text
        )
        self.assertTrue(
            mentions_sqlite_default,
            "expected README.md to state that SQLite is the default (zero-config) "
            "backend; none of the expected SQLite-default phrasings were found",
        )

        self.assertIn(
            "[mongo]", text,
            "expected README.md to document Mongo as an opt-in '[mongo]' extra",
        )


class SkillValidatesTest(unittest.TestCase):
    """§S7 — `agentskills validate skills/vidushi-oa` exits 0, run inside a throwaway
    venv so this test never depends on (or pollutes) the project's own environment."""

    def test_skill_validates(self):
        skill_dir = os.path.join(ROOT, "skills", "vidushi-oa")
        self.assertTrue(
            os.path.isdir(skill_dir), f"expected skill directory at {skill_dir}"
        )

        tmp = tempfile.mkdtemp(prefix="cr018-skillvenv-")
        try:
            venv_result = subprocess.run(
                [sys.executable, "-m", "venv", tmp],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if venv_result.returncode != 0:
                self.skipTest(
                    f"could not create throwaway venv: {venv_result.stderr}"
                )

            venv_pip = os.path.join(tmp, "bin", "pip")
            venv_agentskills = os.path.join(tmp, "bin", "agentskills")

            try:
                install_result = subprocess.run(
                    [venv_pip, "install", "skills-ref"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                self.skipTest(f"pip install skills-ref unavailable: {exc}")

            if install_result.returncode != 0:
                self.skipTest(
                    "pip install skills-ref failed (likely no network): "
                    f"{install_result.stderr}"
                )

            if not os.path.isfile(venv_agentskills):
                self.skipTest(
                    f"skills-ref did not provide an 'agentskills' executable at "
                    f"{venv_agentskills}"
                )

            validate_result = subprocess.run(
                [venv_agentskills, "validate", skill_dir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                validate_result.returncode, 0,
                "expected 'agentskills validate skills/vidushi-oa' to exit 0:\n"
                f"stdout={validate_result.stdout}\nstderr={validate_result.stderr}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UvToolInstallEndToEndTest(unittest.TestCase):
    """§S7 — the built wheel actually installs via `uv tool install` (isolated) and the
    resulting `voa` console script runs."""

    def test_uv_tool_install_yields_voa(self):
        uv_path = shutil.which("uv")
        if uv_path is None:
            self.skipTest("uv is not installed on PATH")

        tmp = tempfile.mkdtemp(prefix="cr018-uvtool-")
        try:
            dist_dir = os.path.join(tmp, "dist")
            os.makedirs(dist_dir, exist_ok=True)

            build_result = subprocess.run(
                [
                    os.path.join(ROOT, ".venv", "bin", "python"),
                    "-m", "build", "--wheel", "--outdir", dist_dir,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(
                build_result.returncode, 0,
                f"wheel build failed:\nstdout={build_result.stdout}\n"
                f"stderr={build_result.stderr}",
            )

            wheels = [
                f for f in os.listdir(dist_dir) if f.endswith(".whl")
            ]
            self.assertEqual(
                len(wheels), 1,
                f"expected exactly one built wheel in {dist_dir}, found: {wheels}",
            )
            wheel_path = os.path.join(dist_dir, wheels[0])

            tool_dir = os.path.join(tmp, "tools")
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(tool_dir, exist_ok=True)
            os.makedirs(bin_dir, exist_ok=True)

            isolated_env = dict(os.environ)
            isolated_env["UV_TOOL_DIR"] = tool_dir
            isolated_env["UV_TOOL_BIN_DIR"] = bin_dir

            install_result = subprocess.run(
                [uv_path, "tool", "install", "--from", wheel_path, "vidushi-oa"],
                capture_output=True,
                text=True,
                timeout=300,
                env=isolated_env,
            )
            self.assertEqual(
                install_result.returncode, 0,
                "expected 'uv tool install --from <wheel> vidushi-oa' to succeed:\n"
                f"stdout={install_result.stdout}\nstderr={install_result.stderr}",
            )

            voa_path = os.path.join(bin_dir, "voa")
            self.assertTrue(
                os.path.isfile(voa_path),
                f"expected an isolated 'voa' console script at {voa_path} after "
                f"'uv tool install'; bin dir contents: {os.listdir(bin_dir) if os.path.isdir(bin_dir) else '<missing>'}",
            )

            # Sanity: this install must NOT have touched the developer's real uv tool
            # state — the isolated dirs are distinct from the default ~/.local ones.
            real_uv_tool_bin = os.path.expanduser("~/.local/bin")
            self.assertNotEqual(
                os.path.realpath(bin_dir), os.path.realpath(real_uv_tool_bin),
                "the isolated UV_TOOL_BIN_DIR must not resolve to the real "
                "~/.local/bin uv tool bin directory",
            )

            help_result = subprocess.run(
                [voa_path, "--help"],
                capture_output=True,
                text=True,
                timeout=60,
                env=isolated_env,
            )
            self.assertEqual(
                help_result.returncode, 0,
                "expected the isolated 'voa --help' to exit 0:\n"
                f"stdout={help_result.stdout}\nstderr={help_result.stderr}",
            )
            self.assertIn(
                "query", help_result.stdout,
                f"expected 'voa --help' output to mention the 'query' verb, got:\n"
                f"{help_result.stdout}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
