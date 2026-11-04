"""CSV export and email digest."""
from __future__ import annotations
import csv
import io
import logging
import smtplib
from email.message import EmailMessage
from . import db
from .config import settings

log = logging.getLogger("radar.exports")
TABLES = {
    "events": "SELECT id,created_at,brand,source,kind,importance,category,title,so_what,counter_move,url FROM events WHERE kind<>'baseline' ORDER BY id DESC",
    "products": "SELECT brand,product_id,title,product_type,price,compare_at,available,variants,published_at,created_at,first_seen,last_changed,handle FROM products ORDER BY brand,price",
    "price_history": "SELECT brand,product_id,price,compare_at,available,seen_at FROM price_history ORDER BY brand,product_id,seen_at",
    "signals": "SELECT created_at,kind,brand,importance,title,detail FROM signals ORDER BY id DESC",
    "matches": "SELECT category,self_title,self_price,brand,rival_title,rival_price,score FROM product_matches ORDER BY category,self_title",
    "reviews": "SELECT brand,product_id,rating,created,title,body FROM reviews ORDER BY created DESC",
    "videos": "SELECT brand,channel,views,published,title,video_id FROM videos ORDER BY views DESC",
    "search_suggestions": "SELECT seed,suggestion,brand,first_seen,last_seen FROM search_suggestions ORDER BY seed",
    "app_metrics": "SELECT brand,package,name,rating,ratings_count,downloads,updated_on,seen_at FROM app_metrics ORDER BY package,seen_at",
}


def csv_bytes(table: str) -> bytes:
    rows = db.q(TABLES[table])
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def email_enabled() -> bool:
    s = settings.smtp
    return all(s.get(k) for k in ("host", "user", "password", "to"))


def send_email(subject: str, body: str) -> bool:
    if not email_enabled():
        return False
    s = settings.smtp
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s.get("from") or s["user"], s["to"]
    msg.set_content(body)
    port = int(s.get("port") or 587)
    try:
        # 465 is implicit TLS (SMTP_SSL); 587/25 upgrade with STARTTLS, required unless the relay is on this machine
        with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(s["host"], port, timeout=30) as srv:
            if port != 465:
                srv.ehlo()
                if srv.has_extn("starttls") or s["host"] not in ("localhost", "127.0.0.1"):
                    srv.starttls()
                    srv.ehlo()
            srv.login(s["user"], s["password"])
            srv.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("email failed: %s", e)
        return False
