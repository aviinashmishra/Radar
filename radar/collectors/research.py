"""Research papers, for every company in every sector (daily, resumable).

  authored   papers with an author at the company:
               Crossref   author affiliations that name the company (Crossref's match is fuzzy: kept only when an
                          affiliation really contains the name)
               Europe PMC affiliation search, for the health sector
               OpenAlex   the company as a research institution, while OpenAlex's free daily budget lasts (or always,
                          with OPENALEX_API_KEY)
  about      papers that name the company in the title (Crossref, OpenAlex) and arXiv preprints that name it
  sector     the most cited recent research on the sector's own topics (brand 'Sector')

Crossref, Europe PMC and arXiv need no key and have no daily budget. Runs one sector at a time across the hub
(the sources are shared) and picks up the companies read longest ago first, so an interrupted run still makes
progress. A new paper by a company becomes a `research` event.
"""
from __future__ import annotations
import html as _html
import os
import re
import sys
import threading as _threading
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from .. import db
from ..config import settings
from .base import get, tracked, log

OA = "https://api.openalex.org/"
CR = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UA = {"User-Agent": "NazarRadar/1.0 (research monitor)"}
RUN_SECONDS = 900          # one sector's read; the next run resumes with the companies not read yet
REREAD_HOURS = 20          # a company read within this window waits for the next daily run

_RUNNING = _threading.Lock()
_GLOBAL = getattr(sys, "_radar_research_lock", None) or _threading.Lock()   # shared by every sector copy in the process
sys._radar_research_lock = _GLOBAL


