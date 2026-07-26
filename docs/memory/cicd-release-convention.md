---
name: cicd-release-convention
description: "CI/CD publish model — automated production release from master (git-flow), TestPyPI on release/*"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 99afe2dc-d6bb-44f2-8156-92ea1d4df250
  modified: 2026-07-26T06:00:11.207Z
---

The user's CI/CD release model (their standard across "almost every project"), for GitHub Actions + git-flow:

- **Production release is AUTOMATED from `master`.** The moment a git-flow `finish` lands on master (with its
  version tag) and is pushed, CI triggers on master and **automatically publishes to production PyPI**. The
  **gate is "only from master"** (the branch condition) — master only ever receives release-finish merges,
  so the git-flow discipline IS the human decision. **Do NOT add a manual-approval reviewer gate** to the
  production publish; that contradicts the model.
- **Release tags are plain SemVer — NO `v` prefix** (`1.2.3`, not `v1.2.3`), per the git-workflow rule
  (user choice, 2026-07-26). Three things must agree: the workflow triggers on
  `on.push.tags: "[0-9]+.[0-9]+.[0-9]+*"`; **git-flow must be set to match** —
  `git config gitflow.prefix.versiontag ""` (its default is `v`, and `.git/config` is **untracked**, so
  set this per-clone / at `git flow init`); and the skill already mandates no-`v`. Then
  `git flow release finish 1.2.3` tags `1.2.3` and the tag push fires the PyPI publish. **A `v`-prefixed
  tag would silently NOT trigger publish.** (Publish is gated `if: refs/tags/*` — it relies on the branch
  discipline that only release-finish creates tags; no per-tag master-reachability check.)
- **Test-publish to TestPyPI on `release/*` branch push** — packaging + deploy are validated there during
  `git flow release`, before the production release from master.
- `test` job (build wheel + pytest + release gate) runs on every push.
- Use OIDC trusted publishing (`permissions: id-token: write`, `environment: pypi`/`testpypi` for
  trusted-publisher scoping) — no long-lived stored token.
- The **`release/*` branch is also where the final release-qualification steps run**: the **`no-mistakes`
  workflow** (the `no-mistakes` skill) and **AXI validation/qualification** (per the AXI standard, so the
  released code can be **submitted to the AXI catalog**). These are release-branch *process* steps run by
  the user/orchestrator — not necessarily automated GitHub Actions jobs.

## OSS license — GPL-3.0-or-later (enables CI + public PyPI)

