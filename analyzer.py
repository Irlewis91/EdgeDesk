"""Bet / parlay analyzer: score a bet somebody is thinking of placing.

Each leg is priced the same way the daily picks are (no-vig market anchor, tilted a little by the
team or player model), then compared with the price the user was actually offered on their
platform. The result is a 0-100 confidence score plus the numbers behind it.

  score 50  = a fair price, no edge either way
  above 50  = the price is better than our estimate of the true odds (scaled by how much data backs it)
  below 50  = the price is worse than the true odds; a typical sportsbook line with no edge lands near 32
"""
import math
import re
import statistics

import engine as E
import pricing as PR
import props as P
from history import implied

SPORTSBOOKS = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "BetRivers", "bet365", "Fanatics", "Hard Rock",
               "theScore Bet", "Bally Bet", "Bovada", "Pinnacle"]
EXCHANGES = ["Polymarket", "Kalshi"]
PICKEM = ["PrizePicks", "Underdog"]
PLATFORMS = SPORTSBOOKS + EXCHANGES + PICKEM + ["Other"]
# what a pick'em slip pays when every pick hits, if the user doesn't say (standard "power play" table)
PICKEM_PAYOUT = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 37.5}
SD_MARGIN = {"NFL": 13.5, "NCAAF": 16.0, "NBA": 12.0, "WNBA": 11.0, "MLB": 4.3, "NHL": 2.4}
SD_TOTAL = {"NFL": 10.0, "NCAAF": 13.0, "NBA": 18.0, "WNBA": 15.0, "MLB": 4.4, "NHL": 2.3}
HOLD = 0.045            # a sportsbook's usual margin per side when we can't price a leg ourselves
MAX_LEGS = 12
_N = statistics.NormalDist()


def american(d):
    if not d or d <= 1:
        return ""
    return "+%d" % round((d - 1) * 100) if d >= 2 else "-%d" % round(100 / (d - 1))


def parse_price(txt, platform=""):
    """'-110', '+150', '2.5' (decimal), '62c' / '0.62' (prediction-market price), '3x' (payout) -> decimal odds."""
    s = str(txt if txt is not None else "").strip().lower().replace("−", "-").replace(" ", "").replace(",", "")
    if not s:
        return None
    if s in ("even", "ev", "evens", "pk"):
        return 2.0
    p = None
    m = re.match(r"^(\d+(?:\.\d+)?)(¢|c|cents)$", s)
    if m:
        p = float(m.group(1)) / 100
    elif re.match(r"^0?\.\d+$", s):
        p = float(s)
    elif platform in EXCHANGES and re.match(r"^\d{1,2}$", s):
        p = int(s) / 100
    if p is not None:
        if not 0.01 <= p <= 0.99:
            return None
        return 1 / min(0.995, p + (E.kalshi_fee(p) if platform == "Kalshi" else 0))
    m = re.match(r"^(\d+(?:\.\d+)?)x$", s)
    if m:
        return float(m.group(1)) if float(m.group(1)) > 1 else None
    m = re.match(r"^([+-]?)(\d+(?:\.\d+)?)$", s)
    if not m:
        return None
    v = float(m.group(2))
    if v >= 100 and (m.group(1) or float(v).is_integer()):
        return E.dec(-v if m.group(1) == "-" else v)
    if not m.group(1) and 1.01 <= v < 100:
        return v
    return None


def _q(venue, odds=None, dec=None, price=None):
    d = dec if dec is not None else E.dec(odds)
    return {"venue": venue, "dec": d, "price": price or (E.fmt_ml(odds) if odds is not None else american(d))}


def _shift(p_ref, delta, sd):
    """Move a probability along a normal curve by `delta` points."""
    return _N.cdf(_N.inv_cdf(min(0.999, max(0.001, p_ref))) + delta / sd)


def _title(g):
    return "%s @ %s" % (g["away"]["display"], g["home"]["display"])


