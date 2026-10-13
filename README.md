# EdgeDesk

A local dashboard that picks **10 bets a day**, scores any bet or parlay you enter, and pulls
in world news, TLDR Tech, the stock market, and a congressional-trading "money trail".

```
python3 server.py            # http://127.0.0.1:8787, refreshes every 30 min
python3 server.py --once     # one run, saves data/picks/YYYY-MM-DD.json, exits
```

It uses only the Python standard library (3.9+) and needs no API keys. Every feed it reads by
default is free.

## Bet analyzer
Open **Bet analyzer**, choose where you're betting (any of 12 sportsbooks, Polymarket, Kalshi,
PrizePicks, Underdog, or Other), and enter one bet or a parlay of up to 12 legs: moneyline,
spread, total, player prop, or anything else with its odds. Each leg is priced the same way the
daily picks are (`analyzer.py`): the no-vig market, tilted a little by the team or player model.

The result is a **confidence score out of 100**, with the chance of winning, fair odds, expected
value, a suggested stake when the price is good, and a note when another book has a better price.
50 is a fair price; a standard sportsbook line with no edge lands near 32. Same-game legs are
flagged because their results move together. Lines the app can't match are scored on price alone.

## Optional add-ons
| Env / package | Adds |
|---|---|
| `pip3 install anthropic` + `ANTHROPIC_API_KEY` | Claude (Opus 5) re-ranks the picks and writes rationales, a market brief, and money-trail analysis |
| `ODDS_API_KEY` (free at the-odds-api.com) | Multi-sportsbook odds feed |
| `EDGEDESK_MODEL` | Override the Claude model |

## How picks are made
1. **All the books:** main lines come from Action Network, which covers DraftKings, FanDuel,
   BetMGM, Caesars, BetRivers, bet365, Fanatics, Hard Rock, theScore, Bally, consensus and the
   opener, plus public ticket and money splits. Bovada supplies main lines, alt spreads, alt
   totals and player props. Pinnacle supplies sharp lines, alts and props. Polymarket and Kalshi
   supply game markets.
   FanDuel's own public feed supplies player props, alt-line ladders and anytime scorers for
   free. (DraftKings, PrizePicks and Underdog block bots; DraftKings' main lines still arrive
   through Action Network.)
2. **Fair price:** Pinnacle's no-vig price, or the median across books when Pinnacle is missing.
   Stale outlier prices and the opener are excluded.
3. **Statistical team models** (`stats.py`):
   - **NFL:** nflverse EPA per play and points for/against (2025 regressed into 2026), pace, pass
     rate, turnover margin, rest and wind. The coach factor is every head coach's wins versus
     closing-line expectation since 2006, heavily shrunk.
   - **MLB:** team OPS and runs per game, team ERA/WHIP, Statcast starter xERA feeding a Poisson
     run model. The manager factor is the one-run and extra-inning record.
   - **NHL:** regressed goals for/against feeding a Poisson model, plus the ESPN coach.
   - **Soccer:** time-weighted goals plus xG in a Poisson model (home/draw/away, totals). The
     tactical profile covers shots, accuracy, corners, fouls and cards. The manager comes from
     Wikipedia.
   - **NBA, WNBA and NCAAF:** shrunk season margins, ESPN team stats (pace and shot profile), and
     the ESPN coach record.
4. **Pricing** (`pricing.py`): moneylines, spreads, totals and every alternate line are priced
   from the model's margin and total distributions (normal, or a Poisson grid), then blended
   with the fair price. The market carries 3× the weight, and the model can tilt it by at most
   4 points. Every bet is shopped for the best price.
5. **Player props** (`props.py`): each player's game logs (nflverse for NFL, ESPN elsewhere) are
   adjusted for the opposing defense and modeled as Poisson or normal, then blended with
   Pinnacle's no-vig line. Lottery-ticket prices and apparent role changes are down-weighted.
6. **Social, news and public money** adjust confidence. Ranking is EV × confidence, with at most
   one bet per game and capped numbers of props, alt lines and prediction markets.

## Added in the latest pass
- **Apify** (`apify_src.py`, optional and paid): DraftKings player props with alt-line ladders,
  Underdog and PrizePicks lines. It is **off by default** and runs only with `EDGEDESK_APIFY=1`
  and `APIFY_TOKEN` set. It is pay-per-use on your Apify account, about
  $0.35 per NFL pull, capped per run and cached for 3 hours. Tune it with
  `EDGEDESK_APIFY_LEAGUES`, `EDGEDESK_APIFY_CAP_USD` and `EDGEDESK_APIFY_TTL`.
- **Matchup depth**:
  - current form and trend, for teams and players
  - offensive and defensive schemes (pass rate, depth of target, tempo, pass rush, run and pass defense)
  - coach vs coach, and coach vs franchise
  - player head-to-head vs this opponent, and vs the opposing head coach (4 seasons)
  - against-the-spread history: overall, by role, and head-to-head
- **Weather** (`weather.py`): Open-Meteo game-time forecast for outdoor venues. It feeds totals
  and passing props.
- **Parlays**: 2-, 3- and 4-leg suggestions built from positive-edge picks, one leg per game,
  priced at a single sportsbook.
- **Money trail**: Congress trades plus large Polymarket and Kalshi trades, each linked to the
  headline it lines up with. Sources include Mother Jones.
- **last30days** (`research.py`): if the /last30days skill is installed and set up, and Python
  3.12+ exists, the top 3 picks get a quick 14-day research pass.

## Hosting
```
EDGEDESK_HOST=0.0.0.0 EDGEDESK_PASSWORD=choose-one PORT=8787 python3 server.py
```
With `EDGEDESK_PASSWORD` set, the app asks for HTTP basic auth: user `edgedesk` and that
password. It is one always-on Python process with no database, so any small VM or container
host works. Mount `data/` on a persistent disk if you want pick history kept.

**Phone access on this Mac:** put a value after `EDGEDESK_PASSWORD=` in `.env` and restart. The
app then listens on the network (not just this Mac) and is at `http://<mac-name>.local:8787`
from devices on the same Wi-Fi. Requests from the Mac itself skip the password, so the Desktop
launcher and the morning email keep working. The email's "Full board" link switches to that
address; set `EDGEDESK_URL` to override it (for example a Tailscale address).

## Connections (`connect.py`)
Each news theme (trade and China, the Middle East and defense, rates, AI and chips, crypto,
energy, health, Big Tech, elections, autos) is linked to:
- the prediction markets that price it
- sector ETF moves
- Congress trades in exposed stocks

Trades by members sitting on a committee with jurisdiction over the theme are flagged. Committee
data comes from @unitedstates/congress-legislators. Trades placed in the three weeks before
today's activity are called out. These are correlations, not accusations.

## Congress money trail
Recent STOCK Act trades are merged from two sources: Quiver Quantitative's public page and Kadoa's open Congress Trading Monitor (kadoa.com/congress), which adds executive-branch officials, a link to each original filing, late-disclosure flags, and lobbying links (the company lobbied on a bill sent to a committee the member sits on). Trades found in both are counted once. If that fails, the
app falls back to the House Clerk PTR index. Each ticker is linked to:
- today's headlines (company name or sector themes)
- related Polymarket and Kalshi markets
- the price move since the trade

Flags cover clusters, late filings (>45 days), large trades, and bipartisan interest.
These are correlations, not accusations.

*For research and entertainment only. No edge is guaranteed. Bet responsibly and within the law where you live.*
