"""Push alerts: Telegram, generic webhook (Discord/Slack), and always the console + DB."""
from __future__ import annotations
import logging
import requests
from . import db
from .config import settings

log = logging.getLogger("radar.alerts")
ICON = {"new_product": "🆕", "price_drop": "🔻", "price_rise": "🔺", "sale_started": "🔥", "stock_out": "📦", "restock": "📦",
        "hidden_product": "🕵️", "news": "📰", "social": "💬", "hiring": "🧑‍💼", "homepage_change": "🖼️", "new_collection": "🗂️",
        "bestseller_move": "🏆", "new_ad": "📣", "new_domain": "🌱", "price_war": "⚔️", "stockout_opportunity": "🎯",
        "launch_predictor": "🚀", "momentum": "📈", "store_unreachable": "🚧"}


def _send(text: str) -> None:
    if settings.telegram_token and settings.telegram_chat:
        try:
            requests.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                          json={"chat_id": settings.telegram_chat, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("telegram failed: %s", e)
    if settings.webhook_url:
        try:
            body = {"content": text[:1900]} if "discord" in settings.webhook_url else {"text": text[:3000]}
            requests.post(settings.webhook_url, json=body, timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("webhook failed: %s", e)


def format_event(e: dict) -> str:
    return (f"{ICON.get(e['kind'], '•')} [{e['importance']}/10] {e['brand']} · {e['kind'].replace('_', ' ')}\n"
            f"{e['title']}\n"
            + (f"Why: {e['so_what']}\n" if e.get("so_what") else "")
            + (f"Move: {e['counter_move']}\n" if e.get("counter_move") and e['counter_move'] != "No action" else "")
            + (f"{e['url']}" if e.get("url") else "")).strip()


def flush(threshold: int | None = None) -> int:
    """Alert every enriched, un-alerted event at or above the threshold, plus every new signal."""
    thr = threshold if threshold is not None else settings.alert_threshold
    sent = 0
    events = db.q("SELECT * FROM events WHERE enriched>0 AND alerted=0 AND importance>=? ORDER BY importance DESC, id LIMIT 40", (thr,))
    for e in events:
        msg = format_event(e)
        log.info("ALERT %s", msg.replace("\n", " | "))
        _send(msg)
        with db.tx() as c:
            c.execute("UPDATE events SET alerted=1 WHERE id=?", (e["id"],))
        sent += 1
    # everything below threshold is marked as handled so it never re-triggers
    with db.tx() as c:
        c.execute("UPDATE events SET alerted=-1 WHERE enriched>0 AND alerted=0 AND importance<?", (thr,))
    for s in db.q("SELECT * FROM signals WHERE alerted=0 ORDER BY id LIMIT 20"):
        msg = f"{ICON.get(s['kind'], '•')} SIGNAL [{s['importance']}/10] {s['title']}\n{s['detail'][:600]}"
        log.info("SIGNAL %s", msg.replace("\n", " | "))
        _send(msg)
        with db.tx() as c:
            c.execute("UPDATE signals SET alerted=1 WHERE id=?", (s["id"],))
        sent += 1
    return sent


def send_text(text: str) -> None:
    _send(text)
