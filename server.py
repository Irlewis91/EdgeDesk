#!/usr/bin/env python3
"""EdgeDesk — daily bets, prediction markets, sports history, news, and the
congressional money trail. Stdlib-only local web app.

  python3 server.py            # serve http://127.0.0.1:8787 and refresh every 30 min
  python3 server.py --once     # run the pipeline once, save today's picks, exit
"""
import argparse
import json
import os


def _load_env():
    """Read KEY=value lines from .env next to this file (keys you paste yourself, never committed)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip():
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except FileNotFoundError:
        pass


_load_env()
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai
import analyzer as AZ
import apify_src as AP
import books as B
import research as R
import connect as C
import engine as E
import history as H
import mailer as M
import pricing as PR
import sources as S
import stats as ST
import weather as W

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
PICKS = os.path.join(DATA, "picks")
os.makedirs(PICKS, exist_ok=True)

STATE = {"dashboard": None, "running": False, "progress": "", "last_error": "", "ctx": None}
CTX_FILE = os.path.join(DATA, "analyzer.json")


def analyzer_ctx():
    """Full games (lines, alt lines, props, models) and every priced prop from the last refresh."""
    if STATE["ctx"] is None and os.path.exists(CTX_FILE):
        try:
            with open(CTX_FILE) as fh:
                d = json.load(fh)
            STATE["ctx"] = {"games": {str(g["id"]): g for g in d["games"]}, "props": d["props"]}
        except Exception:
            pass
    return STATE["ctx"]


def step(msg):
    STATE["progress"] = msg
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run_pipeline():
    if STATE["running"]:
        return
    STATE["running"] = True
    t0 = time.time()
    try:
        step("Fetching markets, sports, news, congress, social…")
        with ThreadPoolExecutor(10) as ex:
            f = {
                "pm": ex.submit(S.polymarket_markets, 400),
                "pm_games": ex.submit(S.polymarket_games),
                "km": ex.submit(S.kalshi_markets, 4),
                "ks": ex.submit(S.kalshi_sports),
                "games": ex.submit(S.espn_games, 96, 180),
                "news": ex.submit(S.news),
                "trades": ex.submit(S.congress_trades),
                "tape": ex.submit(S.markets_overview),
                "reddit": ex.submit(S.reddit_hot),
                "sleeper": ex.submit(H.sleeper_trending),
                "books": ex.submit(B.all_books),
                "apify": ex.submit(AP.fetch_all),
                "pm_trades": ex.submit(S.polymarket_trades),
            }
            r = {k: v.result() for k, v in f.items()}
        r["books"]["apify"] = r["apify"]
        titles = {m["ticker"]: (m["question"] + ((" — " + m["sub"]) if m.get("sub") and m["sub"] not in m["question"] else ""))
                  for m in r["km"] + r["ks"] if m.get("ticker")}
        big_trades = r["pm_trades"] + S.kalshi_trades(titles)

        step("Enriching %d games with historical data…" % len(r["games"]))
        games = H.enrich(r["games"])
        step("Building team models (stats, coaches, tactics)…")
        games = ST.build(games)
        step("Pulling game-time weather for outdoor venues…")
        games = W.apply(games)
        headlines = [h for cat in ("world", "us", "business", "sports", "tech") for h in r["news"].get(cat, [])]

        step("Line shopping across every book; pricing lines, alts and player props…")
        games, sport_cands, prop_cands = PR.price_all(games, r["books"], r["pm_games"], r["ks"])
        pred_cands = E.prediction_candidates(r["pm"], r["km"], headlines)
        cands = sport_cands + prop_cands + pred_cands
        cands.sort(key=E.score, reverse=True)
        step("Pulling social chatter for top %d candidates…" % min(40, len(cands)))
        cands = E.attach_social(cands, r["reddit"], headlines, 40, r["news"].get("sports", []))
        bets = E.top_bets(list(cands), 10)
        props_board = E.board([c for c in cands if c.get("market") in ("prop", "alt_prop")], 40)
        alt_board = E.board([c for c in cands if c.get("market") in ("alt_spread", "alt_total")], 20)
        parlays = E.parlays(cands)
        step("Researching the top picks with last30days (if installed)…")
        bets = R.attach(bets)
        line_board = E.board([c for c in cands if c.get("market") in ("spread", "total", "moneyline")], 30)

        step("Connecting news, prediction markets and Congress trades…")
        threads = C.build(r["news"], r["pm"], r["km"], r["trades"], r["tape"])
        money = C.money_trail(r["news"], r["trades"], big_trades, threads)

        step("Tracing congressional money trail…")
        trails = E.money_trail(r["trades"], r["news"], r["pm"], r["km"])
        members = E.member_board(r["trades"])

        ai_note, brief = "", ""
        ok, info = ai.available()
        if ok:
            step("Asking Claude to review picks and money trail…")
            pool = sorted(cands, key=E.score, reverse=True)[:25]
            for c in pool:
                c.setdefault("rationale", E.rationale(c))
            res, info = ai.review(pool, trails, headlines, r["tape"], threads)
            if res:
                by_id = {c["id"]: c for c in pool}
                ai_bets = []
                for p in sorted(res.get("picks", []), key=lambda p: p.get("rank", 99)):
                    c = by_id.get(p.get("id"))
                    if c and c not in ai_bets:
                        c.update({"ai_rationale": p.get("rationale"), "ai_risk": p.get("risk"),
                                  "ai_verdict": p.get("verdict"), "ai_confidence": p.get("confidence"),
                                  "score": round(E.score(c) * 1000, 1)})
                        ai_bets.append(c)
                if len(ai_bets) >= 5:
                    bets = ai_bets[:10]
                    for c in bets:
                        c["plain_bet"] = E.plain_bet(c)
                tmap = {x.get("ticker"): x.get("analysis") for x in res.get("money_trail", [])}
                for t in trails:
                    if tmap.get(t["ticker"]):
                        t["ai_analysis"] = tmap[t["ticker"]]
                brief = res.get("brief", "")
                ai_note = "Reviewed by %s" % info
            else:
                ai_note = "Claude review skipped: %s" % info
        else:
            ai_note = "Claude review off — %s. Picks use the quantitative engine only." % info

        dash = {
            "generated": S.now_utc().isoformat(), "took_s": round(time.time() - t0, 1),
            "bets": bets, "ai": ai_note, "brief": brief,
            "candidates_considered": len(cands),
            "games": [{k: v for k, v in g.items() if k not in ("props", "alts")} | {"n_props": len(g.get("props", [])), "n_alts": len(g.get("alts", []))}
                      for g in games if g.get("lines") or g.get("model")],
            "props_board": props_board, "alt_board": alt_board, "line_board": line_board, "connections": threads,
            "parlays": parlays, "money_trail": money,
            "counts": {"books": len({b for g in games for b in g.get("lines", {})}), "games": len(games),
                       "props": sum(len(g.get("props", [])) for g in games), "alts": sum(len(g.get("alts", [])) for g in games),
                       "candidates": len(cands)},
            "prediction": {
                "polymarket": sorted(r["pm"], key=lambda m: m["volume24h"], reverse=True)[:60],
                "kalshi": sorted(r["km"], key=lambda m: m["volume24h"], reverse=True)[:60],
                "movers": sorted([m for m in r["pm"] + r["km"] if m["volume24h"] >= 1000],
                                 key=lambda m: abs(m.get("change24h") or 0), reverse=True)[:25],
            },
            "news": r["news"], "tape": r["tape"], "trails": trails[:30], "members": members,
            "trades": r["trades"][:150], "sleeper": r["sleeper"],
            "social_pulse": sorted(r["reddit"], key=lambda p: p.get("at") or "", reverse=True)[:40],
            "status": S.STATUS, "platforms": AZ.PLATFORMS,
        }
        STATE["dashboard"] = dash
        STATE["ctx"] = {"games": {str(g["id"]): g for g in games}, "props": list(PR.PROP_ROWS)}
        with open(CTX_FILE, "w") as fh:
            json.dump({"games": games, "props": STATE["ctx"]["props"]}, fh, default=str)
        day = time.strftime("%Y-%m-%d")
        with open(os.path.join(DATA, "latest.json"), "w") as fh:
            json.dump(dash, fh, default=str)
        with open(os.path.join(PICKS, day + ".json"), "w") as fh:
            json.dump({"generated": dash["generated"], "bets": bets, "ai": ai_note, "brief": brief}, fh, default=str, indent=1)
        step("Done in %.0fs — %d candidates, %d picks." % (time.time() - t0, len(cands), len(bets)))
        STATE["last_error"] = ""
    except Exception as e:
        traceback.print_exc()
        STATE["last_error"] = str(e)
        step("Pipeline failed: %s" % e)
    finally:
        STATE["running"] = False


def refresher(minutes):
    while True:
        run_pipeline()
        time.sleep(minutes * 60)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        pw = os.environ.get("EDGEDESK_PASSWORD")
        if not pw or self.client_address[0] in ("127.0.0.1", "::1"):   # this Mac itself never needs the password
            return True
        import base64
        import hmac
        got = self.headers.get("Authorization", "")
        want = "Basic " + base64.b64encode(("edgedesk:" + pw).encode()).decode()
        if hmac.compare_digest(got, want):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EdgeDesk"')
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authed():
            return
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(ROOT, "static", "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if p == "/api/dashboard":
            d = STATE["dashboard"]
            if d is None and os.path.exists(os.path.join(DATA, "latest.json")):
                with open(os.path.join(DATA, "latest.json")) as fh:
                    d = STATE["dashboard"] = json.load(fh)
            return self._send(200, json.dumps({"dashboard": d, "running": STATE["running"],
                                               "progress": STATE["progress"], "error": STATE["last_error"]}, default=str))
        if p in ("/api/email", "/email"):
            d = STATE["dashboard"]
            if d is None and os.path.exists(os.path.join(DATA, "latest.json")):
                with open(os.path.join(DATA, "latest.json")) as fh:
                    d = STATE["dashboard"] = json.load(fh)
            if d is None:
                return self._send(503, '{"error":"no data yet"}')
            mail = M.render(d)
            if p == "/email":                               # browser preview of the daily email
                return self._send(200, mail["html"], "text/html; charset=utf-8")
            return self._send(200, json.dumps(mail))
        if p == "/api/analyzer/props":                    # players, stats and lines for one game
            ctx = analyzer_ctx()
            gid = (self.path.split("game=")[1].split("&")[0] if "game=" in self.path else "")[:40]
            g = ctx and ctx["games"].get(gid)
            return self._send(200, json.dumps({"props": AZ.prop_menu(g) if g else []}))
        if p == "/api/history":
            days = sorted((f[:-5] for f in os.listdir(PICKS) if f.endswith(".json")), reverse=True)
            return self._send(200, json.dumps(days))
        if p.startswith("/api/history/"):
            f = os.path.join(PICKS, os.path.basename(p) + ".json")
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read())
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/api/refresh":
            if not STATE["running"]:
                threading.Thread(target=run_pipeline, daemon=True).start()
            return self._send(202, '{"ok":true}')
        if self.path == "/api/analyze":
            ctx = analyzer_ctx()
            if ctx is None:
                return self._send(503, '{"error":"The board is still loading. Try again in a couple of minutes."}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 100000:
                    raise ValueError
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError
            except Exception:
                return self._send(400, '{"error":"Could not read that bet."}')
            try:
                return self._send(200, json.dumps(AZ.analyze(body, ctx), default=str))
            except Exception as e:
                traceback.print_exc()
                return self._send(500, json.dumps({"error": "Could not analyze that bet (%s)." % str(e)[:80]}))
        return self._send(404, '{"error":"not found"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run once, save picks, exit")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--refresh", type=int, default=30, help="minutes between refreshes")
    a = ap.parse_args()
    if a.once:
        run_pipeline()
        return 0 if STATE["dashboard"] else 1
    threading.Thread(target=refresher, args=(a.refresh,), daemon=True).start()
    # With a password set, other devices on the network (your phone) can reach the app; without one it stays on this Mac only.
    host = os.environ.get("EDGEDESK_HOST") or ("0.0.0.0" if os.environ.get("EDGEDESK_PASSWORD") else "127.0.0.1")
    if host != "127.0.0.1" and not os.environ.get("EDGEDESK_PASSWORD"):
        print("EDGEDESK_HOST is set but EDGEDESK_PASSWORD is not; staying on 127.0.0.1", flush=True)
        host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    print("EdgeDesk running at http://%s:%d" % (host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
