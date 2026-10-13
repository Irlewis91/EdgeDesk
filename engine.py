"""Scoring engine: turns raw feeds into ranked bet candidates and congress
money-trail leads. Pure functions over the dicts produced by sources/history."""
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import sources as S
from history import implied

STOP = set("will the a an of in on to by be is for at and or vs vs. before after than more less win wins "
           "2025 2026 2027 does do what who which how any this that with from as end".split())


def words(t):
    return [w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if w not in STOP and len(w) > 2]


MONTH_WORDS = set("january february march april may june july august september october november december jan feb mar apr jun "
                  "jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday q1 q2 q3 q4".split())
NAME_ALIAS = {"u.s": "us", "usa": "us", "america": "us", "american": "us", "united": "us", "states": "us",
              "federal": "fed", "reserve": "fed", "fomc": "fed", "gop": "republican", "republicans": "republican",
              "democrats": "democrat", "democratic": "democrat", "btc": "bitcoin", "eth": "ethereum"}
FILLER = set("will the a an of in on to by be is are was for at as and or vs v than does do did what who which how when "
             "any this that with from end it its has have been yes".split())
# words that change what is being asked even when everything else matches ("be 4.25%" vs "be above 4.25%")
COMPARE = set("above below over under more less least most fewer greater higher lower between exactly not no "
              "first second third each both either neither only".split())
_MON = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_FULL_DATE = re.compile(r"\b(%s)\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b" % "|".join(_MON), re.I)
_BEFORE_YEAR = re.compile(r"\bbefore\s+((?:19|20)\d\d)\b", re.I)
_DATE = re.compile(r"\b(?:%s)\w*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|\b(?:19|20)\d\d\b" % "|".join(_MON), re.I)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?:\s*[ap]\.?m\b\.?)?(?:\s*(?:et|est|edt|ct|pt|utc|gmt)\b)?", re.I)


def specifics(text):
    """What makes a market question THIS question, as (words, numbers, years, deadlines).

    words maps each content word to whether it is written as a proper noun (person, place, team, ticker);
    numbers are the thresholds; years and deadlines are the dates it names ("before 2028" is the same
    deadline as "by December 31, 2027")."""
    t = _CLOCK.sub(" ", text or "")
    deadlines = {(int(y), _MON.index(m.lower()) + 1, int(d)) for m, d, y in _FULL_DATE.findall(t)}
    deadlines |= {(int(y) - 1, 12, 31) for y in _BEFORE_YEAR.findall(t)}
    years = {int(y) for y in re.findall(r"\b(?:19|20)\d\d\b", _BEFORE_YEAR.sub(" ", t))} | {d[0] for d in deadlines}
    t = _DATE.sub(" ", _BEFORE_YEAR.sub(" ", t))
    found = {}
    for w in re.findall(r"[^\W\d_][\w.'’&-]*", t):
        core = w.strip(".'’-")
        low = re.sub(r"['’]s$", "", core.lower())
        low = NAME_ALIAS.get(low, low)
        if len(low) < 2 or low in FILLER or low in MONTH_WORDS:
            continue
        found[low] = found.get(low, False) or core[:1].isupper()
    nums = {x.rstrip(".").replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", t)}
    return found, {n.rstrip("0").rstrip(".") if "." in n else n for n in nums}, years, deadlines


def same_question(a, b):
    """True only when two venues are asking the same thing: the same people/places/teams, the same
    numbers and the same dates. Sharing a template ("Will <state> enact a data center moratorium…")
    is not enough, and neither is sharing the subject when the ask differs (win vs run vs qualify)."""
    wa, na, ya, da = specifics(a)
    wb, nb, yb, db = specifics(b)
    if na != nb or (ya and yb and ya != yb) or (da and db and da != db):
        return False
    loose = []
    for mine, theirs in ((wa, wb), (wb, wa)):
        # a plain word may differ in form across venues (nomination / nominee); a name may not
        left = [w for w in mine if w not in theirs and not (
            not mine[w] and len(w) >= 5 and any(o[:5] == w[:5] and not theirs[o] for o in theirs))]
        if any(mine[w] or w in COMPARE for w in left):
            return False
        loose.append(left)
    # leftover wording is tolerated only as a couple of extra words on one side; different words on
    # both sides (win / run) is a different question
    return not (loose[0] and loose[1]) and len(loose[0]) + len(loose[1]) <= 2


def dec(ml):
    return 1 + (ml / 100 if ml > 0 else 100 / -ml)


def kalshi_fee(p):
    return 0.07 * p * (1 - p)      # Kalshi taker fee per $1 contract (approx.)


def kelly(p, price_dec, frac=0.25, cap=0.03):
    b = price_dec - 1
    f = (b * p - (1 - p)) / b if b > 0 else 0
    return max(0.0, min(cap, f * frac))


def hours_until(iso):
    d = S.parse_dt(iso)
    return (d - S.now_utc()).total_seconds() / 3600 if d else None


# ------------------------------------------------------------- team matching
def norm_team(x):
    x = (x or "").lower().replace("&", "and")
    x = re.sub(r"\bst\.?(?=\s|$)", "state", x)
    x = re.sub(r"\b(fc|sc|cf|afc)\b", "", x)
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", x)).strip()


def team_keys(t):
    keys = {t["name"], t["display"], t["short"], t["location"], t["abbr"], t["location"] + " " + t["name"]}
    return {norm_team(k) for k in keys if k and len(k) > 1}


def team_hit(label, t):
    """Exact match on any normalized team alias ('Florida' must not match 'Florida Atlantic')."""
    return norm_team(label) in team_keys(t)


def match_pm_game(g, pm_games):
    for m in pm_games:
        o0, o1 = m["outcomes"]
        gd = S.parse_dt(g["date"])
        md = S.parse_dt(m["start"])
        if gd and md and abs((gd - md).total_seconds()) > 36 * 3600:
            continue
        if team_hit(o0, g["home"]) and team_hit(o1, g["away"]):
            return {"home": m["asks"][0], "away": m["asks"][1], "mid_home": m["prices"][0], "m": m}
        if team_hit(o0, g["away"]) and team_hit(o1, g["home"]):
            return {"home": m["asks"][1], "away": m["asks"][0], "mid_home": m["prices"][1], "m": m}
    return None


MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def _kalshi_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    try:
        return datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).date()
    except (KeyError, ValueError):
        return None


