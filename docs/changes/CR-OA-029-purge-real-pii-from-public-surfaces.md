# CR-OA-029 — Purge real personal identifiers from public surfaces + enforce the invariant repo-wide

**Status:** COMPLETED (shipped 2026-07-28 on 1.1.0)
**Type:** bugfix
**Priority:** High
**Depends on:** 023
**Labels:** privacy, no-personal-data, skill, tests, release-prep
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 8 (Consequences — the no-personal-data invariant: the client carries only field descriptions with artificial `example`-style samples)

## Context

CR-OA-023 established the **no-personal-data invariant** — the shipped client hardcodes no real mailbox
address, alias, or account name — and `test_cr_oa_020`'s `_REAL_PERSONAL_MARKERS` guard enforces it over
`vidushi_oa/`. But the guard is **scoped to the engine only**: two other **public** surfaces still carry real
PII. The repo publishes to a **public GitHub repo** (the skill is added via `npx skills add anthill-tec/office_assistant/skills/vidushi-oa`) and PyPI, so anything tracked ships publicly. (The real store data is safe — `data/*.jsonl` and `documents/**` are gitignored.) Leaks found:
- `skills/vidushi-oa/SKILL.md` — the **shipped skill** hardcodes the user's real Gmail address and `antojk@anthilllabs.in`.
- Five test files hardcode `new.book1604@fastmail.com` and the real display name `Antony John`.

## Scope

### §S1 Replace real identifiers with fictitious `example`-style placeholders
Across the public surfaces (`skills/`, `tests/`; `vidushi_oa/` is already clean), replace every real personal
identifier with an artificial placeholder consistent with the existing convention (provider infrastructure
hostnames such as `imap.gmail.com` / the provider domain remain exempt):
- `new.book1604@fastmail.com` → a fictitious Fastmail-domain placeholder (e.g. `you@fastmail.com`).
- the real Gmail address → a fictitious Gmail-domain placeholder (e.g. `you@gmail.com`).
- `antojk@anthilllabs.in` → a fictitious business placeholder on a reserved domain (e.g. `you@yourbusiness.example`).
- the display name `Antony John` → a fictitious name (e.g. `Alex Doe`).
The replacements keep each test's behaviour identical (a consistent fake address/name per fixture) and keep
the SKILL.md guidance meaningful with generic examples.

**Surfaces (verified 2026-07-28):** `skills/vidushi-oa/SKILL.md:66,150`; `tests/test_cr_oa_020_jmap.py`,
`tests/test_cr_oa_022_send_transport.py`, `tests/test_cr_oa_024_jmap_content_type.py`,
`tests/test_cr_oa_028_body_retrieval.py` (the `new.book1604`/`Antony John` fixtures).

### §S2 Extend the no-personal-data guard to `skills/` + `tests/`
A guard test asserts the public surfaces `vidushi_oa/`, `skills/`, and `tests/` contain **none** of the real
markers `antojk`, `anthilllabs`, `new.book1604` (and the real display name) — the guard **excludes its own
marker-definition file(s)** (the files that legitimately hold the markers as scan targets) so it does not
self-match. This promotes the CR-023 client-only invariant to a **mechanically-audited** guard over those
three surfaces so a real address can never re-enter a shipped surface.

A **second, narrower guard** covers the maintainer's **personal Gmail address** alone (the exact literal, built
from parts so the guard file itself is not a hit) across **every tracked file in the repo** — `docs/`,
`AGENTS.md`/`CLAUDE.md` and this CR included. That one identifier must never reach the public repo at all,
whereas the other markers stay in-scope only for the three shipped surfaces above.

## Acceptance criteria

### §S1
- [ ] `git grep -nE "new\.book1604|antojk@gmail|antojk@anthilllabs|Antony John" -- 'skills/*' 'tests/*'` returns **zero** matches outside the guard's own marker-definition line(s).
- [ ] `skills/vidushi-oa/SKILL.md` names only fictitious example addresses (no `antojk@…`); its subscription/business guidance still reads sensibly with the placeholders.
- [ ] The affected test suites still pass unchanged in behaviour with the substituted fixtures.

### §S2
- [ ] A guard test asserts `vidushi_oa/`, `skills/`, and `tests/` contain none of `("antojk", "anthilllabs", "new.book1604")` nor the real display name, **excluding** the guard's own marker-definition file(s); the test **fails before §S1** and passes after.
- [ ] A second guard test scans **every** tracked file in the repo (no directory filter) for the exact personal Gmail literal and reports zero hits, without matching its own definition of that literal.
- [ ] **Caller-existence / mechanical audit:** the guards are real tests (collected by pytest), not comments; running them is the audit.

## Estimated size
XS — a string-replacement sweep across a handful of tracked files plus one guard test that widens an existing
invariant's scope.

## Risk
Minimal — fixture/doc string substitution with no production-code or behaviour change; the guard prevents
regression. The only care point is the guard not self-matching its own marker definitions (handled by
excluding those file(s) from its scan).

## Non-goals
Auditing `docs/` or `CLAUDE.md`/`AGENTS.md` for the general markers (the repo's own guide legitimately
describes the maintainer) — **except** the personal Gmail literal, which §S2's second guard purges repo-wide.
Changing the `data/*.jsonl` / `documents/**` handling (already gitignored). Any production-code change.
