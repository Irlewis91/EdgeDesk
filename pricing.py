"""Pricing: merge every book onto each ESPN game, then price moneylines, spreads,
totals, alternate lines and player props against the statistical models.

Fair price anchor = Pinnacle no-vig when available, else the median no-vig across
all books. Model probability = market anchor (weight 2) blended with the team stats
model, ESPN's predictor, historical estimates and the coach adjustment. Every bet is
shopped across all books for the best price.
"""
import math
import statistics
from collections import defaultdict

import engine as E
import props as P
import sources as S
from history import implied

SPORT_BOOK_ORDER = ["Pinnacle", "Consensus", "DraftKings", "FanDuel", "BetMGM", "Caesars", "BetRivers", "bet365",
                    "Fanatics", "Hard Rock", "theScore Bet", "Bally Bet", "Bovada", "Opener"]


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def novig(a, b):
    if a is None or b is None:
        return None
    ia, ib = implied(a), implied(b)
    return ia / (ia + ib)


def dec(ml):
    return E.dec(ml)


REFERENCE_ONLY = {"Opener", "Consensus"}      # not bettable: opening line / market average


def bettable(book):
    return book.replace(" alt", "") not in REFERENCE_ONLY


# ---------------------------------------------------------------- merging
def _same(bg, g):
    gd, bd = S.parse_dt(g["date"]), S.parse_dt(bg.get("start") if isinstance(bg.get("start"), str) else None)
    if bd is None and isinstance(bg.get("start"), (int, float)):
        from datetime import datetime, timezone
        bd = datetime.fromtimestamp(bg["start"] / 1000, timezone.utc)
    if gd and bd and abs((gd - bd).total_seconds()) > 30 * 3600:
        return False
    return E.team_hit(bg["home"], g["home"]) and E.team_hit(bg["away"], g["away"])


def merge(games, book_sets):
    pool = [bg for rows in book_sets.values() for bg in rows]
    for g in games:
        g["lines"], g["alts"], g["props"], g["public"] = {}, [], [], {}
        for bg in pool:
            if bg["league"] not in (g["league"],) and not (g["sport"] == "soccer" and bg["league"] in ("EPL", "MLS")):
                continue
            if not _same(bg, g):
                continue
            g["lines"].update(bg["books"])
            g["alts"] += bg["alts"]
            g["props"] += bg["props"]
            if bg["public"]:
                g["public"] = bg["public"]
        # ESPN's DraftKings line as a fallback book
        o = g["odds"]
        if o.get("ml_home") and "DraftKings" not in g["lines"]:
            g["lines"]["DraftKings"] = {"ml_home": o["ml_home"], "ml_away": o["ml_away"]}
    return games


# ------------------------------------------------------------ fair prices
def fair_ml(g):
    L = g["lines"]
    if g["sport"] == "soccer":
        return None                       # 3-way: handled via ESPN draw odds in engine path
    pin = L.get("Pinnacle", {})
    if pin.get("ml_home") and pin.get("ml_away"):
        return novig(pin["ml_home"], pin["ml_away"]), "Pinnacle no-vig"
    vals = [novig(b["ml_home"], b["ml_away"]) for n, b in L.items() if n != "Opener" and b.get("ml_home") and b.get("ml_away")]
    vals = [v for v in vals if v]
    return (statistics.median(vals), "median of %d books" % len(vals)) if vals else None


