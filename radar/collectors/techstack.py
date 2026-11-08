"""Tech-stack and theme detection for each brand's storefront (Shopify) or, without one, its public website.

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
# signatures on any company website (fintech, travel, AI, ...), matched only against script/link URLs and inline script
# code, never page copy: a domain is evidence that the tool is loaded, a word in the text is not
WEB_APPS = {"googletagmanager.com": "GTM", "connect.facebook.net": "Meta Pixel", "clarity.ms": "MS Clarity", "static.hotjar.com": "Hotjar",
            "cdn.segment.com": "Segment", "mixpanel.com": "Mixpanel", "amplitude.com": "Amplitude", "heapanalytics.com": "Heap",
            "posthog.com": "PostHog", "optimizely.com": "Optimizely", "visualwebsiteoptimizer.com": "VWO", "launchdarkly.com": "LaunchDarkly",
            "widget.intercom.io": "Intercom", "js.intercomcdn.com": "Intercom", "js.driftt.com": "Drift", "js.hs-scripts.com": "HubSpot",
            "js.hs-analytics.net": "HubSpot", "pardot.com": "Pardot", "munchkin.marketo.net": "Marketo", "cdn.cookielaw.org": "OneTrust consent",
            "consent.cookiebot.com": "Cookiebot", "browser.sentry-cdn.com": "Sentry", "js-agent.newrelic.com": "New Relic",
            "datadoghq-browser-agent.com": "Datadog RUM", "cdn.branch.io": "Branch links", "appsflyer.com": "AppsFlyer",
            "gstatic.com/firebasejs": "Firebase", "google.com/recaptcha": "reCAPTCHA", "haptik.ai": "Haptik bot", "yellow.ai": "Yellow.ai bot",
            "verloop.io": "Verloop bot", "snap.licdn.com": "LinkedIn Insight", "googleadservices.com": "Google Ads", "doubleclick.net": "DoubleClick",
            "bat.bing.com": "Microsoft Ads", "criteo.net": "Criteo", "taboola.com": "Taboola", "outbrain.com": "Outbrain",
            "netcoresmartech.com": "Netcore", "appboycdn.com": "Braze", "braze.com": "Braze", "contentsquare.net": "Contentsquare",
            "clevertap": "CleverTap", "moengage.com": "MoEngage", "webengage.com": "WebEngage", "klaviyo.com": "Klaviyo",
            "static.zdassets.com": "Zendesk", "wchat.freshchat.com": "Freshchat", "checkout.razorpay.com": "Razorpay",
            "analytics.tiktok.com": "TikTok pixel", "gtag/js": "Google Analytics", "google-analytics.com": "Google Analytics"}
EDGE = {"cf-ray": "Cloudflare", "server:cloudflare": "Cloudflare", "akamai": "Akamai", "x-akamai": "Akamai", "fastly": "Fastly",
        "x-amz-cf-id": "CloudFront", "x-vercel": "Vercel", "server:vercel": "Vercel", "x-nf-request-id": "Netlify", "server:nginx": "nginx",
        "x-azure-ref": "Azure Front Door", "server:gws": "Google Front End", "server:google frontend": "Google Front End"}
# what the site is built on (stored in the `theme` column for non-Shopify sites); structural markers only
PLATFORMS = [("__next_data__", "Next.js"), ("/_next/static", "Next.js"), ("__nuxt__", "Nuxt"), (" ng-version=", "Angular"), ("___gatsby", "Gatsby"),
             ("/wp-content/", "WordPress"), ("/sites/default/files", "Drupal"), ("drupal-settings-json", "Drupal"), ("/etc.clientlibs/", "Adobe AEM"),
             ("framerusercontent.com", "Framer"), ("website-files.com", "Webflow"), ("static.wixstatic.com", "Wix"), ("static1.squarespace.com", "Squarespace"),
             ("data-reactroot", "React"), ("<div id=\"root\"></div>", "React SPA"), ("<div id=\"app\"></div>", "Vue/SPA"), ("mage/requirejs/mixins", "Magento")]


def _web_hay(html: str) -> str:
    """Script/link URLs plus inline <script> code: where third-party tools show up on a site."""
    urls = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', html, re.I)
    inline = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.I | re.S)
    return (" ".join(urls) + " " + " ".join(x[:4000] for x in inline if "__NEXT_DATA__" not in x[:200])).lower()


@tracked("techstack")
def run() -> int:
    n = 0
    for b in settings.brands:
        site = (b.shopify or b.website or "").rstrip("/")   # a storefront where there is one, else the company's public site
        if not site:
            continue
        try:
            r = get(site + "/", timeout=25)
        except Exception as e:  # noqa: BLE001
            log.info("techstack %s failed: %s", b.name, e)
            continue
        if r.status_code != 200:
            continue
        t = r.text
        low = t.lower()
        if b.shopify:
            apps = sorted({label for key, label in APPS.items() if label and key in low})
        else:   # a company website: tools by their script domains, the edge by the response headers
            hay = _web_hay(t)
            hdr = " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
            apps = sorted({label for key, label in WEB_APPS.items() if key in hay} | {label for key, label in EDGE.items() if key in hdr})
        shop = re.search(r'Shopify\.shop\s*=\s*"([^"]+)"', t)
        theme = re.search(r'Shopify\.theme\s*=\s*(\{[^}]+\})', t)
        theme_name = ""
        if not b.shopify:
            theme_name = next((name for key, name in PLATFORMS if key in low), "")
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
                            detail="New checkout, BNPL, CRM, analytics or support tool = a conversion or retention bet.", url=site,
                            data={"added": added, "removed": removed}, dedupe_key=f"stack|{b.name}|{'+'.join(added)}|{'-'.join(removed)}"):
                n += 1
        if theme_name and prev["theme"] and theme_name != prev["theme"]:
            if db.add_event(b.name, "techstack", "theme_change", f"{b.name} changed site platform/theme: {prev['theme']} -> {theme_name}",
                            detail="A theme or platform change usually means a redesign or a big campaign landing.", url=site,
                            data={"old": prev["theme"], "new": theme_name}, dedupe_key=f"theme|{b.name}|{theme_name}"):
                n += 1
    return n
