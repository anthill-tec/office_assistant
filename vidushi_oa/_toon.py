"""Thin TOON encode/decode shim over the official `toon-format` library
(imported as `toon_format`), exposing its `encode`/`decode` as `to_toon`/`from_toon`."""
import toon_format as toon


def to_toon(obj):
    """Encode a JSON-shaped object to its TOON string representation."""
    return toon.encode(obj)


def from_toon(s):
    """Decode a TOON string back to the JSON-shaped object."""
    return toon.decode(s)
