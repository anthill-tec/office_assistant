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
- **`gmail-xoauth2` profile — a SECOND emulator, Dovecot** (`dovecot/dovecot:2.3-latest`, task #71,
  `tests/e2e/test_gmail_xoauth2_e2e.py`). A single account whose **oauth2 passdb** introspects an RFC 7662
  stub; drives the real `GmailXoauth2Adapter` XOAUTH2 SASL on **both** channels — IMAPS login and SMTP
  submission (STARTTLS → `AUTH XOAUTH2` → relay to a sink).

**Capability split — why two emulators is the irreducible floor.** Stalwart speaks **JMAP + IMAP + SMTP** and
does **PLAIN/LOGIN** SASL, so it hosts the fastmail(JMAP)/gmail/yahoo(password) profiles — but it does **not**
accept the `user=…\x01auth=Bearer …\x01\x01` **XOAUTH2** blob our `GmailXoauth2Adapter` emits (its bearer-SASL
is OAUTHBEARER-shaped). **Dovecot** *does* accept XOAUTH2 SASL against an introspection endpoint — but speaks
**no JMAP**. Neither server alone covers both JMAP and XOAUTH2, so **the tier runs two emulators**: Stalwart for
JMAP + password IMAP/SMTP, Dovecot for XOAUTH2 IMAP/SMTP. (The Dovecot tier runs the stub introspection
endpoint + SMTP sink as two tiny `python:3.12-alpine` containers on a shared throwaway network — reached at
network aliases — because this host's firewall drops docker-bridge → host traffic, ruling out in-process
host-gateway auxiliaries; all three containers + the network are torn down on session exit, ryuk-reaped, zero
leakage.)

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

**As built (2026-07-29) — DELIVERED on `release/1.1.1`.** The optional `endpoint` object is on the account
entry (`accounts.add_account`), stored **only when truthy** so a real account's persisted schema is
byte-identical and carried forward across a re-registration; `mail-auth --endpoint '<json>'` sets it;
`imap_endpoint_kwargs` (`mail/imap.py`) is the single mapping into the IMAP/SMTP adapters, `fastmail_adapter`
maps `jmap_url` onto `JmapAdapter.session_url`, and `build_client` layers `VIDUSHI_MAIL_ENDPOINTS` (ignored
with a warning when malformed) over the persisted value. The **no-real-account-regression** invariant is
asserted across every adapter by `tests/test_mail_endpoint_override.py`. User-facing setup guidance lives in
[`skills/vidushi-oa/references/mail-setup.md`](../../skills/vidushi-oa/references/mail-setup.md).

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
- **Gmail `X-GM-RAW` search — protocol/round-trip COVERED end-to-end; semantics real-Gmail-only** (task #68,
  `tests/e2e/test_gmail_xgmraw_e2e.py`). Empirically (2026-07-29, `stalwartlabs/mail-server:v0.11.8-alpine`,
  `gmail` profile): Stalwart's `CAPABILITY` does **not** advertise `X-GM-EXT-1`, and
  `UID SEARCH X-GM-RAW "…"` is **rejected with a tagged `BAD`** (`Invalid sequence set "X-GM-RAW"…`) — as is
  `X-GM-THRID` in a FETCH (`Invalid attribute "X-GM-THRID"`) — while a plain RFC 3501 `SEARCH` on the same
  connection returns `OK`. A self-hosted server therefore **cannot reproduce Gmail's proprietary full-text
  search semantics**. So the E2E test covers the maximum the emulator allows — the **client protocol/round-trip
  path**: it drives the real `GmailImapAdapter.search` over a real implicit-TLS (verification-off) connection to
  the live container, captures the emitted bytes off the wire, and asserts the **exact, correctly-ESCAPED**
  `UID SEARCH X-GM-RAW "<query>"` (the CR-OA-025 quoted-phrase-escaping path, exercised with embedded quotes and
  a backslash), and that the command genuinely round-trips — the server's own `BAD` (echoing the `X-GM-RAW`
  token) comes back and is surfaced as `imaplib.IMAP4.error`, imaplib's structured protocol-error signal, not an
  unhandled crash. **What it does NOT prove:** which messages a real `X-GM-RAW` query matches — Gmail's search
  semantics remain real-Gmail-only. Surfacing a non-Gmail server's `BAD` is *correct* here (real Gmail never
  `BAD`s a well-formed `X-GM-RAW`), so this is by design, not a client bug.
