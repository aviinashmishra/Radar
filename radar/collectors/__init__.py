"""Collectors. Each one is a function `run() -> int` returning the number of new events it produced.

Tiers (see runner.py):
  fast   - every 10 min : shopify (products, prices, stock, best-seller rank, hidden + pre-launch pages)
  medium - every 30 min : news (Google News + Bing News RSS), reddit, feeds (blogs / newsrooms / analyst RSS), youtube search,
                          search demand (autocomplete), Google Trends (trending in India now)
  slow   - daily        : page watch, tech stack, reviews (Judge.me), app stores (Google Play + Apple App Store), screenshots
                          (visual diff), meta ads, new-store detection (crt.sh), financials (share prices, SEC EDGAR, money
                          news + fact extraction), attention (Wikipedia page views)
  medium (+)            : channels (Amazon.in / Flipkart listings, prices, ratings), sales (Pebble's own Shopify orders)
  slow (+)              : qcommerce (Blinkit shelves per city + retail presence from news), voice (YouTube comments,
                          App Store / Play / Amazon reviews), regions (Google Trends by state, place mentions)
"""
from . import (shopify, watchlist, news, reddit, feeds, youtube, searchdemand, trends, pagewatch, techstack, reviews, appstore,  # noqa: F401
               screenshots, ads, newstores, financials, attention, channels, sales, qcommerce, voice, regions)

FAST = [shopify]
MEDIUM = [watchlist, news, reddit, feeds, youtube, searchdemand, trends, channels, sales]
SLOW = [pagewatch, techstack, reviews, appstore, screenshots, ads, newstores, financials, attention, qcommerce, voice, regions]
ALL = FAST + MEDIUM + SLOW
