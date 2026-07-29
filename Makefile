# Vidushi OA — developer convenience targets.
#
# The E2E tier is LOCAL-ONLY (DN-mail-e2e-emulator-testing.md): a pre-release smoke pass
# that drives the real `voa` mail verbs against two throwaway Dockerized emulators —
# Stalwart (JMAP + password IMAP/SMTP) and Dovecot (the XOAUTH2 profile).
# It is excluded from the default test population by `addopts = -m "not e2e"` (pyproject) and
# must never run in CI. Run it manually before a release and after any mail-subsystem change.

PY ?= .venv/bin/python

.PHONY: test e2e e2e-install

## test: the default fakes-only suite (never collects the e2e tier).
test:
	$(PY) -m pytest tests/ -q

## e2e-install: install the [e2e] extra (testcontainers) into the repo venv.
e2e-install:
	$(PY) -m pip install -e ".[e2e]"

## e2e: run ONLY the Dockerized-emulator smoke tests (requires Docker running + the [e2e] extra;
## the Dovecot XOAUTH2 fixture additionally needs the `openssl` CLI, and skips without it).
e2e:
	$(PY) -m pytest -m e2e tests/e2e -v
