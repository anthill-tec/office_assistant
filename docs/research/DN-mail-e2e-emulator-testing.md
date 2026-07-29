# DN — E2E validation of the mail send/draft path against Dockerized provider emulators

> **Type:** DN (design note) · **Status:** ACCEPTED (2026-07-29)
> **Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
> **Informs:** the 1.1.1 release gate (send/draft validation) + the durable mail E2E tier
> **Related:** [DN-mail-access.md](DN-mail-access.md) §Decision 7 (sending) + its as-built refinement · [DN-agent-interface-toon.md](DN-agent-interface-toon.md)

## Context

The embedded mail **send/draft** path (DN-mail-access §Decision 7) is covered **only by in-process
fakes** — `JmapAdapter(transport=…)` fakes, `FakeImapConn`, `FakeSMTP`. Those fakes encode *our
assumptions* about each provider's protocol, so a wrong assumption passes green yet breaks live. The
canonical case is the **round-1 empty-Fastmail-draft bug** (the wrong `Email/set $draft` assumption,
superseded by blob upload + `Email/import`) — the fakes did not catch it; code review + real-protocol
reasoning did, and the fix then baked the corrected assumption back into the fakes.

Passing the suite therefore proves *"the adapters emit the right protocol against servers that behave
as our fakes describe"* — **not** *"a real send works."* This DN specifies a **local E2E smoke-test tier
that runs the real `voa` verbs against real mail servers in throwaway Docker containers**, closing that
gap without depending on a hosted third-party account.

**Scope (decided 2026-07-29): local, not CI.** These are **smoke tests run locally** — before a release,
and after any change to the mail subsystem — a manual **pre-release** gate, **not** part of the CI matrix
(no Docker-in-CI). They **gate the 1.1.1 `git flow release finish`** in that we run them locally and
require green before finishing; CI continues to run only the fakes-based suite. Being smoke tests, they
assert the **critical send/draft paths work end-to-end**, not exhaustive coverage. This is a **DN, not a
CR** — we are mid-release-cycle and do not open CRs on a release branch; the DN is the design +
specification of record, implemented directly on `release/1.1.1`.

Provider-sandbox research (2026-07-29) is recorded under **Research provenance**; the short version:
**neither Fastmail nor Gmail offers an external sandbox** with throwaway accounts, so both paths use
**self-hosted Dockerized emulators**.

## Decision 1 — E2E via Dockerized emulators, set up + torn down per run

Add an **E2E test tier** that drives the real `voa mail-auth` / `mail-draft` / `mail-send` verbs against
a **real mail server running in a throwaway Docker container**, brought **up before the E2E session and
torn down after** it. It is **opt-in, local, and isolated**: gated behind `@pytest.mark.e2e` + a `[e2e]` extra,
**skipped** when Docker or the extra is absent, and **never wired into CI** — run **manually and locally,
before a release** (and after any mail-subsystem change). The default `pytest tests/ -q` suite and the CI
matrix stay **fakes-only** and unchanged. Docker is already installed and running on the dev host.

Rationale: an emulator exercises the actual on-the-wire protocol (blob upload, `Email/import`,
`EmailSubmission`, IMAP `APPEND`/`STORE`/`UID EXPUNGE`, SMTP submission) that in-process fakes only
imitate — the one class of defect fakes structurally cannot catch.

## Decision 2 — one Stalwart instance, per-provider profiles

**Decided 2026-07-29.** A **single Stalwart container** (`stalwartlabs/mail-server`) hosts the whole tier.
Stalwart speaks **JMAP + IMAP + SMTP**, so one instance can host multiple accounts, each configured as a
**provider profile** that mimics that provider's observable behaviour — one container, differentiated by
config:

- **`fastmail` profile** — a **JMAP** account (blob upload + `Email/import` + `EmailSubmission`) with a
  Fastmail-style layout (drafts/sent resolved by role). Validates the JMAP send/draft path.
