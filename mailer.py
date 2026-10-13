"""Daily email: 5 best bets plus 5 money-trail items per category, as HTML + plain text.

Email clients ignore most CSS, so the layout is tables with inline styles only.
"""
import datetime
import html
import os
import re
import subprocess

INK, MUTED, LINE, PAPER, HILITE = "#15173b", "#5a5f8c", "#dcdff0", "#f3f4fb", "#c8f442"
BRAND = "#5b3df5"
FONT = "font-family:Helvetica,Arial,sans-serif"
KINDS = [("congress", "Congress and officials' trades", "#5b3df5"),
         ("polymarket", "Polymarket large trades", "#2d7ff9"),
         ("kalshi", "Kalshi large trades", "#0b8f5f")]


def e(s):
    return html.escape(str(s if s is not None else ""))


def _dt(s):
    try:
        s = str(s).replace("Z", "+00:00")
        s = re.sub(r"\.\d+", "", s)
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            return d, False
        return d.astimezone(), True
    except Exception:
        return None, False


def when(s, with_day=True):
    d, timed = _dt(s)
    if not d:
        return str(s or "")
    day = d.strftime("%a %b ") + str(d.day)
    if not timed or len(str(s)) <= 10:
        return day
    t = d.strftime("%I:%M %p").lstrip("0")
    return (day + ", " + t) if with_day else t


def why(b, limit=330):
    """The strongest reasons from the rationale, minus the numbers shown elsewhere and off-topic chatter."""
    txt = re.split(r";?\s*Last 30 days:", str(b.get("rationale") or ""))[0]
    txt = re.sub(r"\(\? yrs experience, ", "(", txt)
    txt = re.sub(r", shrink [\d.]+", "", txt)
    txt = re.sub(r"(\d)\.0(?=[,;:\s]|$)", r"\1", txt)
    parts = re.split(r"(?<=[a-z0-9\)%])\.\s+(?=[A-Z])", txt)
    keep = [p.strip().rstrip(".") for p in parts
            if p.strip() and not p.startswith(("Model gives", "Blends the market", "Social:"))]
    out = ""
    for p in keep:
        if out and len(out) + len(p) > limit:
            break
        out += ("" if not out else " ") + p + "."
    return out[:limit + 80]


def pick_bets(dash, n=5):
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for b in dash.get("bets") or []:
        d, timed = _dt(b.get("starts"))
        if d and timed and d < now:
            continue
        out.append(b)
    return out[:n]


def pick_trail(dash, n=5):
    out = {}
    for kind, _, _ in KINDS:
        seen, rows = set(), []
        for r in dash.get("money_trail") or []:
            det = str(r.get("detail") or "")
            key = str(r.get("what") or "").split(" ")[-1] if kind == "congress" else det   # one row per stock / market
            if r.get("kind") != kind or key in seen or re.match(r"^KX[A-Z0-9.\-]+$", det):
                continue
            seen.add(key)
            rows.append(r)
            if len(rows) == n:
                break
        out[kind] = rows
    return out


def headline(r):
    n = (r.get("news") or [None])[0]
    return n if n and n.get("strength") == "direct" else None


def bar(label, p, color):
    return ('<div style="font-size:11px;color:%s;margin-top:4px">%s <b style="color:%s">%.1f%%</b></div>'
            '<div style="background:#e6e8f7;border-radius:3px"><div style="width:%d%%;height:7px;background:%s;border-radius:3px"></div></div>'
            % (MUTED, label, INK, p * 100, max(2, min(100, round(p * 100))), color))