# ------------------------------------------------------------------ legs
def _moneyline(g, side):
    lines, cross = g.get("lines") or {}, g.get("cross") or {}
    quotes = [_q(b, x["ml_" + side]) for b, x in lines.items() if side != "draw" and x.get("ml_" + side) and PR.bettable(b)]
    for venue, key in (("Polymarket", "polymarket"), ("Kalshi", "kalshi")):
        c = (cross.get(key) or {}).get(side)
        if c and 0.03 < c < 0.97:
            d = 1 / (c + (E.kalshi_fee(c) if venue == "Kalshi" else 0))
            quotes.append(_q(venue, dec=d, price="%.0f¢ (%s)" % (c * 100, american(d))))
    name = "Draw" if side == "draw" else g[side]["display"] + " to win"
    est = PR.ml_estimate(g) if side != "draw" else None
    if est:
        fair = est["fair_home"] if side == "home" else 1 - est["fair_home"]
        p = est["p_home"] if side == "home" else 1 - est["p_home"]
        cap = 0.04 if fair >= 0.3 else 0.025
        p = min(max(p, fair - cap), fair + cap)
        _, bump = PR.public_note(g, "ml_" + side)
        conf = 50 + (8 if "Pinnacle" in est["src"] else 0) + min(10, 2 * (len(est["ests"]) - 1)) - 60 * est["spread"] + bump
        return {"label": name, "p": p, "push": 0.0, "fair": fair, "conf": max(10, min(90, conf)), "quotes": quotes,
                "basis": "No-vig market (%s) blended with %d model estimate%s" % (est["src"], len(est["ests"]) - 1, "" if len(est["ests"]) == 2 else "s")}
    # three-way markets (soccer) and games with no two-way price: Poisson score model, anchored to ESPN's three-way odds
    m, o = g.get("model") or {}, g.get("odds") or {}
    fair = None
    if o.get("ml_home") and o.get("ml_away") and o.get("ml_draw"):
        imp = {k: implied(o["ml_" + k]) for k in ("home", "away", "draw")}
        fair = imp[side] / sum(imp.values())
    pm = None
    if m.get("poisson"):
        from stats import poisson_grid
        grid = poisson_grid(m["lam_home"], m["lam_away"], 12)
        pm = sum(grid[i][j] for i in range(12) for j in range(12)
                 if (i > j if side == "home" else i < j if side == "away" else i == j))
    if fair is None and pm is None:
        return {"label": name, "quotes": quotes}
    p = PR.blend(fair, pm) if fair is not None else pm
    if fair is not None:
        p = min(max(p, fair - 0.04), fair + 0.04)
    return {"label": name + (" (90 minutes)" if g.get("sport") == "soccer" and side != "draw" else ""), "p": p, "push": 0.0, "fair": fair,
            "conf": 44 if fair is not None and pm is not None else 34, "quotes": quotes,
            "basis": "Three-way market odds" + (" blended with the expected-goals model" if pm is not None else "") if fair is not None else "Expected-goals model only"}


def _line_quotes(g, kind, side, line):
    out = []
    for b, x in (g.get("lines") or {}).items():
        if not PR.bettable(b):
            continue
        if kind == "spread" and x.get("spread_" + side) == line and x.get("spread_%s_odds" % side):
            out.append(_q(b, x["spread_%s_odds" % side]))
        if kind == "total" and x.get("total") == line and x.get(side + "_odds"):
            out.append(_q(b, x[side + "_odds"]))
    have = {q["venue"] for q in out}
    for a in g.get("alts") or []:
        if a["kind"] == kind and a["side"] == side and a["line"] == line and a.get("odds") and a["book"] not in have:
            out.append(_q(a["book"], a["odds"]))
            have.add(a["book"])
    return out


