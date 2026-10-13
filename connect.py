"""Connections: world news <-> prediction markets <-> Congress trades <-> stocks.

Each theme (trade/China, Middle East & defense, rates, AI & chips, crypto, energy…)
collects today's headlines, the prediction markets that price it, sector ETF moves,
and congressional trades in exposed tickers. A trade is weighted up when the member
sits on a committee with jurisdiction over that theme (committee data from the
@unitedstates/congress-legislators project) and when it was placed shortly before
the theme's news/market activity. Output is a ranked list of theme "threads" with
a plain-English narrative. These are leads, not proof of anything.
"""
import re
from collections import defaultdict
from datetime import timedelta

from sources import _mark, fetch, now_utc, parse_dt

THEMES = [
    {"id": "trade", "name": "Trade, tariffs & China",
     "kw": "tariff tariffs trade truce china chinese xi beijing export exports import sanctions wto bessent",
     "tickers": "AAPL NVDA AMD AVGO QCOM TSLA NKE CAT DE BA GM F WMT AMZN MU INTC TXN LRCX AMAT",
     "etf": ["SMH", "XLI"], "committees": ["Ways and Means", "Finance", "Foreign Affairs", "Foreign Relations", "China"]},
    {"id": "mideast", "name": "Middle East, Iran & defense",
     "kw": "iran israel gaza hamas hezbollah tehran strike missile war pentagon military troops defense houthi netanyahu",
     "tickers": "LMT RTX NOC GD LHX HII LDOS PLTR KTOS AVAV BA", "etf": ["ITA"],
     "committees": ["Armed Services", "Intelligence", "Foreign Affairs", "Foreign Relations", "Homeland Security", "Appropriations"]},
    {"id": "ukraine", "name": "Russia–Ukraine",
     "kw": "russia russian ukraine ukrainian putin zelensky kyiv moscow nato",
     "tickers": "LMT RTX NOC GD LHX XOM CVX", "etf": ["ITA", "XLE"],
     "committees": ["Armed Services", "Foreign Affairs", "Foreign Relations", "Intelligence"]},
    {"id": "rates", "name": "Fed, rates & inflation",
     "kw": "fed federal reserve powell rate rates cut hike inflation cpi treasury yields yield bond jobs unemployment recession",
     "tickers": "JPM BAC WFC C GS MS SCHW BLK AXP COF V MA", "etf": ["XLF"],
     "committees": ["Financial Services", "Banking", "Budget", "Joint Economic"]},
    {"id": "ai", "name": "AI & semiconductors",
     "kw": "ai openai anthropic nvidia chip chips semiconductor gpu datacenter data center gpt claude gemini",
     "tickers": "NVDA AMD AVGO TSM QCOM MU ASML AMAT LRCX KLAC ARM SMCI MRVL MSFT GOOGL GOOG META AMZN ORCL",
     "etf": ["SMH", "XLK"], "committees": ["Science", "Commerce", "Energy and Commerce", "Intelligence", "China"]},
    {"id": "crypto", "name": "Crypto",
     "kw": "bitcoin btc crypto ethereum stablecoin coinbase sec etf blockchain",
     "tickers": "COIN MSTR HOOD", "etf": [], "committees": ["Financial Services", "Banking", "Agriculture"]},
    {"id": "energy", "name": "Oil & energy",
     "kw": "oil crude opec gas lng energy pipeline drilling barrel refinery",
     "tickers": "XOM CVX COP OXY SLB EOG PSX MPC VLO HAL KMI ET FSLR ENPH NEE", "etf": ["XLE"],
     "committees": ["Energy and Commerce", "Energy and Natural Resources", "Natural Resources", "Environment"]},
    {"id": "health", "name": "Health care & drug pricing",
     "kw": "fda drug drugs medicare medicaid vaccine pharma obesity insurer insurers health hospital",
     "tickers": "PFE MRK LLY JNJ ABBV UNH BMY AMGN GILD CVS HUM MRNA NVO ELV CI", "etf": ["XLV"],
     "committees": ["Energy and Commerce", "Health, Education", "Ways and Means", "Finance", "Aging"]},
    {"id": "bigtech", "name": "Big Tech, antitrust & platforms",
     "kw": "antitrust ftc doj monopoly apple google meta amazon microsoft app store privacy tiktok",
     "tickers": "AAPL MSFT GOOGL GOOG AMZN META NFLX", "etf": ["XLK"],
     "committees": ["Judiciary", "Energy and Commerce", "Commerce"]},
    {"id": "elections", "name": "Elections & politics",
     "kw": "election elections vote voters poll polls primary senate house midterm mayor governor trump democrats republicans",
     "tickers": "", "etf": [], "committees": ["Administration", "Rules"]},
    {"id": "autos", "name": "Autos & EVs",
     "kw": "tesla musk ev electric vehicle vehicles automaker autos uaw emissions",
     "tickers": "TSLA GM F RIVN LCID", "etf": ["XLY"], "committees": ["Energy and Commerce", "Commerce", "Transportation"]},
]
for t in THEMES:
    t["kwset"] = set(t["kw"].split())
    t["tickset"] = set(t["tickers"].split())