def _day(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _get_json(url: str, params: dict, *, min_gap: float, timeout: int = 25):
    try:
        r = get(url, params=params, headers=UA, timeout=timeout, min_gap=min_gap)
    except Exception as e:  # noqa: BLE001
        log.info("research %s failed: %s", url.split("?")[0], e)
        return None, None
    if r.status_code != 200:
        return None, r
    try:
        return r.json(), r
    except ValueError:
        return None, r


# ---------------------------------------------------------------- OpenAlex (budgeted)
def _oa(path: str, params: dict):
    if time.time() < float(db.kv_get("research:openalex_until") or 0):
        return None
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params = {**params, "api_key": key}
    d, r = _get_json(OA + path, params, min_gap=0.3, timeout=20)
    if d is None and r is not None and r.status_code == 429:   # the free daily budget is spent: wait for its reset
        try:
            wait = int(r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset") or 3600)
        except ValueError:
            wait = 3600
        db.kv_set("research:openalex_until", time.time() + min(wait, 86400))
        log.info("OpenAlex budget spent; next try in %.1f h", min(wait, 86400) / 3600)
    return d


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words = sorted(((pos, w) for w, ps in inv.items() for pos in ps))
    return " ".join(w for _, w in words)[:1500]


_SUFFIX = re.compile(r"\b(Ltd|Limited|Inc|Pvt|Private|Corp|Corporation|LLC|PLC|Co)\b\.?", re.I)


def _names(b) -> list[str]:
    """The brand, the legal entity and any former name ('PRISM (formerly Oravel Stays Ltd.)' -> PRISM, Oravel Stays)."""
    raw = (settings.company(b.name) or {}).get("entity") or ""
    former = re.findall(r"\((?:formerly|earlier|previously|ex)\s+([^)]+)\)", raw, re.I)
    ent = re.sub(r"\(.*?\)", "", raw)
    out = [b.name] + [_SUFFIX.sub("", x).strip(" ,.;") for x in [ent, *former]]
    return [n for n in dict.fromkeys(out) if n and len(n) >= 3]


def _on_sector(text: str) -> bool:
    from ..relevance import relevant
    return relevant(text, None)


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _word_rx(name: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.I)


def _institution(b) -> str | None:
    """OpenAlex institution id for the company, when it publishes as one (Google, Infosys, TCS, Nvidia ...)."""
    cached = db.kv_get(f"research_inst:{b.name}")
    if cached and cached.get("checked", "") > _day(30):
        return cached.get("id")
    found, asked = None, False
    for name in _names(b):
        d = _oa("institutions", {"search": name, "filter": "type:company", "per-page": 5})
        if d is None:
            continue
        asked = True
        for inst in d.get("results", []):
            dn = _compact(inst.get("display_name") or "")
            exact = _compact(name) == dn
            # an everyday-word name (Jupiter, Slice, Block) only counts on an exact legal-entity match
            if exact or (not b.ambiguous and _compact(b.name) and dn.startswith(_compact(b.name)) and len(dn) <= len(_compact(b.name)) + 12):
                found = inst["id"].rsplit("/", 1)[-1]
                break
        if found:
            break
    if asked:   # only remember a lookup that really happened (not one skipped for budget)
        db.kv_set(f"research_inst:{b.name}", {"id": found, "checked": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    return found


def _store_oa(w: dict, brand: str, relation: str) -> bool:
    loc = w.get("primary_location") or {}
    return _store("openalex", (w.get("id") or "").rsplit("/", 1)[-1], brand, relation, title=w.get("title") or w.get("display_name") or "",
                  venue=((loc.get("source") or {}).get("display_name")) or "", published=w.get("publication_date") or "",
                  cited=w.get("cited_by_count") or 0, authors=", ".join(((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])[:4]),
                  url=w.get("doi") or loc.get("landing_page_url") or w.get("id") or "", abstract=_abstract(w.get("abstract_inverted_index")),
                  topic=((w.get("primary_topic") or {}).get("display_name")) or "")


def _store(source: str, pid: str, brand: str, relation: str, *, title: str, venue: str = "", published: str = "", cited: int = 0,
           authors: str = "", url: str = "", abstract: str = "", topic: str = "") -> bool:
    if not pid or not title:
        return False
    with db.tx() as c:
        new = c.execute("SELECT 1 FROM papers WHERE source=? AND paper_id=?", (source, pid)).fetchone() is None
        c.execute("INSERT INTO papers(source,paper_id,brand,relation,title,venue,published,cited_by,authors,url,abstract,topic,seen_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,paper_id) DO UPDATE SET cited_by=excluded.cited_by, seen_at=excluded.seen_at",
                  (source, pid[:200], brand, relation, title[:400], venue[:200], published, int(cited or 0), authors[:300], url, abstract[:1500],
                   topic[:200], db.now()))
    if new and relation == "authored" and published >= _day(45):
        db.add_event(brand, "research", "research", f"{brand} research: {title[:150]}", detail=venue, url=url,
                     data={"relation": "authored", "source": source}, dedupe_key=f"paper|{source}|{pid}")
    return new


# ---------------------------------------------------------------- Crossref (no key, no daily budget)
def _cr_date(it: dict) -> str:
    for k in ("published", "issued", "published-online", "published-print", "created"):
        parts = ((it.get(k) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y, m, d = (list(parts) + [1, 1])[:3]
            return f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
    return ""


def _cr_plain(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _cr_store(it: dict, brand: str, relation: str) -> bool:
    doi = it.get("DOI") or ""
    title = _cr_plain((it.get("title") or [""])[0])
    authors = ", ".join(" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in (it.get("author") or [])[:4])
    return _store("crossref", doi.lower(), brand, relation, title=title, venue=_cr_plain((it.get("container-title") or [""])[0]),
                  published=_cr_date(it), cited=it.get("is-referenced-by-count") or 0, authors=authors, url=f"https://doi.org/{doi}" if doi else "",
                  abstract=_cr_plain(it.get("abstract") or "")[:1500], topic=", ".join((it.get("subject") or [])[:2]))


CR_SELECT = "DOI,title,author,issued,published,container-title,is-referenced-by-count,abstract,subject"


CORP = re.compile(r"\b(pvt|private|ltd|limited|inc|corp|corporation|llc|plc|gmbh|technolog\w*|labs?|research|hospitals?|healthcare|"
                  r"health|group|company|solutions|systems|software|services|motors?|energy|power|bank|finance|financial|ventures|"
                  r"platforms?|ai|digital|electric|mobility|rooms|stays|hotels?|travels?|airlines?|airways|aviation|pharma\w*|clinics?)\b", re.I)
ACADEMIC = re.compile(r"\b(universit\w*|college|school|institute|state|ministry|government|department of|faculty|academy|council|polytechnic)\b", re.I)


def _company_affiliation(aff: str, name: str, entity: bool, bare_ok: bool = False) -> bool:
    """An affiliation names the company itself: 'MakeMyTrip Pvt Ltd, Bangalore' yes, 'Oyo State College of Health' no."""
    rx = _word_rx(name)
    styled = name[1:] != name[1:].lower()   # OYO, IndiGo, MakeMyTrip: the casing itself is the brand ("Oyo" is a Nigerian state)
    for seg in re.split(r"[;,|]", aff):
        if not rx.search(seg) or (styled and name not in seg):
            continue
        rest = rx.sub(" ", seg)
        if ACADEMIC.search(rest) and not ACADEMIC.search(name):
            continue
        mixed = styled and not name.isupper()   # IndiGo, MakeMyTrip alone are the company; "OYO" alone is often a Nigerian state
        if entity or CORP.search(rest) or CORP.search(name) or ((mixed or (bare_ok and not name.isupper())) and not rest.strip(" .-()")):
            return True
    return False


def _crossref_authored(b) -> int:
    n = 0
    names = _names(b)
    for name in names[:3]:
        if b.ambiguous and name == b.name:
            continue   # "Jupiter" in an affiliation is a planetarium; the legal entity name is safe
        if name != b.name and len(name.split()) < 2:
            continue   # a one-word legal name ("PRISM") names labs and projects everywhere
        d, _ = _get_json(CR, {"query.affiliation": name, "filter": f"from-pub-date:{_day(540)}", "rows": 60, "select": CR_SELECT,
                              "sort": "published", "order": "desc"}, min_gap=0.4, timeout=30)
        entity = name != b.name and len(name.split()) >= 2   # a one-word legal name (PRISM) is as loose as a brand name
        bare = not b.ambiguous and len(name) >= 5            # "Airbnb, San Francisco" yes; a 3-letter name alone is too often a place
        for it in ((d or {}).get("message") or {}).get("items", []):
            affs = [a.get("name") or "" for au in (it.get("author") or []) for a in (au.get("affiliation") or [])]
            if any(_company_affiliation(a, name, entity=entity, bare_ok=bare) for a in affs):
                n += _cr_store(it, b.name, "authored")
    return n


def _about_ok(title: str, text: str, b) -> bool:
    from ..relevance import relevant
    if not relevant(title, b):   # the same naming rules as news
        return False
    if b.name[1:] != b.name[1:].lower() and b.name not in text and not any(a in text for a in b.aliases):
        return False   # a styled name (IndiGo, OYO) must appear in its own casing: 'Natural Indigo Dyes' is not the airline
    return b.tier != "adjacent" or _on_sector(_word_rx(b.name).sub(" ", text))


def _crossref_about(b) -> int:
    if b.ambiguous:
        return 0
    n = 0
    d, _ = _get_json(CR, {"query.bibliographic": b.name, "filter": f"from-pub-date:{_day(730)}", "rows": 25, "select": CR_SELECT},
                     min_gap=0.4, timeout=30)
    for it in ((d or {}).get("message") or {}).get("items", []):
        title = _cr_plain((it.get("title") or [""])[0])
        if _about_ok(title, f"{title} {_cr_plain(it.get('abstract') or '')}", b):
            n += _cr_store(it, b.name, "about")
    return n


def _crossref_sector() -> int:
    """The sector's own topics, ranked by relevance (citation order would surface any loosely matching paper)."""
    n = 0
    for term in (settings.sector_news_terms or [settings.sector])[:4]:
        words = [w for w in re.findall(r"[A-Za-z]{3,}", term) if w.lower() not in ("india", "indian", "the", "and", "for")]
        d, _ = _get_json(CR, {"query.bibliographic": term, "filter": f"from-pub-date:{_day(365)},type:journal-article", "rows": 15,
                              "select": CR_SELECT}, min_gap=0.4, timeout=30)
        for it in ((d or {}).get("message") or {}).get("items", []):
            title = _cr_plain((it.get("title") or [""])[0])
            if words and sum(1 for w in words if re.search(rf"\b{re.escape(w)}", title, re.I)) >= min(2, len(words)):
                n += _cr_store(it, "Sector", "sector")
    return n


# ---------------------------------------------------------------- Europe PMC (health)
def _europepmc(b) -> int:
    n = 0
    for name in _names(b)[:2]:
        if b.ambiguous and name == b.name:
            continue
        d, _ = _get_json(EPMC, {"query": f'AFF:"{name}" AND FIRST_PDATE:[{_day(540)} TO {_day(-1)}]', "format": "json", "pageSize": 25,
                                "sort": "P_PDATE_D desc", "resultType": "core"}, min_gap=0.3, timeout=30)
        for r in ((d or {}).get("resultList") or {}).get("result", []):
            pid = r.get("doi") or r.get("pmid") or r.get("id")
            n += _store("europepmc", str(pid).lower(), b.name, "authored", title=_cr_plain(r.get("title") or ""),
                        venue=((r.get("journalInfo") or {}).get("journal") or {}).get("title") or r.get("journalTitle") or "",
                        published=r.get("firstPublicationDate") or "", cited=r.get("citedByCount") or 0, authors=(r.get("authorString") or "")[:300],
                        url=f"https://doi.org/{r['doi']}" if r.get("doi") else f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}",
                        abstract=_cr_plain(r.get("abstractText") or "")[:1500])
    return n


# ---------------------------------------------------------------- arXiv
def _arxiv(b) -> int:
    if b.ambiguous:
        return 0
    n = 0
    try:
        r = get("http://export.arxiv.org/api/query", params={"search_query": f'abs:"{b.name}" OR ti:"{b.name}"', "sortBy": "submittedDate",
                                                           "sortOrder": "descending", "max_results": 6}, headers=UA, timeout=20, min_gap=3.0)
    except Exception as e:  # noqa: BLE001
        log.info("arxiv %s failed: %s", b.name, e)
        return 0
    if r.status_code != 200:
        return 0
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return 0
    rx = _word_rx(b.name)
    for e in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip()
        summary = re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()
        if not rx.search(f"{title} {summary}") or not _about_ok(f"{title} {summary}", f"{title} {summary}", b):
            continue
        aid = e.findtext("a:id", "", ns).rsplit("/", 1)[-1]
        n += _store("arxiv", aid, b.name, "about", title=title, venue="arXiv", published=e.findtext("a:published", "", ns)[:10],
                    authors=", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:4]), url=f"https://arxiv.org/abs/{aid}",
                    abstract=summary[:1500])
    return n


# ---------------------------------------------------------------- the collector
def running() -> bool:
    return _RUNNING.locked()


def start_background() -> bool:
    if _RUNNING.locked():
        return False
    _threading.Thread(target=run, daemon=True, name="radar-research").start()
    return True


@tracked("research")
def run() -> int:
    if not _RUNNING.acquire(blocking=False):
        return 0
    try:
        db.kv_set("research:progress", {"running": True, "started": db.now(), "queued": _GLOBAL.locked()})
        with _GLOBAL:   # one sector at a time across the hub: the sources are shared
            db.kv_set("research:progress", {"running": True, "started": db.now()})
            n = _run()
        db.kv_set("research:progress", {"running": False, "finished": db.now(), "new": n})
        return n
    finally:
        _RUNNING.release()


def _run() -> int:
    from ..schema_ext import ensure
    ensure()
    n, t0 = 0, time.time()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=REREAD_HOURS)).isoformat()
    order = sorted(settings.brands, key=lambda b: db.kv_get(f"research_done:{b.name}") or "")   # longest ago first
    health = (settings.sector_key or "") == "health" or re.search(r"health|pharma|hospital|medic", settings.sector or "", re.I)
    for b in order:
        if time.time() - t0 > RUN_SECONDS:
            break
        if (db.kv_get(f"research_done:{b.name}") or "") > cutoff:
            continue
        got = 0
        for step in (_crossref_authored, _crossref_about, _arxiv) + ((_europepmc,) if health else ()):
            try:
                got += step(b)
            except Exception as e:  # noqa: BLE001
                log.info("research %s %s failed: %s", step.__name__, b.name, e)
        inst = _institution(b)
        if inst:
            d = _oa("works", {"filter": f"authorships.institutions.id:{inst},from_publication_date:{_day(365)}", "sort": "publication_date:desc",
                              "per-page": 15})
            for w in (d or {}).get("results", []):
                got += _store_oa(w, b.name, "authored")
        n += got
        db.kv_set(f"research_done:{b.name}", datetime.now(timezone.utc).isoformat())
    if (db.kv_get("research_done:@sector") or "") <= cutoff:
        try:
            n += _crossref_sector()
            db.kv_set("research_done:@sector", datetime.now(timezone.utc).isoformat())
        except Exception as e:  # noqa: BLE001
            log.info("sector research failed: %s", e)
    return n
