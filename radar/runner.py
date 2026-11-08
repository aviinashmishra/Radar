"""Scheduler. `python -m radar.runner --once` runs everything one time; `--loop` keeps it live forever.

Tiers:
  fast   every 10 min   shopify catalogue / prices / stock / best-sellers / hidden products
  medium every 30 min   google news, reddit, feeds
  slow   every 24 h     page watch, meta ads, new-store detection, daily brief
  weekly Monday 09:00   weekly brief
After every tier: enrich -> signals -> alerts.
"""
from __future__ import annotations
import argparse
import logging
import threading
import time
from datetime import datetime
from . import db, enrich, signals, alerts, brief
from .collectors import FAST, MEDIUM, SLOW, ALL

log = logging.getLogger("radar.runner")
INTERVALS = {"fast": 10 * 60, "medium": 30 * 60, "slow": 24 * 3600}
_busy = threading.Lock()


def busy() -> bool:
    return _busy.locked()


def post_process() -> dict:
    from . import maintenance, matcher
    try:
        maintenance.run()  # versioned data repairs; a no-op once a database is up to date
    except Exception:  # noqa: BLE001 - a failed repair must never stop scoring and alerts
        log.exception("maintenance failed; continuing")
    n_enriched = 0
    while n := enrich.enrich_pending():  # drain: a first sweep makes ~800 events and only scored ones reach the dashboard
        n_enriched += n
    n_matches = matcher.rebuild()
    n_signals = signals.run()
    n_alerts = alerts.flush()
    return {"enriched": n_enriched, "matches": n_matches, "signals": n_signals, "alerts": n_alerts}


def run_tier(tier: str) -> dict:
    mods = {"fast": FAST, "medium": MEDIUM, "slow": SLOW, "all": FAST + MEDIUM + SLOW}[tier]
    if not _busy.acquire(blocking=False):
        return {"skipped": "another run in progress"}
    try:
        t0 = time.time()
        counts = {}
        for m in mods:
            name = m.__name__.rsplit(".", 1)[-1]
            log.info("collector %s starting", name)
            counts[name] = m.run()
            log.info("collector %s -> %s new events", name, counts[name])
            # a first sweep of a new sector lands thousands of headlines before the tier ends: score as we go so the
            # dashboard shows moves within minutes instead of after the whole tier (same total work, earlier)
            if (db.q1("SELECT COUNT(*) c FROM events WHERE enriched=0") or {}).get("c", 0) >= 100:
                while enrich.enrich_pending():
                    pass
        post = post_process()
        db.kv_set(f"last_run:{tier}", {"at": db.now(), "counts": counts, **post, "seconds": round(time.time() - t0)})
        return {"tier": tier, "counts": counts, **post, "seconds": round(time.time() - t0)}
    finally:
        _busy.release()


def run_one(name: str) -> dict:
    """One collector by name (news, financials, trends, attention, ...) followed by the usual scoring, matching,
    signals and alerts. Progress lives in kv `collect:<name>` so the dashboard button can poll it."""
    mods = {m.__name__.rsplit(".", 1)[-1]: m for m in ALL}
    if name not in mods:
        return {"error": f"unknown collector {name}"}
    if not _busy.acquire(blocking=False):
        return {"skipped": "another run in progress"}
    db.kv_set(f"collect:{name}", {"state": "running", "started": db.now()})
    t0 = time.time()
    try:
        n = mods[name].run()
        post = post_process()
        res = {"state": "done", "at": db.now(), "added": n, **post, "seconds": round(time.time() - t0)}
    except Exception as e:  # noqa: BLE001 - the button must always get an answer
        log.exception("collector %s failed", name)
        res = {"state": "failed", "at": db.now(), "error": f"{type(e).__name__}: {e}", "seconds": round(time.time() - t0)}
    finally:
        _busy.release()
    db.kv_set(f"collect:{name}", res)
    return res


def loop(stop: threading.Event | None = None) -> None:
    """Blocking scheduler loop. First pass runs every tier so the DB is warm."""
    stop = stop or threading.Event()
    last = {k: 0.0 for k in INTERVALS}
    last_weekly = db.kv_get("last_weekly_brief", "")
    last_daily = db.kv_get("last_daily_brief", "")
    while not stop.is_set():
        now = time.time()
        for tier, every in INTERVALS.items():
            if now - last[tier] >= every:
                try:
                    log.info("tier %s: %s", tier, run_tier(tier))
                except Exception:  # noqa: BLE001
                    log.exception("tier %s crashed", tier)
                last[tier] = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        if datetime.now().hour >= 8 and last_daily != today:
            brief.write(days=1)
            db.kv_set("last_daily_brief", today)
            last_daily = today
        week = datetime.now().strftime("%G-W%V")
        if datetime.now().weekday() == 0 and datetime.now().hour >= 9 and last_weekly != week:
            brief.write(days=7)
            db.kv_set("last_weekly_brief", week)
            last_weekly = week
        if datetime.now().hour >= 8 and db.kv_get("last_reports_check", "") != today:  # daily / weekly / monthly / yearly / sector reports
            try:
                from . import reports
                reports.run_due(push=True)
            except Exception:  # noqa: BLE001
                log.exception("reports failed")
            db.kv_set("last_reports_check", today)
        stop.wait(30)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pebble Competitor Radar runner")
    ap.add_argument("--once", action="store_true", help="run every collector once and exit (good for GitHub Actions)")
    ap.add_argument("--tier", choices=["fast", "medium", "slow", "all"], default="all")
    ap.add_argument("--loop", action="store_true", help="run forever on the tier schedule")
    ap.add_argument("--brief", choices=["1", "7"], help="write and push a 1-day or 7-day brief, then exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if a.brief:
            print(brief.write(days=int(a.brief)))
        elif a.loop:
            loop()
        else:
            print(run_tier(a.tier))
    finally:
        db.close()


if __name__ == "__main__":
    main()
