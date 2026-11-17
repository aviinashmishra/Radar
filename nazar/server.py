"""Nazar hub: the ASGI front door for every sector radar plus the cross-sector API and page.

Routing
    /                      Nazar home (nazar/static/index.html)
    /nazar/api/*           cross-sector API (sectors, pulse, tape, universe, themes, lens, search, ask, run)
    /nazar/static/*        hub assets
    /s/{key}               switch sector (cookie) and open that sector's dashboard
    /app, /api/*, /static/*, /shots/*
                           the selected sector's own radar app (unchanged radar code), with Nazar's bar injected into
                           its page and its texts written for that sector's perspective
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from . import engine, hubdata

log = logging.getLogger("nazar.server")
STATIC = Path(__file__).parent / "static"
RADAR_STATIC = Path(__file__).parent.parent / "radar" / "static"   # the design system (theme.css) is shared with the sector dashboards
COOKIE = "nazar_sector"
RADAR_PATHS = ("/api/", "/static/", "/shots/")
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine.load_all()
    engine.start_schedulers(_stop)
    engine.start_bot(_stop)
    if not engine.HOSTED:   # first hub page answers at once (cloud: the databases are still empty here, before any snapshot)
        threading.Thread(target=hubdata.sectors_overview, daemon=True, name="nazar-overview-warm").start()
    exit_after = float(os.environ.get("NAZAR_EXIT_AFTER") or 0)   # tests and CI: stop by itself
    if exit_after:
        threading.Timer(exit_after, lambda: os._exit(0)).start()
    log.info("Nazar up: %s", ", ".join(f"{s.key}({s.scheduler})" for s in engine.sectors()))
    yield
    _stop.set()


hub = FastAPI(title="Nazar", version="1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#0f2a5c"/>'
           '<circle cx="32" cy="32" r="21" fill="#ffffff"/><circle cx="32" cy="32" r="13" fill="#2a78d6"/><circle cx="32" cy="32" r="6" fill="#0b0b0b"/>'
           '<circle cx="28" cy="27" r="2.2" fill="#fff"/></svg>')


def _public() -> bool:
    """The hosted deployment is the public site (radar/cloud.PUBLIC): pages hide the operator's controls."""
    return engine.HOSTED and os.environ.get("NAZAR_PUBLIC", "1") != "0"


