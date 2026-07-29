# CR-OA-031 — Portable query translation layer (per-provider, validated, never silently empty)

**Status:** COMPLETED (shipped 2026-07-30 on develop)
**Type:** bugfix
**Priority:** High
**Depends on:** 020, 030
**Labels:** mail, search, query-translation, jmap, imap, gmail, axi
**Phase:** Wave 11 (1.1.2 read-path patch)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 2 (per-provider adapters + capability flags) · §Decision 5 (token-saving `mail-*` verbs)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`voa mail-search --help` advertises a **portable query grammar** — `subject:`, `from:`, `category:`,
`newer_than:`, `has:attachment`, `OR`, and quoted `"exact phrase"` matching — and
`references/search-recipes.md` builds every documented recipe on it. **No translation layer exists.**
The raw query string is handed to each provider verbatim: `JmapAdapter.search` drops it into a single
JMAP `filter: {"text": query}`, and `GmailImapAdapter.search` passes it straight to `X-GM-RAW`.

Reproduced against a compliant JMAP server (`tests/e2e/test_mail_read_e2e.py`): `subject:Amazon` matches
nothing, and **`newer_than:` is a silent no-op** — `Amazon newer_than:7d` still returns a 40-day-old
message. From the field on Gmail, `newer_than:1w` returns `count: 0` because Gmail's date operators
accept only `d`/`m`/`y` units, so an invalid operator matches nothing. Every one of these fails
**silently at exit 0**: an agent reads "no mail in the last week" and misses live orders. That silent
wrong answer — not the missing feature — is the defect. (Bare-keyword search on JMAP is *correct*: the
`text` filter matches properly on a compliant server; the field's total `count: 0` is CR-OA-030's
swallowed error, a separate cause.)

## Scope

### §S1 The portable grammar — one parser, one definition
A single parser turns a portable query string into a provider-neutral **query model**: terms (bare
keywords and quoted phrases), qualifiers (`subject:` `from:` `to:` `category:` `newer_than:`
`has:attachment`), `OR` alternation with the implicit-AND default, and **nestable parenthesised groups**.
It is the sole definition of the grammar the CLI advertises; adapters consume the model, never the raw
string.

**Parenthesised groups are in-grammar (corrected 2026-07-30).** `voa mail-search --help` advertises
"parenthesised groups" in both its description and its `query` help, and such queries work today on the
Gmail passthrough path — so omitting them would make this CR a **capability regression against our own
advertised grammar** (a documented, working query becoming a parse error). The model is therefore a small
**tree**: a node is either a leaf (term/qualifier) or a group carrying an operator (`AND`/`OR`) and child
nodes; groups nest. Each compiler (§S2–§S4) walks that tree recursively. `label:` is NOT advertised
anywhere (absent from the CLI help and from `references/search-recipes.md`) and stays a non-goal.

**Relative dates** normalise once, in the model: `newer_than:<N><unit>` with `unit ∈ {d, w, m, y}`
resolves to an absolute cutoff date (weeks fold to days — `1w` → 7 days) so no provider ever receives a
unit it does not implement.

**Surfaces (verified 2026-07-29):** `vidushi_oa/_cli.py` mail-search help (~line 1590),
`skills/vidushi-oa/references/search-recipes.md`.

### §S2 JMAP compilation — real `FilterCondition`s
The model compiles to RFC 8621 `FilterCondition`s instead of one `text` blob: terms → `text`,
`subject:` → `subject`, `from:` → `from`, `to:` → `to`, `newer_than:` → `after` (the resolved absolute
date), `has:attachment` → `hasAttachment: true`; implicit-AND → `{"operator": "AND", "conditions": [...]}`,
`OR` → `{"operator": "OR", ...}`.