def match_kalshi_game(g, ks_by_event):
    gd = S.parse_dt(g["date"])
    for ev, mk in ks_by_event.items():
        kd = _kalshi_date(ev)
        if gd and kd and abs((gd.date() - kd).days) > 1:
            continue
        h = next((m for m in mk if team_hit(m["sub"], g["home"])), None)
        a = next((m for m in mk if team_hit(m["sub"], g["away"])), None)
        if h and a and h is not a:
            return {"home": h, "away": a}
    return None


# ------------------------------------------------------------ sports bets
def sports_candidates(games, pm_games, kalshi_sports):
    ks_by_event = defaultdict(list)
    for m in kalshi_sports:
        ks_by_event[m["event_ticker"]].append(m)
    out = []
    for g in games:
        o = g["odds"]
        if g["sport"] == "soccer" and not o.get("ml_draw"):
            continue   # can't price a 3-way market without the draw
        pm = match_pm_game(g, pm_games)
        km = match_kalshi_game(g, ks_by_event)
        g["cross"] = {"polymarket": pm and {"home": pm["home"], "away": pm["away"], "url": pm["m"]["url"]},
                      "kalshi": km and {"home": km["home"]["yes_ask"], "away": km["away"]["yes_ask"], "url": km["home"]["url"]}}
        # ---- market consensus (no-vig) from every venue we have
        mkt, src = [], []
        if o.get("ml_home") and o.get("ml_away"):
            ih, ia = implied(o["ml_home"]), implied(o["ml_away"])
            idr = implied(o["ml_draw"]) if o.get("ml_draw") else 0
            mkt.append(ih / (ih + ia + idr))
            src.append("%s %s/%s" % (o["provider"] or "Sportsbook", fmt_ml(o["ml_home"]), fmt_ml(o["ml_away"])))
        if pm:
            mkt.append(pm["mid_home"])
            src.append("Polymarket %.0f¢/%.0f¢" % (pm["home"] * 100, pm["away"] * 100))
        if km and km["home"]["mid"] > 0:
            # soccer has a separate tie contract, so a team's mid is already its 3-way probability
            mkt.append(km["home"]["mid"] if g["sport"] == "soccer" else km["home"]["mid"] / max(0.01, km["home"]["mid"] + km["away"]["mid"]))
            src.append("Kalshi %.0f¢/%.0f¢" % (km["home"]["yes_ask"] * 100, km["away"]["yes_ask"] * 100))
        if not mkt:
            continue
        # a venue far from the others is almost always a mismatched market; drop it
        if len(mkt) >= 2:
            med = sorted(mkt)[len(mkt) // 2]
            bad = [i for i, x in enumerate(mkt) if abs(x - med) > 0.12]
            for i in reversed(bad):
                if src[i].startswith("Polymarket"):
                    pm = None
                elif src[i].startswith("Kalshi"):
                    km = None
                del mkt[i], src[i]
            if not mkt:
                continue
        p_mkt = sum(mkt) / len(mkt)
        # ---- blend: market carries weight 2; models/history add their weights
        ests = [("Market consensus", p_mkt, 2.0, "; ".join(src))]
        if g.get("predictor_home"):
            ests.append(("ESPN Matchup Predictor (FPI/BPI)", g["predictor_home"], 0.5 if g["league"] == "NCAAF" else 0.8, "%.1f%% home" % (g["predictor_home"] * 100)))
        for e in (g.get("history") or {}).get("estimates", []):
            ests.append((e["source"], e["p_home"], e["weight"], e["note"]))
        wsum = sum(w for _, _, w, _ in ests)
        p_home = sum(p * w for _, p, w, _ in ests) / wsum
        spread_of_models = max(p for _, p, _, _ in ests) - min(p for _, p, _, _ in ests)
        draw_p = (implied(o["ml_draw"]) / (implied(o["ml_home"]) + implied(o["ml_away"]) + implied(o["ml_draw"]))) if o.get("ml_draw") and o.get("ml_home") and o.get("ml_away") else 0
        for side in ("home", "away"):
            p = p_home if side == "home" else (1 - p_home - draw_p)
            team, opp = g[side], g["away" if side == "home" else "home"]
            venues = []
            ml = o.get("ml_" + side)
            if ml:
                venues.append({"venue": o["provider"] or "Sportsbook", "price": fmt_ml(ml), "dec": dec(ml), "url": g["url"]})
            if pm and 0.02 < pm[side] < 0.98:
                venues.append({"venue": "Polymarket", "price": "%.0f¢" % (pm[side] * 100), "dec": 1 / pm[side], "url": pm["m"]["url"]})
            if km and 0.02 < km[side]["yes_ask"] < 0.98:
                c = km[side]["yes_ask"]
                venues.append({"venue": "Kalshi", "price": "%.0f¢" % (c * 100), "dec": 1 / (c + kalshi_fee(c)), "url": km[side]["url"]})
            if not venues:
                continue
            best = max(venues, key=lambda v: v["dec"])
            ev = p * best["dec"] - 1
            p_side_mkt = p_mkt if side == "home" else 1 - p_mkt - draw_p
            # skip longshots and suspiciously large model-vs-market gaps (usually stale/mismatched data)
            if ev <= 0.005 or p < 0.22 or 1 / best["dec"] < 0.15:
                continue
            factors = [{"label": n, "value": "%.1f%%" % ((pp if side == "home" else 1 - pp - draw_p) * 100), "note": note}
                       for n, pp, _, note in ests]
            hist = g.get("history") or {}
            ctx = history_notes(g, side, hist)
            gap = abs(p - p_side_mkt)
            conf = 50 + 8 * min(3, len(ests) - 1) + (6 if len(mkt) > 1 else 0) - 60 * spread_of_models - 250 * max(0, gap - 0.06)
            conf = max(10, min(90, conf))
            out.append({
                "id": "sp-%s-%s" % (g["id"], side), "type": "sports", "league": g["league"],
                "title": "%s @ %s" % (g["away"]["display"], g["home"]["display"]),
                "pick": "%s moneyline" % team["display"], "keywords": [team["name"], opp["name"]],
                "news_keys": [team["display"], opp["display"], team["location"] + " " + team["name"]],
                "venue": best["venue"], "price": best["price"], "url": best["url"], "all_prices": venues,
                "dec": best["dec"], "side_name": team["display"], "opp_name": opp["display"],
                "p_model": p, "p_market": p_side_mkt,
                "edge": p - 1 / best["dec"], "ev": ev, "confidence": conf,
                "kelly": kelly(p, best["dec"]), "starts": g["date"],
                "factors": factors, "context": ctx, "research": g.get("research_link"),
            })
    return out


def fmt_ml(ml):
    return ("+%d" % ml) if ml and ml > 0 else str(ml)


def history_notes(g, side, hist):
    t, o = g[side], g["away" if side == "home" else "home"]
    notes = []
    if g.get("form_" + side):
        notes.append("%s last 5: %s (opp %s)" % (t["abbr"], g["form_" + side], g.get("form_" + ("away" if side == "home" else "home"), "")))
    if g.get("injuries_" + side):
        notes.append("%s key absences: %s" % (t["abbr"], ", ".join(g["injuries_" + side][:4])))
    oside = "away" if side == "home" else "home"
    if g.get("injuries_" + oside):
        notes.append("%s key absences: %s" % (o["abbr"], ", ".join(g["injuries_" + oside][:4])))
    nv = hist.get("nflverse")
    if nv:
        notes.append("nflverse: %s ATS %s this season, %s last season; H2H %s" % (
            t["abbr"], nv[side + "_this_season"]["ats"], nv[side + "_last_season"]["ats"], nv["h2h_last10"]))
        if nv.get("calibration"):
            notes.append(nv["calibration"])
        if nv.get("rest"):
            notes.append("Rest: " + nv["rest"] + (" · divisional game" if nv.get("divisional") else ""))
    mlb = hist.get("MLB StatsAPI + Savant")
    if mlb:
        notes.append("SP: %s (ERA %s, xERA %s) vs %s (ERA %s, xERA %s)" % (
            mlb[side]["pitcher"], mlb[side]["era"], mlb[side]["xera"], mlb[oside]["pitcher"], mlb[oside]["era"], mlb[oside]["xera"]))
        notes.append("Run diff %s vs %s; L10 %s vs %s" % (mlb[side]["run_diff"], mlb[oside]["run_diff"], mlb[side]["last10"], mlb[oside]["last10"]))
    nhl = hist.get("NHL API")
    if nhl and side in nhl and oside in nhl:
        notes.append("NHL: goal diff %s vs %s, L10 %s vs %s" % (nhl[side]["goal_diff"], nhl[oside]["goal_diff"], nhl[side]["last10"], nhl[oside]["last10"]))
    fd = hist.get("football-data.co.uk")
    if fd:
        notes.append("Form (last 8): %s vs %s; H2H: %s" % (fd[side + "_last8"]["form"], fd[oside + "_last8"]["form"], "; ".join(fd["h2h"][-3:]) or "none"))
    es = hist.get("ESPN season results")
    if es:
        notes.append("Season avg margin %+.1f vs %+.1f" % (es[side]["avg_margin"], es[oside]["avg_margin"]))
    if hist.get("TheSportsDB H2H"):
        notes.append("Recent meetings: " + "; ".join(hist["TheSportsDB H2H"][-3:]))
    return notes


# ------------------------------------------------- prediction-market bets
def binary_yes(m):
    """(yes_price_to_buy, no_price_to_buy, mid) for a binary market, or None."""
    if m["venue"] == "Polymarket":
        if [o.lower() for o in m["outcomes"]] != ["yes", "no"]:
            return None
        ya = m["best_ask"] or m["prices"][0]
        na = (1 - m["best_bid"]) if m["best_bid"] else m["prices"][1]
        return ya, na, m["prices"][0]
    if m["yes_ask"] <= 0:
        return None
    return m["yes_ask"], 1 - m["yes_bid"] if m["yes_bid"] else 1 - m["mid"], m["mid"]


def market_text(m):
    return (m.get("question", "") + " " + (m.get("sub") or "")).strip()


def prediction_candidates(pm, km, headlines):
    out = []
    # 1) Cross-venue disagreement on the same question
    kidx = [(k, set(words(market_text(k)))) for k in km]
    for p in pm:
        py = binary_yes(p)
        if not py:
            continue
        pw = set(words(p["question"]))
        if len(pw) < 3:
            continue
        best, sim = None, 0
        for k, kw in kidx:
            if not kw:
                continue
            j = len(pw & kw) / len(pw | kw)
            # similar wording is only a lead; e.g. Louisiana vs Alabama share the whole template but are different bets
            if j > sim and j >= 0.5 and same_question(p["question"], market_text(k)):
                best, sim = k, j
        if not best:
            continue
        pe, ke = S.parse_dt(p["end"]), S.parse_dt(best["end"])
        if pe and ke and abs((pe - ke).days) > 7:
            continue
        ky = binary_yes(best)
        if not ky:
            continue
        cons = (py[2] + ky[2]) / 2
        for side in ("YES", "NO"):
            opts = [("Polymarket", py[0] if side == "YES" else py[1], p["url"]),
                    ("Kalshi", (ky[0] + kalshi_fee(ky[0])) if side == "YES" else (ky[1] + kalshi_fee(ky[1])), best["url"])]
            venue, price, url = min(opts, key=lambda x: x[1])
            prob = cons if side == "YES" else 1 - cons
            if not 0.03 < price < 0.97:
                continue
            ev = prob / price - 1
            if ev > 0.03:
                out.append(_pm_cand("xv", p, side, venue, price, url, prob, ev, 45 + 30 * (sim - 0.5),
                                    [{"label": "Polymarket YES", "value": "%.0f¢" % (py[2] * 100), "note": p["question"]},
                                     {"label": "Kalshi YES", "value": "%.0f¢" % (ky[2] * 100), "note": market_text(best)},
                                     {"label": "Question match", "value": "%.0f%%" % (sim * 100), "note": "verify both venues resolve identically"}],
                                    "Cross-venue price gap"))
    # 2) Favorite-longshot bias: near-certain outcomes resolving soon are often underpriced
    for m in pm + km:
        b = binary_yes(m)
        h = hours_until(m["end"])
        if not b or h is None or not 6 < h < 24 * 10:
            continue
        vol_ok = m["volume24h"] >= (10000 if m["venue"] == "Polymarket" else 2000)
        if not vol_ok:
            continue
        for side, price in (("YES", b[0]), ("NO", b[1])):
            if m["venue"] == "Kalshi":
                price = price + kalshi_fee(price)
            if 0.80 <= price <= 0.95:
                prob = min(0.99, (b[2] if side == "YES" else 1 - b[2]) + 0.15 * (1 - price))
                ev = prob / price - 1
                if ev > 0.01:
                    ann = (1 / price - 1) * (8760 / max(h, 1)) * 100
                    out.append(_pm_cand("fav", m, side, m["venue"], price, m["url"], prob, ev, 42,
                                        [{"label": "Price", "value": "%.0f¢" % (price * 100), "note": "resolves in %.0f h" % h},
                                         {"label": "Longshot-bias adj.", "value": "+%.1f pts" % (15 * (1 - price)), "note": "favorites in 80-95¢ range historically settle above implied"},
                                         {"label": "Annualized yield", "value": "%.0f%%" % ann, "note": "if it resolves as priced"}],
                                        "High-probability favorite"))
    # 3) News-confirmed momentum
    for m in pm + km:
        b = binary_yes(m)
        if not b or abs(m.get("change24h", 0)) < 0.10 or m["volume24h"] < 20000:
            continue
        kw = set(words(market_text(m)))
        hits = [h for h in headlines if len(kw & set(words(h["title"]))) >= 2]
        if not hits:
            continue
        side = "YES" if m["change24h"] > 0 else "NO"
        price = b[0] if side == "YES" else b[1]
        if not 0.1 < price < 0.9:
            continue
        prob = min(0.97, (b[2] if side == "YES" else 1 - b[2]) + 0.2 * abs(m["change24h"]))
        ev = prob / price - 1
        if ev > 0.02:
            out.append(_pm_cand("mom", m, side, m["venue"], price, m["url"], prob, ev, 30,
                                [{"label": "24h move", "value": "%+.0f pts" % (m["change24h"] * 100), "note": "volume $%s" % f"{m['volume24h']:,.0f}"},
                                 {"label": "News confirmation", "value": str(len(hits)), "note": hits[0]["title"][:120]}],
                                "News-driven momentum", news=hits[:3]))
    return out


def _pm_cand(kind, m, side, venue, price, url, prob, ev, conf, factors, why, news=None):
    return {"id": "pm-%s-%s-%s" % (kind, m["id"], side), "type": "prediction", "league": why,
            "title": m.get("event") or m["question"], "pick": "%s — %s" % (side, market_text(m)),
            "keywords": words(market_text(m))[:5], "venue": venue, "price": "%.0f¢" % (price * 100),
            "url": url, "dec": 1 / price, "side_name": side, "question": market_text(m),
            "p_model": prob, "p_market": price, "edge": prob - price, "ev": ev,
            "confidence": conf, "kelly": kelly(prob, 1 / price), "starts": m["end"], "factors": factors,
            "context": [], "news": news or []}


# ------------------------------------------------------ social + news
def attach_social(cands, reddit_posts, headlines, n=30, sports_headlines=None):
    def one(c):
        kw = [k for k in c["keywords"] if k][:3]
        if not kw:
            return c
        q = " ".join(kw[:2])
        posts = S.bluesky(q, 25)
        kws = {k.lower() for k in kw}
        rd = [p for p in reddit_posts if sum(k in p["text"].lower() for k in kws) >= min(2, len(kws))]
        allp = posts + rd
        c["social"] = {
            "mentions": len(allp), "bluesky": len(posts), "reddit": len(rd),
            "tone": round(sum(p["tone"] for p in allp) / len(allp), 2) if allp else 0,
            "samples": sorted(allp, key=lambda p: p.get("likes", 0), reverse=True)[:4],
            "query": q,
        }
        if c["type"] == "sports":
            nk = [k.lower() for k in c.get("news_keys", [])]
            c["news"] = [h for h in (sports_headlines or headlines) if any(k in h["title"].lower() for k in nk)][:3]
        else:
            c["news"] = c.get("news") or [h for h in headlines if len(set(words(h["title"])) & kws) >= 2][:3]
        # small confidence nudge: buzz with a clear tone
        if len(allp) >= 8:
            c["confidence"] = max(10, min(92, c["confidence"] + 4 * c["social"]["tone"]))
        return c
    with ThreadPoolExecutor(6) as ex:
        cands[:n] = list(ex.map(one, cands[:n]))
    return cands


def score(c):
    # EV weighted by confidence; clipped so a single wild EV can't dominate
    return min(c["ev"], 0.25) * (c["confidence"] / 100)


def rationale(c):
    parts = []
    implied_ = c["p_model"] - c["edge"]
    parts.append("Model gives %.1f%% vs %.1f%% implied by the best price (%s at %s) — edge %+.1f pts, EV %+.1f%%." % (
        c["p_model"] * 100, implied_ * 100, c["venue"], c["price"], c["edge"] * 100, c["ev"] * 100))
    if c["type"] == "sports":
        extra = [f["label"] for f in c["factors"][1:]]
        if extra:
            parts.append("Blends the market with " + ", ".join(extra) + ".")
        if c.get("model_components"):
            parts.append(c["model_components"][0] + ".")
        co = c.get("coaches") or {}
        cn = ["%s (%s)" % (v["name"], v.get("note", "")) for v in (co.get("home"), co.get("away")) if v]
        if cn:
            parts.append("Coaching: " + "; ".join(cn) + ".")
        if c.get("public"):
            parts.append(c["public"] + ".")
    if c.get("context"):
        parts.append("; ".join(c["context"][:3]) + ".")
    s = c.get("social")
    if s and s["mentions"]:
        mood = "positive" if s["tone"] > 0.15 else "negative" if s["tone"] < -0.15 else "mixed"
        parts.append("Social: %d recent posts (%d Bluesky, %d Reddit), tone %s." % (s["mentions"], s["bluesky"], s["reddit"], mood))
    if c.get("news"):
        parts.append("News: " + c["news"][0]["title"][:140])
    return " ".join(parts)


def fair_ml(p):
    """American odds at which a bet with win probability p breaks even."""
    return "+%d" % round(100 * (1 - p) / p) if p < 0.5 else "-%d" % round(100 * p / (1 - p))


def _odds_floor(p):
    return fair_ml(min(0.97, max(0.03, p)))


def plain_bet(c, bankroll=1000):
    """Plain English: what to bet, where, the worst price still worth it, and the payout."""
    stake = max(1, round(c.get("kelly", 0) * bankroll))
    ret = stake * c["dec"]
    cents = c["price"].endswith("¢")
    mk = c.get("market", "moneyline")
    limit = "Still worth it at odds of %s or better; skip it if the price gets worse than that." % _odds_floor(c["p_model"] / max(1e-9, 1 - c.get("p_push", 0)))
    if c["type"] == "prediction":
        yes = c["side_name"] == "YES"
        what = "Bet that this %s happen: \"%s\"" % ("WILL" if yes else "will NOT", c.get("question", c["pick"]))
        how = "Buy %s shares on %s (currently %s)" % (c["side_name"], c["venue"], c["price"])
        limit = "Still worth it up to %d¢; if the price climbs past that, skip it." % max(1, int(c["p_model"] * 100 - 1))
        outcome = "each share pays $1 if you're right and $0 if you're wrong"
    elif mk == "moneyline":
        what = "Bet on the %s to beat the %s" % (c["side_name"], c["opp_name"])
        if cents:
            how = "Buy \"%s to win\" contracts on %s (currently %s)" % (c["side_name"], c["venue"], c["price"])
            limit = "Still worth it up to %d¢; if the price climbs past that, skip it." % max(1, int(c["p_model"] * 100 - (4 if c["venue"] == "Kalshi" else 1)))
        else:
            how = "Place a moneyline bet on %s at %s" % (c["venue"], c["price"])
        outcome = "they just need to win the game outright"
    elif mk in ("spread", "alt_spread"):
        ln = c["line"]
        what = "Bet the %s %s%g%s" % (c["side_name"], "+" if ln > 0 else "", ln, " (alternate line)" if mk == "alt_spread" else "")
        how = "Place it on %s at %s" % (c["venue"], c["price"])
        if ln < 0:
            outcome = "they must win by %s or more" % (int(-ln) + 1 if not float(ln).is_integer() else "%d (win by exactly %d is a push/refund)" % (int(-ln) + 1, int(-ln)))
        else:
            outcome = ("they can lose by up to %d and still cash" % int(ln) if not float(ln).is_integer() else "they can lose by up to %d (lose by exactly %d = push)" % (int(ln) - 1, int(ln))) if ln >= 1 else "they must not lose"
    elif mk in ("total", "alt_total"):
        over = c["side"] == "over"
        ln = c["line"]
        what = "Bet the %s %g total points in %s%s" % ("OVER" if over else "UNDER", ln, c["title"], " (alternate line)" if mk == "alt_total" else "")
        how = "Place it on %s at %s" % (c["venue"], c["price"])
        need = int(math.floor(ln)) + 1 if over else int(math.ceil(ln)) - 1
        outcome = "the two teams need %d or %s combined" % (need, "more" if over else "fewer")
    else:  # prop
        if c["stat"] in ("anytime_td", "anytime_goal"):
            what = "Bet %s to score a %s at any point in the game" % (c["player"], c["stat_label"])
            outcome = "one score wins it"
        else:
            over = c["side"] == "over"
            need = int(math.floor(c["line"])) + 1 if over else int(math.ceil(c["line"])) - 1
            what = "Bet %s %s %g %s" % (c["player"], "OVER" if over else "UNDER", c["line"], c["stat_label"])
            outcome = "it needs %d or %s %s" % (need, "more" if over else "fewer", c["stat_label"])
        how = "Place the player prop on %s at %s" % (c["venue"], c["price"])
    return ("The bet: %s. %s; %s. Suggested stake: $%d on a $%s bankroll (%.1f%%), which returns about $%.2f (profit $%.2f) if it hits. %s"
            % (what, how, outcome, stake, f"{bankroll:,}", stake / bankroll * 100, ret, ret - stake, limit))


def top_bets(cands, n=10):
    """Top-n with diversity: one bet per game/event, capped props / alt lines / prediction markets."""
    cands.sort(key=score, reverse=True)
    caps = {"prop": 3, "alt": 2, "prediction": 3}
    used = defaultdict(int)
    picked, per_event, players = [], set(), set()
    for c in cands:
        kind = "prediction" if c["type"] == "prediction" else "prop" if c.get("market") in ("prop", "alt_prop") else "alt" if str(c.get("market", "")).startswith("alt") else "main"
        ev_key = c["title"] + ("|" + c.get("player", "") if kind == "prop" else "")
        if ev_key in per_event or (kind == "prop" and c.get("player") in players):
            continue
        if kind != "prop" and c["title"] in per_event:
            continue
        if kind in caps and used[kind] >= caps[kind]:
            continue
        used[kind] += 1
        per_event.add(ev_key)
        per_event.add(c["title"]) if kind != "prop" else players.add(c.get("player"))
        c["score"] = round(score(c) * 1000, 1)
        c["rationale"] = rationale(c)
        c["plain_bet"] = plain_bet(c)
        picked.append(c)
        if len(picked) == n:
            break
    return picked


SPORTSBOOKS = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "BetRivers", "bet365", "Fanatics", "Hard Rock",
               "theScore Bet", "Bally Bet", "Bovada", "Pinnacle"]


