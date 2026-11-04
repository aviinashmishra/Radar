"""Pebble Competitor Radar - live competitive intelligence for Indian D2C electronics."""
import sys

__version__ = "1.0.0"

# Windows consoles and redirected logs default to cp1252, which turns alert emoji and "·" into \U0001f4f0 and "�".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):  # not a real text stream (pythonw, some IDE consoles)
        pass
