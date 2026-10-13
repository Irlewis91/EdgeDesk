"""Sportsbook odds from every source that serves them publicly.

  Action Network   main lines (ML / spread / total) for DraftKings, FanDuel, BetMGM,
                   Caesars, BetRivers, bet365, Fanatics, Hard Rock, theScore/ESPN BET,
                   Bally, consensus & opener, plus public ticket / money splits
  Bovada           main lines, ALTERNATE spreads & totals, PLAYER PROPS, anytime TD
  Pinnacle         sharpest book: main + alternate lines and player props (used as
                   the "fair price" anchor when present)
  (Polymarket / Kalshi game markets come from sources.py)

  FanDuel          its own public site feed: PLAYER PROPS, alt-line ladders (20+, 25+ ...),
                   anytime TD / goal scorers. Free, no key of ours.

DraftKings, PrizePicks and Underdog block automated clients; DraftKings' main lines still
arrive through Action Network. (apify_src.py can buy those feeds, and is off by default.)
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor

from sources import _mark, fetch, fnum

# Action Network book ids -> brand (one state feed per brand)
AN_BOOKS = {68: "DraftKings", 1006: "FanDuel", 939: "BetMGM", 1005: "Caesars", 972: "BetRivers",
            79: "bet365", 2821: "Fanatics", 1901: "Hard Rock", 4601: "theScore Bet", 3882: "Bally Bet",
            15: "Consensus", 30: "Opener"}
AN_LEAGUES = {"NFL": "nfl", "NCAAF": "ncaaf", "MLB": "mlb", "NBA": "nba", "WNBA": "wnba", "NHL": "nhl"}

BOVADA_PATHS = {"NFL": "football/nfl", "NCAAF": "football/college-football", "MLB": "baseball/mlb",
                "NBA": "basketball/nba", "WNBA": "basketball/wnba", "NHL": "hockey/nhl",
                "EPL": "soccer/europe/england/premier-league", "MLS": "soccer/north-america/united-states/mls"}

PINNACLE = "https://guest.api.arcadia.pinnacle.com/0.1"
PIN_HEADERS = {"X-API-Key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R", "Referer": "https://www.pinnacle.com/"}
PIN_LEAGUES = {"NFL": 889, "NCAAF": 880, "MLB": 246, "NBA": 487, "WNBA": 578, "NHL": 1456, "EPL": 1980, "MLS": 2663}


def am(x):
    if x in (None, "", "EVEN", "even"):
        return 100 if x in ("EVEN", "even") else None
    try:
        v = int(float(str(x).replace("+", "")))
        return v if v != 0 else None
    except ValueError:
        return None


def blank_game(league, home, away, start):
    return {"league": league, "home": home, "away": away, "start": start, "books": {}, "public": {},
            "alts": [], "props": []}


# ------------------------------------------------------------ Action Network
def action_network():
    ids = ",".join(str(i) for i in AN_BOOKS)
    games = []

    def one(item):
        label, slug = item
        try:
            d = fetch("https://api.actionnetwork.com/web/v2/scoreboard/%s?bookIds=%s" % (slug, ids), ttl=900, timeout=40)
        except Exception:
            return []
        out = []
        for gm in d.get("games", []):
            if gm.get("status") not in ("scheduled", "pre"):
                continue
            teams = {t["id"]: t for t in gm.get("teams", [])}
            h, a = teams.get(gm["home_team_id"]), teams.get(gm["away_team_id"])
            if not h or not a:
                continue
            g = blank_game(label, h["full_name"], a["full_name"], gm.get("start_time"))
            for bid, mk in (gm.get("markets") or {}).items():
                name = AN_BOOKS.get(int(bid))
                ev = (mk or {}).get("event") or {}
                if not name or not ev:
                    continue
                b = {}
                for o in ev.get("moneyline", []):
                    b["ml_" + o["side"]] = o.get("odds")
                    if name == "Consensus" or "public_ml_" + o["side"] not in g["public"]:
                        bi = o.get("bet_info") or {}
                        if (bi.get("tickets") or {}).get("percent"):
                            g["public"]["ml_" + o["side"]] = {"tickets": bi["tickets"]["percent"], "money": bi["money"]["percent"]}
                for o in ev.get("spread", []):
                    b["spread_" + o["side"]] = o.get("value")
                    b["spread_%s_odds" % o["side"]] = o.get("odds")
                    bi = o.get("bet_info") or {}
                    if (bi.get("tickets") or {}).get("percent"):
                        g["public"]["spread_" + o["side"]] = {"tickets": bi["tickets"]["percent"], "money": bi["money"]["percent"]}
                for o in ev.get("total", []):
                    b["total"] = o.get("value")
                    b[o["side"] + "_odds"] = o.get("odds")
                if b:
                    g["books"][name] = b
            out.append(g)
        return out
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(one, AN_LEAGUES.items()):
            games.extend(rows)
    _mark("Action Network (12 sportsbooks)", bool(games), len(games))
    return games


# ------------------------------------------------------------------- Bovada
PROP_NAME = re.compile(r"^(Total|Alternate) (.+?) - (.+?)(?: \(([A-Z]{2,4})\))?$")


def bovada():
    games = []

    def one(item):
        label, path = item
        try:
            d = fetch("https://www.bovada.lv/services/sports/event/v2/events/A/description/%s" % path, ttl=900, timeout=40)
        except Exception:
            return []
        out = []
        for grp in d:
            for ev in grp.get("events", []):
                if ev.get("live"):
                    continue
                comps = {c.get("home"): c.get("name") for c in ev.get("competitors", [])}
                home, away = comps.get(True), comps.get(False)
                if not home or not away:
                    continue
                g = blank_game(label, home, away, ev.get("startTime"))
                b = {}
                for dg in ev.get("displayGroups", []):
                    dgn = dg.get("description", "")
                    for m in dg.get("markets", []):
                        if (m.get("period") or {}).get("main") is False:
                            continue
                        desc = m.get("description", "")
                        outs = [o for o in m.get("outcomes", []) if o.get("status") == "O"]
                        if dgn == "Game Lines":
                            for o in outs:
                                pr = o.get("price") or {}
                                side = "home" if o.get("description") == home else "away" if o.get("description") == away else (o.get("type") or "").lower()
                                if desc.startswith("Moneyline") and side in ("home", "away"):
                                    b["ml_" + side] = am(pr.get("american"))
                                elif desc.startswith("Point Spread") and side in ("home", "away"):
                                    b["spread_" + side] = fnum(pr.get("handicap"), None)
                                    b["spread_%s_odds" % side] = am(pr.get("american"))
                                elif desc.startswith("Total") and side in ("o", "u"):
                                    b["total"] = fnum(pr.get("handicap"), None)
                                    b[("over" if side == "o" else "under") + "_odds"] = am(pr.get("american"))
                        elif dgn == "Alternate Lines":
                            kind = "spread" if desc.startswith("Spread") else "total" if desc == "Total Points" or desc.startswith("Total Points") and " - " not in desc else None
                            if not kind:
                                continue
                            for o in outs:
                                pr = o.get("price") or {}
                                line = fnum(pr.get("handicap"), None)
                                if line is None:
                                    continue
                                side = ("home" if o.get("description") == home else "away") if kind == "spread" else ("over" if (o.get("type") or "").upper() == "O" else "under")
                                g["alts"].append({"book": "Bovada", "kind": kind, "side": side, "line": line, "odds": am(pr.get("american"))})
                        elif "Prop" in dgn or dgn in ("Passing Yards", "Rushing Yards", "Receiving Yards", "Player Points",
                                                         "Player Rebounds", "Player Assists", "Player Threes", "Pitcher Props",
                                                         "Batter Props", "Player Shots", "Goalscorer"):
                            if desc.startswith("Anytime Touchdown") or desc.startswith("Anytime Goal"):
                                for o in outs:
                                    player = re.sub(r"\s*\([A-Z]{2,4}\)$", "", o.get("description", ""))
                                    g["props"].append({"book": "Bovada", "player": player, "stat": "anytime_td" if "Touchdown" in desc else "anytime_goal",
                                                       "line": 0.5, "over": am((o.get("price") or {}).get("american")), "under": None})
                                continue
                            pm = PROP_NAME.match(desc)
                            if not pm or pm.group(1) != "Total":
                                continue
                            stat, player = pm.group(2), pm.group(3)
                            ov = next((o for o in outs if (o.get("type") or "").upper() == "O" or o.get("description", "").lower().startswith("over")), None)
                            un = next((o for o in outs if (o.get("type") or "").upper() == "U" or o.get("description", "").lower().startswith("under")), None)
                            if not ov or not un:
                                continue
                            line = fnum((ov.get("price") or {}).get("handicap"), None)
                            if line is None:
                                continue
                            g["props"].append({"book": "Bovada", "player": player, "stat": stat.strip(), "line": line,
                                               "over": am(ov["price"].get("american")), "under": am(un["price"].get("american"))})
                if b:
                    g["books"]["Bovada"] = b
                out.append(g)
        return out
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(one, BOVADA_PATHS.items()):
            games.extend(rows)
    _mark("Bovada (lines, alts, props)", bool(games), sum(len(g["props"]) for g in games))
    return games


# ----------------------------------------------------------------- Pinnacle
def pinnacle():
    games = []

    def one(item):
        label, lid = item
        try:
            mt = fetch("%s/leagues/%d/matchups" % (PINNACLE, lid), ttl=900, headers=PIN_HEADERS, timeout=40)
            mk = fetch("%s/leagues/%d/markets/straight" % (PINNACLE, lid), ttl=600, headers=PIN_HEADERS, timeout=40)
        except Exception:
            return []
        by_match = {}
        for m in mk:
            by_match.setdefault(m["matchupId"], []).append(m)
        out, idx = [], {}
        for x in mt:
            if x.get("special") or x.get("isLive") or x.get("type") != "matchup":
                continue
            parts = {p["alignment"]: p for p in x.get("participants", [])}
            if "home" not in parts or "away" not in parts:
                continue
            g = blank_game(label, parts["home"]["name"], parts["away"]["name"], x.get("startTime"))
            b = {}
            for m in by_match.get(x["id"], []):
                if m.get("period") != 0:
                    continue
                pr = m.get("prices", [])
                alt = bool(m.get("isAlternate"))
                if m["type"] == "moneyline" and not alt:
                    for p in pr:
                        b["ml_" + p.get("designation", "")] = p.get("price")
                elif m["type"] == "spread":
                    for p in pr:
                        side = p.get("designation")
                        if alt:
                            g["alts"].append({"book": "Pinnacle", "kind": "spread", "side": side, "line": p.get("points"), "odds": p.get("price")})
                        else:
                            b["spread_" + side] = p.get("points")
                            b["spread_%s_odds" % side] = p.get("price")
                elif m["type"] == "total":
                    for p in pr:
                        side = p.get("designation")
                        if alt:
                            g["alts"].append({"book": "Pinnacle", "kind": "total", "side": side, "line": p.get("points"), "odds": p.get("price")})
                        else:
                            b["total"] = p.get("points")
                            b[side + "_odds"] = p.get("price")
            if b:
                g["books"]["Pinnacle"] = b
            idx[x["id"]] = g
            out.append(g)
        # player props live as "special" matchups whose parent is the game
        for x in mt:
            sp = x.get("special") or {}
            if sp.get("category") != "Player Props" or not x.get("parent"):
                continue
            g = idx.get(x["parent"]["id"])
            desc = sp.get("description", "")
            mm = re.match(r"^(.+?) \((.+)\)$", desc) or re.match(r"^(.+?) (Total .+)$", desc)
            if not g or not mm:
                continue
            player, stat = mm.group(1).strip(), mm.group(2).strip()
            pid = {p["id"]: p["name"] for p in x.get("participants", [])}
            for m in by_match.get(x["id"], []):
                prices = {pid.get(p["participantId"], ""): p for p in m.get("prices", [])}
                if "Over" in prices and "Under" in prices:
                    g["props"].append({"book": "Pinnacle", "player": player, "stat": stat, "line": prices["Over"].get("points"),
                                       "over": prices["Over"].get("price"), "under": prices["Under"].get("price")})
        return out
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(one, PIN_LEAGUES.items()):
            games.extend(rows)
    _mark("Pinnacle (sharp lines, alts, props)", bool(games), len(games))
    return games


# ------------------------------------------------------------------ FanDuel
# The same JSON the public sportsbook page loads. The state only picks which regional copy to read.
FD_HOST = "https://sbapi.%s.sportsbook.fanduel.com/api" % os.environ.get("EDGEDESK_FANDUEL_STATE", "az").lower()
FD_KEY = "FhMFpcPWXMeyZxOx"                       # public key embedded in FanDuel's own web page
FD_PAGES = {"NFL": ("nfl", ["passing-props", "receiving-props", "rushing-props", "td-scorer-props"], 7),
            "NBA": ("nba", ["player-points", "player-rebounds", "player-assists"], 2),
            "WNBA": ("wnba", ["player-points", "player-rebounds", "player-assists"], 2),
            "MLB": ("mlb", ["pitcher-props"], 2),
            "NHL": ("nhl", ["goals"], 2)}          # league -> (page id, prop tabs, days ahead)
FD_STAT = {"passing yds": "Passing Yards", "receiving yds": "Receiving Yards", "rushing yds": "Rushing Yards",
           "rushing + receiving yds": "Rushing + Receiving Yards", "total receptions": "Receptions"}
FD_MAX_GAMES = 16


def _fd_odds(r):
    return am((((r.get("winRunnerOdds") or {}).get("americanDisplayOdds")) or {}).get("americanOdds"))


def _fd_props(markets):
    out = []
    for m in markets:
        if m.get("marketStatus") != "OPEN":
            continue
        name, runners = m.get("marketName", ""), [r for r in m.get("runners", []) if r.get("runnerStatus") == "ACTIVE"]
        if m.get("marketType") in ("ANY_TIME_TOUCHDOWN_SCORER", "ANY_TIME_GOAL_SCORER"):
            stat = "anytime_td" if "TOUCHDOWN" in m["marketType"] else "anytime_goal"
            out += [{"book": "FanDuel", "player": r["runnerName"], "stat": stat, "line": 0.5, "over": _fd_odds(r), "under": None}
                    for r in runners if _fd_odds(r) and r.get("runnerName") not in ("No Touchdown Scorer", "No Goalscorer")]
            continue
        if " - " not in name:
            continue
        player, stat = [x.strip() for x in name.split(" - ", 1)]
        alt = stat.lower().startswith("alt ")
        stat = stat[4:] if alt else stat
        stat = FD_STAT.get(stat.lower(), stat)
        if alt:                                   # ladder: "250+ Yards" == over 249.5, one-sided
            for r in runners:
                mm = re.search(r"(\d+)\+", r.get("runnerName", "").replace(player, ""))
                if mm and _fd_odds(r):
                    out.append({"book": "FanDuel", "player": player, "stat": stat, "line": float(mm.group(1)) - 0.5,
                                "over": _fd_odds(r), "under": None, "alt": True})
            continue
        ov = next((r for r in runners if r.get("runnerName", "").rstrip().endswith("Over")), None)
        un = next((r for r in runners if r.get("runnerName", "").rstrip().endswith("Under")), None)
        if ov and un and ov.get("handicap") and _fd_odds(ov) and _fd_odds(un):
            out.append({"book": "FanDuel", "player": player, "stat": stat, "line": float(ov["handicap"]),
                        "over": _fd_odds(ov), "under": _fd_odds(un)})
    return out


def fanduel():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    hdr = {"Accept": "application/json"}
    jobs, games = [], []
    for label, (page, tabs, days) in FD_PAGES.items():
        try:
            d = fetch("%s/content-managed-page?page=CUSTOM&customPageId=%s&_ak=%s" % (FD_HOST, page, FD_KEY), ttl=1500, timeout=40, headers=hdr)
        except Exception:
            continue
        evs = []
        for eid, ev in ((d.get("attachments") or {}).get("events") or {}).items():
            name = re.sub(r"\s*\([^)]*\)", "", ev.get("name") or "")
            if " @ " not in name or not ev.get("openDate"):
                continue
            try:
                start = datetime.datetime.fromisoformat(ev["openDate"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if now < start < now + datetime.timedelta(days=days):
                away, home = [x.strip() for x in name.split(" @ ", 1)]
                evs.append((start, eid, blank_game(label, home, away, ev["openDate"])))
        for _, eid, g in sorted(evs, key=lambda x: x[0])[:FD_MAX_GAMES]:
            games.append(g)
            jobs += [(g, eid, t) for t in tabs]

    def one(job):
        g, eid, tab = job
        try:
            d = fetch("%s/event-page?_ak=%s&eventId=%s&tab=%s" % (FD_HOST, FD_KEY, eid, tab), ttl=2400, timeout=40, headers=hdr)
        except Exception:
            return 1
        g["props"] += _fd_props(((d.get("attachments") or {}).get("markets") or {}).values())
        return 0
    with ThreadPoolExecutor(4) as ex:
        failed = sum(ex.map(one, jobs))
    for g in games:                                # the same market can sit on two tabs
        seen, uniq = set(), []
        for p in g["props"]:
            k = (p["player"], p["stat"], p["line"], bool(p.get("alt")))
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        g["props"] = uniq
    games = [g for g in games if g["props"]]
    n = sum(len(g["props"]) for g in games)
    _mark("FanDuel (player props, alt ladders)", bool(games) or not jobs, n,
          "%d of %d pages did not load" % (failed, len(jobs)) if failed else "")
    return games


def all_books():
    with ThreadPoolExecutor(4) as ex:
        a, b, p, f = ex.submit(action_network), ex.submit(bovada), ex.submit(pinnacle), ex.submit(fanduel)
        return {"action": a.result(), "bovada": b.result(), "pinnacle": p.result(), "fanduel": f.result()}