def _american(d):
    return "+%d" % round((d - 1) * 100) if d >= 2 else "-%d" % round(100 / (d - 1))


def parlays(cands):
    """Parlay suggestions built only from +EV legs in different games (so the legs are close to
    independent), all priced at ONE sportsbook since a parlay has to be placed in one place."""
    pool = [c for c in sorted(cands, key=score, reverse=True) if c["type"] == "sports" and c["ev"] > 0.01 and c["confidence"] >= 40][:60]
    out = []
    for name, n_legs, min_p, blurb in (("Safer double", 2, 0.50, "Two of the likeliest positive-edge legs."),
                                        ("Balanced treble", 3, 0.42, "Three legs across different games; the main suggestion."),
                                        ("Long shot", 4, 0.30, "Four legs, bigger payout, hits rarely. Keep the stake small.")):
        best = None
        for book in SPORTSBOOKS:
            legs, games = [], set()
            for c in pool:
                if c["title"] in games or c["p_model"] < min_p:
                    continue
                q = next((v for v in c.get("all_prices", []) if v["venue"] == book), None)
                if not q:
                    continue
                p_win = c["p_model"] / max(1e-9, 1 - c.get("p_push", 0))
                if p_win * q["dec"] - 1 <= 0:          # leg must still be +EV at THIS book's price
                    continue
                legs.append({"id": c["id"], "pick": c["pick"], "title": c["title"], "league": c["league"], "market": c.get("market"),
                             "price": q["price"], "dec": q["dec"], "p": p_win, "starts": c.get("starts")})
                games.add(c["title"])
                if len(legs) == n_legs:
                    break
            if len(legs) < n_legs:
                continue
            dec_, p_ = 1.0, 1.0
            for l in legs:
                dec_ *= l["dec"]
                p_ *= l["p"]
            ev = p_ * dec_ - 1
            if best is None or ev > best["ev"]:
                best = {"name": name, "blurb": blurb, "book": book, "legs": legs, "dec": dec_, "price": _american(dec_),
                        "p": p_, "ev": ev, "fair_price": _american(1 / p_) if 0 < p_ < 1 else ""}
        if best:
            stake = max(1, round(1000 * kelly(best["p"], best["dec"], frac=0.15, cap=0.01)))
            best["stake"] = stake
            best["plain"] = ("The parlay: on %s, add these %d legs to one bet slip: %s. Combined odds about %s. All %d must win. "
                             "Chance of hitting: about %.0f%%. Suggested stake: $%d on a $1,000 bankroll, returning about $%.2f if it hits. "
                             "Sportsbooks may price a parlay slightly differently from the product of the legs, so check the slip; "
                             "it stays worth it at %s or better." % (
                                 best["book"], len(best["legs"]), "; ".join("%s%s (%s)" % (l["pick"], (" in " + l["title"]) if l.get("market") in ("total", "alt_total") else "", l["price"]) for l in best["legs"]),
                                 best["price"], len(best["legs"]), best["p"] * 100, stake, stake * best["dec"], best["fair_price"]))
            out.append(best)
    return out


