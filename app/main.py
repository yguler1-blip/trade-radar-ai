# app/main.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, requests
from datetime import datetime, timezone

app = FastAPI(title="Trade Radar (MVP+) — Yiğit Mode")

# =========================
# Config
# =========================
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))  # seconds
VOL_MIN_USD = int(os.getenv("VOL_MIN_USD", "60000000"))  # default 60M
PCT_MIN = float(os.getenv("PCT_MIN", "2"))  # 24h abs% min
PCT_MAX = float(os.getenv("PCT_MAX", "25"))  # 24h abs% max
TOP_N = int(os.getenv("TOP_N", "10"))

WHALE_THRESHOLD_USD = float(os.getenv("WHALE_THRESHOLD_USD", "750000"))  # $750k
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "60"))  # aggTrades limit

# Prefer Binance mirror to avoid 451 in some regions
BINANCE_ENDPOINTS = [
    os.getenv("BINANCE_BASE", "").strip(),
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
]
BINANCE_ENDPOINTS = [x for x in BINANCE_ENDPOINTS if x]

USER_AGENT = {"User-Agent": "trade-radar-mvp-plus"}


# =========================
# Helpers
# =========================
_cache = {"ts": 0, "data": None}

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def now_ts():
    return int(time.time())

def fmt_usd(n):
    try:
        n = float(n)
    except:
        return str(n)
    if n >= 1e9:
        return f"{n/1e9:.2f}B"
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.2f}K"
    return f"{n:.2f}"

def http_get_json(url, params=None, timeout=20, headers=None):
    h = dict(USER_AGENT)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json()

def http_get_json_with_fallback(path, params=None, timeout=20):
    last_err = None
    for base in BINANCE_ENDPOINTS:
        try:
            url = base.rstrip("/") + path
            return http_get_json(url, params=params, timeout=timeout)
        except Exception as e:
            last_err = e
            continue
    raise last_err


# =========================
# Scoring / Plan
# =========================
def score_coin(p24, vol24_usd, spread_hint=0.002):
    """
    score 0-100: momentum + liquidity + low-spread hint.
    This is MVP logic, not financial advice.
    """
    # momentum: clamp -20..40
    p24c = clamp(p24, -20.0, 40.0)
    m = (p24c + 20.0) / 60.0 * 100.0
    # penalize insane spikes
    if p24 > 120:
        m -= 40
    elif p24 > 60:
        m -= 20
    m = clamp(m, 0, 100)

    # liquidity: log scale 1M..100M..10B
    v = clamp((math.log10(max(vol24_usd, 1.0)) - 6.0) / (10.0 - 6.0) * 100.0, 0, 100)

    # spread hint: prefer <0.8%
    s = clamp((0.008 - spread_hint) / (0.008 - 0.0005) * 100.0, 0, 100)

    base = 0.45 * m + 0.40 * v + 0.15 * s
    return round(clamp(base, 0, 100), 1)