- **`gmail` profile** — an **IMAP+SMTP** account shaped like Gmail: a **`[Gmail]/Drafts`** mailbox carrying
  the `\Drafts` special-use attribute, **and** a **Sieve `fileinto "Sent"`** rule that reproduces Gmail's
  server-side auto-file of sent mail — so our *skip-the-Sent-`APPEND`-for-Gmail* branch is validated
  **end-to-end** (send → we skip the `APPEND` → the server's Sieve files it to Sent → assert it is in Sent),
  not merely asserted.
- **`yahoo` profile** — a plain **RFC 3501 IMAP+SMTP** account, standard folder names; the generic IMAP path.

Each test targets its profile's account + endpoint. **Supersedes the earlier Cyrus split:** for **smoke
tests**, Stalwart is a real, compliant JMAP server, and the round-1-class defect (`Email/set $draft` vs
blob+`Email/import`) is caught by *any* real JMAP server — Cyrus's Fastmail-exact fidelity is not worth a
second container here. (A `cyrus` profile/container can be added later if Fastmail-exact JMAP fidelity is
ever needed.) GreenMail was considered and rejected — a mock, not a real server (kept only as a lighter
local fallback if a real Stalwart IMAP proves heavy).

## Decision 3 — PREREQUISITE: configurable provider endpoints (blocking gap)

The engine **cannot be pointed at any server but the real providers today** — the E2E tier is impossible
without first fixing this. Verified gaps (2026-07-29):

- `JmapAdapter.session_url` defaults to `https://api.fastmail.com/jmap/session` and `fastmail_adapter`
  never overrides it.
- `fastmail_adapter` hardcodes `host="imap.fastmail.com"`; the Gmail adapter hardcodes
  `imap.gmail.com` / `smtp.gmail.com`.
- The account registry entry carries no endpoint field (`name, provider, address, secret_ref, auth_mode,
  send, aliases`), and `mail-auth` has no `--endpoint`/`--host` flag.

**Specification.** Introduce a per-account **endpoint override**, resolved from account config with the
production default preserved (a bare install still targets the real provider). Concretely: an optional
`endpoint` object on the account entry (`jmap_url` / `imap_host` / `imap_port` / `smtp_host` / `smtp_port`,
each optional), settable at registration and read by `fastmail_adapter` / the IMAP adapters / the SMTP
sender. A test-only environment override (`VIDUSHI_MAIL_ENDPOINTS` JSON, consulted only when set) is
acceptable for the E2E harness so tests need not mutate the user's real registry. **No behaviour change
for real accounts** — the override is absent by default. This is the first deliverable; the emulator
tests depend on it.

## Decision 4 — lifecycle: `testcontainers`, session-scoped

Use **`testcontainers-python`** (an `[e2e]` extra) for container lifecycle: a **session-scoped pytest
fixture** starts the emulator, waits for readiness (port + a protocol ping), yields its host/port, and
**tears it down** on session exit — matching "fully set up and torn down for the E2E tests." Raw
`docker compose` is the fallback if we prefer no Python dep (a fixture shells out to `compose up -d` /
`compose down -v`), but `testcontainers` gives cleaner per-run isolation and readiness gating. Each test
provisions a **fresh throwaway account** via `voa mail-auth --send` against the emulator endpoint, so
tests never touch the user's real credentials or mailboxes.

## Decision 5 — test specifications (what the E2E tier asserts)

Per path, driving the **real CLI verbs** end-to-end against the emulator:

- **Draft round-trip.** `voa mail-draft --account <emu> --from <id> --to <emu-addr> --subject … --body …`
  → assert the message **actually lands in the emulator's Drafts**: JMAP path — a real blob upload +
  `Email/import` created it (fetch it back, compare body/headers, `$draft` set); IMAP path — a real
  `APPEND` placed it in the `\Drafts` special-use mailbox (fetch by UID, compare).
- **Send round-trip.** `voa mail-send --account <emu> --draft <id>` → assert the message is **delivered
  and filed in Sent** and **removed from Drafts** (JMAP `EmailSubmission` + `onSuccessUpdateEmail`; IMAP
  SMTP + safe-gated Sent `APPEND`/de-draft). Assert the returned message id.
- **Safe-gate (IMAP).** Configure the emulator to refuse the Sent `APPEND` (or advertise no `\Sent`) →
  assert the draft is **retained untouched**, never expunged (the data-loss guard).
- **Special-use resolution.** Configure the emulator to name its drafts folder non-literally and advertise
  the `\Drafts` attribute → assert resolution via the attribute (exercises the `[Gmail]/Drafts` /
  `Draft` fallback code paths).
- **AXI #9.** Assert `mail-draft`'s TOON output carries the runnable `mail-send` `next[]` (end-to-end, not
  just unit).

## Fidelity: what the profiles reproduce, and the residual

- **Gmail's folder layout + server-side auto-file-Sent ARE reproduced** by the `gmail` profile (Decision 2):
  the `[Gmail]/Drafts` `\Drafts` special-use mailbox exercises our resolution/fallback, and the Sieve
  `fileinto "Sent"` rule reproduces Gmail's server-side sent-filing — so the *skip-the-Sent-`APPEND`* branch
  is validated end-to-end, not just asserted. A deterministic unit test additionally asserts **no** Sent
  `APPEND` is issued for a Gmail account (independent of the server).