def words(s):
    return set(re.findall(r"[a-z]+", (s or "").lower()))


# --------------------------------------------------------- committees (YAML)
_comm = {}


def committees():
    """bioguide -> [committee names]. Tiny purpose-built YAML reader for the two files."""
    if "by" in _comm:
        return _comm["by"]
    base = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"
    names, by = {}, defaultdict(set)
    try:
        cur = None
        for line in fetch(base + "committees-current.yaml", as_json=False, ttl=7 * 86400).splitlines():
            if line.startswith("- type:"):
                cur = {}
            elif cur is not None and line.startswith("  name:"):
                cur["name"] = line.split(":", 1)[1].strip().strip("'\"")
            elif cur is not None and line.startswith("  thomas_id:"):
                names[line.split(":", 1)[1].strip().strip("'\"")] = cur.get("name", "")
        code = None
        for line in fetch(base + "committee-membership-current.yaml", as_json=False, ttl=7 * 86400).splitlines():
            m = re.match(r"^([A-Z0-9]+):\s*$", line)
            if m:
                code = m.group(1)
                continue
            m = re.match(r"^\s+bioguide:\s*(\S+)", line)
            if m and code:
                cname = names.get(code[:4])
                if cname:
                    by[m.group(1)].add(re.sub(r"^(House|Senate|Joint) (Committee on |Select Committee on |Permanent Select Committee on |Special Committee on )?(the )?", "", cname))
        _mark("Congress committees", bool(by), len(by))
    except Exception as e:
        _mark("Congress committees", False, error=str(e))
    _comm["by"] = {k: sorted(v) for k, v in by.items()}
    return _comm["by"]


def market_text(m):
    return (m.get("question", "") + " " + (m.get("sub") or "") + " " + (m.get("event") or "")).strip()


def build(news, pm, km, trades, tape):
    comm = committees()
    heads = [h for cat in ("world", "us", "business", "tech") for h in news.get(cat, [])]
    etf = {q["symbol"]: q for q in tape}
    cutoff = (now_utc() - timedelta(days=60)).date().isoformat()
    recent_trades = [t for t in trades if t.get("traded", "") >= cutoff and t.get("ticker")]
    threads = []
    for th in THEMES:
        hn = [h for h in heads if len(th["kwset"] & words(h["title"])) >= 1 and
              (len(th["kwset"] & words(h["title"])) >= 2 or len(th["kwset"] & words(h["title"] + " " + (h.get("summary") or ""))) >= 2)]
        mk = []
        for m in pm + km:
            hits = th["kwset"] & words(market_text(m))
            if len(hits) >= 1 and m.get("volume24h", 0) >= 500 and (len(hits) >= 2 or m.get("volume24h", 0) >= 20000):
                yes = m["prices"][0] if m["venue"] == "Polymarket" and m.get("prices") else m.get("mid")
                q = m.get("question", "") + ((" — " + m["sub"]) if m.get("sub") and m["sub"] not in m.get("question", "") else "")
                mk.append({"venue": m["venue"], "question": q[:140], "yes": yes, "change24h": m.get("change24h") or 0,
                           "volume24h": m.get("volume24h", 0), "url": m["url"]})
        mk.sort(key=lambda x: (abs(x["change24h"]) * 3 + min(x["volume24h"], 2e6) / 2e6), reverse=True)
        tr = []
        for t in recent_trades:
            if t["ticker"] not in th["tickset"]:
                continue
            mc = comm.get(t.get("bioguide") or "", [])
            rel = [c for c in mc if any(k.lower() in c.lower() for k in th["committees"])]
            tr.append(dict(t, committees=mc, relevant_committees=rel))
        tr.sort(key=lambda t: (bool(t["relevant_committees"]), t.get("amount_high", 0)), reverse=True)
        etfs = [etf[s] for s in th["etf"] if s in etf]
        if not hn and not mk and not tr:
            continue
        committee_trades = [t for t in tr if t["relevant_committees"]]
        buys = sum(1 for t in tr if "purchase" in t["type"].lower())
        sells = sum(1 for t in tr if "sale" in t["type"].lower())
        big_move = max([abs(m["change24h"]) for m in mk] or [0])
        score = (min(len(hn), 12) * 1.0 + min(len(mk), 8) * 1.5 + big_move * 20 + len(tr) * 1.2
                 + len(committee_trades) * 4 + sum(abs(e.get("chg1d") or 0) for e in etfs))
        # positioning: trades placed in the 21 days before today's news/market activity on the theme
        recent_cut = (now_utc() - timedelta(days=21)).date().isoformat()
        lead = [t for t in tr if (hn or mk) and t["traded"] >= recent_cut]
        threads.append({
            "id": th["id"], "name": th["name"], "score": round(score, 1),
            "news": hn[:8], "markets": mk[:8], "trades": tr[:12], "etfs": [{"symbol": e["symbol"], "label": e.get("label"), "chg1d": e.get("chg1d")} for e in etfs],
            "stats": {"headlines": len(hn), "markets": len(mk), "trades": len(tr), "committee_trades": len(committee_trades),
                      "buys": buys, "sells": sells, "lead_trades": len(lead)},
            "narrative": _story(th, hn, mk, tr, committee_trades, etfs, buys, sells, lead),
        })
    threads.sort(key=lambda t: t["score"], reverse=True)
    _mark("Connections engine", bool(threads), len(threads))
    return threads


