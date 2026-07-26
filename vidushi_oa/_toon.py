"""TOON encode/decode shim over the python-toon library (named oa_toon to avoid
the import-name clash with the `toon` package it wraps; cf. oa_mongo.py).

`python-toon` 0.1.1 mis-round-trips a *map key* that contains an ASCII bracket
(`[`/`]`): its encoder quotes the key (e.g. `"[GM]": 2`), but its decoder's
`parse_header` scans for the first `[` on every line and misreads that quoted
key as an array header, raising `Unterminated quoted key`. The mail source-tag
tally (`{"source_tag": {"[GM]": N, ...}}`, CR-OA-020 §S5) is exactly this shape.

We work around it losslessly *around* the library: before encoding we substitute
the ASCII brackets in map keys for their fullwidth twins (U+FF3B/U+FF3D, which the
decoder does not treat specially), and after decoding we substitute them back.
Only map *keys* containing a bracket are touched — values (which round-trip fine)
and bracket-free keys are byte-identical to before, so all other output is
unchanged.
"""
import toon

_ASCII_LB, _ASCII_RB = "[", "]"
_SAFE_LB, _SAFE_RB = "［", "］"  # fullwidth [ and ] — bracket-safe sentinels


def _encode_keys(obj):
    if isinstance(obj, dict):
        return {
            (k.replace(_ASCII_LB, _SAFE_LB).replace(_ASCII_RB, _SAFE_RB)
             if isinstance(k, str) else k): _encode_keys(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_encode_keys(v) for v in obj]
    return obj


def _decode_keys(obj):
    if isinstance(obj, dict):
        return {
            (k.replace(_SAFE_LB, _ASCII_LB).replace(_SAFE_RB, _ASCII_RB)
             if isinstance(k, str) else k): _decode_keys(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_decode_keys(v) for v in obj]
    return obj


def to_toon(obj):
    """Encode a JSON-shaped object to its TOON string representation."""
    return toon.encode(_encode_keys(obj))


def from_toon(s):
    """Decode a TOON string back to the JSON-shaped object."""
    return _decode_keys(toon.decode(s))