def _nearest_fair(g, kind, line):
    """No-vig probability (home covers / over) at the closest line any book posts as a two-sided market."""
    best = None
    for b, x in (g.get("lines") or {}).items():
        if b == "Opener":
            continue
        if kind == "spread":
            ref, a, c = x.get("spread_home"), x.get("spread_home_odds"), x.get("spread_away_odds")
        else:
            ref, a, c = x.get("total"), x.get("over_odds"), x.get("under_odds")
        if ref is None or not a or not c:
            continue
        key = (abs(ref - line), b != "Pinnacle")
        if best is None or key < best[0]:
            best = (key, ref, PR.novig(a, c), b)
    return best and best[1:]


def _spread_total(g, kind, side, line):
    m, lg = g.get("model") or {}, g["league"]
    home_line = line if side in ("home", "over") else -line
    ref_line = line if kind == "total" else home_line
    fair, src = PR.fair_line(g, kind, ref_line)
    exact = fair is not None
    if not exact:
        near = _nearest_fair(g, kind, ref_line)
        if near:
            nl, nf, nb = near
            if kind == "spread":
                fair = _shift(nf, ref_line - nl, m.get("margin_sd") or SD_MARGIN.get(lg, 12))
            else:
                fair = _shift(nf, nl - ref_line, m.get("total_sd") or SD_TOTAL.get(lg, 10))
            src = "%s's %s%g, moved to this line" % (nb, "" if kind == "total" else "home ", nl)
    mw, mp = PR.model_margin_prob(g, ref_line) if kind == "spread" else PR.model_total_prob(g, ref_line)
    if fair is None and mw is None:
        lab = _line_label(g, kind, side, line)
        return {"label": lab, "quotes": _line_quotes(g, kind, side, line)}
    mp = mp or 0.0
    model_np = None if mw is None else mw / max(1e-9, 1 - mp)
    if fair is None:
        p_np, basis, conf = model_np, "Team model only (no two-sided market found)", 32
    else:
        p_np = PR.blend(fair, model_np)
        p_np = min(max(p_np, fair - 0.04), fair + 0.04)
        basis = "No-vig market (%s)%s" % (src, " blended with the team model" if model_np is not None else "")
        conf = 48 + (8 if src and "Pinnacle" in src else 0) - (0 if exact else 8) - (0 if model_np is not None else 6)
        if kind == "spread":
            conf += PR.public_note(g, "spread_" + side)[1]
    if side in ("away", "under"):
        p_np = 1 - p_np
        fair = None if fair is None else 1 - fair
    return {"label": _line_label(g, kind, side, line), "p": p_np, "push": mp, "fair": fair, "conf": max(10, min(85, conf)),
            "quotes": _line_quotes(g, kind, side, line), "basis": basis,
            "notes": [] if exact or fair is None else ["No book in our data posts this exact line, so the market price was moved from the nearest one."]}


def _line_label(g, kind, side, line):
    if kind == "spread":
        return "%s %s%g" % (g[side]["display"], "+" if line > 0 else "", line) if line else "%s pick'em (spread 0)" % g[side]["display"]
    return "%s %g" % ("Over" if side == "over" else "Under", line)


def _prop_offers(g, player, stat, line=None):
    pn = P.norm_name(player)
    return [o for o in g.get("props") or [] if P.norm_name(o["player"]) == pn and P.canon(o["stat"]) == stat
            and (line is None or float(o["line"]) == line)]


