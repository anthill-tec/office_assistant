"""Compat shim (CR-OA-011) — the CLI implementation moved into `vidushi_oa._cli`.

When imported (`import store`) this resolves to the SAME module object as `vidushi_oa._cli`
(which `vidushi_oa.cli` also re-exports), so the old `store.main` / `store.STORES` / …
attributes keep working and share identity. When run directly (`python3 scripts/store.py …`)
it just delegates to the real `main()`.
"""
import sys

if __name__ == "__main__":
    from vidushi_oa._cli import main
    main()
else:
    import vidushi_oa._cli as _impl
    sys.modules[__name__] = _impl
