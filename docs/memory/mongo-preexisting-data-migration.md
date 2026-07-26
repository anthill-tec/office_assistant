---
name: mongo-preexisting-data-migration
description: "This machine's OA KEEPS its store on Mongo vidushi_oa@27017 (no forced SQLite migration); set VIDUSHI_BACKEND=mongo for CLI + SessionStart hook. Public package ships SQLite default + Mongo opt-in."
metadata:
  type: project
---

**Decision (user, 2026-07-26): on THIS machine, OA keeps its store on MongoDB — do NOT migrate the
data to SQLite.** The local **Mongo `vidushi_oa` on `127.0.0.1:27017`** holds the user's real
accumulated office-assistant data from prior sessions, and it stays the live store here. The **public
package** (CR-OA-018) ships **SQLite as the zero-config default** for new users, **with Mongo opt-in**
via the `[mongo]` extra — but that default does not change how the user runs OA locally.

- **Port note:** `27017` = the office-assistant instance (`vidushi_oa` db). **`27018` hosts the user's
  platform DBs and is OFF-LIMITS** — never touch it.
- mongod runs normally on this machine (it was only stopped temporarily so `act`'s `mongo:7` service
  could bind 27017 during CI validation — see [[cicd-release-convention]]).

## Operational requirement — the default is now SQLite, so pin Mongo locally

Since CR-OA-018 made **SQLite the default backend**, running `voa` here with no override reads an
**empty SQLite store**, not the real Mongo data. So `VIDUSHI_BACKEND=mongo` MUST be set persistently
and reach **both**:
- the **`voa` CLI** (every invocation), and
- the **`.claude/settings.json` SessionStart hook** (the bare-CLI `attention` worklist) — else the
  session opens against the empty SQLite default and shows nothing.

Set it where both inherit it (e.g. an exported/universal env var and/or the hook command's env). Verify
with `voa stats <type>` showing the real counts.

## Compatibility (confirmed from code, 2026-07-26)

Same db + same collections — CR-OA-018's pluggable backend wraps the same pymongo collections, **no
rename**. Collections = store-type keys: `contacts · invoices · warranties · cases · products ·
subscriptions · insurance · orders`. Only `orders` (CR-OA-015) is newer than the data → simply empty.

## Optional SQLite copy (NOT required)

If a SQLite copy is ever wanted (a portable backup, or to dogfood the shipped default), the snapshot→
import bridge produces one without disturbing Mongo:
`VIDUSHI_BACKEND=mongo voa snapshot` (→ `data/*.jsonl`) then `voa import` (→ SQLite default). Run
`VIDUSHI_BACKEND=mongo voa validate` first if importing, in case the $jsonSchema validators tightened
since older docs were written.