def board(cands, n=25):
    """Ranked list for a secondary board (props, alt lines) with rationale and plain bet."""
    out = []
    for c in sorted(cands, key=score, reverse=True)[:n]:
        c["score"] = round(score(c) * 1000, 1)
        c["rationale"] = rationale(c)
        c["plain_bet"] = plain_bet(c)
        out.append(c)
    return out


def top_bets(cands, n=10):
    cands.sort(key=score, reverse=True)
    picked, per_event, n_sports, n_pm = [], set(), 0, 0
    for c in cands:
        ev_key = c["title"]
        if ev_key in per_event:
            continue
        if c["type"] == "sports" and n_sports >= 7:
            continue
        if c["type"] == "prediction" and n_pm >= 6:
            continue
        per_event.add(ev_key)
        n_sports += c["type"] == "sports"
        n_pm += c["type"] == "prediction"
        c["score"] = round(score(c) * 1000, 1)
        c["rationale"] = rationale(c)
        c["plain_bet"] = plain_bet(c)
        picked.append(c)
        if len(picked) == n:
            break
    return picked


# ------------------------------------------------------ congress money trail
THEMES = {
    "Defense": ("LMT RTX NOC GD LHX BA HII LDOS PLTR KTOS AVAV", "defense pentagon military missile ukraine nato war israel iran drone troops"),
    "Semiconductors": ("NVDA AMD AVGO INTC TSM QCOM MU ASML AMAT LRCX KLAC TXN ARM SMCI MRVL ON", "chip chips semiconductor export taiwan tariff china nvidia"),
    "Big Tech": ("AAPL MSFT GOOGL GOOG AMZN META NFLX ORCL CRM ADBE IBM", "antitrust ftc doj regulation app cloud privacy ai"),
    "Energy": ("XOM CVX COP OXY SLB EOG PSX MPC VLO HAL KMI ET", "oil opec gas energy drilling crude pipeline lng"),
    "Healthcare": ("PFE MRK LLY JNJ ABBV UNH BMY AMGN GILD CVS HUM MRNA NVO ELV CI", "fda drug medicare medicaid health vaccine pricing obesity insurer"),
    "Financials": ("JPM BAC WFC C GS MS SCHW BLK AXP V MA COF PYPL", "fed rate bank banks inflation regulation lending"),
    "Autos/EV": ("TSLA F GM RIVN LCID", "tariff auto autos musk tesla emissions"),
    "Crypto": ("COIN MSTR HOOD", "crypto bitcoin stablecoin sec"),
    "Airlines/Travel": ("DAL UAL AAL LUV BKNG", "airline airlines faa travel"),
    "Telecom/Media": ("T VZ TMUS DIS CMCSA WBD PARA", "fcc merger broadband media"),
}
TICKER_THEME = {t: name for name, (ts, _) in THEMES.items() for t in ts.split()}


