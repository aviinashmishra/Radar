"""Google Play companion-app tracker.

Every smartwatch / earbud brand ships an app, and the public Play Store page carries rating, rating count,
install bucket and "Updated on". Rating-count growth is a free proxy for active device sales; an app update
right before a launch is a tell; a rating slide is a support crisis you can advertise against.
Events: app_update, app_rating_change, app_growth.
"""
from __future__ import annotations
import html
import re
from .. import db
from ..config import settings
from .base import get, tracked, log

PAGE = "https://play.google.com/store/apps/details?id={pkg}&hl=en_IN&gl=IN"


def fetch(pkg: str) -> dict | None:
    r = get(PAGE.format(pkg=pkg), timeout=25, min_gap=2.0)
    if r.status_code != 200:
        return None
    t = r.text
    rv = re.search(r'ratingValue":"([0-9.]+)', t)
    rc = re.search(r'ratingCount":"([0-9]+)', t)
    up = re.search(r'>Updated on<[^>]*><[^>]*>([^<]+)<', t)
    dl = re.search(r'<div class="[^"]*">([0-9.,]+[KLMCr+]+)</div><div class="[^"]*">Downloads', t)
    name = re.search(r'<h1[^>]*><span[^>]*>([^<]+)</span>', t) or re.search(r'<title>([^<]+?) - Apps on Google Play', t)
    return {"package": pkg, "name": html.unescape(name.group(1)).strip() if name else pkg,
            "rating": round(float(rv.group(1)), 2) if rv else None, "ratings_count": int(rc.group(1)) if rc else None,
            "downloads": dl.group(1) if dl else "", "updated_on": up.group(1).strip() if up else ""}


ITUNES = "https://itunes.apple.com/lookup?id={id}&country=in"


def fetch_ios(app_id: int) -> dict | None:
    """Apple App Store (iTunes lookup API, free): rating, rating count, version and release date in the India store."""
    r = get(ITUNES.format(id=app_id), timeout=25, min_gap=1.0)
    if r.status_code != 200:
        return None
    try:
        res = (r.json().get("results") or [None])[0]
    except ValueError:
        return None
    if not res:
        return None
    return {"package": f"ios:{app_id}", "name": res.get("trackName") or str(app_id),
            "rating": round(float(res.get("averageUserRating") or 0), 2) or None, "ratings_count": int(res.get("userRatingCount") or 0),
            "downloads": "", "updated_on": (res.get("currentVersionReleaseDate") or "")[:10], "version": res.get("version", ""),
            "url": res.get("trackViewUrl") or f"https://apps.apple.com/in/app/id{app_id}", "seller": res.get("sellerName", "")}


