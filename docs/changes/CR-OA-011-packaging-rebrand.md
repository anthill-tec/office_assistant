# CR-OA-011 — Packaging + rebrand to Vidushi OA

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 010
**Labels:** packaging, rebrand, release, v0.1.0
**Phase:** Wave 6 (v0.1.0)
**Design reference:** PRD-distribution-release §1–§3 · [DN-packaging-distribution.md](../research/DN-packaging-distribution.md)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Turn the `scripts/` folder into an installable Python distribution `vidushi-oa` (console command `oa`)
with a setup mode, and rebrand the product to **Vidushi OA** — keeping `oa` / `OA_*` / the `office_assistant`
DB name for compatibility (all read as *vidushi-OA*).

## Scope

### §S1 Restructure into a package
Move `scripts/{store,oa_mongo,transitions,oa_toon}.py` → a `vidushi_oa/` package (`cli.py`, `mongo.py`,
`transitions.py`, `toon.py`) and `data/schema/*.json` → `vidushi_oa/schema/*.json` loaded via
`importlib.resources`. Keep `scripts/store.py` as a **thin compat shim** (`from vidushi_oa.cli import main`)
so existing callers/tests keep working. Internal imports updated (`oa_mongo` → `vidushi_oa.mongo`, etc.).

### §S2 `pyproject.toml` + console entry
A `pyproject.toml` (hatchling): dist `vidushi-oa`, `requires-python = ">=3.10"`, deps `pymongo>=4.16` +
`python-toon>=0.1.3,<0.2`, `[project.scripts] oa = "vidushi_oa.cli:main"`, and `vidushi_oa/schema/*.json`
included as package data. `python -m build` produces a wheel; `pip install` in a fresh venv exposes `oa`.

### §S3 `oa setup` — provision/verify local MongoDB
A new `setup` verb: checks the `OA_MONGO_URI` connection is reachable, prints clear guidance if not (how to
start a local `mongod`), and on success runs `init` (collections + indexes + validators). Idempotent;
`--check` to only diagnose without writing.

### §S4 Rebrand
`CLAUDE.md` / `README.md` / `scripts/README.md` carry the **Vidushi OA** brand + the `pip install
vidushi-oa` / `oa` usage. `oa`, `OA_*` env, and DB `office_assistant` are unchanged (documented as
compat-preserved).

## Acceptance criteria
- [ ] §S1 `import vidushi_oa.cli` works; `vidushi_oa` exposes the CLI + `vidushi_oa.schema` resources; `scripts/store.py` still runs (shim) and the full 122-test suite passes unchanged.
- [ ] §S2 `python -m build` succeeds; installing the wheel in a clean venv exposes an `oa` command whose `oa --help` matches `store.py --help`; the wheel bundles the 7 schema JSON files.
- [ ] §S3 `oa setup --check` exits 0 when Mongo is reachable and non-zero with actionable guidance when not; `oa setup` on a fresh DB creates the collections/indexes/validators (equivalent to `init`).
- [ ] §S4 `grep -ri "Vidushi OA" CLAUDE.md README.md` is non-empty; no doc still calls the product "office-assistant" as the product name (the repo/DB name may remain).
- [ ] **(no-regression)** every existing verb behaves identically via both `oa <verb>` and `python3 scripts/store.py <verb>`.

## Estimated size
L — a package restructure + build + a setup verb, all behaviour-preserving.

## Risk
Import/path churn across 122 tests — mitigated by the `scripts/store.py` compat shim and a per-cycle
regression gate. `importlib.resources` schema loading must work both in-tree and from an installed wheel.

## Non-goals
Public PyPI publish (pending the license decision — see the DN §6); renaming `oa`/`OA_*`/the DB; CI.