- **Workspace-Gmail XOAUTH2 IMAP + SMTP — now COVERED end-to-end (via Dovecot)** (task #71,
  `tests/e2e/test_gmail_xoauth2_e2e.py`, the `gmail-xoauth2` profile). This was the round-1 residual (XOAUTH2
  needs an OAuth token endpoint); it is now driven end-to-end against a live **Dovecot** container whose oauth2
  passdb introspects an RFC 7662 stub. The real, unmodified `GmailXoauth2Adapter` authenticates with the
  `XOAUTH2` SASL mechanism — its own `_xoauth2_raw` blob — on **both** channels: (a) IMAPS login (proved via
  `list_folders()` over a real implicit-TLS connection) and (b) SMTP submission (`create_draft` APPEND →
  `send_draft`: STARTTLS → EHLO → `AUTH XOAUTH2` → `sendmail`), with Dovecot relaying the accepted submission
  to an SMTP sink that confirms delivery. The **only** stub is the OAuth token *provider* (a zero-network
  callable) — minting a real Google access token needs a live token endpoint, orthogonal to what this tier
  validates. Stalwart could not host this path (it does not accept our XOAUTH2 SASL blob), which is why the
  tier runs a second emulator (see Decision 2 "Capability split").
- **Residual — Gmail `X-GM-RAW` / `X-GM-THRID` search SEMANTICS (exhausted; no emulator can cover it):** these
  two Gmail-proprietary extensions are implemented by **neither** candidate — Stalwart `BAD`s both (above) and
  Hoodiecrow (the other mock IMAP considered) implements neither — so no self-hosted server can reproduce which
  messages a real query matches. The client protocol/round-trip path **is** covered (above); the semantics
  remain real-Gmail-only and are the accepted residual. A Stalwart/Dovecot profile is a **strong proxy, not
  proof** against real Fastmail/Gmail production; a real-account spot-check stays optional.

## Components (deliverables)

1. ~~**Configurable endpoints** (Decision 3) — engine change; the blocking prerequisite.~~ **DONE** — see
   Decision 3 §As built.
2. ~~**`[e2e]` extra** — `testcontainers` (+ any client dep); no change to base/`[mongo]`/`[sqlite]`.~~
   **DONE** — `[project.optional-dependencies] e2e = ["testcontainers>=4"]`; seeding uses only the stdlib,
   so that is the sole added dep.
3. ~~**Emulator fixture + profiles** — a session-scoped `testcontainers` fixture for the single Stalwart
   image (readiness gating + teardown) that seeds the `fastmail` / `gmail` / `yahoo` provider profiles.~~
   **DONE** — `tests/e2e/conftest.py` (`stalwartlabs/mail-server:v0.11.8-alpine`; the `gmail` profile's
   `[Gmail]/Drafts` special-use rename + its ManageSieve `fileinto "Sent"` script are seeded there).
4. ~~**E2E test module** — `tests/e2e/test_mail_send_draft_e2e.py`, `@pytest.mark.e2e`, the Decision-5 cases
   for both paths.~~ **DONE** — plus `tests/e2e/test_emulator_profiles.py` (profile shape) and
   `tests/e2e/test_gmail_xgmraw_e2e.py` (task #68, see Fidelity above).
5. ~~**Marker + skip wiring** — register `e2e`, auto-skip without Docker/the extra; a `make e2e` /
   documented invocation.~~ **DONE** — the `e2e` marker is registered in `pyproject.toml`, which also pins
   `addopts = -m 'not e2e'` so the default population **excludes the tier by construction**; the
   fixture's Docker/extra auto-skip is the secondary guard.
6. ~~**Documented local pre-release invocation** — a `make e2e` target (or a documented command) + a
   release-checklist line ("run the local E2E smoke tests; require green") — the manual **local** gate run
   before `git flow release finish`.~~ **DONE** — `make e2e` (+ `make e2e-install`) in the `Makefile`, and
   the mandatory checklist line in [`AGENTS.md`](../../AGENTS.md) → **Release process** step 3.iii.
   **Not** a CI job, and **not** auto-wired into the skill-release gate (which stays Docker-free); the
   smoke run is a deliberate local step the releaser performs.

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
pre-release step, not CI). Implementation **landed on `release/1.1.1`** (design-note-driven, no CR — all six
Components delivered, and it caught three real defects: the missing JMAP `identityId`, unverified IMAP/SMTP
TLS, and unquoted spaced mailbox names). The one condition still open is therefore the **local run itself**:
`make e2e` green on the release branch. The remaining checklist (TestPyPI dry-run → gate → full suite → irreversible-publish confirm → finish) resumes
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
