"""Guards the mandatory release process: the gate's hermetic backend pin and the
AGENTS.md checklist claims that an agent would act on irreversibly.

The checklist in AGENTS.md ("Release process — MANDATORY") tells an agent which pushes
publish to PyPI. A published version can never be re-uploaded, so a claim that drifts
out of sync with `.github/workflows/ci.yml` is a real hazard, not a doc nit. These tests
pin each claim to the workflow that backs it.
"""
import os
import re
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class ReleaseGateEnvPinTest(unittest.TestCase):
    """`.skill-release.toml` [env] must make the gate hermetic against the ambient env."""

    def setUp(self):
        with open(os.path.join(ROOT, ".skill-release.toml"), "rb") as fh:
            self.env = tomllib.load(fh).get("env", {})

    def test_backend_pinned_to_shipped_sqlite_default(self):
        # The gate inherits os.environ; an ambient VIDUSHI_BACKEND=mongo would otherwise
        # make the checks exercise Mongo instead of the SQLite default users install on.
        self.assertEqual(self.env.get("VIDUSHI_BACKEND"), "sqlite")

    def test_sqlite_path_pinned_inside_the_throwaway_tmp_dir(self):
        # Unset, sqlite falls back to $XDG_DATA_HOME/vidushi-oa/oa.db — the operator's real store.
        self.assertEqual(self.env.get("VIDUSHI_SQLITE_PATH"), "$TMP/oa.db")

    def test_data_dir_also_stays_in_the_throwaway_tmp_dir(self):
        self.assertTrue(str(self.env.get("VIDUSHI_DATA_DIR", "")).startswith("$TMP"))


class ReleaseChecklistMatchesCiTest(unittest.TestCase):
    """Every AGENTS.md claim about what publishes must hold in ci.yml."""

    def setUp(self):
        self.agents = _read("AGENTS.md")
        self.ci = _read(".github", "workflows", "ci.yml")

    def test_agents_md_carries_the_mandatory_release_section(self):
        self.assertIn("## Release process — MANDATORY (never skip a step)", self.agents)
        for step in ("no-mistakes", "TestPyPI dry-run", "release gate", "irreversibly"):
            self.assertIn(step, self.agents)

    def test_push_trigger_has_no_tags_filter_so_main_and_tags_must_be_one_push(self):
        # Claim: "CI's `push` trigger filters on `branches:` only (no `tags:`)".
        on_block = self.ci.split("jobs:", 1)[0]
        self.assertIn("branches:", on_block)
        self.assertNotIn("tags:", on_block)
        self.assertIn("git push origin main --tags", self.agents)

    def test_publish_job_is_ref_gated_only_so_a_dispatch_on_main_really_publishes(self):
        # Claim: "the production `publish` job is gated on `github.ref == 'refs/heads/main'`
        # with no event-type guard" — hence "Never dispatch against `main`".
        publish = self.ci.split("  publish:", 1)[1]
        gate = re.search(r"^    if: (.+)$", publish, re.M).group(1).strip()
        self.assertEqual(gate, "github.ref == 'refs/heads/main'")
        self.assertNotIn("github.event_name", gate)
        self.assertIn("Never dispatch against `main`", self.agents)

    def test_testpypi_dry_run_is_the_manual_dispatch_job(self):
        # Claim: dispatch a release branch to get a TestPyPI dry-run via `test-publish`.
        test_publish = self.ci.split("  test-publish:", 1)[1].split("  publish:", 1)[0]
        self.assertIn("if: github.event_name == 'workflow_dispatch'", test_publish)
        self.assertIn("https://test.pypi.org/legacy/", test_publish)
        self.assertIn("gh workflow run ci.yml --ref release/X.Y.Z", self.agents)

    def test_untagged_main_head_skips_the_publish_green(self):
        # Claim: pushing main alone "runs a build whose gate step finds no tag and skips green".
        self.assertIn("git tag --points-at HEAD", self.ci)
        self.assertIn('echo "publish=false" >> "$GITHUB_OUTPUT"', self.ci)

    def test_suite_prerequisites_documented_match_ci(self):
        # Claim: the suite needs a live mongod + the [mongo] extra, which CI supplies.
        self.assertIn("mongo:7", self.ci)
        self.assertIn('pip install -e ".[mongo,sqlite,test]"', self.ci)
        self.assertIn("127.0.0.1:27017", self.agents)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
