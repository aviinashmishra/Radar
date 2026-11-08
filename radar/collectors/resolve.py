"""Identity resolution: finds the public ids a brand needs for the slow-tier sensors when competitors.yaml /
companies.yaml do not name them, so a new sector gets app reviews, app metrics, Wikipedia attention and SEC filings
without anyone typing ids by hand.

  ios        App Store ids from the iTunes Search API (India store); the seller or the app title must name the brand
  apps       Google Play packages from the store's search page; the package or the app's title must name the brand
  wikipedia  the English article, validated with the page-summary API (a configured title that 404s or lands on a
             disambiguation page is replaced by a search hit that reads like a company article)
  sec_cik    the SEC CIK for a US-listed ticker or ADR, from the SEC's own company_tickers.json

Results live in kv `ids:<brand>` with the day they were checked and are re-checked every 14 days (3 days while
something is still missing). `ids(brand)` merges config and resolved values and is what the other collectors read.
Configured values always win; resolution only fills gaps, and a wrong configured Wikipedia title is dropped rather
than fetched every day.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from .. import db
from ..config import settings
from .base import get, tracked, log

ITUNES_SEARCH = "https://itunes.apple.com/search?term={q}&country=in&entity=software&limit=12"
PLAY_SEARCH = "https://play.google.com/store/search?q={q}&c=apps&hl=en_IN&gl=IN"
PLAY_PAGE = "https://play.google.com/store/apps/details?id={pkg}&hl=en_IN&gl=IN"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{t}"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit=6&srprop=snippet"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
WUA = {"User-Agent": "PebbleRadar/1.0 (competitive research; public data only)"}
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
RECHECK_DAYS = 14
RECHECK_EMPTY_DAYS = 3
MIN_IOS_RATINGS = 30
# words that appear in many company names and never identify one on their own
GENERIC = {"india", "indian", "motor", "motors", "energy", "health", "healthcare", "labs", "lab", "bank", "money", "pay", "air",
           "app", "the", "and", "for", "of", "group", "ltd", "limited", "inc", "co", "company", "technologies", "technology",
           "digital", "online", "new", "power", "green", "solar", "electric", "auto", "care", "hospitals", "hospital", "tech",
           "one", "my", "go", "store", "shop", "mobile", "global", "world", "first", "life", "home", "smart", "net", "web", "plus"}
COMPANYISH = re.compile(r"\b(company|companies|corporation|brand|start-?up|manufacturer|platform|airline|bank|app\b|service|"
                        r"retailer|firm|operator|provider|marketplace|conglomerate|multinational|maker|automaker|developer|"
                        r"chain|utility|exchange|insurer|lender|laboratory|e-?commerce|fintech|edtech|subsidiary|headquartered)", re.I)


# ---------------------------------------------------------------- name matching
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _names(b) -> list[str]:
    out = [b.name] + list(b.aliases or [])
    return [n for n in dict.fromkeys(_norm(n) for n in out) if n]


def _compressed(b) -> list[str]:
    """'Tata Motors' -> 'tatamotors', 'MakeMyTrip' -> 'makemytrip'; only forms long enough to be unmistakable."""
    return [c for c in (n.replace(" ", "") for n in _names(b)) if len(c) >= 5]


def _tokens(b) -> set[str]:
    toks = set()
    for n in _names(b):
        for t in n.split():
            if len(t) >= 4 and t not in GENERIC:
                toks.add(t)
    return toks


def mentions(text: str, b) -> bool:
    """Does `text` name the brand? Whole-word match on the name or an alias; an ambiguous name (Hero, Jupiter, Yatra)
    counts only when it is the full name and nothing on the brand's exclude list is present."""
    t = " " + _norm(text) + " "
    if any(_norm(x) and (" " + _norm(x) + " ") in t for x in (b.exclude or [])):
        return False
    return any((" " + n + " ") in t for n in _names(b))


def _excluded(text: str, b) -> bool:
    t = " " + _norm(text) + " "
    return any(_norm(x) and (" " + _norm(x) + " ") in t for x in (b.exclude or []))


# ---------------------------------------------------------------- App Store (iTunes Search API)
def find_ios(b) -> list[int]:
    found: list[tuple[int, int]] = []
    for term in [b.name] + list(b.aliases or [])[:1]:
        try:
            r = get(ITUNES_SEARCH.format(q=quote(term)), timeout=30, min_gap=1.0)
        except Exception as e:  # noqa: BLE001
            log.info("itunes search %s failed: %s", term, e)
            continue
        if r.status_code != 200:
            continue
        try:
            results = r.json().get("results") or []
        except ValueError:
            continue
        for x in results:
            seller, title = x.get("sellerName") or "", x.get("trackName") or ""
            if _excluded(seller + " " + title, b):
                continue
            ok = mentions(seller, b) or (not b.ambiguous and mentions(title, b))
            if b.ambiguous and not mentions(seller, b):
                ok = False
            if ok and (x.get("userRatingCount") or 0) >= MIN_IOS_RATINGS and x.get("trackId"):
                found.append((int(x["userRatingCount"]), int(x["trackId"])))
        if found:
            break
    found.sort(reverse=True)
    return [i for _, i in found[:2]]


