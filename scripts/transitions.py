"""Compat shim (CR-OA-011) — `import transitions` resolves to the SAME module object as
`vidushi_oa.transitions`."""
import sys

import vidushi_oa.transitions as _impl

sys.modules[__name__] = _impl
