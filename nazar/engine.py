"""Sector engine: one isolated copy of the `radar` package per sector, all in this process.

The radar package is written for a single sector: its settings, database connection and schedule are module-level
singletons. Nazar loads the same source once per sector under a private package name (radar_auto, radar_fintech, ...),
with RADAR_CONFIG / RADAR_DB pointing at that sector's folder, so every copy keeps its own settings, SQLite file,
collectors and FastAPI app, and the hub can read them all side by side.
"""
from __future__ import annotations
import importlib
import importlib.util
import logging
import os
import tempfile
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
import yaml

log = logging.getLogger("nazar.engine")
ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path(os.environ.get("NAZAR_CONFIG") or ROOT / "config" / "nazar.yaml")
HOSTED = bool(os.environ.get("VERCEL")) or os.environ.get("RADAR_HOSTED") == "1"   # Vercel: snapshots in the temp folder, collectors on GitHub
DATA_DIR = Path(os.environ.get("NAZAR_DATA_DIR") or (Path(tempfile.gettempdir()) / "radar" if HOSTED else ROOT))   # relocates every sector database
ENV_KEYS = ("RADAR_CONFIG", "RADAR_DB", "RADAR_SECTOR", "RADAR_SECTOR_LABEL", "RADAR_SNAPSHOT")
_load_lock = threading.Lock()


@dataclass
class Sector:
    key: str
    label: str
    short: str
    icon: str
    color: str
    tagline: str
    config_path: Path
    db_path: Path
    snapshot: str
    primary: bool = False
    mod: ModuleType | None = None          # the package (radar_<key>)
    scheduler: str = "off"                 # off | running | external (another process already collects into this database)
    primed: bool = False
    thread: threading.Thread | None = None
    meta: dict = field(default_factory=dict)

    # ---- handles into the package ----
    def sub(self, name: str) -> ModuleType:
        return importlib.import_module(f"{self.mod.__name__}.{name}")

    @property
    def settings(self):
        return self.sub("config").settings

    @property
    def db(self):
        return self.sub("db")

    @property
    def app(self):
        return self.sub("server").app

    @property
    def runner(self):
        return self.sub("runner")

    @property
    def me(self) -> str:
        return self.settings.me

    def public(self) -> dict:
        s = self.settings
        return {"key": self.key, "label": self.label, "short": self.short, "icon": self.icon, "color": self.color, "tagline": self.tagline,
                "primary": self.primary, "me": s.me, "self_brand": s.self_brand.name if s.self_brand else None, "sector": s.sector,
                "brands": len(s.brands), "listed": sum(1 for c in s.companies.values() if c.get("ticker")),
                "indian": sum(1 for b in s.brands if b.origin == "Indian"), "global": sum(1 for b in s.brands if b.origin != "Indian"),
                "segments": [{"key": g["key"], "name": g["name"]} for g in s.segments if g.get("show", True)],
                "scheduler": self.scheduler, "world": s.world}


_SECTORS: list[Sector] = []


def registry() -> dict:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}


