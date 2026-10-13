"""Player-prop model: every prop is priced from the player's own statistical history.

  NFL   nflverse weekly player stats (this season + last), adjusted for the opposing
        defense's allowed-per-game in that stat (also from nflverse)
  MLB / NBA / WNBA / NHL / NCAAF   ESPN game logs (this season + last)

Distribution: Poisson for low-count stats (TDs, receptions, strikeouts, walks…),
normal for yardage/points. The model probability is then blended with the no-vig
market (Pinnacle preferred), so the edge only survives when the numbers disagree.
"""
import csv
import io
import math
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sources import _mark, fetch, now_utc

DAY = 86400

# canonical stat key <- book wording
STAT_ALIASES = {
    "passing_yards": ["Passing Yards", "Total Passing Yards", "Pass Yards"],
    "passing_tds": ["Passing Touchdowns", "Total Touchdown Passes", "Total Passing Touchdowns", "Pass TDs", "Passing TDs"],
    "completions": ["Completions", "Total Pass Completions", "Pass Completions", "Passing Completions"],
    "attempts": ["Passing Attempts", "Total Pass Attempts", "Pass Attempts"],
    "interceptions": ["Interceptions Thrown", "Total Interceptions", "Interceptions", "INT"],
    "rushing_yards": ["Rushing Yards", "Total Rushing Yards", "Rush Yards"],
    "carries": ["Rush Attempts", "Total Rush Attempts", "Rushing Attempts"],
    "receptions": ["Receptions", "Total Receptions"],
    "receiving_yards": ["Receiving Yards", "Total Receiving Yards"],
    "pass_rush_yards": ["Passing & Rushing Yards O/U", "Pass+Rush Yds", "Pass + Rush Yards"],
    "rush_rec_yards": ["Rush + Rec Yards", "Rush+Rec Yds", "Rushing + Receiving Yards"],
    "anytime_td": ["anytime_td", "Rush + Rec TDs", "Rush+Rec TDs"],
    "strikeouts": ["Strikeouts", "Total Strikeouts"],
    "pitcher_outs": ["Pitcher Outs"],
    "hits_allowed": ["Hits Allowed"],
    "walks_allowed": ["Pitcher Walks"],
    "total_bases": ["Bases", "Total Bases"],
    "hrr": ["Hits, Runs and RBIs"],
    "points": ["Points", "Total Points"],
    "rebounds": ["Rebounds", "Total Rebounds"],
    "assists": ["Assists", "Total Assists"],
    "anytime_goal": ["anytime_goal"],
}
CANON = {alias.lower(): k for k, v in STAT_ALIASES.items() for alias in v}
LABEL = {"passing_yards": "passing yards", "passing_tds": "passing TDs", "completions": "completions",
         "attempts": "pass attempts", "interceptions": "interceptions", "rushing_yards": "rushing yards",
         "carries": "rush attempts", "receptions": "receptions", "receiving_yards": "receiving yards",
         "pass_rush_yards": "passing + rushing yards", "rush_rec_yards": "rushing + receiving yards", "anytime_td": "touchdown", "strikeouts": "strikeouts",
         "pitcher_outs": "outs recorded", "hits_allowed": "hits allowed", "walks_allowed": "walks allowed",
         "total_bases": "total bases", "hrr": "hits + runs + RBIs", "points": "points", "rebounds": "rebounds",
         "assists": "assists", "anytime_goal": "goal"}
POISSON = {"passing_tds", "interceptions", "receptions", "anytime_td", "strikeouts", "walks_allowed",
           "hits_allowed", "total_bases", "hrr", "anytime_goal", "carries", "assists", "rebounds"}


def canon(stat):
    return CANON.get((stat or "").lower())


def norm_name(n):
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\.?$", "", (n or "").lower().strip())
    return re.sub(r"[^a-z ]", "", n).strip()


