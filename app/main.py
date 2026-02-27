from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, statistics, requests

app = FastAPI(title="Trade Radar (MVP+)")

# CONFIG
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", 60))
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT", 50))
TOP_PICKS_VAL = int(os.getenv("TOP_PICKS", 10))
MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", 50000000))
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", 0.00001))
MAX_ABS_24H = float(os.getenv("MAX_ABS_24H", 35))
MIN_ABS_24H = float(os.getenv("MIN_ABS_24H", 2))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", 35))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", 75))
WHALE_TTL_SEC = int(os.getenv("WHALE_TTL_SEC", 20))
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", 80))
WHALE_NOTIONAL_USD = float(os.getenv("WHALE_NOTIONAL_USD", 500000))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_ENABLED = bool(TG_TOKEN and TG_CHAT_ID and os.getenv("TELEGRAM_ENABLED", "0") == "1")

USER_AGENT = {"User-Agent": "trade-radar-mvp-plus"}
STABLE_SKIP = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDE", "USD1", "FDUSD", "EURT", "USDP", "PYUSD", "FRAX"}

_cache_top = {"ts": 0, "data": None}
_cache_whales = {"ts": 0, "data": None}
_cache_binance_symbols = {"ts": 0, "set": set()}

def now() -> int: return int(time.time())
def clamp(x, lo, hi): return max(lo, min(hi, x))
def safe_float(x, default=0.0):
    try: return float(x)
    except: return default

def http_get_json(url, params=None, timeout=20, headers=None):
    h = dict(USER_AGENT)
    if headers: h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json()

def ema(values, period):
    if not values or period <= 0: return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]: e = v * k + e * (1 - k)
    return e

def rsi(values, period=14):
    if len(values) < period + 1: return None
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0)) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    a = sum(trs[:period]) / period
    for tr in trs[period:]: a = (a * (period - 1) + tr) / period
    return a

def build_trade_plan(price, atr_val=None):
    if not price or price <= 0: return {"entry": None, "stop": None, "tp1": None, "tp2": None}
    if atr_val and atr_val > 0:
        stop, tp1, tp2 = price - 1.2 * atr_val, price + 1.0 * atr_val, price + 1.8 * atr_val
    else:
        stop, tp1, tp2 = price * 0.97, price * 1.04, price * 1.07
    return {"entry": round(price, 8), "stop": round(max(stop, 0), 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8)}

def score_coin(p24, vol24_usd, spread_pct, rsi14=None, ema20v=None, ema50v=None, atrv=None, price=None):
    momentum = clamp((clamp(p24, -20.0, 30.0) + 20.0) / 50.0 * 100.0, 0, 100)
    liq = clamp((math.log10(max(vol24_usd, 1.0)) - 7.0) / 3.0 * 100.0, 0, 100)
    spread = clamp((0.010 - spread_pct) / 0.009 * 100.0, 0, 100)
    trend = 75.0 if (ema20v and ema50v and ema20v > ema50v) else 35.0
    rsi_s = 50.0
    if rsi14:
        if rsi14 < RSI_OVERSOLD: rsi_s = 75.0
        elif rsi14 > RSI_OVERBOUGHT: rsi_s = 20.0
        else: rsi_s = clamp(75.0 - (rsi14 - RSI_OVERSOLD) * (40.0 / (RSI_OVERBOUGHT - RSI_OVERSOLD)), 35.0, 75.0)
    risk = 65.0
    if atrv and price:
        ap = (atrv / price) * 100.0
        risk = 20.0 if ap > 6 else 35.0 if ap > 4 else 65.0
    return round(0.3*momentum + 0.28*liq + 0.12*spread + 0.15*trend + 0.1*rsi_s + 0.05*risk, 1)

def fetch_universe_cryptocompare(limit=UNIVERSE_LIMIT):
    return http_get_json("https://min-api.cryptocompare.com/data/top/totalvolfull", {"limit": limit, "tsym": "USD"}).get("Data", [])

def fetch_histohour_cryptocompare(symbol, hours=200):
    res = http_get_json("https://min-api.cryptocompare.com/data/v2/histohour", {"fsym": symbol, "tsym": "USD", "limit": hours})
    return res.get("Data", {}).get("Data", [])

