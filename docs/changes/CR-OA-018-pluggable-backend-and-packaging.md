# CR-OA-018 — Pluggable persistence (SQLite default) + GPL-v3 packaging for `uv tool`/PyPI distribution

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 016, 017
**Labels:** packaging, persistence, sqlite, distribution, license, pypi
**Phase:** Wave 8 (distribution readiness)
**Design reference:** [DN-persistence-mongodb.md](../research/DN-persistence-mongodb.md) (2026-07-25) · [DN-packaging-distribution.md](../research/DN-packaging-distribution.md) (Decisions 6–7) · [PRD-distribution-release.md](../research/PRD-distribution-release.md)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

The engine hard-requires a running MongoDB, which blocks the portable, publishable tool the project
now wants: a persistent local install (`uv tool install vidushi-oa`) with no external server. Per
DN-persistence-mongodb (2026-07-25) the store becomes **pluggable** behind a backend seam with an
**embedded SQLite default** (Mongo opt-in), and per DN-packaging-distribution (Decisions 6–7) the repo
goes **GPL-3.0-or-later**, unblocking a public **PyPI** publish + CI/CD, installed persistently via
**`uv tool`**. This CR delivers all of that. It rides on CR-OA-017's audited, fully-AXI-conformant CLI.

## Scope

### §S1 Pluggable persistence seam + neutral query model
Introduce a backend interface (`vidushi_oa/backends/`) with a **neutral, backend-agnostic query/update
model** (design 2026-07-26): conditions `(field, op, value)` — `op ∈ {eq, ne, in, lt, lte, gt, gte,
exists, elem_match}` — combined with `all`/`any`/`none` and dotted paths; updates as `{set, push,
resolve-in-array}`; plus `count_by(field)` and validator/index provisioning. The CLI expresses each
query in this neutral model, and **each backend compiles it to its OWN native query** — no backend's
dialect is privileged and there is no dialect-translation layer. A factory selects the implementation
from **`VIDUSHI_BACKEND`** ∈ {`sqlite` (default), `mongo`}. `gen_id`, `transitions.py`, the sweeps,
`attention`, and TOON output sit **above** the seam and are untouched; the CLI verbs build the neutral
model, never a backend driver's query docs.

### §S2 SQLite backend (the default)
Implement the SQLite backend (stdlib `sqlite3`): one row per record `(id TEXT PRIMARY KEY, doc JSON)`.
Its **native compiler** renders the neutral model to `SELECT … WHERE …` over **JSON1** (`json_extract`
for scalar/dotted paths; `EXISTS (… json_each …)` for `elem_match`), so the nested-`actions[]` and
cross-domain `DUE`/`attention` queries run natively. Writes validate against the **same packaged JSON
Schemas** using the **`jsonschema`** package (a `[sqlite]` install extra), rejecting a malformed record
with no row written — parity with Mongo's `$jsonSchema`. `MongoBackend` compiles the same neutral model
to a query document.

### §S3 Backend-agnostic `setup` + optional `pymongo`
`voa setup` provisions the **active** backend: SQLite → ensure `$XDG_DATA_HOME/vidushi-oa/oa.db`
(override `VIDUSHI_SQLITE_PATH`), the table, the unique `id` index; Mongo → the existing connect/init
path. `pymongo` becomes an **optional extra** (`vidushi-oa[mongo]`) imported only by the Mongo backend;
the default install has no server dependency. `voa setup --check` diagnoses the active backend.