def _story(th, hn, mk, tr, ct, etfs, buys, sells, lead):
    s = []
    if hn:
        s.append("%d headline%s today, led by \"%s\"." % (len(hn), "s" if len(hn) != 1 else "", hn[0]["title"][:110]))
    if mk:
        m = mk[0]
        s.append("Prediction markets are pricing it: %s — %s YES %s%s." % (
            m["question"][:90], m["venue"], ("%d¢" % round(m["yes"] * 100)) if m["yes"] is not None else "n/a",
            (" (%+d pts in 24h)" % round(m["change24h"] * 100)) if m["change24h"] else ""))
    if etfs:
        s.append("Sector tape: " + ", ".join("%s %+.2f%%" % (e["symbol"], e.get("chg1d") or 0) for e in etfs) + ".")
    if tr:
        s.append("Congress: %d disclosed trade%s in exposed stocks over 60 days (%d buy / %d sell)." % (len(tr), "s" if len(tr) != 1 else "", buys, sells))
    if ct:
        t = ct[0]
        s.append("%s (%s) sits on %s and %s %s %s on %s." % (
            t["member"], t["party"], ", ".join(t["relevant_committees"][:2]),
            "bought" if "purchase" in t["type"].lower() else "sold", t["amount"], t["ticker"], t["traded"]))
    if lead:
        s.append("%d of those trades were placed in the 3 weeks before today's activity on this theme." % len(lead))
    s.append("Correlation only — a lead to investigate, not evidence of wrongdoing.")
    return " ".join(s)


def theme_of(text):
    w = words(text)
    best, n = None, 0
    for th in THEMES:
        k = len(th["kwset"] & w)
        if k > n:
            best, n = th, k
    return best if n >= 1 else None


AMBIGUOUS_FIRST = set("""johnson morgan wells taiwan american america united general national first bank international
    global southern northern western eastern capital charles walt texas delta china chinese british royal canadian
    republic liberty freedom union pacific atlantic alaska hawaiian california florida new north south east west
    martin marathon ford lincoln jackson williams wilson thomas kennedy carter warner fox discovery target block
    public digital advanced applied analog energy power health medical home best dollar""".split())


