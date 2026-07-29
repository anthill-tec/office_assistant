"""CR-OA-029 §S2 — repo-wide no-personal-data guard over `vidushi_oa/`, `skills/`, `tests/`.

CR-OA-023's `_REAL_PERSONAL_MARKERS` guard (`tests/test_cr_oa_023_os_aware_setup.py:69`)
only ever scanned `vidushi_oa/`. This module promotes that invariant to a
mechanically-audited, REPO-WIDE guard: no real personal identifier may appear in ANY
tracked file under `vidushi_oa/`, `skills/`, or `tests/` -- the three surfaces this repo
ships publicly (public GitHub repo via `npx skills add`, and PyPI).

Per docs/changes/CR-OA-029-purge-real-pii-from-public-surfaces.md §S2 / AC:
  - scans MUST cover `vidushi_oa/`, `skills/`, AND `tests/`
  - MUST flag the real markers `antojk`, `anthilllabs`, `new.book1604` AND the real
    display name `Antony John`
  - MUST exclude the guard's own marker-definition file(s) (this file, and
    `tests/test_cr_oa_023_os_aware_setup.py` which legitimately holds
    `_REAL_PERSONAL_MARKERS` as scan targets) so the guard does not self-match
  - the test fails BEFORE §S1's purge and passes after

Today (pre-§S1) this MUST fail: `skills/vidushi-oa/SKILL.md` still hardcodes
the real Gmail address (line 66) and `antojk@anthilllabs.in` (line 150); five test files
(`test_cr_oa_020_jmap.py`, `test_cr_oa_022_send_transport.py`,
`test_cr_oa_024_jmap_content_type.py`, `test_cr_oa_028_body_retrieval.py`) still hardcode
`new.book1604@fastmail.com` and/or the display name `Antony John`.
"""
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The real personal identifiers that must never appear in a shipped public surface.
# Kept identical to CR-OA-023's `_REAL_PERSONAL_MARKERS` (tests/test_cr_oa_023_os_aware_setup.py:69),
# extended with the real display name per CR-OA-029 §S2.
_REAL_PERSONAL_MARKERS = ("antojk", "anthilllabs", "new.book1604")
_REAL_DISPLAY_NAME = "Antony John"

# Scanned surfaces -- the tracked, publicly-shipped parts of the repo (§S2 / non-goals:
# docs/ and CLAUDE.md/AGENTS.md are explicitly out of scope -- they legitimately name the
# maintainer).
_SCANNED_DIRS = ("vidushi_oa", "skills", "tests")

# The guard's own marker-definition file(s): they legitimately hold the markers/name as
# scan TARGETS (string literals to detect), not as leaked personal data, so they must be
# excluded from the scan to avoid self-matching.
_SELF_EXCLUDED_RELPATHS = (
    os.path.join("tests", "test_cr_oa_029_no_personal_data.py"),
    os.path.join("tests", "test_cr_oa_023_os_aware_setup.py"),
)


