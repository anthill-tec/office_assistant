---
name: cicd-release-convention
description: "CI/CD release model — automated PyPI publish from main (git-flow + hatch-vcs), manual TestPyPI dry-run; skill ships as a public GitHub repo. 1.0.0 shipped."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 99afe2dc-d6bb-44f2-8156-92ea1d4df250
  modified: 2026-07-27T00:00:00.000Z
---

**1.0.0 SHIPPED (2026-07-27):** `vidushi-oa 1.0.0` is live on PyPI (https://pypi.org/project/vidushi-oa/,
`uv tool install vidushi-oa` → `voa`); the skill is public on `main`
(`npx skills add anthill-tec/office_assistant/skills/vidushi-oa`). Repo:
**`anthill-tec/office_assistant`** (public). The model below is what actually shipped — it evolved from
earlier notes during the release, so trust this version.

## Release model (GitHub Actions + git-flow + hatch-vcs) — as shipped

- **Production branch is `main`** (git-flow `gitflow.branch.master = main`; there is NO `master` branch).
  Everything targets `refs/heads/main`.
- **Version is single-sourced from the git TAG via hatch-vcs.** `pyproject.toml` has
  `dynamic = ["version"]` + `[tool.hatch.version] source = "vcs"` (`local_scheme = "no-local-version"`);
  there is **no static `[project].version` and no version-bump commit**. `git flow release finish 1.2.3`
  tags `main` with `1.2.3` (no-`v`; `gitflow.prefix.versiontag = ""`), and hatch-vcs makes the built
  version **== the tag by construction**. An untagged commit builds a dev version (e.g. `0.1.1.devN`).
- **PyPI publish TRIGGER = push to `main`; the SemVer tag is a SAFETY CHECK, not the trigger.** The `publish`
  job is `if: github.ref == 'refs/heads/main'`; a **gate step** checks HEAD carries a clean SemVer tag AND
  `git merge-base --is-ancestor $SHA origin/main` — if there's **no tag it SKIPS (stays green)**, so ordinary
  main commits don't red-CI; only a tagged, on-main commit publishes. No version-literal compare (hatch-vcs =
  tag). No manual-reviewer gate — `main` + git-flow discipline IS the gate.
- **TestPyPI is a MANUAL `workflow_dispatch` dry-run** (NOT a `release/*` push trigger, per the sandesh
  model). Validate the deploy with `gh workflow run ci.yml --ref <branch>` before the real release; it
  uploads a dev version (`skip-existing: true` makes it idempotent).
- **`test` job** (build wheel + pytest + release gate) runs on every push; needs **`fetch-depth: 0`**
  (hatch-vcs reads tags). Publisher jobs also use `fetch-depth: 0` + **`twine check dist/*`**.
- **OIDC trusted publishing**, no stored token. Each publisher job needs `permissions: id-token: write`
  **AND `contents: read`** — a `permissions:` block resets all other scopes to none, so without
  `contents: read` `actions/checkout` fails ("Repository not found") on a private repo. **Trusted publishers
  are configured on the index side under owner `anthill-tec`**: TestPyPI (env `testpypi`) + PyPI (env `pypi`),
  project `vidushi-oa`, repo `office_assistant`, workflow `ci.yml` (pending publishers auto-create the project
  on first publish). GitHub `testpypi`/`pypi` environments auto-create on first use.
- **The `release/*` branch is where release-qualification runs** before finishing: the **`no-mistakes`
  workflow** (the `no-mistakes` skill — it caught the entire mail-error-handling hardening + real deploy bugs)
  and **AXI conformance validation**. These are release-branch *process* steps, not CI jobs. NOTE: no-mistakes
  commits its fixes to its **gate remote** (`~/.no-mistakes/...`), NOT your local branch — after a run,
  reconcile your local to the gate ref (`git fetch no-mistakes <branch>` then rebase/reset) before continuing.

## Skill publishing — just a public GitHub repo (NOT an "AXI-catalog submission")

Correcting an earlier note: publishing the skill is **not** a marketplace/catalog submission. The Vercel
Agent Skills ecosystem (`npx skills`) **auto-discovers/indexes skills from any public GitHub repo** — there
is no publish step. The bundle at `skills/vidushi-oa/SKILL.md` (valid frontmatter, `agentskills validate`-
clean) is installable the moment the repo is public and the skill is on `main`:
`npx skills add anthill-tec/office_assistant/skills/vidushi-oa` (or the whole repo). No CI job needed. See
[[vercel-skills-bundle-packaging]]. Ref: https://vercel.com/docs/agent-resources/skills.

