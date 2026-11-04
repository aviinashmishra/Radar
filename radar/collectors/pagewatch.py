"""Page watcher: snapshot the visible text of any public page and diff it.

Automatically watches every brand's homepage (banner / hero / offer changes) plus the `watch_pages`
from config (careers, press, offers). A careers page gaining "Category Head - Audio" is a strategy signal.
"""
from __future__ import annotations
import difflib
from .. import db
from ..config import settings
from .base import get, clean_text, sha, tracked, log

HIRING_WORDS = ("hiring", "job", "career", "opening", "role", "position", "engineer", "manager", "head of", "lead")


def _targets() -> list[tuple[str, str]]:
    out = []
    for b in settings.brands:
        if b.shopify:
            out.append((b.name, b.shopify + "/"))
        for u in b.watch_pages:
            out.append((b.name, u))
    return out


def _significant_additions(old: str, new: str) -> list[str]:
    """Sentences that exist in the new text but not the old."""
    o = set(s.strip() for s in old.replace("|", ".").split(". ") if len(s.strip()) > 20)
    added = []
    for s in new.replace("|", ".").split(". "):
        s = s.strip()
        if len(s) > 20 and s not in o:
            added.append(s)
    return added


@tracked("pagewatch")
def run() -> int:
    n = 0
    for brand, url in _targets():
        try:
            r = get(url, timeout=25)
        except Exception as e:
            log.info("pagewatch %s failed: %s", url, e)
            continue
        if r.status_code != 200:
            continue
        text = clean_text(r.text)[:60000]
        h = sha(text)
        prev = db.q1("SELECT text_hash, text FROM page_snapshots WHERE url=?", (url,))
        with db.tx() as c:
            c.execute("INSERT INTO page_snapshots(url,brand,text_hash,text,captured_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(url) DO UPDATE SET text_hash=excluded.text_hash, text=excluded.text, captured_at=excluded.captured_at",
                      (url, brand, h, text, db.now()))
        if not prev or prev["text_hash"] == h:
            continue
        added = _significant_additions(prev["text"] or "", text)
        if not added:
            continue
        ratio = difflib.SequenceMatcher(None, prev["text"][:20000], text[:20000]).quick_ratio()
        is_home = url.rstrip("/") == (settings.brand(brand).shopify or "").rstrip("/")
        joined = " | ".join(added[:8])[:1500]
        hiring = any(w in joined.lower() for w in HIRING_WORDS) and not is_home
        kind = "hiring" if hiring else ("homepage_change" if is_home else "page_change")
        if db.add_event(brand, "pagewatch", kind,
                        f"{'Homepage' if is_home else 'Page'} changed ({(1 - ratio) * 100:.0f}% different): {added[0][:120]}",
                        detail=joined, url=url, data={"added": added[:20], "similarity": round(ratio, 3)},
                        dedupe_key=f"pagewatch|{url}|{h}"):
            n += 1
    return n