- **Residual (out of scope for the smoke gate, small, explicitly accepted for 1.1.1):** `X-GM-RAW` is a
  *read/search* concern, not send/draft; **XOAUTH2 SMTP** (Workspace Gmail) needs an OAuth token endpoint →
  stays fake-covered (continuation-response unit-tested); and a Stalwart profile is a **strong proxy, not
  proof** against real Fastmail/Gmail production. A real-account spot-check stays optional.

## Components (deliverables)

1. **Configurable endpoints** (Decision 3) — engine change; the blocking prerequisite.
2. **`[e2e]` extra** — `testcontainers` (+ any client dep); no change to base/`[mongo]`/`[sqlite]`.
3. **Emulator fixture + profiles** — a session-scoped `testcontainers` fixture for the single Stalwart
   image (readiness gating + teardown) that seeds the `fastmail` / `gmail` / `yahoo` provider profiles.
4. **E2E test module** — `tests/e2e/test_mail_send_draft_e2e.py`, `@pytest.mark.e2e`, the Decision-5 cases
   for both paths.
5. **Marker + skip wiring** — register `e2e`, auto-skip without Docker/the extra; a `make e2e` / documented
   invocation.
6. **Documented local pre-release invocation** — a `make e2e` target (or a documented command) + a
   release-checklist line ("run the local E2E smoke tests; require green") — the manual **local** gate run
   before `git flow release finish`. **Not** a CI job, and **not** auto-wired into the skill-release gate
   (which stays Docker-free); the smoke run is a deliberate local step the releaser performs.

## Borrowed Docker configs (published starting points to modify)

Do **not** hand-write from scratch — borrow the maintainers' published Stalwart config and add our three
provider profiles (sources in Research provenance).

- **Base image — `stalwartlabs/mail-server:latest-alpine`.** Published compose examples expose IMAP `143`,
  IMAPS `993`, SMTP `25`, submission `587`, SMTPS `465`, JMAP/admin over HTTP `8080` (+ `443`) with a data
  volume; **prune to `587` (submission) + `143` (IMAP) + `8080` (JMAP/API)** for our round-trips. Starting
  compose: `awesome-docker-compose.com/stalwart` and `stalw.art/docs/install/platform/docker`.
- **Provider profiles (the config we add).** On first boot, seed three accounts via Stalwart's admin
  API/CLI: **`fastmail`** (JMAP; JMAP session at `http://<host>:8080/jmap/session` → `endpoint.jmap_url`,
  Decision 3); **`gmail`** (IMAP/SMTP + a `[Gmail]/Drafts` mailbox with the `\Drafts` special-use attribute
  + a Sieve `fileinto "Sent"` script — Stalwart's documented auto-save-sent approach); **`yahoo`** (plain
  IMAP/SMTP, standard folders). Special-use folder names and Sieve scripts are standard Stalwart config.
- **Fallback — GreenMail** (`greenmail/standalone:2.0.1`, SMTP `3025` / IMAP `3143`, auto-creates accounts
  on first login) only if a real Stalwart IMAP proves heavy locally; it is a mock, so not the default.

`testcontainers-python` (Decision 4) wraps the single Stalwart image (tag + pruned ports + a readiness
wait), and the fixture seeds the three profiles — the whole "compose" surface is a few lines in a fixture,
not a checked-in stack.

## Relationship to the 1.1.1 release

1.1.1 is otherwise green (no-mistakes + skills conformance + release gate + fakes). Per the 2026-07-29
decision it **holds at `git flow release finish`** until the E2E tier (Decisions 3–5, JMAP + IMAP/SMTP
paths) is implemented and the smoke tests are **run locally and passing against the emulators** (a manual
pre-release step, not CI). Implementation lands on `release/1.1.1` (design-note-driven, no CR). The
remaining checklist (TestPyPI dry-run → gate → full suite → irreversible-publish confirm → finish) resumes
once the local E2E smoke run is green.

## Research provenance (primary sources, 2026-07-29)

- Fastmail: no external sandbox (fminabox is staff-only); external devs use api.fastmail.com/jmap/session
  + a generated token — fastmail.com/for-developers/integrating-with-fastmail, fastmail.com/blog/fastmail-in-a-box;
  official protocol suite github.com/fastmail/JMAP-TestSuite.
- Gmail: no official IMAP/SMTP sandbox — every call hits a real mailbox; substitutes are a dedicated test
  account or generic emulators (Nylas/mailtrap guidance).
- Emulators: GreenMail (github.com/greenmail-mail-test/greenmail — SMTP/IMAP/POP3, testing-purpose, Docker);
  Stalwart (stalw.art — JMAP+IMAP+SMTP single binary, EmailSubmission compliance); Cyrus JMAP dummy (Docker
  no-auth JMAP playground; Cyrus is Fastmail's engine); Dovecot/docker-mailserver (production IMAP).
- `testcontainers-python` for session-scoped container lifecycle.