def bet_card(i, b):
    book = b.get("venue") or ""
    meta = " · ".join(x for x in [b.get("title"), b.get("league"), when(b.get("starts"))] if x)
    plain = str(b.get("plain_bet") or "")
    plain_html = ("<b>The bet:</b>" + e(plain[len("The bet:"):])) if plain.startswith("The bet:") else e(plain)
    wx = ('<div style="font-size:12px;color:%s;margin-top:6px">Weather: %s</div>' % (MUTED, e(b["weather"]))) if b.get("weather") else ""
    top = i <= 3
    return ('<div style="background:#fff;border:1px solid %s;border-radius:10px;padding:16px 18px;margin-bottom:14px">'
            '<div style="font-size:18px;font-weight:800;line-height:1.3"><span style="background:%s;color:%s;border-radius:5px;padding:2px 9px">%d</span> %s</div>'
            '<div style="font-size:12px;color:%s;margin-top:4px">%s</div>'
            '<div style="font-size:14px;margin-top:10px"><b style="font-size:20px">%s</b> %s &nbsp;·&nbsp; EV <b>%+.1f%%</b> &nbsp;·&nbsp; Confidence <b>%d</b>/100</div>'
            '%s%s'
            '<div style="font-size:14px;line-height:1.5;margin-top:10px"><b style="font-size:10px;letter-spacing:.08em;color:%s">WHY</b><br>%s</div>%s'
            '<div style="background:#f0edff;border-left:4px solid %s;border-radius:8px;padding:12px 14px;margin-top:12px;font-size:14px;line-height:1.5">%s</div></div>'
            % (LINE, HILITE if top else BRAND, INK if top else "#fff", i, e(b.get("pick")), MUTED, e(meta),
               e(b.get("price")), e(book), (b.get("ev") or 0) * 100, round(b.get("confidence") or 0),
               bar("Model", b.get("p_model") or 0, BRAND), bar("Market", b.get("p_market") or 0, "#b3b7d6"),
               MUTED, e(why(b)), wx, BRAND, plain_html))


def trail_row(r, color):
    n = headline(r)
    what = str(r.get("what") or "")
    what = what[:1].lower() + what[1:]
    comm = ", ".join(r.get("committees") or [])
    meta = " · ".join(x for x in [str(r.get("size") or ""), when(r.get("at")), ("sits on " + comm) if comm else "",
                                  "disclosed late" if r.get("late") else ""] if x)
    if n:
        news = '<a href="%s" style="color:%s">%s</a> <span style="color:%s">(%s)</span>' % (e(n.get("link")), INK, e(n.get("title")), MUTED, e(n.get("source")))
    else:
        news = '<span style="color:%s">No directly related headline today.</span>' % MUTED
    if r.get("lobbying"):
        news += '<div style="color:%s;margin-top:3px">Lobbying link: %s.</div>' % (MUTED, e(r["lobbying"]))
    return ('<div style="border-left:4px solid %s;padding-left:12px;margin-top:14px;font-size:13px;line-height:1.4">'
            '<div style="font-size:14px"><b>%s</b> %s%s<b>%s</b></div><div style="font-size:12px;color:%s">%s</div>%s</div>'
            % (color, e(r.get("who")), e(what), " — " if r.get("kind") == "congress" else " ", e(r.get("detail")), MUTED, e(meta), news))


def section_title(t, sub=""):
    return ('<div style="margin:18px 0 12px"><span style="background:%s;padding:2px 8px;font-size:13px;font-weight:800;letter-spacing:.1em">%s</span>'
            ' <span style="font-size:12px;color:%s">%s</span></div>' % (HILITE, e(t.upper()), MUTED, e(sub)))


def board_url():
    """Where the full board lives: a phone-reachable address once a password is set, else this Mac only."""
    if os.environ.get("EDGEDESK_URL"):
        return os.environ["EDGEDESK_URL"], ""
    if os.environ.get("EDGEDESK_PASSWORD"):
        try:
            name = subprocess.check_output(["scutil", "--get", "LocalHostName"], timeout=5).decode().strip()
        except Exception:
            name = ""
        if name:
            return "http://%s.local:%s" % (name, os.environ.get("PORT", "8787")), " (on your home Wi-Fi; user name edgedesk)"
    return "http://localhost:8787", " (on your Mac)"