def _prop(g, rows, player, stat, side, line):
    pn, lab = P.norm_name(player), P.LABEL.get(stat, stat)
    if stat.startswith("anytime"):
        label, side, line = "%s to score a %s" % (player, lab), "over", 0.5
    else:
        label = "%s %s %g %s" % (player, side.title(), line, lab)
    offers = _prop_offers(g, player, stat, line)
    quotes = [_q(o["book"], o[side]) for o in offers if o.get(side) and not o.get("dfs")]
    mine = [r for r in rows if P.norm_name(r["player"]) == pn and r["stat"] == stat and r["league"] == g["league"]]
    hit = next((r for r in mine if r["line"] == line and r["side"] == side), None)
    if hit:
        notes = ["Recent games: %s (season-weighted average %.1f)." % (", ".join("%g" % x for x in hit["recent"][-6:]), hit["mean"])]
        if hit.get("role_change"):
            notes.append("Our model and the market disagree sharply here, which usually means a role or injury change.")
        return {"label": label, "p": hit["p"], "push": 0.0, "fair": hit["p_fair"], "conf": hit["confidence"], "quotes": quotes,
                "basis": "No-vig market blended with the player's game-log model", "notes": notes}
    if mine:                                   # same player and stat, different line: re-run the distribution at this line
        ref = min(mine, key=lambda r: (abs(r["line"] - line), r["side"] != "over"))
        over = lambda r_, ln: P.poisson_over(r_["mean"], ln) if (stat in P.POISSON or r_["mean"] < 8) else P.normal_over(r_["mean"], r_["sd"], ln)
        tilt = (ref["p"] - ref["p_model_raw"]) * (1 if ref["side"] == "over" else -1)      # how far the market pulled the model
        p_over = min(0.97, max(0.03, over(ref, line) + tilt))
        two = next((o for o in offers if o.get("over") and o.get("under")), None)
        fair = None
        if two:
            fair = implied(two["over"]) / (implied(two["over"]) + implied(two["under"]))
            p_over = min(max((2 * fair + p_over) / 3, fair - 0.06), fair + 0.06)
        p = p_over if side == "over" else 1 - p_over
        return {"label": label, "p": p, "push": 0.0, "fair": None if fair is None else (fair if side == "over" else 1 - fair),
                "conf": max(10, ref["confidence"] - (4 if two else 12)), "quotes": quotes,
                "basis": "Player's game-log model at this line" + (", blended with the market" if two else ""),
                "notes": ["Season-weighted average %.1f; the nearest line we priced directly was %g." % (ref["mean"], ref["line"])]}
    two = next((o for o in offers if o.get("over") and o.get("under") and not o.get("dfs")), None)
    if two:
        fair = implied(two["over"]) / (implied(two["over"]) + implied(two["under"]))
        fair = fair if side == "over" else 1 - fair
        return {"label": label, "p": fair, "push": 0.0, "fair": fair, "conf": 36, "quotes": quotes,
                "basis": "No-vig market only (%s); not enough game history for the player model" % two["book"]}
    return {"label": label, "quotes": quotes}


def prop_menu(g):
    """Players, stats and posted lines for one game, for the form's dropdowns."""
    by = {}
    for o in g.get("props") or []:
        k = P.canon(o["stat"])
        if not k or o.get("line") is None:
            continue
        row = by.setdefault((o["player"], k), {"player": o["player"], "stat": k, "label": P.LABEL.get(k, k), "lines": {}})
        ln = row["lines"].setdefault(float(o["line"]), {})
        for side in ("over", "under"):
            if o.get(side) and not o.get("dfs"):
                ln.setdefault(side, {})[o["book"]] = E.fmt_ml(o[side])
    out = []
    for row in by.values():
        row["lines"] = [{"line": l, **v} for l, v in sorted(row["lines"].items())]
        out.append(row)
    return sorted(out, key=lambda r: (r["player"], r["stat"]))


# --------------------------------------------------------------- scoring
def score_of(ev, conf, n=1, unpriced=0, correlated=False):
    """0-100. 50 is a fair price. Edge in your favour counts for more when more data backs the estimate;
    edge against you (the book's margin) counts in full. Parlay edge is scaled by sqrt(legs)."""
    v = ev * 100 * 4 / math.sqrt(max(1, n))
    if v > 0:
        v *= 0.6 + conf / 250.0
    s = 50 + max(-42, min(40, v)) - 3 * unpriced - (6 if correlated else 0)
    return int(round(max(1, min(99, s))))


def grade(s):
    return "Strong" if s >= 70 else "Good" if s >= 58 else "Fair" if s >= 42 else "Weak" if s >= 30 else "Poor"


