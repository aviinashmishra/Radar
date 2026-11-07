"""Search-demand radar via Google autocomplete (public, no key).

Autocomplete reflects what India is typing right now. A new suggestion such as "boat airdopes 800" appearing
under "boat airdopes" means demand for a model has started. Category seeds ("best smartwatch under") show the
price points buyers care about. Events: search_trend.
"""
from __future__ import annotations
from .. import db
from ..config import settings
from ..relevance import attribute, relevant
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


def brand_for(seed_brand: str, suggestion: str) -> str:
    """Brand seeds keep only suggestions that are about the brand ("urban cruiser" is not URBAN); sector seeds
    are attributed to whichever brand the suggestion names."""
    if seed_brand != "Sector":
        b = settings.brand(seed_brand)
        return seed_brand if b and relevant(suggestion, b, cased=False, strict=True) else ""
    return attribute(suggestion, cased=False, strict=True)


@tracked("searchdemand")
def run() -> int:
    n = 0
    seeds = []
    for s in settings.search_seeds:
        if "{brand}" in s:
            seeds += [(s.replace("{brand}", t), b.name) for b in settings.brands for t in (b.search_terms or [b.name])]
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
        first = not db.q1("SELECT 1 FROM search_suggestions WHERE seed=?", (seed,))  # a new seed is a baseline, not news
        owner = {s: brand_for(brand, s) for s in sugg}
        sugg = [s for s in sugg if owner[s]]
        with db.tx() as c:
            for s in sugg:
                c.execute("INSERT INTO search_suggestions(seed,suggestion,brand,first_seen,last_seen) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(seed,suggestion) DO UPDATE SET last_seen=excluded.last_seen",
                          (seed, s, owner[s], ts, ts))
        if first:
            continue
        for s in sugg:
            if s not in known and s.strip() != seed.strip():
                b = owner[s]
                if db.add_event(b, "google", "search_trend", f"People started searching: \"{s}\"",
                                detail=f"new autocomplete suggestion under \"{seed.strip()}\"",
                                url=f"https://www.google.com/search?q={s.replace(' ', '+')}", data={"seed": seed, "suggestion": s},
                                dedupe_key=f"suggest|{seed}|{s}"):
                    n += 1
    return n