@hub.get("/", response_class=HTMLResponse)
def home():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    if _public():
        html = html.replace("<html ", '<html data-public="1" ', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@hub.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(FAVICON, media_type="image/svg+xml")


@hub.get("/robots.txt", include_in_schema=False)
def robots():
    # pages are for people and search engines; the JSON API, exports and screenshots are not worth crawling
    return Response("User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /nazar/api/\nDisallow: /shots/\n", media_type="text/plain")


@hub.get("/nazar/static/{name}", include_in_schema=False)
def static_file(name: str):
    p = (STATIC / name).resolve()
    if not p.is_file():
        p = (RADAR_STATIC / name).resolve()
    if not p.is_file() or (STATIC.resolve() not in p.parents and RADAR_STATIC.resolve() not in p.parents):
        raise HTTPException(404)
    media = {"js": "application/javascript", "css": "text/css", "html": "text/html", "svg": "image/svg+xml"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return Response(p.read_bytes(), media_type=media, headers={"Cache-Control": "no-cache"})


@hub.get("/s/{key}", include_in_schema=False)
def switch(key: str):
    sec = engine.get(key)
    r = RedirectResponse("/app", status_code=302)   # the browser keeps the #/route fragment across the redirect
    r.set_cookie(COOKIE, sec.key, max_age=365 * 86400, samesite="lax")
    return r


# ---------------------------------------------------------------- hub API
@hub.get("/nazar/api/sectors")
async def api_sectors():
    return await run_in_threadpool(hubdata.sectors_overview)


@hub.get("/nazar/api/pulse")
async def api_pulse(min_importance: int = 5, limit: int = 120, since: str = "", sector: str = "", kind: str = "", q: str = "", days: int = 7):
    return await run_in_threadpool(hubdata.pulse, min_importance, min(limit, 400), since, sector, kind, q, days)


@hub.get("/nazar/api/tape")
async def api_tape():
    return await run_in_threadpool(hubdata.tape)


@hub.get("/nazar/api/universe")
async def api_universe():
    return await run_in_threadpool(hubdata.universe)


@hub.get("/nazar/api/themes")
async def api_themes(days: int = 7):
    return await run_in_threadpool(hubdata.themes, max(1, min(days, 90)))


@hub.get("/nazar/api/lens")
async def api_lens(days: int = 30):
    return await run_in_threadpool(hubdata.lens, max(1, min(days, 365)))


@hub.get("/nazar/api/search")
async def api_search(q: str = ""):
    return await run_in_threadpool(hubdata.search, q)


class AskIn(BaseModel):
    question: str
    sector: str | None = None


@hub.post("/nazar/api/ask")
async def api_ask(body: AskIn):
    return await run_in_threadpool(hubdata.ask, body.question, body.sector)


@hub.post("/nazar/api/run")
async def api_run(sector: str = "", tier: str = Query("medium", pattern="^(fast|medium|slow|all)$")):
    if sector == "all":
        if engine.HOSTED and engine.primary().sub("cloud").public_lock():   # one "off here" note, not one per sector
            return [hubdata.run(None, tier)]
        return [hubdata.run(s.key, tier) for s in engine.sectors()]
    return hubdata.run(sector or None, tier)


ROOT = Path(__file__).resolve().parent.parent


def _code_mtime() -> float:
    """Newest Python file under nazar/ and radar/: the hub must restart to pick those up (static files are read per request)."""
    newest = 0.0
    for folder in (ROOT / "nazar", ROOT / "radar"):
        for f in folder.rglob("*.py"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    return newest


BUILD_LOADED = _code_mtime()


def build_info() -> dict:
    on_disk = _code_mtime()
    return {"loaded": BUILD_LOADED, "on_disk": on_disk, "stale": on_disk > BUILD_LOADED + 1, "pid": os.getpid()}


def _relaunch() -> None:
    """Start a fresh `python run.py` on the same port once this process has let go of the socket, then exit."""
    host, port = os.environ.get("NAZAR_HOST", "127.0.0.1"), int(os.environ.get("NAZAR_PORT", "8080"))
    child = (f"import socket, time, os, sys, runpy\n"
             f"for _ in range(120):\n"
             f"    s = socket.socket(); s.settimeout(0.5)\n"
             f"    if s.connect_ex(({host!r}, {port})) != 0: s.close(); break\n"
             f"    s.close(); time.sleep(0.5)\n"
             f"os.chdir({str(ROOT)!r}); sys.argv = ['run.py']\n"
             f"runpy.run_path('run.py', run_name='__main__')\n")
    log_path = ROOT / "data" / "nazar.log"
    log_path.parent.mkdir(exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    with open(log_path, "ab") as out:
        subprocess.Popen([sys.executable, "-X", "utf8", "-c", child], cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True, env={**os.environ, "NAZAR_RESTARTED": "1", "NAZAR_PORT": str(port)})
    time.sleep(1.0)
    os._exit(0)


@hub.post("/nazar/api/restart")
def api_restart():
    """Relaunch the whole hub (every sector) so new Python is loaded; the pages offer this when build_info says stale."""
    if engine.HOSTED:
        return {"ok": False, "message": "This is the cloud deployment: Vercel redeploys it on every push, nothing to restart here."}
    if any(s.runner.busy() for s in engine.sectors() if s.mod is not None and s.scheduler == "running"):
        return {"ok": False, "message": "A sweep is running in one of the sectors; restart when it finishes."}
    threading.Timer(0.5, _relaunch).start()
    return {"ok": True, "message": "Restarting Nazar… every sector reconnects in a few seconds.", "pid": os.getpid()}


@hub.get("/nazar/api/health")
def api_health():
    h = hubdata.health()
    h["build"] = build_info()
    h["pid"] = os.getpid()
    return h


NOT_FOUND = """<!doctype html><html lang="en" data-theme="auto"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found · Nazar</title><link rel="icon" href="/favicon.ico"><link rel="stylesheet" href="/nazar/static/theme.css">
<style>body{min-height:100vh;display:grid;place-items:center;padding:16px}.card{max-width:440px;text-align:center;display:grid;gap:12px;padding:32px}
h1{font-size:22px;letter-spacing:-.02em}a.btn{justify-self:center;padding:9px 18px;border-radius:999px;background:var(--accent);color:var(--accent-ink,#fff);font-weight:600}
a.btn:hover{text-decoration:none}</style>
<script>try{var t=localStorage.getItem("theme")||"auto";document.documentElement.dataset.theme=t;document.documentElement.dataset.dark=matchMedia("(prefers-color-scheme: dark)").matches?"1":"0"}catch(e){}</script></head>
<body><div class="card"><h1>This page is not here</h1><div class="muted">The link may be old, or the address has a typo. Every sector and company is one search away from the home page.</div>
<a class="btn" href="/">Go to Nazar</a></div></body></html>"""


@hub.get("/{rest:path}", include_in_schema=False)
def not_found(rest: str):
    """Last route: an unknown page gets a page back (unknown API paths keep their JSON 404)."""
    if rest.startswith("nazar/"):
        raise HTTPException(404)
    return HTMLResponse(NOT_FOUND, status_code=404)


# ---------------------------------------------------------------- dispatcher: sector apps behind one origin
def _cookie(scope, name: str) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                if "=" in part:
                    kk, vv = part.strip().split("=", 1)
                    if kk == name:
                        return vv
    return None


def _sector_for(scope) -> engine.Sector:
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    key = (qs.get("sector") or [None])[0] or _cookie(scope, COOKIE)
    return engine.get(key)


_UI_RULES_CACHE: dict[str, list[tuple[re.Pattern, str]]] = {}


def _ui_rules(sec: engine.Sector) -> list[tuple[re.Pattern, str]]:
    """How the radar's own page text reads inside this sector: the dashboard was written for Pebble."""
    st = sec.settings
    key = f"{sec.key}:{st.self_brand.name if st.self_brand else ''}"   # the seat can change at runtime (perspective picker)
    if key not in _UI_RULES_CACHE:
        rules = [(re.compile(r"\bPebble (Competitor )?Radar\b"), "Nazar")]
        if st.self_brand and st.self_brand.name != "Pebble":
            rules.append((re.compile(r"\bPebble\b"), st.self_brand.name))
        elif not st.self_brand:
            rules += [(re.compile(r"\bPebble's\b"), "home brand's"), (re.compile(r"\bPebble\b"), "home brand")]
        _UI_RULES_CACHE[key] = rules
    return _UI_RULES_CACHE[key]


# ---------------------------------------------------------------- reading a sector from another seat
# The worker writes each event's "why it matters" (so_what) and "what to do" (counter_move) for the sector's YAML home
# brand: in IoT that is Pebble ("compare against Pebble's nearest model"). A visitor reading IoT neutrally, or as
# another company, should not get Pebble's to-do list: as another company the advice is re-addressed to that company;
# neutrally the clauses that address Pebble are dropped (the rest of the sentence stays). Rows about Pebble itself
# keep their text, and quoted text (a search query, a review) is never touched.
_ADVICE_KEYS = ("so_what", "counter_move")
_CLAUSE = re.compile(r"(?<=[.;:])\s+")
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”')


def _drop_home_clauses(text: str, home_rx: re.Pattern) -> str:
    parts = _CLAUSE.split(text)
    keep = [p for p in parts if not home_rx.search(_QUOTED.sub("", p))]
    if len(keep) == len(parts):
        return text
    for i in range(len(keep) - 1):   # "winning on; [dropped]. Quote: ..." -> "winning on. Quote: ..."
        if keep[i].endswith(";") and keep[i + 1][:1].isupper():
            keep[i] = keep[i][:-1] + "."
    out = " ".join(keep).strip()
    return re.sub(r"[;:,]$", ".", out)


def _reseat(obj, home: str, seat: str | None, home_rx: re.Pattern):
    if isinstance(obj, list):
        for x in obj:
            _reseat(x, home, seat, home_rx)
    elif isinstance(obj, dict):
        if obj.get("brand") != home:
            # a signal (no "source") keeps its advice in "detail"; an event's detail may be raw text (an article, a review):
            # neutrally its home-addressed clauses go too, but it is never re-addressed to another company
            signal = "source" not in obj
            for k in _ADVICE_KEYS + (("detail",) if "detail" in obj else ()):
                v = obj.get(k)
                if isinstance(v, str) and home in v:
                    if seat:
                        if k != "detail" or signal:
                            obj[k] = home_rx.sub(seat, v)
                    else:
                        obj[k] = _drop_home_clauses(v, home_rx)
        if isinstance(obj.get("body"), str) and "period" in obj and home in obj["body"]:   # a written brief: its "->" lines are moves
            lines = obj["body"].split("\n")
            if seat:
                obj["body"] = "\n".join(home_rx.sub(seat, ln) if ln.lstrip().startswith("->") else ln for ln in lines)
            else:
                obj["body"] = "\n".join(ln for ln in lines if not (ln.lstrip().startswith("->") and home_rx.search(ln)))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _reseat(v, home, seat, home_rx)


def _seat_rewrite(sec: engine.Sector, payload):
    """Re-address a sector's stored advice to the current seat (see above). Returns True when anything may have changed."""
    home = sec.settings.config_self
    seat = sec.settings.self_brand.name if sec.settings.self_brand else None
    if not home or seat == home:
        return False
    _reseat(payload, home, seat, re.compile(rf"\b{re.escape(home)}\b"))
    return True


_RADAR_NAME = re.compile(r"\bPebble (Competitor )?Radar\b", re.I)   # the old product name inside stored briefs and report titles
_PUBLIC_TEXT = [(re.compile(r", model in config/market\.yaml"), ", model estimate"),
                (re.compile(r"\(see market\.yaml for the assumptions\)"), "(a model estimate)"),
                (re.compile(r"config/market\.yaml, config/companies\.yaml"), "seeded industry estimates"),
                (re.compile(r"config/market\.yaml \(pebble\.revenue_cr\)"), "company estimate"),
                (re.compile(r"(in )?config/(market|companies|channels|competitors)\.yaml"), "")]


def _sectors_json() -> str:
    return json.dumps([{"key": s.key, "label": s.label, "short": s.short, "icon": s.icon, "color": s.color, "primary": s.primary} for s in engine.sectors()])


class _Rewriter:
    """Buffers HTML / JSON / JS answers of a sector app so Nazar can inject its bar and re-address the text."""

    def __init__(self, sec: engine.Sector, send, path: str):
        self.sec, self._send, self.path = sec, send, path
        self.mode: str | None = None
        self.start: dict | None = None
        self.buf = bytearray()

    async def send(self, message):
        t = message["type"]
        if t == "http.response.start":
            ct = b""
            for k, v in message.get("headers") or []:
                if k.lower() == b"content-type":
                    ct = v.lower()
            if b"text/event-stream" in ct:
                self.mode = None
            elif b"text/html" in ct:
                self.mode = "html"
            elif b"application/json" in ct:
                self.mode = "json"
            elif b"javascript" in ct:
                self.mode = "js"
            if self.mode:
                self.start = message
                return
            return await self._send(message)
        if t == "http.response.body" and self.mode:
            self.buf += message.get("body", b"")
            if message.get("more_body"):
                return
            body = self._transform(bytes(self.buf))
            headers = [(k, v) for k, v in self.start["headers"] if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
            await self._send({**self.start, "headers": headers})
            await self._send({"type": "http.response.body", "body": body, "more_body": False})
            self.mode = None
            return
        await self._send(message)

    def _transform(self, raw: bytes) -> bytes:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw
        sec = self.sec
        if self.mode == "html":
            text = text.replace("<title>Pebble Radar</title>", f"<title>Nazar · {sec.label}</title>", 1)
            text = text.replace(">Pebble Radar<", f">Nazar · {sec.short}<", 1)
            initial = (sec.settings.self_brand.name[:1] if sec.settings.self_brand else "✦")
            text = text.replace('font-weight:600">P</text>', f'font-weight:600">{initial}</text>', 1)
            text = text.replace("</head>", '<link rel="stylesheet" href="/nazar/static/bar.css">\n</head>', 1)
            payload = json.dumps({"sector": sec.key, "label": sec.label, "short": sec.short, "icon": sec.icon, "color": sec.color, "me": sec.me,
                                  "self": sec.settings.self_brand.name if sec.settings.self_brand else None, "own_data": sec.settings.own_data, "primary": sec.primary, "scheduler": sec.scheduler})
            text = text.replace("</body>", f'<script>window.NAZAR={payload};window.NAZAR.sectors={_sectors_json()};</script>\n'
                                           f'<script src="/nazar/static/bar.js"></script>\n</body>', 1)
            for rx, rep in _ui_rules(sec):   # static page text says "Pebble" for the home brand: read it as the current seat
                text = rx.sub(rep, text)
        elif self.mode == "js":
            for rx, rep in _ui_rules(sec):
                text = rx.sub(rep, text)
        elif self.mode == "json":
            if not sec.primary and "Pebble" in text:
                text = sec.settings.localize(text) or text
            home = sec.settings.config_self
            if home and home in text and not self.path.startswith("/api/export"):
                try:
                    data = json.loads(text)
                    if _seat_rewrite(sec, data):
                        text = json.dumps(data, ensure_ascii=False)
                except ValueError:
                    pass
            text = _RADAR_NAME.sub("Nazar", text)
            if _public() and ".yaml" in text:   # stored briefs and reports cite the operator's config files
                for rx, rep in _PUBLIC_TEXT:
                    text = rx.sub(rep, text)
        return text.encode("utf-8")


def _sector_by_key(key) -> engine.Sector | None:
    return next((s for s in engine.sectors() if s.key == key), None) if isinstance(key, str) else None


def _hub_reseat(obj) -> None:
    """Cross-sector answers carry rows from every sector: re-address each sector's advice to that sector's seat."""
    if isinstance(obj, list):
        for x in obj:
            _hub_reseat(x)
        return
    if not isinstance(obj, dict):
        return
    sec = _sector_by_key(obj["key"]) if "top" in obj and isinstance(obj.get("key"), str) else _sector_by_key(obj.get("sector"))
    if sec is not None and sec.settings.config_self:
        _seat_rewrite(sec, obj)
        return
    for v in obj.values():
        if isinstance(v, (dict, list)):
            _hub_reseat(v)


class _HubJson:
    """Buffers a hub JSON answer and passes it through _hub_reseat."""

    def __init__(self, send):
        self._send, self.start, self.buf, self.json = send, None, bytearray(), False

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.json = any(k.lower() == b"content-type" and b"application/json" in v.lower() for k, v in message.get("headers") or [])
            if self.json:
                self.start = message
                return
            return await self._send(message)
        if message["type"] == "http.response.body" and self.json:
            self.buf += message.get("body", b"")
            if message.get("more_body"):
                return
            body = bytes(self.buf)
            if any(s.settings.config_self and s.settings.config_self.encode() in body for s in engine.sectors()):
                try:
                    data = json.loads(body)
                    _hub_reseat(data)
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                except ValueError:
                    pass
            headers = [(k, v) for k, v in self.start["headers"] if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
            await self._send({**self.start, "headers": headers})
            await self._send({"type": "http.response.body", "body": body, "more_body": False})
            self.json = False
            return
        await self._send(message)


class Nazar:
    """ASGI app: hub routes for Nazar itself, the selected sector's radar app for everything the radar page requests."""

    def __init__(self, hub_app: FastAPI):
        self.hub = hub_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.hub(scope, receive, send)
        if not engine.loaded():   # serverless: the first request of an instance may arrive before (or without) lifespan
            await run_in_threadpool(engine.load_all)
        path = scope.get("path") or "/"
        if path == "/api/restart":   # a sector page's restart button restarts the whole hub
            scope2 = dict(scope); scope2["path"], scope2["raw_path"] = "/nazar/api/restart", b"/nazar/api/restart"
            return await self.hub(scope2, receive, send)
        if not (path == "/app" or path.startswith(RADAR_PATHS)):   # hub request: apply this browser's per-sector seats (cookies seat_<key>)
            tokens = []
            for sec in engine.sectors():
                raw = _cookie(scope, f"seat_{sec.key}")
                if raw is not None:
                    cfg = sec.sub("config")
                    tokens.append((cfg, cfg.SEAT.set(unquote(raw))))
            try:
                return await self._hub_request(scope, receive, send, path)
            finally:
                for cfg, tok in tokens:
                    cfg.SEAT.reset(tok)
        return await self._sector_request(scope, receive, send, path)

    async def _hub_request(self, scope, receive, send, path):
        if engine.HOSTED:   # open to everyone: no password on the hub or the sectors
            if path.startswith("/nazar/api/"):   # every sector's latest snapshot before a cross-sector answer (one cheap check per minute each)
                await asyncio.gather(*(run_in_threadpool(sec.sub("cloud").refresh) for sec in engine.sectors()))
        if path.startswith("/nazar/api/") and path != "/nazar/api/health":
            send = _HubJson(send).send
        return await self.hub(scope, receive, send)

    async def _sector_request(self, scope, receive, send, path):
        sec = _sector_for(scope)
        scope2 = dict(scope)
        if path == "/app":
            scope2["path"], scope2["raw_path"] = "/", b"/"
        rewriter = _Rewriter(sec, send, path)
        # the browser's seat for this sector also applies while the page text is rewritten (the radar's own gate
        # sets it for its handlers, but that scope ends before the response body is sent)
        raw = _cookie(scope, f"seat_{sec.key}")
        cfg = sec.sub("config")
        token = cfg.SEAT.set(unquote(raw)) if raw is not None else None
        try:
            return await sec.app(scope2, receive, rewriter.send)
        finally:
            if token is not None:
                cfg.SEAT.reset(token)


app = Nazar(hub)


def main() -> None:
    import uvicorn
    uvicorn.run("nazar.server:app", host=os.environ.get("NAZAR_HOST", "127.0.0.1"), port=int(os.environ.get("NAZAR_PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
