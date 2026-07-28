"""CR-OA-027 §S1 — `orders.order_date` validator accepts null (aligned to the
documented `str|null` contract in `data/schema.md:102`).

Today `vidushi_oa/schema/orders.schema.json:25-27` declares
`"order_date": {"bsonType": "string"}`, which REJECTS null. The sqlite backend
(`vidushi_oa/backends/sqlite.py::_validate`) adapts that Mongo `$jsonSchema` to plain
JSON Schema and enforces it on `insert()` via `jsonschema.validate` -- a `null`
`order_date` therefore raises an uncaught `jsonschema.ValidationError` (NOT caught by
`vidushi_oa/_cli.py::main`'s `except (ValueError, NotImplementedError)`), so
`voa add orders --json '{... "order_date": null}'` crashes with a traceback and a
non-zero exit instead of the standard success envelope.

Verifies the §S1 acceptance criteria:
  - Adding an `orders` row with `"order_date": null` SUCCEEDS and `voa validate orders`
    reports it clean (`[]`). RED TODAY: the string-only validator rejects null.
  - Regression: a row with a string `order_date` (`"2026-07-01"`) still validates
    clean (passes today -- guard against the fix breaking the existing contract).
  - Regression: a row with a non-string/non-null `order_date` (a number) is STILL
    rejected by the validator, both before AND after the fix (widening to
    `["string", "null"]` does not open the door to arbitrary types).
  - AXI: `voa validate orders` on a clean store returns the definitive empty state
    `[]` (AXI #5) with exit 0; `voa add orders ... order_date:null` returns the
    standard TOON status envelope carrying the new id (RED today, same underlying
    cause as the first bullet).

Runs the REAL `voa` CLI (`scripts/store.py`) end to end against the default SQLite
backend, isolated via `VIDUSHI_SQLITE_PATH` (throwaway tempfile) and
`VIDUSHI_DATA_DIR` (throwaway tempdir) -- NEVER the real repo `data/` or the real
`~/.local/share/vidushi-oa/oa.db`. `tests/conftest.py`'s autouse fixture pins
`VIDUSHI_BACKEND=mongo` in the parent process env for the pre-existing Mongo suite;
every subprocess env dict here is a COPY of `os.environ` with `VIDUSHI_BACKEND`
explicitly overridden to `sqlite` before the subprocess runs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")


def _order_payload(**overrides):
    payload = {
        "merchant": "DateCo", "number": "O-DATE-1", "amount": 10,
        "currency": "INR", "status": "IN_PROGRESS", "acct": "personal",
    }
    payload.update(overrides)
    return payload


class OrderDateNullableValidatorTest(unittest.TestCase):
    """§S1 -- `orders.order_date` accepts string OR null; a non-string/non-null
    value (e.g. a number) stays rejected, both before and after the fix."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr027-data-")
        self.sqlite_dir = tempfile.mkdtemp(prefix="oa-cr027-sqlite-")
        self.sqlite_path = os.path.join(self.sqlite_dir, "oa.db")

        self.env = dict(os.environ)
        self.env["VIDUSHI_BACKEND"] = "sqlite"
        self.env["VIDUSHI_SQLITE_PATH"] = self.sqlite_path
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.env["VIDUSHI_FORMAT"] = "json"

        init_result = self._run("init")
        self.assertEqual(
            init_result.returncode, 0,
            f"store.py init (sqlite) failed: stdout={init_result.stdout!r} "
            f"stderr={init_result.stderr!r}",
        )

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.sqlite_dir, ignore_errors=True)

    def _run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=env or self.env,
        )

    def _add(self, payload, env=None):
        return self._run("add", "orders", "--json", json.dumps(payload), env=env)

    def _validate_orders(self, env=None):
        return self._run("validate", "orders", env=env)

    def _order_ids(self):
        result = self._run("query", "orders", "--json")
        self.assertEqual(result.returncode, 0, f"query orders failed: {result.stderr!r}")
        rows = json.loads(result.stdout.strip())
        return {r["id"] for r in rows}

    # ---- AC1: null order_date accepted, validate reports clean (RED today) ----

    def test_add_order_with_null_order_date_succeeds_and_validates_clean(self):
        add_result = self._add(_order_payload(order_date=None, number="O-DATE-1"))

        self.assertEqual(
            add_result.returncode, 0,
            f"expected 'add orders' with order_date=null to succeed (str|null per "
            f"data/schema.md:102), got rc={add_result.returncode}; "
            f"stdout={add_result.stdout!r} stderr={add_result.stderr!r}",
        )
        added_payload = json.loads(add_result.stdout.strip())
        self.assertEqual(added_payload.get("added"), ["ord_dateco_o-date-1"])
        # negative bound: nothing skipped as a dupe on a fresh db
        self.assertEqual(added_payload.get("skipped"), [])

        validate_result = self._validate_orders()
        self.assertEqual(
            validate_result.returncode, 0,
            f"validate orders should exit 0: stderr={validate_result.stderr!r}",
        )
        parsed = json.loads(validate_result.stdout.strip())
        self.assertEqual(
            parsed, [], f"expected the definitive empty state [] after a null "
            f"order_date add, got: {parsed}",
        )
        self.assertNotIn("ord_dateco_o-date-1", parsed)

    # ---- AC2 (regression): string order_date still validates clean ----

    def test_add_order_with_string_order_date_still_validates_clean(self):
        add_result = self._add(_order_payload(order_date="2026-07-01", number="O-DATE-2"))

        self.assertEqual(
            add_result.returncode, 0,
            f"a string order_date must keep working (regression guard): "
            f"stdout={add_result.stdout!r} stderr={add_result.stderr!r}",
        )
        added_payload = json.loads(add_result.stdout.strip())
        self.assertEqual(added_payload.get("added"), ["ord_dateco_o-date-2"])
        self.assertEqual(added_payload.get("skipped"), [])

        validate_result = self._validate_orders()
        self.assertEqual(validate_result.returncode, 0, f"stderr={validate_result.stderr!r}")
        parsed = json.loads(validate_result.stdout.strip())
        self.assertEqual(parsed, [])
        self.assertNotIn("ord_dateco_o-date-2", parsed)

    # ---- AC3 (regression): non-string/non-null order_date stays rejected ----

    def test_add_order_with_numeric_order_date_is_still_flagged_nonconforming(self):
        # `add` itself always succeeds (schema enforcement for the sqlite backend
        # lives in the `validate` verb, which loads the schema fresh from disk on
        # every invocation -- see vidushi_oa/_cli.py::cmd_validate / _load_schema);
        # the CR's regression guarantee is that `validate` keeps flagging a
        # non-string/non-null order_date, both before AND after widening the
        # bsonType to ["string", "null"].
        add_result = self._add(_order_payload(order_date=20260701, number="O-DATE-3"))
        self.assertEqual(
            add_result.returncode, 0,
            f"precondition failed -- add must succeed so validate has a row to "
            f"check: stdout={add_result.stdout!r} stderr={add_result.stderr!r}",
        )
        added_id = json.loads(add_result.stdout.strip())["added"][0]
        self.assertEqual(added_id, "ord_dateco_o-date-3")

        validate_result = self._validate_orders()
        self.assertEqual(validate_result.returncode, 0, f"stderr={validate_result.stderr!r}")
        parsed = json.loads(validate_result.stdout.strip())
        self.assertIn(
            added_id, parsed,
            f"a numeric order_date must still be reported nonconforming (widening "
            f"bsonType to [string, null] must not admit other types), got: {parsed}",
        )
        # positive bound: exactly this one bad row on an otherwise-fresh store
        self.assertEqual(parsed, [added_id])

    # ---- AC4 (AXI): add's TOON status envelope + validate's definitive empty state ----

    def test_axi_null_add_toon_envelope_then_validate_definitive_empty_state(self):
        toon_env = dict(self.env)
        toon_env.pop("VIDUSHI_FORMAT", None)  # exercise the TOON default (AXI #5/#9)

        # a fresh (empty) store must report the definitive empty state up front too,
        # not a bare/ambiguous output
        clean_validate = self._validate_orders(env=toon_env)
        self.assertEqual(
            clean_validate.returncode, 0,
            f"validate orders on a clean store must exit 0: stderr={clean_validate.stderr!r}",
        )
        self.assertEqual(
            clean_validate.stdout.strip(), "[0]:",
            f"expected the definitive empty TOON state '[0]:' on a clean store, "
            f"got: {clean_validate.stdout!r}",
        )

        add_result = self._add(_order_payload(order_date=None, number="O-DATE-4"), env=toon_env)
        self.assertEqual(
            add_result.returncode, 0,
            f"expected the TOON add of a null-order_date row to succeed: "
            f"stdout={add_result.stdout!r} stderr={add_result.stderr!r}",
        )
        lines = add_result.stdout.strip().splitlines()
        self.assertEqual(
            lines, ["added[1]: ord_dateco_o-date-4", "skipped[0]:"],
            f"expected the standard TOON status envelope carrying the new id, "
            f"got: {add_result.stdout!r}",
        )
        # negative bound: no error text anywhere in the TOON success envelope
        self.assertNotIn("error", add_result.stdout.lower())

        # RED today: `validate` (the real schema-enforcement point for the sqlite
        # backend -- it reloads the schema fresh from disk on every call) must go
        # back to reporting the definitive empty state after the null-order_date
        # add, not flag the row it just accepted.
        after_add_validate = self._validate_orders(env=toon_env)
        self.assertEqual(
            after_add_validate.returncode, 0,
            f"validate orders should exit 0: stderr={after_add_validate.stderr!r}",
        )
        self.assertEqual(
            after_add_validate.stdout.strip(), "[0]:",
            f"expected the definitive empty TOON state '[0]:' after a null "
            f"order_date add, got: {after_add_validate.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()
