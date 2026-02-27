# app/main.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import os, time, math, statistics, hashlib
import requests
from datetime import datetime, timezone

app = FastAPI(title="Trade Radar (MVP+) — Yiğit Mode")

# =========================
# CONFIG
# =========================
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))  # seconds for /api/top
KLINES_TTL = int(os.getenv("KLINES_TTL", "90"))  # seconds for indicator cache
SCALP_TTL = int(os.getenv("SCALP_TTL", "30"))
WHALE_TTL = int(os.getenv("WHALE_TTL", "20"))

VOL_MIN_USD = int(os.getenv("VOL_MIN_USD", "60000000"))  # 60M/day default
PCT_MIN = float(os.getenv("PCT_MIN", "2.0"))             # abs 24h min
PCT_MAX = float(os.getenv("PCT_MAX", "25.0"))            # abs 24h max
TOP_N = int(os.getenv("TOP_N", "10"))

# Scalp engine
SCALP_TARGET_MIN = float(os.getenv("SCALP_TARGET_MIN", "0.02"))  # 2%
SCALP_TARGET_MAX = float(os.getenv("SCALP_TARGET_MAX", "0.03"))  # 3%
SCALP_STOP_ATR_MULT = float(os.getenv("SCALP_STOP_ATR_MULT", "1.2"))
SCALP_TAKE_ATR_MULT = float(os.getenv("SCALP_TAKE_ATR_MULT", "1.8"))

# Whale
WHALE_THRESHOLD_USD = float(os.getenv("WHALE_THRESHOLD_USD", "750000"))  # $750k
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "80"))

# Telegram alerts
TG_ENABLED = os.getenv("TELEGRAM_ENABLED", "0") == "1"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

# Stable-ish skip
STABLE_SKIP = {
    "USDT","USDC","BUSD","TUSD","FDUSD","DAI","EUR","TRY","BRL","GBP","AUD","RUB","UAH"
}
BAD_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# =========================
# CACHES (in-memory)
# =========================
_cache_top = {"ts": 0, "data": None}
_cache_klines = {}  # key: (symbol, interval) -> {"ts":..., "data":...}
_cache_scalp = {"ts": 0, "data": None}
_cache_whales = {"ts": 0, "data": None}
_alert_dedup = {"last_hash": ""}  # for telegram spam prevention


# =========================
# HELPERS
# =========================
def now_ts() -> int:
    return int(time.time())

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def fmt_usd(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.2f}M"
    if n >= 1e3: return f"{n/1e3:.2f}K"
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

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def send_telegram(text: str):
    if not (TG_ENABLED and TG_TOKEN and TG_CHAT_ID):
        return
    # dedup
    h = sha1(text)
    if _alert_dedup.get("last_hash") == h:
        return
    _alert_dedup["last_hash"] = h
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


# =========================
# INDICATORS
# =========================
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
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = max(d, 0.0)
        l = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
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


# =========================
# BINANCE FETCHERS
# =========================
def fetch_binance_24h_all():
    return http_get_json_with_fallback("/api/v3/ticker/24hr", timeout=25)

def fetch_binance_klines(symbol_pair: str, interval: str, limit: int = 200):
    # /api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200
    params = {"symbol": symbol_pair, "interval": interval, "limit": limit}
    return http_get_json_with_fallback("/api/v3/klines", params=params, timeout=25)

def fetch_binance_agg_trades(symbol_pair: str, limit: int = WHALE_LOOKBACK_TRADES):
    params = {"symbol": symbol_pair, "limit": limit}
    return http_get_json_with_fallback("/api/v3/aggTrades", params=params, timeout=20)


# =========================
# MARKET MODE + SCORING
# =========================
def compute_market_mode_from_rows(rows_by_vol):
    by_sym = {r["symbol"]: r for r in rows_by_vol}
    btc = by_sym.get("BTC")
    eth = by_sym.get("ETH")
    btc_pct = safe_float(btc["chg24_pct"]) if btc else 0.0
    eth_pct = safe_float(eth["chg24_pct"]) if eth else 0.0

    top20 = rows_by_vol[:20] if len(rows_by_vol) >= 20 else rows_by_vol
    median20 = statistics.median([safe_float(x["chg24_pct"]) for x in top20]) if top20 else 0.0

    idx = 0.5 * btc_pct + 0.3 * eth_pct + 0.2 * median20

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

    return mode, gate, round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)