def implied(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def dec(ml):
    return 1 + (ml / 100 if ml > 0 else 100 / -ml)


def poisson_over(mu, line):
    k = int(math.floor(line))            # P(X > line) = 1 - P(X <= floor(line))
    mu = max(mu, 0.01)
    cdf, term = 0.0, math.exp(-mu)
    for i in range(k + 1):
        cdf += term
        term *= mu / (i + 1)
    return max(0.0, min(1.0, 1 - cdf))


def normal_over(mu, sd, line):
    return 0.5 * (1 - math.erf((line - mu) / (max(sd, 1e-6) * math.sqrt(2))))


# ------------------------------------------------------------ NFL (nflverse)
_nfl = {}


def _nfl_player_rows():
    if "rows" in _nfl:
        return _nfl["rows"], _nfl["def"]
    rows = []
    yr = now_utc().year
    for season in (yr, yr - 1, yr - 2, yr - 3):
        try:
            t = fetch("https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_%d.csv" % season,
                      as_json=False, ttl=6 * 3600, timeout=60)
            rows += [r for r in csv.DictReader(io.StringIO(t)) if r.get("season_type") == "REG"]
        except Exception:
            pass
    by = defaultdict(list)
    for r in rows:
        by[norm_name(r["player_display_name"])].append(r)
    # defense allowed per game (by opponent) for each stat, this season weighted
    allowed, games = defaultdict(lambda: defaultdict(float)), defaultdict(set)
    for r in rows:
        if int(r["season"]) != yr:
            continue
        d = r["opponent_team"]
        games[d].add(r["week"])
        for k in ("passing_yards", "rushing_yards", "receiving_yards", "receptions", "completions", "passing_tds"):
            allowed[d][k] += float(r.get(k) or 0)
    league = defaultdict(float)
    for d, s in allowed.items():
        for k, v in s.items():
            league[k] += v / max(1, len(games[d]))
    n = max(1, len(allowed))
    dfac = {d: {k: (v / max(1, len(games[d]))) / (league[k] / n) for k, v in s.items() if league[k]} for d, s in allowed.items()}
    _nfl["rows"], _nfl["def"] = by, dfac
    _mark("nflverse player stats", bool(rows), len(rows))
    return by, dfac


def _nfl_next_opp(team):
    """Next scheduled opponent from nflverse's schedule."""
    import history
    alias = {"LA": "LA", "LAR": "LA", "WSH": "WAS"}
    team = alias.get(team, team)
    for r in history._nfl_games():
        if not r["result"] and team in (r["home_team"], r["away_team"]):
            return r["away_team"] if r["home_team"] == team else r["home_team"]
    return ""


def _nfl_matchup_ctx(team):
    """Next opponent + its head coach, and game_id -> opposing head coach for history lookups."""
    import history
    alias = {"LAR": "LA", "WSH": "WAS"}
    team = alias.get(team, team)
    games = history._nfl_games()
    nxt = next((r for r in games if not r["result"] and team in (r["home_team"], r["away_team"])), None)
    if not nxt:
        return "", "", {}
    home = nxt["home_team"] == team
    opp = nxt["away_team"] if home else nxt["home_team"]
    opp_coach = nxt["away_coach"] if home else nxt["home_coach"]
    by_game = {r["game_id"]: (r["away_coach"] if r["home_team"] == team else r["home_coach"]) for r in games
               if team in (r["home_team"], r["away_team"])}
    return opp, opp_coach, by_game


def _nfl_values(rows, key):
    out = []
    for r in rows:
        f = lambda c: float(r.get(c) or 0)
        if key == "anytime_td":
            v = f("rushing_tds") + f("receiving_tds")
        elif key == "pass_rush_yards":
            v = f("passing_yards") + f("rushing_yards")
        elif key == "rush_rec_yards":
            v = f("rushing_yards") + f("receiving_yards")
        elif key == "interceptions":
            v = f("passing_interceptions")
        else:
            v = f(key)
        out.append((int(r["season"]), int(r["week"]), v, r["team"], r["opponent_team"], r.get("game_id", "")))
    return sorted(out, key=lambda x: (x[0], x[1]))


# --------------------------------------------------------- ESPN game logs
ESPN_KEYS = {"strikeouts": "strikeouts", "hits_allowed": "hits", "walks_allowed": "walks", "pitcher_outs": "innings",
             "points": "points", "rebounds": "totalRebounds", "assists": "assists",
             "rushing_yards": "rushingYards", "receiving_yards": "receivingYards", "receptions": "receptions",
             "passing_yards": "passingYards", "total_bases": "totalBases", "hrr": None, "anytime_goal": "goals"}
ESPN_SPORT = {"MLB": "baseball/mlb", "NBA": "basketball/nba", "WNBA": "basketball/wnba", "NHL": "hockey/nhl",
              "NCAAF": "football/college-football"}


def _espn_athlete(name, league):
    try:
        s = fetch("https://site.web.api.espn.com/apis/search/v2?limit=6&type=player&query=" + urllib.parse.quote(name), ttl=7 * DAY)
    except Exception:
        return None
    lg = ESPN_SPORT[league].split("/")[1]
    for r in s.get("results", []):
        for c in r.get("contents", []):
            web = (c.get("link") or {}).get("web", "") if isinstance(c.get("link"), dict) else ""
            if norm_name(c.get("displayName")) == norm_name(name) and ("/%s/" % lg.replace("college-football", "college-football") in web):
                m = re.search(r"~a:(\d+)", c.get("uid", ""))
                if m:
                    return m.group(1)
    return None


def _espn_values(league, aid, key):
    vals = []
    yr = now_utc().year
    for season in (yr, yr - 1, yr - 2):
        try:
            gl = fetch("https://site.web.api.espn.com/apis/common/v3/sports/%s/athletes/%s/gamelog?season=%d" % (ESPN_SPORT[league], aid, season), ttl=12 * 3600)
        except Exception:
            continue
        names = gl.get("names") or []
        evmeta = gl.get("events") or {}
        for st in gl.get("seasonTypes", []):
            if "Regular" not in st.get("displayName", "Regular"):
                continue
            for cat in st.get("categories", []):
                for e in cat.get("events", []):
                    stats = e.get("stats") or []
                    row = dict(zip(names, stats))
                    if key == "hrr":
                        v = sum(float(row.get(k) or 0) for k in ("hits", "runs", "RBIs"))
                    elif key == "pitcher_outs":
                        ip = str(row.get("innings") or "0").split(".")
                        v = int(ip[0]) * 3 + (int(ip[1]) if len(ip) > 1 and ip[1] else 0)
                    else:
                        k = ESPN_KEYS.get(key)
                        if not k or k not in row:
                            continue
                        raw = str(row[k]).split("-")[0]
                        try:
                            v = float(raw)
                        except ValueError:
                            continue
                    meta = evmeta.get(e.get("eventId")) or {}
                    date = meta.get("gameDate", "")
                    vals.append((season, date, v, "", (meta.get("opponent") or {}).get("displayName", ""), ""))
    return sorted(vals, key=lambda x: (x[0], x[1]))


# ------------------------------------------------------------------- model
def model_prob(values, key, line, opp_factor=1.0):
    """Weighted history -> P(over). This season weight 1.0, last season 0.4, recent games boosted."""
    if len(values) < 4:
        return None
    yr = max(v[0] for v in values)
    cur = [v[2] for v in values if v[0] == yr]
    prev = [v[2] for v in values if v[0] == yr - 1][-12:]
    pts = [(x, 0.4) for x in prev] + [(x, (2.0 if i >= len(cur) - 3 else 1.0)) for i, x in enumerate(cur)]
    wsum = sum(w for _, w in pts)
    mu = sum(x * w for x, w in pts) / wsum * opp_factor
    var = sum(w * (x - mu) ** 2 for x, w in pts) / wsum
    sd = max(math.sqrt(var), 0.2 * mu, 1.0)
    recent = (cur or prev)[-10:]
    hit = sum(1 for x in recent if x > line) / len(recent)
    if key in POISSON or mu < 8:
        p = poisson_over(mu, line)
    else:
        p = normal_over(mu, sd, line)
    last3 = cur[-3:]
    season_avg = sum(cur) / len(cur) if cur else None
    trend = None
    if len(cur) >= 4 and season_avg:
        d = (sum(last3) / len(last3)) / season_avg - 1
        trend = "up %d%% over the last 3" % round(d * 100) if d > 0.12 else "down %d%% over the last 3" % round(-d * 100) if d < -0.12 else "steady"
    return {"p_over": p, "mean": mu, "sd": sd, "n": len(pts), "n_cur": len(cur), "hit_rate": hit,
            "season_avg": round(season_avg, 1) if season_avg is not None else None, "trend": trend,
            "recent": [round(x, 1) for x in recent], "last_season_avg": round(sum(prev) / len(prev), 1) if prev else None}


def price_props(games, espn_games):
    """games: merged book games (with props list). Returns scored prop candidates."""
    by_nfl, dfac = _nfl_player_rows() if any(g["league"] == "NFL" for g in games) else ({}, {})
    # gather unique props per (league, player, stat, line)
    offers = defaultdict(list)
    wx = {}
    for g in games:
        for pr in g.get("props", []):
            k = canon(pr["stat"])
            if not k or pr.get("line") is None:
                continue
            offers[(g["league"], pr["player"], k, float(pr["line"]), g["home"], g["away"], g.get("start"))].append(pr)
            wx[(g["home"], g["away"])] = (g.get("pass_factor", 1.0), g.get("weather_note"))
    # ESPN athlete lookups for non-NFL (limit volume)
    need = {(lg, pl) for (lg, pl, k, *_rest) in offers if lg in ESPN_SPORT and k in ESPN_KEYS}
    need = list(need)[:160]
    with ThreadPoolExecutor(8) as ex:
        ids = dict(zip(need, ex.map(lambda x: _espn_athlete(x[1], x[0]), need)))
    cache = {}

    def history(lg, player, key):
        ck = (lg, player, key)
        if ck in cache:
            return cache[ck]
        vals = []
        if lg == "NFL":
            vals = _nfl_values(by_nfl.get(norm_name(player), []), key)
        elif ids.get((lg, player)):
            vals = _espn_values(lg, ids[(lg, player)], key)
        cache[ck] = vals
        return vals
    items = list(offers.items())
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda it: history(it[0][0], it[0][1], it[0][2]), items))
    # two-sided markets per player and stat: the anchor for pricing one-sided ladder lines ("100+ yards")
    anchors = defaultdict(list)
    for (lg, player, key, line, *_r), prs in items:
        two = (next((p for p in prs if p["book"] == "Pinnacle" and p.get("over") and p.get("under")), None)
               or next((p for p in prs if p.get("over") and p.get("under") and not p.get("dfs")), None))
        if two:
            io_, iu = implied(two["over"]), implied(two["under"])
            anchors[(lg, player, key)].append((line, io_ / (io_ + iu)))
    out = []
    for (lg, player, key, line, home, away, start), prs in items:
        vals = history(lg, player, key)
        if not vals:
            continue
        team = vals[-1][3] if lg == "NFL" else ""
        opp_fac, opp, opp_coach, vs_opp, vs_coach = 1.0, "", "", [], []
        if lg == "NFL" and team:
            # opponent = the other team in this game
            opp, opp_coach, coach_by_game = _nfl_matchup_ctx(team)
            base = {"pass_rush_yards": "passing_yards", "rush_rec_yards": "rushing_yards"}.get(key, key)
            f = dfac.get(opp, {}).get(base)
            if f:
                opp_fac = 1 + 0.5 * (f - 1)          # half-weight the matchup adjustment
            vs_opp = [v[2] for v in vals if v[4] == opp]
            vs_coach = [v[2] for v in vals if opp_coach and coach_by_game.get(v[5]) == opp_coach]
            base_mean = sum(v[2] for v in vals) / len(vals)
            # personal history vs this defense / this head coach nudges the mean (shrunk hard: small samples)
            for sample, k in ((vs_opp, 8), (vs_coach, 10)):
                if sample and base_mean > 0:
                    opp_fac *= 1 + (len(sample) / (len(sample) + k)) * ((sum(sample) / len(sample)) / base_mean - 1) * 0.5
        wfac, wnote = wx.get((home, away), (1.0, None))
        if wfac != 1.0 and key in ("passing_yards", "completions", "attempts", "passing_tds", "pass_rush_yards", "receiving_yards", "receptions"):
            opp_fac *= wfac if key.startswith("pass") or key in ("completions", "attempts") else 1 - (1 - wfac) * 0.6
        else:
            wfac = 1.0
        if lg != "NFL":
            opp = next((v[4] for v in vals if v[4] and v[4] in (home, away)), "")
            vs_opp = [v[2] for v in vals if v[4] and v[4] in (home, away)]
            base_mean = sum(v[2] for v in vals) / len(vals)
            if len(vs_opp) >= 3 and base_mean > 0:
                opp_fac *= 1 + (len(vs_opp) / (len(vs_opp) + 10)) * ((sum(vs_opp) / len(vs_opp)) / base_mean - 1) * 0.5
        m = model_prob(vals, key, line, opp_fac)
        if not m:
            continue
        # market: no-vig from Pinnacle if present, else Bovada; best price across books
        one_sided = False
        pin = next((p for p in prs if p["book"] == "Pinnacle" and p.get("under")), None)
        ref = pin or next((p for p in prs if p.get("under") and not p.get("dfs")), None) or next((p for p in prs if p.get("under")), None)
        if ref:
            io_, iu = implied(ref["over"]), implied(ref["under"])
            fair_over = io_ / (io_ + iu)
        elif anchors.get((lg, player, key)):
            # one-sided ladder line: take the market's view from the player's two-sided line and carry it
            # to this number along the player's own distribution (scale the mean until it reproduces that price)
            a_line, a_fair = min(anchors[(lg, player, key)], key=lambda a: abs(a[0] - line))
            pois = key in POISSON or m["mean"] < 8
            over = lambda sc, ln: poisson_over(m["mean"] * sc, ln) if pois else normal_over(m["mean"] * sc, m["sd"] * sc, ln)
            lo, hi = 0.2, 5.0
            for _ in range(40):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if over(mid, a_line) < a_fair else (lo, mid)
            # ...but never above what the book itself is charging: its price already includes its margin,
            # so the true chance of a one-sided line sits below the implied price, not above it
            fair_over = min(over(lo, line), implied(max(p_["over"] for p_ in prs if p_.get("over"))) / 1.05)
            fair_over = min(0.99, max(0.01, fair_over))
            one_sided = True
        else:                                          # one-sided with no two-sided line anywhere (anytime TD/goal): strip the hold
            fair_over = implied(max(p_["over"] for p_ in prs if p_.get("over"))) / (1.20 if key.startswith("anytime") else 1.10)
        for side in ("over", "under"):
            pm = m["p_over"] if side == "over" else 1 - m["p_over"]
            pf = fair_over if side == "over" else 1 - fair_over
            if pf < 0.2 or pf > 0.85 or m["n_cur"] < 2:
                continue                                  # no lottery tickets / no stale-only history
            gap = abs(pm - pf)
            # a huge disagreement usually means a role change the market already knows about
            w = 1.2 * min(1.0, m["n"] / 10) * (0.3 if gap > 0.25 else 1.0)
            p = (2 * pf + w * pm) / (2 + w)
            cap = min(0.06, 0.12 * pf)                    # the model can tilt the market, not overrule it; less so on longshots
            p = min(max(p, pf - cap), pf + cap)
            conf = 45 + (10 if pin else 0) + min(10, 2 * m["n_cur"]) - 80 * max(0, gap - 0.12) - (8 if one_sided else 0)
            quotes = [(p_["book"], p_[side]) for p_ in prs if p_.get(side)]
            if not quotes:
                continue
            book, odds = max(quotes, key=lambda q: dec(q[1]))
            ev = p * dec(odds) - 1
            out.append({
                "league": lg, "player": player, "stat": key, "stat_label": LABEL.get(key, key), "line": line,
                "side": side, "book": book, "odds": odds, "all_quotes": quotes, "p_model_raw": pm, "p_fair": pf,
                "p": p, "ev": ev, "home": home, "away": away, "start": start, "team": team, "opp": opp,
                "opp_factor": round(opp_fac, 3), "mean": round(m["mean"], 1), "sd": round(m["sd"], 1),
                "hit_rate": m["hit_rate"], "recent": m["recent"], "n": m["n"], "n_cur": m["n_cur"],
                "last_season_avg": m["last_season_avg"], "sharp_ref": bool(pin),
                "season_avg": m["season_avg"], "trend": m["trend"], "opp_coach": opp_coach,
                "weather_factor": wfac, "weather_note": wnote,
                "vs_opp": [round(x, 1) for x in vs_opp[-6:]], "vs_coach": [round(x, 1) for x in vs_coach[-8:]],
                "vs_opp_n": len(vs_opp), "vs_coach_n": len(vs_coach),
                "vs_opp_over": sum(1 for x in vs_opp if x > line), "vs_coach_over": sum(1 for x in vs_coach if x > line),
                "is_alt": any(p_.get("alt") for p_ in prs),
                "popularity": max([p_.get("popularity") or 0 for p_ in prs] or [0]),
                "confidence": max(10, min(85, conf)), "role_change": gap > 0.25,
            })
    _mark("Player prop model", bool(out), len(out))
    return out
