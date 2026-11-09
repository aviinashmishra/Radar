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


@hub.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@hub.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(FAVICON, media_type="image/svg+xml")


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
        return text.encode("utf-8")


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
        if engine.HOSTED:   # sector requests have the radar's own gate
            pc = engine.primary().sub("cloud")
            auth = next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"authorization"), None)
            if path not in ("/nazar/api/health", "/favicon.ico") and pc.needs_password(path) and not pc.authorized(auth):
                return await _plain(send, 401, "Password required", [(b"www-authenticate", b'Basic realm="Nazar", charset="UTF-8"')])
            if path.startswith("/nazar/api/"):   # every sector's latest snapshot before a cross-sector answer (one cheap check per minute each)
                await asyncio.gather(*(run_in_threadpool(sec.sub("cloud").refresh) for sec in engine.sectors()))
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


async def _plain(send, status: int, text: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())] + (extra or [])})
    await send({"type": "http.response.body", "body": body})


app = Nazar(hub)


def main() -> None:
    import uvicorn
    uvicorn.run("nazar.server:app", host=os.environ.get("NAZAR_HOST", "127.0.0.1"), port=int(os.environ.get("NAZAR_PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
