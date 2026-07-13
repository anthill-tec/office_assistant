# CR-OA-011 — Packaging + full rename to Vidushi OA

**Status:** COMPLETED (2026-07-12)
**Type:** feature
**Priority:** High
**Depends on:** 010
**Labels:** packaging, rebrand, rename, migration, release, v0.1.0
**Phase:** Wave 6 (v0.1.0)
**Design reference:** PRD-distribution-release §1–§3 · [DN-packaging-distribution.md](../research/DN-packaging-distribution.md) (Decisions 4, 5)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

Turn the `scripts/` folder into an installable Python distribution `vidushi-oa` (console `voa`) with a
setup mode, and **fully rename** the product to **Vidushi OA** — a hard cut of the `oa`/`OA_*` names and a
migration of the Mongo DB `office_assistant` → `vidushi_oa` (user-confirmed 2026-07-12: full rename, no
back-compat aliases). The repo/folder name (`office_assistant`) is out of scope (a separate git/GitHub step).

## Scope

### §S1 Package restructure
`scripts/{store,oa_mongo,transitions,oa_toon}.py` → a **`vidushi_oa/`** package (`cli.py`, `mongo.py`,
`transitions.py`, `toon.py`); `data/schema/*.json` → `vidushi_oa/schema/*.json` loaded via
`importlib.resources`. `scripts/store.py` stays a thin **path-compat shim** (`from vidushi_oa.cli import
main`) so the in-repo file path keeps working; internal imports repointed (`import mongo` etc.).

### §S2 `pyproject.toml` + `voa` console entry
`pyproject.toml` (hatchling): dist `vidushi-oa`, `requires-python=">=3.10"`, deps `pymongo>=4.16` +
`python-toon>=0.1.3,<0.2`, `[project.scripts] voa = "vidushi_oa.cli:main"`, schema JSON as package data.
`python -m build` → a wheel; `pip install` exposes `voa`.

### §S3 Name hard-cut (env + console — no aliases)
- **Env:** `OA_MONGO_URI`/`OA_MONGO_DB`/`OA_DATA_DIR`/`OA_FORMAT` → `VIDUSHI_MONGO_URI`/`VIDUSHI_MONGO_DB`/
  `VIDUSHI_DATA_DIR`/`VIDUSHI_FORMAT`. The old `OA_*` names are **no longer read** (hard cut).
- **Console:** `oa` → `voa`.
- Update **every test module + doc** to the new env/command names (all 122 tests re-pointed).

### §S4 DB migration `office_assistant` → `vidushi_oa`
The default Mongo DB becomes `vidushi_oa`; the test DB becomes `vidushi_oa_test`. Migrate the live data
**safely**: `snapshot` (backup) → create `vidushi_oa` → `import` → **verify all 118 records + validators
clean** → only then drop the old `office_assistant` (a backup snapshot is retained; the drop pauses for
explicit confirmation — hard to reverse).

### §S5 `voa setup` + rebrand docs
A `setup` verb: verify the `VIDUSHI_MONGO_URI` connection, guide provisioning if unreachable, and run
`init` on success (`--check` diagnoses only). Docs (`CLAUDE.md`/`README.md`/`scripts/README.md`) → the
**Vidushi OA** brand + `voa`/`VIDUSHI_*` usage; no `OA_*`/`oa`/`office_assistant`-as-product references remain.

## Acceptance criteria
- [x] §S1 `import vidushi_oa.cli` works; `vidushi_oa.schema` resources load in-tree; `scripts/store.py` shim still runs.
- [x] §S3 the full suite passes **only** under `VIDUSHI_*` env (a test setting the old `OA_MONGO_DB` does not isolate — proof of the hard cut); production code carries no `OA_MONGO\|OA_DATA\|OA_FORMAT` (the only residual references are literal strings in `test_cr_oa_011_rename.py`, which assert the old vars are *ignored* — the negative-proof of the hard cut).
- [x] §S2 `python -m build` succeeds; a clean-venv install exposes `voa` (`voa --help` == the old `store.py --help`); the wheel bundles the 7 schema JSONs; `oa` is NOT installed (entry_points.txt: `voa = vidushi_oa.cli:main` only).
- [x] §S4 after migration, `voa stats <type>` totals match the pre-migration counts for all 7 stores (118), `voa validate` returns `[]` for each; the backup snapshot exists. **The old `office_assistant` DB is deliberately RETAINED as a live backup — the drop is deferred pending explicit user confirmation (hard to reverse; see Risk).**
- [x] §S5 `voa setup --check` → 0 when Mongo reachable, non-zero + guidance when not; `voa setup` on a fresh DB creates collections/indexes/validators; docs carry "Vidushi OA" and no product-level "office-assistant"/`OA_*`.
- [x] **(regression)** every verb behaves identically via `voa <verb>` and `python3 scripts/store.py <verb>`.

## Close-out (2026-07-12)
Executed in five cycles on `feature/CR-OA-011-packaging-rebrand`: **A** env/DB-name hard-cut (`OA_*`→`VIDUSHI_*`, default DB `vidushi_oa`), **B** package restructure (`scripts/`→`vidushi_oa/`) + `pyproject.toml` + `voa` console + `importlib.resources` schema, **C** `voa setup` verb, **D** live DB migration (`office_assistant`→`vidushi_oa`, snapshot-backup→import→118-count parity + validator-clean verify; **old DB retained**), **E** docs rebrand. VERIFY (post-GREEN) caught two blocking defects — a broken `python -m build` (duplicate schema paths) and a test tearDown dropping the **live** `vidushi_oa` DB (the suite was wiping migrated data) — both FIXED and independently re-verified. Final gate: **148/148 green**, wheel builds with all 7 schemas + `voa` entry point, migration parity 118=118, `vidushi_oa` survives a full suite run.

## Estimated size
XL — a package restructure + a hard-cut rename across code/tests/docs + a live-DB migration + a build + a setup verb. Execute in cycles (env rename → restructure/build → setup → DB migration → docs), regression-gated each.

## Risk
The **DB migration** touches the user's live 118 records — mitigated by a backup snapshot, count/validator
verification before any drop, and an explicit confirmation gate on the drop. The hard-cut env rename churns
every test — mitigated by per-cycle regression. `importlib.resources` schema loading must work in-tree and
from a wheel.

## Non-goals
Public PyPI publish (private/git-install for v0.1.0 per the DN); renaming the repo/GitHub project; any
`OA_*`/`oa` back-compat aliases (explicitly hard-cut).
