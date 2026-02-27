from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, statistics
import requests

app = FastAPI(title="Trade Radar (MVP+)")


# ---------------------------
# CONFIG
# ---------------------------
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "60"))
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT", "50"))          # how many coins to scan from CryptoCompare
TOP_PICKS = int(os.getenv("TOP_PICKS", "10"))                    # return top N picks
MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", "50000000"))        # 24h volume filter (USD)
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", "0.00001"))
MAX_ABS_24H = float(os.getenv("MAX_ABS_24H", "35"))              # avoid extreme pump > 35% 24h
MIN_ABS_24H = float(os.getenv("MIN_ABS_24H", "2"))               # avoid dead coins < 2% 24h (tunable)
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "35"))            # bias buys near oversold
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "75"))        # penalize too hot

# Whale detection
WHALE_TTL_SEC = int(os.getenv("WHALE_TTL_SEC", "20"))            # whale endpoint cache
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "80"))
WHALE_NOTIONAL_USD = float(os.getenv("WHALE_NOTIONAL_USD", "500000"))  # $ threshold per trade

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
_cache_binance_symbols = {"ts": 0, "set": set()}  # refreshed occasionally


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

    # Wilder smoothing
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
    # Wilder
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def build_trade_plan(price, atr_val=None):
    # A simple "scalp-ish" plan (not advice): tighter if ATR available
    if not price or price <= 0:
        return {"entry": None, "stop": None, "tp1": None, "tp2": None}
    if atr_val and atr_val > 0:
        # use ATR for dynamic levels
        stop = price - 1.2 * atr_val
        tp1 = price + 1.0 * atr_val
        tp2 = price + 1.8 * atr_val
    else:
        stop = price * 0.97
        tp1 = price * 1.04
        tp2 = price * 1.07
    return {
        "entry": round(price, 8),
        "stop": round(max(stop, 0), 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
    }


def score_coin(p24, vol24_usd, spread_pct, rsi14=None, ema20v=None, ema50v=None, atrv=None, price=None):
    """
    Score 0-100:
    - momentum (24h% in a sane range)
    - liquidity (log volume)
    - trend (EMA20 > EMA50 bonus)
    - RSI (penalize too hot, bonus slightly oversold)
    - risk (ATR% too high penalize)
    """
    # Momentum component (cap)
    p24c = clamp(p24, -20.0, 30.0)
    momentum = (p24c + 20.0) / 50.0 * 100.0
    momentum = clamp(momentum, 0, 100)

    # Liquidity component (log scale)
    vol = max(vol24_usd, 1.0)
    liq = clamp((math.log10(vol) - 7.0) / (10.0 - 7.0) * 100.0, 0, 100)  # 10M..10B+

    # Spread component (lower spread better) – our spread is an estimate for banks/exchanges
    spread = clamp((0.010 - spread_pct) / (0.010 - 0.001) * 100.0, 0, 100)

    # Trend component
    trend = 50.0
    if ema20v is not None and ema50v is not None:
        trend = 75.0 if ema20v > ema50v else 35.0

    # RSI component
    rsi_score = 50.0
    if rsi14 is not None:
        if rsi14 < RSI_OVERSOLD:
            rsi_score = 75.0  # slightly oversold = interesting for bounce
        elif rsi14 > RSI_OVERBOUGHT:
            rsi_score = 20.0  # too hot
        else:
            # map 35..75 to 75..35 (more neutral-ish)
            rsi_score = clamp(75.0 - (rsi14 - RSI_OVERSOLD) * (40.0 / (RSI_OVERBOUGHT - RSI_OVERSOLD)), 35.0, 75.0)

    # ATR% risk penalty
    risk = 50.0
    if atrv is not None and price and price > 0:
        atr_pct = (atrv / price) * 100.0
        # too volatile -> lower score
        if atr_pct > 6:
            risk = 20.0
        elif atr_pct > 4:
            risk = 35.0
        else:
            risk = 65.0

    base = (
        0.30 * momentum +
        0.28 * liq +
        0.12 * spread +
        0.15 * trend +
        0.10 * rsi_score +
        0.05 * risk
    )
    return round(clamp(base, 0, 100), 1)


# ---------------------------
# DATA SOURCES
# ---------------------------
def fetch_universe_cryptocompare(limit=UNIVERSE_LIMIT):
    url = "https://min-api.cryptocompare.com/data/top/totalvolfull"
    params = {"limit": limit, "tsym": "USD"}
    payload = http_get_json(url, params=params, timeout=25)
    return payload.get("Data", []) or []


def fetch_histohour_cryptocompare(symbol, hours=200):
    # hourly OHLC (USD)
    url = "https://min-api.cryptocompare.com/data/v2/histohour"
    params = {"fsym": symbol, "tsym": "USD", "limit": hours}
    payload = http_get_json(url, params=params, timeout=25)
    data = ((payload.get("Data") or {}).get("Data")) or []
    return data


def refresh_binance_symbol_set(force=False):
    # cache for 6 hours
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
        # keep old if exists
        return _cache_binance_symbols["set"]


def fetch_binance_recent_trades_usdt(symbol, limit=WHALE_LOOKBACK_TRADES):
    # aggTrades is lighter and public
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
# MARKET MODE
# ---------------------------
def compute_market_mode_from_universe(rows):
    """
    Market Index = 0.5*BTC + 0.3*ETH + 0.2*Top20 median
    """
    by_sym = {r["symbol"]: r for r in rows}
    btc = by_sym.get("BTC")
    eth = by_sym.get("ETH")

    btc_chg = safe_float(btc["chg24_pct"]) if btc else 0.0
    eth_chg = safe_float(eth["chg24_pct"]) if eth else 0.0

    top20 = rows[:20] if len(rows) >= 20 else rows
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

    return mode, round(idx, 2), round(btc_chg, 2), round(eth_chg, 2), round(median20, 2)


# ---------------------------
# CORE: TOP PICKS
# ---------------------------
def get_top_picks_cached():
    if _cache_top["data"] and (now() - _cache_top["ts"] < CACHE_TTL_SEC):
        return _cache_top["data"]

    try:
        universe = fetch_universe_cryptocompare(UNIVERSE_LIMIT)
        rows_raw = []

        for item in universe:
            coin_info = item.get("CoinInfo", {}) or {}
            raw = (item.get("RAW", {}) or {}).get("USD", {}) or {}
            symbol = coin_info.get("Name", "") or ""
            if not symbol or symbol in STABLE_SKIP:
                continue

            price = safe_float(raw.get("PRICE"))
            p24 = safe_float(raw.get("CHANGEPCT24HOUR"))
            vol24 = safe_float(raw.get("TOTALVOLUME24H"), 0.0) * price  # approx USD volume

            if price < MIN_PRICE_USD:
                continue
            if vol24 < MIN_VOL_USD:
                continue
            if abs(p24) > MAX_ABS_24H:
                continue
            if abs(p24) < MIN_ABS_24H:
                continue

            # We'll enrich with indicators
            rows_raw.append({
                "symbol": symbol,
                "price": price,
                "chg24_pct": p24,
                "vol24_usd": vol24,
            })

        # Sort raw by volume first (so we enrich fewer coins)
        rows_raw.sort(key=lambda x: x["vol24_usd"], reverse=True)
        rows_raw = rows_raw[:min(30, len(rows_raw))]  # enrich up to 30, score and pick top10

        enriched = []
        for r in rows_raw:
            sym = r["symbol"]
            price = r["price"]

            rsi14 = None
            ema20v = None
            ema50v = None
            atrv = None

            try:
                candles = fetch_histohour_cryptocompare(sym, hours=200)
                closes = [safe_float(c.get("close")) for c in candles if safe_float(c.get("close")) > 0]
                highs = [safe_float(c.get("high")) for c in candles if safe_float(c.get("high")) > 0]
                lows = [safe_float(c.get("low")) for c in candles if safe_float(c.get("low")) > 0]

                if len(closes) >= 60:
                    rsi14 = rsi(closes, 14)
                    ema20v = ema(closes[-80:], 20)
                    ema50v = ema(closes[-120:], 50)
                    if len(highs) == len(lows) == len(closes):
                        atrv = atr(highs, lows, closes, 14)
            except Exception:
                pass

            # Spread estimate placeholder (banks/exchanges differ). keep modest.
            spread_pct = 0.002

            score = score_coin(
                p24=r["chg24_pct"],
                vol24_usd=r["vol24_usd"],
                spread_pct=spread_pct,
                rsi14=rsi14,
                ema20v=ema20v,
                ema50v=ema50v,
                atrv=atrv,
                price=price,
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
                "why": _explain_pick(r["chg24_pct"], r["vol24_usd"], rsi14, ema20v, ema50v),
            })

        enriched.sort(key=lambda x: x["score"], reverse=True)

        # Market mode from the same enriched list plus BTC/ETH if present
        # For mode, we want a broader set: use volume-sorted initial (rows_raw) + try inject BTC/ETH from universe quickly
        mode_rows = []
        for item in universe[:50]:
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
            mode_rows.append({
                "symbol": sym, "chg24_pct": round(p24, 2), "vol24_usd": int(vol24)
            })
        mode_rows.sort(key=lambda x: x["vol24_usd"], reverse=True)

        market_mode, market_index, btc24, eth24, median20 = compute_market_mode_from_universe(mode_rows)

        payload = {
            "ts": now(),
            "market_mode": market_mode,
            "market_index": market_index,
            "btc_24h": btc24,
            "eth_24h": eth24,
            "median20_24h": median20,
            "top_picks": enriched[:TOP_PICKS],
        }

        _cache_top["ts"] = now()
        _cache_top["data"] = payload
        return payload

    except Exception as e:
        payload = {
            "ts": now(),
            "market_mode": "UNKNOWN",
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


def _explain_pick(p24, vol24, rsi14, ema20v, ema50v):
    reasons = []
    if vol24 >= 200_000_000:
        reasons.append("çok yüksek likidite")
    elif vol24 >= 80_000_000:
        reasons.append("yüksek likidite")

    if p24 >= 8:
        reasons.append("güçlü momentum")
    elif p24 >= 3:
        reasons.append("pozitif momentum")

    if rsi14 is not None:
        if rsi14 < RSI_OVERSOLD:
            reasons.append("RSI düşük (bounce potansiyeli)")
        elif rsi14 > RSI_OVERBOUGHT:
            reasons.append("RSI yüksek (ısınmış)")

    if ema20v is not None and ema50v is not None:
        if ema20v > ema50v:
            reasons.append("trend yukarı (EMA20>EMA50)")
        else:
            reasons.append("trend zayıf (EMA20<EMA50)")

    return ", ".join(reasons) if reasons else "likidite + momentum filtresi"


# ---------------------------
# WHALE ALARMS
# ---------------------------
def get_whales_cached():
    if _cache_whales["data"] and (now() - _cache_whales["ts"] < WHALE_TTL_SEC):
        return _cache_whales["data"]

    top = get_top_picks_cached()
    symbols = [x["symbol"] for x in (top.get("top_picks") or [])]

    # Binance USDT pairs availability
    binance_set = refresh_binance_symbol_set()
    watch = [s for s in symbols if s in binance_set]

    alerts = []
    for s in watch[:8]:  # keep it light
        try:
            trades = fetch_binance_recent_trades_usdt(s, limit=WHALE_LOOKBACK_TRADES)
            # trades are in chronological order? not guaranteed; we'll scan all
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

    # Sort newest & biggest first
    alerts.sort(key=lambda a: (a.get("ts", 0), a.get("notional_usd", 0)), reverse=True)
    alerts = alerts[:20]

    payload = {"ts": now(), "whales": alerts}

    # Telegram (optional) – only send the top newest one, and only if it's fresh
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
# UI (backend-driven)
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
      <head>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <style>
          body{font-family:Arial;margin:18px;max-width:980px}
          .row{display:flex;gap:12px;flex-wrap:wrap}
          .pill{border:1px solid #ddd;border-radius:999px;padding:8px 12px;display:inline-block}
          .card{border:1px solid #ddd;border-radius:14px;padding:12px;margin:10px 0}
          .muted{color:#666}
          button{padding:8px 12px;border-radius:10px;border:1px solid #ccc;background:#fafafa}
          .grid{display:grid;grid-template-columns:1fr;gap:10px}
          @media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}
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
        <div class="title">Trade Radar (MVP+)</div>

        <div class="row">
          <div class="pill" id="mode">Market Mode: Loading...</div>
          <div class="pill" id="btc">BTC 24h: ...</div>
          <div class="pill" id="eth">ETH 24h: ...</div>
          <div class="pill" id="idx">Index: ...</div>
          <button onclick="reloadAll()">Yenile</button>
        </div>

        <div id="err" class="danger" style="margin-top:10px"></div>

        <h3 style="margin-top:18px">🔥 Top 10 Picks</h3>
        <div id="picks" class="grid"></div>

        <h3 style="margin-top:18px">🐋 Whale Alerts (Binance)</h3>
        <div class="muted small">Büyük tekil işlemler (threshold: <span class="mono" id="whaleTh"></span> USD)</div>
        <div id="whales"></div>

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
                    Plan (scalp template): Entry <b>${p.plan?.entry ?? "-"}</b>,
                    SL <b>${p.plan?.stop ?? "-"}</b>,
                    TP1 <b>${p.plan?.tp1 ?? "-"}</b>,
                    TP2 <b>${p.plan?.tp2 ?? "-"}</b>
                  </div>
                `;
                box.appendChild(el);
              });

              // whales
              document.getElementById('whaleTh').innerText = (500000).toLocaleString();
              const wr = await fetch("/api/whales");
              const wj = await wr.json();
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
                document.getElementById('whales').innerHTML = `<div class="muted small">Şu an threshold üstü whale işlemi yakalanmadı (veya Binance rate-limit).</div>`;
              }

            }catch(e){
              document.getElementById('err').innerText = "UI Error: " + e.message;
              document.getElementById('mode').innerText = "Market Mode: UNKNOWN";
            }
          }

          reloadAll();
          // auto refresh every 30s
          setInterval(reloadAll, 30000);
        </script>
      </body>
    </html>
    """