def fair_line(g, kind, line):
    """No-vig P(home covers `line`) or P(over `line`) at that exact number.

    Order of trust: Pinnacle's main line, Pinnacle's alternate pair, then the other books' main lines,
    but only when at least two of them post this number (or it is the only number anyone posts). A single
    book sitting on an old number is a stale line, not a fair price; trusting it made every better
    alternate price at that number look like a huge edge."""
    L = g["lines"]

    def main_pair(x):
        if kind == "spread" and x.get("spread_home") == line and x.get("spread_home_odds") and x.get("spread_away_odds"):
            return novig(x["spread_home_odds"], x["spread_away_odds"])
        if kind == "total" and x.get("total") == line and x.get("over_odds") and x.get("under_odds"):
            return novig(x["over_odds"], x["under_odds"])
        return None

    def alt_pair(book):
        if kind == "spread":
            a_ = next((a for a in g["alts"] if a["book"] == book and a["kind"] == "spread" and a["side"] == "home" and a["line"] == line and a.get("odds")), None)
            b_ = next((a for a in g["alts"] if a["book"] == book and a["kind"] == "spread" and a["side"] == "away" and a["line"] == -line and a.get("odds")), None)
        else:
            a_ = next((a for a in g["alts"] if a["book"] == book and a["kind"] == "total" and a["side"] == "over" and a["line"] == line and a.get("odds")), None)
            b_ = next((a for a in g["alts"] if a["book"] == book and a["kind"] == "total" and a["side"] == "under" and a["line"] == line and a.get("odds")), None)
        return novig(a_["odds"], b_["odds"]) if a_ and b_ else None

    p = main_pair(L.get("Pinnacle", {}))
    if p is not None:
        return p, "Pinnacle"
    p = alt_pair("Pinnacle")
    if p is not None:
        return p, "Pinnacle alt"
    key = "spread_home" if kind == "spread" else "total"
    others = {b: x for b, x in L.items() if b not in ("Pinnacle", "Opener") and x.get(key) is not None}
    at = [(b, main_pair(x)) for b, x in others.items()]
    at = [(b, v) for b, v in at if v is not None]
    if at and (len(at) >= 2 or len({x[key] for x in others.values()}) == 1):
        return statistics.median(v for _, v in at), (at[0][0] if len(at) == 1 else "median of %d books" % len(at))
    p = alt_pair("Bovada")
    if p is not None:
        return p, "Bovada alt"
    return None, None


# ----------------------------------------------------------- model probs
def model_margin_prob(g, home_line):
    """P(home margin + home_line > 0), P(push). Uses Poisson grid or normal."""
    m = g.get("model") or {}
    if m.get("poisson"):
        from stats import poisson_grid
        grid = poisson_grid(m["lam_home"], m["lam_away"], 16)
        win = push = 0.0
        for i in range(16):
            for j in range(16):
                d = i - j + home_line
                if d > 0:
                    win += grid[i][j]
                elif d == 0:
                    push += grid[i][j]
        return win, push
    if m.get("margin_mu") is None:
        return None, None
    mu = m["margin_mu"] + (m.get("coach_adj", 0) * m["margin_sd"] / 0.4)
    sd = m["margin_sd"]
    if float(home_line).is_integer():
        win = 1 - ncdf((-home_line + 0.5 - mu) / sd)
        push = ncdf((-home_line + 0.5 - mu) / sd) - ncdf((-home_line - 0.5 - mu) / sd)
    else:
        win, push = 1 - ncdf((-home_line - mu) / sd), 0.0
    return win, push


def model_total_prob(g, line):
    m = g.get("model") or {}
    if m.get("poisson"):
        from stats import poisson_grid
        grid = poisson_grid(m["lam_home"], m["lam_away"], 20)
        over = sum(grid[i][j] for i in range(20) for j in range(20) if i + j > line)
        push = sum(grid[i][j] for i in range(20) for j in range(20) if i + j == line)
        return over, push
    if not m.get("total_mu"):
        return None, None
    mu, sd = m["total_mu"], m.get("total_sd", 10)
    if float(line).is_integer():
        return 1 - ncdf((line + 0.5 - mu) / sd), ncdf((line + 0.5 - mu) / sd) - ncdf((line - 0.5 - mu) / sd)
    return 1 - ncdf((line - mu) / sd), 0.0


def blend(fair, model, w=0.8):
    if model is None:
        return fair
    if fair is None:
        return None
    return (3 * fair + w * model) / (3 + w)