def score_coin(p24, vol24_usd, spread_hint=0.002, gate="NEUTRAL"):
    # Momentum clamp -18..22
    p24c = clamp(p24, -18.0, 22.0)
    momentum = clamp((p24c + 18.0) / 40.0 * 100.0, 0, 100)

    # Liquidity log 30M..10B
    v = clamp((math.log10(max(vol24_usd, 1.0)) - 7.5) / (10.0 - 7.5) * 100.0, 0, 100)

    # Spread hint
    s = clamp((0.010 - spread_hint) / (0.010 - 0.001) * 100.0, 0, 100)

    base = 0.34 * momentum + 0.52 * v + 0.14 * s

    if gate in ("BEARISH", "PANIC"):
        base -= 6.0
        if p24 > 8:
            base -= 4.0

    return round(clamp(base, 0, 100), 1)

def simple_ai_signal(ind1h, ind4h, ind1d, market_gate: str):
    """
    “AI-like” rule engine:
    - Trend via EMA20 vs EMA50
    - RSI not overheated
    - ATR risk
    Returns BUY / WAIT / AVOID + reasons
    """
    reasons = []
    verdict = "WAIT"

    def trend_label(ind):
        if ind["ema20"] is None or ind["ema50"] is None:
            return None
        return "UP" if ind["ema20"] > ind["ema50"] else "DOWN"

    t1h = trend_label(ind1h)
    t4h = trend_label(ind4h)
    t1d = trend_label(ind1d)

    rsi1h = ind1h.get("rsi")
    rsi4h = ind4h.get("rsi")
    atrp4h = ind4h.get("atr_pct")

    if market_gate == "PANIC":
        reasons.append("Market PANIC: risk yüksek")
        return "AVOID", reasons

    # baseline
    up_votes = sum([1 for t in (t1h, t4h, t1d) if t == "UP"])
    down_votes = sum([1 for t in (t1h, t4h, t1d) if t == "DOWN"])

    if up_votes >= 2:
        reasons.append("Trend: çoklu timeframe UP")
    if down_votes >= 2:
        reasons.append("Trend: çoklu timeframe DOWN")

    # RSI logic
    if rsi4h is not None:
        if rsi4h > 72:
            reasons.append("RSI(4h) yüksek (ısınmış)")
        elif rsi4h < 35:
            reasons.append("RSI(4h) düşük (bounce ihtimali)")
        else:
            reasons.append("RSI(4h) dengeli")

    # ATR risk
    if atrp4h is not None:
        if atrp4h > 6:
            reasons.append("ATR% yüksek (volatil)")
        elif atrp4h > 4.2:
            reasons.append("ATR% orta (dikkat)")
        else:
            reasons.append("ATR% düşük/orta")

    # decision
    if down_votes >= 2:
        verdict = "AVOID"
    elif up_votes >= 2:
        # avoid buying if overheated
        if rsi4h is not None and rsi4h > 72:
            verdict = "WAIT"
        else:
            verdict = "BUY"
    else:
        verdict = "WAIT"

    # bearish gate dampening
    if market_gate == "BEARISH" and verdict == "BUY":
        reasons.append("Market BEARISH: BUY olsa bile küçük pozisyon")
        verdict = "WAIT"

    return verdict, reasons


