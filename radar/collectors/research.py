"""Research papers, for every company in every sector (daily).

  authored   papers with an author at the company: OpenAlex resolves the company to a research institution
             (cached a month in kv research_inst:<brand>) and lists its works from the last year
  about      papers that name the company in the title or abstract (OpenAlex, last two years, most cited first)
             and arXiv preprints that name it
  sector     the most cited recent research on the sector's own topics (brand 'Sector')

OpenAlex and arXiv are free and need no key. A new paper by a company becomes a `research` event.
"""
from __future__ import annotations
import re
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from .. import db
from ..config import settings
from .base import get, tracked, log

OA = "https://api.openalex.org/"
UA = {"User-Agent": "NazarRadar/1.0 (research monitor)"}
STEP_SECONDS = 420


def _day(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _oa(path: str, params: dict):
    contact = settings.sec_contact if "@" in (settings.sec_contact or "") else ""
    if contact:
        params = {**params, "mailto": re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", contact).group(0)}
    try:
        r = get(OA + path, params=params, headers=UA, timeout=20, min_gap=0.3)
    except Exception as e:  # noqa: BLE001
        log.info("openalex %s failed: %s", path, e)
        return None
    if r.status_code != 200:
        log.info("openalex %s -> %s", path, r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words = sorted(((pos, w) for w, ps in inv.items() for pos in ps))
    return " ".join(w for _, w in words)[:1500]


def _names(b) -> list[str]:
    ent = (settings.company(b.name) or {}).get("entity") or ""
    ent = re.sub(r"\(.*?\)|\b(Ltd|Limited|Inc|Pvt|Private|Corp|Corporation|LLC|PLC|Co)\b\.?", "", ent).strip(" ,.;")
    return [n for n in dict.fromkeys([b.name, ent]) if n and len(n) >= 3]


def _on_sector(text: str) -> bool:
    from ..relevance import relevant
    return relevant(text, None)


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _institution(b) -> str | None:
    """OpenAlex institution id for the company, when it publishes as one (Google, Infosys, TCS, Nvidia ...)."""
    cached = db.kv_get(f"research_inst:{b.name}")
    if cached and cached.get("checked", "") > _day(30):
        return cached.get("id")
    found = None
    for name in _names(b):
        d = _oa("institutions", {"search": name, "filter": "type:company", "per-page": 5})
        for inst in (d or {}).get("results", []):
            dn = _compact(inst.get("display_name") or "")
            exact = _compact(name) == dn
            # an everyday-word name (Jupiter, Slice, Block) only counts on an exact legal-entity match
            if exact or (not b.ambiguous and _compact(b.name) and dn.startswith(_compact(b.name)) and len(dn) <= len(_compact(b.name)) + 12):
                found = inst["id"].rsplit("/", 1)[-1]
                break
        if found:
            break
    db.kv_set(f"research_inst:{b.name}", {"id": found, "checked": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    return found


def _store(source: str, w: dict, brand: str, relation: str) -> bool:
    pid = (w.get("id") or "").rsplit("/", 1)[-1]
    if not pid:
        return False
    loc = w.get("primary_location") or {}
    venue = ((loc.get("source") or {}).get("display_name")) or ""
    url = w.get("doi") or loc.get("landing_page_url") or w.get("id") or ""
    authors = ", ".join(((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])[:4])
    topic = ((w.get("primary_topic") or {}).get("display_name")) or ""
    with db.tx() as c:
        new = c.execute("SELECT 1 FROM papers WHERE source=? AND paper_id=?", (source, pid)).fetchone() is None
        c.execute("INSERT INTO papers(source,paper_id,brand,relation,title,venue,published,cited_by,authors,url,abstract,topic,seen_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,paper_id) DO UPDATE SET cited_by=excluded.cited_by, seen_at=excluded.seen_at",
                  (source, pid, brand, relation, (w.get("title") or w.get("display_name") or "")[:400], venue[:200], w.get("publication_date") or "",
                   int(w.get("cited_by_count") or 0), authors[:300], url, _abstract(w.get("abstract_inverted_index")), topic[:200], db.now()))
    return new


def _arxiv(b) -> int:
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
    rx = re.compile(rf"\b{re.escape(b.name)}\b", re.I)
    for e in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip()
        summary = re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()
        if not rx.search(f"{title} {summary}"):
            continue
        aid = e.findtext("a:id", "", ns).rsplit("/", 1)[-1]
        authors = ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:4])
        with db.tx() as c:
            n += c.execute("INSERT OR IGNORE INTO papers(source,paper_id,brand,relation,title,venue,published,cited_by,authors,url,abstract,topic,seen_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("arxiv", aid, b.name, "about", title[:400], "arXiv", e.findtext("a:published", "", ns)[:10], 0, authors[:300],
                            f"https://arxiv.org/abs/{aid}", summary[:1500], "", db.now())).rowcount
    return n


@tracked("research")
def run() -> int:
    from ..schema_ext import ensure
    ensure()
    n, t0 = 0, time.time()
    for b in settings.brands:
        if time.time() - t0 > STEP_SECONDS:
            break
        inst = _institution(b)
        if inst:
            d = _oa("works", {"filter": f"authorships.institutions.id:{inst},from_publication_date:{_day(365)}", "sort": "publication_date:desc",
                              "per-page": 15})
            for w in (d or {}).get("results", []):
                if _store("openalex", w, b.name, "authored"):
                    n += 1
                    if (w.get("publication_date") or "") >= _day(45):
                        db.add_event(b.name, "research", "research", f"{b.name} research: {(w.get('title') or '')[:150]}",
                                     detail=((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
                                     url=w.get("doi") or w.get("id") or "", data={"relation": "authored"},
                                     dedupe_key=f"paper|{w.get('id')}")
        if b.ambiguous:   # "Jupiter", "Slice", "Hero": a title search finds astronomy and CT scans, not the company
            continue
        rx = re.compile(rf"\b{re.escape(b.name)}\b", re.I)
        d = _oa("works", {"filter": f'title_and_abstract.search:"{b.name}",from_publication_date:{_day(730)}', "sort": "cited_by_count:desc",
                          "per-page": 8})
        for w in (d or {}).get("results", []):
            text = f"{w.get('title') or ''} {_abstract(w.get('abstract_inverted_index'))}"
            if rx.search(text) and (b.tier != "adjacent" or _on_sector(rx.sub(" ", text))):
                n += _store("openalex", w, b.name, "about")
        n += _arxiv(b)
    for term in (settings.sector_news_terms or [settings.sector])[:3]:
        if time.time() - t0 > STEP_SECONDS + 120:
            break
        d = _oa("works", {"search": term, "filter": f"from_publication_date:{_day(240)}", "sort": "cited_by_count:desc", "per-page": 8})
        for w in (d or {}).get("results", []):
            n += _store("openalex", w, "Sector", "sector")
    return n