def _leg(raw, ctx, platform):
    market = str(raw.get("market") or "other")
    g = ctx["games"].get(str(raw.get("game") or ""))
    desc = str(raw.get("description") or "").strip()[:140]
    ev_ = {"quotes": [], "label": desc or "Bet"}
    try:
        line = float(raw["line"]) if str(raw.get("line", "")).strip() not in ("", "None") else None
    except (TypeError, ValueError):
        line = None
    side = str(raw.get("side") or "")
    if g and market == "moneyline" and side in ("home", "away", "draw"):
        ev_ = _moneyline(g, side)
    elif g and market in ("spread", "total") and line is not None and side in (("home", "away") if market == "spread" else ("over", "under")):
        ev_ = _spread_total(g, market, side, line)
    elif g and market == "prop" and raw.get("player") and raw.get("stat") and (line is not None or str(raw["stat"]).startswith("anytime")):
        ev_ = _prop(g, ctx["props"], str(raw["player"]), str(raw["stat"]), side if side in ("over", "under") else "over", line)
    out = {"label": ev_.get("label") or desc or "Bet", "game": _title(g) if g else "", "league": g["league"] if g else "",
           "starts": g["date"] if g else None, "game_id": g["id"] if g else None, "notes": list(ev_.get("notes") or []),
           "basis": ev_.get("basis"), "conf": ev_.get("conf")}
    quotes = sorted(ev_.get("quotes") or [], key=lambda q: -q["dec"])
    dec = parse_price(raw.get("odds"), platform)
    here = next((q for q in quotes if q["venue"] == platform), None)
    if raw.get("odds") and dec is None:
        out["notes"].append("Couldn't read the odds “%s”. Use American odds such as -110 or +150." % str(raw["odds"])[:20])
    if dec is None and here and platform not in PICKEM:
        dec = here["dec"]
        out["notes"].append("No odds entered, so %s's current price in our data (%s) was used." % (platform, here["price"]))
    elif dec and here and abs(1 / dec - 1 / here["dec"]) > 0.012:
        out["notes"].append("Our latest data shows %s at %s for this; lines move, so check your slip." % (platform, here["price"]))
    out["dec"], out["price"] = dec, (american(dec) if dec else "")
    priced = ev_.get("p") is not None
    if priced:
        out["p"], out["fair"] = ev_["p"], ev_.get("fair")
    elif dec:
        out["p"], out["conf"] = (1 / dec) / (1 + HOLD), 25
        out["basis"] = "Not priced by our models: assumed to be a typical sportsbook price"
        out["notes"].append("We couldn't match this to a game and market we track, so it is treated as an ordinary line with the book's usual margin.")
    else:
        out["p"] = None
    out["priced"] = priced
    if out["p"] is not None:
        out["fair_price"] = american(1 / out["p"])
        if dec:
            out["ev"] = out["p"] * dec - 1
            out["score"] = score_of(out["ev"], out["conf"] or 25, 1, 0 if priced else 1)
    best = quotes[0] if quotes else None
    if best and dec and best["venue"] != platform and best["dec"] > dec * 1.012:
        out["best"] = {"venue": best["venue"], "price": best["price"]}
    if g and (E.hours_until(g["date"]) or 0) < 0:
        out["notes"].append("This game has already started; the prices we hold are from before kick-off.")
    return out


