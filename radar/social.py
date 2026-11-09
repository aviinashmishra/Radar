"""Read side of the social, web and research layer (collectors/social.py, collectors/research.py) for the dashboard.

overview   platform status, share of voice per company per platform, the most engaged posts, sentiment
posts      the stream across platforms, filterable
threads    conversations with their comment trees (replies nested under the comment they answer)
accounts   every company's own accounts and audiences
research   papers by and about each company, and the sector's research
reply      a reply draft for one comment, written for the current seat (AI when a key is set, else a template)
"""
from __future__ import annotations
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings
from .collectors.social import PLATFORM_LABEL

ENGAGE = "(likes + 2*replies + 3*reposts + views/100.0)"   # one comparable number across platforms


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _ensure() -> None:
    from .schema_ext import ensure
    ensure()


def _hosted() -> bool:
    import os
    return bool(os.environ.get("VERCEL")) or os.environ.get("RADAR_HOSTED") == "1"


def kick(force: bool = False) -> dict:
    """Make sure this sector's social read is happening: a page that finds nothing (or data older than 3 hours) starts it
    in the background at once instead of waiting for the sweep queue. Returns the live progress for the page."""
    from .collectors import social as col
    prog = db.kv_get("social:progress") or {}
    if not _hosted():
        last = (db.kv_get("social:status:bluesky") or {}).get("at") or ""
        empty = not db.q1("SELECT 1 FROM social_posts LIMIT 1")
        stale = last < (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
        if (force or empty or stale) and not col.running():
            col.start_background()
            prog = {"running": True, "step": "starting", "done": []}
    prog["running"] = bool(prog.get("running")) and (col.running() or _hosted())
    prog["hosted"] = _hosted()
    return prog


def research_kick(force: bool = False) -> dict:
    from .collectors import research as col
    prog = db.kv_get("research:progress") or {}
    if not _hosted() and (force or not db.q1("SELECT 1 FROM papers LIMIT 1")) and not col.running():
        col.start_background()
        prog = {"running": True}
    prog["running"] = bool(prog.get("running")) and col.running()
    prog["hosted"] = _hosted()
    return prog


def progress() -> dict:
    """Cheap poll for the page while a read runs: which platforms are done, how many posts so far."""
    _ensure()
    from .collectors import social as col
    prog = db.kv_get("social:progress") or {}
    prog["running"] = bool(prog.get("running")) and col.running()
    prog["posts"] = db.q1("SELECT COUNT(*) n FROM social_posts")["n"]
    prog["comments"] = db.q1("SELECT COUNT(*) n FROM social_comments")["n"]
    from .collectors import research as rcol
    prog["research"] = {"running": rcol.running(), "papers": db.q1("SELECT COUNT(*) n FROM papers")["n"]}
    return prog


def platform_status() -> list[dict]:
    """One row per platform: is it live in this sector, what it needs if not, when it last ran."""
    _ensure()
    posts = {r["platform"]: r for r in db.q("SELECT platform, COUNT(*) n, MAX(seen_at) last FROM social_posts GROUP BY platform")}
    comments = {r["platform"]: r["n"] for r in db.q("SELECT platform, COUNT(*) n FROM social_comments GROUP BY platform")}
    accts = defaultdict(lambda: defaultdict(int))
    for r in db.q("SELECT platform, status, COUNT(*) n FROM social_accounts GROUP BY platform, status"):
        accts[r["platform"]][r["status"]] = r["n"]
    step_of = {"x": "x", "bluesky": "bluesky", "hn": "hn", "reddit": "reddit", "lemmy": "lemmy", "mastodon": "mastodon", "web": "web",
               "producthunt": "web", "youtube": "shorts", "whatsapp": "owned", "substack": "owned", "telegram": "owned",
               "instagram": "meta", "facebook": "meta", "linkedin": "owned", "threads": "owned"}
    out = []
    for p, label in PLATFORM_LABEL.items():
        st = db.kv_get(f"social:status:{step_of.get(p, p)}") or {}
        a = accts.get(p, {})
        n_posts = (posts.get(p) or {}).get("n", 0) if posts.get(p) else 0
        if a.get("needs_token"):
            state, why = "needs_token", "sign-in wall: add a Meta Graph API token to read it"
        elif n_posts or a.get("live"):
            state, why = "live", ""
        elif p in ("linkedin", "threads") and a.get("found"):
            state, why = "links", "no public feed: profiles linked from each company"
        elif a.get("unreachable"):
            state, why = "blocked", "the platform refused anonymous reads from this network"
        elif st.get("ok") is False:
            state, why = "error", st.get("error", "")
        elif not st:
            state, why = "pending", "first read runs with the next sweep"
        else:
            state, why = "quiet", "read, nothing about these companies yet"
        out.append({"platform": p, "label": label, "state": state, "why": why, "posts": n_posts, "comments": comments.get(p, 0),
                    "accounts": sum(a.values()), "last": (posts.get(p) or {}).get("last") if posts.get(p) else st.get("at")})
    return out


def overview(days: int = 30) -> dict:
    _ensure()
    since = _since(days)
    me = _me()
    rows = db.q(f"SELECT brand, platform, COUNT(*) n, SUM({ENGAGE}) eng, AVG(sentiment) s FROM social_posts WHERE created>=? AND brand NOT IN ('Sector') "
                "GROUP BY brand, platform", (since,))
    by_brand: dict[str, dict] = {}
    for r in rows:
        b = by_brand.setdefault(r["brand"], {"brand": r["brand"], "posts": 0, "engagement": 0.0, "platforms": {}, "sent": [], "self": r["brand"] == me})
        b["posts"] += r["n"]
        b["engagement"] += r["eng"] or 0
        b["platforms"][r["platform"]] = r["n"]
        if r["s"] is not None:
            b["sent"].append((r["s"], r["n"]))
    for b in by_brand.values():
        w = sum(n for _, n in b["sent"])
        b["sentiment"] = round(sum(s * n for s, n in b["sent"]) / w, 2) if w else None
        del b["sent"]
        b["engagement"] = round(b["engagement"])
    total = sum(b["posts"] for b in by_brand.values()) or 1
    for b in by_brand.values():
        b["share_pct"] = round(b["posts"] * 100 / total, 1)
    c7 = db.q1("SELECT COUNT(*) n FROM social_comments WHERE created>=?", (_since(7),))["n"]
    p7 = db.q1("SELECT COUNT(*) n FROM social_posts WHERE created>=?", (_since(7),))["n"]
    top = db.q(f"SELECT *, {ENGAGE} eng FROM social_posts WHERE created>=? ORDER BY eng DESC LIMIT 12", (_since(14),))
    day_rows = db.q("SELECT substr(created,1,10) d, COUNT(*) n FROM social_posts WHERE created>=? GROUP BY d", (_since(14),))
    days_map = {r["d"]: r["n"] for r in day_rows}
    today = datetime.now(timezone.utc).date()
    spark = [days_map.get((today - timedelta(days=13 - i)).isoformat(), 0) for i in range(14)]
    status = platform_status()
    return {"progress": kick(), "days": days, "posts_7d": p7, "comments_7d": c7, "platforms_live": sum(1 for s in status if s["state"] == "live"),
            "accounts": db.q1("SELECT COUNT(*) n FROM social_accounts")["n"], "papers": db.q1("SELECT COUNT(*) n FROM papers")["n"],
            "spark": spark, "brands": sorted(by_brand.values(), key=lambda b: -b["posts"]), "top": top, "status": status, "self": me}


def posts(platform: str = "", brand: str = "", kind: str = "", days: int = 30, limit: int = 120, q: str = "") -> list[dict]:
    _ensure()
    sql, args = f"SELECT *, {ENGAGE} eng FROM social_posts WHERE created>=?", [_since(days)]
    for col, val in (("platform", platform), ("brand", brand), ("kind", kind)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    if q:
        sql += " AND (title LIKE ? OR text LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return db.q(sql + " ORDER BY created DESC LIMIT ?", (*args, min(limit, 400)))


def _tree(rows: list[dict], root: str) -> list[dict]:
    kids = defaultdict(list)
    for r in rows:
        kids[r["parent_id"] or root].append(r)

    def build(pid: str, depth: int) -> list[dict]:
        if depth > 12:
            return []
        out = []
        for r in sorted(kids.get(pid, []), key=lambda x: (-(x["likes"] or 0), x["created"] or "")):
            out.append({**r, "replies": build(r["comment_id"], depth + 1)})
        return out
    top = build(root, 0)
    seen = {id(x) for x in top}
    orphans = [r for r in rows if r["parent_id"] not in {x["comment_id"] for x in rows} and r["parent_id"] != root]
    return top + [{**r, "replies": build(r["comment_id"], 1)} for r in orphans if id(r) not in seen][:30]


def threads(platform: str = "", brand: str = "", days: int = 30, limit: int = 25) -> list[dict]:
    """Conversations: posts that have comments, with the full reply tree."""
    _ensure()
    sql = ("SELECT c.platform, c.post_id, COUNT(*) n, MAX(c.created) last FROM social_comments c WHERE c.created>=?"
           + (" AND c.platform=?" if platform else "") + (" AND c.brand=?" if brand else "") + " GROUP BY c.platform, c.post_id ORDER BY n DESC, last DESC LIMIT ?")
    args = [_since(days)] + ([platform] if platform else []) + ([brand] if brand else []) + [min(limit, 60)]
    out = []
    for t in db.q(sql, tuple(args)):
        p = db.q1("SELECT * FROM social_posts WHERE platform=? AND post_id=?", (t["platform"], t["post_id"])) or \
            {"platform": t["platform"], "post_id": t["post_id"], "title": "", "text": "", "url": "", "brand": ""}
        rows = db.q("SELECT * FROM social_comments WHERE platform=? AND post_id=? ORDER BY created LIMIT 400", (t["platform"], t["post_id"]))
        out.append({"post": p, "comments": len(rows), "last": t["last"], "tree": _tree(rows, t["post_id"]),
                    "sentiment": round(sum(r["sentiment"] or 0 for r in rows) / len(rows), 2) if rows else None,
                    "brand": p.get("brand") or (rows[0]["brand"] if rows else "")})
    return out


def accounts() -> dict:
    _ensure()
    rows = db.q("SELECT * FROM social_accounts ORDER BY brand, platform")
    by = defaultdict(dict)
    for r in rows:
        by[r["brand"]][r["platform"]] = r
    return {"brands": [{"brand": b.name, "self": b.name == _me(), "accounts": by.get(b.name, {})} for b in settings.brands],
            "platforms": [p for p in PLATFORM_LABEL if any(p in v for v in by.values())]}


def research(days: int = 730, brand: str = "", relation: str = "") -> dict:
    _ensure()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    sql, args = "SELECT * FROM papers WHERE published>=?", [since]
    if brand:
        sql += " AND brand=?"
        args.append(brand)
    if relation:
        sql += " AND relation=?"
        args.append(relation)
    rows = db.q(sql + " ORDER BY published DESC LIMIT 400", tuple(args))
    per = defaultdict(lambda: {"authored": 0, "about": 0, "cited": 0})
    for r in db.q("SELECT brand, relation, COUNT(*) n, SUM(cited_by) c FROM papers WHERE published>=? GROUP BY brand, relation", (since,)):
        per[r["brand"]][r["relation"] if r["relation"] in ("authored", "about") else "about"] += r["n"]
        per[r["brand"]]["cited"] += r["c"] or 0
    topics = db.q("SELECT topic, COUNT(*) n FROM papers WHERE published>=? AND topic<>'' GROUP BY topic ORDER BY n DESC LIMIT 12", (since,))
    return {"progress": research_kick(), "papers": rows, "by_brand": sorted(({"brand": k, **v} for k, v in per.items() if k != "Sector"), key=lambda x: -(x["authored"] * 3 + x["about"])),
            "topics": topics, "authored": sum(v["authored"] for v in per.values()), "about": sum(v["about"] for k, v in per.items() if k != "Sector"),
            "sector": sum(1 for r in rows if r["relation"] == "sector"), "institutions": db.q1("SELECT COUNT(*) n FROM kv WHERE k LIKE 'research_inst:%' AND v LIKE '%\"id\": \"I%'")["n"]}


def reply_draft(platform: str, comment_id: str, allow_ai: bool = True) -> dict:
    """A reply to one comment, written from the current seat. The user posts it on the platform (opened in a new tab)."""
    _ensure()
    c = db.q1("SELECT * FROM social_comments WHERE platform=? AND comment_id=?", (platform, comment_id))
    if not c:
        return {"error": "comment not found"}
    p = db.q1("SELECT * FROM social_posts WHERE platform=? AND post_id=?", (platform, c["post_id"])) or {}
    seat = _me()
    voice = f"the social team of {seat}" if seat else "an industry analyst"
    if allow_ai and settings.ai_enabled:
        from . import enrich
        prompt = (f"You are {voice}. Write one short, friendly, specific reply (max 60 words, no hashtags, no emojis unless the comment uses them) "
                  f"to this comment on {PLATFORM_LABEL.get(platform, platform)}. Thread: {(p.get('title') or p.get('text') or '')[:300]!r}. "
                  f"Comment by {c['author']}: {c['text'][:800]!r}. "
                  + ("If it is a complaint, acknowledge it and offer a concrete next step (DM / support link). " if (c["sentiment"] or 0) < 0 else "")
                  + "Return only the reply text.")
        text = enrich.ask_text(prompt, max_tokens=300)
        if text:
            return {"draft": text.strip().strip('"'), "mode": "ai", "url": c["url"] or p.get("url")}
    neg = (c["sentiment"] or 0) < 0
    who = c["author"] or "there"
    if seat:
        text = (f"Hi {who}, sorry to hear this. We'd like to fix it: could you DM us the details so the {seat} team can follow up today?"
                if neg else f"Thanks {who}! Glad this landed. The {seat} team reads every comment here, so keep the feedback coming.")
    else:
        text = (f"Interesting point, {who}. Do you see the same with the other players, or is this specific to {c['brand']}?"
                if not neg else f"That matches what others in this thread report about {c['brand']}. Has anything changed since?")
    return {"draft": text, "mode": "template", "url": c["url"] or p.get("url")}


def hub_summary(days: int = 7) -> dict:
    """Small per-sector numbers for the Nazar hub."""
    _ensure()
    since = _since(days)
    return {"posts": db.q1("SELECT COUNT(*) n FROM social_posts WHERE created>=?", (since,))["n"],
            "comments": db.q1("SELECT COUNT(*) n FROM social_comments WHERE created>=?", (since,))["n"],
            "papers": db.q1("SELECT COUNT(*) n FROM papers")["n"],
            "top": db.q(f"SELECT platform, brand, title, text, url, created, {ENGAGE} eng FROM social_posts WHERE created>=? ORDER BY eng DESC LIMIT 5", (since,))}