def sane_prices(prices, fair, tol=0.04, gap=0.025):
    """Drop prices that are too good to be real. prices: [(venue, implied probability)].

    Two tests, and no venue is exempt (Pinnacle used to be, so a Pinnacle price measured against some
    other book's stale number always survived and became the "best price"):
      1. nothing more than `tol` better than the no-vig fair price;
      2. the best price may not beat fair while standing more than `gap` clear of every other venue.
         One book alone on a number is a stale or mismatched line, not an edge."""
    kept = sorted(((v, p) for v, p in prices if p >= fair - tol), key=lambda x: x[1])
    while len(kept) >= 2 and kept[0][1] < fair and kept[1][1] - kept[0][1] > gap:
        kept.pop(0)
    ok = {v for v, _ in kept}
    return [(v, p) for v, p in prices if v in ok]


def drop_stale(quotes, fair=None, tol=0.04):
    """Remove prices far better than the rest of the market — usually stale lines that get cancelled or moved.
    quotes: [(book, american_odds)]"""
    quotes = [(b, o) for b, o in quotes if o]
    if not quotes:
        return quotes
    ref = fair if fair is not None else statistics.median(implied(o) for _, o in quotes)
    ok = {b for b, _ in sane_prices([(b, implied(o)) for b, o in quotes], ref, tol)}
    return [(b, o) for b, o in quotes if b in ok]


def ev_of(p_win, p_push, odds):
    return p_win * (dec(odds) - 1) - (1 - p_win - p_push)


def public_note(g, key):
    pb = g.get("public", {}).get(key)
    if not pb:
        return None, 0
    diff = pb["money"] - pb["tickets"]
    if diff >= 10:
        return "Sharp-money signal: %d%% of money on %d%% of tickets" % (pb["money"], pb["tickets"]), 4
    if diff <= -10:
        return "Public-heavy side: %d%% of tickets but only %d%% of money" % (pb["tickets"], pb["money"]), -3
    return "Public split: %d%% tickets / %d%% money" % (pb["tickets"], pb["money"]), 0


# -------------------------------------------------------------- candidates
def _base(g, market, pick, side_name, opp_name):
    m = g.get("model") or {}
    return {"type": "sports", "market": market, "league": g["league"], "title": "%s @ %s" % (g["away"]["display"], g["home"]["display"]),
            "weather": (g.get("weather") or {}).get("summary"), "schemes": m.get("schemes"), "trend": m.get("trend"),
            "pick": pick, "side_name": side_name, "opp_name": opp_name, "starts": g["date"], "research": g.get("research_link"),
            "keywords": [g["home"]["name"], g["away"]["name"]], "news_keys": [g["home"]["display"], g["away"]["display"]],
            "model_components": m.get("components", []), "coaches": m.get("coaches"), "tactics": m.get("tactics"),
            "game_id": g["id"]}


def line_shop(g, key_home, key_away):
    return [(b, x.get(key_home), x.get(key_away)) for b, x in g["lines"].items() if x.get(key_home) is not None]


def ml_estimate(g):
    """Every estimate of P(home wins) and their weighted blend. None when there is no two-way market."""
    fm = fair_ml(g)
    if not fm:
        return None
    fair_home, fair_src = fm
    m = g.get("model") or {}
    ests = [("Market (%s)" % fair_src, fair_home, 3.0)]
    if g.get("predictor_home"):
        ests.append(("ESPN Matchup Predictor", g["predictor_home"], 0.5 if g["league"] == "NCAAF" else 0.7))
    if m.get("p_home_model") is not None:
        pm_ = m["p_home_model"] + m.get("coach_adj", 0)
        ests.append(("Team stats model" + (" + coach" if m.get("coach_adj") else ""), pm_, 0.9 if g["league"] in ("NFL", "MLB") else 0.6))
    for e in (g.get("history") or {}).get("estimates", []):
        ests.append((e["source"], e["p_home"], e["weight"] * 0.6))
    wsum = sum(w for *_, w in ests)
    return {"fair_home": fair_home, "src": fair_src, "ests": ests, "p_home": sum(p * w for _, p, w in ests) / wsum,
            "spread": max(p for _, p, _ in ests) - min(p for _, p, _ in ests)}