# =========================
# MULTI-TF INDICATORS
# =========================
def get_indicators(symbol: str, interval: str):
    key = (symbol, interval)
    hit = _cache_klines.get(key)
    if hit and (now_ts() - hit["ts"] <= KLINES_TTL):
        return hit["data"]

    pair = f"{symbol}USDT"
    kl = fetch_binance_klines(pair, interval, limit=200)

    closes, highs, lows = [], [], []
    for k in kl:
        # kline: [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...]
        highs.append(safe_float(k[2]))
        lows.append(safe_float(k[3]))
        closes.append(safe_float(k[4]))

    # indicators
    ema20 = ema(closes[-120:], 20) if len(closes) >= 30 else None
    ema50 = ema(closes[-160:], 50) if len(closes) >= 60 else None
    rsi14 = rsi(closes, 14) if len(closes) >= 20 else None
    atr14 = atr(highs, lows, closes, 14) if len(closes) >= 20 else None

    last = closes[-1] if closes else None
    atr_pct = (atr14 / last * 100.0) if (atr14 and last and last > 0) else None

    data = {
        "interval": interval,
        "last": round(last, 8) if last else None,
        "ema20": round(ema20, 8) if ema20 else None,
        "ema50": round(ema50, 8) if ema50 else None,
        "rsi": round(rsi14, 2) if rsi14 is not None else None,
        "atr": round(atr14, 8) if atr14 else None,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
    }
    _cache_klines[key] = {"ts": now_ts(), "data": data}
    return data


# =========================
# CORE: TOP PICKS
# =========================
def build_top_picks():
    data = fetch_binance_24h_all()

    rows_all = []
    for t in data:
        sym = (t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith(BAD_SUFFIXES):
            continue

        base = sym[:-4]
        if not base or base in STABLE_SKIP:
            continue

        last = safe_float(t.get("lastPrice"))
        p24 = safe_float(t.get("priceChangePercent"))
        qv = safe_float(t.get("quoteVolume"))  # USDT

        if last <= 0 or qv <= 0:
            continue

        rows_all.append({
            "symbol": base,
            "pair": sym,
            "price": last,
            "chg24_pct": p24,
            "vol24_usd": qv
        })

    rows_by_vol = sorted(rows_all, key=lambda r: r["vol24_usd"], reverse=True)
    market_mode, gate, idx, btc24, eth24, median20 = compute_market_mode_from_rows(rows_by_vol)

    picks = []
    for r in rows_all:
        # filters
        if r["vol24_usd"] < VOL_MIN_USD:
            continue
        ap = abs(r["chg24_pct"])
        if ap < PCT_MIN or ap > PCT_MAX:
            continue
        if r["price"] < 0.00001:
            continue

        score = score_coin(r["chg24_pct"], r["vol24_usd"], gate=gate)
        picks.append({
            "symbol": r["symbol"],
            "pair": r["pair"],
            "price": round(r["price"], 8),
            "chg24_pct": round(r["chg24_pct"], 2),
            "vol24_usd": int(r["vol24_usd"]),
            "score": score
        })

    picks.sort(key=lambda x: x["score"], reverse=True)
    picks = picks[:TOP_N]

    # enrich with indicators + AI-like signal
    enriched = []
    for p in picks:
        sym = p["symbol"]
        ind1h = get_indicators(sym, "1h")
        ind4h = get_indicators(sym, "4h")
        ind1d = get_indicators(sym, "1d")
        verdict, reasons = simple_ai_signal(ind1h, ind4h, ind1d, gate)

        plan = {
            "entry": p["price"],
            "stop": round(p["price"] * 0.97, 8),
            "tp1": round(p["price"] * 1.04, 8),
            "tp2": round(p["price"] * 1.07, 8),
        }

        enriched.append({
            **p,
            "market_gate": gate,
            "indicators": {"1h": ind1h, "4h": ind4h, "1d": ind1d},
            "ai_signal": verdict,
            "ai_reasons": reasons[:6],
            "plan": plan,
        })

    return {
        "ts": now_ts(),
        "source": "binance_multi",
        "market_mode": market_mode,
        "market_gate": gate,
        "btc_24h": btc24,
        "eth_24h": eth24,
        "median20_24h": median20,
        "index": idx,
        "filters": {
            "vol_min_usd": VOL_MIN_USD,
            "pct_min": PCT_MIN,
            "pct_max": PCT_MAX,
            "whale_threshold_usd": int(WHALE_THRESHOLD_USD),
            "scalp_target_min": SCALP_TARGET_MIN,
            "scalp_target_max": SCALP_TARGET_MAX,
        },
        "top_picks": enriched
    }


def get_top_cached():
    if _cache_top["data"] and (now_ts() - _cache_top["ts"] <= CACHE_TTL):
        return _cache_top["data"]
    out = {
        "ts": now_ts(),
        "source": "binance_multi",
        "market_mode": "UNKNOWN",
        "market_gate": "UNKNOWN",
        "btc_24h": 0.0,
        "eth_24h": 0.0,
        "median20_24h": 0.0,
        "index": 0.0,
        "filters": {},
        "top_picks": [],
        "warnings": [],
    }
    try:
        out = build_top_picks()
        out["warnings"] = []
    except Exception as e:
        out["warnings"] = [f"Top build failed: {repr(e)}"]
    _cache_top["ts"] = now_ts()
    _cache_top["data"] = out
    return out


# =========================
# WHALE V2 (pressure + events)
# =========================
def whale_v2():
    if _cache_whales["data"] and (now_ts() - _cache_whales["ts"] <= WHALE_TTL):
        return _cache_whales["data"]

    top = get_top_cached()
    picks = top.get("top_picks", [])[:min(6, len(top.get("top_picks", [])))]

    events = []
    pressure = []  # per symbol buy/sell notional

    for p in picks:
        pair = p["pair"]
        sym = p["symbol"]
        try:
            trades = fetch_binance_agg_trades(pair, limit=WHALE_LOOKBACK_TRADES)
            buy_notional = 0.0
            sell_notional = 0.0
            whales = 0
            for tr in trades:
                price = safe_float(tr.get("p"))
                qty = safe_float(tr.get("q"))
                notional = price * qty
                is_buyer_maker = bool(tr.get("m", False))  # True => sell aggressor
                if is_buyer_maker:
                    sell_notional += notional
                else:
                    buy_notional += notional

                if notional >= WHALE_THRESHOLD_USD:
                    whales += 1
                    ts_ms = int(tr.get("T", 0))
                    dt = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).astimezone()
                    side = "SELL" if is_buyer_maker else "BUY"
                    events.append({
                        "symbol": sym,
                        "pair": pair,
                        "side": side,
                        "usd": round(notional, 2),
                        "price": round(price, 8),
                        "qty": round(qty, 6),
                        "time": dt.isoformat(timespec="seconds"),
                    })

            total = buy_notional + sell_notional
            pressure_idx = 0.0 if total <= 0 else ((buy_notional - sell_notional) / total) * 100.0
            pressure.append({
                "symbol": sym,
                "pair": pair,
                "buy_usd": int(buy_notional),
                "sell_usd": int(sell_notional),
                "pressure_idx": round(pressure_idx, 1),
                "whale_hits": whales
            })
        except Exception as e:
            pressure.append({"symbol": sym, "pair": pair, "error": repr(e)[:220]})

    # sort events big first
    events.sort(key=lambda x: float(x.get("usd", 0.0)), reverse=True)
    pressure.sort(key=lambda x: float(x.get("pressure_idx", 0.0)), reverse=True)

    out = {
        "ts": now_ts(),
        "threshold_usd": int(WHALE_THRESHOLD_USD),
        "pressure": pressure,
        "events": events[:20]
    }

    # Telegram: only if there is a very big whale
    if events:
        big = events[0]
        msg = f"🐋 Whale: {big['pair']} {big['side']} ~${fmt_usd(big['usd'])} @ {big['price']}"
        send_telegram(msg)

    _cache_whales["ts"] = now_ts()
    _cache_whales["data"] = out
    return out


