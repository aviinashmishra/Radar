"""`python -m nazar` starts the hub with every sector radar inside it.

Port: NAZAR_PORT (default 8080). If 8080 is taken by an older single-sector radar process, Nazar starts on 8090
instead, adopts that process's database for the IoT sector (no second scheduler on the same file) and says so.
"""
from __future__ import annotations
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def _busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    port = int(os.environ.get("NAZAR_PORT") or 8080)
    if _busy(port):
        alt = next(p for p in range(8090, 8110) if not _busy(p))
        print(f"Port {port} is in use (an older radar window?). Nazar starts on {alt}; close the old window and restart to take {port} back.")
        port = alt
        os.environ["NAZAR_PORT"] = str(port)
    print(f"Nazar: http://127.0.0.1:{port}   (Ctrl+C to stop)")
    from nazar.server import main as serve
    serve()


if __name__ == "__main__":
    main()
