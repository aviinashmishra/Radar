"""One-command launcher: `python run.py`

Starts Nazar (every sector radar in one process, hub on top), waits until it answers, then opens it in your browser.
`python run.py --radar` starts only the Pebble IoT radar (the original single-sector server).
Picks the next free port if 8080 is busy. Works from any folder.
"""
from __future__ import annotations
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

if not (ROOT / ".env").exists() and (ROOT / ".env.example").exists():
    (ROOT / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

try:
    import uvicorn  # noqa: F401
    import fastapi  # noqa: F401
    import feedparser  # noqa: F401
    import yaml  # noqa: F401
except ImportError:
    print("Installing dependencies (first run only)...")
    os.system(f'"{sys.executable}" -m pip install -q -r requirements-full.txt')


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


RADAR_ONLY = "--radar" in sys.argv
PORT = int(os.environ.get("NAZAR_PORT") or os.environ.get("RADAR_PORT") or free_port(8080))
os.environ["NAZAR_PORT"] = str(PORT)
URL = f"http://127.0.0.1:{PORT}"


def open_when_ready() -> None:
    import urllib.request
    for _ in range(120):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"{URL}/{'api' if RADAR_ONLY else 'nazar/api'}/health", timeout=2) as r:
                if r.status == 200:
                    print(f"\nDashboard: {URL}\n")
                    if not os.environ.get("NAZAR_RESTARTED"):
                        webbrowser.open(URL)
                    return
        except Exception:  # noqa: BLE001
            continue
    print(f"Server did not answer on {URL} after 2 minutes. Scroll up for the error.")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Starting {'Pebble Competitor Radar' if RADAR_ONLY else 'Nazar (all sectors)'} on {URL} (Ctrl+C to stop)")
    try:
        uvicorn.run("radar.server:app" if RADAR_ONLY else "nazar.server:app", host="127.0.0.1", port=PORT, log_level="info")
    except Exception as e:  # noqa: BLE001
        print(f"\nFAILED: {e}")
        input("Press Enter to close...")
