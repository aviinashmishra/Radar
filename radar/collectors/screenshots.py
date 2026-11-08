"""Visual diff: daily homepage screenshots, pixel-compared with yesterday's.

Text diffing misses a new hero banner, a sale badge or a redesigned layout. Playwright renders the page,
Pillow measures how many pixels changed, and when Claude or Gemini is configured it describes what changed in words.
Events: visual_change. Images are served by the dashboard at /shots/<file>.
"""
from __future__ import annotations
import re
import base64
from pathlib import Path
from .. import db
from ..config import settings
from .base import Unavailable, tracked, log

SHOTS = settings.db_path.parent / "shots"  # next to the DB, so Docker's /data volume keeps them
THRESHOLD_PCT = 12.0


def _capture(url: str, path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("playwright not installed; screenshots skipped")
        return False
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - browser binary missing (fresh Docker image, CI runner)
            raise Unavailable("Chromium not installed; run: python -m playwright install chromium") from e
        page = browser.new_page(viewport={"width": 1280, "height": 2000})
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(path), type="jpeg", quality=72, full_page=False)  # ~200 KB vs ~1.4 MB as PNG
            return True
        except Exception as e:  # noqa: BLE001
            log.info("screenshot %s failed: %s", url, e)
            return False
        finally:
            browser.close()


def _mime(p: Path) -> str:
    return "image/png" if p.suffix.lower() == ".png" else "image/jpeg"


def _diff_pct(a: Path, b: Path) -> float:
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 40 else 0)
    hist = diff.histogram()
    return round(100.0 * hist[255] / (ia.size[0] * ia.size[1]), 2)


def _describe(old: Path, new: Path, brand: str) -> str | None:
    """Ask the AI provider what changed between the two screenshots (only when AI is configured)."""
    prompt = (f"These are yesterday's and today's homepage of {brand}, an Indian electronics brand. In 2-3 sentences, what changed "
              f"(new hero product, offer, sale badge, layout, pricing)? Mention any product names or prices visible. "
              f"If nothing meaningful changed, say so.")
    if settings.ai_provider == "gemini":
        from .. import gemini
        return gemini.vision(prompt, [old.read_bytes(), new.read_bytes()], mime=_mime(new))
    from ..enrich import _client_or_none
    client = _client_or_none()
    if client is None:
        return None
    imgs = []
    for pth in (old, new):
        imgs.append({"type": "image", "source": {"type": "base64", "media_type": _mime(pth),
                                                  "data": base64.standard_b64encode(pth.read_bytes()).decode()}})
    try:
        resp = client.messages.create(
            model=settings.model, max_tokens=600, output_config={"effort": "low"},
            messages=[{"role": "user", "content": imgs + [{"type": "text", "text": prompt}]}])
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:  # noqa: BLE001
        log.info("vision describe failed: %s", e)
        return None


@tracked("screenshots")
def run() -> int:
    if not settings.screenshots:
        return 0
    SHOTS.mkdir(parents=True, exist_ok=True)
    n = 0
    day = db.now()[:10]
    for b in settings.brands:
        site = (b.shopify or b.website or "").rstrip("/")   # storefront, else the public company site
        if not site:
            continue
        safe = re.sub(r"[^a-z0-9]+", "-", b.name.lower()).strip("-")
        path = SHOTS / f"{safe}-{day}.jpg"
        if path.exists() or path.with_suffix(".png").exists():
            continue  # one per day
        if not _capture(site + "/", path):
            continue
        prev = db.q1("SELECT path FROM screenshots WHERE brand=? ORDER BY id DESC LIMIT 1", (b.name,))
        pct = None
        if prev and Path(prev["path"]).exists():
            try:
                pct = _diff_pct(Path(prev["path"]), path)
            except Exception as e:  # noqa: BLE001
                log.info("diff failed: %s", e)
        with db.tx() as c:
            c.execute("INSERT INTO screenshots(brand,url,path,diff_pct,taken_at) VALUES(?,?,?,?,?)", (b.name, site, str(path), pct, db.now()))
        if pct is not None and pct >= THRESHOLD_PCT:
            desc = _describe(Path(prev["path"]), path, b.name) or f"{pct:.0f}% of the homepage pixels changed since the last capture."
            if db.add_event(b.name, "visual", "visual_change", f"{b.name} homepage looks {pct:.0f}% different today",
                            detail=desc, url=site,
                            data={"diff_pct": pct, "before": f"/shots/{Path(prev['path']).name}", "after": f"/shots/{path.name}"},
                            dedupe_key=f"visual|{b.name}|{day}"):
                n += 1
    return n