# ---------------------------------------------------------------- Google Play (search page + app page)
def _play_title(pkg: str) -> str:
    try:
        r = get(PLAY_PAGE.format(pkg=pkg), timeout=25, min_gap=2.0, headers={"User-Agent": CHROME_UA})
    except Exception:  # noqa: BLE001
        return ""
    if r.status_code != 200:
        return ""
    m = re.search(r'<h1[^>]*><span[^>]*>([^<]+)</span>', r.text) or re.search(r'<title>([^<]+?) - Apps on Google Play', r.text)
    dev = re.search(r'/store/apps/dev(?:eloper)?\?id=([^"&]+)"[^>]*>(?:<span[^>]*>)?([^<]{2,80})<', r.text)
    return " ".join(x for x in ((m.group(1) if m else ""), (dev.group(2) if dev else "")) if x)


def find_play(b) -> list[str]:
    try:
        r = get(PLAY_SEARCH.format(q=quote(b.name)), timeout=30, min_gap=2.0, headers={"User-Agent": CHROME_UA})
    except Exception as e:  # noqa: BLE001
        log.info("play search %s failed: %s", b.name, e)
        return []
    if r.status_code != 200:
        return []
    ids = list(dict.fromkeys(re.findall(r'/store/apps/details\?id=([a-zA-Z0-9_.]+)', r.text)))[:8]
    if not ids:
        return []
    comp, toks = _compressed(b), _tokens(b)
    if b.ambiguous:  # Yatra must not pick up DigiYatra: the name has to be a whole package segment
        sure = [p for p in ids if any(c in p.lower().split(".") for c in comp + list(toks))]
    else:
        sure = [p for p in ids if any(c in p.lower() for c in comp) or any(t in p.lower().split(".") for t in toks)]
    out = sure[:2]
    if not out:  # nothing in the package names: read the first two app pages and trust the title + developer
        for p in ids[:2]:
            if mentions(_play_title(p), b):
                out.append(p)
    return out


# ---------------------------------------------------------------- Wikipedia
def _summary(title: str) -> dict | None:
    for attempt in range(2):
        try:
            r = get(WIKI_SUMMARY.format(t=quote(title.replace(" ", "_"), safe="")), timeout=40, min_gap=1.2, headers=WUA)
        except Exception as e:  # noqa: BLE001
            log.info("wikipedia summary %s failed: %s", title, e)
            return None
        if r.status_code == 429 and attempt == 0:  # the REST API throttles bursts; one patient retry
            time.sleep(8)
            continue
        if r.status_code == 404:
            return {"missing": True}
        if r.status_code != 200:
            return None  # throttled or down: undecided, never "missing"
        try:
            return r.json()
        except ValueError:
            return None
    return None


def _article_ok(s: dict | None, b) -> bool:
    if not s or s.get("missing") or s.get("type") != "standard":
        return False
    text = (s.get("title") or "") + " " + (s.get("description") or "") + " " + (s.get("extract") or "")
    if _excluded(text, b):
        return False
    return bool(COMPANYISH.search(text)) or mentions(s.get("description") or "", b)


def find_wikipedia(b, configured: str | None, entity: str | None) -> tuple[str | None, bool]:
    """Returns (title or None, decided). decided=False means Wikipedia could not be reached, so nothing changes."""
    if configured:
        s = _summary(configured)
        if s is None:
            return configured, False
        if _article_ok(s, b) or (s.get("type") == "standard" and not s.get("missing")):
            return s.get("title") or configured, True  # the configured title stands (redirects resolve to the real title)
    for q in (f"{b.name} {entity or ''}".strip(), f"{b.name} company"):
        try:
            r = get(WIKI_SEARCH.format(q=quote(q)), timeout=40, min_gap=0.5, headers=WUA)
        except Exception as e:  # noqa: BLE001
            log.info("wikipedia search %s failed: %s", q, e)
            return configured, False
        if r.status_code != 200:
            continue
        try:
            hits = r.json()["query"]["search"]
        except (ValueError, KeyError):
            continue
        for h in hits:
            title = h.get("title") or ""
            if not mentions(title, b) or _excluded(title, b):  # the article itself must be named after the brand
                continue
            s = _summary(title)
            if s is None:
                return configured, False
            if _article_ok(s, b):
                return s.get("title") or title, True
    return None, True


