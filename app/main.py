from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import os, time, math, statistics, requests, xml.etree.ElementTree as ET

app = FastAPI(title="Trade Radar (MVP+) — Yiğit Mode")

# =========================
# Config / Env
# =========================
CACHE_TTL = int(os.getenv("CACHE_TTL", "45"))          # seconds
SILVER_CACHE_TTL = int(os.getenv("SILVER_CACHE_TTL", "30"))
CRYPTO_CACHE_TTL = int(os.getenv("CRYPTO_CACHE_TTL", "45"))

TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY", "").strip()
NEWS_RSS = [
    # Hafif + genelde erişilebilir RSS'ler (TOS'a takılmadan)
    "https://www.kitco.com/rss/news",                  # Kitco (genel)
    "https://www.investing.com/rss/news_301.rss",      # Investing - commodities (bazı bölgelerde blok olabilir)
    "https://finance.yahoo.com/news/rssindex",         # Yahoo Finance genel
]

# =========================
# Helpers
# =========================
_session = requests.Session()
_session.headers.update({"User-Agent": "trade-radar-yigit/1.0 (+render)"})

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def now_ts():
    return int(time.time())

def get_json(url, params=None, timeout=20):
    r = _session.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def pct(a, b):
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0

def gram_silver_try(xag_usd, usd_try):
    # 1 troy ounce = 31.1034768 grams
    return (xag_usd / 31.1034768) * usd_try

def ema(values, period=20):
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains.append(d)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-d)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    r = 100 - (100 / (1 + rs))
    # smooth
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            r = 100.0
        else:
            rs = avg_gain / avg_loss
            r = 100 - (100 / (1 + rs))
    return float(r)

def simple_sentiment(text):
    # ultra-basit lexicon (EN/TR karışık). İstersen büyütürüz.
    pos = ["surge","rally","bull","beat","gain","up","record","strong","rise","positive",
           "yüksel","artış","güçlü","rekor","pozitif","ralli"]
    neg = ["crash","dump","bear","fall","down","weak","risk","fear","recession","negative",
           "düş","çök","zayıf","risk","korku","negatif","resesyon"]
    t = (text or "").lower()
    score = 0
    for w in pos:
        if w in t: score += 1
    for w in neg:
        if w in t: score -= 1
    return clamp(score, -5, 5)

