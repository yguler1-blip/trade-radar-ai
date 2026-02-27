from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, statistics
import requests

app = FastAPI(title="Trade Radar (MVP+)")

# ---------------------------
# CONFIG
# ---------------------------
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "60"))
TOP_PICKS = int(os.getenv("TOP_PICKS", "10"))

MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", "60000000"))   # 60M/day
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", "0.00001"))

MAX_ABS_24H = float(os.getenv("MAX_ABS_24H", "25"))         # pump killer
MIN_ABS_24H = float(os.getenv("MIN_ABS_24H", "2.0"))

WHALE_TTL_SEC = int(os.getenv("WHALE_TTL_SEC", "30"))
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "80"))
WHALE_NOTIONAL_USD = float(os.getenv("WHALE_NOTIONAL_USD", "750000"))

USER_AGENT = {"User-Agent": "trade-radar-mvp-plus"}

STABLE_SKIP = {
    "USDT","USDC","DAI","BUSD","TUSD","USDE","USD1","FDUSD","EURT","USDP","PYUSD","FRAX"
}

_cache_top = {"ts": 0, "data": None}
_cache_whales = {"ts": 0, "data": None}

# ---------------------------
# HELPERS
# ---------------------------
def now() -> int:
    return int(time.time())

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def http_get_json(url, params=None, timeout=20, headers=None):
    h = dict(USER_AGENT)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json()

def score_coin(p24, vol24_usd, spread_pct=0.002, market_gate="NEUTRAL"):
    # momentum
    p24c = clamp(p24, -18.0, 22.0)
    momentum = clamp((p24c + 18.0) / 40.0 * 100.0, 0, 100)

    # liquidity
    vol = max(vol24_usd, 1.0)
    liq = clamp((math.log10(vol) - 7.5) / (10.0 - 7.5) * 100.0, 0, 100)

    # spread score (rough)
    spread = clamp((0.010 - spread_pct) / (0.010 - 0.001) * 100.0, 0, 100)

    base = 0.35 * momentum + 0.50 * liq + 0.15 * spread

    if market_gate in ("BEARISH", "PANIC"):
        base -= 6.0
        if p24 > 8:
            base -= 4.0

    return round(clamp(base, 0, 100), 1)

def build_trade_plan(price):
    # very simple plan; later ATR ekleriz
    stop = price * 0.975
    tp1 = price * 1.03
    tp2 = price * 1.055
    return {"entry": round(price, 8), "stop": round(stop, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8)}

def compute_market_mode(rows_sorted_by_vol):
    by_sym = {r["symbol"]: r for r in rows_sorted_by_vol}
    btc = by_sym.get("BTC")
    eth = by_sym.get("ETH")
    btc_chg = safe_float(btc["chg24_pct"]) if btc else 0.0
    eth_chg = safe_float(eth["chg24_pct"]) if eth else 0.0

    top20 = rows_sorted_by_vol[:20] if len(rows_sorted_by_vol) >= 20 else rows_sorted_by_vol
    median20 = statistics.median([safe_float(x["chg24_pct"]) for x in top20]) if top20 else 0.0

    idx = 0.5 * btc_chg + 0.3 * eth_chg + 0.2 * median20

    if idx > 1.2:
        mode = "STRONG BULLISH"
        gate = "BULLISH"
    elif idx > 0.4:
        mode = "BULLISH"
        gate = "BULLISH"
    elif idx < -1.2:
        mode = "PANIC"
        gate = "PANIC"
    elif idx < -0.4:
        mode = "BEARISH"
        gate = "BEARISH"
    else:
        mode = "NEUTRAL"
        gate = "NEUTRAL"

    return mode, gate, round(idx, 2), round(btc_chg, 2), round(eth_chg, 2), round(median20, 2)

def explain_pick(p24, vol24):
    reasons = []
    if vol24 >= 300_000_000:
        reasons.append("çok yüksek likidite")
    elif vol24 >= 120_000_000:
        reasons.append("yüksek likidite")

    if p24 >= 6:
        reasons.append("güçlü momentum")
    elif p24 >= 3:
        reasons.append("pozitif momentum")
    else:
        reasons.append("ılımlı hareket")

    return ", ".join(reasons)

