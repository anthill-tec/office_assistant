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

- **Vendor the release-gate script into the repo at release.** The CI `test` job runs
  `~/.claude/scripts/skill-release-gate.py`, which lives in the user's home and **will NOT exist on a
  GitHub runner**. During release, **copy `skill-release-gate.py` into the project** (e.g. `scripts/`) and
  point the workflow at the repo-relative path, so the gate runs in CI. (CR-OA-018 only authors +
  parse-validates the workflow; this is the release-time fix.)
- **When vendoring, bump the gate's stale store count 7 → 8.** The home copy of `skill-release-gate.py`
  hard-codes **7** collections/schemas (it predates CR-OA-015's `orders` store), so two checks FAIL against
  the current engine: *"wheel bundles 7x vidushi_oa/schema/*.json (found 8)"* and *"setup provisions 7
  collections (found 8: …,orders)"*. **8 is correct** (contacts, invoices, warranties, cases, products,
  subscriptions, insurance, orders) — the repo pytest suite already expects 8. Update the vendored copy to 8
  (and thereafter derive it from the engine's `STORES` rather than a literal). Phase 1 (`agentskills validate`
  the skill bundle) already PASSES — only these two stale-count assertions fail, and they do **not** reflect
  a defect in engine or skill code.
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

## Remote CI tracking

Use the **`ci-monitor` skill** to watch GitHub Actions runs remotely after pushing (release/* → TestPyPI,
master tag → PyPI) — track packaging/deploy execution without leaving the session.

**Why:** matches the git-flow release mechanism the user uses everywhere; the branch (master) is the gate.
**How to apply:** when authoring any CI/CD publish workflow for this user, gate production publish to
master (tag), automated; TestPyPI on release/*; never propose a manual-approval gate as "safer" — the
git-flow model already gates it. See [[vercel-skills-bundle-packaging]] (CR-018 packaging context).