### §S4 `snapshot`/`import` parity + live-data migration (field-level fidelity)
`snapshot` and `import` work identically on both backends (they already speak `data/*.jsonl`), so they
are the migration path: `VIDUSHI_BACKEND=mongo voa snapshot` → `VIDUSHI_BACKEND=sqlite voa import`.
Beyond a round-trip test, this CR performs the **actual cutover of the existing live `vidushi_oa` Mongo
store** to SQLite (mirroring CR-OA-011 §S4's live migration), with **field-level fidelity** — every
record's nested `actions[]` history (opened/resolved), `--append-log` case-log entries, `documents[]`,
FKs, `source`, and lifecycle fields survive byte-for-byte (deep-equal per record, not just row counts).
The chezmoi-versioned `data/*.jsonl` snapshots are the **rollback path** and stay the plain-text journal;
Mongo is retained as the opt-in backend, so the cutover is reversible (re-`import` under
`VIDUSHI_BACKEND=mongo`). No live data is dropped by this CR — the cutover is verified, and pruning the
old Mongo DB is a later, separate step (as in CR-OA-011).

### §S5 GPL-3.0 licensing
Add a top-level `LICENSE` (GPL-3.0-or-later text) and set `license`/classifier metadata in
`pyproject.toml` so the built wheel declares it.

### §S6 `uv tool` / PyPI publish path + CI/CD
Make `pyproject.toml` PyPI-ready (metadata, license, long-description) and document `uv tool install
vidushi-oa`. Add a GitHub Actions workflow with a **two-tier publish strategy matching git-flow**
(user-directed 2026-07-26):
- a `test` job builds the wheel + runs the pytest suite + the release gate on **every push**;
- a **test-publish** job publishes to **TestPyPI** when a **`release/*`** branch is pushed — where packaging
  + deploy are validated during `git flow release` (the branch also hosts the release-qualification steps —
  the `no-mistakes` workflow + AXI validation for AXI-catalog submission — run by the orchestrator, not CI);
- a **production** job publishes to **PyPI automatically on a master version tag** (`refs/tags/…`) — **gated
  to master only** (git-flow discipline: master receives merges solely via `release finish`, so that
  ceremony is the deliberate act) via OIDC trusted publishing (`id-token: write` + an `environment: pypi`
  for trusted-publisher scoping). No manual-reviewer gate — automated production release from master is the
  house model.
This CR **authors and parse-validates** the workflow + lands the packaging; the workflow is **first
exercised at release time on the release branch** (the repo flip-to-public + the TestPyPI/PyPI
trusted-publisher setup are release-time ops, not inside this CR). No live publish happens inside this CR.

### §S7 Skill + docs wiring
Repoint the engine-install instructions in `skills/vidushi-oa/SKILL.md`, `README.md`, and
`scripts/README.md` from raw `pip` to **`uv tool install vidushi-oa`** (persistent), and note SQLite is
the zero-config default with Mongo as `[mongo]` opt-in. Both CR-OA-016 install paths (local/dev, public)
are updated.

## Acceptance criteria

### §S1
- [ ] A backend interface type exists with the methods above; `VIDUSHI_BACKEND=sqlite` and `=mongo` each resolve to a concrete implementation via the factory; an unknown value errors (structured, exit 1).
- [ ] **Integration:** after this CR, the `voa` CLI verbs in `vidushi_oa/_cli.py` obtain their store via the backend factory (not `pymongo` directly). **Caller-existence:** `grep -rn 'import pymongo' vidushi_oa/` returns hits **only** under the mongo backend module (0 in `_cli.py`); the factory has ≥1 non-test caller.

### §S2
- [ ] With `VIDUSHI_BACKEND=sqlite` on a temp db file, the full lifecycle passes: `setup → add → event → due-sweep/delivery-sweep → attention → validate` — a test drives every verb and asserts the same observable outcomes as the Mongo suite.
- [ ] A neutral `elem_match` condition (`actions` where `status=OPEN`), compiled by the SQLite backend to JSON1, returns exactly the rows whose `actions[]` contains an OPEN action; a test seeds mixed rows and asserts the match set. A **cross-backend parity** test runs the SAME neutral query against `mongo` and `sqlite` and asserts identical result ids.
- [ ] The SQLite backend rejects an out-of-enum `status` using the packaged schema (no row lands), and a duplicate `id` is de-duped/rejected — parity with the Mongo validator/index ACs.

### §S3
- [ ] With **no** `VIDUSHI_BACKEND` set, the backend is SQLite and `voa setup` creates the db at `$XDG_DATA_HOME/vidushi-oa/oa.db` (or `VIDUSHI_SQLITE_PATH`); a test asserts the file exists and the lifecycle runs.
- [ ] Build the wheel and install it into a **clean venv without pymongo**: `voa setup` + the SQLite lifecycle succeed (proves pymongo is not a hard dependency); installing `vidushi-oa[mongo]` pulls `pymongo` and `VIDUSHI_BACKEND=mongo` works.

### §S4
- [ ] `snapshot` then `import` round-trips on SQLite (row counts + `validate [0]:` preserved).
- [ ] **Field-level fidelity:** a cross-backend move (Mongo → `snapshot` → SQLite `import`) yields records that are **deep-equal** to the source per `id` — including nested `actions[]` (with `opened`/`resolved`/log entries), `documents[]`, `source`, and every FK/lifecycle field — asserted by a test that deep-compares the full record set, not just counts.
- [ ] **Migration validated against the live data (read-only):** a `snapshot` of the **live** `vidushi_oa` Mongo store (read-only) imported into a throwaway SQLite db reproduces every store's row count and a clean `validate` on the SQLite side — proving the migration works on the real data shape **without touching the live store**. (Per the migrate-first decision 2026-07-26, the *actual production cutover* — pointing the default SQLite db at the migrated data — is a **release-time operational step**, not performed inside this CR; the live Mongo DB stays intact, rollback via re-`import` under `VIDUSHI_BACKEND=mongo`.)

### §S5
- [ ] A top-level `LICENSE` file contains the GPL-3.0 text; the **built wheel's** metadata declares `License: GPL-3.0-or-later` (verified by inspecting the wheel's METADATA, not by reading `pyproject.toml`).

