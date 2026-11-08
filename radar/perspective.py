"""Whose seat the radar writes from, chosen at runtime.

The dashboard can be read as a neutral analyst or "written for" one company of the sector: every "you", at-risk
figure, price match and own-sales panel then refers to that company. The choice is stored in this sector's database
(kv `perspective`), so it survives restarts and travels with the data; nothing in the YAML needs editing. With no
stored choice the radar is neutral, whatever `self:` says in competitors.yaml.
"""
from __future__ import annotations
import logging
import os
import subprocess
import sys
import threading
import time
from . import db
from .config import settings, CONFIG_PATH, ROOT

log = logging.getLogger("radar.perspective")
KEY = "perspective"


def current() -> str | None:
    return settings.self_brand.name if settings.self_brand else None


def options() -> list[dict]:
    return [{"name": b.name, "origin": b.origin, "tier": b.tier, "store": bool(b.shopify)} for b in settings.brands]


def state() -> dict:
    return {"self": current(), "own_data": settings.own_data, "options": options(), "me": settings.me}


def apply_saved() -> str | None:
    """Called once when the sector loads (server lifespan, or the Nazar engine): default neutral."""
    saved = db.kv_get(KEY)
    name = saved.get("brand") if isinstance(saved, dict) else saved
    try:
        chosen = settings.set_perspective(name or None)
    except ValueError:
        log.warning("stored perspective %r is not a brand any more; back to neutral", name)
        chosen = settings.set_perspective(None)
    log.info("perspective: %s", chosen or "neutral")
    return chosen


def set(name: str | None) -> dict:  # noqa: A001 - mirrors kv_set
    """Switch and persist; SKU matches computed from the old seat are rebuilt in the background."""
    chosen = settings.set_perspective((name or "").strip() or None)
    db.kv_set(KEY, {"brand": chosen})
    threading.Thread(target=_rebuild, name="radar-perspective", daemon=True).start()
    return state()


def _rebuild() -> None:
    """SKU matches pair the home brand's products with rivals, so they belong to a seat. The rebuild is a minute of
    pure CPU on a full catalogue; run it in a child process so the dashboard stays responsive (SQLite is in WAL mode,
    both processes can use the database). The child reads the stored seat itself."""
    if not current():
        return  # the neutral view has no home brand to match; the next sweep's rebuild clears the old pairs
    env = {**os.environ, "RADAR_DB": str(settings.db_path), "RADAR_CONFIG": str(CONFIG_PATH), "RADAR_SECTOR": settings.sector_key}
    code = "from radar import db, perspective, matcher; db.connect(); perspective.apply_saved(); print(matcher.rebuild())"
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            log.info("perspective: %s SKU matches rebuilt for %s in %.0fs", (r.stdout.strip().splitlines() or ["?"])[-1], current(), time.time() - t0)
        else:
            log.warning("match rebuild after perspective change failed: %s", (r.stderr or "").strip()[-400:])
    except Exception as e:  # noqa: BLE001
        log.warning("match rebuild after perspective change failed: %s", e)
