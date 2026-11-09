"""Shared helpers: polite HTTP, text cleaning, run bookkeeping."""
from __future__ import annotations
import hashlib
import logging
import re
import time
from functools import wraps
import requests
from .. import db

log = logging.getLogger("radar")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PebbleRadar/1.0 (+competitive-intel; public data only)"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.8"})

# Nazar loads this package once per sector, so the per-host clock lives on `sys`: ten sectors polling Reddit, Google
# News or Wikipedia at once then still look like one polite client instead of ten (which is what earned the 429s).
import sys as _sys
_last_hit: dict[str, float] = getattr(_sys, "_radar_last_hit", None) or {}
_sys._radar_last_hit = _last_hit
_lock = getattr(_sys, "_radar_hit_lock", None) or __import__("threading").Lock()
_sys._radar_hit_lock = _lock
RETRY_429 = {"www.reddit.com": 30, "en.wikipedia.org": 8, "wikimedia.org": 8, "play.google.com": 20, "news.google.com": 15, "itunes.apple.com": 5,
             "api.gdeltproject.org": 10, "api.openalex.org": 5, "export.arxiv.org": 10, "hn.algolia.com": 5, "api.bsky.app": 10}


def get(url: str, *, timeout: int = 25, min_gap: float = 1.0, **kw) -> requests.Response:
    """GET with a per-host minimum gap (shared across every sector in the process) so we never hammer a host; a 429
    waits the host's Retry-After (or a per-host default) once and tries again."""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    while True:
        with _lock:
            wait = _last_hit.get(host, 0) + min_gap - time.time()
            if wait <= 0:
                _last_hit[host] = time.time() + min_gap  # reserve the slot before the request so parallel sectors queue up
                break
        time.sleep(min(wait, 5))
    try:
        r = session.get(url, timeout=timeout, **kw)
    except (requests.ConnectionError, requests.Timeout):
        time.sleep(3)  # one retry: stores drop idle keep-alive connections mid-sweep
        r = session.get(url, timeout=timeout, **kw)
    if r.status_code == 429 and host in RETRY_429:
        try:
            pause = min(60, int(r.headers.get("Retry-After") or RETRY_429[host]))
        except ValueError:
            pause = RETRY_429[host]
        log.info("%s rate-limited; waiting %ss", host, pause)
        with _lock:
            _last_hit[host] = time.time() + pause
        time.sleep(pause)
        r = session.get(url, timeout=timeout, **kw)
    with _lock:
        _last_hit[host] = time.time()
    return r


class Unavailable(Exception):
    """The source answered but has nothing usable (blocked, moved, 404). Logged as a failed run, without a traceback."""


def get_json(url: str, **kw):
    r = get(url, **kw)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def clean_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:24]


def tracked(name: str):
    """Decorator: log each collector run per brand into the runs table."""
    def deco(fn):
        @wraps(fn)
        def wrapper(brand_name: str = "*", *a, **kw):
            started = db.now()
            try:
                n = fn(*a, **kw) if brand_name == "*" else fn(brand_name, *a, **kw)
                db.log_run(name, brand_name, started, True, n or 0)
                return n or 0
            except Unavailable as e:
                log.warning("%s: %s %s", name, brand_name, e)
                db.log_run(name, brand_name, started, False, 0, str(e))
                return 0
            except Exception as e:  # noqa: BLE001 - a collector must never kill the loop
                log.exception("%s failed for %s", name, brand_name)
                db.log_run(name, brand_name, started, False, 0, f"{type(e).__name__}: {e}")
                return 0
        return wrapper
    return deco
