"""Optional last30days research on the top picks.

If the /last30days skill engine is installed, set up (SETUP_COMPLETE=true in
~/.config/last30days/.env) and a Python 3.12+ interpreter exists, run a quick
14-day pass on each top pick's matchup/player and attach what people are saying
(Reddit, X, YouTube, HN, Polymarket…) to the pick. Cached per topic per day.
Never runs the skill's setup wizard itself - that needs your consent choices.
"""
import glob
import hashlib
import json
import os

import paths
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from sources import _mark

CACHE_DIR = os.path.join(paths.DATA, "research")
NAME = "last30days research"


def _python():
    for c in [os.environ.get("LAST30DAYS_PYTHON"), "python3.14", "python3.13", "python3.12"]:
        if c and shutil.which(c):
            return shutil.which(c)
    uv = shutil.which("uv") or next((x for x in (os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.cargo/bin/uv"))
                                     if os.path.exists(x)), None)
    if uv:
        try:
            p = subprocess.run([uv, "python", "find", ">=3.12"], capture_output=True, text=True, timeout=20).stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    return None


def _engine():
    pats = [os.environ.get("LAST30DAYS_ENGINE", ""),
            "~/.claude/skills/last30days/scripts/last30days.py",
            "~/.claude/plugins/cache/last30days-skill/last30days/*/skills/last30days/scripts/last30days.py",
            "~/.claude/plugins/cache/last30days-skill/last30days/*/scripts/last30days.py",
            "~/Library/Application Support/Claude/local-agent-mode-sessions/*/*/rpm/*/skills/last30days/scripts/last30days.py"]
    for p in pats:
        hits = sorted(glob.glob(os.path.expanduser(p))) if p else []
        if hits:
            return hits[-1]
    return None


def status():
    eng, py = _engine(), _python()
    if not eng:
        return None, None, "last30days skill not installed"
    if not py:
        return None, None, "needs Python 3.12+ (install uv, then: uv python install 3.12)"
    try:
        with open(os.path.expanduser("~/.config/last30days/.env")) as fh:
            if "SETUP_COMPLETE=true" not in fh.read():
                raise FileNotFoundError
    except FileNotFoundError:
        return None, None, "run /last30days once in Claude to complete its first-run setup"
    return eng, py, "ready"


def _topic(c):
    if c.get("market") in ("prop", "alt_prop"):
        return "%s %s" % (c.get("player", ""), c.get("league", ""))
    if c["type"] == "prediction":
        return c.get("question", c["title"])[:90]
    return re.sub(r"\s+@\s+", " vs ", c["title"])


def _run(eng, py, topic):
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.md5((time.strftime("%Y-%m-%d") + topic).encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    try:
        out = subprocess.run([py, eng, topic, "--quick", "--days=14", "--emit=compact", "--auto-resolve"],
                             capture_output=True, text=True, timeout=280,
                             env=dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", ""))).stdout
    except Exception as e:
        return {"topic": topic, "error": str(e)[:120]}
    body = out.split("<!-- PASS-THROUGH FOOTER")[0]
    clusters = []
    for m in re.finditer(r"^#{3,4} \d+\. (.+?)(?: \(score (\d+), (\d+) items?, sources: ([^)]+)\))?\s*$", body, re.M):
        nxt = body[m.end():m.end() + 900]
        meta = re.search(r"-\s+(\d{4}-\d{2}-\d{2}) \| ([^|\n]+)\|", nxt)
        url = re.search(r"URL: \[(https?://[^\]]+)\]", nxt)
        clusters.append({"title": m.group(1).strip()[:160], "score": int(m.group(2) or 0), "items": int(m.group(3) or 1),
                         "sources": (m.group(4) or (meta.group(2).strip() if meta else "")), "url": url.group(1) if url else ""})
    quotes = sorted(((int(v.replace(",", "")), "%s: %s" % (who, txt.strip()[:220]))
                     for who, v, txt in re.findall(r"^\s*-\s+((?:u/|@)[\w.-]+) \(([\d,]+) (?:upvotes|likes)\): (.+)$", body, re.M)), reverse=True)
    foot = re.search(r"✅ All agents reported back!(.*?)\n---", out, re.S)
    res = {"topic": topic, "clusters": sorted(clusters, key=lambda c: -c["score"])[:6], "quotes": [q for _, q in quotes[:4]],
           "coverage": [l.strip("├─└ ").strip() for l in (foot.group(1).splitlines() if foot else []) if l.strip()][:8], "at": time.time()}
    with open(path, "w") as fh:
        json.dump(res, fh)
    return res


def attach(bets, n=3):
    eng, py, why = status()
    if not eng:
        _mark(NAME, False, error="off: " + why)
        return bets
    picks = bets[:n]
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda c: _run(eng, py, _topic(c)), picks))
    done = 0
    for c, r in zip(picks, results):
        if r.get("clusters") or r.get("quotes"):
            c["research"] = r
            done += 1
            if r["clusters"]:
                c["context"] = list(c.get("context", [])) + ["Last 30 days: " + "; ".join(x["title"] for x in r["clusters"][:2])]
    _mark(NAME, done > 0, done, "" if done else "ran but found nothing usable")
    return bets