def render(dash):
    url, url_note = board_url()
    now = datetime.datetime.now().astimezone()
    day = now.strftime("%A, %B ") + str(now.day)
    bets, trail = pick_bets(dash), pick_trail(dash)
    gen = when(dash.get("generated"))
    subject = "EdgeDesk — best bets and money trail for " + day
    if not bets:
        subject = "EdgeDesk — no picks this morning (" + day + ")"

    rows = [section_title("%d best bets" % len(bets) if bets else "Best bets", "ranked by edge and confidence")]
    rows += [bet_card(i, b) for i, b in enumerate(bets, 1)]
    if not bets:
        rows.append('<div style="font-size:14px">No bets cleared the bar in the latest refresh.</div>')
    elif len(bets) < 5:
        rows.append('<div style="font-size:12px;color:%s">Only %d picks were available this morning.</div>' % (MUTED, len(bets)))
    rows.append(section_title("Money trail", "who moved money, and the news it lines up with"))
    for kind, label, color in KINDS:
        body = "".join(trail_row(r, color) for r in trail[kind]) or '<div style="font-size:13px;color:%s;margin-top:10px">Nothing to show today.</div>' % MUTED
        rows.append('<div style="background:#fff;border:1px solid %s;border-radius:10px;padding:14px 18px 16px;margin-bottom:14px">'
                    '<div style="font-size:15px;font-weight:800"><span style="color:%s">&#9679;</span> %s</div>%s</div>' % (LINE, color, e(label), body))
    page = ('<div style="background:%s;padding:18px 10px"><div style="max-width:640px;margin:0 auto;%s;color:%s">'
            '<div style="background:%s;background-image:linear-gradient(135deg,#5b3df5,#9b5cf6);border-radius:12px;padding:22px">'
            '<div style="color:#fff;font-weight:800;font-size:18px;letter-spacing:.14em"><span style="background:%s;color:%s;border-radius:4px;padding:4px 8px;letter-spacing:0">ED</span> &nbsp;EDGEDESK</div>'
            '<div style="color:#fff;font-size:26px;font-weight:800;margin-top:16px">%s</div>'
            '<div style="color:#e4defc;font-size:13px;margin-top:6px">Data refreshed %s · %s candidates considered · %s books</div></div>'
            '%s'
            '<div style="font-size:12px;color:%s;line-height:1.6">These are coincidences in timing, not evidence of wrongdoing or inside knowledge.<br><br>'
            'Full board: <a href="%s" style="color:%s">%s</a>%s<br>'
            'Model estimates, not guarantees — stakes shown assume a $1,000 bankroll; only bet what you can afford to lose.</div></div></div>'
            % (PAPER, FONT, INK, BRAND, HILITE, INK, e(day), e(gen), e(dash.get("candidates_considered", "?")),
               e((dash.get("counts") or {}).get("books", "?")), "".join(rows), MUTED, e(url), INK, e(url), e(url_note)))

    t = ["EDGEDESK — " + day, "Data refreshed %s. %s candidates considered." % (gen, dash.get("candidates_considered", "?")), "", "BEST BETS", ""]
    for i, b in enumerate(bets, 1):
        t += ["%d. %s" % (i, b.get("pick")),
              "   %s, %s, %s" % (b.get("title"), b.get("league"), when(b.get("starts"))),
              "   %s at %s. EV %+.1f%%, confidence %d, model %.1f%% vs market %.1f%%."
              % (b.get("price"), b.get("venue") or "best book", (b.get("ev") or 0) * 100, round(b.get("confidence") or 0),
                 (b.get("p_model") or 0) * 100, (b.get("p_market") or 0) * 100),
              "   Why: " + why(b), "   " + str(b.get("plain_bet") or ""), ""]
    t += ["MONEY TRAIL", ""]
    for kind, label, _ in KINDS:
        t.append(label.upper())
        for r in trail[kind]:
            n = headline(r)
            t.append("- %s: %s — %s (%s, %s)" % (r.get("who"), r.get("what"), r.get("detail"), r.get("size"), when(r.get("at"))))
            t.append("  " + ("%s (%s) %s" % (n.get("title"), n.get("source"), n.get("link")) if n else "No directly related headline today."))
            if r.get("lobbying"):
                t.append("  Lobbying link: %s." % r["lobbying"])
        if not trail[kind]:
            t.append("- Nothing to show today.")
        t.append("")
    t += ["These are coincidences in timing, not evidence of wrongdoing or inside knowledge.", "",
          "Full board: %s%s" % (url, url_note),
          "Model estimates, not guarantees — stakes shown assume a $1,000 bankroll; only bet what you can afford to lose."]
    return {"subject": subject, "html": page, "text": "\n".join(t), "bets": len(bets),
            "trail": {k: len(v) for k, v in trail.items()}}
