"""Compat shim (CR-OA-011) — the implementation moved into the `vidushi_oa` package.

`import oa_mongo` now resolves to the SAME module object as `vidushi_oa._mongo` (which
`vidushi_oa.mongo` also re-exports), so the old path keeps working AND shares the same
process-wide client cache / module state. Aliasing (rather than a star re-import) is what
lets callers mutate shared private state (e.g. resetting `oa_mongo._client`) and observe
identical objects via `assertIs`.
"""
import sys

import vidushi_oa._mongo as _impl

sys.modules[__name__] = _impl
