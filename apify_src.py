"""Apify-sourced betting data for the books that block direct access.

  zen-studio/draftkings-odds          DraftKings player props incl. full alt-line ladders
  zen-studio/underdog-player-props    Underdog lines with both prices
  zen-studio/prizepicks-player-props  PrizePicks lines + pick popularity

Pay-per-use on YOUR Apify account (a full NFL pull is roughly $0.35), so it is OFF by
default: FanDuel's free feed (books.py) already supplies props and alt-line ladders.
Runs only with EDGEDESK_APIFY=1 and a token: env APIFY_TOKEN, or ~/.apify/auth.json.
Spend is capped per actor run (EDGEDESK_APIFY_CAP_USD, default 0.30) and results are
cached for EDGEDESK_APIFY_TTL seconds (default 3 hours) so the 30-minute refresh
does not re-buy the same data.
"""
import json
import os
import re
import time
import urllib.request

from sources import _mark

API = "https://api.apify.com/v2/acts/%s/run-sync-get-dataset-items?maxTotalChargeUsd=%s&timeout=240&clean=1"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "apify_cache.json")
DK_LEAGUES = {"NFL": "88808", "NCAAF": "87637"}
LEAGUES = [x.strip().upper() for x in os.environ.get("EDGEDESK_APIFY_LEAGUES", "NFL").split(",") if x.strip()]


def token():
    t = os.environ.get("APIFY_TOKEN")
    if t:
        return t
    try:
        with open(os.path.expanduser("~/.apify/auth.json")) as fh:
            return json.load(fh).get("token")
    except Exception:
        return None


def _run(actor, payload, tok):
    cap = os.environ.get("EDGEDESK_APIFY_CAP_USD", "0.30")
    req = urllib.request.Request(API % (actor.replace("/", "~"), cap), data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok}, method="POST")
    with urllib.request.urlopen(req, timeout=270) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _am(x):
    try:
        return int(str(x).replace("+", "").replace("−", "-"))
    except (TypeError, ValueError):
        return None


def _game(games, league, home, away, start):
    key = (league, home, away)
    if key not in games:
        games[key] = {"league": league, "home": home, "away": away, "start": start, "books": {}, "public": {}, "alts": [], "props": []}
    return games[key]


def _dk_team(s):          # "CAR Panthers" -> "Panthers"
    return re.sub(r"^[A-Z]{2,3}\s+", "", s or "")


def parse_draftkings(rows, league):
    games = {}
    for r in rows:
        if r.get("marketType") != "player_prop" or not r.get("participant"):
            continue
        g = _game(games, league, _dk_team(r.get("homeTeam")), _dk_team(r.get("awayTeam")), r.get("startTime"))
        player = r["participant"]
        stat = (r.get("market") or "").replace(player, "").strip()
        odds = _am(r.get("odds"))
        sel = str(r.get("selection") or "")
        m = re.match(r"^(\d+(?:\.\d+)?)\+$", sel)
        if "Touchdown Scorer" in stat and "Anytime" in stat:
            g["props"].append({"book": "DraftKings", "player": player, "stat": "anytime_td", "line": 0.5, "over": odds, "under": None})
        elif m:                    # milestone ladder: "20+" == over 19.5, one-sided alt line
            g["props"].append({"book": "DraftKings", "player": player, "stat": stat, "line": float(m.group(1)) - 0.5,
                               "over": odds, "under": None, "alt": True})
        elif r.get("points") is not None and str(r.get("outcome") or sel).lower().startswith(("over", "under")):
            side = "over" if str(r.get("outcome") or sel).lower().startswith("over") else "under"
            row = next((p for p in g["props"] if p["book"] == "DraftKings" and p["player"] == player and p["stat"] == stat
                        and p["line"] == float(r["points"]) and not p.get("alt")), None)
            if not row:
                row = {"book": "DraftKings", "player": player, "stat": stat, "line": float(r["points"]), "over": None, "under": None,
                       "public": {"bets": r.get("betsPercent"), "handle": r.get("handlePercent")}}
                g["props"].append(row)
            row[side] = odds
    return list(games.values())


def parse_underdog(rows, league):
    games = {}
    for r in rows:
        if r.get("line") is None or not r.get("player_name"):
            continue
        g = _game(games, league, r.get("home_team_name", ""), r.get("away_team_name", ""), r.get("game_start"))
        g["props"].append({"book": "Underdog", "player": r["player_name"], "stat": r.get("stat_display") or r.get("stat"),
                           "line": float(r["line"]), "over": _am(r.get("higher_american_odds")), "under": _am(r.get("lower_american_odds"))})
    return list(games.values())


def parse_prizepicks(rows, league):
    games = {}
    for r in rows:
        if r.get("line") is None or "+" in (r.get("player_name") or "") or "Combo" in (r.get("stat") or ""):
            continue
        g = _game(games, league, r.get("home_team_name", ""), r.get("away_team_name", ""), r.get("game_start"))
        # PrizePicks has no per-leg price; -122 is the break-even of its standard multi-pick payouts
        g["props"].append({"book": "PrizePicks", "player": r["player_name"], "stat": r.get("stat"), "line": float(r["line"]),
                           "over": -122, "under": -122, "dfs": True, "popularity": r.get("trending_count")})
    return list(games.values())


def fetch_all():
    if os.environ.get("EDGEDESK_APIFY", "").lower() not in ("1", "on", "true", "yes"):
        _mark("Apify (paid: DraftKings / Underdog / PrizePicks props)", True, 0,
              "off, nothing is being bought; set EDGEDESK_APIFY=1 to turn it on")
        return []
    tok = token()
    if not tok:
        _mark("Apify (paid: DraftKings / Underdog / PrizePicks props)", False,
              error="off: set APIFY_TOKEN to enable (pay-per-use, ~$0.35 per NFL pull)")
        return []
    ttl = int(os.environ.get("EDGEDESK_APIFY_TTL", "10800"))
    try:
        with open(CACHE) as fh:
            c = json.load(fh)
        if time.time() - c["at"] < ttl:
            _mark("Apify (paid: DraftKings / Underdog / PrizePicks props)", True, sum(len(g["props"]) for g in c["games"]), "cached")
            return c["games"]
    except Exception:
        pass
    games, errs = [], []
    for lg in LEAGUES:
        jobs = [("zen-studio/underdog-player-props", {"leagues": [lg if lg != "NCAAF" else "CFB"]}, parse_underdog),
                ("zen-studio/prizepicks-player-props", {"leagues": [lg if lg != "NCAAF" else "CFB"], "oddsTiers": ["standard"]}, parse_prizepicks)]
        if lg in DK_LEAGUES:
            jobs.insert(0, ("zen-studio/draftkings-odds", {"leagueIds": DK_LEAGUES[lg], "market": "player_props", "maxResults": 1500}, parse_draftkings))
        for actor, payload, parser in jobs:
            try:
                games += parser(_run(actor, payload, tok), lg)
            except Exception as e:
                errs.append("%s: %s" % (actor.split("/")[1], str(e)[:60]))
    n = sum(len(g["props"]) for g in games)
    if games:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as fh:
            json.dump({"at": time.time(), "games": games}, fh)
    _mark("Apify (paid: DraftKings / Underdog / PrizePicks props)", bool(games), n, "; ".join(errs))
    return games
