"""TOON encode/decode shim over the python-toon library (named oa_toon to avoid
the import-name clash with the `toon` package it wraps; cf. oa_mongo.py)."""
import toon


def to_toon(obj):
    """Encode a JSON-shaped object to its TOON string representation."""
    return toon.encode(obj)


def from_toon(s):
    """Decode a TOON string back to the JSON-shaped object."""
    return toon.decode(s)