### §S3 Gmail (`X-GM-RAW`) compilation — native operators, valid units
The model compiles to Gmail search syntax with **provider-valid** units only: `newer_than:` emits Gmail's
`d`/`m`/`y` form (a `w` in the portable query having already folded to days in §S1), `category:` /
`has:attachment` / `subject:` / `from:` / `to:` map to their native operators, quoted phrases stay
quoted (preserving CR-OA-025's escaping).

### §S4 Yahoo / plain IMAP compilation — RFC 3501, capability-honest
The model compiles to RFC 3501 `SEARCH` keys (`SUBJECT`, `FROM`, `TO`, `SINCE`, `TEXT`). Qualifiers with
no RFC 3501 equivalent (`category:`, `has:attachment`) are **refused, not dropped** — per §S5 — because
silently ignoring them is what produced the wrong answers this CR exists to remove.

### §S5 Unsupported qualifiers are a structured error, never a silent empty
A qualifier the target provider cannot express, or an unparseable/unknown qualifier or unit, produces an
AXI #6 structured error with a **non-zero exit** naming the qualifier, the account, and the reason — never
`count: 0`. In a multi-account search a per-account refusal degrades into the existing
`failed_accounts[]` fail-soft envelope rather than failing the whole search. Provider capability is
declared through the existing adapter capability flags.

### §S6 Documentation reconciliation
`mail-search --help` and `references/search-recipes.md` state exactly the qualifiers the parser accepts
and the units it supports (`d`/`w`/`m`/`y`), and note per-provider capability gaps (e.g. `category:` is
Gmail-only). No recipe in the docs may use a qualifier/unit combination the parser rejects.

## Acceptance criteria

### §S1
- [x] `parse("Amazon subject:\"order shipped\" newer_than:2w has:attachment")` yields a model with terms
      `["Amazon"]`, `subject == "order shipped"` (phrase preserved), `has_attachment is True`, and a
      `newer_than` resolved to an **absolute** date exactly 14 days before the reference date.
- [x] `newer_than:1w` and `newer_than:7d` resolve to the **same** absolute cutoff.
- [x] `parse("a OR b")` yields an OR alternation; `parse("a b")` yields implicit-AND.
- [x] An unknown qualifier (`bogus:x`) or unknown unit (`newer_than:3q`) raises a parse error naming the
      offending token.
- [x] **Groups:** `parse("(a OR b) c")` yields a tree whose top level is an AND of [a group holding an OR
      of `a`,`b`] and the term `c`; groups **nest** (`parse("((a OR b) AND c) OR d")` or the implicit-AND
      equivalent parses to the corresponding nested tree). Unbalanced parentheses raise a parse error
      naming the offending token.
- [x] A parenthesised group carrying a qualifier (`(category:purchases OR subject:refund)`) parses with
      each qualifier inside the group, not flattened away.

### §S2
- [x] `subject:Amazon` compiles to a JMAP filter containing `{"subject": "Amazon"}` — asserted on the
      request body — and **not** a single `{"text": "subject:Amazon"}`.
- [x] `newer_than:7d` compiles to `{"after": "<ISO-8601 cutoff>"}`; `has:attachment` to
      `{"hasAttachment": true}`; `a OR b` to `{"operator": "OR", "conditions": [...]}`.
- [x] A nested group compiles to **nested** JMAP `FilterOperator`s — `(a OR b) c` →
      `{"operator": "AND", "conditions": [{"operator": "OR", "conditions": [...]}, {"text": "c"}]}`.
- [x] **E2E (`fastmail` profile):** `voa mail-search 'subject:<seeded-subject>'` returns exactly the
      seeded message, and `voa mail-search 'Amazon newer_than:7d'` returns **only** the recent Amazon row —
      the 40-day-old seeded message is **excluded** (today it is wrongly included).

### §S3
- [x] `newer_than:1w` compiles to Gmail `newer_than:7d` (never a literal `1w`), asserted on the emitted
      `X-GM-RAW` string.
- [x] `category:purchases "out for delivery"` compiles to `X-GM-RAW` preserving the category operator and
      the quoted phrase with CR-OA-025 escaping intact.
- [x] A nested group emits Gmail's **native parentheses** — `(a OR b) c` → `(a OR b) c` — and the two
      CR-OA-025 assertions that asserted raw round-trip equality are reconciled: the no-over-escaping
      assertion is preserved, while the superseded expectation (`newer_than:3m` reaching the wire
      verbatim) becomes the compiled `newer_than:90d`.

### §S4
- [x] `subject:X newer_than:7d` compiles to an RFC 3501 key sequence containing `SUBJECT "X"` and
      `SINCE <DD-Mon-YYYY>`.
- [x] `has:attachment` against the Yahoo/plain-IMAP adapter exits non-zero with a structured error naming
      the unsupported qualifier — it is **not** silently dropped.

### §S5
- [x] A single-account search using an unsupported qualifier exits **non-zero** with a structured error
      payload naming the qualifier and the account (no traceback).
- [x] A multi-account search where one account refuses the qualifier still returns the other accounts'
      results and lists the refusing account under `failed_accounts[]` (exit 0), matching the existing
      fail-soft contract.
- [x] **No silent empties (mechanically auditable):** a grep shows no adapter `search` path returning an
      empty result for an unsupported/unparseable qualifier.

### §S6
- [x] `voa mail-search --help` lists exactly the parser's accepted qualifiers and the `d`/`w`/`m`/`y`
      units, and marks `category:` as Gmail-only.
- [x] Every qualifier/unit used in `references/search-recipes.md` parses without error (a test walks the
      documented recipes through `parse`).

## Estimated size
M–L — a grammar parser + query model, three provider compilers, the capability/refusal contract, and the
docs reconciliation.

## Risk
Behaviour change for existing callers: queries that today silently match nothing (or match too much) will
start returning correct results or a **loud error** — intended, but it changes what the skill sees.
Over-strict refusal could break a working recipe, so §S6's documented-recipe test is the guard. The
Gmail path must not regress CR-OA-025's quoted-phrase escaping.

## Non-goals
The JMAP `deliveredTo`/error-surfacing fixes (CR-OA-030); `mail-get` body retrieval (CR-OA-032); new
qualifiers beyond the advertised set (`label:`, `larger:`, `filename:` remain out); server-side sorting
or pagination.