def _tracked_files_under(reldir):
    """Return tracked (git ls-files) paths under `reldir`, relative to ROOT.

    Using `git ls-files` (rather than os.walk) means untracked/ignored artifacts
    (`__pycache__`, build output, local secrets) are never scanned -- only what this
    repo actually ships/tracks, matching the CR's "tracked public surfaces" framing.
    """
    out = subprocess.run(
        ["git", "ls-files", reldir],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _iter_scanned_files():
    """Yield (dirname, relpath) pairs for every tracked, non-excluded file under the
    scanned surfaces."""
    for reldir in _SCANNED_DIRS:
        for relpath in _tracked_files_under(reldir):
            if relpath in _SELF_EXCLUDED_RELPATHS:
                continue
            yield reldir, relpath


def _find_marker_hits():
    """Scan every tracked, non-excluded file under the scanned surfaces for the real
    personal markers + display name.

    Returns (hits, files_scanned_by_dir) where `hits` is a list of formatted
    "relpath:lineno: marker -> line text" strings (for a precise failure message) and
    `files_scanned_by_dir` maps each scanned dir name -> count of files actually read,
    so the test can also guard against a vacuous pass caused by a broken scan path
    rather than an actual clean purge.
    """
    needles = _REAL_PERSONAL_MARKERS + (_REAL_DISPLAY_NAME,)
    hits = []
    files_scanned_by_dir = {d: 0 for d in _SCANNED_DIRS}

    for reldir, relpath in _iter_scanned_files():
        abspath = os.path.join(ROOT, relpath)
        try:
            with open(abspath, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, IsADirectoryError, OSError):
            # Skip binaries / unreadable entries -- they can't carry a text marker.
            continue

        files_scanned_by_dir[reldir] += 1

        for lineno, line in enumerate(lines, start=1):
            for needle in needles:
                if needle in line:
                    hits.append(
                        "%s:%d: %r -> %s" % (relpath, lineno, needle, line.rstrip("\n"))
                    )

    return hits, files_scanned_by_dir


class NoPersonalDataGuardTest(unittest.TestCase):
    """CR-OA-029 §S2 -- the no-personal-data invariant, widened repo-wide."""

    def test_scan_covers_all_three_public_surfaces(self):
        """Guard against a vacuous pass: if the scan path were broken (e.g. a typo'd
        dir, or `git ls-files` returning nothing), zero hits would mean "found nothing"
        rather than "actually clean". Assert real coverage in each of the three dirs."""
        _, files_scanned_by_dir = _find_marker_hits()
        for reldir in _SCANNED_DIRS:
            self.assertGreaterEqual(
                files_scanned_by_dir[reldir],
                1,
                "expected >=1 tracked file scanned under %r, got %d -- the scan path "
                "is broken, not clean" % (reldir, files_scanned_by_dir[reldir]),
            )

    def test_no_real_personal_identifiers_in_public_surfaces(self):
        """`vidushi_oa/`, `skills/`, and `tests/` must contain NONE of the real
        markers ("antojk", "anthilllabs", "new.book1604") nor the real display name
        "Antony John", excluding the guard's own marker-definition file(s).

        MUST FAIL today: skills/vidushi-oa/SKILL.md:66,150 and five test fixture files
        still hardcode the real address/name (see docs/changes/CR-OA-029...md §S1).
        MUST PASS once §S1's purge lands.
        """
        hits, _ = _find_marker_hits()
        self.assertEqual(
            hits,
            [],
            "found %d real-personal-identifier match(es) that must be purged "
            "(CR-OA-029 §S1) before this guard can pass:\n%s"
            % (len(hits), "\n".join(hits)),
        )


# The maintainer's personal Gmail address -- narrower scope than
# `_REAL_PERSONAL_MARKERS` above: ONLY this exact literal, scanned across EVERY
# tracked file in the repo (not just vidushi_oa/, skills/, tests/), per an explicit
# user decision that this one address must never be published anywhere in this
# public repo. The other markers (new.book1604@fastmail.com, antojk@anthilllabs.in,
# the "Antony John" byline, the bare "antojk" username) are explicitly OUT of scope
# for this guard and are left to the existing CR-OA-029 machinery above.
#
# The literal is CONSTRUCTED FROM PARTS so the contiguous string never appears in
# this guard file -- that lets the scan below cover EVERY tracked file including this
# one (no self-exclusion) while keeping the check honest and self-consistent.
_GMAIL_LITERAL = "antojk" + "@gmail.com"


def _all_tracked_files():
    """Return every tracked file in the repo (git ls-files, no path/dir filter)."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _find_gmail_literal_hits():
    """Scan every tracked file in the repo for the literal maintainer personal Gmail
    address (see `_GMAIL_LITERAL`, constructed from parts).

    No file is excluded -- this guard file itself never contains the contiguous
    literal (it is built from parts), so the scan honestly covers ALL tracked files
    including this one. Returns a list of "relpath:lineno: line text" strings so a
    failure lists every offending file:line precisely (what GREEN needs to purge).
    """
    hits = []
    for relpath in _all_tracked_files():
        abspath = os.path.join(ROOT, relpath)
        try:
            with open(abspath, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, IsADirectoryError, OSError):
            # Skip binaries / unreadable entries -- they can't carry a text marker.
            continue
        for lineno, line in enumerate(lines, start=1):
            if _GMAIL_LITERAL in line:
                hits.append("%s:%d: %s" % (relpath, lineno, line.rstrip("\n")))
    return hits


class PersonalGmailAddressNeverPublishedTest(unittest.TestCase):
    """The maintainer's personal Gmail address (see `_GMAIL_LITERAL`) must NEVER be
    published anywhere in this public repo.

    Narrower than, and independent of, the CR-OA-029 `_REAL_PERSONAL_MARKERS` guard
    above: that guard already flags the `antojk` substring but only within
    `vidushi_oa/`, `skills/`, `tests/`, and it deliberately permits other
    antojk-prefixed identifiers (the antojk@anthilllabs.in business address, the
    bare antojk username). This test is scoped to ONLY the exact personal Gmail
    literal, scanned across EVERY tracked file in the repo (no directory
    restriction) -- this guard file included, since the literal is built from parts
    and never appears contiguously here.
    """

    def test_gmail_literal_absent_from_all_tracked_files(self):
        hits = _find_gmail_literal_hits()
        self.assertEqual(
            hits,
            [],
            "found the maintainer's personal Gmail address literal %r in %d "
            "tracked file location(s) that must be purged:\n%s"
            % (_GMAIL_LITERAL, len(hits), "\n".join(hits)),
        )


if __name__ == "__main__":
    unittest.main()