def moneyline(g, pm_game, k_game):
    """Moneyline candidates using every book + Polymarket + Kalshi."""
    out = []
    est = ml_estimate(g)
    if not est:
        return out
    fair_home, fair_src, ests, p_home, spread_models = est["fair_home"], est["src"], est["ests"], est["p_home"], est["spread"]
    for side in ("home", "away"):
        p = p_home if side == "home" else 1 - p_home
        quotes = [(b, x.get("ml_" + side)) for b, x in g["lines"].items() if x.get("ml_" + side) and bettable(b)]
        fair_side = fair_home if side == "home" else 1 - fair_home
        venues = [{"venue": b, "price": E.fmt_ml(o), "dec": dec(o), "url": g["url"]} for b, o in quotes]
        if pm_game and 0.03 < pm_game[side] < 0.97:
            venues.append({"venue": "Polymarket", "price": "%.0f¢" % (pm_game[side] * 100), "dec": 1 / pm_game[side], "url": pm_game["m"]["url"]})
        if k_game and 0.03 < k_game[side]["yes_ask"] < 0.97:
            c = k_game[side]["yes_ask"]
            venues.append({"venue": "Kalshi", "price": "%.0f¢" % (c * 100), "dec": 1 / (c + E.kalshi_fee(c)), "url": k_game[side]["url"]})
        ok = {v for v, _ in sane_prices([(v["venue"], 1 / v["dec"]) for v in venues], fair_side, 0.035)}
        venues = [v for v in venues if v["venue"] in ok]
        if not venues:
            continue
        best = max(venues, key=lambda v: v["dec"])
        # models may tilt the market, but only a little (and less for longshots)
        cap = 0.04 if fair_side >= 0.3 else 0.025
        p = min(max(p, fair_side - cap), fair_side + cap)
        ev = p * best["dec"] - 1
        if ev <= 0.01 or p < 0.25 or 1 / best["dec"] < 0.2:
            continue
        note, bump = public_note(g, "ml_" + side)
        conf = 50 + (8 if "Pinnacle" in fair_src else 0) + min(10, 2 * (len(ests) - 1)) - 60 * spread_models + bump
        team, opp = g[side], g["away" if side == "home" else "home"]
        c = _base(g, "moneyline", "%s moneyline" % team["display"], team["display"], opp["display"])
        c.update({"id": "ml-%s-%s" % (g["id"], side), "venue": best["venue"], "price": best["price"], "url": best["url"],
                  "dec": best["dec"], "all_prices": sorted(venues, key=lambda v: -v["dec"]), "p_model": p, "p_market": fair_side,
                  "edge": p - 1 / best["dec"], "ev": ev, "confidence": max(10, min(90, conf)), "kelly": E.kelly(p, best["dec"]),
                  "factors": [{"label": n, "value": "%.1f%%" % ((pp if side == "home" else 1 - pp) * 100), "note": ""} for n, pp, _ in ests],
                  "public": note, "context": E.history_notes(g, side, g.get("history") or {})})
        out.append(c)
    return out


