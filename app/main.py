from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, requests

app = FastAPI(title="Trade Radar (MVP)")


CACHE_TTL = 60
_cache = {"ts": 0, "data": None}

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def get_json(url, timeout=15):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def score_coin(p24, vol24_usdt, spread):
    p24c = clamp(p24, -20.0, 40.0)
    m = (p24c + 20.0) / 60.0 * 100.0
    if p24 > 120:
        m -= 40
    elif p24 > 60:
        m -= 20
    m = clamp(m, 0, 100)

    v = clamp((math.log10(max(vol24_usdt, 1.0)) - 6.0) / (8.0 - 6.0) * 100.0, 0, 100)
    s = clamp((0.008 - spread) / (0.008 - 0.0005) * 100.0, 0, 100)

    base = 0.45 * m + 0.35 * v + 0.20 * s
    return round(clamp(base, 0, 100), 1)

def build_trade_plan(last_price):
    entry = last_price
    stop = last_price * 0.97
    tp1  = last_price * 1.04
    tp2  = last_price * 1.07
    return {"entry": round(entry, 8), "stop": round(stop, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8)}

def get_top_picks():
    url = "https://api.coincap.io/v2/assets?limit=200"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        return {
            "ts": int(time.time()),
            "market_mode": "UNKNOWN",
            "top_picks": [],
            "error": repr(e)
        }

    rows = []
    for coin in data:
        try:
            price = float(coin.get("priceUsd", 0))
            p24 = float(coin.get("changePercent24Hr", 0))
            vol = float(coin.get("volumeUsd24Hr", 0))
        except:
            continue

        if vol < 1_000_000:
            continue
        if price <= 0:
            continue

        score = score_coin(p24=p24, vol24_usdt=vol, spread=0.002)

        rows.append({
            "symbol": coin.get("symbol", ""),
            "price": round(price, 6),
            "chg24_pct": round(p24, 2),
            "vol24_usdt": int(vol),
            "spread_pct": 0.2,
            "score": score,
            "plan": build_trade_plan(price)
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    return {
        "ts": int(time.time()),
        "market_mode": "RISK-ON",
        "top_picks": rows[:10]
    }
@app.get("/api/top", response_class=JSONResponse)
def api_top():
    return get_top_picks()

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <style>body{font-family:Arial;margin:20px;max-width:900px}.card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}</style>
    </head><body>
    <h2>Trade Radar (MVP)</h2>
    <div id="mode">Loading...</div>
    <button onclick="loadData()">Yenile</button>
    <div id="list"></div>
    <script>
      async function loadData(){
        const r = await fetch('/api/top'); const d = await r.json();
        document.getElementById('mode').innerText = "Market Mode: " + d.market_mode + " (ts=" + d.ts + ")";
        const list = document.getElementById('list'); list.innerHTML = "";
        d.top_picks.forEach(x=>{
          const el = document.createElement('div'); el.className="card";
          el.innerHTML = `<b>${x.symbol}</b> — Score: <b>${x.score}</b><br>
          Fiyat: ${x.price} | 24h: ${x.chg24_pct}% | Spread: ${x.spread_pct}%<br>
          Plan: Entry ${x.plan.entry} / Stop ${x.plan.stop} / TP1 ${x.plan.tp1} / TP2 ${x.plan.tp2}`;
          list.appendChild(el);
        })
      }
      loadData();
    </script></body></html>
    """