def analyze(payload, ctx):
    platform = str(payload.get("platform") or "Other")
    if platform not in PLATFORMS:
        platform = "Other"
    raw = [l for l in (payload.get("legs") or []) if isinstance(l, dict)][:MAX_LEGS]
    if not raw:
        return {"error": "Add at least one bet."}
    legs = [_leg(l, ctx, platform) for l in raw]
    n, notes = len(legs), []
    slip = parse_price(payload.get("slip_odds"), platform)
    if payload.get("slip_odds") and slip is None:
        notes.append("Couldn't read the total odds “%s”, so they were worked out from the legs." % str(payload["slip_odds"])[:20])
    if platform in PICKEM:
        if n < 2:
            return {"error": "%s entries need at least two picks. Add another pick, or choose a sportsbook." % platform, "legs": legs}
        if slip is None:
            slip = PICKEM_PAYOUT.get(n)
            if slip is None:
                return {"error": "Enter what the slip pays (for example 25x) for a %d-pick entry." % n, "legs": legs}
            notes.append("No payout entered, so the standard %gx for a %d-pick entry was assumed. Enter your slip's payout to be exact." % (slip, n))
        for l in legs:                               # no per-pick odds on pick'em: share the payout evenly
            if l["p"] is None and not l["dec"]:
                l["dec"] = slip ** (1.0 / n)
                l["p"], l["conf"], l["priced"] = (1 / l["dec"]) / (1 + HOLD), 25, False
                l["basis"] = "Not priced by our models: assumed to be a typical pick'em line"
                l["fair_price"] = american(1 / l["p"])
    missing = [l["label"] for l in legs if l["p"] is None]
    if missing:
        return {"error": "Enter the odds for: %s." % "; ".join(missing), "legs": legs}
    p = 1.0
    for l in legs:
        p *= l["p"]
    if slip is None:
        need = [l["label"] for l in legs if not l["dec"]]
        if need:
            return {"error": "Enter your odds for: %s.%s" % ("; ".join(need), " Or enter the total odds from your slip." if n > 1 else ""), "legs": legs}
        dec = 1.0
        for l in legs:
            dec *= l["dec"]
    else:
        dec = slip
    games = [l["game_id"] for l in legs if l["game_id"]]
    same_game = n > 1 and len(set(games)) < len(games)
    if same_game:
        notes.append("Two or more legs are from the same game. Their results move together, so the true chance differs from simply "
                     "multiplying the legs, and books price same-game parlays lower. "
                     + ("Your slip's total odds were used for the price." if slip else "Enter the total odds from your slip for an accurate score."))
    unpriced = sum(1 for l in legs if not l["priced"])
    conf = max(10, sum(l["conf"] or 25 for l in legs) / n - 3 * (n - 1))
    ev = p * dec - 1
    score = score_of(ev, conf, n, unpriced, same_game and not slip)
    gr = grade(score)
    kind = "bet" if n == 1 else "%d-leg parlay" % n
    if ev > 0.01:
        verdict = "The price beats our estimate of the true odds: fair would be about %s and you are getting %s." % (american(1 / p), american(dec))
    elif ev > -0.02:
        verdict = "Close to a fair price. There is no real edge either way at %s (fair is about %s)." % (american(dec), american(1 / p))
    else:
        verdict = "The price is short of the true odds: fair would be about %s, and you are getting %s." % (american(1 / p), american(dec))
    if n > 1:
        verdict += " All %d legs have to win, which we put at %.1f%%." % (n, p * 100)
    res = {"platform": platform, "kind": kind, "score": score, "grade": gr, "verdict": verdict, "p": p, "dec": dec,
           "price": american(dec), "fair_price": american(1 / p), "ev": ev, "data_confidence": round(conf),
           "legs": legs, "notes": notes, "same_game": same_game, "unpriced": unpriced}
    better = [l for l in legs if l.get("best")]
    if better and n == 1:
        notes.append("Better price available: %s at %s." % (better[0]["best"]["price"], better[0]["best"]["venue"]))
    if ev > 0:
        k = E.kelly(p, dec, frac=0.25 if n == 1 else 0.15, cap=0.03 if n == 1 else 0.01)
        res["stake_pct"] = k
    try:
        stake = float(str(payload.get("stake") or "").replace("$", "").replace(",", ""))
    except ValueError:
        stake = 0
    if stake > 0:
        res["stake"] = stake
        res["payout"] = stake * dec
        res["expected"] = stake * ev
    return res