## OSS license — GPL-3.0-or-later (enables CI + public PyPI)

The repo is licensed **GPL-3.0-or-later** (user's choice, 2026-07-26): an OSS license is the gate for a
public GitHub repo → free Actions CI + public PyPI. GPL v3 (2007) is the latest GNU GPL; SPDX id
`GPL-3.0-or-later` (bare `GPL-3.0` deprecated). A verbatim **`LICENSE`** at repo root (GitHub auto-detects →
GPLv3 badge); `pyproject.toml` `license = "GPL-3.0-or-later"` → wheel `License-Expression`. **Do NOT also add
a `License ::` trove classifier** (PEP 639: the SPDX expression supersedes it). Rationale:
`docs/research/DN-packaging-distribution.md` §6. Delivered CR-OA-018 §S5.

## CI provisioning principle + release-gate

- **PRINCIPLE (user, 2026-07-26): provision the CI environment to meet the tests' real requirements — do NOT
  weaken/skip tests to pass in a deficient runner.** When a test passes locally but fails on the runner,
  ADD the capability to CI. For this repo: (a) mongo-backed tests → a `services: mongodb` container; (b) tests
  that use the repo `.venv/bin/python` (`CleanVenvPackagingTest`) → create + run the suite inside a repo-root
  `.venv`; (c) the keyring-fallback test → a REAL storable keyring (`keyrings.alt`, hermetic temp file);
  (d) the snapshot import-parity test → the test GENERATES synthetic `data/*.jsonl` fixtures at setUp + tears
  them down (never the user's gitignored personal data, never a hardcoded row count). (All done.)
- **Release-gate script is vendored at `scripts/skill-release-gate.py`** (a copy of the generic
  `~/.claude/scripts/skill-release-gate.py`, since a runner has no `~/.claude`); CI runs
  `.venv/bin/python scripts/skill-release-gate.py --project-dir .`. Keep the copy in sync with the home
  source. The per-project config is **`.skill-release.toml`** (its stale `7`→`8` store count is fixed).
- **Test the CI workflow locally with `act`** — `act -l` for a fast parse-check (catches YAML errors, the
  Node-actions bump, etc.), a full `act push`/`act workflow_dispatch` to exercise jobs in Docker. (Caveat: a
  local mongod holding `27017` blocks act's own `mongo:7` service — stop it for a full act run.)
- **Live mail-account verification (CR-OA-020) is a release-time test, not a CI gate** — the mail client
  ships tested only against in-process fakes. Set up real Gmail/Fastmail/Yahoo accounts (secrets via the
  vault/keyring resolver) to exercise the real adapters + the XOAUTH2 path end-to-end, as a release-branch
  step. Keep it out of the pytest gate (no creds on runners).
- **Minor follow-up:** bump `actions/checkout@v4` → `@v5` and `setup-python@v5` → `@v6` (Node-20 deprecation
  warning; non-blocking).

## Release cutover — closes the full development cycle

- **Preserve agent state — do NOT lose it.** Prior-session state lives in the **Mongo `vidushi_oa` store**
  ([[mongo-preexisting-data-migration]]) + the **chezmoi-versioned `data/*.jsonl` snapshots** (gitignored
  here). The user **keeps the same data**; local backend stays `VIDUSHI_BACKEND=mongo`.
- **Prune redundant `~/.claude` artifacts** superseded by the bundle: the 7 legacy standalone skills + the
  `inbox-analyst` agent (AGENTS.md "Replacement path"), + any local scripts now shipped in the package.
- **Reinstall from the published artifacts** (now possible — 1.0.0 is on PyPI): `uv tool install vidushi-oa`
  (engine) + `npx skills add anthill-tec/office_assistant/skills/vidushi-oa` (skill) + `voa setup`; confirm
  the pruned environment works from the package alone and the preserved state still reads.

## Remote CI tracking

Use the **`ci-monitor` skill** to watch runs after pushing (main push → PyPI publish; `workflow_dispatch` →
TestPyPI) — track packaging/deploy without leaving the session.

**Why:** matches the git-flow release mechanism the user uses everywhere; **`main` is the gate**, automated,
no manual-approval reviewer. TestPyPI is a manual dry-run. **How to apply:** when authoring any CI/CD publish
workflow for this user, gate the production publish to a tagged, on-`main` commit (skip green otherwise);
never propose a manual-approval gate. See [[vercel-skills-bundle-packaging]].