### §S6
- [ ] `uv tool install` from the built wheel yields an on-PATH `voa` that runs `voa --help` (exit 0) in a clean environment; `README.md` documents the command.
- [ ] A GitHub Actions workflow file exists whose parsed structure confirms: a `test` job (on push) that builds the wheel + runs pytest + the release gate; a **test-publish** job gated to **`release/*`** branches targeting **TestPyPI**; and a **production** publish job gated to a **version tag** targeting **PyPI**, automated (no manual-reviewer gate — gated to master only) with `id-token: write` (OIDC) + `environment: pypi` for trusted-publisher scoping.

### §S7
- [ ] `skills/vidushi-oa/SKILL.md`, `README.md`, and `scripts/README.md` name `uv tool install vidushi-oa` as the engine install (grep), and state SQLite as the default backend with `[mongo]` as the opt-in; `agentskills validate skills/vidushi-oa` still exits 0.

## Estimated size
L — a persistence seam + a full SQLite backend with JSON1 query parity + validation, backend-agnostic
`setup`/`snapshot`, optional-dependency packaging, GPL licensing, a CI/CD workflow, and skill/doc
rewiring. Best cycle-planned as: seam+SQLite (§S1–S2) → setup/optional-dep (§S3) → snapshot/migration
(§S4) → licensing+CI+docs (§S5–S7).

## Risk
Keeping the full suite green across the seam (path/query churn) — mitigated by the seam abstraction and
per-cycle regression. SQLite JSON1 must match Mongo query-document semantics for nested/cross-domain
queries — mitigated by parity tests that run the *same* assertions against both backends. The young
`python-toon` pin still applies. GPL-3.0 is a one-way license choice — confirmed by the user (DN §6).

## Non-goals
The actual first PyPI publish + the repo-public flip (release-time ops, guarded by the release gate);
removing MongoDB (it stays a first-class opt-in); an MCP wrapper; a GUI/TUI; a third mailbox. Re-running
the AXI conformance work (CR-OA-017 owns it).
