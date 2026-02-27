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
    def get_market_mode_from_btc(assets):
    btc = next((c for c in assets if c.get("symbol") == "BTC"), None)
    if not btc:
        return "NEUTRAL"

    try:
        btc_p24 = float(btc.get("changePercent24Hr", 0))
    except:
        btc_p24 = 0.0

    if btc_p24 > 1.0:
        return "BULLISH"
    elif btc_p24 < -1.0:
        return "BEARISH"
    else:
        return "NEUTRAL"


def get_top_picks():
    # CryptoCompare top by volume (USD)
    url = "https://min-api.cryptocompare.com/data/top/totalvolfull"
    params = {"limit": 50, "tsym": "USD"}
    headers = {"User-Agent": "trade-radar-mvp"}

    try:
        r = requests.get(url, params=params, timeout=25, headers=headers)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("Data", [])
        if not data:
            return {"ts": int(time.time()), "market_mode": "UNKNOWN", "top_picks": [], "error": "No data from CryptoCompare"}
    except Exception as e:
        return {"ts": int(time.time()), "market_mode": "UNKNOWN", "top_picks": [], "error": repr(e)}

    rows = []
    for item in data:
        coin_info = item.get("CoinInfo", {}) or {}
        raw = (item.get("RAW", {}) or {}).get("USD", {}) or {}

        symbol = coin_info.get("Name", "")
        price = safe_float(raw.get("PRICE"))
        p24 = safe_float(raw.get("CHANGEPCT24HOUR"))
        vol24 = safe_float(raw.get("TOTALVOLUME24H"), 0.0) * price  # approx USD volume

        if price <= 0:
            continue
        if vol24 < 1_000_000:
            continue

        score = score_coin(p24=p24, vol24_usdt=vol24, spread=0.002)

        rows.append({
            "symbol": symbol,
            "price": round(price, 6),
            "chg24_pct": round(p24, 2),
            "vol24_usdt": int(vol24),
            "spread_pct": 0.2,
            "score": score,
            "plan": build_trade_plan(price)
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    # Market mode (rough): BTC 24h change from same payload if exists
    mode = "NEUTRAL"
    btc = next((x for x in rows if x["symbol"] == "BTC"), None)
    if btc:
        if btc["chg24_pct"] > 1.0:
            mode = "RISK-ON"
        elif btc["chg24_pct"] < -1.0:
            mode = "RISK-OFF"

    return {"ts": int(time.time()), "market_mode": mode, "top_picks": rows[:10]}
@app.get("/api/top", response_class=JSONResponse)
def api_top():
    return get_top_picks()

@app.get("/", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <style>
      body{font-family:Arial;margin:20px;max-width:900px}
      .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
      .muted{color:#666}
      button{padding:8px 12px}
    </style>
    </head><body>
    <h2>Trade Radar (MVP)</h2>
    <div class="muted" id="mode">Loading...</div>
    <button onclick="loadData()">Yenile</button>
    <div id="err" style="color:#b00020;margin-top:10px"></div>
    <div id="list"></div>

    <script>
      async function loadData(){
        document.getElementById('err').innerText = "";
        document.getElementById('mode').innerText = "Loading...";
        const list = document.getElementById('list');
        list.innerHTML = "";

        try{
          // CoinGecko - browser’dan direkt
          const url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=volume_desc&per_page=50&page=1&sparkline=false";
          const r = await fetch(url, {headers: {"accept":"application/json"}});
          if(!r.ok){
            const t = await r.text();
            throw new Error("CoinGecko HTTP " + r.status + " " + t.slice(0,120));
          }
          const data = await r.json();
          // BTC market trend hesaplama
const btc = data.find(x => (x.symbol || "").toLowerCase() === "btc");
let mode = "NEUTRAL";
let btcChg = 0;

if (btc) {
  btcChg = Number(btc.price_change_percentage_24h || 0);
  if (btcChg > 1) mode = "BULLISH";
  else if (btcChg < -1) mode = "BEARISH";
}

document.getElementById('mode').innerText =
  `Market Mode: ${mode} | BTC 24h: ${btcChg.toFixed(2)}%`;
document.getElementById('mode').innerText =
  `Market Mode: ${mode} | BTC 24h: ${btcChg.toFixed(2)}%`;
          document.getElementById('mode').innerText = "Market Mode: LIVE (browser fetch)";

          data.slice(0,10).forEach(x=>{
            const el = document.createElement('div'); el.className="card";
            el.innerHTML = `<b>${(x.symbol||"").toUpperCase()}</b> (${x.name||""})<br>
              Price: ${x.current_price} USD | 24h: ${(x.price_change_percentage_24h||0).toFixed(2)}%<br>
              Vol24: ${Math.round(x.total_volume||0).toLocaleString()} USD`;
            list.appendChild(el);
          });

        }catch(e){
          document.getElementById('mode').innerText = "Market Mode: UNKNOWN";
          document.getElementById('err').innerText = "Error: " + e.message;
        }
      }
      loadData();
    </script>
    </body></html>
    """