# ---------------------------
# BINANCE DATA
# ---------------------------
def fetch_binance_24h_all():
    # NOTE: This is the workhorse. With caching we avoid 429.
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = http_get_json(url, timeout=25)
    return data if isinstance(data, list) else []

def fetch_binance_agg_trades(symbol, limit=WHALE_LOOKBACK_TRADES):
    pair = f"{symbol}USDT"
    url = "https://api.binance.com/api/v3/aggTrades"
    params = {"symbol": pair, "limit": limit}
    return http_get_json(url, params=params, timeout=20)

# ---------------------------
# TOP PICKS (BINANCE ONLY)
# ---------------------------
def get_top_picks_cached():
    if _cache_top["data"] and (now() - _cache_top["ts"] < CACHE_TTL_SEC):
        return _cache_top["data"]

    source = "binance"
    warning = None

    rows_all = []
    try:
        data = fetch_binance_24h_all()
    except Exception as e:
        payload = {
            "ts": now(),
            "source": source,
            "market_mode": "UNKNOWN",
            "market_gate": "UNKNOWN",
            "market_index": 0,
            "btc_24h": 0,
            "eth_24h": 0,
            "median20_24h": 0,
            "top_picks": [],
            "config": {
                "whale_threshold_usd": int(WHALE_NOTIONAL_USD),
                "min_vol_usd": int(MIN_VOL_USD),
                "min_abs_24h": MIN_ABS_24H,
                "max_abs_24h": MAX_ABS_24H,
            },
            "warning": f"Binance fetch failed: {repr(e)}"
        }
        _cache_top["ts"] = now()
        _cache_top["data"] = payload
        return payload

    # filter USDT pairs only, ignore leveraged tokens, ignore stables
    for x in data:
        sym_pair = (x.get("symbol") or "")
        if not sym_pair.endswith("USDT"):
            continue
        base = sym_pair[:-4]  # remove USDT

        if not base or base in STABLE_SKIP:
            continue
        if "UP" in base or "DOWN" in base or base.endswith("BULL") or base.endswith("BEAR"):
            continue

        last_price = safe_float(x.get("lastPrice"))
        quote_vol = safe_float(x.get("quoteVolume"))  # already in quote (USDT) -> USD-ish
        p24 = safe_float(x.get("priceChangePercent"))

        if last_price <= 0 or quote_vol <= 0:
            continue

        rows_all.append({
            "symbol": base,
            "pair": sym_pair,
            "price": last_price,
            "chg24_pct": p24,
            "vol24_usd": quote_vol
        })

    # market mode calc from top volume
    rows_by_vol = sorted(rows_all, key=lambda r: r["vol24_usd"], reverse=True)
    market_mode, gate, market_index, btc24, eth24, median20 = compute_market_mode(rows_by_vol)

    # tradeable filter + score
    tradeable = []
    for r in rows_all:
        price = r["price"]
        vol24 = r["vol24_usd"]
        p24 = r["chg24_pct"]

        if price < MIN_PRICE_USD:
            continue
        if vol24 < MIN_VOL_USD:
            continue
        if abs(p24) > MAX_ABS_24H:
            continue
        if abs(p24) < MIN_ABS_24H:
            continue

        score = score_coin(p24=p24, vol24_usd=vol24, market_gate=gate)
        tradeable.append({
            "symbol": r["symbol"],
            "price": round(price, 6),
            "chg24_pct": round(p24, 2),
            "vol24_usd": int(vol24),
            "score": score,
            "plan": build_trade_plan(price),
            "why": explain_pick(p24, vol24)
        })

    tradeable.sort(key=lambda r: r["score"], reverse=True)

    payload = {
        "ts": now(),
        "source": source,
        "market_mode": market_mode,
        "market_gate": gate,
        "market_index": market_index,
        "btc_24h": btc24,
        "eth_24h": eth24,
        "median20_24h": median20,
        "top_picks": tradeable[:TOP_PICKS],
        "config": {
            "whale_threshold_usd": int(WHALE_NOTIONAL_USD),
            "min_vol_usd": int(MIN_VOL_USD),
            "min_abs_24h": MIN_ABS_24H,
            "max_abs_24h": MAX_ABS_24H,
        }
    }
    if warning:
        payload["warning"] = warning

    _cache_top["ts"] = now()
    _cache_top["data"] = payload
    return payload

