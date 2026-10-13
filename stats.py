"""Statistical team models, coaching and tactics. Produces, per game, a projected
margin and total (with a distribution), component estimates, a coach/manager
profile and a tactical profile. engine.py uses these to price moneylines, spreads,
totals and alternate lines.

NFL     nflverse: EPA/play (off & def), points for/against, pace (plays/game), pass rate,
        turnover margin, 2025 regressed into 2026. Coach model: every head coach's
        record vs the closing line since 2006 (wins above market expectation, ATS).
MLB     MLB StatsAPI team hitting (OPS, R/G) & pitching (ERA/WHIP), Statcast starter
        xERA, run differential; manager + one-run / extra-inning record as a
        bullpen-tactics proxy. Poisson run model.
NHL     NHL API goals for/against, L10, special teams; ESPN coach. Poisson goal model.
Soccer  football-data.co.uk goals + xG (time-weighted) Poisson model (home/draw/away,
        totals), shots / accuracy / corners / discipline tactical profile; manager
        from Wikipedia's club infobox.
NBA / WNBA / NCAAF   ESPN season margins + ESPN team statistics (pace & shot-profile /
        yards-per-play); ESPN coach record & experience.
"""
import json
import math
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import history as H
from sources import ESPN, _mark, fetch, fnum, now_utc