def money_trail(news, trades, big_trades, threads):
    """One feed of money moves, each tied to the news it lines up with:
    Congress stock trades, plus large Polymarket and Kalshi trades."""
    heads = [h for cat in ("world", "us", "business", "tech") for h in news.get(cat, [])]
    comm = committees()
    by_theme = {t["id"]: t for t in threads}
    rows = []

    def related(text, theme, extra_words=()):
        tw = words(text) - {"will", "the", "win", "wins", "before", "after", "what", "who", "above", "below"}
        hits = []
        for h in heads:
            hw = words(h["title"])
            shared = {w for w in tw if len(w) > 3} & hw
            # two shared words only count when both are distinctive (not just a place name like "south carolina")
            direct = len(shared) if (len(shared) >= 3 or (len(shared) == 2 and all(len(w) >= 7 for w in shared))) else 0
            if direct >= 2 or any(x and re.search(r"\b%s\b" % re.escape(x), h["title"], re.I) for x in extra_words):
                hits.append((direct + 3, h))
            elif theme and len(theme["kwset"] & hw) >= 2:
                hits.append((1, h))
        hits.sort(key=lambda x: -x[0])
        return [{"title": h["title"], "link": h["link"], "source": h["source"], "strength": "direct" if s >= 3 else "theme"} for s, h in hits[:3]]

    for t in trades[:400]:
        if not t.get("ticker") or t["ticker"] == "-":
            continue
        lob = t.get("lobbying")
        theme = next((th for th in THEMES if t["ticker"] in th["tickset"]), None)
        company = re.sub(r"\b(INC|CORP|CORPORATION|CO|LTD|PLC|HOLDINGS?|GROUP|CLASS [A-C]|COMMON STOCK|THE)\b\.?,?", "", (t.get("asset") or "").split(" - ")[0], flags=re.I).strip().title()
        first = company.split(" ")[0] if company else ""
        if first.lower() in AMBIGUOUS_FIRST:                 # "Johnson" & Johnson is not Mayor Johnson; "Taiwan" Semi is not Taiwan
            first = ""
        rel = related(company, theme, extra_words=(company if len(company) >= 5 else "", first if len(first) >= 6 else ""))
        mc = comm.get(t.get("bioguide") or "", [])
        relc = [c for c in mc if theme and any(k.lower() in c.lower() for k in theme["committees"])]
        if not rel and not relc and not lob:
            continue
        tag = t.get("party") or t.get("agency") or t.get("chamber") or ""
        rows.append({"kind": "congress", "at": t["traded"], "who": "%s (%s)" % (t["member"], tag) if tag else t["member"],
                     "url": t.get("url") or "", "late": bool(t.get("late")), "source": t.get("source", ""),
                     "lobbying": _lobby_line(lob),
                     "what": "%s %s" % ("Bought" if "purchase" in t["type"].lower() else "Sold", t["ticker"]), "detail": company,
                     "size": t["amount"], "usd": (t.get("amount_low", 0) + t.get("amount_high", 0)) / 2,
                     "theme": theme and theme["name"], "committees": relc[:2], "news": rel,
                     "why": _why("congress", t, theme, relc, rel, lob)})
    for b in big_trades:
        theme = theme_of(b["market"])
        rel = related(b["market"], theme)
        rows.append({"kind": b["venue"].lower(), "at": b["at"], "who": b.get("who") or "anonymous trader",
                     "what": "%s %s at %d¢" % ("Bought" if b["side"] == "buy" else "Sold", b["outcome"], round(b["price"] * 100)),
                     "detail": b["market"], "size": "$%s" % f"{b['usd']:,.0f}", "usd": b["usd"], "theme": theme and theme["name"],
                     "committees": [], "news": rel, "url": b["url"], "why": _why("market", b, theme, [], rel)})
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: (bool(r["news"]) and r["news"][0]["strength"] == "direct", bool(r.get("lobbying")), bool(r["news"]), r["usd"]), reverse=True):
        k = (r["kind"], r["who"], r["what"], r["detail"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    # keep the feed balanced: every Congress row, the biggest prediction-market trades
    cong = [r for r in uniq if r["kind"] == "congress"]
    pm = [r for r in uniq if r["kind"] == "polymarket"][:50]
    ks = [r for r in uniq if r["kind"] == "kalshi"][:40]
    out = cong + pm + ks
    out.sort(key=lambda r: (bool(r["news"]) and r["news"][0]["strength"] == "direct", bool(r.get("lobbying")), bool(r["news"]), r["usd"]), reverse=True)
    return out


def _lobby_line(lob):
    """Kadoa's lobbying link in one sentence, or ''."""
    if not lob:
        return ""
    spend = (" (it spent $%.1fM on lobbying in %s)" % (lob["spend"] / 1e6, lob["spend_year"])) if lob.get("spend") else ""
    n = lob.get("bills", 1) - 1
    more = (" and %d other bill%s" % (n, "" if n == 1 else "s")) if n > 0 else ""
    return "The company lobbied on %s%s sent to %s%s" % (lob.get("bill") or "a bill", more,
                                                        lob.get("committee") or "a committee this member sits on", spend)


def _why(kind, x, theme, relc, rel, lob=None):
    bits = []
    if rel:
        bits.append("%s today's headline \"%s\" (%s)" % ("Lines up with" if rel[0]["strength"] == "direct" else "Same theme as", rel[0]["title"][:100], rel[0]["source"]))
    if relc:
        bits.append("the member sits on %s, which oversees this area" % " and ".join(relc))
    if lob:
        bits.append(_lobby_line(lob)[0].lower() + _lobby_line(lob)[1:] + ", per Kadoa")
    if x.get("late") and kind == "congress":
        bits.append("the trade was disclosed late (%s days after it was made)" % x.get("days_to_file"))
    if theme and not rel:
        bits.append("part of the %s theme" % theme["name"])
    if not bits:
        return "No related headline today."
    return "; ".join(bits)[0].upper() + "; ".join(bits)[1:] + ". A correlation, not proof of anything."