# ---------------------------
# WHALES
# ---------------------------
def get_whales_cached():
    if _cache_whales["data"] and (now() - _cache_whales["ts"] < WHALE_TTL_SEC):
        return _cache_whales["data"]

    top = get_top_picks_cached()
    picks = (top.get("top_picks") or [])
    symbols = [x["symbol"] for x in picks][:7]

    alerts = []
    for s in symbols:
        try:
            trades = fetch_binance_agg_trades(s, limit=WHALE_LOOKBACK_TRADES)
            for t in trades:
                price = safe_float(t.get("p"))
                qty = safe_float(t.get("q"))
                ts = int(t.get("T", 0) / 1000) if t.get("T") else 0
                notional = price * qty
                if notional >= WHALE_NOTIONAL_USD:
                    alerts.append({
                        "symbol": s,
                        "pair": f"{s}USDT",
                        "notional_usd": int(notional),
                        "price": round(price, 6),
                        "qty": round(qty, 6),
                        "ts": ts,
                    })
        except Exception:
            continue

    alerts.sort(key=lambda a: (a.get("ts", 0), a.get("notional_usd", 0)), reverse=True)
    alerts = alerts[:20]

    payload = {"ts": now(), "whales": alerts, "threshold_usd": int(WHALE_NOTIONAL_USD)}

    _cache_whales["ts"] = now()
    _cache_whales["data"] = payload
    return payload

# ---------------------------
# API
# ---------------------------
@app.get("/api/top", response_class=JSONResponse)
def api_top():
    return get_top_picks_cached()

@app.get("/api/whales", response_class=JSONResponse)
def api_whales():
    return get_whales_cached()