DAY = 86400
MARGIN_SD = {"NFL": 13.5, "NCAAF": 16.0, "NBA": 12.0, "WNBA": 11.0}
TOTAL_SD = {"NFL": 10.0, "NCAAF": 13.0, "NBA": 18.0, "WNBA": 15.0}
HFA = {"NFL": 1.5, "NCAAF": 2.5, "NBA": 2.5, "WNBA": 2.0}


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def poisson_pmf(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def poisson_grid(lh, la, n=15):
    return [[poisson_pmf(lh, i) * poisson_pmf(la, j) for j in range(n)] for i in range(n)]


# ================================================================== NFL
_nfl = {}
NV_ALIAS = {"WSH": "WAS", "LAR": "LA"}


def _nfl_team_table():
    if "teams" in _nfl:
        return _nfl["teams"]
    import csv
    import io
    yr = now_utc().year
    rows = []
    for season in (yr, yr - 1):
        try:
            t = fetch("https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_%d.csv" % season,
                      as_json=False, ttl=6 * 3600, timeout=60)
            rows += [r for r in csv.DictReader(io.StringIO(t)) if r.get("season_type") == "REG"]
        except Exception:
            pass
    games = [g for g in H._nfl_games() if g["result"] and int(g["season"]) >= yr - 1 and g["game_type"] == "REG"]
    acc = defaultdict(lambda: defaultdict(float))
    by_game = {(r["season"], r["week"], r["team"]): r for r in rows}
    for r in rows:
        w = 1.0 if int(r["season"]) == yr else 0.3
        t, o = r["team"], r["opponent_team"]
        opp = by_game.get((r["season"], r["week"], o), {})
        f = lambda d, c: float(d.get(c) or 0)
        plays = f(r, "attempts") + f(r, "carries") + f(r, "sacks_suffered")
        oplays = f(opp, "attempts") + f(opp, "carries") + f(opp, "sacks_suffered")
        a = acc[t]
        a["w"] += w
        a["off_epa"] += w * (f(r, "passing_epa") + f(r, "rushing_epa"))
        a["def_epa"] += w * (f(opp, "passing_epa") + f(opp, "rushing_epa"))
        a["plays"] += w * plays
        a["oplays"] += w * oplays
        a["pass"] += w * (f(r, "attempts") + f(r, "sacks_suffered"))
        a["to"] += w * (f(opp, "passing_interceptions") + f(opp, "rushing_fumbles_lost") + f(opp, "sack_fumbles_lost")
                        - f(r, "passing_interceptions") - f(r, "rushing_fumbles_lost") - f(r, "sack_fumbles_lost"))
        a["cur"] += 1 if w == 1.0 else 0
        a["air"] += w * f(r, "passing_air_yards")
        a["att"] += w * f(r, "attempts")
        a["sk_allowed"] += w * f(r, "sacks_suffered")
        a["sk_made"] += w * f(r, "def_sacks")
        a["pass_yds_allowed"] += w * f(opp, "passing_yards")
        a["rush_yds_allowed"] += w * f(opp, "rushing_yards")
        if w == 1.0:
            a.setdefault("recent", [])
            a["recent"].append((int(r["week"]), f(r, "passing_epa") + f(r, "rushing_epa") - f(opp, "passing_epa") - f(opp, "rushing_epa")))
    for g in games:
        w = 1.0 if int(g["season"]) == yr else 0.3
        for t, pf, pa in ((g["home_team"], g["home_score"], g["away_score"]), (g["away_team"], g["away_score"], g["home_score"])):
            acc[t]["pw"] += w
            acc[t]["pf"] += w * float(pf)
            acc[t]["pa"] += w * float(pa)
    teams = {}
    for t, a in acc.items():
        if not a["w"] or not a["pw"]:
            continue
        # regress toward league average with 3 phantom average games
        k = 3.0
        pf = (a["pf"] + k * 22.5) / (a["pw"] + k)
        pa = (a["pa"] + k * 22.5) / (a["pw"] + k)
        epa = (a["off_epa"] - a["def_epa"]) / (a["w"] + k)
        teams[t] = {"pf": pf, "pa": pa, "net_pts": pf - pa, "net_epa": epa,
                    "off_epa_play": a["off_epa"] / max(1, a["plays"]), "def_epa_play": a["def_epa"] / max(1, a["oplays"]),
                    "pace": a["plays"] / a["w"], "pass_rate": a["pass"] / max(1, a["plays"]),
                    "to_margin": a["to"] / a["w"], "games_cur": int(a["cur"]),
                    "adot": a["air"] / max(1, a["att"]), "sack_rate_allowed": a["sk_allowed"] / max(1, a["pass"]),
                    "sacks_pg": a["sk_made"] / a["w"], "pass_yds_allowed_pg": a["pass_yds_allowed"] / a["w"],
                    "rush_yds_allowed_pg": a["rush_yds_allowed"] / a["w"],
                    "recent": [x for _, x in sorted(a.get("recent", []))]}
    _nfl["teams"] = teams
    _mark("nflverse team efficiency", bool(teams), len(teams))
    return teams


def _nfl_coaches():
    """Every head coach since 2006: wins above closing-line expectation, ATS."""
    if "coaches" in _nfl:
        return _nfl["coaches"]
    c = defaultdict(lambda: {"n": 0, "w": 0, "exp": 0.0, "ats": 0, "ats_n": 0})
    for g in H._nfl_games():
        if not g["result"] or int(g["season"]) < 2006 or not g["home_moneyline"] or not g["away_moneyline"]:
            continue
        ph, pa = H.implied(int(float(g["home_moneyline"]))), H.implied(int(float(g["away_moneyline"])))
        p = ph / (ph + pa)
        res, spread = float(g["result"]), float(g["spread_line"] or 0)
        for coach, sign, pexp in ((g["home_coach"], 1, p), (g["away_coach"], -1, 1 - p)):
            if not coach:
                continue
            d = c[coach]
            d["n"] += 1
            d["w"] += 1 if res * sign > 0 else 0.5 if res == 0 else 0
            d["exp"] += pexp
            if res - spread != 0:
                d["ats_n"] += 1
                d["ats"] += 1 if (res - spread) * sign > 0 else 0
    out = {}
    for name, d in c.items():
        excess = d["w"] - d["exp"]
        out[name] = {"coach": name, "games": d["n"], "record": "%d-%d" % (d["w"], d["n"] - d["w"]),
                     "wins_vs_market": round(excess, 1), "per_game": excess / d["n"] if d["n"] else 0,
                     "ats": "%d-%d" % (d["ats"], d["ats_n"] - d["ats"]), "ats_pct": d["ats"] / d["ats_n"] if d["ats_n"] else None,
                     # shrink hard: ~150 phantom neutral games
                     "adj": excess / (d["n"] + 150)}
    _nfl["coaches"] = out
    return out


def _scheme(t):
    """Plain-English offensive/defensive identity from tendencies (league-relative thresholds)."""
    off = ("pass-heavy" if t["pass_rate"] >= 0.60 else "run-heavy" if t["pass_rate"] <= 0.52 else "balanced")
    off += ", " + ("vertical passing game" if t["adot"] >= 8.5 else "short/quick passing game" if t["adot"] <= 6.8 else "intermediate passing game")
    off += ", " + ("fast tempo" if t["pace"] >= 66 else "slow tempo" if t["pace"] <= 60 else "average tempo")
    de = ("heavy pass rush" if t["sacks_pg"] >= 3 else "little pass rush" if t["sacks_pg"] <= 1.6 else "average pass rush")
    de += ", " + ("vulnerable to the pass" if t["pass_yds_allowed_pg"] >= 250 else "stingy vs the pass" if t["pass_yds_allowed_pg"] <= 195 else "average vs the pass")
    de += ", " + ("soft vs the run" if t["rush_yds_allowed_pg"] >= 135 else "stout vs the run" if t["rush_yds_allowed_pg"] <= 95 else "average vs the run")
    return {"offense": off, "defense": de}


def _trend(t):
    r = t.get("recent") or []
    if len(r) < 3:
        return None
    last, base = sum(r[-2:]) / 2, sum(r) / len(r)
    word = "improving" if last - base > 3 else "declining" if last - base < -3 else "steady"
    return "%s (net EPA last 2 games %+.1f vs season %+.1f)" % (word, last, base)


def _coach_h2h(ch, ca, home_team, away_team):
    """Head coach vs head coach, and each coach vs the other franchise, from every game since 1999."""
    out = {}
    w = n = ats = 0
    hv = [0, 0]; av = [0, 0]
    for r in H._nfl_games():
        if not r["result"]:
            continue
        res, spread = float(r["result"]), float(r["spread_line"] or 0)
        pair = {r["home_coach"], r["away_coach"]}
        if ch and ca and pair == {ch, ca}:
            n += 1
            sign = 1 if r["home_coach"] == ch else -1
            w += res * sign > 0
            ats += (res - spread) * sign > 0
        for coach, opp_team, tally in ((ch, away_team, hv), (ca, home_team, av)):
            if coach and coach in pair and opp_team in (r["home_team"], r["away_team"]):
                sign = 1 if r["home_coach"] == coach else -1
                tally[1] += 1
                tally[0] += res * sign > 0
    if n:
        out["coach_vs_coach"] = "%s is %d-%d vs %s (%d-%d ATS)" % (ch, w, n - w, ca, ats, n - ats)
    if hv[1]:
        out["home_coach_vs_team"] = "%s is %d-%d vs %s" % (ch, hv[0], hv[1] - hv[0], away_team)
    if av[1]:
        out["away_coach_vs_team"] = "%s is %d-%d vs %s" % (ca, av[0], av[1] - av[0], home_team)
    return out, (w, n)


def _nfl_ats(team, opp, is_home, is_fav):
    """Against-the-spread history from closing lines: overall, in this role, and head-to-head."""
    yr = now_utc().year
    tot = {"w": 0, "n": 0, "margin": 0.0, "wsum": 0.0}
    role = [0, 0]
    h2h = [0, 0]
    for r in H._nfl_games():
        if not r["result"] or not r["spread_line"] or team not in (r["home_team"], r["away_team"]):
            continue
        season = int(r["season"])
        home = r["home_team"] == team
        sign = 1 if home else -1
        cover = (float(r["result"]) - float(r["spread_line"])) * sign       # >0 = covered
        fav = float(r["spread_line"]) * sign > 0
        if season >= yr - 1 and cover != 0:
            w = 1.0 if season == yr else 0.35
            tot["n"] += 1
            tot["w"] += cover > 0
            tot["margin"] += w * cover
            tot["wsum"] += w
            if home == is_home and fav == is_fav:
                role[1] += 1
                role[0] += cover > 0
        if opp in (r["home_team"], r["away_team"]) and season >= yr - 8 and cover != 0:
            h2h[1] += 1
            h2h[0] += cover > 0
    avg = tot["margin"] / tot["wsum"] if tot["wsum"] else 0.0
    # shrink hard: ATS records are mostly noise, so the nudge is small
    pts = avg * (tot["n"] / (tot["n"] + 14)) * 0.2
    if h2h[1] >= 4:
        pts += (h2h[0] / h2h[1] - 0.5) * 1.2 * (h2h[1] / (h2h[1] + 8))
    return {"record": "%d-%d" % (tot["w"], tot["n"] - tot["w"]), "avg_cover": round(avg, 1),
            "role": "%d-%d" % (role[0], role[1] - role[0]) if role[1] else "", "h2h": "%d-%d" % (h2h[0], h2h[1] - h2h[0]) if h2h[1] else "",
            "pts": max(-1.2, min(1.2, pts))}


def nfl(g):
    teams, coaches = _nfl_team_table(), _nfl_coaches()
    h = NV_ALIAS.get(g["home"]["abbr"], g["home"]["abbr"])
    a = NV_ALIAS.get(g["away"]["abbr"], g["away"]["abbr"])
    th, ta = teams.get(h), teams.get(a)
    if not th or not ta:
        return None
    up = next((r for r in H._nfl_games() if not r["result"] and r["home_team"] == h and r["away_team"] == a), None)
    rh = 0.6 * th["net_pts"] + 0.4 * th["net_epa"]
    ra = 0.6 * ta["net_pts"] + 0.4 * ta["net_epa"]
    rest = 0.0
    if up and up["home_rest"] and up["away_rest"]:
        rest = max(-2, min(2, (int(up["home_rest"]) - int(up["away_rest"])) * 0.25))
    hfa = 0 if g.get("neutral") else HFA["NFL"]
    margin = (rh - ra) / 2 + hfa + rest
    ch = coaches.get(up["home_coach"]) if up else None
    ca = coaches.get(up["away_coach"]) if up else None
    coach_adj = ((ch or {}).get("adj", 0) - (ca or {}).get("adj", 0))
    h2h, (cw, cn) = _coach_h2h(up["home_coach"] if up else None, up["away_coach"] if up else None, h, a)
    if cn >= 3:                                   # coach-vs-coach record, shrunk hard
        coach_adj += (cw - cn / 2) / (cn + 20) * 0.5
    sch_h, sch_a = _scheme(th), _scheme(ta)
    # scheme matchup: a vertical/pass-heavy offense into a defense that is vulnerable to the pass (and vice versa)
    def scheme_edge(off_t, def_t):
        e = 0.0
        if off_t["pass_rate"] >= 0.58:
            e += (def_t["pass_yds_allowed_pg"] - 222) / 222 * 2.0
        if off_t["pass_rate"] <= 0.54:
            e += (def_t["rush_yds_allowed_pg"] - 115) / 115 * 1.5
        e -= (def_t["sacks_pg"] - 2.3) * off_t["sack_rate_allowed"] * 6
        return max(-1.5, min(1.5, e))
    sm = scheme_edge(th, ta) - scheme_edge(ta, th)
    margin += sm
    home_fav = (g["odds"].get("spread") or 0) < 0 if g["odds"].get("spread") is not None else margin > 0
    ats_h, ats_a = _nfl_ats(h, a, True, home_fav), _nfl_ats(a, h, False, not home_fav)
    margin += ats_h["pts"] - ats_a["pts"]
    total = (th["pf"] + ta["pa"] + ta["pf"] + th["pa"]) / 2
    pace = (th["pace"] + ta["pace"]) / 2
    total *= 1 + 0.5 * (pace / 63.0 - 1)           # faster games -> more possessions
    weather = ""
    pass
    return {
        "margin_mu": margin, "margin_sd": MARGIN_SD["NFL"], "total_mu": total, "total_sd": TOTAL_SD["NFL"],
        "p_home_model": ncdf(margin / MARGIN_SD["NFL"]), "coach_adj": coach_adj,
        "components": [
            "Efficiency: %s net %+.1f pts/g, EPA/play off %+.3f def %+.3f · %s net %+.1f, off %+.3f def %+.3f" % (
                h, th["net_pts"], th["off_epa_play"], th["def_epa_play"], a, ta["net_pts"], ta["off_epa_play"], ta["def_epa_play"]),
            "Home field %+.1f, rest %+.2f%s" % (hfa, rest, (", " + weather) if weather else ""),
            "Turnover margin/g: %s %+.1f, %s %+.1f" % (h, th["to_margin"], a, ta["to_margin"]),
            "Scheme matchup worth %+.1f pts to %s" % (sm, h),
            "Against the spread (last 2 seasons): %s %s, avg cover %+.1f%s · %s %s, avg cover %+.1f%s%s. Worth %+.1f pts to %s" % (
                h, ats_h["record"], ats_h["avg_cover"], (", %s as %s %s" % (ats_h["role"], "home", "favorite" if home_fav else "underdog")) if ats_h["role"] else "",
                a, ats_a["record"], ats_a["avg_cover"], (", %s as %s %s" % (ats_a["role"], "road", "underdog" if home_fav else "favorite")) if ats_a["role"] else "",
                (" · head-to-head ATS %s %s" % (h, ats_h["h2h"])) if ats_h["h2h"] else "", ats_h["pts"] - ats_a["pts"], h),
        ] + [x for x in h2h.values()],
        "ats": {"home": ats_h, "away": ats_a},
        "schemes": {"home": sch_h, "away": sch_a},
        "trend": {"home": _trend(th), "away": _trend(ta)},
        "coach_h2h": h2h,
        "coaches": {s: c and {"name": c["coach"], "record": c["record"], "ats": c["ats"], "vs_market": c["wins_vs_market"],
                              "games": c["games"], "note": "%+.1f wins vs closing-line expectation over %d games" % (c["wins_vs_market"], c["games"])}
                    for s, c in (("home", ch), ("away", ca))},
        "tactics": {"home": {"pace": round(th["pace"], 1), "pass_rate": round(th["pass_rate"], 3)},
                    "away": {"pace": round(ta["pace"], 1), "pass_rate": round(ta["pass_rate"], 3)}},
    }


# ================================================================== MLB
_mlb = {}


def _mlb_tables():
    if "t" in _mlb:
        return _mlb["t"]
    yr = now_utc().year
    t = {}
    try:
        d = fetch("https://statsapi.mlb.com/api/v1/teams/stats?season=%d&group=hitting,pitching&stats=season&sportId=1" % yr, ttl=6 * 3600)
        for grp in d.get("stats", []):
            g = grp["group"]["displayName"]
            for s in grp.get("splits", []):
                tid = s["team"]["id"]
                t.setdefault(tid, {"name": s["team"]["name"]})
                st = s["stat"]
                if g == "hitting":
                    t[tid].update({"ops": fnum(st.get("ops")), "rpg": fnum(st.get("runs")) / max(1, fnum(st.get("gamesPlayed"), 1))})
                else:
                    t[tid].update({"era": fnum(st.get("era")), "whip": fnum(st.get("whip")),
                                   "rapg": fnum(st.get("runs")) / max(1, fnum(st.get("gamesPlayed"), 1))})
    except Exception:
        pass
    H._mlb_schedule()
    for tid, tr in (H._mlb.get("stand") or {}).items():
        if tid in t:
            splits = {r.get("type"): r for r in (tr.get("records") or {}).get("splitRecords", [])}
            t[tid]["one_run"] = splits.get("oneRun")
            t[tid]["extra"] = splits.get("extraInning")
    _mlb["t"] = t
    _mark("MLB team stats", bool(t), len(t))
    return t


def _mlb_manager(tid):
    try:
        d = fetch("https://statsapi.mlb.com/api/v1/teams/%s/coaches" % tid, ttl=DAY)
        m = next((c for c in d.get("roster", []) if c.get("job") == "Manager"), None)
        return m and m["person"]["fullName"]
    except Exception:
        return None


def mlb(g):
    t = _mlb_tables()
    sched = H._mlb_schedule()
    day = g["date"][:10]
    gm = next((x for x in sched if x["gameDate"][:10] == day and
               g["home"]["location"].split()[0] in x["teams"]["home"]["team"]["name"] and
               g["away"]["location"].split()[0] in x["teams"]["away"]["team"]["name"]), None)
    if not gm:
        return None
    hid, aid = gm["teams"]["home"]["team"]["id"], gm["teams"]["away"]["team"]["id"]
    th, ta = t.get(hid), t.get(aid)
    if not th or not ta or "ops" not in th or "era" not in ta:
        return None
    lg_rpg = sum(x.get("rpg", 4.5) for x in t.values()) / len(t)
    lg_era = sum(x.get("era", 4.2) for x in t.values()) / len(t)
    sv = H._savant_pitchers()
    xera = {}
    for side, team in (("home", gm["teams"]["home"]), ("away", gm["teams"]["away"])):
        pp = team.get("probablePitcher") or {}
        xera[side] = fnum(sv.get(str(pp.get("id")), {}).get("xera"), None)

    def pitching_factor(team_t, starter_xera):
        # ~5.5 innings of starter, rest bullpen (team ERA proxy)
        st = starter_xera if starter_xera else team_t["era"]
        return (5.5 * st + 3.5 * team_t["era"]) / 9 / lg_era
    lam_h = th["rpg"] * pitching_factor(ta, xera["away"]) * 1.02
    lam_a = ta["rpg"] * pitching_factor(th, xera["home"]) * 0.98
    # manager / bullpen-tactics proxy: one-run game record, heavily shrunk
    def one_run(tt):
        r = tt.get("one_run") or {}
        w, l = r.get("wins", 0), r.get("losses", 0)
        return (w - l) / (w + l + 40) if (w + l) else 0, "%d-%d" % (w, l)
    orh, orh_s = one_run(th)
    ora, ora_s = one_run(ta)
    grid = poisson_grid(lam_h, lam_a, 20)
    p_h = sum(grid[i][j] for i in range(20) for j in range(20) if i > j)
    tie = sum(grid[i][i] for i in range(20))
    p_h += tie * (0.5 + 0.1 * (orh - ora))        # extras decided ~coin flip, nudged by one-run record
    mh, ma = _mlb_manager(hid), _mlb_manager(aid)
    return {
        "lam_home": lam_h, "lam_away": lam_a, "total_mu": lam_h + lam_a, "p_home_model": p_h, "poisson": True,
        "components": [
            "Offense: %s OPS %.3f (%.2f R/G) vs %s OPS %.3f (%.2f R/G)" % (th["name"], th["ops"], th["rpg"], ta["name"], ta["ops"], ta["rpg"]),
            "Pitching: team ERA %.2f vs %.2f; starters' xERA %s vs %s" % (th["era"], ta["era"], xera["home"] or "n/a", xera["away"] or "n/a"),
            "Projected runs %.2f – %.2f" % (lam_h, lam_a),
        ],
        "coaches": {"home": mh and {"name": mh, "note": "one-run games %s, extra innings %s" % (orh_s, "%s-%s" % ((th.get("extra") or {}).get("wins", 0), (th.get("extra") or {}).get("losses", 0)))},
                    "away": ma and {"name": ma, "note": "one-run games %s, extra innings %s" % (ora_s, "%s-%s" % ((ta.get("extra") or {}).get("wins", 0), (ta.get("extra") or {}).get("losses", 0)))}},
        "tactics": {"home": {"OPS": th["ops"], "WHIP": th.get("whip")}, "away": {"OPS": ta["ops"], "WHIP": ta.get("whip")}},
    }


# ================================================================== NHL
def nhl(g):
    H.nhl(g)
    st = H._nhl.get("st") or {}
    sh, sa = st.get(g["home"]["abbr"]), st.get(g["away"]["abbr"])
    if not sh or not sa or not sh.get("gamesPlayed") or not sa.get("gamesPlayed"):
        return None
    k = 10  # regress early-season rates to league average (3.0 goals)
    rate = lambda s, key: (s.get(key, 0) + k * 3.0) / (s["gamesPlayed"] + k)
    lam_h = rate(sh, "goalFor") * rate(sa, "goalAgainst") / 3.0 + 0.12
    lam_a = rate(sa, "goalFor") * rate(sh, "goalAgainst") / 3.0
    grid = poisson_grid(lam_h, lam_a, 12)
    p_h = sum(grid[i][j] for i in range(12) for j in range(12) if i > j) + 0.52 * sum(grid[i][i] for i in range(12))
    return {"lam_home": lam_h, "lam_away": lam_a, "total_mu": lam_h + lam_a, "p_home_model": p_h, "poisson": True,
            "components": ["Goals for/against per game (regressed): %.2f/%.2f vs %.2f/%.2f" % (
                rate(sh, "goalFor"), rate(sh, "goalAgainst"), rate(sa, "goalFor"), rate(sa, "goalAgainst"))],
            "tactics": {"home": {"L10": "%s-%s-%s" % (sh.get("l10Wins"), sh.get("l10Losses"), sh.get("l10OtLosses"))},
                        "away": {"L10": "%s-%s-%s" % (sa.get("l10Wins"), sa.get("l10Losses"), sa.get("l10OtLosses"))}}}


# ================================================================ Soccer
def _wiki_manager(team):
    try:
        s = fetch("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=1&srsearch="
                  + urllib.parse.quote(team + " football club"), ttl=7 * DAY)
        title = s["query"]["search"][0]["title"]
        p = fetch("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=content&rvsection=0&format=json&titles="
                  + urllib.parse.quote(title), ttl=3 * DAY)
        txt = next(iter(p["query"]["pages"].values()))["revisions"][0]["*"]
        m = re.search(r"\|\s*(?:manager|head coach|coach)\s*=\s*(?:\{\{[^|}]*\|)?\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", txt, re.I)
        return m and m.group(1).strip()
    except Exception:
        return None


def soccer(g):
    code = H.FD_LEAGUES.get(g["league_path"])
    if not code:
        return None
    rows = H._fd_rows(code)
    hn, an = H._fd_name(g["home"]["display"], rows), H._fd_name(g["away"]["display"], rows)
    if not hn or not an:
        return None
    seasons = sorted({r.get("Date", "")[-2:] for r in rows})
    cur = H._season_codes()[0]

    def weight(r):   # newest ~38 games full weight, decaying
        return 1.0
    att, dfn, n = defaultdict(float), defaultdict(float), defaultdict(float)
    tot_h = tot_a = cnt = 0.0
    recent = rows[-760:]
    for i, r in enumerate(recent):
        w = 0.35 + 0.65 * i / max(1, len(recent) - 1)
        gh = 0.5 * float(r["FTHG"]) + 0.5 * fnum(r.get("HxG"), float(r["FTHG"]))
        ga = 0.5 * float(r["FTAG"]) + 0.5 * fnum(r.get("AxG"), float(r["FTAG"]))
        att[r["HomeTeam"]] += w * gh; dfn[r["HomeTeam"]] += w * ga; n[r["HomeTeam"]] += w
        att[r["AwayTeam"]] += w * ga; dfn[r["AwayTeam"]] += w * gh; n[r["AwayTeam"]] += w
        tot_h += w * gh; tot_a += w * ga; cnt += w
    if not n.get(hn) or not n.get(an):
        return None
    avg = (tot_h + tot_a) / (2 * cnt)
    home_adv = (tot_h / cnt) / avg
    k = 5
    A = lambda t: (att[t] + k * avg) / (n[t] + k) / avg
    D = lambda t: (dfn[t] + k * avg) / (n[t] + k) / avg
    lam_h = avg * A(hn) * D(an) * home_adv
    lam_a = avg * A(an) * D(hn) / home_adv
    grid = poisson_grid(lam_h, lam_a, 10)
    ph = sum(grid[i][j] for i in range(10) for j in range(10) if i > j)
    pd = sum(grid[i][i] for i in range(10))

    def profile(t):
        gs = [r for r in rows if t in (r["HomeTeam"], r["AwayTeam"])][-10:]
        f = lambda r, hk, ak: fnum(r.get(hk if r["HomeTeam"] == t else ak))
        n_ = max(1, len(gs))
        shots = sum(f(r, "HS", "AS") for r in gs) / n_
        sot = sum(f(r, "HST", "AST") for r in gs) / n_
        return {"shots_pg": round(shots, 1), "on_target_pct": round(100 * sot / shots, 0) if shots else None,
                "corners_pg": round(sum(f(r, "HC", "AC") for r in gs) / n_, 1),
                "fouls_pg": round(sum(f(r, "HF", "AF") for r in gs) / n_, 1),
                "cards_pg": round(sum(f(r, "HY", "AY") + 2 * f(r, "HR", "AR") for r in gs) / n_, 1)}
    mh, ma = _wiki_manager(g["home"]["display"]), _wiki_manager(g["away"]["display"])
    return {"lam_home": lam_h, "lam_away": lam_a, "total_mu": lam_h + lam_a, "p_home_model": ph, "p_draw_model": pd,
            "poisson": True,
            "components": ["xG/goals Poisson: attack %.2f / defence %.2f vs attack %.2f / defence %.2f (1.0 = league avg)" % (A(hn), D(hn), A(an), D(an)),
                           "Projected score %.2f – %.2f; H/D/A %.0f%% / %.0f%% / %.0f%%" % (lam_h, lam_a, ph * 100, pd * 100, (1 - ph - pd) * 100)],
            "coaches": {"home": mh and {"name": mh, "note": "manager (Wikipedia)"}, "away": ma and {"name": ma, "note": "manager (Wikipedia)"}},
            "tactics": {"home": profile(hn), "away": profile(an)}}


# ============================================ ESPN coach + team stats (other)
def _espn_coach(g, side):
    sport, league = g["sport"], g["league_path"]
    yr = now_utc().year
    base = "https://sports.core.api.espn.com/v2/sports/%s/leagues/%s/seasons/%d" % (sport, league, yr)
    try:
        c = fetch("%s/teams/%s/coaches" % (base, g[side]["id"]), ttl=DAY)
        ref = c["items"][0]["$ref"].replace("http:", "https:")
        co = fetch(ref, ttl=DAY)
        rec = ""
        for r in co.get("records", [])[:1]:
            rr = fetch(r["record"]["$ref"].replace("http:", "https:"), ttl=DAY)
            rec = next((i.get("summary") for i in rr.get("items", []) if i.get("summary")), "") if "items" in rr else rr.get("summary", "")
        return {"name": "%s %s" % (co.get("firstName", ""), co.get("lastName", "")), "experience": co.get("experience"),
                "record": rec, "note": "%s yrs experience%s" % (co.get("experience", "?"), (", season " + rec) if rec else "")}
    except Exception:
        return None


def _espn_team_stats(g, side):
    try:
        d = fetch("%s/%s/%s/teams/%s/statistics" % (ESPN, g["sport"], g["league_path"], g[side]["id"]), ttl=6 * 3600)
    except Exception:
        return {}
    flat = {}
    for cat in ((d.get("results") or {}).get("stats") or {}).get("categories", []) or d.get("statistics", {}).get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            flat[s.get("name")] = s.get("value")
    keep = {"avgPoints": "pts/g", "avgFieldGoalsAttempted": "FGA/g", "avgThreePointFieldGoalsAttempted": "3PA/g",
            "avgRebounds": "reb/g", "avgTurnovers": "TO/g", "yardsPerPlay": "yds/play", "totalPointsPerGame": "pts/g",
            "rushingAttempts": "rush att", "passingAttempts": "pass att"}
    return {v: round(flat[k], 2) for k, v in keep.items() if isinstance(flat.get(k), (int, float))}


def generic(g):
    """NBA / WNBA / NCAAF: margin from ESPN season results; team stats for tactics."""
    es = (g.get("history") or {}).get("ESPN season results")
    if not es:
        return None
    lg = g["league"]
    hfa = 0 if g.get("neutral") else HFA.get(lg, 0)
    n = min(es["home"]["games"], es["away"]["games"])
    shrink = n / (n + 4)
    margin = shrink * (es["home"]["avg_margin"] - es["away"]["avg_margin"]) / 2 + hfa

    def ats_pts(rec):
        m_ = re.match(r"(\d+)-(\d+)", rec or "")
        if not m_:
            return 0.0
        w, l = int(m_.group(1)), int(m_.group(2))
        return (w - l) / (w + l + 12) * 1.0
    ats_adj = ats_pts(g.get("ats_home")) - ats_pts(g.get("ats_away"))
    margin += ats_adj
    th, ta = _espn_team_stats(g, "home"), _espn_team_stats(g, "away")
    total = None
    if th.get("pts/g") and ta.get("pts/g"):
        total = th["pts/g"] + ta["pts/g"]
    return {"margin_mu": margin, "margin_sd": MARGIN_SD.get(lg, 12), "total_mu": total, "total_sd": TOTAL_SD.get(lg, 12),
            "p_home_model": ncdf(margin / MARGIN_SD.get(lg, 12)),
            "components": ["Season margins %+.1f vs %+.1f (%d games, shrink %.2f), home %+.1f" % (
                es["home"]["avg_margin"], es["away"]["avg_margin"], n, shrink, hfa)] + (
                ["Against the spread: %s %s, %s %s. Worth %+.1f pts to the home side" % (
                    g["home"]["abbr"], g.get("ats_home"), g["away"]["abbr"], g.get("ats_away"), ats_adj)]
                if (g.get("ats_home") or g.get("ats_away")) else []),
            "tactics": {"home": th, "away": ta}}


def build(games):
    """Attach g['model'] to each ESPN game."""
    def one(g):
        try:
            lg = g["league"]
            m = (nfl(g) if lg == "NFL" else mlb(g) if lg == "MLB" else nhl(g) if lg == "NHL"
                 else soccer(g) if g["sport"] == "soccer" else generic(g))
            if m:
                if lg not in ("NFL", "MLB") and g["sport"] != "soccer":
                    m.setdefault("coaches", {"home": _espn_coach(g, "home"), "away": _espn_coach(g, "away")})
                g["model"] = m
        except Exception as e:
            g["model_error"] = str(e)[:200]
        return g
    with ThreadPoolExecutor(8) as ex:
        games = list(ex.map(one, games))
    _mark("Team models (stats + coaches + tactics)", any(g.get("model") for g in games),
          sum(1 for g in games if g.get("model")))
    return games