# =========================
# SCALP ENGINE
# =========================
def scalp_engine():
    if _cache_scalp["data"] and (now_ts() - _cache_scalp["ts"] <= SCALP_TTL):
        return _cache_scalp["data"]

    top = get_top_cached()
    gate = top.get("market_gate", "NEUTRAL")
    picks = top.get("top_picks", [])

    opportunities = []
    for p in picks:
        sym = p["symbol"]
        ind4h = p.get("indicators", {}).get("4h") or {}
        ind1h = p.get("indicators", {}).get("1h") or {}
        price = safe_float(p.get("price"))

        atr = safe_float(ind4h.get("atr"))
        atr_pct = safe_float(ind4h.get("atr_pct"))

        # Basic scalp filters
        if gate in ("PANIC",):
            continue
        if p.get("ai_signal") == "AVOID":
            continue
        if atr <= 0 or price <= 0:
            continue

        # Target based on ATR, but clamp to user desired 2-3%
        tp_atr = (SCALP_TAKE_ATR_MULT * atr) / price
        tp_pct = clamp(tp_atr, SCALP_TARGET_MIN, SCALP_TARGET_MAX)

        sl_atr = (SCALP_STOP_ATR_MULT * atr) / price
        sl_pct = clamp(sl_atr, 0.008, 0.02)  # 0.8%..2%

        # Favor not overheated RSI
        rsi1h = ind1h.get("rsi")
        if rsi1h is not None and rsi1h > 75:
            continue

        entry = price
        stop = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)

        rr = (tp_pct / max(sl_pct, 1e-6))

        opportunities.append({
            "symbol": sym,
            "pair": p["pair"],
            "entry": round(entry, 8),
            "stop": round(stop, 8),
            "take": round(tp, 8),
            "tp_pct": round(tp_pct * 100, 2),
            "sl_pct": round(sl_pct * 100, 2),
            "rr": round(rr, 2),
            "rsi_1h": rsi1h,
            "atr_pct_4h": atr_pct,
            "ai_signal": p.get("ai_signal"),
            "score": p.get("score")
        })

    opportunities.sort(key=lambda x: (x["rr"], x["score"]), reverse=True)
    opportunities = opportunities[:12]

    out = {"ts": now_ts(), "gate": gate, "opportunities": opportunities}

    # Telegram: only if there is a very good RR opportunity
    if opportunities:
        best = opportunities[0]
        if best["rr"] >= 1.6:
            msg = f"⚡ Scalp: {best['pair']} Entry {best['entry']} SL {best['stop']} TP {best['take']} (RR {best['rr']})"
            send_telegram(msg)

    _cache_scalp["ts"] = now_ts()
    _cache_scalp["data"] = out
    return out