# ---------------------------
# UI
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
      <head>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <style>
          body{font-family:Arial;margin:18px;max-width:1050px}
          .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
          .pill{border:1px solid #ddd;border-radius:999px;padding:8px 12px;display:inline-block}
          .card{border:1px solid #ddd;border-radius:14px;padding:12px}
          .muted{color:#666}
          button{padding:8px 12px;border-radius:10px;border:1px solid #ccc;background:#fafafa}
          .grid{display:grid;grid-template-columns:1fr;gap:10px}
          @media(min-width:820px){.grid{grid-template-columns:1fr 1fr}}
          .title{font-size:20px;font-weight:700;margin:0 0 6px}
          .small{font-size:13px}
          .score{font-weight:700}
          .danger{color:#b00020}
          .ok{color:#0b6}
          .warn{color:#c90}
          .mono{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace}
        </style>
      </head>
      <body>
        <div class="title">Trade Radar (MVP+) — Yiğit Mode</div>

        <div class="row">
          <div class="pill" id="mode">Market Mode: Loading...</div>
          <div class="pill" id="btc">BTC 24h: ...</div>
          <div class="pill" id="eth">ETH 24h: ...</div>
          <div class="pill" id="idx">Index: ...</div>
          <div class="pill muted small" id="cfg">Config: ...</div>
          <div class="pill muted small" id="src">Source: ...</div>
          <button onclick="reloadAll()">Yenile</button>
        </div>

        <div id="err" class="danger" style="margin-top:10px"></div>

        <h3 style="margin-top:18px">🔥 Top 10 “Tradeable” Picks</h3>
        <div class="muted small">Not: Bu bir yatırım tavsiyesi değil. Sistem skor + risk şablonu üretir.</div>
        <div id="picks" class="grid" style="margin-top:10px"></div>

        <h3 style="margin-top:18px">🐋 Whale Alerts (Binance, threshold: <span class="mono" id="whaleTh"></span>)</h3>
        <div id="whales" style="margin-top:10px"></div>

        <script>
          function pctClass(v){
            if(v > 1) return "ok";
            if(v < -1) return "danger";
            return "warn";
          }

          async function reloadAll(){
            document.getElementById('err').innerText = "";
            document.getElementById('picks').innerHTML = "";
            document.getElementById('whales').innerHTML = "";
            document.getElementById('mode').innerText = "Market Mode: Loading...";

            try{
              const r = await fetch("/api/top");
              const top = await r.json();

              document.getElementById('mode').innerText = "Market Mode: " + (top.market_mode || "UNKNOWN");
              document.getElementById('btc').innerHTML = `BTC 24h: <span class="${pctClass(top.btc_24h||0)}">${(top.btc_24h||0).toFixed(2)}%</span>`;
              document.getElementById('eth').innerHTML = `ETH 24h: <span class="${pctClass(top.eth_24h||0)}">${(top.eth_24h||0).toFixed(2)}%</span>`;
              document.getElementById('idx').innerHTML = `Index: <span class="${pctClass(top.market_index||0)}">${(top.market_index||0).toFixed(2)}</span>`;
              document.getElementById('src').innerText = "Source: " + (top.source || "unknown");

              if(top.config){
                document.getElementById('cfg').innerText =
                  `VolMin=${(top.config.min_vol_usd||0).toLocaleString()} | 24h%=${top.config.min_abs_24h}-${top.config.max_abs_24h} | Whale=${(top.config.whale_threshold_usd||0).toLocaleString()}`;
              }

              if(top.warning){
                document.getElementById('err').innerText = "Warning: " + top.warning;
              }

              const picks = (top.top_picks || []);
              const box = document.getElementById('picks');

              if(!picks.length){
                const el = document.createElement('div');
                el.className="card";
                el.innerHTML = `<b>Top 10 boş geldi.</b><div class="muted small">Filtreler çok sıkı olabilir (VolMin / 24h%).</div>`;
                box.appendChild(el);
              }else{
                picks.forEach(p=>{
                  const el = document.createElement('div');
                  el.className = "card";
                  el.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
                      <div><b>${p.symbol}</b> <span class="muted small">(${p.why || ""})</span></div>
                      <div class="score">Score: ${p.score}</div>
                    </div>
                    <div class="small" style="margin-top:6px">
                      Price: <b>${p.price}</b> USD |
                      24h: <span class="${pctClass(p.chg24_pct||0)}">${(p.chg24_pct||0).toFixed(2)}%</span> |
                      Vol24: ${Number(p.vol24_usd||0).toLocaleString()} USD
                    </div>
                    <div class="small" style="margin-top:8px">
                      Plan: Entry <b>${p.plan?.entry ?? "-"}</b>,
                      SL <b>${p.plan?.stop ?? "-"}</b>,
                      TP1 <b>${p.plan?.tp1 ?? "-"}</b>,
                      TP2 <b>${p.plan?.tp2 ?? "-"}</b>
                    </div>
                  `;
                  box.appendChild(el);
                });
              }

              const wr = await fetch("/api/whales");
              const wj = await wr.json();
              document.getElementById('whaleTh').innerText = "$" + (wj.threshold_usd||0).toLocaleString();

              if(wj && wj.whales && wj.whales.length){
                const wbox = document.getElementById('whales');
                wj.whales.forEach(w=>{
                  const d = new Date((w.ts||0)*1000);
                  const el = document.createElement('div');
                  el.className = "card";
                  el.innerHTML = `
                    <b>${w.pair}</b> — <b>$${Number(w.notional_usd||0).toLocaleString()}</b>
                    <div class="muted small">Price: ${w.price} | Qty: ${w.qty} | Time: ${d.toLocaleString()}</div>
                  `;
                  wbox.appendChild(el);
                });
              } else {
                document.getElementById('whales').innerHTML =
                  `<div class="muted small">Şu an threshold üstü whale işlemi yakalanmadı (veya Binance rate-limit).</div>`;
              }

            }catch(e){
              document.getElementById('err').innerText = "UI Error: " + e.message;
              document.getElementById('mode').innerText = "Market Mode: UNKNOWN";
            }
          }

          reloadAll();
          setInterval(reloadAll, 30000);
        </script>
      </body>
    </html>
    """
