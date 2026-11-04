"""Search-demand radar via Google autocomplete (public, no key).

Autocomplete reflects what India is typing right now. A new suggestion such as "boat airdopes 800" appearing
under "boat airdopes" means demand for a model has started. Category seeds ("best smartwatch under") show the
price points buyers care about. Events: search_trend.
"""
from __future__ import annotations
from .. import db
from ..config import settings
from .base import get, tracked, log

URL = "https://suggestqueries.google.com/complete/search"


def suggest(q: str) -> list[str]:
    r = get(URL, params={"client": "firefox", "q": q, "hl": "en", "gl": "in"}, timeout=15, min_gap=0.7)
    if r.status_code != 200:
        return []
    try:
        return [s for s in r.json()[1] if isinstance(s, str)]
    except (ValueError, IndexError):
        return []


def _brand_for(text: str) -> str:
    t = text.lower()
    for b in settings.brands:
        if b.name.lower() in t:
            return b.name
    return "Sector"


@tracked("searchdemand")
def run() -> int:
    n = 0
    first = db.q1("SELECT COUNT(*) c FROM search_suggestions")["c"] == 0
    seeds = []
    for s in settings.search_seeds:
        if "{brand}" in s:
            seeds += [(s.replace("{brand}", b.name), b.name) for b in settings.brands]
        else:
            seeds.append((s, "Sector"))
    ts = db.now()
    for seed, brand in seeds:
        try:
            sugg = suggest(seed)
        except Exception as e:  # noqa: BLE001
            log.info("suggest %s failed: %s", seed, e)
            continue
        known = {r["suggestion"] for r in db.q("SELECT suggestion FROM search_suggestions WHERE seed=?", (seed,))}
        with db.tx() as c:
            for s in sugg:
                c.execute("INSERT INTO search_suggestions(seed,suggestion,brand,first_seen,last_seen) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(seed,suggestion) DO UPDATE SET last_seen=excluded.last_seen",
                          (seed, s, brand if brand != "Sector" else _brand_for(s), ts, ts))
        if first:
            continue
        for s in sugg:
            if s not in known and s.strip() != seed.strip():
                b = brand if brand != "Sector" else _brand_for(s)
                if db.add_event(b, "google", "search_trend", f"People started searching: \"{s}\"",
                                detail=f"new autocomplete suggestion under \"{seed.strip()}\"",
                                url=f"https://www.google.com/search?q={s.replace(' ', '+')}", data={"seed": seed, "suggestion": s},
                                dedupe_key=f"suggest|{seed}|{s}"):
                    n += 1
    return n