def company(asset):
    n = (asset or "").split(" - ")[0]
    n = re.sub(r"\b(INC|CORP|CORPORATION|CO|LTD|PLC|HOLDINGS?|GROUP|CLASS [A-C]|COMMON STOCK|THE|N\.?V|S\.?A|COMPANY|INCORPORATED)\b\.?,?", "", n, flags=re.I)
    n = re.sub(r"[.,]+", " ", n)
    return re.sub(r"'S\b", "'s", re.sub(r"\s+", " ", n).strip().title())


def money_trail(trades, news_all, pm, km):
    heads = [h for cat in ("world", "us", "business", "tech") for h in news_all.get(cat, [])]
    by = defaultdict(list)
    for t in trades:
        if re.match(r"^[A-Z][A-Z.]{0,5}$", t.get("ticker") or ""):
            by[t["ticker"]].append(t)
    trails = []
    for tk, ts in by.items():
        name = company(ts[0]["asset"])
        theme = TICKER_THEME.get(tk)
        theme_words = set(THEMES[theme][1].split()) if theme else set()
        # company phrase: "first solar", "live nation", or a distinctive single word like "microsoft"
        nw = [w for w in name.lower().split() if w not in ("ordinary", "shares", "common", "class", "adr", "sponsored")]
        phrase = " ".join(nw[:2]) if len(nw) >= 2 else (nw[0] if nw else "")
        name_re = re.compile(r"\b%s\b" % re.escape(phrase), re.I) if len(phrase) >= 4 else None
        news_hits = []
        for h in heads:
            title = h["title"]
            direct = bool((name_re and name_re.search(title)) or (len(tk) >= 3 and re.search(r"\b%s\b" % re.escape(tk), title)))
            thematic = len(theme_words & set(words(title))) >= 2
            if direct or thematic:
                news_hits.append({"title": title, "link": h["link"], "source": h["source"], "published": h.get("published"),
                                  "kind": "direct" if direct else "sector"})
        mkts = []
        for m in pm + km:
            q = market_text(m).lower()
            if (name_re and name_re.search(q)) or (theme and len(theme_words & set(words(q))) >= 2):
                b = binary_yes(m)
                mkts.append({"venue": m["venue"], "question": market_text(m), "yes": round((b[2] if b else 0) * 100), "url": m["url"]})
        buys = [t for t in ts if "purchase" in t["type"].lower()]
        sells = [t for t in ts if "sale" in t["type"].lower()]
        members = sorted({t["member"] for t in ts})
        parties = sorted({t["party"] for t in ts if t["party"]})
        flags = []
        dates = sorted(t["traded"] for t in ts if t["traded"])
        if len(members) >= 2 and dates and (S.parse_dt(dates[-1]) - S.parse_dt(dates[0])).days <= 30:
            direction = "buying" if len(buys) >= len(sells) else "selling"
            flags.append("Cluster: %d members %s within 30 days" % (len(members), direction))
        late = [t for t in ts if t["traded"] and t["filed"] and (S.parse_dt(t["filed"]) - S.parse_dt(t["traded"])).days > 45]
        if late:
            flags.append("%d late disclosure(s) (>45 days, STOCK Act window)" % len(late))
        big = [t for t in ts if t["amount_high"] >= 250000]
        if big:
            flags.append("%d trade(s) ≥ $250K" % len(big))
        if len(parties) > 1:
            flags.append("Bipartisan interest")
        direct_news = [n for n in news_hits if n["kind"] == "direct"]
        if direct_news and dates:
            flags.append("Trades dated before %d current headline(s) naming the company" % len(direct_news))
        exr = [t["excess_return"] for t in ts if t.get("excess_return") is not None]
        vol = sum((t["amount_low"] + t["amount_high"]) / 2 for t in ts)
        score = (len(ts) * 1.0 + len(members) * 2 + len(direct_news) * 3 + min(6, len(news_hits)) + len(mkts) * 1.5
                 + len(late) * 2 + len(big) * 2 + (vol / 250000))
        trails.append({
            "ticker": tk, "company": name, "theme": theme or "", "trades": sorted(ts, key=lambda t: t["traded"], reverse=True),
            "n_trades": len(ts), "buys": len(buys), "sells": len(sells), "members": members, "parties": parties,
            "est_volume": vol, "avg_excess_return": round(sum(exr) / len(exr), 2) if exr else None,
            "news": news_hits[:6], "markets": mkts[:5], "flags": flags, "score": round(score, 1),
        })
    trails.sort(key=lambda t: t["score"], reverse=True)
    for t in trails[:12]:
        q = S.quote(t["ticker"], "6mo")
        if q and q.get("series"):
            first = min(x["traded"] for x in t["trades"] if x["traded"])
            p0 = S.price_on(q, first)
            t["price"] = round(q["price"], 2)
            t["move_since_first_trade"] = round((q["price"] / p0 - 1) * 100, 1) if p0 else None
            t["spark"] = [c for _, c in q["series"][-90:]]
    for t in trails:
        t["narrative"] = trail_story(t)
    return trails