def spreads_totals(g):
    """Main spreads/totals across all books, plus every alternate line."""
    out = []
    m = g.get("model") or {}
    if not m:
        return out
    offers = defaultdict(list)   # (kind, side, line) -> [(book, odds)]
    for b, x in g["lines"].items():
        if b == "Opener":
            continue
        if x.get("spread_home") is not None:
            offers[("spread", "home", x["spread_home"])].append((b, x.get("spread_home_odds")))
            offers[("spread", "away", x.get("spread_away"))].append((b, x.get("spread_away_odds")))
        if x.get("total") is not None:
            offers[("total", "over", x["total"])].append((b, x.get("over_odds")))
            offers[("total", "under", x["total"])].append((b, x.get("under_odds")))
    main = {k for k in offers}
    for a in g["alts"]:
        if a["line"] is None or a["odds"] is None:
            continue
        key = (a["kind"], a["side"], a["line"])
        offers[key].append((a["book"] + (" alt" if key not in main else ""), a["odds"]))
    for (kind, side, line), quotes in offers.items():
        quotes = [(b, o) for b, o in quotes if o and bettable(b)]
        if line is None or not quotes:
            continue
        worst = {}                                   # a book quoting one selection twice has two different markets
        for b, o in quotes:                          # mixed together; keep its lower price so neither can mislead
            k = b.replace(" alt", "")
            if k not in worst or dec(o) < dec(worst[k][1]):
                worst[k] = (b, o)
        quotes = list(worst.values())
        if kind == "spread":
            home_line = line if side == "home" else -line
            fair_home, src = fair_line(g, "spread", home_line)
            mw, mp = model_margin_prob(g, home_line)
            if mw is None:
                continue
            if side == "away":
                mw = 1 - mw - mp
                fair = None if fair_home is None else 1 - fair_home
            else:
                fair = fair_home
        else:
            fair_over, src = fair_line(g, "total", line)
            mw, mp = model_total_prob(g, line)
            if mw is None:
                continue
            if side == "under":
                mw = 1 - mw - mp
                fair = None if fair_over is None else 1 - fair_over
            else:
                fair = fair_over
        if fair is None:
            continue
        # fair excludes pushes; scale model win into the same no-push space before blending
        p_np = blend(fair, mw / max(1e-9, 1 - mp))
        p_win = p_np * (1 - mp)
        quotes = drop_stale(quotes, fair * (1 - mp), 0.035)
        if not quotes:
            continue
        cap = 0.04 if 0.3 <= fair <= 0.7 else 0.025      # the model may tilt the market less at long or short prices
        p_np = min(max(p_np, fair - cap), fair + cap)
        p_win = p_np * (1 - mp)
        book, odds = max(quotes, key=lambda q: dec(q[1]))
        ev = ev_of(p_win, mp, odds)
        is_alt = "alt" in book or (kind, side, line) not in main
        if ev <= 0.015 or p_win < 0.25 or implied(odds) < 0.22:
            continue
        team = g[side] if kind == "spread" else None
        if kind == "spread":
            pick = "%s %s%g" % (team["display"], "+" if line > 0 else "", line) if line else "%s pick'em (spread 0)" % team["display"]
            side_name, opp_name = team["display"], g["away" if side == "home" else "home"]["display"]
        else:
            pick = "%s %g total points" % ("Over" if side == "over" else "Under", line)
            side_name, opp_name = pick, ""
        note, bump = public_note(g, "spread_" + side) if kind == "spread" else (None, 0)
        conf = 48 + (8 if src and "Pinnacle" in src else 0) + bump - (4 if is_alt else 0)
        c = _base(g, ("alt_" if is_alt else "") + kind, pick, side_name, opp_name)
        c.update({"id": "%s-%s-%s-%s" % (kind, g["id"], side, line), "venue": book.replace(" alt", ""), "price": E.fmt_ml(odds),
                  "url": g["url"], "dec": dec(odds), "line": line, "side": side,
                  "all_prices": [{"venue": b.replace(" alt", ""), "price": E.fmt_ml(o), "dec": dec(o), "url": g["url"]} for b, o in sorted(quotes, key=lambda q: -dec(q[1]))][:8],
                  "p_model": p_win, "p_push": mp, "p_market": fair * (1 - mp), "edge": p_win - implied(odds), "ev": ev,
                  "confidence": max(10, min(85, conf)), "kelly": E.kelly(p_win / max(1e-9, 1 - mp), dec(odds)),
                  "factors": [{"label": "Market (%s)" % src, "value": "%.1f%%" % (fair * 100), "note": "no-vig at this exact line"},
                              {"label": "Stats model", "value": "%.1f%%" % (mw / max(1e-9, 1 - mp) * 100),
                               "note": ("projected margin %+.1f" % m["margin_mu"]) if kind == "spread" and m.get("margin_mu") is not None
                               else ("projected total %.1f" % m["total_mu"]) if m.get("total_mu") else "Poisson model"}],
                  "public": note, "context": m.get("components", [])[:3]})
        out.append(c)
    return out


PROP_ROWS = []


