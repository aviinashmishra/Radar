"""Meta Ad Library (Facebook + Instagram ads). Official API, needs META_ACCESS_TOKEN.

Without a token this collector is a silent no-op. With one it records every new creative per brand and
keeps a running "days live" per ad, which feeds the winner detector (ads alive 30+ days are paying off).
"""
from __future__ import annotations
from datetime import datetime, timezone
from .. import db
from ..config import settings
from .base import get_json, tracked, log

API = "https://graph.facebook.com/v19.0/ads_archive"
FIELDS = "id,page_name,ad_creative_bodies,ad_creative_link_titles,ad_delivery_start_time,ad_snapshot_url,publisher_platforms"


@tracked("meta_ads")
def run() -> int:
    if not settings.meta_token:
        return 0
    n = 0
    for b in settings.brands:
        params = {"search_terms": b.name, "ad_reached_countries": "['IN']", "ad_type": "ALL", "ad_active_status": "ACTIVE",
                  "fields": FIELDS, "limit": 100, "access_token": settings.meta_token}
        data = get_json(API, params=params, timeout=30)
        if not data or "data" not in data:
            log.info("meta ads: no data for %s", b.name)
            continue
        live = []
        for ad in data["data"]:
            start = ad.get("ad_delivery_start_time", "")
            days = 0
            if start:
                try:
                    days = (datetime.now(timezone.utc) - datetime.fromisoformat(start.replace("Z", "+00:00"))).days
                except ValueError:
                    pass
            live.append({"id": ad["id"], "days": days, "body": (ad.get("ad_creative_bodies") or [""])[0][:300]})
            body = (ad.get("ad_creative_bodies") or [""])[0]
            if db.add_event(b.name, "meta_ads", "new_ad", f"New ad by {ad.get('page_name', b.name)}: {body[:140]}",
                            detail=f"since {start[:10]} | platforms={ad.get('publisher_platforms')}",
                            url=ad.get("ad_snapshot_url", ""), data={"ad_id": ad["id"], "start": start},
                            dedupe_key=f"meta_ads|{ad['id']}"):
                n += 1
        db.kv_set(f"ads_live:{b.name}", {"count": len(live), "long_running": [a for a in live if a["days"] >= 30][:20],
                                        "updated": db.now()})
    return n
