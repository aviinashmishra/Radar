"""Page watcher: snapshot the visible text of any public page and diff it.

Automatically watches every brand's homepage (banner / hero / offer changes) plus the `watch_pages`
from config (careers, press, offers). A careers page gaining "Category Head - Audio" is a strategy signal.
"""
from __future__ import annotations
import re
from .. import db
from ..config import settings
from .base import get, clean_text, sha, tracked, log

HIRING_WORDS = ("hiring", "job", "career", "opening", "role", "position", "engineer", "manager", "head of", "lead")
# Shopify product tiles ("Regular price 1,549 Sale price 999") rotate on every homepage every day; they are the
# catalogue sensor's job, not copy changes.
TILE_RX = re.compile(r"regular price|sale price|unit price|add to cart|sold out|quick view|choose options|^\d[\d,.]* reviews?\b", re.I)
MIN_HOME_SHARE = 0.10  # a homepage counts as changed when at least 10% of its sentences are new copy


def _targets() -> list[tuple[str, str]]:
    out = []
    for b in settings.brands:
        if b.shopify:
            out.append((b.name, b.shopify + "/"))
        for u in b.watch_pages:
            out.append((b.name, u))
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in text.replace("|", ".").split(". ") if len(s.strip()) > 20 and not TILE_RX.search(s)]


def _significant_additions(old: str, new: str) -> list[str]:
    """Sentences that exist in the new text but not the old (product tiles excluded)."""
    o = set(_sentences(old))
    return [s for s in _sentences(new) if s not in o]


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
        share = len(added) / max(1, len(_sentences(text)))
        is_home = url.rstrip("/") == (settings.brand(brand).shopify or "").rstrip("/")
        if is_home and share < MIN_HOME_SHARE:
            continue  # a rotated banner line or two, not a new message
        joined = " | ".join(added[:8])[:1500]
        hiring = any(w in joined.lower() for w in HIRING_WORDS) and not is_home
        kind = "hiring" if hiring else ("homepage_change" if is_home else "page_change")
        if db.add_event(brand, "pagewatch", kind,
                        f"{'Homepage' if is_home else 'Page'} changed ({share * 100:.0f}% new copy): {added[0][:120]}",
                        detail=joined, url=url, data={"added": added[:20], "new_share": round(share, 3)},
                        dedupe_key=f"pagewatch|{url}|{h}"):
            n += 1
    return n
