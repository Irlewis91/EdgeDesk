"""Historical sports data. Each source adds context to an ESPN game dict under
g["history"] and, when it has enough signal, a probability estimate under
g["history"]["estimates"] = [{"source", "p_home", "weight", "note"}].

Sources (all free, no key):
  nflverse          NFL games 1999-present incl. closing moneylines/spreads
  football-data.co.uk  Top-5 European soccer leagues, results + closing odds
  MLB StatsAPI      probable pitchers, standings, run differential
  Baseball Savant   pitcher ERA / xERA (Statcast expected stats)
  NHL api-web       standings, goal differential, last-10
  ESPN team schedules  completed results this season for every league
  TheSportsDB       head-to-head meeting history
  Sleeper           trending NFL players (fantasy adds/drops, a buzz proxy)
Sports-Reference sites are linked in the UI rather than scraped (their terms
restrict automated access; PFR and CFB Reference return 403 to bots)."""
import csv
import io
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sources import ESPN, _mark, fetch, fnum, now_utc

DAY = 86400


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def implied(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def _csv(url, ttl=DAY):
    return list(csv.DictReader(io.StringIO(fetch(url, as_json=False, ttl=ttl, timeout=60).lstrip("﻿"))))


# ---------------------------------------------------------------- nflverse
NFLVERSE_ALIAS = {"WSH": "WAS", "LAR": "LA", "JAX": "JAX"}
_nfl = {}


def _nfl_games():
    if "rows" not in _nfl:
        try:
            _nfl["rows"] = _csv("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv")
            _mark("nflverse (NFL 1999-now)", True, len(_nfl["rows"]))
        except Exception as e:
            _mark("nflverse (NFL 1999-now)", False, error=str(e))
            _nfl["rows"] = []
        _nfl["calib"] = _calibration(_nfl["rows"])
    return _nfl["rows"]


def _calibration(rows):
    """Closing-line calibration: bucket no-vig home win prob -> realized win rate."""
    b = defaultdict(lambda: [0, 0])
    for r in rows:
        if not r["result"] or not r["home_moneyline"] or not r["away_moneyline"]:
            continue
        ph, pa = implied(int(float(r["home_moneyline"]))), implied(int(float(r["away_moneyline"])))
        p = ph / (ph + pa)
        k = min(19, int(p * 20))
        res = float(r["result"])
        if res != 0:
            b[k][0] += 1 if res > 0 else 0
            b[k][1] += 1
    return {k: (w / n, n) for k, (w, n) in b.items() if n >= 40}


def nfl(g):
    rows = _nfl_games()
    if not rows:
        return
    h = NFLVERSE_ALIAS.get(g["home"]["abbr"], g["home"]["abbr"])
    a = NFLVERSE_ALIAS.get(g["away"]["abbr"], g["away"]["abbr"])
    season = max(int(r["season"]) for r in rows if r["result"])
    done = [r for r in rows if r["result"]]

    def team_line(t, seasons):
        su = ats = n = 0
        margin = 0.0
        for r in done:
            if int(r["season"]) not in seasons or t not in (r["home_team"], r["away_team"]):
                continue
            res = float(r["result"]) * (1 if r["home_team"] == t else -1)
            spread = float(r["spread_line"] or 0) * (1 if r["home_team"] == t else -1)
            n += 1
            su += res > 0
            ats += (res - spread) > 0
            margin += res
        return {"games": n, "su": "%d-%d" % (su, n - su), "ats": "%d-%d" % (ats, n - ats),
                "avg_margin": round(margin / n, 1) if n else None}

    h2h = [r for r in done if {r["home_team"], r["away_team"]} == {h, a}][-10:]
    hw = sum(1 for r in h2h if (float(r["result"]) > 0) == (r["home_team"] == h) and float(r["result"]) != 0)
    upcoming = next((r for r in rows if not r["result"] and r["home_team"] == h and r["away_team"] == a), None)
    hist = g.setdefault("history", {})
    hist["nflverse"] = {
        "home_this_season": team_line(h, {season}), "away_this_season": team_line(a, {season}),
        "home_last_season": team_line(h, {season - 1}), "away_last_season": team_line(a, {season - 1}),
        "h2h_last10": "%s %d-%d vs %s" % (h, hw, len(h2h) - hw, a),
        "h2h_games": [{"season": r["season"], "score": "%s %s-%s %s" % (r["away_team"], r["away_score"], r["home_score"], r["home_team"]),
                       "close_spread": r["spread_line"]} for r in h2h[-5:]],
        "rest": "%s %s days / %s %s days" % (h, upcoming["home_rest"], a, upcoming["away_rest"]) if upcoming else "",
        "divisional": bool(upcoming and upcoming["div_game"] == "1"),
        "weather": ("%s°F, wind %s mph, %s" % (upcoming["temp"], upcoming["wind"], upcoming["roof"])) if upcoming and upcoming["temp"] else "",
    }
    # Calibration correction on the current market price
    mh, ma = g["odds"].get("ml_home"), g["odds"].get("ml_away")
    if mh and ma:
        ph, pa = implied(mh), implied(ma)
        p = ph / (ph + pa)
        cal = _nfl["calib"].get(min(19, int(p * 20)))
        if cal:
            hist["nflverse"]["calibration"] = "Since 1999, home teams closing near %.0f%% won %.1f%% (n=%d)" % (p * 100, cal[0] * 100, cal[1])
            _est(g, "nflverse closing-line calibration", cal[0], min(1.0, cal[1] / 400) * 0.5,
                 "historical win rate at this price")


# ---------------------------------------------------------- football-data.co.uk
FD_LEAGUES = {"eng.1": "E0", "esp.1": "SP1", "ger.1": "D1", "ita.1": "I1", "fra.1": "F1"}
_fd = {}
FD_ALIAS = {"manchester united": "man united", "manchester city": "man city", "tottenham hotspur": "tottenham",
            "newcastle united": "newcastle", "wolverhampton wanderers": "wolves", "nottingham forest": "nott'm forest",
            "brighton & hove albion": "brighton", "west ham united": "west ham", "leeds united": "leeds",
            "afc bournemouth": "bournemouth", "atletico madrid": "ath madrid", "athletic club": "ath bilbao",
            "real sociedad": "sociedad", "bayern munich": "bayern munich", "borussia dortmund": "dortmund",
            "bayer leverkusen": "leverkusen", "internazionale": "inter", "ac milan": "milan",
            "paris saint-germain": "paris sg", "olympique de marseille": "marseille", "olympique lyonnais": "lyon"}


def _season_codes():
    y = now_utc().year % 100
    start = y if now_utc().month >= 7 else y - 1
    return ["%02d%02d" % (s, s + 1) for s in (start, start - 1, start - 2)]


def _fd_rows(code):
    if code not in _fd:
        rows = []
        for sc in _season_codes():
            try:
                rows += [r for r in _csv("https://www.football-data.co.uk/mmz4281/%s/%s.csv" % (sc, code)) if r.get("FTR")]
            except Exception:
                pass
        _fd[code] = rows
        _mark("football-data.co.uk (soccer)", bool(rows) or _fd_ok(), sum(len(v) for v in _fd.values()))
    return _fd[code]


def _fd_ok():
    return any(_fd.values())


def _fd_name(espn_name, rows):
    n = espn_name.lower()
    n = FD_ALIAS.get(n, n)
    names = {r["HomeTeam"] for r in rows}
    if not names:
        return None
    best = max(names, key=lambda x: len(set(re.findall(r"\w+", x.lower())) & set(re.findall(r"\w+", n))))
    return best if set(re.findall(r"\w+", best.lower())) & set(re.findall(r"\w+", n)) else None


def soccer(g):
    code = FD_LEAGUES.get(g["league_path"])
    if not code:
        return
    rows = _fd_rows(code)
    h, a = _fd_name(g["home"]["display"], rows), _fd_name(g["away"]["display"], rows)
    if not h or not a:
        return

    def form(t, n=8):
        games = [r for r in rows if t in (r["HomeTeam"], r["AwayTeam"])][-n:]
        s, gd = "", 0
        for r in games:
            home = r["HomeTeam"] == t
            gf, ga = int(r["FTHG"]), int(r["FTAG"])
            if not home:
                gf, ga = ga, gf
            gd += gf - ga
            s += "W" if gf > ga else "D" if gf == ga else "L"
        xg = [fnum(r.get("HxG") if r["HomeTeam"] == t else r.get("AxG"), None) for r in games]
        xg = [x for x in xg if x is not None]
        return {"form": s, "goal_diff": gd, "xg_per_game": round(sum(xg) / len(xg), 2) if xg else None}

    h2h = [r for r in rows if {r["HomeTeam"], r["AwayTeam"]} == {h, a}]
    g.setdefault("history", {})["football-data.co.uk"] = {
        "home_last8": form(h), "away_last8": form(a),
        "h2h": ["%s %s-%s %s (%s)" % (r["HomeTeam"], r["FTHG"], r["FTAG"], r["AwayTeam"], r["Date"]) for r in h2h[-5:]],
    }
    # Home-win base rate at this price, from Pinnacle closing odds (sharpest book)
    mh = g["odds"].get("ml_home")
    if mh:
        p = implied(mh)
        hits = n = 0
        for r in rows:
            o = fnum(r.get("PSCH") or r.get("PSH"), 0)
            if o > 1 and abs(1 / o - p) < 0.04:
                n += 1
                hits += r["FTR"] == "H"
        if n >= 30:
            g["history"]["football-data.co.uk"]["calibration"] = "Home sides priced ~%.0f%% (Pinnacle close) won %.0f%% (n=%d)" % (p * 100, hits / n * 100, n)


# ------------------------------------------------------------------- MLB
_mlb = {}


def _savant_pitchers():
    if "sv" not in _mlb:
        try:
            rows = _csv("https://baseballsavant.mlb.com/leaderboard/expected_statistics?type=pitcher&year=%d&csv=true&min=50" % now_utc().year)
            _mlb["sv"] = {r["player_id"]: r for r in rows}
            _mark("Baseball Savant (Statcast)", True, len(rows))
        except Exception as e:
            _mlb["sv"] = {}
            _mark("Baseball Savant (Statcast)", False, error=str(e))
    return _mlb["sv"]


def _mlb_schedule():
    if "sched" not in _mlb:
        d0 = now_utc().date()
        try:
            d = fetch("https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=%s&endDate=%s&hydrate=probablePitcher,team"
                      % (d0 - timedelta(days=1), d0 + timedelta(days=3)), ttl=1800)
            _mlb["sched"] = [gm for dt in d.get("dates", []) for gm in dt.get("games", [])]
            st = fetch("https://statsapi.mlb.com/api/v1/standings?leagueId=103,104", ttl=3600)
            _mlb["stand"] = {tr["team"]["id"]: tr for rec in st.get("records", []) for tr in rec.get("teamRecords", [])}
            _mark("MLB StatsAPI", True, len(_mlb["sched"]))
        except Exception as e:
            _mlb["sched"], _mlb["stand"] = [], {}
            _mark("MLB StatsAPI", False, error=str(e))
    return _mlb["sched"]


def mlb(g):
    sched, sv = _mlb_schedule(), _savant_pitchers()
    day = g["date"][:10]
    gm = next((x for x in sched if x["gameDate"][:10] == day and
               g["home"]["location"].split()[0] in x["teams"]["home"]["team"]["name"] and
               g["away"]["location"].split()[0] in x["teams"]["away"]["team"]["name"]), None)
    if not gm:
        return
    info = {}
    xera = {}
    for side in ("home", "away"):
        t = gm["teams"][side]
        pp = t.get("probablePitcher") or {}
        s = sv.get(str(pp.get("id")), {})
        stand = _mlb["stand"].get(t["team"]["id"], {})
        info[side] = {"pitcher": pp.get("fullName", "TBD"), "era": s.get("era"), "xera": s.get("xera"),
                      "run_diff": stand.get("runDifferential"), "streak": (stand.get("streak") or {}).get("streakCode"),
                      "last10": next(("%d-%d" % (r["wins"], r["losses"]) for r in
                                      (stand.get("records") or {}).get("splitRecords", []) if r.get("type") == "lastTen"), "")}
        xera[side] = fnum(s.get("xera"), None)
    g.setdefault("history", {})["MLB StatsAPI + Savant"] = info
    if xera["home"] and xera["away"]:
        # ~0.1 win prob per 1.0 xERA gap over a starter's ~5.5 innings, centered on the market
        base = _market_p(g)
        if base:
            p = min(0.85, max(0.15, base + 0.045 * (xera["away"] - xera["home"])))
            _est(g, "Statcast starting-pitcher xERA", p, 0.35,
                 "%s xERA %.2f vs %s xERA %.2f" % (info["home"]["pitcher"], xera["home"], info["away"]["pitcher"], xera["away"]))


# ------------------------------------------------------------------- NHL
_nhl = {}


def nhl(g):
    if "st" not in _nhl:
        try:
            _nhl["st"] = {s["teamAbbrev"]["default"]: s for s in fetch("https://api-web.nhle.com/v1/standings/now", ttl=3600)["standings"]}
            _mark("NHL API", True, len(_nhl["st"]))
        except Exception as e:
            _nhl["st"] = {}
            _mark("NHL API", False, error=str(e))
    out = {}
    for side in ("home", "away"):
        s = _nhl["st"].get(g[side]["abbr"])
        if s:
            out[side] = {"points_pct": s.get("pointPctg"), "goal_diff": s.get("goalDifferential"),
                         "last10": "%s-%s-%s" % (s.get("l10Wins"), s.get("l10Losses"), s.get("l10OtLosses")),
                         "streak": "%s%s" % (s.get("streakCode", ""), s.get("streakCount", ""))}
    if out:
        g.setdefault("history", {})["NHL API"] = out


# ------------------------------------------- ESPN season results (all leagues)
MARGIN_SD = {"NFL": 13.5, "NCAAF": 16.0, "NBA": 12.0, "WNBA": 11.0, "MLB": 4.3, "NHL": 2.4}
HOME_EDGE = {"NFL": 1.5, "NCAAF": 2.5, "NBA": 2.5, "WNBA": 2.0, "MLB": 0.15, "NHL": 0.15}


def _espn_results(g, side):
    t = g[side]
    try:
        d = fetch("%s/%s/%s/teams/%s/schedule" % (ESPN, g["sport"], g["league_path"], t["id"]), ttl=3 * 3600)
    except Exception:
        return None
    res = []
    for e in d.get("events", []):
        c = e.get("competitions", [{}])[0]
        if not c.get("status", {}).get("type", {}).get("completed"):
            continue
        me = next((x for x in c.get("competitors", []) if x.get("id") == t["id"]), None)
        op = next((x for x in c.get("competitors", []) if x.get("id") != t["id"]), None)
        if not me or not op:
            continue
        try:
            ms, os_ = fnum((me.get("score") or {}).get("value", me.get("score"))), fnum((op.get("score") or {}).get("value", op.get("score")))
        except AttributeError:
            continue
        res.append({"date": e.get("date", "")[:10], "opp": op.get("team", {}).get("abbreviation"), "opp_id": op.get("id"),
                    "margin": ms - os_, "home": me.get("homeAway") == "home"})
    return res


def espn_form(g):
    r = {s: _espn_results(g, s) for s in ("home", "away")}
    if not r["home"] or not r["away"]:
        return
    h2h = [x for x in r["home"] if x["opp_id"] == g["away"]["id"]]
    info = {}
    for s in ("home", "away"):
        rs = r[s]
        info[s] = {"games": len(rs), "avg_margin": round(sum(x["margin"] for x in rs) / len(rs), 2),
                   "last10": "".join("W" if x["margin"] > 0 else "L" if x["margin"] < 0 else "D" for x in rs[-10:])}
    info["h2h_this_season"] = ["%s %+g" % (x["date"], x["margin"]) for x in h2h]
    g.setdefault("history", {})["ESPN season results"] = info
    sd = MARGIN_SD.get(g["league"])
    if sd and min(info["home"]["games"], info["away"]["games"]) >= 4:
        edge = 0 if g.get("neutral") else HOME_EDGE.get(g["league"], 0)
        p = norm_cdf(((info["home"]["avg_margin"] - info["away"]["avg_margin"]) / 2 + edge) / sd)
        w = 0.4 * min(1.0, min(info["home"]["games"], info["away"]["games"]) / 12)
        _est(g, "Point-differential power rating", p, w,
             "avg margin %+.1f vs %+.1f" % (info["home"]["avg_margin"], info["away"]["avg_margin"]))


# ----------------------------------------------------- TheSportsDB head-to-head
def sportsdb_h2h(g):
    q = "%s_vs_%s" % (g["home"]["display"].replace(" ", "_"), g["away"]["display"].replace(" ", "_"))
    try:
        evs = fetch("https://www.thesportsdb.com/api/v1/json/3/searchevents.php?e=" + q, ttl=DAY).get("event") or []
    except Exception:
        return
    past = [e for e in evs if e.get("intHomeScore") not in (None, "")]
    if past:
        g.setdefault("history", {})["TheSportsDB H2H"] = [
            "%s %s %s-%s %s" % (e["dateEvent"], e["strHomeTeam"], e["intHomeScore"], e["intAwayScore"], e["strAwayTeam"])
            for e in sorted(past, key=lambda e: e["dateEvent"])[-5:]]
        _mark("TheSportsDB", True, len(past))


# ------------------------------------------------------------ Sleeper (NFL)
def sleeper_trending():
    try:
        adds = fetch("https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=25", ttl=3600)
        drops = fetch("https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=24&limit=25", ttl=3600)
        _mark("Sleeper (NFL trends)", True, len(adds) + len(drops))
        return {"adds": adds, "drops": drops}
    except Exception as e:
        _mark("Sleeper (NFL trends)", False, error=str(e))
        return {}


# ----------------------------------------------------------------- helpers
def _market_p(g):
    mh, ma = g["odds"].get("ml_home"), g["odds"].get("ml_away")
    if not mh or not ma:
        return None
    ph, pa = implied(mh), implied(ma)
    return ph / (ph + pa)


def _est(g, source, p_home, weight, note):
    g.setdefault("history", {}).setdefault("estimates", []).append(
        {"source": source, "p_home": round(p_home, 4), "weight": round(weight, 3), "note": note})


def sr_links(g):
    """Deep links to Sports-Reference family pages for manual research."""
    base = {"NBA": "https://www.basketball-reference.com/teams/", "MLB": "https://www.baseball-reference.com/teams/",
            "NHL": "https://www.hockey-reference.com/teams/", "NFL": "https://www.pro-football-reference.com/teams/",
            "NCAAF": "https://www.sports-reference.com/cfb/schools/", "WNBA": "https://www.basketball-reference.com/wnba/teams/",
            "EPL": "https://fbref.com/en/comps/9/", "MLS": "https://fbref.com/en/comps/22/"}.get(g["league"])
    return base


def enrich(games):
    """Attach every applicable historical source to each game."""
    _nfl_games() if any(g["league"] == "NFL" for g in games) else None

    def one(g):
        try:
            if g["league"] == "NFL":
                nfl(g)
            elif g["sport"] == "soccer":
                soccer(g)
            elif g["league"] == "MLB":
                mlb(g)
            elif g["league"] == "NHL":
                nhl(g)
            if g["sport"] != "soccer":
                espn_form(g)
            sportsdb_h2h(g)
            g["research_link"] = sr_links(g)
        except Exception as e:
            g.setdefault("history", {})["error"] = str(e)[:160]
        return g
    with ThreadPoolExecutor(6) as ex:
        games = list(ex.map(one, games))
    n = sum(1 for g in games if g.get("history"))
    _mark("ESPN team history", n > 0, n)
    return games
