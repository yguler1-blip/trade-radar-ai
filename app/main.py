from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, statistics
import requests

app = FastAPI(title="Trade Radar (MVP+)")


# ---------------------------
# CONFIG (chosen defaults)
# ---------------------------
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "75"))
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT", "70"))
TOP_PICKS = int(os.getenv("TOP_PICKS", "10"))

# Scalp-friendly but safer
MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", "60000000"))        # 60M/day
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", "0.00001"))
MAX_ABS_24H = float(os.getenv("MAX_ABS_24H", "25"))              # pump killer
MIN_ABS_24H = float(os.getenv("MIN_ABS_24H", "2.0"))

RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "35"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "72"))        # a bit stricter

# Whale detection
WHALE_TTL_SEC = int(os.getenv("WHALE_TTL_SEC", "30"))
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "70"))
WHALE_NOTIONAL_USD = float(os.getenv("WHALE_NOTIONAL_USD", "750000"))  # chosen: 750k

# Optional Telegram alerts
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_ENABLED = bool(TG_TOKEN and TG_CHAT_ID and os.getenv("TELEGRAM_ENABLED", "0") == "1")

USER_AGENT = {"User-Agent": "trade-radar-mvp-plus"}

STABLE_SKIP = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDE", "USD1", "FDUSD",
    "EURT", "USDP", "PYUSD", "FRAX",
}

# ---------------------------
# SIMPLE IN-MEMORY CACHES
# ---------------------------
_cache_top = {"ts": 0, "data": None}
_cache_whales = {"ts": 0, "data": None}
_cache_binance_symbols = {"ts": 0, "set": set()}


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


