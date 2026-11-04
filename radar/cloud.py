"""Cloud mode: the dashboard on Vercel, the collectors on GitHub Actions, one snapshot between them.

Vercel runs the app as a serverless function: no background scheduler, no disk that survives, no always-on
bot thread. So the radar is split into two halves, neither of which needs a server of its own:

  GitHub Actions (worker)   runs the collectors on a schedule. `python -m radar.cloud pull` restores the
                            last database at the start of a run, `python -m radar.cloud push` publishes it
                            at the end as `radar.db.gz` on the release tagged `data`. A release file is
                            replaced, never committed, so the repository does not grow with every sweep.
  Vercel (dashboard)        each function instance downloads that snapshot into its temp folder and checks
                            for a newer one at most once a minute. A check is one small request: a new upload
                            gets a new download address, so nothing is downloaded unless the data changed.

Things that need the worker go back to GitHub: Run starts the workflow (GITHUB_TOKEN with Actions: write),
and the Telegram bot answers through a webhook instead of polling.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import hmac
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from . import db
from .config import settings

log = logging.getLogger("radar.cloud")

HOSTED = bool(os.environ.get("VERCEL")) or os.environ.get("RADAR_HOSTED") == "1"
REPO = (os.environ.get("RADAR_REPO") or os.environ.get("GITHUB_REPOSITORY")
        or ("{}/{}".format(os.environ["VERCEL_GIT_REPO_OWNER"], os.environ["VERCEL_GIT_REPO_SLUG"])
            if os.environ.get("VERCEL_GIT_REPO_OWNER") and os.environ.get("VERCEL_GIT_REPO_SLUG") else "")
        or settings.cloud_repo)
BRANCH = os.environ.get("RADAR_BRANCH", "main")
TAG = os.environ.get("RADAR_DATA_TAG", "data")
WORKFLOW = os.environ.get("RADAR_WORKFLOW", "radar.yml")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
PASSWORD = os.environ.get("RADAR_PASSWORD", "")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
WEB = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
TTL = int(os.environ.get("RADAR_SNAPSHOT_TTL", "60"))
DB_ASSET = "radar.db.gz"
SHOT_DAYS = 3            # screenshots kept on the release: enough for today's diff and the before/after view
STALE_HOURS = 3          # the fast tier runs every 15 minutes, so a 3-hour-old snapshot means the worker stopped
SHOT_RX = re.compile(r"^(?P<slug>.+)-(?P<day>\d{4}-\d{2}-\d{2})\.(?:png|jpe?g)$")
TG_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or (
    hashlib.sha256(f"radar-webhook:{settings.telegram_token}".encode()).hexdigest()[:40] if settings.telegram_token else "")
RELEASE_NOTE = ("Machine-written by the Pebble Competitor Radar workflow. `radar.db.gz` is the latest database "
                "(replaced after every sweep); the images are the last few days of homepage screenshots. "
                "The Vercel dashboard reads these files. Do not edit by hand.")


class NoSnapshot(Exception):
    """Nothing published yet (first run) or the repo cannot be read."""


# ---------------------------------------------------------------- GitHub helpers
def _gh(method: str, path_or_url: str, **kw) -> requests.Response:
    url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", **kw.pop("headers", {})}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return requests.request(method, url, headers=headers, timeout=kw.pop("timeout", 30), **kw)


def public_url(name: str) -> str:
    return f"{WEB}/{REPO}/releases/download/{TAG}/{name}"


def _release(create: bool = False) -> dict | None:
    r = _gh("GET", f"/repos/{REPO}/releases/tags/{TAG}")
    if r.status_code == 404:
        if not create:
            return None
        r = _gh("POST", f"/repos/{REPO}/releases",
                json={"tag_name": TAG, "name": "Radar data snapshot", "body": RELEASE_NOTE, "prerelease": True})
    r.raise_for_status()
    return r.json()


def _asset_bytes(asset: dict) -> bytes:
    r = _gh("GET", asset["url"], headers={"Accept": "application/octet-stream"}, timeout=120)
    r.raise_for_status()
    return r.content


def _upload(rel: dict, name: str, data: bytes, mime: str) -> dict:
    r = _gh("POST", rel["upload_url"].split("{", 1)[0], params={"name": name}, data=data,
            headers={"Content-Type": mime}, timeout=300)
    r.raise_for_status()
    return r.json()


def _delete_asset(asset: dict) -> None:
    _gh("DELETE", f"/repos/{REPO}/releases/assets/{asset['id']}").raise_for_status()


def _replace(rel: dict, name: str, data: bytes, mime: str) -> None:
    """Upload under a temporary name, then swap, so readers keep the old file until the new one is complete."""
    tmp = name + ".next"
    for a in rel["assets"]:
        if a["name"] == tmp:  # left behind by a run that died mid-upload
            _delete_asset(a)
    new = _upload(rel, tmp, data, mime)
    for a in rel["assets"]:
        if a["name"] == name:
            _delete_asset(a)
    _gh("PATCH", f"/repos/{REPO}/releases/assets/{new['id']}", json={"name": name}).raise_for_status()


def _output(key: str, value: str) -> None:
    """Hand a value to later workflow steps (no-op outside GitHub Actions)."""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def _mime(name: str) -> str:
    return "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else "image/png"


# ---------------------------------------------------------------- snapshot in / out
def install(blob: bytes) -> None:
    """Swap the live database for a downloaded snapshot (gzipped or plain)."""
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("snapshot is not a SQLite database")
    path = settings.db_path
    tmp = path.with_name(path.name + ".incoming")
    tmp.write_bytes(raw)
    with db._lock:  # queries take the same lock, so none runs against a half-swapped file
        db.close()
        for ext in ("-wal", "-shm"):
            Path(f"{path}{ext}").unlink(missing_ok=True)
        os.replace(tmp, path)
        db.connect()


def _fetch(current: str | None) -> tuple[str, bytes | None]:
    """(key, gzipped db) of the published snapshot; the bytes are None when the key has not changed."""
    if not REPO:
        raise NoSnapshot("RADAR_REPO is not set (owner/name of the GitHub repo that runs the workflow)")
    if TOKEN:  # works for private repos; the API says which upload is current without downloading it
        rel = _release()
        asset = next((a for a in (rel or {}).get("assets", []) if a["name"] == DB_ASSET), None)
        if not asset:
            raise NoSnapshot(f"no {DB_ASSET} on the '{TAG}' release of {REPO} yet: the first workflow run has not finished")
        key = f"{asset['id']}:{asset['updated_at']}"
        return (key, None) if key == current else (key, _asset_bytes(asset))
    r = requests.get(public_url(DB_ASSET), allow_redirects=False, timeout=20)
    if r.status_code == 404:
        raise NoSnapshot(f"no {DB_ASSET} published for {REPO} yet (or the repo is private: set GITHUB_TOKEN)")
    if r.is_redirect:  # github.com redirects each upload to its own storage address
        loc = r.headers["Location"]
        key = loc.split("?", 1)[0]
        if key == current:
            return key, None
        d = requests.get(loc, timeout=120)
        d.raise_for_status()
        return key, d.content
    r.raise_for_status()
    key = hashlib.sha256(r.content).hexdigest()
    return (key, None) if key == current else (key, r.content)


_state: dict = {"key": None, "loaded_at": None, "checked": 0.0, "error": None}
_refresh_lock = threading.Lock()


def refresh(force: bool = False) -> None:
    """Make sure this instance serves the latest snapshot. Cheap when called often: it checks once per TTL."""
    if not HOSTED:
        return
    loaded = _state["key"] is not None
    wait = TTL if loaded else min(TTL, 15)
    if not force and time.time() - _state["checked"] < wait:
        return
    # the first load blocks every request until it lands; later checks run once while others serve the current copy
    if not _refresh_lock.acquire(blocking=not loaded):
        return
    try:
        if not force and time.time() - _state["checked"] < wait:
            return
        _state["checked"] = time.time()
        key, blob = _fetch(_state["key"])
        if blob is not None:
            install(blob)
            _state.update(key=key, loaded_at=db.now())
            log.info("snapshot loaded (%.1f MB)", len(blob) / 1e6)
        _state["error"] = None
    except NoSnapshot as e:
        _state["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - keep serving the copy we have
        _state["error"] = f"snapshot check failed: {e}"[:300]
        log.warning(_state["error"])
    finally:
        _refresh_lock.release()


def info() -> dict | None:
    """What the dashboard shows about the cloud setup (None when running locally)."""
    if not HOSTED:
        return None
    snap = db.kv_get("snapshot") or {}
    return {"repo": REPO, "published_at": snap.get("at"), "tier": snap.get("tier"), "run_url": snap.get("run_url"),
            "loaded_at": _state["loaded_at"], "error": _state["error"], "can_run": bool(TOKEN and REPO),
            "password": bool(PASSWORD), "telegram_webhook": bool(settings.telegram_token),
            "release_url": f"{WEB}/{REPO}/releases/tag/{TAG}" if REPO else None,
            "actions_url": f"{WEB}/{REPO}/actions/workflows/{WORKFLOW}" if REPO else None}


# ---------------------------------------------------------------- hosted actions
def public_lock() -> str | None:
    """On a deployment without a password, anything that spends AI credits or starts work is switched off."""
    if HOSTED and not PASSWORD:
        return ("This deployment has no password, so actions that use your AI key or start sweeps are off. "
                "Set RADAR_PASSWORD in the Vercel project settings and redeploy.")
    return None


def dispatch(tier: str) -> dict:
    """Start a sweep on GitHub Actions (the only place collectors run in cloud mode)."""
    if not (TOKEN and REPO):
        return {"started": None, "message": "Sweeps run on GitHub Actions on their own schedule. To start one from "
                                             "here, add GITHUB_TOKEN (fine-grained, Actions: write) to the Vercel project."}
    r = _gh("POST", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches", json={"ref": BRANCH, "inputs": {"tier": tier}})
    if r.status_code in (200, 204):
        return {"started": tier, "where": "github", "url": f"{WEB}/{REPO}/actions/workflows/{WORKFLOW}",
                "message": f"{tier} sweep queued on GitHub Actions. New data appears here a minute after it finishes."}
    return {"started": None, "message": f"GitHub did not start the sweep ({r.status_code}): {r.text[:160]}"}


def shot(name: str) -> tuple[str, str | bytes]:
    """('redirect', url) for a public repo, ('bytes', data) when a token is needed to read the release."""
    if not TOKEN:
        return "redirect", public_url(name)
    rel = _release() or {}
    asset = next((a for a in rel.get("assets", []) if a["name"] == name), None)
    if not asset:
        raise NoSnapshot(name)
    return "bytes", _asset_bytes(asset)


def authorized(header: str | None) -> bool:
    """HTTP Basic auth against RADAR_PASSWORD (any user name). Always true when no password is set."""
    if not PASSWORD:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        pw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace").partition(":")[2]
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(pw.encode(), PASSWORD.encode())


OPEN_PATHS = {"/api/health", "/favicon.ico", "/api/telegram/webhook"}  # each checks its own secret where needed


def needs_password(path: str) -> bool:
    return bool(PASSWORD) and path not in OPEN_PATHS and not path.startswith("/api/cron/")


def heartbeat(act: bool) -> dict:
    """Daily Vercel cron: if the worker has gone quiet, say so (Telegram / webhook) and try to restart it."""
    at = (db.kv_get("snapshot") or {}).get("at")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() / 3600 if at else None
    out = {"snapshot_at": at, "age_hours": round(age, 1) if age is not None else None, "stale": age is None or age > STALE_HOURS}
    if out["stale"] and act:
        from . import alerts
        out["dispatch"] = dispatch("fast")
        where = f"{WEB}/{REPO}/actions" if REPO else "the repo's Actions tab"
        alerts.send_text(f"⚠️ Pebble Radar: the cloud data is {'missing' if age is None else f'{age:.0f} h old'}. "
                         f"GitHub Actions may be paused or failing: {where}")
        out["alerted"] = bool(settings.telegram_token or settings.webhook_url)
    return out


# ---------------------------------------------------------------- workflow CLI
def pull() -> bool:
    """Start of a workflow run: restore the published database and the newest screenshot of each brand."""
    try:
        rel = _release()
        asset = next((a for a in (rel or {}).get("assets", []) if a["name"] == DB_ASSET), None)
        if not asset:
            raise NoSnapshot("no snapshot published yet")
    except NoSnapshot as e:
        print(f"{e}: this run starts a fresh database")
        _output("restored", "false")
        return False
    blob = _asset_bytes(asset)
    install(blob)
    newest: dict[str, dict] = {}
    for a in rel["assets"]:
        m = SHOT_RX.match(a["name"])
        if m and m["day"] > newest.get(m["slug"], {}).get("day", ""):
            newest[m["slug"]] = {"day": m["day"], "asset": a}
    shots = settings.db_path.parent / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    for v in newest.values():  # yesterday's capture is what today's is diffed against
        (shots / v["asset"]["name"]).write_bytes(_asset_bytes(v["asset"]))
    print(f"restored {DB_ASSET} ({len(blob) / 1e6:.1f} MB gzipped) and {len(newest)} screenshots")
    _output("restored", "true")
    return True


def push() -> None:
    """End of a workflow run: publish the database and any new screenshots; prune screenshots past SHOT_DAYS."""
    if not (TOKEN and REPO):
        sys.exit("push needs GITHUB_TOKEN and the repo (GITHUB_REPOSITORY in Actions, else RADAR_REPO)")
    run = os.environ.get("GITHUB_RUN_ID")
    db.kv_set("snapshot", {"at": db.now(), "tier": os.environ.get("RADAR_TIER"),
                           "run_url": f"{WEB}/{REPO}/actions/runs/{run}" if run else None})
    db.close()  # checkpoint the WAL so radar.db alone is complete
    blob = gzip.compress(settings.db_path.read_bytes(), compresslevel=9)
    rel = _release(create=True)
    _replace(rel, DB_ASSET, blob, "application/gzip")
    have = {a["name"] for a in rel["assets"]}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SHOT_DAYS)).strftime("%Y-%m-%d")
    shots = settings.db_path.parent / "shots"
    sent = 0
    for p in sorted(shots.iterdir()) if shots.exists() else []:
        m = SHOT_RX.match(p.name)
        if m and m["day"] >= cutoff and p.name not in have:
            _upload(rel, p.name, p.read_bytes(), _mime(p.name))
            sent += 1
    pruned = 0
    for a in rel["assets"]:
        m = SHOT_RX.match(a["name"])
        if m and m["day"] < cutoff:
            _delete_asset(a)
            pruned += 1
    print(f"published {DB_ASSET} ({len(blob) / 1e6:.1f} MB) to {REPO}@{TAG}; screenshots +{sent} -{pruned}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Move the radar database between GitHub Actions runs and the Vercel dashboard")
    ap.add_argument("action", choices=["pull", "push"])
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        pull() if a.action == "pull" else push()
    finally:
        db.close()


if __name__ == "__main__":
    main()
