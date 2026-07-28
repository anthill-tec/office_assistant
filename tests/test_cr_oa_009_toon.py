"""CR-OA-009 §S1 — scripts/oa_toon.py encoder seam + toon-format dependency pin.

Verifies the ACs: `scripts/oa_toon.py` exposes `to_toon`/`from_toon`; round-trips
store-shaped data losslessly (a list of uniform dicts, a single dict, and a
nested structure); `to_toon` on a uniform list of dicts emits TOON's tabular
array header + indented rows; and `toon-format` is pinned exactly
`==0.9.0b1` in a `requirements.txt` at the repo root (alongside the existing
`pymongo` runtime dependency). The old `python-toon` library is fully removed.

The shim module lives at `scripts/oa_toon.py` (NOT `scripts/toon.py`) — the
`toon-format` library it wraps imports as `toon_format`, and the project keeps a
project-prefixed shim (not the bare third-party name) so a stray `import toon`
never resolves to the shim itself. This matches the project's
existing `oa_mongo.py` convention (a project-prefixed shim, not the bare
third-party name). It is loaded here by file path via importlib, under a
non-`toon` module name, to keep it distinct from the real `toon` package.
Requires no MongoDB — the shim is pure.
"""
import importlib.util
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
OA_TOON_PY = os.path.join(SCRIPTS, "oa_toon.py")
REQUIREMENTS_TXT = os.path.join(ROOT, "requirements.txt")


def _load_oa_toon():
    """Load scripts/oa_toon.py by file path (the shim module), distinct from
    the third-party `toon` library it wraps internally."""
    spec = importlib.util.spec_from_file_location("oa_toon_shim", OA_TOON_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToonShimSurfaceTest(unittest.TestCase):
    def test_module_exposes_to_toon_and_from_toon_callables(self):
        oa_toon = _load_oa_toon()
        self.assertTrue(
            callable(getattr(oa_toon, "to_toon", None)),
            "scripts/oa_toon.py must expose a callable to_toon(obj) -> str",
        )
        self.assertTrue(
            callable(getattr(oa_toon, "from_toon", None)),
            "scripts/oa_toon.py must expose a callable from_toon(s) -> obj",
        )


class ToonRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.oa_toon = _load_oa_toon()

    def test_round_trip_list_of_uniform_dicts(self):
        rows = [
            {
                "id": "sub_fastmail",
                "provider": "Fastmail",
                "disposition": "KEEP",
                "status": "IN_PROGRESS",
                "renews": "2026-07-15",
            },
            {
                "id": "sub_netflix",
                "provider": "Netflix",
                "disposition": "TOMBSTONE",
                "status": "DUE",
                "renews": "2026-07-20",
            },
            {
                "id": "sub_spotify",
                "provider": "Spotify",
                "disposition": "KEEP",
                "status": "NEW",
                "renews": "2026-08-01",
            },
        ]
        encoded = self.oa_toon.to_toon(rows)
        self.assertIsInstance(encoded, str)
        decoded = self.oa_toon.from_toon(encoded)
        self.assertEqual(decoded, rows)
        # negative: a mangled row count must NOT still compare equal
        self.assertNotEqual(decoded, rows[:2])

    def test_round_trip_single_dict(self):
        row = {
            "id": "sub_fastmail",
            "provider": "Fastmail",
            "disposition": "KEEP",
            "status": "IN_PROGRESS",
            "renews": "2026-07-15",
        }
        encoded = self.oa_toon.to_toon(row)
        self.assertIsInstance(encoded, str)
        decoded = self.oa_toon.from_toon(encoded)
        self.assertEqual(decoded, row)
        self.assertIsInstance(decoded, dict)

    def test_round_trip_nested_structure(self):
        obj = {"a": 1, "b": [{"x": 1}]}
        encoded = self.oa_toon.to_toon(obj)
        decoded = self.oa_toon.from_toon(encoded)
        self.assertEqual(decoded, obj)
        # negative: nested list must survive as a list of dicts, not flatten
        self.assertIsInstance(decoded["b"], list)
        self.assertEqual(decoded["b"], [{"x": 1}])


class ToonTabularShapeTest(unittest.TestCase):
    def setUp(self):
        self.oa_toon = _load_oa_toon()
        self.rows = [
            {
                "id": "sub_fastmail",
                "provider": "Fastmail",
                "disposition": "KEEP",
                "status": "IN_PROGRESS",
                "renews": "2026-07-15",
            },
            {
                "id": "sub_netflix",
                "provider": "Netflix",
                "disposition": "TOMBSTONE",
                "status": "DUE",
                "renews": "2026-07-20",
            },
        ]

    def test_header_line_matches_toon_tabular_array_format(self):
        encoded = self.oa_toon.to_toon(self.rows)
        lines = [line for line in encoded.splitlines() if line.strip()]
        self.assertTrue(lines, "to_toon produced no non-blank output")
        header = lines[0]
        self.assertRegex(
            header,
            r"^\[2[,]?\]\{id,provider,disposition,status,renews\}:",
            f"header line does not match TOON tabular-array shape: {header!r}",
        )

    def test_exactly_two_indented_data_rows_follow_header(self):
        encoded = self.oa_toon.to_toon(self.rows)
        lines = [line for line in encoded.splitlines() if line.strip()]
        data_lines = lines[1:]
        self.assertEqual(
            len(data_lines),
            2,
            f"expected exactly 2 indented data rows, got {len(data_lines)}: {data_lines}",
        )
        for line in data_lines:
            self.assertTrue(line[:1].isspace(), f"data row is not indented: {line!r}")
        # negative bound: no stray third row / no un-indented trailer line
        self.assertNotEqual(len(data_lines), 3)


class RequirementsPinTest(unittest.TestCase):
    def test_requirements_txt_pins_toon_format(self):
        self.assertTrue(
            os.path.isfile(REQUIREMENTS_TXT),
            "requirements.txt does not exist at the repo root",
        )
        with open(REQUIREMENTS_TXT, "r", encoding="utf-8") as fh:
            content = fh.read()
        pin_re = re.compile(r"toon-format\s*==\s*0\.9\.0b1")
        self.assertRegex(
            content,
            pin_re,
            "requirements.txt missing the exact toon-format==0.9.0b1 pin",
        )
        # negative: the old python-toon library must be fully removed
        self.assertNotRegex(
            content,
            re.compile(r"python-toon"),
            "requirements.txt must not still reference python-toon (old lib removed)",
        )

    def test_requirements_txt_lists_pymongo(self):
        self.assertTrue(
            os.path.isfile(REQUIREMENTS_TXT),
            "requirements.txt does not exist at the repo root",
        )
        with open(REQUIREMENTS_TXT, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertTrue(
            any(re.match(r"^\s*pymongo\b", line) for line in lines),
            "requirements.txt missing the existing pymongo runtime dependency",
        )


if __name__ == "__main__":
    unittest.main()