The repo is licensed **GPL-3.0-or-later** (user's choice, 2026-07-26). Rationale: an **OSS license is the
gate for a public GitHub repo → free GitHub Actions CI** and a **public PyPI publish**. GPL v3 (2007) is
the **latest** GNU GPL (no v4); SPDX id `GPL-3.0-or-later` (bare `GPL-3.0` is deprecated). Requirements:
- A verbatim **`LICENSE`** file at the **repo root** (GitHub convention — `licensee` auto-detects it, showing
  the GPLv3 badge; if GitHub ever fails to detect, swap to choosealicense.com's exact `gpl-3.0.txt`).
- `pyproject.toml` `license = "GPL-3.0-or-later"` → wheel `License-Expression: GPL-3.0-or-later`.
Full decision + rationale: `docs/research/DN-packaging-distribution.md` §6 (in-repo). Delivered in CR-OA-018 §S5.

## CI workflow — release-time TODOs + local testing

- **PRINCIPLE (user, 2026-07-26): provision the CI environment to meet the tests' real requirements — do
  NOT weaken/skip tests to pass in a deficient runner.** When a test passes locally but fails on the GitHub
  runner because the runner lacks something, ADD the capability to CI; don't relax the assertion or skip.
  Concretely for this repo: (a) mongo-backed tests → a `services: mongodb` container; (b) tests that use the
  repo `.venv/bin/python` (e.g. `CleanVenvPackagingTest`'s `VENV_PYTHON`) → create + run the suite inside a
  repo-root `.venv` on the runner (mirrors the local `.venv/bin/python -m pytest`); (c) the keyring-fallback
  test → install a REAL storable keyring backend on the runner (`keyrings.alt`, pointed at a hermetic temp
  file) so the fallback actually stores and the warning names `keyring`; (d) the snapshot import-parity test →
  the test itself GENERATES synthetic `data/*.jsonl` fixtures at setUp and tears them down (never depend on the
  user's gitignored personal `data/*.jsonl`, never assert a hardcoded row count). Weakening a test to green a
  deficient env throws away the coverage the test encodes.

- **Vendor the release-gate script into the repo for CI — a remote runner can't reach `~/.claude`.**
  `~/.claude/scripts/skill-release-gate.py` is a **generic cross-project ecosystem tool** (config-driven,
  shared across skill-bundle projects) — but because a GitHub runner has no `~/.claude`, the workflow **must
  run a repo-local copy**. Decision (2026-07-26): keep a copy at **`scripts/skill-release-gate.py`** and point
  the CI `test` job at `python3 scripts/skill-release-gate.py --project-dir .` (done in CR-OA-018's
  `.github/workflows/ci.yml`). The home copy stays the canonical/generic source — **keep the vendored copy in
  sync** with it (refresh on ecosystem updates). Each project still carries its own **`.skill-release.toml`**
  (engine + lifecycle/AXI checks); the script only reads that. If the generic tool is ever published as an
  installable package, CI could `pip`-install it instead of vendoring — until then, the repo copy is required.
- **The stale store-count bug was in OUR in-repo `.skill-release.toml` (not the script) — FIXED 7 → 8.**
  Two checks failed because the config predated CR-OA-015's `orders` store: `[engine.wheel_glob_counts]`
  `"vidushi_oa/schema/*.json" = 7` and the lifecycle check `"setup provisions 7 collections"` /
  `expect = ["initialized[7]"]`. **8 is correct** (contacts, invoices, warranties, cases, products,
  subscriptions, insurance, orders) — the repo pytest suite already expects 8. Bumped all three to 8 in
  `bugfix/skill-release-toml-store-count`. The generic script reads the count from config; ideally the check
  should derive from the engine's `STORES` rather than a literal. Phase 1 (`agentskills validate`) already PASSED.
- **Test the CI workflow locally with `act`** (nektos/act) before pushing — `act -n`/`act --list` to
  validate structure, or a full run to exercise the jobs in Docker. Catches the gate-script-path issue and
  other runner-only problems without burning GitHub Actions minutes.
- **Live mail-account verification (CR-OA-020) is a release-time test, not a CI gate.** The embedded mail
  client (`mail-*` verbs + Gmail/Fastmail/Yahoo adapters + XOAUTH2) ships tested only against in-process
  fakes — no live credentials in the suite (a deliberate CR-020 decision). **During the release**, set up a
  live-account verification test (a real Gmail/Fastmail/Yahoo account each, secrets via the vault/keyring
  resolver, `VIDUSHI_SECRET_BACKEND`) to exercise the real adapters end-to-end — this also finally exercises
  the real `_default_adapter_factory` against live servers and the deferred Fastmail JMAP-vs-app-password
  auth-mode path (see CR-OA-020 "Deferred follow-ups"). Keep it out of the pytest gate (no creds on runners);
  run it as a release-branch qualification step alongside `no-mistakes` + AXI validation.

## Release cutover — the last step that closes the full development cycle

The release "completes the circle": ship the package, prune the now-redundant local setup, reinstall from
the package alone, with **agent state preserved throughout**.

- **Preserve agent state — do NOT lose it.** Prior-session state lives in two places that must stay readable
  by the released architecture: the **Mongo `vidushi_oa` store** ([[mongo-preexisting-data-migration]]) and
  the **`data/*.jsonl` snapshots**. The JSONL are **chezmoi-versioned** (the user's dotfile state, gitignored
  in this repo) — the user **keeps the same data** to preserve the agent's state, and it must read cleanly
  after release. (This is also why the import-parity test generates its own throwaway fixtures instead of
  depending on that personal data.) Local backend stays `VIDUSHI_BACKEND=mongo`; the JSONL are the chezmoi mirror.
- **LAST step BEFORE release — prune redundant `~/.claude` artifacts** superseded by the bundle: the **7 legacy
  standalone skills** + the **`inbox-analyst` agent** (AGENTS.md "Replacement path" — the unified
  `skills/vidushi-oa/` supersedes them all), plus any **local scripts now shipped in the package/repo** (no
  duplication). Do this only once the bundle is verified.
- **AFTER release — reinstall via the new installer schema:** `uv tool install vidushi-oa` (engine) +
  `npx skills add ./skills/vidushi-oa` (skill) + `voa setup`; confirm the pruned environment works **from the
  package alone** and the preserved state is still readable. That closes the cycle.

## Remote CI tracking

Use the **`ci-monitor` skill** to watch GitHub Actions runs remotely after pushing (release/* → TestPyPI,
master tag → PyPI) — track packaging/deploy execution without leaving the session.

**Why:** matches the git-flow release mechanism the user uses everywhere; the branch (master) is the gate.
**How to apply:** when authoring any CI/CD publish workflow for this user, gate production publish to
master (tag), automated; TestPyPI on release/*; never propose a manual-approval gate as "safer" — the
git-flow model already gates it. See [[vercel-skills-bundle-packaging]] (CR-018 packaging context).
