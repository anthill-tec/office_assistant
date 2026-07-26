"""Vidushi office-assistant package (CR-OA-011)."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vidushi-oa")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
