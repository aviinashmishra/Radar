"""Collectors. Each one is a function `run() -> int` returning the number of new events it produced.

Tiers (see runner.py):
  fast   - every 10 min : shopify (products, prices, stock, best-seller rank, hidden + pre-launch pages)
  medium - every 30 min : news (Google News RSS), reddit, feeds (blogs/atom), youtube search, search demand (autocomplete)
  slow   - daily        : page watch, tech stack, reviews (Judge.me), app store (Google Play), screenshots (visual diff),
                          meta ads, new-store detection (crt.sh)
"""
from . import shopify, watchlist, news, reddit, feeds, youtube, searchdemand, pagewatch, techstack, reviews, appstore, screenshots, ads, newstores  # noqa: F401

FAST = [shopify]
MEDIUM = [watchlist, news, reddit, feeds, youtube, searchdemand]
SLOW = [pagewatch, techstack, reviews, appstore, screenshots, ads, newstores]
ALL = FAST + MEDIUM + SLOW