def run_ios() -> int:
    n = 0
    for b in settings.brands:
        for app_id in (settings.company(b.name).get("ios") or []):
            try:
                cur = fetch_ios(int(app_id))
            except Exception as e:  # noqa: BLE001
                log.info("app store %s failed: %s", app_id, e)
                continue
            if not cur or cur["rating"] is None:
                continue
            prev = db.q1("SELECT * FROM app_metrics WHERE package=? ORDER BY id DESC LIMIT 1", (cur["package"],))
            with db.tx() as c:
                c.execute("INSERT INTO app_metrics(brand,package,name,rating,ratings_count,downloads,updated_on,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                          (b.name, cur["package"], cur["name"], cur["rating"], cur["ratings_count"], cur["downloads"], cur["updated_on"], db.now()))
            if not prev:
                continue
            if cur["updated_on"] and cur["updated_on"] != prev["updated_on"]:
                if db.add_event(b.name, "appstore", "app_update", f"{cur['name']} (iOS) updated to {cur['version']} on {cur['updated_on']}",
                                detail=f"rating {cur['rating']} from {cur['ratings_count']:,} ratings on the India App Store",
                                url=cur["url"], data=cur, dedupe_key=f"ios|{app_id}|update|{cur['updated_on']}"):
                    n += 1
            if prev["rating"] and abs(cur["rating"] - prev["rating"]) >= 0.1:
                direction = "fell" if cur["rating"] < prev["rating"] else "rose"
                if db.add_event(b.name, "appstore", "app_rating_change", f"{cur['name']} (iOS) rating {direction} {prev['rating']} -> {cur['rating']}",
                                detail="iPhone buyers skew premium: a rating slide there hurts the ₹5,000+ SKUs first.",
                                url=cur["url"], data=cur, dedupe_key=f"ios|{app_id}|rating|{cur['rating']}"):
                    n += 1
            if prev["ratings_count"] and cur["ratings_count"] - prev["ratings_count"] >= max(50, int(prev["ratings_count"] * 0.03)):
                if db.add_event(b.name, "appstore", "app_growth", f"{cur['name']} (iOS) gained {cur['ratings_count'] - prev['ratings_count']:,} ratings since last check",
                                detail="Rating-count growth on iOS tracks premium device sales.", url=cur["url"], data=cur,
                                dedupe_key=f"ios|{app_id}|growth|{cur['ratings_count']}"):
                    n += 1
    return n


@tracked("appstore")
def run() -> int:
    n = run_ios()
    for b in settings.brands:
        for pkg in b.apps:
            try:
                cur = fetch(pkg)
            except Exception as e:  # noqa: BLE001
                log.info("play %s failed: %s", pkg, e)
                continue
            if not cur or cur["rating"] is None:
                continue
            prev = db.q1("SELECT * FROM app_metrics WHERE package=? ORDER BY id DESC LIMIT 1", (pkg,))
            with db.tx() as c:
                c.execute("INSERT INTO app_metrics(brand,package,name,rating,ratings_count,downloads,updated_on,seen_at) "
                          "VALUES(?,?,?,?,?,?,?,?)", (b.name, pkg, cur["name"], cur["rating"], cur["ratings_count"],
                                                     cur["downloads"], cur["updated_on"], db.now()))
            url = PAGE.format(pkg=pkg)
            if not prev:
                continue
            if cur["updated_on"] and cur["updated_on"] != prev["updated_on"]:
                if db.add_event(b.name, "play", "app_update", f"{cur['name']} app updated ({cur['updated_on']})",
                                detail=f"rating {cur['rating']} from {cur['ratings_count']:,} ratings, {cur['downloads']} installs",
                                url=url, data=cur, dedupe_key=f"play|{pkg}|update|{cur['updated_on']}"):
                    n += 1
            if prev["rating"] and abs(cur["rating"] - prev["rating"]) >= 0.1:
                direction = "fell" if cur["rating"] < prev["rating"] else "rose"
                if db.add_event(b.name, "play", "app_rating_change", f"{cur['name']} rating {direction} {prev['rating']} -> {cur['rating']}",
                                detail="A falling companion-app rating is a support problem you can message against.",
                                url=url, data=cur, dedupe_key=f"play|{pkg}|rating|{cur['rating']}"):
                    n += 1
            if prev["ratings_count"] and cur["ratings_count"] and cur["ratings_count"] - prev["ratings_count"] >= max(200, int(prev["ratings_count"] * 0.02)):
                if db.add_event(b.name, "play", "app_growth",
                                f"{cur['name']} gained {cur['ratings_count'] - prev['ratings_count']:,} ratings since last check",
                                detail="Rating-count growth tracks active device sales.", url=url, data=cur,
                                dedupe_key=f"play|{pkg}|growth|{cur['ratings_count']}"):
                    n += 1
            if cur["downloads"] and prev["downloads"] and cur["downloads"] != prev["downloads"]:
                if db.add_event(b.name, "play", "app_growth", f"{cur['name']} crossed {cur['downloads']} installs (was {prev['downloads']})",
                                url=url, data=cur, dedupe_key=f"play|{pkg}|installs|{cur['downloads']}"):
                    n += 1
    return n
