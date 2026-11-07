"""Daily half of the distribution sensor: quick-commerce shelves per city (Blinkit) and retail presence evidence
from the text streams (Croma, Reliance Digital, Zepto, Instamart, Vijay Sales ... named next to a brand in news,
press, Reddit or video titles). See collectors/channels.py for the marketplace half that runs with the medium tier.
"""
from __future__ import annotations
from .base import tracked
from . import channels


@tracked("qcommerce")
def run() -> int:
    n = channels.run_quick_commerce()
    channels.scan_mentions()
    return n
