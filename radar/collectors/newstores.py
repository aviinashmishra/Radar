"""New-entrant detection via Certificate Transparency logs (crt.sh).

Every new HTTPS site gets a certificate, and the log is public. Searching for sector keywords in
newly issued certificate names surfaces stores before they have any press. Noisy, so events are
tagged brand="NewEntrant" and scored low until the AI layer or a human confirms them.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from .. import db
from .base import get, tracked, log

KEYWORDS = ["smartwatch", "earbuds", "wearables", "smart-ring", "audio-india"]
SKIP = re.compile(r"\*|cloudflare|amazonaws|azure|google|localhost|test|staging|dev\.", re.I)


@tracked("new_stores")
def run() -> int:
    n = 0
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    for kw in KEYWORDS:
        try:
            r = get(f"https://crt.sh/?q=%25{kw}%25&output=json&exclude=expired", timeout=40, min_gap=3)
        except Exception as e:
            log.info("crt.sh %s failed: %s", kw, e)
            continue
        if r.status_code != 200:
            continue
        try:
            rows = r.json()
        except ValueError:
            continue
        seen = set()
        for row in rows:
            name = (row.get("common_name") or "").lower()
            if not name or name in seen or SKIP.search(name) or row.get("not_before", "")[:10] < since:
                continue
            if not (name.endswith(".in") or name.endswith(".com") or name.endswith(".co")):
                continue
            seen.add(name)
            if db.add_event("NewEntrant", "crt.sh", "new_domain", f"New certificate: {name} (keyword '{kw}')",
                            detail=f"issued {row.get('not_before', '')[:10]} by {row.get('issuer_name', '')[:80]}",
                            url=f"https://{name}", data={"keyword": kw, "domain": name},
                            dedupe_key=f"crt|{name}"):
                n += 1
            if len(seen) >= 30:
                break
    return n