# ---------------------------------------------------------------- SEC CIK
def _sec_map() -> dict[str, int]:
    cached = db.kv_get("sec_tickers") or {}
    if cached.get("day") and cached["day"] >= (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d"):
        return cached.get("map") or {}
    from .financials import _ua
    try:
        r = get(SEC_TICKERS, timeout=60, min_gap=1.0, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
    except Exception as e:  # noqa: BLE001
        log.info("sec company_tickers failed: %s", e)
        return cached.get("map") or {}
    if r.status_code != 200:
        return cached.get("map") or {}
    try:
        m = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values() if v.get("ticker")}
    except (ValueError, AttributeError, KeyError, TypeError):
        return cached.get("map") or {}
    db.kv_set("sec_tickers", {"day": db.now()[:10], "map": m})
    return m


def find_cik(ticker: str | None) -> int | None:
    if not ticker or "." in ticker:  # .NS / .BO / .L / .T ... are not SEC filers
        return None
    return _sec_map().get(ticker.upper().replace("-", "-"))


# ---------------------------------------------------------------- merged view
def resolved(brand: str) -> dict:
    return db.kv_get(f"ids:{brand}") or {}


def ids(brand: str) -> dict:
    """Config first, resolved values for what config leaves blank. `wikipedia` is None when the configured title was
    checked and found missing, so callers stop fetching it."""
    b = settings.brand(brand)
    c = settings.company(brand)
    r = resolved(brand)
    out = {"ios": [int(x) for x in (c.get("ios") or [])] or [int(x) for x in (r.get("ios") or [])],
           "apps": list(b.apps if b else []) or list(r.get("apps") or []),
           "sec_cik": c.get("sec_cik") or r.get("sec_cik"),
           "wikipedia": c.get("wikipedia")}
    if r.get("wikipedia_checked"):
        out["wikipedia"] = r.get("wikipedia")
    return out


def _stale(r: dict, missing: bool) -> bool:
    day = r.get("checked")
    if not day:
        return True
    age = (datetime.now(timezone.utc).date() - datetime.strptime(day, "%Y-%m-%d").date()).days
    return age >= (RECHECK_EMPTY_DAYS if missing else RECHECK_DAYS)


@tracked("resolve")
def run(only: tuple[str, ...] | None = None) -> int:
    """Fill missing ids for every brand. Returns the number of new ids found. `only` limits the kinds
    (e.g. ("wikipedia",) for the quick priming pass)."""
    kinds = set(only or ("ios", "apps", "wikipedia", "sec_cik"))
    n = 0
    for b in settings.brands:
        c = settings.company(b.name)
        r = resolved(b.name)
        need_ios = "ios" in kinds and not c.get("ios") and not r.get("ios")
        need_apps = "apps" in kinds and not b.apps and not r.get("apps")
        need_wiki = "wikipedia" in kinds and not r.get("wikipedia_checked")
        need_cik = "sec_cik" in kinds and not c.get("sec_cik") and not r.get("sec_cik") and c.get("ticker") and "." not in c["ticker"]
        missing = any((need_ios, need_apps, need_wiki, need_cik))
        if not _stale(r, missing):
            continue  # checked recently: every 3 days while something is missing, every 14 days once complete
        recheck = _stale(r, False)  # the 14-day pass re-resolves everything config leaves blank, in case a store listing changed
        upd = dict(r)
        if "ios" in kinds and not c.get("ios") and (need_ios or recheck):
            got = find_ios(b)
            if got:
                n += len(set(got) - set(upd.get("ios") or []))
                upd["ios"] = got
        if "apps" in kinds and not b.apps and (need_apps or recheck):
            got = find_play(b)
            if got:
                n += len(set(got) - set(upd.get("apps") or []))
                upd["apps"] = got
        if "wikipedia" in kinds and (need_wiki or recheck):
            title, decided = find_wikipedia(b, c.get("wikipedia"), c.get("entity"))
            if decided:
                if title and title != c.get("wikipedia") and title != upd.get("wikipedia"):
                    n += 1
                if not title and c.get("wikipedia"):
                    log.info("wikipedia: no article found for %s (configured %r is missing or a disambiguation page)", b.name, c.get("wikipedia"))
                upd["wikipedia"], upd["wikipedia_checked"] = title, True
        if need_cik:
            cik = find_cik(c.get("ticker"))
            if cik:
                upd["sec_cik"] = cik
                n += 1
        upd["checked"] = db.now()[:10]
        db.kv_set(f"ids:{b.name}", upd)
        found = {k: upd.get(k) for k in ("ios", "apps", "wikipedia", "sec_cik") if upd.get(k)}
        if found:
            log.info("ids %s: %s", b.name, json.dumps(found, ensure_ascii=False))
    return n


def coverage() -> dict:
    """How many brands now have each id (config + resolved), for the Settings page and the hub."""
    tot = {"brands": len(settings.brands), "ios": 0, "apps": 0, "wikipedia": 0, "sec_cik": 0, "ticker": 0}
    for b in settings.brands:
        i = ids(b.name)
        tot["ios"] += bool(i["ios"])
        tot["apps"] += bool(i["apps"])
        tot["wikipedia"] += bool(i["wikipedia"])
        tot["sec_cik"] += bool(i["sec_cik"])
        tot["ticker"] += bool(settings.company(b.name).get("ticker"))
    return tot