def refresh_binance_symbol_set():
    if _cache_binance_symbols["ts"] and now() - _cache_binance_symbols["ts"] < 21600: return _cache_binance_symbols["set"]
    try:
        js = http_get_json("https://api.binance.com/api/v3/exchangeInfo")
        syms = {s["baseAsset"] for s in js.get("symbols", []) if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
        _cache_binance_symbols.update({"ts": now(), "set": syms})
        return syms
    except: return _cache_binance_symbols["set"]

def fetch_binance_recent_trades_usdt(symbol, limit=WHALE_LOOKBACK_TRADES):
    return http_get_json("https://api.binance.com/api/v3/aggTrades", {"symbol": f"{symbol}USDT", "limit": limit})

def compute_market_mode_from_universe(rows):
    by_sym = {r["symbol"]: r for r in rows}
    btc_chg = safe_float(by_sym.get("BTC", {}).get("chg24_pct", 0))
    eth_chg = safe_float(by_sym.get("ETH", {}).get("chg24_pct", 0))
    m20 = statistics.median([safe_float(x["chg24_pct"]) for x in rows[:20]]) if rows else 0.0
    idx = 0.5 * btc_chg + 0.3 * eth_chg + 0.2 * m20
    mode = "STRONG BULLISH" if idx > 1.2 else "BULLISH" if idx > 0.4 else "PANIC" if idx < -1.2 else "BEARISH" if idx < -0.4 else "NEUTRAL"
    return mode, round(idx, 2), round(btc_chg, 2), round(eth_chg, 2), round(m20, 2)

@app.get("/api/top", response_class=JSONResponse)
def api_top():
    if _cache_top["data"] and (now() - _cache_top["ts"] < CACHE_TTL_SEC): return _cache_top["data"]
    try:
        uni = fetch_universe_cryptocompare()
        rows_raw = []
        for i in uni:
            raw = i.get("RAW", {}).get("USD", {})
            sym = i.get("CoinInfo", {}).get("Name")
            if not sym or sym in STABLE_SKIP: continue
            p, p24 = safe_float(raw.get("PRICE")), safe_float(raw.get("CHANGEPCT24HOUR"))
            v24 = safe_float(raw.get("TOTALVOLUME24H")) * p
            if p > MIN_PRICE_USD and v24 > MIN_VOL_USD and MIN_ABS_24H < abs(p24) < MAX_ABS_24H:
                rows_raw.append({"symbol": sym, "price": p, "chg24_pct": p24, "vol24_usd": v24})
        
        rows_raw.sort(key=lambda x: x["vol24_usd"], reverse=True)
        enriched = []
        for r in rows_raw[:30]:
            sym, p = r["symbol"], r["price"]
            rsi14, e20, e50, atrv = None, None, None, None
            try:
                c = fetch_histohour_cryptocompare(sym)
                cl = [safe_float(x.get("close")) for x in c if safe_float(x.get("close")) > 0]
                hi = [safe_float(x.get("high")) for x in c]
                lo = [safe_float(x.get("low")) for x in c]
                if len(cl) >= 60:
                    rsi14, e20, e50 = rsi(cl, 14), ema(cl[-80:], 20), ema(cl[-120:], 50)
                    if len(hi) == len(lo) == len(cl): atrv = atr(hi, lo, cl, 14)
            except: pass
            
            sc = score_coin(r["chg24_pct"], r["vol24_usd"], 0.002, rsi14, e20, e50, atrv, p)
            enriched.append({
                "symbol": sym, "price": round(p, 6), "chg24_pct": round(r["chg24_pct"], 2),
                "vol24_usd": int(r["vol24_usd"]), "score": sc, "rsi14": round(rsi14, 2) if rsi14 else None,
                "ema20": round(e20, 6) if e20 else None, "ema50": round(e50, 6) if e50 else None,
                "atr": round(atrv, 6) if atrv else None, "plan": build_trade_plan(p, atrv)
            })
        
        enriched.sort(key=lambda x: x["score"], reverse=True)
        mode, idx, btc, eth, med = compute_market_mode_from_universe(rows_raw)
        payload = {"ts": now(), "market_mode": mode, "market_index": idx, "btc_24h": btc, "eth_24h": eth, "median20_24h": med, "top_picks": enriched[:TOP_PICKS_VAL]}
        _cache_top.update({"ts": now(), "data": payload})
        return payload
    except Exception as e: return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
      <head>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <style>
          body{font-family:sans-serif;margin:18px;max-width:980px;background:#f4f7f6}
          .pill{border:1px solid #ddd;border-radius:999px;padding:8px 12px;display:inline-block;background:#fff;margin:4px}
          .card{border:1px solid #ddd;border-radius:14px;padding:12px;margin:10px 0;background:#fff;box-shadow:0 2px 4px rgba(0,0,0,0.05)}
          .grid{display:grid;grid-template-columns:1fr;gap:10px}
          @media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}
          .ok{color:#0b6} .danger{color:#b00} .warn{color:#c90}
        </style>
      </head>
      <body>
        <h2>Trade Radar (MVP+)</h2>
        <div id="header"></div>
        <div id="picks" class="grid"></div>
        <script>
          async function load(){
            const res = await fetch('/api/top');
            const data = await res.json();
            document.getElementById('header').innerHTML = `
              <div class="pill">Mode: ${data.market_mode}</div>
              <div class="pill">Index: ${data.market_index}</div>
              <div class="pill">BTC: ${data.btc_24h}%</div>
            `;
            document.getElementById('picks').innerHTML = data.top_picks.map(p => `
              <div class="card">
                <b>${p.symbol}</b> - Score: ${p.score}<br/>
                Price: ${p.price} | 24h: ${p.chg24_pct}%<br/>
                <small>Entry: ${p.plan.entry} | SL: ${p.plan.stop} | TP: ${p.plan.tp1}</small>
              </div>
            `).join('');
          }
          load();
          setInterval(load, 30000);
        </script>
      </body>
    </html>
    """