# =========================
# METALS/FX (placeholder hook)
# =========================
def metals_fx():
    """
    Bankaların mobil alış/satışlarını otomatik çekmek:
    - resmi API yok
    - ekran scraping (legal/fragile) yapmıyoruz
    Bu endpoint: ileride API key ile metal/FX provider bağlayacağız.
    """
    return {
        "ts": now_ts(),
        "status": "NOT_CONFIGURED",
        "note": "Bankaların alış/satış fiyatı otomatik çekimi için resmi API gerekir. İstersen GoldAPI/Metals-API gibi provider bağlarız veya manuel input ekranı ekleriz.",
        "providers": ["GoldAPI", "Metals-API", "ExchangeRate API (FX)"],
        "your_banks": ["Garanti", "Vakıfbank", "Ziraat", "Fiba", "Anadolu"]
    }


# =========================
# API
# =========================
@app.get("/api/top", response_class=JSONResponse)
def api_top():
    return get_top_cached()

@app.get("/api/whales", response_class=JSONResponse)
def api_whales():
    return whale_v2()

@app.get("/api/scalp", response_class=JSONResponse)
def api_scalp():
    return scalp_engine()

@app.get("/api/metals", response_class=JSONResponse)
def api_metals():
    return metals_fx()

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Trade Radar (MVP+) — Yiğit Mode</title>
  <style>
    body{font-family:Arial;margin:18px;max-width:1050px}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
    .pill{border:1px solid #ddd;border-radius:999px;padding:6px 10px;font-size:12px;background:#fafafa}
    .card{border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0}
    .muted{color:#666}
    button{padding:8px 12px;border-radius:10px;border:1px solid #ddd;background:#fff}
    .warn{color:#b00020}
    .good{color:#0a7}
    .bad{color:#c00}
    small{color:#666}
    .grid{display:grid;grid-template-columns:1fr;gap:10px}
    @media(min-width:860px){.grid{grid-template-columns:1fr 1fr}}
    pre{white-space:pre-wrap}
  </style>
</head>
<body>
  <h2>Trade Radar (MVP+) — Yiğit Mode</h2>

  <div class="row">
    <span class="pill">Market Mode: <b id="mode">...</b></span>
    <span class="pill">BTC 24h: <b id="btc">...</b></span>
    <span class="pill">ETH 24h: <b id="eth">...</b></span>
    <span class="pill">Index: <b id="idx">...</b></span>
    <span class="pill" id="flt">...</span>
    <span class="pill">Source: <b id="src">...</b></span>
    <button onclick="reloadAll()">Yenile</button>
  </div>

  <div id="warn" class="warn"></div>

  <h3>🔥 Top 10 “Tradeable” Picks</h3>
  <div class="muted">Not: Bu bir yatırım tavsiyesi değil. Sistem skor + sinyal + şablon üretir.</div>
  <div id="picks" class="grid"></div>

  <h3>⚡ Scalp Opportunities</h3>
  <div class="muted">2–3% hedefli hızlı fırsat listesi (RR + filtreler).</div>
  <div id="scalp" class="grid"></div>

  <h3>🐋 Whale v2</h3>
  <div class="muted">Pressure index: (BUY - SELL) / total * 100</div>
  <div id="pressure" class="grid"></div>
  <div id="whales"></div>

  <h3>🥇 Metals/FX</h3>
  <div class="muted">Bankalardan otomatik fiyat için resmi API gerekir (hook hazır).</div>
  <div id="metals"></div>

<script>
function clsMode(m){
  if(m==="BULLISH"||m==="STRONG BULLISH") return "good";
  if(m==="BEARISH"||m==="PANIC") return "bad";
  return "";
}
function fmtUSD(x){
  const n = Number(x||0);
  if(n>=1e9) return (n/1e9).toFixed(2)+"B";
  if(n>=1e6) return (n/1e6).toFixed(2)+"M";
  if(n>=1e3) return (n/1e3).toFixed(2)+"K";
  return n.toFixed(0);
}
function pill(text){ return `<span class="pill">${text}</span>`; }

async function reloadAll(){
  document.getElementById("warn").innerText="";
  document.getElementById("picks").innerHTML="";
  document.getElementById("scalp").innerHTML="";
  document.getElementById("pressure").innerHTML="";
  document.getElementById("whales").innerHTML="";
  document.getElementById("metals").innerHTML="";

  try{
    const r = await fetch("/api/top?ts="+Date.now());
    const top = await r.json();

    document.getElementById("mode").innerText = top.market_mode || "UNKNOWN";
    document.getElementById("mode").className = clsMode(top.market_mode || "");
    document.getElementById("btc").innerText = (top.btc_24h ?? 0).toFixed(2)+"%";
    document.getElementById("eth").innerText = (top.eth_24h ?? 0).toFixed(2)+"%";
    document.getElementById("idx").innerText = (top.index ?? 0).toFixed(2);
    document.getElementById("src").innerText = top.source || "unknown";

    const f = top.filters || {};
    document.getElementById("flt").innerText =
      `VolMin=${fmtUSD(f.vol_min_usd)} | 24h%=${f.pct_min}-${f.pct_max} | Whale=${fmtUSD(f.whale_threshold_usd)}`;

    if(top.warnings && top.warnings.length){
      document.getElementById("warn").innerText = "Warning: " + top.warnings.join(" | ");
    }

    const picks = top.top_picks || [];
    const box = document.getElementById("picks");
    if(!picks.length){
      box.innerHTML = `<div class="card muted">Top 10 boş geldi. Filtreleri gevşetebilirsin (VOL_MIN_USD, PCT_MIN).</div>`;
    } else {
      picks.forEach(p=>{
        const ind = p.indicators || {};
        const i1h = ind["1h"]||{};
        const i4h = ind["4h"]||{};
        const i1d = ind["1d"]||{};
        const reasons = (p.ai_reasons||[]).slice(0,4).map(x=>"• "+x).join("<br>");

        const el = document.createElement("div");
        el.className="card";
        el.innerHTML = `
          <div class="row" style="justify-content:space-between">
            <div>
              <b style="font-size:18px">${p.symbol}</b> <small>(${p.pair})</small>
              ${pill("Score: <b>"+p.score+"</b>")}
              ${pill("AI: <b>"+(p.ai_signal||"WAIT")+"</b>")}
            </div>
          </div>
          <div class="muted">
            Price: <b>${p.price}</b> | 24h: <b>${(p.chg24_pct||0).toFixed(2)}%</b> | Vol24: <b>${fmtUSD(p.vol24_usd)}</b>
          </div>
          <div class="row">
            ${pill("RSI 1h: <b>"+(i1h.rsi ?? "-")+"</b>")}
            ${pill("RSI 4h: <b>"+(i4h.rsi ?? "-")+"</b>")}
            ${pill("ATR% 4h: <b>"+(i4h.atr_pct ?? "-")+"</b>")}
          </div>
          <div class="muted" style="margin-top:6px">${reasons || ""}</div>
          <div class="row" style="margin-top:8px">
            ${pill("Entry: <b>"+(p.plan?.entry ?? "-")+"</b>")}
            ${pill("Stop: <b>"+(p.plan?.stop ?? "-")+"</b>")}
            ${pill("TP1: <b>"+(p.plan?.tp1 ?? "-")+"</b>")}
            ${pill("TP2: <b>"+(p.plan?.tp2 ?? "-")+"</b>")}
          </div>
        `;
        box.appendChild(el);
      });
    }

    // Scalp
    const s = await (await fetch("/api/scalp?ts="+Date.now())).json();
    const sbox = document.getElementById("scalp");
    const opps = s.opportunities || [];
    if(!opps.length){
      sbox.innerHTML = `<div class="card muted">Şu an scalp fırsatı yok (veya filtreler sıkı).</div>`;
    } else {
      opps.forEach(o=>{
        const el = document.createElement("div");
        el.className="card";
        el.innerHTML = `
          <div class="row" style="justify-content:space-between">
            <div><b style="font-size:18px">${o.symbol}</b> <small>(${o.pair})</small></div>
            ${pill("RR: <b>"+o.rr+"</b>")}
          </div>
          <div class="muted">
            Entry <b>${o.entry}</b> | SL <b>${o.stop}</b> (${o.sl_pct}%) | TP <b>${o.take}</b> (${o.tp_pct}%)
          </div>
          <div class="row">
            ${pill("RSI 1h: <b>"+(o.rsi_1h ?? "-")+"</b>")}
            ${pill("ATR% 4h: <b>"+(o.atr_pct_4h ?? "-")+"</b>")}
            ${pill("AI: <b>"+(o.ai_signal ?? "-")+"</b>")}
          </div>
        `;
        sbox.appendChild(el);
      });
    }

    // Whales
    const w = await (await fetch("/api/whales?ts="+Date.now())).json();
    const pbox = document.getElementById("pressure");
    const pres = w.pressure || [];
    if(!pres.length){
      pbox.innerHTML = `<div class="card muted">Whale pressure verisi yok.</div>`;
    } else {
      pres.forEach(x=>{
        const el = document.createElement("div");
        el.className="card";
        if(x.error){
          el.innerHTML = `<b>${x.symbol}</b> <small>(${x.pair})</small><br><span class="warn">${x.error}</span>`;
        } else {
          el.innerHTML = `
            <div class="row" style="justify-content:space-between">
              <div><b>${x.symbol}</b> <small>(${x.pair})</small></div>
              ${pill("Pressure: <b>"+x.pressure_idx+"</b>")}
            </div>
            <div class="muted">BUY $${fmtUSD(x.buy_usd)} | SELL $${fmtUSD(x.sell_usd)} | Whale hits: ${x.whale_hits}</div>
          `;
        }
        pbox.appendChild(el);
      });
    }

    const ebox = document.getElementById("whales");
    const ev = w.events || [];
    if(ev.length){
      ebox.innerHTML = `<div class="card"><b>Son Whale İşlemleri</b><br>${ev.slice(0,10).map(x =>
        `${x.pair} <b>${x.side}</b> $${fmtUSD(x.usd)} @ ${x.price} <small>(${x.time})</small>`
      ).join("<br>")}</div>`;
    } else {
      ebox.innerHTML = `<div class="card muted">Threshold üstü whale yakalanmadı.</div>`;
    }

    // Metals/FX
    const m = await (await fetch("/api/metals")).json();
    document.getElementById("metals").innerHTML =
      `<div class="card"><b>Status:</b> ${m.status}<br><span class="muted">${m.note}</span></div>`;

  }catch(e){
    document.getElementById("warn").innerText = "UI Error: " + e.message;
  }
}
reloadAll();
setInterval(reloadAll, 30000);
</script>
</body>
</html>
    """

