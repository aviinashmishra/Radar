"""Two-way Telegram bot. Alerts already go out; this lets you talk back from your phone.

Commands:  /ask <question>   Ask the Radar
           /top [days]       highest-importance events
           /brand <name>     a brand's latest moves
           /signals          open signals
           /brief            write the weekly brief now
           /run [tier]       trigger a sweep
           /status           what is running
Runs as a long-polling thread inside the local server when TELEGRAM_BOT_TOKEN is set. On Vercel there is no
thread to poll with, so Telegram calls /api/telegram/webhook instead (POST /api/telegram/connect sets that up).
"""
from __future__ import annotations
import logging
import threading
import time
import requests
from . import db, chat, brief, runner, cloud
from .config import settings

log = logging.getLogger("radar.bot")


def handle(text: str) -> str:
    text = (text or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    if cmd in ("/start", "/help"):
        return ("Pebble Radar bot.\n/ask <question>\n/top [days]\n/brand <name>\n/news [brand]\n/money [brand]\n/reviews [brand]\n/signals\n/brief\n"
                "/report [daily|weekly|monthly|yearly|sector]\n/run [fast|medium|slow|all]\n/status\nOr just type a question.")
    if cmd == "/news":
        from . import newsintel
        st = newsintel.stories(7, arg.strip() or None, limit=8)
        return "\n\n".join(f"[{s['importance']}] {s['brand']} · {s['topic']} · {s['n_sources']} outlet{'s' if s['n_sources'] != 1 else ''}\n"
                           f"{s['title'][:110]}\n{s['sources'][0]['url'] if s['sources'] else ''}" for s in st) or "No stories this week."
    if cmd == "/reviews":
        from . import voice
        if arg.strip():
            v = voice.brand_view(arg.strip(), 90)
            if not v["summary"]:
                return f"No comments or reviews for '{arg.strip()}' yet."
            b = v["summary"]
            qs = "\n".join(f"“{q['text'][:140]}” ({q['source']})" for q in (b["quotes"]["neg"][:2] + b["quotes"]["pos"][:1]))
            return f"{v['conclusion']}\n\n{qs}"
        m = voice.market(90)
        top = "\n".join(f"{r['brand']}: {r['composite'] or '–'}★ composite · {r['voice_items']} items · {r['neg_pct'] if r['neg_pct'] is not None else '–'}% negative"
                        for r in m["scoreboard"][:10])
        return f"{m['conclusion']}\n\nScoreboard:\n{top}"
    if cmd == "/money":
        from . import finance
        if arg.strip():
            p = finance.profile(arg.strip())
            if not (p["revenue"] or p["quote"] or p["valuation"]):
                return f"Nothing on file for '{arg.strip()}'."
            r, q, pr = p["revenue"], p["quote"], p["profit"]
            lines = [f"{p['brand']} · {p['entity'] or ''} ({p['ownership'] or 'private'})"]
            if r:
                lines.append(f"Revenue {finance.fmt_money(r['value'], r['currency'], r['unit'])} {r['period']} ({r['confidence']})"
                             + (f", {p['revenue_growth_pct']:+.1f}% YoY" if p["revenue_growth_pct"] is not None else ""))
            if pr:
                lines.append(f"Net result {finance.fmt_money(pr['signed'], pr['currency'], pr['unit'])} {pr['period']}")
            if p["valuation"]:
                lines.append(f"Valuation {finance.fmt_money(p['valuation']['value'], p['valuation']['currency'], p['valuation']['unit'])} ({p['valuation']['period']})")
            if q:
                lines.append(f"Share {q['ticker']} {q['close']:,.2f} {q['currency']} ({q['change_pct']:+.1f}% 1d, {q['change_30d_pct']:+.1f}% 30d)" if q.get("change_30d_pct") is not None
                             else f"Share {q['ticker']} {q['close']:,.2f} {q['currency']} ({q['change_pct']:+.1f}% 1d)")
            if p["scale_vs_pebble"]:
                lines.append(f"{p['scale_vs_pebble']}× Pebble's revenue ({p['scale_basis']})")
            return "\n".join(lines)
        rows = finance.peers()["brands"]
        out = []
        for p in rows[:14]:
            r = p["revenue"]
            if not r:
                continue
            out.append(f"{p['brand']}: {finance.fmt_money(r['value'], r['currency'], r['unit'])} {r['period']}"
                       + (f" {p['revenue_growth_pct']:+.0f}%" if p["revenue_growth_pct"] is not None else "")
                       + (f" · {p['scale_vs_pebble']}× Pebble" if p["scale_vs_pebble"] else " · group figure" if p["scale_basis"] == "parent" else ""))
        return "\n".join(out) or "No figures on file yet."
    if cmd == "/report":
        from . import reports
        kind = arg.strip() or "weekly"
        if kind not in reports.KINDS:
            return "kind must be daily, weekly, monthly, yearly or sector"
        r = reports.build(kind, push=False)
        return r["body"][:3800]
    if cmd == "/top":
        days = int(arg) if arg.strip().isdigit() else 7
        rows = db.q("SELECT importance,brand,kind,title FROM events WHERE created_at>=datetime('now', ?) AND kind<>'baseline' "
                    "ORDER BY importance DESC, id DESC LIMIT 10", (f"-{days} days",))
        return "\n".join(f"[{r['importance']}] {r['brand']} {r['kind']}: {r['title'][:90]}" for r in rows) or "Nothing yet."
    if cmd == "/brand":
        if not arg.strip():
            return "Usage: /brand <name>, e.g. /brand boAt"
        rows = db.q("SELECT importance,kind,title,created_at FROM events WHERE brand LIKE ? AND kind<>'baseline' ORDER BY id DESC LIMIT 10", (f"%{arg.strip()}%",))
        return "\n".join(f"{r['created_at'][5:10]} [{r['importance']}] {r['kind']}: {r['title'][:90]}" for r in rows) or f"No events for '{arg}'."
    if cmd == "/signals":
        rows = db.q("SELECT importance,title FROM signals ORDER BY id DESC LIMIT 10")
        return "\n".join(f"[{r['importance']}] {r['title']}" for r in rows) or "No signals yet."
    if cmd == "/brief":
        return brief.write(days=7, push=False)
    if cmd == "/run":
        tier = arg.strip() or "fast"
        if tier not in ("fast", "medium", "slow", "all"):
            return "tier must be fast, medium, slow or all"
        if cloud.HOSTED:
            return cloud.dispatch(tier)["message"]
        if runner.busy():
            return "A sweep is already running; try again when it finishes (/status)."
        threading.Thread(target=runner.run_tier, args=(tier,), daemon=True).start()
        return f"Started {tier} sweep."
    if cmd == "/status":
        lr = {t: db.kv_get(f"last_run:{t}") for t in ("fast", "medium", "slow")}
        return "\n".join(f"{t}: {v['at'][:16] if v else 'never'} ({v['seconds'] if v else 0}s)" for t, v in lr.items()) + \
               f"\nevents: {db.q1('SELECT COUNT(*) c FROM events')['c']}  products: {db.q1('SELECT COUNT(*) c FROM products')['c']}" + \
               (f"\ncloud snapshot: {(cloud.info() or {}).get('published_at') or 'none yet'}" if cloud.HOSTED else "")
    q = arg if cmd == "/ask" else text
    return chat.ask(q)["answer"] if q else "Ask me something."


def _send(chat_id: str, text: str) -> None:
    for i in range(0, len(text), 3900):
        requests.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                      json={"chat_id": chat_id, "text": text[i:i + 3900], "disable_web_page_preview": True}, timeout=15)


def reply(chat_id: str, text: str) -> None:
    """Answer one message (shared by the polling loop and the cloud webhook)."""
    try:
        _send(chat_id, handle(text))
    except Exception as e:  # noqa: BLE001
        _send(chat_id, f"error: {e}")


def loop(stop: threading.Event) -> None:
    if not settings.telegram_token:
        return
    offset = db.kv_get("tg_offset", 0)
    log.info("telegram bot polling")
    while not stop.is_set():
        try:
            r = requests.get(f"https://api.telegram.org/bot{settings.telegram_token}/getUpdates",
                             params={"timeout": 25, "offset": offset + 1}, timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                db.kv_set("tg_offset", offset)
                msg = u.get("message") or {}
                cid = str(msg.get("chat", {}).get("id", ""))
                if settings.telegram_chat and cid != str(settings.telegram_chat):
                    continue  # only answer the configured chat
                if msg.get("text"):
                    reply(cid, msg["text"])
        except Exception as e:  # noqa: BLE001
            log.info("telegram poll error: %s", e)
            time.sleep(10)