def ema(values, period):
    if not values or period <= 0:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def build_trade_plan(price, atr_val=None):
    if not price or price <= 0:
        return {"entry": None, "stop": None, "tp1": None, "tp2": None}
    if atr_val and atr_val > 0:
        stop = price - 1.15 * atr_val
        tp1 = price + 0.95 * atr_val
        tp2 = price + 1.65 * atr_val
    else:
        stop = price * 0.975
        tp1 = price * 1.03
        tp2 = price * 1.055
    return {
        "entry": round(price, 8),
        "stop": round(max(stop, 0), 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
    }


def score_coin(p24, vol24_usd, spread_pct, rsi14=None, ema20v=None, ema50v=None, atrv=None, price=None, market_gate="NEUTRAL"):
    # Momentum (keep moderate)
    p24c = clamp(p24, -18.0, 22.0)
    momentum = (p24c + 18.0) / 40.0 * 100.0
    momentum = clamp(momentum, 0, 100)

    # Liquidity (log)
    vol = max(vol24_usd, 1.0)
    liq = clamp((math.log10(vol) - 7.5) / (10.0 - 7.5) * 100.0, 0, 100)  # 30M..10B+

    # Spread estimate score
    spread = clamp((0.010 - spread_pct) / (0.010 - 0.001) * 100.0, 0, 100)

    # Trend (dominant in "safer" mode)
    trend = 45.0
    if ema20v is not None and ema50v is not None:
        trend = 85.0 if ema20v > ema50v else 25.0

    # RSI: prefer not overheated
    rsi_score = 55.0
    if rsi14 is not None:
        if rsi14 < RSI_OVERSOLD:
            rsi_score = 70.0
        elif rsi14 > RSI_OVERBOUGHT:
            rsi_score = 15.0
        else:
            # 35..72 -> 70..35
            rsi_score = clamp(70.0 - (rsi14 - RSI_OVERSOLD) * (35.0 / (RSI_OVERBOUGHT - RSI_OVERSOLD)), 35.0, 70.0)

    # ATR% risk
    risk = 55.0
    if atrv is not None and price and price > 0:
        atr_pct = (atrv / price) * 100.0
        if atr_pct > 6:
            risk = 15.0
        elif atr_pct > 4.2:
            risk = 30.0
        else:
            risk = 70.0

    base = (
        0.22 * momentum +
        0.30 * liq +
        0.10 * spread +
        0.22 * trend +
        0.11 * rsi_score +
        0.05 * risk
    )

    # Market gate: in BEARISH/PANIC, require more quality
    if market_gate in ("BEARISH", "PANIC"):
        base -= 6.0
        if ema20v is not None and ema50v is not None and ema20v < ema50v:
            base -= 6.0
        if rsi14 is not None and rsi14 > 65:
            base -= 4.0

    return round(clamp(base, 0, 100), 1)


# ---------------------------
# DATA SOURCES
# ---------------------------
def fetch_universe_cryptocompare(limit=UNIVERSE_LIMIT):
    url = "https://min-api.cryptocompare.com/data/top/totalvolfull"
    params = {"limit": limit, "tsym": "USD"}
    payload = http_get_json(url, params=params, timeout=25)
    return payload.get("Data", []) or []


def fetch_histohour_cryptocompare(symbol, hours=240):
    url = "https://min-api.cryptocompare.com/data/v2/histohour"
    params = {"fsym": symbol, "tsym": "USD", "limit": hours}
    payload = http_get_json(url, params=params, timeout=25)
    data = ((payload.get("Data") or {}).get("Data")) or []
    return data


def refresh_binance_symbol_set(force=False):
    if not force and _cache_binance_symbols["ts"] and now() - _cache_binance_symbols["ts"] < 6 * 3600:
        return _cache_binance_symbols["set"]

    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        js = http_get_json(url, timeout=25)
        syms = set()
        for s in js.get("symbols", []):
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                syms.add(s.get("baseAsset"))
        _cache_binance_symbols["ts"] = now()
        _cache_binance_symbols["set"] = syms
        return syms
    except Exception:
        return _cache_binance_symbols["set"]


def fetch_binance_recent_trades_usdt(symbol, limit=WHALE_LOOKBACK_TRADES):
    pair = f"{symbol}USDT"
    url = "https://api.binance.com/api/v3/aggTrades"
    params = {"symbol": pair, "limit": limit}
    return http_get_json(url, params=params, timeout=20)


def send_telegram(text: str):
    if not TG_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


# ---------------------------
# MARKET MODE (Index)
# ---------------------------
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
    elif idx > 0.4:
        mode = "BULLISH"
    elif idx < -1.2:
        mode = "PANIC"
    elif idx < -0.4:
        mode = "BEARISH"
    else:
        mode = "NEUTRAL"

    # a simpler gate label for scoring
    gate = "NEUTRAL"
    if mode in ("BEARISH",):
        gate = "BEARISH"
    if mode in ("PANIC",):
        gate = "PANIC"

    return mode, gate, round(idx, 2), round(btc_chg, 2), round(eth_chg, 2), round(median20, 2)


# ---------------------------
# PICK EXPLANATIONS
# ---------------------------
def explain_pick(p24, vol24, rsi14, ema20v, ema50v, gate):
    reasons = []
    if vol24 >= 300_000_000:
        reasons.append("çok yüksek likidite")
    elif vol24 >= 120_000_000:
        reasons.append("yüksek likidite")

    if p24 >= 6:
        reasons.append("güçlü momentum")
    elif p24 >= 3:
        reasons.append("pozitif momentum")

    if rsi14 is not None:
        if rsi14 < RSI_OVERSOLD:
            reasons.append("RSI düşük (bounce)")
        elif rsi14 > RSI_OVERBOUGHT:
            reasons.append("RSI yüksek (ısınmış)")
        else:
            reasons.append("RSI dengeli")

    if ema20v is not None and ema50v is not None:
        if ema20v > ema50v:
            reasons.append("trend yukarı (EMA20>EMA50)")
        else:
            reasons.append("trend zayıf (EMA20<EMA50)")

    if gate in ("BEARISH", "PANIC"):
        reasons.append(f"market gate: {gate}")

    return ", ".join(reasons) if reasons else "likidite + trend + RSI filtresi"


# ---------------------------
# CORE: TOP PICKS (cached)
# ---------------------------
def get_top_picks_cached():
    if _cache_top["data"] and (now() - _cache_top["ts"] < CACHE_TTL_SEC):
        return _cache_top["data"]

    try:
        universe = fetch_universe_cryptocompare(UNIVERSE_LIMIT)

        # build a volume-sorted reference list for market mode
        mode_rows = []
        base_rows = []
        for item in universe:
            coin_info = item.get("CoinInfo", {}) or {}
            raw = (item.get("RAW", {}) or {}).get("USD", {}) or {}
            sym = coin_info.get("Name", "") or ""
            if not sym or sym in STABLE_SKIP:
                continue

            price = safe_float(raw.get("PRICE"))
            p24 = safe_float(raw.get("CHANGEPCT24HOUR"))
            vol24 = safe_float(raw.get("TOTALVOLUME24H"), 0.0) * price

            if price <= 0 or vol24 <= 0:
                continue

            mode_rows.append({"symbol": sym, "chg24_pct": round(p24, 2), "vol24_usd": int(vol24)})

            # candidate filters
            if price < MIN_PRICE_USD:
                continue
            if vol24 < MIN_VOL_USD:
                continue
            if abs(p24) > MAX_ABS_24H:
                continue
            if abs(p24) < MIN_ABS_24H:
                continue

            base_rows.append({"symbol": sym, "price": price, "chg24_pct": p24, "vol24_usd": vol24})

        mode_rows.sort(key=lambda x: x["vol24_usd"], reverse=True)
        market_mode, gate, market_index, btc24, eth24, median20 = compute_market_mode(mode_rows)

        # enrich a manageable subset
        base_rows.sort(key=lambda x: x["vol24_usd"], reverse=True)
        base_rows = base_rows[:min(35, len(base_rows))]

        enriched = []
        for r in base_rows:
            sym = r["symbol"]
            price = r["price"]

            rsi14 = None
            ema20v = None
            ema50v = None
            atrv = None

            try:
                candles = fetch_histohour_cryptocompare(sym, hours=240)
                closes = [safe_float(c.get("close")) for c in candles if safe_float(c.get("close")) > 0]
                highs = [safe_float(c.get("high")) for c in candles if safe_float(c.get("high")) > 0]
                lows = [safe_float(c.get("low")) for c in candles if safe_float(c.get("low")) > 0]

                if len(closes) >= 80:
                    rsi14 = rsi(closes, 14)
                    ema20v = ema(closes[-100:], 20)
                    ema50v = ema(closes[-150:], 50)
                    if len(highs) == len(lows) == len(closes):
                        atrv = atr(highs, lows, closes, 14)
            except Exception:
                pass

            spread_pct = 0.002  # estimate

            score = score_coin(
                p24=r["chg24_pct"],
                vol24_usd=r["vol24_usd"],
                spread_pct=spread_pct,
                rsi14=rsi14,
                ema20v=ema20v,
                ema50v=ema50v,
                atrv=atrv,
                price=price,
                market_gate=gate,
            )

            enriched.append({
                "symbol": sym,
                "price": round(price, 6),
                "chg24_pct": round(r["chg24_pct"], 2),
                "vol24_usd": int(r["vol24_usd"]),
                "score": score,
                "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                "ema20": round(ema20v, 6) if ema20v is not None else None,
                "ema50": round(ema50v, 6) if ema50v is not None else None,
                "atr": round(atrv, 6) if atrv is not None else None,
                "plan": build_trade_plan(price, atrv),
                "why": explain_pick(r["chg24_pct"], r["vol24_usd"], rsi14, ema20v, ema50v, gate),
            })

        enriched.sort(key=lambda x: x["score"], reverse=True)

        payload = {
            "ts": now(),
            "market_mode": market_mode,
            "market_gate": gate,
            "market_index": market_index,
            "btc_24h": btc24,
            "eth_24h": eth24,
            "median20_24h": median20,
            "top_picks": enriched[:TOP_PICKS],
            "config": {
                "whale_threshold_usd": int(WHALE_NOTIONAL_USD),
                "min_vol_usd": int(MIN_VOL_USD),
                "min_abs_24h": MIN_ABS_24H,
                "max_abs_24h": MAX_ABS_24H,
            }
        }

        _cache_top["ts"] = now()
        _cache_top["data"] = payload
        return payload

    except Exception as e:
        payload = {
            "ts": now(),
            "market_mode": "UNKNOWN",
            "market_gate": "NEUTRAL",
            "market_index": 0,
            "btc_24h": 0,
            "eth_24h": 0,
            "median20_24h": 0,
            "top_picks": [],
            "error": repr(e),
        }
        _cache_top["ts"] = now()
        _cache_top["data"] = payload
        return payload


# ---------------------------
# WHALE ALARMS (cached)
# ---------------------------
def get_whales_cached():
    if _cache_whales["data"] and (now() - _cache_whales["ts"] < WHALE_TTL_SEC):
        return _cache_whales["data"]

    top = get_top_picks_cached()
    picks = (top.get("top_picks") or [])
    symbols = [x["symbol"] for x in picks]

    binance_set = refresh_binance_symbol_set()
    watch = [s for s in symbols if s in binance_set]

    alerts = []
    for s in watch[:7]:  # keep it light
        try:
            trades = fetch_binance_recent_trades_usdt(s, limit=WHALE_LOOKBACK_TRADES)
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

    # Telegram (optional): send newest fresh one
    if TG_ENABLED and alerts:
        newest = alerts[0]
        if newest.get("ts", 0) and now() - newest["ts"] < 60:
            msg = f"🐋 Whale Alert: {newest['pair']} ~${newest['notional_usd']:,} @ {newest['price']}"
            send_telegram(msg)

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
              if(top.error){
                document.getElementById('err').innerText = "Top API Error: " + top.error;
              }

              document.getElementById('mode').innerText = "Market Mode: " + (top.market_mode || "UNKNOWN");
              document.getElementById('btc').innerHTML = `BTC 24h: <span class="${pctClass(top.btc_24h||0)}">${(top.btc_24h||0).toFixed(2)}%</span>`;
              document.getElementById('eth').innerHTML = `ETH 24h: <span class="${pctClass(top.eth_24h||0)}">${(top.eth_24h||0).toFixed(2)}%</span>`;
              document.getElementById('idx').innerHTML = `Index: <span class="${pctClass(top.market_index||0)}">${(top.market_index||0).toFixed(2)}</span>`;
              if(top.config){
                document.getElementById('cfg').innerText =
                  `VolMin=${(top.config.min_vol_usd||0).toLocaleString()} | 24h%=${top.config.min_abs_24h}-${top.config.max_abs_24h} | Whale=${(top.config.whale_threshold_usd||0).toLocaleString()}`;
              }

              const picks = (top.top_picks || []);
              const box = document.getElementById('picks');

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
                  <div class="small muted" style="margin-top:6px">
                    RSI14: ${p.rsi14 ?? "-"} | EMA20: ${p.ema20 ?? "-"} | EMA50: ${p.ema50 ?? "-"} | ATR: ${p.atr ?? "-"}
                  </div>
                  <div class="small" style="margin-top:8px">
                    Plan (template): Entry <b>${p.plan?.entry ?? "-"}</b>,
                    SL <b>${p.plan?.stop ?? "-"}</b>,
                    TP1 <b>${p.plan?.tp1 ?? "-"}</b>,
                    TP2 <b>${p.plan?.tp2 ?? "-"}</b>
                  </div>
                `;
                box.appendChild(el);
              });

              // whales
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