def prop_candidates(games):
    offers = [dict(g, props=g["props"]) for g in games if g.get("props")]
    rows = P.price_props([{"league": g["league"], "home": g["home"]["display"], "away": g["away"]["display"],
                           "start": g["date"], "props": g["props"],
                           "pass_factor": ((g.get("weather") or {}).get("effect") or {}).get("pass_factor", 1.0),
                           "weather_note": ((g.get("weather") or {}).get("effect") or {}).get("note")} for g in offers], [])
    by_game = {(g["home"]["display"], g["away"]["display"]): g for g in offers}
    out = []
    PROP_ROWS[:] = rows                            # every priced prop, good or bad, for the bet analyzer
    for r in rows:
        if r["ev"] <= 0.02:
            continue
        g = by_game.get((r["home"], r["away"]))
        if r["stat"] in ("anytime_td", "anytime_goal"):
            pick = "%s to score a %s" % (r["player"], r["stat_label"])
        else:
            pick = "%s %s %g %s" % (r["player"], r["side"].upper(), r["line"], r["stat_label"])
        opp_txt = (" vs %s defense (x%.2f)" % (r["opp"], r["opp_factor"])) if r.get("opp") and r["opp_factor"] != 1 else ""
        ctx = ["Season-weighted average %.1f (sd %.1f) over %d games%s" % (r["mean"], r["sd"], r["n"], opp_txt),
               "Went over %g in %d of the last %d: %s" % (r["line"], round(r["hit_rate"] * len(r["recent"])), len(r["recent"]), ", ".join(str(x) for x in r["recent"]))]
        if r.get("season_avg") is not None and r.get("trend"):
            ctx.append("Current: %.1f per game this season, %s" % (r["season_avg"], r["trend"]))
        if r.get("last_season_avg") is not None:
            ctx.append("Last season average %.1f" % r["last_season_avg"])
        if r.get("vs_opp"):
            ctx.append("Head-to-head vs %s: %s (over this line in %d of %d)" % (r["opp"], ", ".join(str(x) for x in r["vs_opp"]), r.get("vs_opp_over", 0), r.get("vs_opp_n", len(r["vs_opp"]))))
        if r.get("vs_coach") and r.get("opp_coach"):
            ctx.append("Vs head coach %s: %s (avg %.1f, over this line in %d of %d)" % (r["opp_coach"], ", ".join(str(x) for x in r["vs_coach"]), sum(r["vs_coach"]) / len(r["vs_coach"]), r.get("vs_coach_over", 0), r.get("vs_coach_n", len(r["vs_coach"]))))
        if r.get("weather_note") and r.get("weather_factor", 1) != 1:
            ctx.append(r["weather_note"])
        if r.get("popularity"):
            ctx.append("PrizePicks popularity: %s picks on this player prop" % f"{int(r['popularity']):,}")
        if r.get("role_change"):
            ctx.append("Model and market disagree sharply — likely a role/injury change; model down-weighted")
        out.append({"type": "sports", "market": "alt_prop" if r.get("is_alt") else "prop", "league": r["league"], "id": "prop-%s-%s-%s-%s" % (r["player"], r["stat"], r["line"], r["side"]),
                    "title": "%s @ %s" % (r["away"], r["home"]), "pick": pick, "player": r["player"], "stat": r["stat"],
                    "stat_label": r["stat_label"], "line": r["line"], "side": r["side"], "side_name": r["player"], "opp_name": "",
                    "venue": r["book"], "price": E.fmt_ml(r["odds"]), "dec": dec(r["odds"]), "url": (g or {}).get("url", ""),
                    "all_prices": [{"venue": b, "price": E.fmt_ml(o), "dec": dec(o), "url": ""} for b, o in sorted(r["all_quotes"], key=lambda q: -dec(q[1]))],
                    "p_model": r["p"], "p_market": r["p_fair"], "edge": r["p"] - implied(r["odds"]), "ev": r["ev"],
                    "confidence": r["confidence"], "kelly": E.kelly(r["p"], dec(r["odds"]), frac=0.2, cap=0.02), "starts": r["start"],
                    "keywords": [r["player"]], "news_keys": [r["player"]],
                    "factors": [{"label": "Market (%s no-vig)" % ("Pinnacle" if r["sharp_ref"] else "book"), "value": "%.1f%%" % (r["p_fair"] * 100), "note": ""},
                                {"label": "Player stat model", "value": "%.1f%%" % (r["p_model_raw"] * 100), "note": "%s distribution" % ("Poisson" if r["stat"] in P.POISSON or r["mean"] < 8 else "normal")}],
                    "context": ctx, "recent": r["recent"], "mean": r["mean"],
                    "h2h": {"opp": r.get("opp"), "vs_opp": r.get("vs_opp") or [], "vs_opp_n": r.get("vs_opp_n", 0), "vs_opp_over": r.get("vs_opp_over", 0),
                            "coach": r.get("opp_coach"), "vs_coach": r.get("vs_coach") or [], "vs_coach_n": r.get("vs_coach_n", 0),
                            "vs_coach_over": r.get("vs_coach_over", 0), "season_avg": r.get("season_avg"), "trend": r.get("trend")}})
    return out