def fetch_rss_titles(url, limit=10, timeout=12):
    try:
        r = _session.get(url, timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        out = []
        for it in items[:limit]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if title:
                out.append({"title": title, "link": link})
        return out
    except Exception:
        return []

# =========================
# Crypto (stable source: CryptoCompare)
# =========================
_crypto_cache = {"ts": 0, "data": None}

def score_coin(p24, vol24_usd, spread_proxy=0.002):
    # Momentum (clamped)
    p24c = clamp(p24, -20.0, 40.0)
    m = (p24c + 20.0) / 60.0 * 100.0
    if p24 > 120:
        m -= 40
    elif p24 > 60:
        m -= 20
    m = clamp(m, 0, 100)

    # Volume: log-scale
    v = clamp((math.log10(max(vol24_usd, 1.0)) - 6.0) / (8.0 - 6.0) * 100.0, 0, 100)

    # Spread proxy (lower better)
    s = clamp((0.008 - spread_proxy) / (0.008 - 0.0005) * 100.0, 0, 100)

    base = 0.45 * m + 0.40 * v + 0.15 * s
    return round(clamp(base, 0, 100), 1)

def build_trade_plan(last_price):
    entry = last_price
    stop = last_price * 0.97
    tp1  = last_price * 1.04
    tp2  = last_price * 1.07
    return {"entry": round(entry, 8), "stop": round(stop, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8)}

def get_crypto_top():
    # cache
    if _crypto_cache["data"] and (now_ts() - _crypto_cache["ts"] < CRYPTO_CACHE_TTL):
        return _crypto_cache["data"]

    url = "https://min-api.cryptocompare.com/data/top/totalvolfull"
    params = {"limit": 80, "tsym": "USD"}
    try:
        payload = get_json(url, params=params, timeout=25)
        data = payload.get("Data", []) or []
    except Exception as e:
        out = {"ts": now_ts(), "market_mode": "UNKNOWN", "top_picks": [], "warning": f"CryptoCompare fetch failed: {repr(e)}"}
        _crypto_cache.update({"ts": now_ts(), "data": out})
        return out

    rows = []
    for item in data:
        coin = item.get("CoinInfo", {}) or {}
        raw = (item.get("RAW", {}) or {}).get("USD", {}) or {}
        symbol = coin.get("Name", "")
        price = safe_float(raw.get("PRICE"))
        p24 = safe_float(raw.get("CHANGEPCT24HOUR"))
        vol24 = safe_float(raw.get("TOTALVOLUME24H"), 0.0) * price

        if not symbol or price <= 0:
            continue
        if vol24 < 25_000_000:
            continue

        score = score_coin(p24, vol24, spread_proxy=0.002)
        rows.append({
            "symbol": symbol,
            "price": round(price, 6),
            "chg24_pct": round(p24, 2),
            "vol24_usd": int(vol24),
            "score": score,
            "plan": build_trade_plan(price),
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    btc = next((x for x in rows if x["symbol"] == "BTC"), None)
    mode = "NEUTRAL"
    btc24 = btc["chg24_pct"] if btc else 0.0
    if btc24 > 1.0:
        mode = "BULLISH"
    elif btc24 < -1.0:
        mode = "BEARISH"

    out = {"ts": now_ts(), "market_mode": mode, "btc_24h": btc24, "top_picks": rows[:10]}
    _crypto_cache.update({"ts": now_ts(), "data": out})
    return out

# =========================
# Silver Radar
# =========================
_silver_cache = {"ts": 0, "data": None}

def twelvedata_quote(symbol):
    # Example: https://api.twelvedata.com/quote?symbol=XAG/USD&apikey=KEY
    url = "https://api.twelvedata.com/quote"
    params = {"symbol": symbol, "apikey": TWELVEDATA_KEY}
    j = get_json(url, params=params, timeout=20)
    # TwelveData returns "close"/"price" depending on endpoint; we handle both
    px = safe_float(j.get("price") or j.get("close") or j.get("last") or 0)
    if px <= 0:
        raise ValueError(f"Bad quote for {symbol}: {j}")
    return px

def twelvedata_timeseries(symbol, interval="1min", outputsize=120):
    # Example: https://api.twelvedata.com/time_series?symbol=AAPL&interval=1min&outputsize=12&apikey=KEY :contentReference[oaicite:2]{index=2}
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    j = get_json(url, params=params, timeout=25)
    vals = j.get("values") or []
    # values are reverse-chronological; convert to chronological closes
    closes = []
    for row in reversed(vals):
        c = safe_float(row.get("close"))
        if c > 0:
            closes.append(c)
    return closes

def silver_anomaly(prices):
    # "whale-like": return spike / zscore of returns
    if not prices or len(prices) < 20:
        return {"has": False, "z": 0.0, "msg": "Yetersiz seri"}
    rets = []
    for i in range(1, len(prices)):
        rets.append((prices[i] / prices[i-1]) - 1.0)
    mu = statistics.mean(rets[-60:]) if len(rets) >= 60 else statistics.mean(rets)
    sd = statistics.pstdev(rets[-60:]) if len(rets) >= 60 else statistics.pstdev(rets)
    last = rets[-1]
    z = 0.0 if sd == 0 else (last - mu) / sd
    has = abs(z) >= 3.0
    direction = "UP" if last > 0 else "DOWN"
    msg = f"Anomali: {direction} | z={z:.2f} | 1m ret={last*100:.2f}%"
    return {"has": bool(has), "z": float(z), "msg": msg, "ret_1m_pct": round(last*100, 3)}

def market_mode_from_rsi_ema(rsi14, ema20, ema50, last):
    # Basit rejim:
    # - EMA20>EMA50 ve RSI>55 => BULLISH
    # - EMA20<EMA50 ve RSI<45 => BEARISH
    # else NEUTRAL
    if rsi14 is None or ema20 is None or ema50 is None:
        return "NEUTRAL"
    if ema20 > ema50 and rsi14 >= 55 and last >= ema20:
        return "BULLISH"
    if ema20 < ema50 and rsi14 <= 45 and last <= ema20:
        return "BEARISH"
    return "NEUTRAL"

def build_silver_plan(gram):
    # “kripto gibi” hızlı plan: tighter stops, küçük hedefler
    entry = gram
    stop = gram * 0.992   # -0.8%
    tp1  = gram * 1.006   # +0.6%
    tp2  = gram * 1.012   # +1.2%
    return {
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "stop_pct": 0.8,
        "tp1_pct": 0.6,
        "tp2_pct": 1.2,
    }

def forecast_band(last, vol_est=0.0007, drift=0.0, minutes=15):
    # Basit “dakika bandı”: geometric random walk değil, sadece band üretir.
    base = []
    opti = []
    pess = []
    p = last
    for i in range(minutes):
        base.append(round(p, 4))
        opti.append(round(p * (1 + abs(drift) + vol_est), 4))
        pess.append(round(p * (1 - abs(drift) - vol_est), 4))
        # drift’li çizgi
        p = p * (1 + drift)
    return {"horizon_min": minutes, "sigma_est": vol_est, "drift_est": drift, "base": base, "optimistic": opti, "pessimistic": pess}

def get_silver_data(force=False, fiba_bid=None, fiba_ask=None):
    if (not force) and _silver_cache["data"] and (now_ts() - _silver_cache["ts"] < SILVER_CACHE_TTL):
        return _silver_cache["data"]

    out = {
        "ts": now_ts(),
        "market_mode": "UNKNOWN",
        "score": 50,
        "xag_usd": None,
        "usd_try": None,
        "gram_theoretical": None,
        "indicators": {"ema20": None, "ema50": None, "rsi14": None, "vol_1m_est": None},
        "forecast_15m": None,
        "anomaly": {"has": False, "msg": "No data"},
        "news": {"sentiment": 0, "titles": []},
        "bank_quote": {"bank": "FIBABANKA", "bid": fiba_bid, "ask": fiba_ask, "spread_pct": None, "premium_vs_theoretical": None},
        "warnings": [],
    }

    # ---- Prices
    try:
        if TWELVEDATA_KEY:
            xag = twelvedata_quote("XAG/USD")
            fx  = twelvedata_quote("USD/TRY")
        else:
            # No key => degrade: try free sources? (kept minimal: require key for reliable minute-level)
            raise RuntimeError("TWELVEDATA_KEY yok (Render Env).")
        out["xag_usd"] = round(xag, 4)
        out["usd_try"] = round(fx, 4)
        gram = gram_silver_try(xag, fx)
        out["gram_theoretical"] = round(gram, 4)
    except Exception as e:
        out["warnings"].append(f"Price fetch failed: {repr(e)}")
        out["market_mode"] = "UNKNOWN"
        _silver_cache.update({"ts": now_ts(), "data": out})
        return out

    # ---- Timeseries (optional, for “minute engine”)
    prices_gram_series = []
    try:
        if TWELVEDATA_KEY:
            xag_series = twelvedata_timeseries("XAG/USD", interval="1min", outputsize=160)
            fx_series  = twelvedata_timeseries("USD/TRY", interval="1min", outputsize=160)
            n = min(len(xag_series), len(fx_series))
            if n >= 30:
                for i in range(n):
                    prices_gram_series.append(gram_silver_try(xag_series[i], fx_series[i]))
    except Exception as e:
        out["warnings"].append(f"Timeseries fetch failed: {repr(e)}")

    if prices_gram_series and len(prices_gram_series) >= 30:
        last = prices_gram_series[-1]
        ema20v = ema(prices_gram_series[-120:], period=20)
        ema50v = ema(prices_gram_series[-180:], period=50)
        rsi14v = rsi(prices_gram_series[-200:], period=14)

        # vol estimate (1m returns stdev)
        rets = []
        for i in range(1, len(prices_gram_series)):
            rets.append((prices_gram_series[i] / prices_gram_series[i-1]) - 1.0)
        vol = statistics.pstdev(rets[-120:]) if len(rets) >= 120 else statistics.pstdev(rets)

        out["indicators"]["ema20"] = round(ema20v, 4) if ema20v else None
        out["indicators"]["ema50"] = round(ema50v, 4) if ema50v else None
        out["indicators"]["rsi14"] = round(rsi14v, 2) if rsi14v is not None else None
        out["indicators"]["vol_1m_est"] = round(vol, 6)

        out["market_mode"] = market_mode_from_rsi_ema(rsi14v, ema20v, ema50v, last)

        # score: blend RSI + trend + vol regime
        score = 50
        if rsi14v is not None:
            score += clamp((rsi14v - 50) * 0.8, -20, 20)
        if ema20v and ema50v:
            score += 8 if ema20v > ema50v else -8
        # high vol => lower confidence
        if vol > 0.0015:
            score -= 10
        out["score"] = int(clamp(score, 0, 100))

        out["anomaly"] = silver_anomaly(prices_gram_series[-180:])
        # drift: last 10m average return
        drift = 0.0
        if len(rets) >= 12:
            drift = statistics.mean(rets[-10:])
        out["forecast_15m"] = forecast_band(last=round(last, 4), vol_est=max(vol, 0.0004), drift=drift, minutes=15)
    else:
        # fallback: no minute series
        out["market_mode"] = "NEUTRAL"
        out["forecast_15m"] = forecast_band(last=out["gram_theoretical"], vol_est=0.0007, drift=0.0, minutes=15)
        out["anomaly"] = {"has": False, "msg": "Seri yok (minute) — sadece spot üzerinden."}

    # ---- Bank calc (optional input)
    try:
        if fiba_bid is not None and fiba_ask is not None and out["gram_theoretical"]:
            bid = safe_float(fiba_bid, 0)
            ask = safe_float(fiba_ask, 0)
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / bid * 100.0
                premium = (ask - out["gram_theoretical"]) / out["gram_theoretical"] * 100.0
                out["bank_quote"]["spread_pct"] = round(spread_pct, 2)
                out["bank_quote"]["premium_vs_theoretical"] = round(premium, 2)
    except Exception:
        pass

    # ---- News (RSS)
    titles = []
    sent = 0
    for rss in NEWS_RSS:
        batch = fetch_rss_titles(rss, limit=6)
        for it in batch:
            if it["title"] and len(titles) < 12:
                titles.append(it)
                sent += simple_sentiment(it["title"])
    out["news"]["titles"] = titles
    out["news"]["sentiment"] = int(clamp(sent, -20, 20))

    _silver_cache.update({"ts": now_ts(), "data": out})
    return out

# =========================
# API Routes
# =========================
@app.get("/api/crypto/top", response_class=JSONResponse)
def api_crypto_top():
    return get_crypto_top()

@app.get("/api/silver", response_class=JSONResponse)
def api_silver(force: int = 0, fiba_bid: float = None, fiba_ask: float = None):
    return get_silver_data(force=bool(force), fiba_bid=fiba_bid, fiba_ask=fiba_ask)

# =========================
# UI Pages
# =========================
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Radar — Yiğit Mode</title>
  <style>
    body{font-family:Arial;margin:20px;max-width:980px}
    .card{border:1px solid #ddd;border-radius:16px;padding:14px;margin:12px 0}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    .btn{padding:10px 14px;border:1px solid #222;border-radius:12px;background:#fff;cursor:pointer}
    .muted{color:#666}
    h2{margin-bottom:6px}
  </style>
</head>
<body>
  <h2>Radar (MVP+) — Yiğit Mode</h2>
  <div class="muted">Açılışta seç: Kripto mu, Gümüş mü?</div>

  <div class="row" style="margin-top:14px">
    <button class="btn" onclick="location.href='/crypto'">🚀 Kripto Radar</button>
    <button class="btn" onclick="location.href='/silver'">🥈 Silver Radar</button>
  </div>

  <div class="card">
    <b>Not</b>
    <div class="muted">Bu bir yatırım tavsiyesi değildir. Sistem sadece skor + risk şablonu üretir.</div>
    <div class="muted">Dakikalık analiz için Render ENV'e <code>TWELVEDATA_KEY</code> girmen gerekir.</div>
  </div>
</body>
</html>
"""

@app.get("/crypto", response_class=HTMLResponse)
def crypto_page():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Trade Radar — Yiğit Mode</title>
  <style>
    body{font-family:Arial;margin:20px;max-width:980px}
    .pill{display:inline-block;border:1px solid #ddd;border-radius:999px;padding:6px 10px;margin:4px 6px 4px 0}
    .card{border:1px solid #ddd;border-radius:16px;padding:14px;margin:12px 0}
    .muted{color:#666}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    button{padding:8px 12px;border-radius:12px;border:1px solid #222;background:#fff;cursor:pointer}
    code{background:#f6f6f6;padding:2px 6px;border-radius:6px}
  </style>
</head>
<body>
  <div class="row">
    <h2 style="margin:0">Trade Radar (MVP+) — Yiğit Mode</h2>
    <div style="flex:1"></div>
    <button onclick="location.href='/'">Mode seç</button>
    <button onclick="location.href='/silver'">🥈 Silver</button>
    <button onclick="loadData()">Yenile</button>
  </div>

  <div id="stats" class="row" style="margin-top:10px"></div>
  <div id="err" class="muted" style="color:#b00020;margin-top:8px"></div>

  <h3>🔥 Top 10 “Tradeable” Picks</h3>
  <div class="muted">Not: Bu bir yatırım tavsiyesi değil. Sistem skor + risk şablonu üretir.</div>
  <div id="list"></div>

<script>
async function loadData(){
  document.getElementById('err').innerText = "";
  document.getElementById('stats').innerHTML = "";
  document.getElementById('list').innerHTML = "";
  try{
    const r = await fetch('/api/crypto/top');
    const j = await r.json();

    const stats = document.getElementById('stats');
    stats.innerHTML =
      `<span class="pill">Market Mode: <b>${j.market_mode||'UNKNOWN'}</b></span>` +
      `<span class="pill">BTC 24h: <b>${(j.btc_24h||0).toFixed(2)}%</b></span>` +
      `<span class="pill">Source: <b>CryptoCompare</b></span>`;

    if(j.warning){
      document.getElementById('err').innerText = "Warning: " + j.warning;
    }

    (j.top_picks||[]).forEach(x=>{
      const el = document.createElement('div'); el.className="card";
      el.innerHTML = `
        <div class="row">
          <div style="font-size:18px"><b>${x.symbol}</b></div>
          <div style="flex:1"></div>
          <div class="pill">Score: <b>${x.score}</b></div>
        </div>
        <div class="muted">Price: ${x.price} | 24h: ${x.chg24_pct}% | Vol24: ${Number(x.vol24_usd||0).toLocaleString()} USD</div>
        <div class="row" style="margin-top:8px">
          <span class="pill">Entry: <b>${x.plan.entry}</b></span>
          <span class="pill">Stop: <b>${x.plan.stop}</b></span>
          <span class="pill">TP1: <b>${x.plan.tp1}</b></span>
          <span class="pill">TP2: <b>${x.plan.tp2}</b></span>
        </div>
      `;
      document.getElementById('list').appendChild(el);
    });

    if((j.top_picks||[]).length === 0){
      document.getElementById('list').innerHTML = "<div class='muted'>Top 10 boş geldi.</div>";
    }
  }catch(e){
    document.getElementById('err').innerText = "Error: " + e.message;
  }
}
loadData();
</script>
</body>
</html>
"""

@app.get("/silver", response_class=HTMLResponse)
def silver_page():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Silver Radar — Yiğit Mode</title>
  <style>
    body{font-family:Arial;margin:20px;max-width:1100px}
    .pill{display:inline-block;border:1px solid #ddd;border-radius:999px;padding:6px 10px;margin:4px 6px 4px 0}
    .card{border:1px solid #ddd;border-radius:16px;padding:14px;margin:12px 0}
    .muted{color:#666}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    button{padding:8px 12px;border-radius:12px;border:1px solid #222;background:#fff;cursor:pointer}
    input{padding:8px 10px;border-radius:10px;border:1px solid #bbb;width:130px}
    .grid{display:grid;grid-template-columns: 1fr 1fr; gap:12px}
    @media (max-width: 900px){ .grid{grid-template-columns:1fr;} }
    pre{background:#fafafa;border:1px solid #eee;border-radius:14px;padding:12px;overflow:auto}
  </style>
</head>
<body>
  <div class="row">
    <h2 style="margin:0">🥈 Silver Radar — Yiğit Mode</h2>
    <div style="flex:1"></div>
    <button onclick="location.href='/'">Mode seç</button>
    <button onclick="location.href='/crypto'">🚀 Kripto</button>
    <button onclick="loadData(0)">Yenile</button>
    <button onclick="loadData(1)">Yenile (force)</button>
  </div>

  <div id="stats" class="row" style="margin-top:10px"></div>
  <div id="warn" class="muted" style="color:#b00020;margin-top:8px"></div>

  <div class="card">
    <b>Fiba (opsiyonel)</b> <span class="muted">— yazarsan premium hesaplar, yazmazsan sistem yine çalışır</span>
    <div class="row" style="margin-top:10px">
      <div>Alış (bid): <input id="bid" placeholder="129.56"/></div>
      <div>Satış (ask): <input id="ask" placeholder="135.91"/></div>
      <button onclick="loadData(1)">Kaydet</button>
      <span class="pill" id="premium">Premium: -</span>
      <span class="pill" id="spread">Makas: -</span>
    </div>
  </div>

  <div class="grid">
    <div class="card" id="plan"></div>
    <div class="card" id="forecast"></div>
  </div>

  <div class="grid">
    <div class="card" id="anomaly"></div>
    <div class="card" id="news"></div>
  </div>

  <div class="card">
    <b>Grafik (TradingView)</b> <span class="muted">— embed yaklaşımı (sayfa içi)</span>
    <div class="grid" style="margin-top:10px">
      <div>
        <div class="muted">XAGUSD</div>
        <div class="tradingview-widget-container">
          <div id="tv_xag"></div>
        </div>
      </div>
      <div>
        <div class="muted">USDTRY</div>
        <div class="tradingview-widget-container">
          <div id="tv_fx"></div>
        </div>
      </div>
    </div>
    <div class="muted" style="margin-top:8px">Not: Widget entegrasyonu TradingView dokümantasyonuna göre yapılır.</div>
  </div>

  <div class="card">
    <b>Debug</b>
    <pre id="dbg">{}</pre>
  </div>

<script>
function embedTV(containerId, symbol){
  // TradingView widget integration guide: :contentReference[oaicite:3]{index=3}
  // Mini Symbol Overview
  const script = document.createElement('script');
  script.src = "https://s3.tradingview.com/external-embedding/embed-widget-mini-symbol-overview.js";
  script.async = true;
  script.innerHTML = JSON.stringify({
    "symbol": symbol,
    "width": "100%",
    "height": 220,
    "locale": "en",
    "dateRange": "1D",
    "colorTheme": "light",
    "isTransparent": false,
    "autosize": true,
    "largeChartUrl": ""
  });
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  el.appendChild(script);
}

async function loadData(force){
  document.getElementById('warn').innerText = "";
  document.getElementById('stats').innerHTML = "";
  document.getElementById('plan').innerHTML = "";
  document.getElementById('forecast').innerHTML = "";
  document.getElementById('anomaly').innerHTML = "";
  document.getElementById('news').innerHTML = "";

  const bid = document.getElementById('bid').value.trim();
  const ask = document.getElementById('ask').value.trim();
  let url = `/api/silver?force=${force?1:0}`;
  if(bid && ask){
    url += `&fiba_bid=${encodeURIComponent(bid)}&fiba_ask=${encodeURIComponent(ask)}`;
  }

  try{
    const r = await fetch(url);
    const j = await r.json();

    const stats = document.getElementById('stats');
    stats.innerHTML =
      `<span class="pill">Market Mode: <b>${j.market_mode||'UNKNOWN'}</b></span>` +
      `<span class="pill">Score: <b>${j.score}</b></span>` +
      `<span class="pill">XAGUSD: <b>${(j.xag_usd||0).toFixed(2)}</b></span>` +
      `<span class="pill">USDTRY: <b>${(j.usd_try||0).toFixed(4)}</b></span>` +
      `<span class="pill">Teorik Gram: <b>${(j.gram_theoretical||0).toFixed(4)}</b></span>`;

    if((j.warnings||[]).length){
      document.getElementById('warn').innerText = "Warning: " + j.warnings.join(" | ");
    }

    // bank calc
    if(j.bank_quote && j.bank_quote.spread_pct != null){
      document.getElementById('spread').innerText = `Makas: ${j.bank_quote.spread_pct.toFixed(2)}%`;
    }
    if(j.bank_quote && j.bank_quote.premium_vs_theoretical != null){
      document.getElementById('premium').innerText = `Premium: ${j.bank_quote.premium_vs_theoretical.toFixed(2)}%`;
    }

    // plan
    const plan = (function(){
      const p = j.gram_theoretical || 0;
      // server also includes plan under forecast/score logic; but keep local display
      return {
        entry: p,
        stop: p*0.992,
        tp1: p*1.006,
        tp2: p*1.012
      }
    })();

    document.getElementById('plan').innerHTML = `
      <b>Trade Plan (Teorik Gram)</b><div class="muted">Bu bir yatırım tavsiyesi değildir. “Setup” mantığıyla risk şablonu verir.</div>
      <div style="margin-top:10px;font-size:16px">
        <b>Entry:</b> ${plan.entry.toFixed(4)} |
        <b>Stop:</b> ${plan.stop.toFixed(4)} |
        <b>TP1:</b> ${plan.tp1.toFixed(4)} |
        <b>TP2:</b> ${plan.tp2.toFixed(4)}
      </div>
      <div class="row" style="margin-top:10px">
        <span class="pill">EMA20: <b>${j.indicators?.ema20 ?? '-'}</b></span>
        <span class="pill">EMA50: <b>${j.indicators?.ema50 ?? '-'}</b></span>
        <span class="pill">RSI14: <b>${j.indicators?.rsi14 ?? '-'}</b></span>
        <span class="pill">Vol(1m est): <b>${j.indicators?.vol_1m_est ?? '-'}</b></span>
      </div>
    `;

    // forecast
    const f = j.forecast_15m || {};
    document.getElementById('forecast').innerHTML = `
      <b>Dakika Senaryosu (15dk band)</b>
      <div class="muted">Kesin tahmin değil; “baz / iyimser / kötümser” band.</div>
      <div class="muted">Sigma est: ${(f.sigma_est||0).toFixed(6)} | Drift est: ${(f.drift_est||0).toFixed(6)} (dakika)</div>
      <pre>${JSON.stringify(f, null, 2)}</pre>
    `;

    // anomaly
    const a = j.anomaly || {};
    document.getElementById('anomaly').innerHTML = `
      <b>Anomali Alerts (“whale-like”)</b>
      <div class="muted">Şok hareket / ivme / USDTRY oynaklığı yakalanır.</div>
      <div style="margin-top:8px">${a.has ? "⚠️ " : "✅ "}${a.msg || "-"}</div>
    `;

    // news
    const titles = (j.news?.titles||[]).map(x=>`• <a href="${x.link}" target="_blank" rel="noreferrer">${x.title}</a>`).join("<br>");
    document.getElementById('news').innerHTML = `
      <b>Haber / Sentiment</b>
      <div class="muted">RSS başlıklarından basit duygu skoru (cache’li).</div>
      <div class="row" style="margin-top:8px">
        <span class="pill">Sentiment: <b>${j.news?.sentiment ?? 0}</b></span>
      </div>
      <div style="margin-top:10px">${titles || "<span class='muted'>Haber yok / RSS erişilemedi.</span>"}</div>
    `;

    document.getElementById('dbg').innerText = JSON.stringify(j, null, 2);

  }catch(e){
    document.getElementById('warn').innerText = "Error: " + e.message;
  }
}

// Embed charts
embedTV("tv_xag", "OANDA:XAGUSD");
embedTV("tv_fx", "FX_IDC:USDTRY");

// initial load
loadData(0);
</script>
</body>
</html>
"""

# =========================
# Notes (for you)
# =========================
# Render ENV önerisi:
# - TWELVEDATA_KEY = (TwelveData API key)
# - CACHE_TTL / SILVER_CACHE_TTL opsiyonel
#
# Bu sürüm "doviz.com private API" kovalamaz. Daha sağlam yol:
# spot (XAGUSD + USDTRY) -> teorik gram -> banka premium/makas
#
# Eğer ileride "doviz.com bankalar tablosu" şart olursa:
# - Tarayıcı Network'te çağrılan endpoint + token yapısını bulmak gerekir.
# - Çoğu zaman public değildir, TOS/engelleme riski var.