def trail_story(t):
    s = "%d disclosed trade(s) in %s (%s) by %s" % (t["n_trades"], t["company"], t["ticker"],
                                                    ", ".join(t["members"][:3]) + (" +%d more" % (len(t["members"]) - 3) if len(t["members"]) > 3 else ""))
    s += " — %d buy / %d sell, ~$%s estimated." % (t["buys"], t["sells"], f"{t['est_volume']:,.0f}")
    if t.get("move_since_first_trade") is not None:
        s += " Stock %+.1f%% since the earliest trade." % t["move_since_first_trade"]
    if t["avg_excess_return"] is not None:
        s += " Avg excess return vs S&P since trade: %+.1f%%." % t["avg_excess_return"]
    direct = [n for n in t["news"] if n["kind"] == "direct"]
    if direct:
        s += " In today's news: \"%s\"." % direct[0]["title"][:110]
    elif t["news"]:
        s += " Sector-level coverage (%s): \"%s\"." % (t["theme"], t["news"][0]["title"][:110])
    if t["markets"]:
        s += " Linked prediction market: %s (%s YES %d¢)." % (t["markets"][0]["question"][:80], t["markets"][0]["venue"], t["markets"][0]["yes"])
    s += " These are correlations from public filings, not evidence of wrongdoing."
    return s


def member_board(trades):
    by = defaultdict(list)
    for t in trades:
        by[t["member"]].append(t)
    rows = []
    for m, ts in by.items():
        exr = [t["excess_return"] for t in ts if t.get("excess_return") is not None]
        rows.append({"member": m, "party": ts[0]["party"], "chamber": ts[0]["chamber"], "photo": ts[0].get("photo", ""),
                     "trades": len(ts), "volume": sum((t["amount_low"] + t["amount_high"]) / 2 for t in ts),
                     "avg_excess_return": round(sum(exr) / len(exr), 2) if exr else None,
                     "top_tickers": [k for k, _ in sorted(((k, sum(1 for t in ts if t["ticker"] == k)) for k in {t["ticker"] for t in ts} if re.match(r"^[A-Z][A-Z.]{0,5}$", k or "")), key=lambda x: -x[1])[:5]]})
    rows.sort(key=lambda r: r["volume"], reverse=True)
    return rows[:40]
