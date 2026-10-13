"""Data fetchers. Stdlib only. Every fetcher returns plain dicts and never raises;
failures are recorded in STATUS so the UI can show which feeds are healthy."""
import ast
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
STATUS = {}          # source name -> {"ok": bool, "count": int, "error": str, "at": iso}
_cache = {}
_lock = threading.Lock()


def now_utc():
    return datetime.now(timezone.utc)


def _mark(name, ok, count=0, error=""):
    STATUS[name] = {"ok": ok, "count": count, "error": error[:200],
                    "at": now_utc().isoformat()}


def fetch(url, as_json=True, timeout=20, ttl=300, headers=None):
    """GET with a small in-memory TTL cache."""
    key = url
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    data = json.loads(raw) if as_json else raw
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def parse_dt(s):
    if not s:
        return None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            s2 = s.replace("Z", "+00:00")
            if len(s2) == 10:
                s2 += "T00:00:00+00:00"
            d = datetime.fromisoformat(s2.replace(" ", "T")) if "T" in s2 or " " in s2 else datetime.fromisoformat(s2)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return parsedate_to_datetime(s)
    except Exception:
        return None


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------- Polymarket
def polymarket_markets(limit=300):
    url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
           "&order=volume24hr&ascending=false&limit=%d" % limit)
    try:
        rows = fetch(url)
    except Exception as e:
        _mark("Polymarket", False, error=str(e))
        return []
    out = []
    for m in rows:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            prices = [fnum(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        except Exception:
            continue
        ev = (m.get("events") or [{}])[0]
        if ev.get("ended") or not prices or max(prices) >= 0.995:
            continue
        out.append({
            "venue": "Polymarket",
            "id": "pm-" + str(m.get("id")),
            "question": m.get("question", ""),
            "event": ev.get("title", ""),
            "outcomes": outcomes,
            "prices": prices,
            "best_bid": fnum(m.get("bestBid")),
            "best_ask": fnum(m.get("bestAsk")),
            "spread": fnum(m.get("spread")),
            "volume24h": fnum(m.get("volume24hr")),
            "liquidity": fnum(m.get("liquidity")),
            "change24h": fnum(m.get("oneDayPriceChange")),
            "end": m.get("endDate") or ev.get("endDate"),
            "league": (ev.get("eventMetadata") or {}).get("league") or (ev.get("series") or [{}])[0].get("title", ""),
            "url": "https://polymarket.com/event/" + (ev.get("slug") or m.get("slug", "")),
        })
    _mark("Polymarket", True, len(out))
    return out


PM_SPORT_SERIES = ["nfl", "mlb", "cfb", "wnba", "nhl", "nba", "mls", "lal", "epl", "bun", "sea", "fl1"]


def polymarket_games(horizon_h=120):
    """Per-game moneyline markets from Polymarket's sports series."""
    try:
        series = {x["sport"]: x["series"] for x in fetch("https://gamma-api.polymarket.com/sports", ttl=86400)}
    except Exception as e:
        _mark("Polymarket games", False, error=str(e))
        return []
    lo = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = now_utc() + timedelta(hours=horizon_h)

    def one(sport):
        sid = series.get(sport)
        if not sid:
            return []
        try:
            evs = fetch("https://gamma-api.polymarket.com/events?series_id=%s&closed=false&end_date_min=%s&limit=300"
                        % (sid, lo), ttl=600)
        except Exception:
            return []
        rows = []
        for ev in evs:
            start = parse_dt(ev.get("startTime") or ev.get("endDate"))
            if not start or start > hi or start < now_utc():
                continue
            for m in ev.get("markets", []):
                if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
                    continue
                try:
                    outs = json.loads(m.get("outcomes") or "[]")
                    prices = [fnum(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except Exception:
                    continue
                if len(outs) != 2 or not prices or max(prices) >= 0.99:
                    continue
                bb, ba = fnum(m.get("bestBid")), fnum(m.get("bestAsk"))
                rows.append({"venue": "Polymarket", "sport": sport, "title": ev.get("title", ""),
                             "question": m.get("question", ""), "outcomes": outs, "prices": prices,
                             # asks to BUY each outcome
                             "asks": [ba or prices[0], (1 - bb) if bb else prices[1]],
                             "start": ev.get("startTime"), "volume24h": fnum(m.get("volume24hr")),
                             "liquidity": fnum(m.get("liquidity")),
                             "url": "https://polymarket.com/event/" + ev.get("slug", "")})
        return rows
    out = []
    with ThreadPoolExecutor(6) as ex:
        for r in ex.map(one, PM_SPORT_SERIES):
            out.extend(r)
    _mark("Polymarket games", bool(out), len(out))
    return out


# ---------------------------------------------------------------------- Kalshi
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SPORTS = ["KXNFLGAME", "KXNCAAFGAME", "KXMLBGAME", "KXWNBAGAME",
                 "KXNBAGAME", "KXNHLGAME", "KXMLSGAME", "KXEPLGAME"]


def _kalshi_market(m, category=""):
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    last = fnum(m.get("last_price_dollars"))
    prev = fnum(m.get("previous_price_dollars"))
    mid = (yb + ya) / 2 if ya > 0 and yb > 0 else last
    series = (m.get("event_ticker") or "").split("-")[0]
    return {
        "venue": "Kalshi",
        "id": "k-" + m.get("ticker", ""),
        "ticker": m.get("ticker", ""),
        "event_ticker": m.get("event_ticker", ""),
        "question": m.get("title", ""),
        "sub": m.get("yes_sub_title", ""),
        "yes_bid": yb, "yes_ask": ya, "mid": mid, "last": last,
        "change24h": (last - prev) if prev else 0.0,
        "volume24h": fnum(m.get("volume_24h_fp")),
        "open_interest": fnum(m.get("open_interest_fp")),
        "liquidity": fnum(m.get("liquidity_dollars")),
        "end": m.get("close_time"),
        "category": category,
        "url": "https://kalshi.com/markets/" + series.lower(),
    }


def kalshi_markets(pages=4):
    out, cursor = [], ""
    try:
        for _ in range(pages):
            url = KALSHI + "/events?limit=200&status=open&with_nested_markets=true"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor)
            d = fetch(url)
            for ev in d.get("events", []):
                for m in ev.get("markets", []):
                    if fnum(m.get("volume_24h_fp")) > 0 and m.get("status") == "active":
                        km = _kalshi_market(m, ev.get("category", ""))
                        km["event"] = ev.get("title", "")
                        out.append(km)
            cursor = d.get("cursor")
            if not cursor:
                break
        _mark("Kalshi", True, len(out))
    except Exception as e:
        _mark("Kalshi", bool(out), len(out), str(e))
    return out


def kalshi_sports():
    out = []

    def one(series):
        try:
            d = fetch(KALSHI + "/markets?series_ticker=%s&status=open&limit=400" % series)
            return [_kalshi_market(m, "Sports") for m in d.get("markets", [])]
        except Exception:
            return []
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(one, KALSHI_SPORTS):
            out.extend(rows)
    _mark("Kalshi sports", bool(out), len(out))
    return out


# ------------------------------------------------------------------------ ESPN
ESPN = "https://site.web.api.espn.com/apis/site/v2/sports"
LEAGUES = [("football", "nfl", "NFL"), ("football", "college-football", "NCAAF"),
           ("baseball", "mlb", "MLB"), ("basketball", "nba", "NBA"),
           ("basketball", "wnba", "WNBA"), ("hockey", "nhl", "NHL"),
           ("soccer", "eng.1", "EPL"), ("soccer", "esp.1", "La Liga"), ("soccer", "ger.1", "Bundesliga"),
           ("soccer", "ita.1", "Serie A"), ("soccer", "fra.1", "Ligue 1"), ("soccer", "usa.1", "MLS")]


def american(o):
    if o is None:
        return None
    s = str(o).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100
    try:
        v = int(float(s.replace("+", "")))
        return v if v != 0 else None
    except ValueError:
        return None


def _team(c):
    t = c.get("team", {})
    rec = next((r.get("summary") for r in c.get("records", []) or []
                if r.get("type") == "total" or r.get("name") == "overall"), "")
    return {"id": t.get("id"), "name": t.get("name") or t.get("shortDisplayName", ""),
            "display": t.get("displayName", ""), "short": t.get("shortDisplayName", ""),
            "location": t.get("location", ""), "abbr": t.get("abbreviation", ""),
            "record": rec or "", "logo": t.get("logo", "")}


def _scoreboard(sport, league, label, horizon_h):
    events, seen = [], set()
    # Ask day by day: the default board shows only "today" (and for football keeps last week's finished
    # games until midweek). The NFL plays weekly, so look a full week ahead for it.
    if league == "nfl":
        horizon_h = max(horizon_h, 168)
    extra = "&limit=300" + ("&groups=80" if league == "college-football" else "")
    for i in range(int(horizon_h / 24) + 1):
        day = (now_utc() + timedelta(days=i)).strftime("%Y%m%d")
        try:
            rows = fetch("%s/%s/%s/scoreboard?dates=%s%s" % (ESPN, sport, league, day, extra if sport == "football" else "")).get("events", [])
        except Exception:
            continue
        events += [e for e in rows if e.get("id") not in seen]
        seen.update(e.get("id") for e in rows)
    games, limit = [], now_utc() + timedelta(hours=horizon_h)
    for e in events:
        st = e.get("status", {}).get("type", {})
        when = parse_dt(e.get("date"))
        if st.get("state") != "pre" or not when or when > limit:
            continue
        comp = e["competitions"][0]
        home = next((c for c in comp["competitors"] if c.get("homeAway") == "home"), None)
        away = next((c for c in comp["competitors"] if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        odds = (comp.get("odds") or [{}])[0]
        ml = odds.get("moneyline") or {}

        def mline(side):
            v = ((ml.get(side) or {}).get("close") or {}).get("odds")
            if v is None:
                v = (odds.get(side + "TeamOdds") or {}).get("moneyLine")
            return american(v)
        dml = ((ml.get("draw") or {}).get("close") or {}).get("odds")
        if dml is None:
            dml = (odds.get("drawOdds") or {}).get("moneyLine")
        link = next((l.get("href") for l in e.get("links", []) if "summary" in (l.get("rel") or [])),
                    "https://www.espn.com/search/_/q/" + urllib.parse.quote(e.get("name", "")))
        games.append({
            "id": e["id"], "sport": sport, "league_path": league, "league": label,
            "name": e.get("name", ""), "date": e.get("date"),
            "neutral": comp.get("neutralSite", False), "venue": comp.get("venue"),
            "home": _team(home), "away": _team(away),
            "odds": {"provider": (odds.get("provider") or {}).get("name", ""),
                     "details": odds.get("details", ""), "spread": odds.get("spread"),
                     "total": odds.get("overUnder"),
                     "ml_home": mline("home"), "ml_away": mline("away"), "ml_draw": american(dml)},
            "url": link,
        })
    return games


def _summary(g):
    try:
        s = fetch("%s/%s/%s/summary?event=%s" % (ESPN, g["sport"], g["league_path"], g["id"]), ttl=900)
    except Exception:
        return g
    pred = s.get("predictor") or {}
    try:
        hp = fnum((pred.get("homeTeam") or {}).get("gameProjection"), None)
        ap = fnum((pred.get("awayTeam") or {}).get("gameProjection"), None)
        if hp is not None and ap is not None and hp + ap > 0:
            g["predictor_home"] = hp / (hp + ap)
    except Exception:
        pass
    inj = {}
    for block in s.get("injuries", []) or []:
        tid = (block.get("team") or {}).get("id")
        outs = [i.get("athlete", {}).get("displayName", "") for i in block.get("injuries", [])
                if (i.get("status") or "").lower() in ("out", "injured reserve", "doubtful")]
        inj[tid] = outs[:6]
    g["injuries_home"] = inj.get(g["home"]["id"], [])
    g["injuries_away"] = inj.get(g["away"]["id"], [])
    form = {}
    for tf in s.get("lastFiveGames", []) or []:
        tid = (tf.get("team") or {}).get("id")
        form[tid] = "".join((ev.get("gameResult") or "?")[:1] for ev in tf.get("events", []))
    g["form_home"] = form.get(g["home"]["id"], "")
    g["form_away"] = form.get(g["away"]["id"], "")
    ats = {}
    for t in s.get("againstTheSpread", []) or []:
        tid = (t.get("team") or {}).get("id")
        rec = next((r.get("summary") for r in t.get("records", []) if "ATS" in (r.get("type", "") + r.get("name", "")).upper()), "")
        ats[tid] = rec
    g["ats_home"] = ats.get(g["home"]["id"], "")
    g["ats_away"] = ats.get(g["away"]["id"], "")
    return g


def espn_games(horizon_h=96, max_summaries=80):
    games = []
    with ThreadPoolExecutor(8) as ex:
        for rows in ex.map(lambda l: _scoreboard(l[0], l[1], l[2], horizon_h), LEAGUES):
            games.extend(rows)
    games.sort(key=lambda g: g["date"])
    with ThreadPoolExecutor(8) as ex:
        games[:max_summaries] = list(ex.map(_summary, games[:max_summaries]))
    _mark("ESPN sports", bool(games), len(games))
    return games


def espn_standings(sport, league):
    try:
        d = fetch("https://site.web.api.espn.com/apis/v2/sports/%s/%s/standings" % (sport, league), ttl=3600)
    except Exception:
        return []
    rows = []

    def walk(node):
        for ch in node.get("children", []) or []:
            walk(ch)
        for ent in (node.get("standings") or {}).get("entries", []) or []:
            stats = {s.get("name"): s.get("displayValue") for s in ent.get("stats", [])}
            rows.append({"team": ent.get("team", {}).get("displayName"),
                         "group": node.get("name", ""),
                         "wins": stats.get("wins"), "losses": stats.get("losses"),
                         "pct": stats.get("winPercent"), "diff": stats.get("pointDifferential") or stats.get("differential"),
                         "streak": stats.get("streak")})
    walk(d)
    return rows


# ---------------------------------------------------------------------- Social
POS = set("lock hammer smash love confident dominant healthy value sharp easy undervalued bullish strong best elite "
          "win winning cover covers moon underpriced great good fire rolling hot".split())
NEG = set("fade trap injured injury out doubtful worried avoid overrated bust bearish weak worst cold "
          "overpriced scandal fraud lose losing collapse suspended questionable struggling bad".split())


def tone(text):
    w = re.findall(r"[a-z']+", text.lower())
    p, n = sum(x in POS for x in w), sum(x in NEG for x in w)
    return 0 if p + n == 0 else (p - n) / (p + n)


def bluesky(query, limit=25):
    url = ("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?sort=latest&limit=%d&q=%s"
           % (limit, urllib.parse.quote(query)))
    try:
        posts = fetch(url, ttl=900).get("posts", [])
    except Exception as e:
        st = STATUS.get("Bluesky", {})
        _mark("Bluesky", st.get("count", 0) > 0, st.get("count", 0), "some searches failed: " + str(e))
        return []
    out = []
    for p in posts:
        txt = (p.get("record") or {}).get("text", "")
        handle = (p.get("author") or {}).get("handle", "")
        rkey = p.get("uri", "").split("/")[-1]
        out.append({"source": "Bluesky", "text": txt[:280], "likes": p.get("likeCount", 0),
                    "reposts": p.get("repostCount", 0), "at": (p.get("record") or {}).get("createdAt"),
                    "url": "https://bsky.app/profile/%s/post/%s" % (handle, rkey), "tone": tone(txt)})
    _mark("Bluesky", True, STATUS.get("Bluesky", {}).get("count", 0) + len(out))
    return out


REDDIT_SUBS = ["sportsbook", "sportsbetting", "Polymarket", "Kalshi", "nfl", "baseball",
               "CFB", "wallstreetbets", "politics", "worldnews"]


def reddit_hot():
    posts = []
    for sub in REDDIT_SUBS:
        try:
            xml = fetch("https://www.reddit.com/r/%s/hot.rss?limit=50" % sub, as_json=False, ttl=1200)
        except Exception:
            continue
        for it in _parse_feed(xml, "r/" + sub):
            body = re.sub(r"<[^>]+>", " ", html.unescape(it.get("summary", "")))
            posts.append({"source": "Reddit r/" + sub, "text": (it["title"] + " — " + body)[:400],
                          "title": it["title"], "url": it["link"], "at": it["published"],
                          "tone": tone(it["title"] + " " + body)})
        time.sleep(0.4)  # be polite; reddit rate-limits bursts
    _mark("Reddit", bool(posts), len(posts), "" if posts else "rate limited or blocked")
    return posts


# ------------------------------------------------------------------------ News
FEEDS = {
    "world": [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
              ("Google News World", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
              ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
              ("NPR World", "https://feeds.npr.org/1004/rss.xml")],
    "us": [("Mother Jones", "https://www.motherjones.com/feed/"),
           ("Google News US", "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en"),
           ("NPR Politics", "https://feeds.npr.org/1014/rss.xml")],
    "business": [("Google News Business", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
                 ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
                 ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml")],
    "sports": [("Google News Sports", "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en")],
    "tech": [("TLDR Tech", "https://tldr.tech/api/rss/tech"),
             ("TLDR AI", "https://tldr.tech/api/rss/ai"),
             ("Google News Tech", "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en")],
}


def _text(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None:
            if x.text and x.text.strip():
                return x.text.strip()
            if x.get("href"):
                return x.get("href")
    return ""


def _parse_feed(xml, source):
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml, count=20)
    xml = re.sub(r"<(/?)(\w+):(\w+)", r"<\1\2_\3", xml)
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return []
    items = root.findall(".//item") or root.findall(".//entry")
    out = []
    for it in items:
        title = html.unescape(_text(it, "title"))
        pub = parse_dt(_text(it, "pubDate", "published", "updated", "dc_date"))
        out.append({"source": source, "title": title, "link": _text(it, "link", "guid"),
                    "summary": _text(it, "description", "summary", "content", "content_encoded"),
                    "published": pub.isoformat() if pub else None})
    return out


def _tldr_expand(item):
    """TLDR's RSS only carries issue titles; pull the stories from the issue page."""
    try:
        page = fetch(item["link"], as_json=False, ttl=3600)
    except Exception:
        return [item]
    stories = []
    for m in re.finditer(r'<article[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>\s*<h3>(.*?)</h3>\s*</a>\s*'
                         r'<div class="newsletter-html">(.*?)</div>', page, re.S):
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if "(Sponsor)" in title:
            continue
        summary = html.unescape(re.sub(r"<[^>]+>", " ", m.group(3)))
        stories.append({"source": item["source"], "title": title, "link": html.unescape(m.group(1)),
                        "summary": re.sub(r"\s+", " ", summary).strip()[:400], "published": item["published"]})
    return stories or [item]


def news():
    out = {}
    jobs = [(cat, name, url) for cat, lst in FEEDS.items() for name, url in lst]

    def one(job):
        cat, name, url = job
        try:
            items = _parse_feed(fetch(url, as_json=False, ttl=900), name)
            if name.startswith("TLDR"):
                exp = []
                for it in items[:1]:
                    exp.extend(_tldr_expand(it))
                items = exp
            _mark(name, True, len(items))
            return cat, items
        except Exception as e:
            _mark(name, False, error=str(e))
            return cat, []
    with ThreadPoolExecutor(8) as ex:
        for cat, items in ex.map(one, jobs):
            out.setdefault(cat, []).extend(items)
    for cat, items in out.items():
        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: x.get("published") or "", reverse=True):
            k = re.sub(r"\W+", "", it["title"].lower())[:60]
            if k and k not in seen:
                seen.add(k)
                it["summary"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(it.get("summary") or ""))).strip()[:300]
                uniq.append(it)
        out[cat] = uniq[:60]
    try:
        hn = fetch("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30")
        out["hn"] = [{"source": "Hacker News", "title": h.get("title"), "link": h.get("url") or
                      "https://news.ycombinator.com/item?id=%s" % h.get("objectID"),
                      "points": h.get("points"), "comments": h.get("num_comments"),
                      "published": h.get("created_at")} for h in hn.get("hits", [])]
        _mark("Hacker News", True, len(out["hn"]))
    except Exception as e:
        _mark("Hacker News", False, error=str(e))
    return out


# -------------------------------------------------- Large prediction-market trades
def polymarket_trades(min_usd=10000, limit=150):
    try:
        rows = fetch("https://data-api.polymarket.com/trades?limit=%d&takerOnly=true&filterType=CASH&filterAmount=%d" % (limit, min_usd), ttl=300)
    except Exception as e:
        _mark("Polymarket large trades", False, error=str(e))
        return []
    out = []
    for t in rows:
        usd = fnum(t.get("size")) * fnum(t.get("price"))
        out.append({"venue": "Polymarket", "market": t.get("title", ""), "outcome": t.get("outcome", ""), "side": (t.get("side") or "").lower(),
                    "price": fnum(t.get("price")), "usd": usd, "who": t.get("pseudonym") or t.get("name") or "",
                    "at": datetime.fromtimestamp(fnum(t.get("timestamp")), timezone.utc).isoformat(),
                    "url": "https://polymarket.com/event/" + (t.get("eventSlug") or t.get("slug") or "")})
    _mark("Polymarket large trades", True, len(out))
    return out


def kalshi_trades(titles, min_usd=400, pages=5):
    """titles: ticker -> market title (from the markets feed). Kalshi has no size filter, so scan the tape."""
    out, cursor = [], ""
    try:
        for _ in range(pages):
            d = fetch(KALSHI + "/markets/trades?limit=1000" + ("&cursor=" + urllib.parse.quote(cursor) if cursor else ""), ttl=300)
            for t in d.get("trades", []):
                side = t.get("taker_side", "yes")
                price = fnum(t.get("yes_price_dollars") if side == "yes" else t.get("no_price_dollars"))
                usd = fnum(t.get("count_fp")) * price
                if usd >= min_usd:
                    tk = t.get("ticker", "")
                    if tk.startswith("KXMVE"):          # multi-leg combo tickets: no single market to name
                        continue
                    out.append({"venue": "Kalshi", "ticker": tk, "market": titles.get(tk) or tk, "outcome": side.upper(), "side": "buy", "price": price,
                                "usd": usd, "who": "block trade" if t.get("is_block_trade") else "", "at": t.get("created_time"),
                                "url": "https://kalshi.com/markets/" + tk.split("-")[0].lower()})
            cursor = d.get("cursor")
            if not cursor:
                break
        # the markets feed only covers the busiest markets; look up names for the biggest unnamed trades
        looked = {}
        for o in sorted(out, key=lambda o: -o["usd"]):
            tk = o["ticker"]
            if o["market"] != tk:
                continue
            if tk not in looked and len(looked) < 40:
                try:
                    m = fetch(KALSHI + "/markets/" + urllib.parse.quote(tk), ttl=86400).get("market", {})
                    title = (m.get("title") or "").replace("**", "").strip()
                    sub = (m.get("yes_sub_title") or "").strip()
                    looked[tk] = title + ((" — " + sub) if sub and sub.lower() not in title.lower() else "")
                except Exception:
                    looked[tk] = ""
            if looked.get(tk):
                o["market"] = looked[tk]
        _mark("Kalshi large trades", True, len(out))
    except Exception as e:
        _mark("Kalshi large trades", bool(out), len(out), str(e))
    return out


# ---------------------------------------------------------------------- Stocks
MARKET_WATCH = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^RUT", "Russell 2000"),
                ("^VIX", "VIX"), ("^TNX", "10Y Yield"), ("CL=F", "Crude Oil"), ("GC=F", "Gold"),
                ("BTC-USD", "Bitcoin"), ("DX-Y.NYB", "US Dollar")]
SECTORS = [("XLK", "Tech"), ("SMH", "Semis"), ("XLF", "Financials"), ("XLE", "Energy"), ("XLV", "Health"),
           ("ITA", "Defense"), ("XLI", "Industrials"), ("XLY", "Discretionary"), ("XLP", "Staples"), ("XLU", "Utilities")]


_yahoo_block_until = [0.0]
_yahoo_sem = threading.Semaphore(2)
CNBC_MAP = {"^GSPC": ".SPX", "^IXIC": ".IXIC", "^DJI": ".DJI", "^RUT": ".RUT", "^VIX": ".VIX",
            "^TNX": "US10Y", "CL=F": "@CL.1", "GC=F": "@GC.1", "BTC-USD": "BTC.CM=", "DX-Y.NYB": ".DXY"}


def _pack(symbol, name, pts):
    if len(pts) < 2:
        return None
    last, prev = pts[-1][1], pts[-2][1]
    m1 = pts[-22][1] if len(pts) > 22 else pts[0][1]
    return {"symbol": symbol, "name": name, "price": last, "chg1d": (last / prev - 1) * 100,
            "chg1m": (last / m1 - 1) * 100, "series": pts}


def _yahoo(symbol, rng):
    if time.time() < _yahoo_block_until[0]:
        return None
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (urllib.parse.quote(symbol), rng)
    with _yahoo_sem:
        try:
            r = fetch(url, ttl=900)["chart"]["result"][0]
        except Exception as e:
            if "429" in str(e):
                _yahoo_block_until[0] = time.time() + 600   # back off 10 min
            return None
    ts = r.get("timestamp") or []
    closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    pts = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c) for t, c in zip(ts, closes) if c is not None]
    return _pack(symbol, r.get("meta", {}).get("shortName") or symbol, pts)


def _nasdaq(symbol, rng):
    days = {"3mo": 100, "6mo": 190, "1y": 370}.get(rng, 190)
    start = (now_utc() - timedelta(days=days)).date().isoformat()
    for cls in ("stocks", "etf"):
        try:
            d = fetch("https://api.nasdaq.com/api/quote/%s/historical?assetclass=%s&fromdate=%s&limit=400"
                      % (urllib.parse.quote(symbol), cls, start), ttl=1800, headers={"Accept": "application/json"})
            rows = ((d.get("data") or {}).get("tradesTable") or {}).get("rows") or []
            pts = []
            for r in rows:
                m, dd, y = r["date"].split("/")
                pts.append(("%s-%s-%s" % (y, m, dd), fnum(r["close"].replace("$", "").replace(",", ""))))
            if pts:
                return _pack(symbol, symbol, sorted(pts))
        except Exception:
            continue
    return None


def _cnbc(symbol):
    code = CNBC_MAP.get(symbol)
    if not code:
        return None
    try:
        d = fetch("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=%s"
                  "&requestMethod=itv&noform=1&output=json" % urllib.parse.quote(code), ttl=600)
        q = d["FormattedQuoteResult"]["FormattedQuote"][0]
        return {"symbol": symbol, "name": q.get("shortName") or q.get("name"),
                "price": fnum(q.get("last", "").replace(",", "").replace("%", "")),
                "chg1d": fnum(q.get("change_pct", "").replace("%", "")), "chg1m": None, "series": []}
    except Exception:
        return None


def quote(symbol, rng="6mo"):
    """Daily closes. Yahoo first; Nasdaq for equities/ETFs and CNBC snapshots for
    indices/futures when Yahoo throttles."""
    q = _yahoo(symbol, rng)
    if q:
        return q
    if symbol in CNBC_MAP:
        return _cnbc(symbol)
    return _nasdaq(symbol, rng)


def price_on(q, day):
    """Close on or just after `day` (ISO date)."""
    if not q or not q.get("series"):
        return None
    for d, c in q["series"]:
        if d >= day:
            return c
    return None


def markets_overview():
    syms = MARKET_WATCH + SECTORS
    with ThreadPoolExecutor(4) as ex:
        qs = list(ex.map(lambda s: quote(s[0], "3mo"), syms))
    out = []
    for (sym, label), q in zip(syms, qs):
        if q:
            q["label"] = label
            q["group"] = "sector" if (sym, label) in SECTORS else "index"
            q["spark"] = [c for _, c in q["series"][-60:]]
            q.pop("series")
            out.append(q)
    _mark("Yahoo Finance", bool(out), len(out))
    return out


# -------------------------------------------------------------------- Congress
KADOA = "https://www.kadoa.com/congress/data/"


def _quiver_trades():
    """Quiver's public page (embedded JSON)."""
    page = fetch("https://www.quiverquant.com/congresstrading/", as_json=False, ttl=3600, timeout=40)
    m = re.search(r"recentTradesData\s*=\s*(\[\[.*?\]\])\s*;", page, re.S)
    rows = ast.literal_eval(m.group(1))
    trades = []
    for r in rows:
        amt = r[4] or ""
        nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]{3,}", amt)]
        trades.append({
            "ticker": (r[0] or "").upper(), "asset": r[1], "asset_type": r[2],
            "type": r[3], "amount": amt, "amount_low": nums[0] if nums else 0,
            "amount_high": nums[-1] if nums else 0, "member": r[5], "chamber": r[6],
            "party": r[7], "filed": (r[8] or "")[:10], "traded": (r[9] or "")[:10],
            "excess_return": r[12] if isinstance(r[12], (int, float)) else None,
            "bioguide": r[15] if len(r) > 15 else "", "photo": r[14] if len(r) > 14 else "",
            "source": "Quiver Quantitative (STOCK Act filings)",
        })
    return trades


def kadoa_trades(days=120):
    """Kadoa's open Congress Trading Monitor: House Clerk, Senate eFD and Office of Government Ethics
    filings, each linked to the original document, with late-filing flags. Also reads its lobbying
    file: companies that lobbied on bills sent to a committee the trading member sits on."""
    rows = fetch(KADOA + "trades.json", ttl=3600, timeout=60)
    bio = {}
    try:
        for f in fetch(KADOA + "filers.json", ttl=86400, timeout=40):
            m = re.search(r"/([A-Z]\d{6})\.jpg", f.get("photo_url") or "")
            bio[f["id"]] = (m.group(1) if m else "", f.get("photo_url") or "")
    except Exception:
        pass
    lobby = {}
    try:
        ov = fetch(KADOA + "oversight.json", ttl=6 * 3600, timeout=60)
        names = ov.get("committeeNames", {})
        for mb in ov.get("members", []):
            for tl in mb.get("topLinked", []):
                bills = tl.get("bills") or []
                if not bills:
                    continue
                on = [c for c in bills[0].get("committees", []) if c in (mb.get("committees") or [])]
                lobby[(mb["filerId"], tl["ticker"])] = {
                    "bill": bills[0].get("label", ""), "bill_title": (bills[0].get("title") or "")[:120],
                    "bills": len(bills), "committee": names.get(on[0], on[0]) if on else "",
                    "spend": tl.get("spend"), "spend_year": tl.get("spendYear")}
    except Exception:
        pass
    cutoff = (now_utc() - timedelta(days=days)).date().isoformat()
    out = []
    for r in rows:
        tk = (r.get("ticker") or "").upper()
        if not tk or (r.get("filing_date") or "") < cutoff or r.get("transaction_type") == "Exchange":
            continue
        ex = r.get("branch") == "executive"
        out.append({
            "ticker": tk, "asset": r.get("asset_name") or tk, "asset_type": r.get("asset_type") or "",
            "type": "Purchase" if "purchase" in (r.get("transaction_type") or "").lower() else "Sale",
            "amount": r.get("amount_range_label") or "", "amount_low": r.get("amount_range_low") or 0,
            "amount_high": r.get("amount_range_high") or r.get("amount_range_low") or 0,
            "member": r.get("filer_name") or "", "chamber": "Executive" if ex else (r.get("chamber") or "").title(),
            "party": r.get("party") or "", "agency": r.get("agency") or "", "office": r.get("office") or "",
            "filed": r.get("filing_date") or "", "traded": r.get("transaction_date") or r.get("filing_date") or "",
            "excess_return": r.get("excess_since"), "bioguide": bio.get(r.get("filer_id"), ("", ""))[0],
            "photo": bio.get(r.get("filer_id"), ("", ""))[1], "url": r.get("doc_url") or "",
            "late": bool(r.get("is_late")), "days_to_file": r.get("days_to_file"),
            "lobbying": lobby.get((r.get("filer_id"), tk)),
            "source": "Kadoa Congress Trading Monitor (%s)" % {"house_clerk": "House Clerk", "senate_efd": "Senate eFD",
                                                               "oge_executive": "Office of Government Ethics"}.get(r.get("source_id"), "filings"),
        })
    return out


def _trade_key(t):
    last = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", (t.get("member") or "").lower()).split()
    return (last[-1] if last else "", t.get("ticker"), t.get("traded"), "b" if "purchase" in (t.get("type") or "").lower() else "s")


def congress_trades():
    """Recent STOCK Act disclosures, merged from Quiver and Kadoa (which adds executive-branch officials,
    filing links, late flags and lobbying links). Fallback: House Clerk PTR index (names/dates only)."""
    quiver, kadoa, errs = [], [], []
    try:
        quiver = _quiver_trades()
    except Exception as e:
        errs.append("Quiver: " + str(e))
    _mark("Congress trades (Quiver)", bool(quiver), len(quiver), errs[-1] if errs else "")
    try:
        kadoa = kadoa_trades()
        _mark("Congress trades (Kadoa)", bool(kadoa), len(kadoa))
    except Exception as e:
        errs.append("Kadoa: " + str(e))
        _mark("Congress trades (Kadoa)", False, error=str(e))
    if not quiver and not kadoa:
        return house_ptr_index()
    by = {}
    for t in kadoa:
        by.setdefault(_trade_key(t), t)
    merged, used = [], set()
    for t in quiver:
        k = by.get(_trade_key(t))
        if k:                                               # same trade in both: keep Quiver's row, add Kadoa's extras
            used.add(_trade_key(t))
            for f in ("url", "late", "days_to_file", "lobbying"):
                t[f] = k.get(f)
            t["bioguide"] = t.get("bioguide") or k.get("bioguide")
            t["source"] = "Quiver + Kadoa (STOCK Act filings)"
        merged.append(t)
    merged += [t for key, t in by.items() if key not in used]
    merged.sort(key=lambda t: (t.get("filed") or "", t.get("traded") or "", t.get("amount_high") or 0), reverse=True)
    out, per = [], {}
    for t in merged:                                        # no one filer (e.g. hundreds of rows in one filing) crowds out the rest
        n = per.get(t["member"], 0)
        if n < 20:
            per[t["member"]] = n + 1
            out.append(t)
    return out


def house_ptr_index():
    import io
    import zipfile
    year = now_utc().year
    try:
        req = urllib.request.Request("https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%dFD.zip" % year,
                                     headers={"User-Agent": UA})
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=40).read()))
        root = ET.fromstring(z.read("%dFD.xml" % year))
        rows = []
        for mb in root.findall("Member"):
            if mb.findtext("FilingType") != "P":
                continue
            fd = datetime.strptime(mb.findtext("FilingDate"), "%m/%d/%Y").date().isoformat()
            doc = mb.findtext("DocID")
            rows.append({"ticker": "", "asset": "Periodic Transaction Report (PDF)", "type": "Filing",
                         "amount": "", "amount_low": 0, "amount_high": 0,
                         "member": "%s %s" % (mb.findtext("First"), mb.findtext("Last")),
                         "chamber": "House", "party": "", "filed": fd, "traded": fd, "excess_return": None,
                         "url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf" % (year, doc),
                         "source": "House Clerk"})
        rows.sort(key=lambda r: r["filed"], reverse=True)
        _mark("Congress trades", True, len(rows), "fallback: House Clerk index (no tickers)")
        return rows[:200]
    except Exception as e:
        _mark("Congress trades", False, error=str(e))
        return []


# ---------------------------------------------------- Optional: The Odds API
def odds_api(key):
    """Multi-book odds (requires free key from the-odds-api.com)."""
    if not key:
        return []
    out = []
    for sport in ["americanfootball_nfl", "americanfootball_ncaaf", "baseball_mlb", "basketball_wnba",
                  "basketball_nba", "icehockey_nhl", "soccer_epl"]:
        try:
            out.extend(fetch("https://api.the-odds-api.com/v4/sports/%s/odds?regions=us&markets=h2h&oddsFormat=american&apiKey=%s"
                             % (sport, key), ttl=1800))
        except Exception:
            continue
    _mark("The Odds API", bool(out), len(out))
    return out