def build_trade_plan(last_price):
    # Simple risk template
    entry = last_price
    stop = last_price * 0.97
    tp1  = last_price * 1.04
    tp2  = last_price * 1.07
    return {
        "entry": round(entry, 8),
        "stop": round(stop, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
    }


# =========================
# Binance fetchers
# =========================
def fetch_binance_24h_all():
    # GET /api/v3/ticker/24hr
    return http_get_json_with_fallback("/api/v3/ticker/24hr", timeout=25)

def fetch_binance_agg_trades(symbol, limit=WHALE_LOOKBACK_TRADES):
    # GET /api/v3/aggTrades?symbol=BTCUSDT&limit=...
    pair = f"{symbol}USDT"
    params = {"symbol": pair, "limit": limit}
    return http_get_json_with_fallback("/api/v3/aggTrades", params=params, timeout=20)


# =========================
# Core logic
# =========================
def compute_market_mode(btc_pct, eth_pct):
    # very rough regime:
    # index is average of BTC+ETH 24h
    idx = (btc_pct + eth_pct) / 2.0
    if idx > 1.0:
        return "BULLISH", round(idx, 2)
    if idx < -1.0:
        return "BEARISH", round(idx, 2)
    return "NEUTRAL", round(idx, 2)

def build_top_picks_from_binance(tickers):
    """
    tickers: list of dicts from /ticker/24hr
    Use: quoteVolume (USDT), lastPrice, priceChangePercent
    Filter: USDT pairs, exclude stablecoins + weird leveraged tokens
    """
    exclude = set([
        "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI",
        "EUR", "TRY", "BRL", "GBP", "AUD", "RUB", "UAH",
    ])
    # exclude common leveraged suffixes
    bad_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith(bad_suffixes):
            continue

        base = sym[:-4]  # remove USDT
        if base in exclude:
            continue

        last = safe_float(t.get("lastPrice"))
        p24 = safe_float(t.get("priceChangePercent"))
        qv = safe_float(t.get("quoteVolume"))  # in USDT
        if last <= 0:
            continue

        # filters (abs change between PCT_MIN..PCT_MAX, volume >= VOL_MIN_USD)
        if qv < VOL_MIN_USD:
            continue

        ap24 = abs(p24)
        if ap24 < PCT_MIN or ap24 > PCT_MAX:
            continue

        score = score_coin(p24=p24, vol24_usd=qv, spread_hint=0.002)

        rows.append({
            "symbol": base,
            "pair": sym,
            "price": round(last, 8),
            "chg24_pct": round(p24, 2),
            "vol24_usdt": int(qv),
            "score": score,
            "plan": build_trade_plan(last),
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:TOP_N]

def build_whale_alerts(picks, threshold_usd=WHALE_THRESHOLD_USD):
    """
    For each pick, pull recent aggTrades and detect any single trade above threshold.
    Approx value = price * quantity (q)
    """
    alerts = []
    for p in picks[:min(6, len(picks))]:  # limit to reduce rate / latency
        sym = p["symbol"]
        price = float(p["price"])
        try:
            trades = fetch_binance_agg_trades(sym, limit=WHALE_LOOKBACK_TRADES)
            for tr in trades:
                qty = safe_float(tr.get("q"))
                is_buyer_maker = tr.get("m", False)  # True means sell-side aggressor
                usd = qty * price
                if usd >= threshold_usd:
                    ts_ms = int(tr.get("T", 0))
                    dt = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).astimezone()
                    side = "SELL" if is_buyer_maker else "BUY"
                    alerts.append({
                        "symbol": sym,
                        "pair": f"{sym}USDT",
                        "side": side,
                        "usd": round(usd, 2),
                        "qty": round(qty, 6),
                        "price": round(price, 8),
                        "time": dt.isoformat(timespec="seconds"),
                    })
        except Exception as e:
            # swallow but keep note
            alerts.append({
                "symbol": sym,
                "pair": f"{sym}USDT",
                "error": repr(e)[:220],
            })
    # sort by usd desc if present
    def usd_key(a):
        return float(a.get("usd", 0.0))
    alerts.sort(key=usd_key, reverse=True)
    return alerts

def get_state():
    # cache
    if _cache["data"] and (now_ts() - _cache["ts"] <= CACHE_TTL):
        return _cache["data"]

    out = {
        "ts": now_ts(),
        "source": "binance",
        "market_mode": "UNKNOWN",
        "btc_24h": 0.0,
        "eth_24h": 0.0,
        "index": 0.0,
        "filters": {
            "vol_min_usd": VOL_MIN_USD,
            "pct_min": PCT_MIN,
            "pct_max": PCT_MAX,
            "whale_threshold_usd": WHALE_THRESHOLD_USD,
        },
        "top_picks": [],
        "whale_alerts": [],
        "warnings": [],
    }

    try:
        tickers = fetch_binance_24h_all()
    except Exception as e:
        out["warnings"].append(f"Binance fetch failed: {repr(e)}")
        _cache["ts"] = now_ts()
        _cache["data"] = out
        return out

    # BTC / ETH regime
    btc = next((x for x in tickers if x.get("symbol") == "BTCUSDT"), None)
    eth = next((x for x in tickers if x.get("symbol") == "ETHUSDT"), None)
    btc_pct = safe_float(btc.get("priceChangePercent")) if btc else 0.0
    eth_pct = safe_float(eth.get("priceChangePercent")) if eth else 0.0
    mode, idx = compute_market_mode(btc_pct, eth_pct)

    out["btc_24h"] = round(btc_pct, 2)
    out["eth_24h"] = round(eth_pct, 2)
    out["market_mode"] = mode
    out["index"] = idx

    # Picks
    picks = build_top_picks_from_binance(tickers)
    out["top_picks"] = picks

    # Whale alerts
    if picks:
        out["whale_alerts"] = build_whale_alerts(picks, threshold_usd=WHALE_THRESHOLD_USD)

    _cache["ts"] = now_ts()
    _cache["data"] = out
    return out


# =========================
# API
# =========================
@app.get("/api/top", response_class=JSONResponse)
def api_top():
    return get_state()

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Trade Radar (MVP+) — Yiğit Mode</title>
    <style>
      body{font-family:Arial;margin:18px;max-width:980px}
      .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
      .pill{border:1px solid #ddd;border-radius:999px;padding:6px 10px;font-size:12px;background:#fafafa}
      .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
      .muted{color:#666}
      button{padding:8px 12px;border-radius:10px;border:1px solid #ddd;background:#fff}
      .warn{color:#b00020}
      .good{color:#0a7}
      .bad{color:#c00}
      small{color:#666}
      pre{white-space:pre-wrap}
    </style>
  </head>
  <body>
    <h2>Trade Radar (MVP+) — Yiğit Mode</h2>

    <div class="row" id="pills">
      <span class="pill">Market Mode: <b id="mode">...</b></span>
      <span class="pill">BTC 24h: <b id="btc">...</b></span>
      <span class="pill">ETH 24h: <b id="eth">...</b></span>
      <span class="pill">Index: <b id="idx">...</b></span>
      <span class="pill" id="flt">...</span>
      <button onclick="loadData()">Yenile</button>
    </div>

    <div id="warn" class="warn"></div>

    <h3>🔥 Top 10 “Tradeable” Picks</h3>
    <div class="muted">
      Not: Bu bir yatırım tavsiyesi değil. Sistem skor + risk şablonu üretir.
    </div>
    <div id="list"></div>

    <h3>🐋 Whale Alerts (Binance, threshold: <span id="whaleTh">...</span>)</h3>
    <div class="muted">Not: Whale = tek işlem değeri (yaklaşık) threshold üstü.</div>
    <div id="whales"></div>

    <script>
      function clsForMode(mode){
        if(mode === "BULLISH") return "good";
        if(mode === "BEARISH") return "bad";
        return "";
      }

      function fmtUSD(n){
        const x = Number(n||0);
        if(x>=1e9) return (x/1e9).toFixed(2)+"B";
        if(x>=1e6) return (x/1e6).toFixed(2)+"M";
        if(x>=1e3) return (x/1e3).toFixed(2)+"K";
        return x.toFixed(0);
      }

      async function loadData(){
        document.getElementById('warn').innerText = "";
        document.getElementById('list').innerHTML = "";
        document.getElementById('whales').innerHTML = "";

        let j;
        try{
          const r = await fetch("/api/top?ts=" + Date.now());
          j = await r.json();
        }catch(e){
          document.getElementById('warn').innerText = "Fetch error: " + e.message;
          return;
        }

        const modeEl = document.getElementById('mode');
        modeEl.innerText = j.market_mode || "UNKNOWN";
        modeEl.className = clsForMode(j.market_mode || "UNKNOWN");

        document.getElementById('btc').innerText = (j.btc_24h ?? 0).toFixed(2) + "%";
        document.getElementById('eth').innerText = (j.eth_24h ?? 0).toFixed(2) + "%";
        document.getElementById('idx').innerText = (j.index ?? 0).toFixed(2);

        const f = j.filters || {};
        document.getElementById('flt').innerText =
          `VolMin=${fmtUSD(f.vol_min_usd)} | 24h%=${f.pct_min}-${f.pct_max} | Whale=${fmtUSD(f.whale_threshold_usd)}`;

        document.getElementById('whaleTh').innerText = "$" + fmtUSD(f.whale_threshold_usd);

        if(j.warnings && j.warnings.length){
          document.getElementById('warn').innerText = "Warning: " + j.warnings.join(" | ");
        }

        const list = document.getElementById('list');
        if(!j.top_picks || j.top_picks.length === 0){
          list.innerHTML = `<div class="muted">Top 10 boş geldi.<br>
            Filtreler çok sıkı olabilir (VolMin / 24h%).</div>`;
        }else{
          j.top_picks.forEach(x=>{
            const el = document.createElement('div'); el.className = "card";
            const plan = x.plan || {};
            el.innerHTML = `
              <div class="row" style="justify-content:space-between">
                <div>
                  <b style="font-size:18px">${x.symbol}</b> <small>(${x.pair})</small>
                </div>
                <div class="pill">Score: <b>${x.score}</b></div>
              </div>
              <div class="muted">
                Price: <b>${x.price}</b> USDT &nbsp;|&nbsp;
                24h: <b>${(x.chg24_pct||0).toFixed(2)}%</b> &nbsp;|&nbsp;
                Vol24: <b>${fmtUSD(x.vol24_usdt)}</b> USDT
              </div>
              <div style="margin-top:8px">
                <div class="pill" style="display:inline-block;margin-right:6px">Entry: <b>${plan.entry}</b></div>
                <div class="pill" style="display:inline-block;margin-right:6px">Stop: <b>${plan.stop}</b></div>
                <div class="pill" style="display:inline-block;margin-right:6px">TP1: <b>${plan.tp1}</b></div>
                <div class="pill" style="display:inline-block">TP2: <b>${plan.tp2}</b></div>
              </div>
            `;
            list.appendChild(el);
          });
        }

        const whales = document.getElementById('whales');
        if(!j.whale_alerts || j.whale_alerts.length === 0){
          whales.innerHTML = `<div class="muted">Şu an threshold üstü whale işlemi yakalanmadı (veya endpoint limit/kısıt).</div>`;
        }else{
          j.whale_alerts.slice(0,12).forEach(w=>{
            const el = document.createElement('div'); el.className="card";
            if(w.error){
              el.innerHTML = `<b>${w.symbol}</b> <small>(${w.pair})</small><br><span class="warn">${w.error}</span>`;
            }else{
              const sideCls = (w.side === "BUY") ? "good" : "bad";
              el.innerHTML = `
                <div class="row" style="justify-content:space-between">
                  <div><b>${w.symbol}</b> <small>(${w.pair})</small></div>
                  <div class="pill ${sideCls}"><b>${w.side}</b></div>
                </div>
                <div class="muted">
                  Value: <b>$${fmtUSD(w.usd)}</b> | Qty: <b>${w.qty}</b> | Price: <b>${w.price}</b>
                </div>
                <div class="muted">Time: ${w.time}</div>
              `;
            }
            whales.appendChild(el);
          });
        }
      }

      loadData();
    </script>
  </body>
</html>
    """