def line_board(g):
    """Best available price per market for the line-shopping table."""
    rows = []
    for key, label in (("ml_home", g["home"]["abbr"] + " ML"), ("ml_away", g["away"]["abbr"] + " ML")):
        q = [(b, x[key]) for b, x in g["lines"].items() if x.get(key)]
        if q:
            b, o = max(q, key=lambda t: dec(t[1]))
            rows.append({"market": label, "best_book": b, "best": E.fmt_ml(o), "books": {bb: E.fmt_ml(oo) for bb, oo in q}})
    for side in ("home", "away"):
        q = [(b, x.get("spread_" + side), x.get("spread_%s_odds" % side)) for b, x in g["lines"].items() if x.get("spread_" + side) is not None and x.get("spread_%s_odds" % side)]
        if q:
            b, ln, o = max(q, key=lambda t: (t[1], dec(t[2])))
            rows.append({"market": "%s spread" % g[side]["abbr"], "best_book": b, "best": "%+g (%s)" % (ln, E.fmt_ml(o)),
                         "books": {bb: "%+g (%s)" % (l2, E.fmt_ml(o2)) for bb, l2, o2 in q}})
    for side in ("over", "under"):
        q = [(b, x.get("total"), x.get(side + "_odds")) for b, x in g["lines"].items() if x.get("total") is not None and x.get(side + "_odds")]
        if q:
            b, ln, o = (min if side == "over" else max)(q, key=lambda t: (t[1], -dec(t[2])) if side == "over" else (t[1], dec(t[2])))
            rows.append({"market": side.title(), "best_book": b, "best": "%g (%s)" % (ln, E.fmt_ml(o)),
                         "books": {bb: "%g (%s)" % (l2, E.fmt_ml(o2)) for bb, l2, o2 in q}})
    return rows


def price_all(games, book_sets, pm_games, kalshi_sports):
    games = merge(games, book_sets)
    ks_by_event = defaultdict(list)
    for m in kalshi_sports:
        ks_by_event[m["event_ticker"]].append(m)
    cands = []
    for g in games:
        pm = E.match_pm_game(g, pm_games)
        km = E.match_kalshi_game(g, ks_by_event)
        g["cross"] = {"polymarket": pm and {"home": pm["home"], "away": pm["away"], "url": pm["m"]["url"]},
                      "kalshi": km and {"home": km["home"]["yes_ask"], "away": km["away"]["yes_ask"], "url": km["home"]["url"]}}
        try:
            cands += moneyline(g, pm, km)
            cands += spreads_totals(g)
        except Exception as e:
            g["pricing_error"] = str(e)[:200]
        g["board"] = line_board(g)
        g["n_books"] = len(g["lines"])
    props = prop_candidates(games)
    S._mark("Line shopping (books per game)", True, round(sum(g["n_books"] for g in games) / max(1, len(games)), 1))
    return games, cands, props
