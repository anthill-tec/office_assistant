"""CR-OA-025 §S2 root-cause (cycle C2a) — TOON round-trip for colon-bearing inline
array elements (RED).

The current backing lib (`python-toon`) has a decoder bug: an inline-array
element containing a colon `:` does not round-trip through
`vidushi_oa.toon.to_toon`/`from_toon` (the project shim over `_toon.py`). We
are migrating to the official `toon-format` lib to fix this; these tests pin
the round-trip contract the migration must satisfy.

Confirmed today (2026-07-28, current `.venv`):
  - `from_toon(to_toon({"next": ["a:b"]}))` decodes to `{"next": ["b\\""]}`,
    not `{"next": ["a:b"]}` — silent corruption, not an exception.
  - The realistic `mail-search` `next[]` shape (multiple inline-array
    elements, one containing both a colon and an embedded quoted phrase)
    raises `toon.ToonDecodeError: Expected 3 values, but got 2` on decode.
  - A scalar (non-array) value containing a colon already round-trips fine
    today — kept here as a passing control/guard against regression.

Imports `vidushi_oa.toon` directly (the same module `scripts/oa_toon.py` and
`tests/test_cr_oa_025_search_guidance.py` re-export/import), so these tests
exercise OUR shim — the thing the migration changes — not the raw
third-party library.
"""
import unittest

from vidushi_oa import toon as oa_toon


class ToonInlineArrayColonRoundTripTest(unittest.TestCase):
    def test_inline_array_element_with_colon_round_trips(self):
        obj = {"next": ["a:b"]}
        encoded = oa_toon.to_toon(obj)
        decoded = oa_toon.from_toon(encoded)
        self.assertEqual(
            decoded,
            obj,
            f"colon in inline-array element did not round-trip: "
            f"encoded={encoded!r} decoded={decoded!r}",
        )
        # negative: the known-corrupt result must NOT be what we get
        self.assertNotEqual(decoded, {"next": ['b"']})

    def test_mail_search_next_array_with_colon_and_quoted_phrase_round_trips(self):
        obj = {
            "count": 1,
            "results": [{"id": "i", "uid": "1"}],
            "next": [
                "mail-get --account gmail --uid 1",
                'mail-search category:purchases "out for delivery" --accounts <name>',
                "mail-accounts",
            ],
        }
        encoded = oa_toon.to_toon(obj)
        decoded = oa_toon.from_toon(encoded)
        self.assertEqual(
            decoded,
            obj,
            f"realistic mail-search next[] shape did not round-trip: "
            f"encoded={encoded!r} decoded={decoded!r}",
        )
        # negative bound: exactly 3 next-hints must survive, not fewer/more
        self.assertEqual(len(decoded["next"]), 3)
        self.assertEqual(decoded["results"], [{"id": "i", "uid": "1"}])


class ToonScalarColonRoundTripGuardTest(unittest.TestCase):
    """Control: a colon inside a scalar (not an inline-array element) already
    round-trips correctly today and must stay that way through the migration."""

    def test_scalar_value_with_colon_round_trips(self):
        obj = {"a": "cat:p only"}
        encoded = oa_toon.to_toon(obj)
        decoded = oa_toon.from_toon(encoded)
        self.assertEqual(decoded, obj)
        self.assertIsInstance(decoded["a"], str)


if __name__ == "__main__":
    unittest.main()
