"""Tech-stack and theme detection for each Shopify store.

The homepage HTML names the theme (Shopify.theme) and loads the apps the brand pays for: review widgets,
checkout (GoKwik / Shopflo), BNPL (Snapmint / Simpl), CRM (CleverTap / MoEngage / Klaviyo), analytics.
A new app is a strategy decision you can see the day it ships. Events: stack_change, theme_change.
Also records the myshopify domain, which the review collector needs.
"""
from __future__ import annotations
import json
import re
from .. import db
from ..config import settings
from .base import get, tracked, log

APPS = {"judge.me": "Judge.me reviews", "loox": "Loox reviews", "yotpo": "Yotpo", "okendo": "Okendo", "stamped": "Stamped",
        "reviews.io": "Reviews.io", "rivyo": "Rivyo", "gokwik": "GoKwik checkout", "shopflo": "Shopflo checkout",
        "razorpay": "Razorpay", "snapmint": "Snapmint BNPL", "simpl": "Simpl pay-later", "klaviyo": "Klaviyo",
        "clevertap": "CleverTap", "moengage": "MoEngage", "webengage": "WebEngage", "wisepops": "Wisepops",
        "pushowl": "PushOwl", "shiprocket": "Shiprocket", "hotjar": "Hotjar", "clarity.ms": "MS Clarity",
        "googletagmanager": "GTM", "fbevents": "Meta Pixel", "tiktok": "TikTok pixel", "rebuyengine": "Rebuy upsell",
        "limespot": "LimeSpot", "nosto": "Nosto", "searchanise": "Searchanise", "boostcommerce": "Boost search",
        "globo": "Globo", "pagefly": "PageFly", "gempages": "GemPages", "shogun": "Shogun", "tidio": "Tidio chat",
        "gorgias": "Gorgias", "zendesk": "Zendesk", "freshchat": "Freshchat", "interakt": "Interakt WhatsApp",
        "wati": "WATI WhatsApp", "bitespeed": "BiteSpeed WhatsApp", "aisensy": "AiSensy", "zipy": "Zipy",
        "returnprime": "Return Prime", "wonderment": "Wonderment", "ucbrowser": "", "affirm": "Affirm", "sezzle": "Sezzle"}


@tracked("techstack")
def run() -> int:
    n = 0
    for b in settings.brands:
        if not b.shopify:
            continue
        try:
            r = get(b.shopify + "/", timeout=25)
        except Exception as e:  # noqa: BLE001
            log.info("techstack %s failed: %s", b.name, e)
            continue
        if r.status_code != 200:
            continue
        t = r.text
        low = t.lower()
        apps = sorted({label for key, label in APPS.items() if label and key in low})
        shop = re.search(r'Shopify\.shop\s*=\s*"([^"]+)"', t)
        theme = re.search(r'Shopify\.theme\s*=\s*(\{[^}]+\})', t)
        theme_name = ""
        if theme:
            try:
                td = json.loads(theme.group(1))
                theme_name = f"{td.get('schema_name') or td.get('name')} {td.get('schema_version', '')}".strip()
            except ValueError:
                theme_name = theme.group(1)[:80]
        prev = db.q1("SELECT * FROM techstack WHERE brand=?", (b.name,))
        with db.tx() as c:
            c.execute("INSERT INTO techstack(brand,shop,theme,apps,seen_at) VALUES(?,?,?,?,?) ON CONFLICT(brand) DO UPDATE SET "
                      "shop=excluded.shop, theme=excluded.theme, apps=excluded.apps, seen_at=excluded.seen_at",
                      (b.name, shop.group(1) if shop else (prev or {}).get("shop", ""), theme_name, json.dumps(apps), db.now()))
        if not prev:
            continue
        old = set(json.loads(prev["apps"] or "[]"))
        added, removed = sorted(set(apps) - old), sorted(old - set(apps))
        if added or removed:
            if db.add_event(b.name, "techstack", "stack_change",
                            f"{b.name} stack changed: " + "; ".join(([f"added {', '.join(added)}"] if added else []) +
                                                                    ([f"removed {', '.join(removed)}"] if removed else [])),
                            detail="New checkout, BNPL, CRM or review app = a conversion or retention bet.", url=b.shopify,
                            data={"added": added, "removed": removed}, dedupe_key=f"stack|{b.name}|{'+'.join(added)}|{'-'.join(removed)}"):
                n += 1
        if theme_name and prev["theme"] and theme_name != prev["theme"]:
            if db.add_event(b.name, "techstack", "theme_change", f"{b.name} changed store theme: {prev['theme']} -> {theme_name}",
                            detail="A theme change usually means a redesign or a big campaign landing.", url=b.shopify,
                            data={"old": prev["theme"], "new": theme_name}, dedupe_key=f"theme|{b.name}|{theme_name}"):
                n += 1
    return n