def _load_package(sec: Sector) -> ModuleType:
    """Import the radar source as package `radar_<key>` with this sector's environment."""
    name = f"radar_{sec.key}"
    if name in sys.modules:
        return sys.modules[name]
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    os.environ.update({"RADAR_CONFIG": str(sec.config_path), "RADAR_DB": str(sec.db_path), "RADAR_SECTOR": sec.key,
                       "RADAR_SECTOR_LABEL": sec.label, "RADAR_SNAPSHOT": sec.snapshot})
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "radar" / "__init__.py", submodule_search_locations=[str(ROOT / "radar")])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for sub in ("config", "db", "server", "runner", "collectors", "enrich", "signals", "alerts", "brief", "newsintel", "finance", "market",
                    "analytics", "chat", "voice", "channels", "regions", "strategy", "reports"):
            importlib.import_module(f"{name}.{sub}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def load_all() -> list[Sector]:
    """Load every sector listed in config/nazar.yaml (idempotent)."""
    global _SECTORS
    with _load_lock:
        if _SECTORS:
            return _SECTORS
        reg = registry()
        only = {k.strip() for k in (os.environ.get("NAZAR_SECTORS") or "").split(",") if k.strip()}  # tests: a subset
        out: list[Sector] = []
        for s in reg.get("sectors") or []:
            if only and s["key"] not in only and not s.get("primary"):
                continue
            db = Path(s["db"])
            sec = Sector(key=s["key"], label=s["label"], short=s.get("short") or s["label"], icon=s.get("icon") or "•", color=s.get("color") or "#2a78d6",
                         tagline=s.get("tagline") or "", config_path=(ROOT / s["config"]).resolve(),
                         db_path=db if db.is_absolute() else (DATA_DIR / db).resolve(), snapshot=s.get("snapshot") or f"{s['key']}.db.gz",
                         primary=bool(s.get("primary")))
            sec.db_path.parent.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            sec.mod = _load_package(sec)
            sec.db.connect()
            sec.sub("perspective").apply_saved()   # whose seat this sector writes from (stored in its db; default neutral)
            log.info("sector %s loaded in %.1fs (%d brands, db %s)", sec.key, time.time() - t0, len(sec.settings.brands), sec.db_path)
            out.append(sec)
        _SECTORS = out
        return out


def loaded() -> bool:
    return bool(_SECTORS)


def sectors() -> list[Sector]:
    return _SECTORS or load_all()


def get(key: str | None) -> Sector:
    secs = sectors()
    for s in secs:
        if s.key == key:
            return s
    return next((s for s in secs if s.primary), secs[0])


def primary() -> Sector:
    return get(None)


# ---------------------------------------------------------------- schedulers
def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _external_radar(sec: Sector) -> bool:
    """A plain `python -m radar.server` (port 8080) already collects into the primary database: do not run a second
    scheduler on the same file. Only the primary sector can be in that situation."""
    if not sec.primary:
        return False
    port = int(os.environ.get("NAZAR_PORT", "8080"))
    for other in (8080,):
        if other == port or not _port_open(other):
            continue
        try:
            import requests
            r = requests.get(f"http://127.0.0.1:{other}/api/stats", timeout=3)
            if r.ok and (r.json().get("sector_key") or "iot") == sec.key and not r.json().get("hosted"):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def start_schedulers(stop: threading.Event) -> None:
    """One scheduler thread per sector, started a little apart so the first sweeps do not all hit the same hosts at once.
    NAZAR_SCHEDULERS=off disables collection (dashboard only)."""
    if HOSTED:   # the GitHub Actions workflow collects; this process only serves the published snapshots
        for sec in sectors():
            sec.scheduler = "cloud"
        log.info("cloud mode: collectors run on GitHub Actions, snapshots from the data release")
        return
    if (os.environ.get("NAZAR_SCHEDULERS") or "on").lower() in ("off", "0", "no"):
        for sec in sectors():
            sec.scheduler = "off"
        log.info("schedulers disabled (NAZAR_SCHEDULERS=off)")
        return
    stagger = float(os.environ.get("NAZAR_STAGGER_SEC", "45"))
    medium_min = int(os.environ.get("NAZAR_MEDIUM_MIN", "45"))

    def _run(sec: Sector, delay: float) -> None:
        if stop.wait(delay):
            return
        try:
            sec.runner.loop(stop)
        except Exception:  # noqa: BLE001
            log.exception("scheduler for %s crashed", sec.key)
            sec.scheduler = "crashed"

    delay = 0.0
    for sec in sectors():
        if _external_radar(sec):
            sec.scheduler = "external"
            log.info("sector %s: another radar process already collects into %s; Nazar reads it without a second scheduler", sec.key, sec.db_path)
            continue
        if not sec.primary:  # sectors without stores have nothing for the 10-minute tier; news every 45 min is plenty
            sec.runner.INTERVALS["medium"] = medium_min * 60
        sec.thread = threading.Thread(target=_run, args=(sec, delay), daemon=True, name=f"nazar-sched-{sec.key}")
        sec.thread.start()
        sec.scheduler = "running"
        delay += stagger
    threading.Thread(target=_prime, args=(stop,), daemon=True, name="nazar-primer").start()
    threading.Thread(target=_prime_social, args=(stop,), daemon=True, name="nazar-social-primer").start()


def _prime(stop: threading.Event) -> None:
    """A new sector's first sweep reaches share prices and Wikipedia attention only after news, feeds and app stores.
    Fetch those two cheap sources first so the hub's market tape and attention board are alive within minutes."""
    if stop.wait(5):
        return
    for sec in sectors():
        if stop.is_set() or sec.scheduler not in ("running",):
            continue
        try:
            have = sec.db.q1("SELECT COUNT(*) c FROM quotes")["c"]
            if have:
                sec.primed = True
                continue
            fin = sec.sub("collectors.financials")
            att = sec.sub("collectors.attention")
            res = sec.sub("collectors.resolve")
            n1 = fin.run_quotes()
            fin.run_fundamentals()  # market cap, multiples, TTM revenue and profit: one batch call plus one per ticker
            res.run(only=("wikipedia",))  # a wrong or missing article title would leave the attention board empty
            n2 = att.run()
            sec.primed = True
            log.info("primed %s: quotes ok, %d quote events, %d attention events", sec.key, n1, n2)
        except Exception:  # noqa: BLE001
            log.exception("priming %s failed", sec.key)


def _prime_social(stop: threading.Event) -> None:
    """Social and research would otherwise wait for each sector's whole first sweep (news, feeds, reviews ...): start them
    straight away, a little apart, for every sector that has no social data yet or last read it over 3 hours ago."""
    if stop.wait(20):
        return
    for kind in ("social", "research"):
        for sec in sectors():
            if stop.is_set() or sec.scheduler != "running":
                continue
            try:
                s = sec.sub("social")
                (s.kick if kind == "social" else s.research_kick)()
            except Exception:  # noqa: BLE001
                log.exception("starting %s for %s failed", kind, sec.key)
            if stop.wait(8):
                return


def start_bot(stop: threading.Event) -> None:
    """The Telegram bot polls one token; only the primary sector runs it (the others still push alerts)."""
    sec = primary()
    if HOSTED or sec.scheduler == "external" or not sec.settings.telegram_token:
        return
    bot = sec.sub("bot")
    threading.Thread(target=bot.loop, args=(stop,), daemon=True, name="nazar-telegram").start()
