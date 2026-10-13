"""Game-time weather for outdoor venues (Open-Meteo: free, no key) and its effect on
totals and passing props.

  Football  wind is the big one: each mph over 10 trims the total and passing volume;
            heavy rain/snow and deep cold trim a little more.
  Baseball  warm air carries: about +0.1 runs per 10°F above 70 (and the reverse).
  Soccer    strong wind or heavy rain trims expected goals slightly.
Indoor/dome venues are skipped.
"""
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from sources import _mark, fetch, parse_dt

DAY = 86400


def _geo(city, state):
    try:
        d = fetch("https://geocoding-api.open-meteo.com/v1/search?count=5&name=" + urllib.parse.quote(city), ttl=30 * DAY)
        res = d.get("results") or []
        hit = next((r for r in res if state and (r.get("admin1", "").lower().startswith(state.lower()) or r.get("admin1_code", "") == state
                                                 or state.upper() in (r.get("admin1", "")[:2].upper(),))), None) or (res[0] if res else None)
        return (hit["latitude"], hit["longitude"]) if hit else None
    except Exception:
        return None


def forecast(g):
    v = g.get("venue") or {}
    if v.get("indoor") or not (v.get("address") or {}).get("city"):
        return {"indoor": True, "summary": "Indoors"} if v.get("indoor") else None
    ll = _geo(v["address"]["city"], v["address"].get("state", ""))
    when = parse_dt(g["date"])
    if not ll or not when:
        return None
    try:
        f = fetch("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m,wind_speed_10m,wind_gusts_10m,"
                  "precipitation_probability,precipitation&temperature_unit=fahrenheit&wind_speed_unit=mph&forecast_days=7&timezone=UTC" % ll, ttl=3600)
        h = f["hourly"]
        key = when.strftime("%Y-%m-%dT%H:00")
        i = h["time"].index(key)
        sl = slice(i, i + 3)                       # the ~3 hours of the game
        avg = lambda k: sum(x or 0 for x in h[k][sl]) / max(1, len(h[k][sl]))
        w = {"indoor": False, "temp_f": round(avg("temperature_2m")), "wind_mph": round(avg("wind_speed_10m")),
             "gust_mph": round(max(x or 0 for x in h["wind_gusts_10m"][sl])), "precip_prob": round(max(x or 0 for x in h["precipitation_probability"][sl])),
             "precip_in": round(sum(x or 0 for x in h["precipitation"][sl]) / 25.4, 2)}
        w["summary"] = "%d°F, wind %d mph (gusts %d), %d%% chance of precipitation" % (w["temp_f"], w["wind_mph"], w["gust_mph"], w["precip_prob"])
        return w
    except Exception:
        return None


def effect(g, w):
    """Returns {total_pts, pass_factor, note} for the model."""
    if not w or w.get("indoor"):
        return None
    sport, notes = g["sport"], []
    total, pf = 0.0, 1.0
    wet = w["precip_prob"] >= 60
    if sport == "football":
        if w["wind_mph"] > 10:
            total -= min(4.0, (w["wind_mph"] - 10) * 0.3)
            pf -= min(0.14, (w["wind_mph"] - 10) * 0.012)
            notes.append("wind %d mph" % w["wind_mph"])
        if wet:
            total -= 1.0
            pf -= 0.03
            notes.append("%d%% rain/snow" % w["precip_prob"])
        if w["temp_f"] <= 25:
            total -= 1.0
            pf -= 0.02
            notes.append("%d°F" % w["temp_f"])
    elif sport == "baseball":
        total += max(-0.5, min(0.5, (w["temp_f"] - 70) / 10 * 0.1))
        if abs(w["temp_f"] - 70) >= 12:
            notes.append("%d°F" % w["temp_f"])
        if wet:
            notes.append("%d%% rain (delay/postponement risk)" % w["precip_prob"])
    elif sport == "soccer":
        if w["wind_mph"] >= 18 or (wet and w["precip_in"] >= 0.2):
            total -= 0.12
            notes.append("wind %d mph" % w["wind_mph"] if w["wind_mph"] >= 18 else "heavy rain")
    if not notes:
        return {"total_pts": 0.0, "pass_factor": 1.0, "note": "Weather neutral: " + w["summary"]}
    return {"total_pts": round(total, 2), "pass_factor": round(pf, 3),
            "note": "Weather (%s): total %+.1f%s" % (", ".join(notes), total, (", passing volume x%.2f" % pf) if pf != 1 else "")}


def apply(games):
    outdoor = [g for g in games if g["sport"] in ("football", "baseball", "soccer")]
    with ThreadPoolExecutor(6) as ex:
        ws = list(ex.map(forecast, outdoor))
    n = 0
    for g, w in zip(outdoor, ws):
        if not w:
            continue
        g["weather"] = w
        e = effect(g, w)
        if not e:
            continue
        g["weather"]["effect"] = e
        n += 1
        m = g.get("model")
        if not m:
            continue
        if m.get("poisson") and m.get("total_mu"):
            k = (m["total_mu"] + e["total_pts"]) / m["total_mu"]
            m["lam_home"] *= k
            m["lam_away"] *= k
            m["total_mu"] = m["lam_home"] + m["lam_away"]
        elif m.get("total_mu"):
            m["total_mu"] += e["total_pts"]
        m["components"] = [c for c in m.get("components", []) if "wind" not in c or "Home field" not in c] + [e["note"]]
    _mark("Weather (Open-Meteo)", n > 0, n)
    return games
