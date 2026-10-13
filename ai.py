"""Optional Claude reasoning layer. Active only when the `anthropic` package is
installed and credentials are available (ANTHROPIC_API_KEY or `ant auth login`).
Claude reviews the engine's candidates plus news/social context, re-ranks them,
writes rationales, and narrates the congress money trail. It cannot change
prices or probabilities computed from live data."""
import json
import os
import re

MODEL = os.environ.get("EDGEDESK_MODEL", "claude-opus-5")

SYSTEM = """You are the analyst behind a daily betting and markets briefing.
You receive candidate bets already priced by a quantitative engine (model probability,
best available price, EV, confidence) together with the supporting evidence: historical
sports data, injuries, cross-venue prices, social-media chatter and headlines. You also
receive congressional trading "money trail" leads.

Your job:
1. Choose the 10 best bets from the candidates. Prefer real, explainable edges
   (cross-venue gaps, model/market disagreement backed by several independent sources)
   over thin or single-source edges. Down-rank anything where the evidence conflicts,
   the resolution rules might differ across venues, or social hype looks like the only driver.
2. For each pick write a 2-4 sentence rationale that cites the specific evidence given
   (numbers, sources, posts, headlines), and one sentence on the main risk.
3. For the top congressional trading leads, explain in 1-3 sentences how the trades,
   today's news and any linked prediction markets might relate. Be careful and neutral:
   these are public disclosures and correlations, not proof of misconduct. Say so when
   a link is weak.
4. Write a 3-5 sentence market brief tying together world news, the stock tape and
   prediction markets.

Only use facts present in the input. Do not invent statistics or events.
Respond with a single JSON object and nothing else, shaped as:
{"picks":[{"id":str,"rank":int,"verdict":"bet"|"lean","confidence":int,"rationale":str,"risk":str}],
 "money_trail":[{"ticker":str,"analysis":str}],
 "brief":str}"""


def available():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "anthropic package not installed (pip3 install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.path.isdir(os.path.expanduser("~/.config/anthropic"))):
        return False, "no Anthropic credentials (set ANTHROPIC_API_KEY)"
    return True, MODEL


def _slim(c):
    keep = ("id", "type", "market", "league", "title", "pick", "venue", "price", "p_model", "p_market", "ev", "confidence",
            "factors", "context", "starts", "model_components", "coaches", "public")
    d = {k: c.get(k) for k in keep}
    for k in ("p_model", "p_market", "ev"):
        d[k] = round(d[k] or 0, 4)
    s = c.get("social") or {}
    d["social"] = {"mentions": s.get("mentions"), "tone": s.get("tone"),
                   "samples": [p["text"][:220] for p in s.get("samples", [])[:3]]}
    d["news"] = [n["title"] for n in (c.get("news") or [])[:3]]
    return d


def review(candidates, trails, headlines, tape, threads=None):
    ok, info = available()
    if not ok:
        return None, info
    import anthropic
    payload = {
        "candidates": [_slim(c) for c in candidates[:25]],
        "money_trail": [{k: t[k] for k in ("ticker", "company", "theme", "members", "parties", "buys", "sells",
                                            "est_volume", "avg_excess_return", "flags")} |
                        {"news": [n["title"] for n in t["news"][:3]], "markets": [m["question"] for m in t["markets"][:2]],
                         "move_since_first_trade": t.get("move_since_first_trade")} for t in trails[:8]],
        "headlines": [h["title"] for h in headlines[:40]],
        "connections": [{"theme": t["name"], "summary": t["narrative"]} for t in (threads or [])[:6]],
        "stock_tape": [{"name": q.get("label"), "chg1d": round(q.get("chg1d") or 0, 2), "chg1m": q.get("chg1m") and round(q["chg1m"], 2)} for q in tape],
    }
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "output_config": {"effort": "medium"}},
        )
    except anthropic.RateLimitError as e:
        return None, "rate limited: %s" % e
    except anthropic.APIStatusError as e:
        return None, "API error %s: %s" % (e.status_code, e.message)
    except anthropic.APIConnectionError as e:
        return None, "connection error: %s" % e
    if resp.stop_reason == "refusal":
        return None, "model declined the request"
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None, "no JSON in response"
    try:
        return json.loads(m.group(0)), resp.model
    except json.JSONDecodeError as e:
        return None, "bad JSON: %s" % e
