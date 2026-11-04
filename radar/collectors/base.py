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

_last_hit: dict[str, float] = {}


def get(url: str, *, timeout: int = 25, min_gap: float = 1.0, **kw) -> requests.Response:
    """GET with a per-host minimum gap so we never hammer a store."""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    wait = _last_hit.get(host, 0) + min_gap - time.time()
    if wait > 0:
        time.sleep(wait)
    try:
        r = session.get(url, timeout=timeout, **kw)
    except (requests.ConnectionError, requests.Timeout):
        time.sleep(3)  # one retry: stores drop idle keep-alive connections mid-sweep
        r = session.get(url, timeout=timeout, **kw)
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
