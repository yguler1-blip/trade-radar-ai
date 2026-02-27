from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import os, time, math, statistics, hashlib
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

app = FastAPI(title="Trade Radar (Crypto + Silver) — Yiğit Mode")

# ============================================================
# Shared Helpers
# ============================================================
USER_AGENT = {"User-Agent": "trade-radar-yigit-mode"}

def now_ts() -> int:
    return int(time.time())

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def http_get_json(url: str, params=None, timeout=20, headers=None):
    h = dict(USER_AGENT)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.json()

def http_get_text(url: str, params=None, timeout=20, headers=None):
    h = dict(USER_AGENT)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, timeout=timeout, headers=h)
    r.raise_for_status()
    return r.text


# ============================================================
# SIMPLE TTL CACHE
# ============================================================
class TTLCache:
    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str):
        o = self.store.get(key)
        if not o:
            return None
        if now_ts() - o["ts"] > o["ttl"]:
            return None
        return o["val"]

    def set(self, key: str, val: Any, ttl: int):
        self.store[key] = {"ts": now_ts(), "ttl": ttl, "val": val}

CACHE = TTLCache()


# ============================================================
# ============================================================
# CRYPTO RADAR (Binance mirror + Top10 + Scalp + Whale)
# ============================================================
# ============================================================
CRYPTO_CACHE_TTL = int(os.getenv("CRYPTO_CACHE_TTL", "30"))
KLINES_TTL = int(os.getenv("KLINES_TTL", "90"))
SCALP_TTL = int(os.getenv("SCALP_TTL", "30"))
WHALE_TTL = int(os.getenv("WHALE_TTL", "20"))

VOL_MIN_USD = int(os.getenv("VOL_MIN_USD", "60000000"))
PCT_MIN = float(os.getenv("PCT_MIN", "2.0"))
PCT_MAX = float(os.getenv("PCT_MAX", "25.0"))
TOP_N = int(os.getenv("TOP_N", "10"))

SCALP_TARGET_MIN = float(os.getenv("SCALP_TARGET_MIN", "0.02"))
SCALP_TARGET_MAX = float(os.getenv("SCALP_TARGET_MAX", "0.03"))
SCALP_STOP_ATR_MULT = float(os.getenv("SCALP_STOP_ATR_MULT", "1.2"))
SCALP_TAKE_ATR_MULT = float(os.getenv("SCALP_TAKE_ATR_MULT", "1.8"))

WHALE_THRESHOLD_USD = float(os.getenv("WHALE_THRESHOLD_USD", "750000"))
WHALE_LOOKBACK_TRADES = int(os.getenv("WHALE_LOOKBACK_TRADES", "80"))

TG_ENABLED = os.getenv("TELEGRAM_ENABLED", "0") == "1"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BINANCE_ENDPOINTS = [
    os.getenv("BINANCE_BASE", "").strip(),
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
]
BINANCE_ENDPOINTS = [x for x in BINANCE_ENDPOINTS if x]

STABLE_SKIP = {
    "USDT","USDC","BUSD","TUSD","FDUSD","DAI","EUR","TRY","BRL","GBP","AUD","RUB","UAH"
}
BAD_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

_cache_top = {"ts": 0, "data": None}
_cache_klines: Dict[Tuple[str,str], Dict[str,Any]] = {}
_cache_scalp = {"ts": 0, "data": None}
_cache_whales = {"ts": 0, "data": None}
_alert_dedup = {"last_hash": ""}

def send_telegram(text: str):
    if not (TG_ENABLED and TG_TOKEN and TG_CHAT_ID):
        return
    h = sha1(text)
    if _alert_dedup.get("last_hash") == h:
        return
    _alert_dedup["last_hash"] = h
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

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

def score_coin(p24, vol24_usd, spread_hint=0.002, gate="NEUTRAL"):
    p24c = clamp(p24, -18.0, 22.0)
    momentum = clamp((p24c + 18.0) / 40.0 * 100.0, 0, 100)
    v = clamp((math.log10(max(vol24_usd, 1.0)) - 7.5) / (10.0 - 7.5) * 100.0, 0, 100)
    s = clamp((0.010 - spread_hint) / (0.010 - 0.001) * 100.0, 0, 100)
    base = 0.34 * momentum + 0.52 * v + 0.14 * s
    if gate in ("BEARISH", "PANIC"):
        base -= 6.0
        if p24 > 8:
            base -= 4.0
    return round(clamp(base, 0, 100), 1)

def fetch_binance_24h_all():
    return http_get_json_with_fallback("/api/v3/ticker/24hr", timeout=25)

def fetch_binance_klines(symbol_pair: str, interval: str, limit: int = 200):
    params = {"symbol": symbol_pair, "interval": interval, "limit": limit}
    return http_get_json_with_fallback("/api/v3/klines", params=params, timeout=25)

def fetch_binance_agg_trades(symbol_pair: str, limit: int = WHALE_LOOKBACK_TRADES):
    params = {"symbol": symbol_pair, "limit": limit}
    return http_get_json_with_fallback("/api/v3/aggTrades", params=params, timeout=20)

def get_indicators(symbol: str, interval: str):
    key = (symbol, interval)
    hit = _cache_klines.get(key)
    if hit and (now_ts() - hit["ts"] <= KLINES_TTL):
        return hit["data"]

    pair = f"{symbol}USDT"
    kl = fetch_binance_klines(pair, interval, limit=200)
    closes, highs, lows = [], [], []
    for k in kl:
        highs.append(safe_float(k[2]))
        lows.append(safe_float(k[3]))
        closes.append(safe_float(k[4]))

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
        return "STRONG BULLISH", "BULLISH", round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)
    if idx > 0.4:
        return "BULLISH", "BULLISH", round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)
    if idx < -1.2:
        return "PANIC", "PANIC", round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)
    if idx < -0.4:
        return "BEARISH", "BEARISH", round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)
    return "NEUTRAL", "NEUTRAL", round(idx, 2), round(btc_pct, 2), round(eth_pct, 2), round(median20, 2)

def simple_ai_signal(ind1h, ind4h, ind1d, market_gate: str):
    reasons = []
    verdict = "WAIT"

    def trend_label(ind):
        if ind.get("ema20") is None or ind.get("ema50") is None:
            return None
        return "UP" if ind["ema20"] > ind["ema50"] else "DOWN"

    t1h = trend_label(ind1h)
    t4h = trend_label(ind4h)
    t1d = trend_label(ind1d)

    rsi4h = ind4h.get("rsi")

    if market_gate == "PANIC":
        return "AVOID", ["Market PANIC: risk yüksek"]

    up_votes = sum([1 for t in (t1h, t4h, t1d) if t == "UP"])
    down_votes = sum([1 for t in (t1h, t4h, t1d) if t == "DOWN"])

    if up_votes >= 2:
        reasons.append("Trend: çoklu timeframe UP")
    if down_votes >= 2:
        reasons.append("Trend: çoklu timeframe DOWN")

    if rsi4h is not None:
        if rsi4h > 72:
            reasons.append("RSI(4h) yüksek (ısınmış)")
        elif rsi4h < 35:
            reasons.append("RSI(4h) düşük (bounce ihtimali)")
        else:
            reasons.append("RSI(4h) dengeli")

    if down_votes >= 2:
        verdict = "AVOID"
    elif up_votes >= 2:
        verdict = "WAIT" if (rsi4h is not None and rsi4h > 72) else "BUY"
    else:
        verdict = "WAIT"

    if market_gate == "BEARISH" and verdict == "BUY":
        reasons.append("Market BEARISH: küçük pozisyon / temkin")
        verdict = "WAIT"

    return verdict, reasons[:6]

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
        qv = safe_float(t.get("quoteVolume"))
        if last <= 0 or qv <= 0:
            continue

        rows_all.append({"symbol": base, "pair": sym, "price": last, "chg24_pct": p24, "vol24_usd": qv})

    rows_by_vol = sorted(rows_all, key=lambda r: r["vol24_usd"], reverse=True)
    market_mode, gate, idx, btc24, eth24, median20 = compute_market_mode_from_rows(rows_by_vol)

    picks = []
    for r in rows_all:
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
        enriched.append({**p, "market_gate": gate, "indicators": {"1h": ind1h, "4h": ind4h, "1d": ind1d},
                         "ai_signal": verdict, "ai_reasons": reasons, "plan": plan})

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
        "top_picks": enriched,
        "warnings": []
    }

def get_crypto_top():
    if _cache_top["data"] and (now_ts() - _cache_top["ts"] <= CRYPTO_CACHE_TTL):
        return _cache_top["data"]
    try:
        out = build_top_picks()
    except Exception as e:
        out = {"ts": now_ts(), "source": "binance_multi", "market_mode": "UNKNOWN", "market_gate": "UNKNOWN",
               "btc_24h": 0.0, "eth_24h": 0.0, "median20_24h": 0.0, "index": 0.0,
               "filters": {}, "top_picks": [], "warnings": [f"Top build failed: {repr(e)}"]}
    _cache_top["ts"] = now_ts()
    _cache_top["data"] = out
    return out

def whale_v2():
    if _cache_whales["data"] and (now_ts() - _cache_whales["ts"] <= WHALE_TTL):
        return _cache_whales["data"]

    top = get_crypto_top()
    picks = top.get("top_picks", [])[:min(6, len(top.get("top_picks", [])))]

    events = []
    pressure = []

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

    events.sort(key=lambda x: float(x.get("usd", 0.0)), reverse=True)
    pressure.sort(key=lambda x: float(x.get("pressure_idx", 0.0)), reverse=True)

    out = {"ts": now_ts(), "threshold_usd": int(WHALE_THRESHOLD_USD), "pressure": pressure, "events": events[:20]}

    if events:
        big = events[0]
        send_telegram(f"🐋 Whale: {big['pair']} {big['side']} ~${big['usd']:.0f} @ {big['price']}")

    _cache_whales["ts"] = now_ts()
    _cache_whales["data"] = out
    return out

def scalp_engine():
    if _cache_scalp["data"] and (now_ts() - _cache_scalp["ts"] <= SCALP_TTL):
        return _cache_scalp["data"]

    top = get_crypto_top()
    gate = top.get("market_gate", "NEUTRAL")
    picks = top.get("top_picks", [])

    opportunities = []
    for p in picks:
        sym = p["symbol"]
        ind4h = p.get("indicators", {}).get("4h") or {}
        ind1h = p.get("indicators", {}).get("1h") or {}
        price = safe_float(p.get("price"))

        atrv = safe_float(ind4h.get("atr"))
        atr_pct = safe_float(ind4h.get("atr_pct"))

        if gate in ("PANIC",):
            continue
        if p.get("ai_signal") == "AVOID":
            continue
        if atrv <= 0 or price <= 0:
            continue

        tp_atr = (SCALP_TAKE_ATR_MULT * atrv) / price
        tp_pct = clamp(tp_atr, SCALP_TARGET_MIN, SCALP_TARGET_MAX)

        sl_atr = (SCALP_STOP_ATR_MULT * atrv) / price
        sl_pct = clamp(sl_atr, 0.008, 0.02)

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

    if opportunities:
        best = opportunities[0]
        if best["rr"] >= 1.6:
            send_telegram(f"⚡ Scalp: {best['pair']} Entry {best['entry']} SL {best['stop']} TP {best['take']} (RR {best['rr']})")

    _cache_scalp["ts"] = now_ts()
    _cache_scalp["data"] = out
    return out


# ============================================================
# ============================================================
# SILVER RADAR (XAGUSD + USDTRY + scenario + anomaly + news)
# ============================================================
# ============================================================
CACHE_TTL_PRICE = int(os.getenv("CACHE_TTL_PRICE", "25"))
CACHE_TTL_FX    = int(os.getenv("CACHE_TTL_FX", "60"))
CACHE_TTL_NEWS  = int(os.getenv("CACHE_TTL_NEWS", "600"))
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "480"))
POLL_MIN_SECONDS   = int(os.getenv("POLL_MIN_SECONDS", "10"))

METALS_API_KEY = os.getenv("METALS_API_KEY", "").strip()

HISTORY: List[Tuple[int, float, float, float]] = []
_last_poll_ts = 0
BANK_QUOTE = {"ts": 0, "bank": "FIBABANKA", "bid": None, "ask": None}

SENTIMENT_KEYWORDS_POS = [
    "rate cut", "cuts rates", "dovish", "ceasefire", "deal", "agreement", "stimulus",
    "risk-off", "safe haven", "inflation rising"
]
SENTIMENT_KEYWORDS_NEG = [
    "rate hike", "hawkish", "strong dollar", "yields rise", "bond yields surge",
    "liquidation", "margin call", "risk-on", "equities rally"
]

def fetch_usdtry() -> float:
    cache_key = "fx_usdtry"
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        j = http_get_json("https://api.exchangerate.host/latest", params={"base":"USD","symbols":"TRY"}, timeout=10)
        rate = safe_float((j.get("rates") or {}).get("TRY"))
        if rate > 0:
            CACHE.set(cache_key, rate, CACHE_TTL_FX)
            return rate
    except Exception:
        pass

    try:
        j = http_get_json("https://open.er-api.com/v6/latest/USD", timeout=10)
        rate = safe_float((j.get("rates") or {}).get("TRY"))
        if rate > 0:
            CACHE.set(cache_key, rate, CACHE_TTL_FX)
            return rate
    except Exception:
        pass

    raise RuntimeError("USDTRY fetch failed")

def fetch_xagusd() -> float:
    cache_key = "xag_usd"
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    if METALS_API_KEY:
        try:
            j = http_get_json(
                "https://metals-api.com/api/latest",
                params={"access_key": METALS_API_KEY, "base":"USD", "symbols":"XAG"},
                timeout=12,
            )
            rate = safe_float((j.get("rates") or {}).get("XAG"))
            if rate > 0:
                xag_usd = 1.0 / rate
                if xag_usd > 0:
                    CACHE.set(cache_key, xag_usd, CACHE_TTL_PRICE)
                    return xag_usd
        except Exception:
            pass

    # Yahoo SI=F proxy
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/SI=F"
        j = http_get_json(url, params={"interval":"1m", "range":"1d"}, timeout=12)
        res = (((j.get("chart") or {}).get("result") or [None])[0]) or {}
        meta = res.get("meta") or {}
        price = safe_float(meta.get("regularMarketPrice"))
        if price > 0:
            CACHE.set(cache_key, price, CACHE_TTL_PRICE)
            return price
    except Exception:
        pass

    raise RuntimeError("XAGUSD fetch failed (set METALS_API_KEY for stability)")

def theoretical_gram_try(xag_usd: float, usdtry: float) -> float:
    return (xag_usd * usdtry) / 31.1034768

def fetch_silver_headlines() -> Dict[str, Any]:
    cache_key = "news_silver"
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    query = "silver price OR XAGUSD OR COMEX silver OR gold silver ratio"
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl":"en-US", "gl":"US", "ceid":"US:en"}
    try:
        xml = http_get_text(url, params=params, timeout=12)
        titles = []
        parts = xml.split("<title>")
        for p in parts[2:30]:
            t = p.split("</title>")[0].strip()
            if t and "Google News" not in t:
                titles.append(t)
        payload = {"ts": now_ts(), "titles": titles[:20]}
        CACHE.set(cache_key, payload, CACHE_TTL_NEWS)
        return payload
    except Exception as e:
        payload = {"ts": now_ts(), "titles": [], "error": repr(e)}
        CACHE.set(cache_key, payload, CACHE_TTL_NEWS)
        return payload

def sentiment_score_from_titles(titles: List[str]) -> float:
    if not titles:
        return 0.0
    text = " | ".join([t.lower() for t in titles])
    pos = sum(1 for k in SENTIMENT_KEYWORDS_POS if k in text)
    neg = sum(1 for k in SENTIMENT_KEYWORDS_NEG if k in text)
    raw = pos - neg
    return clamp(raw / 8.0, -1.0, 1.0)

def compute_returns(series: List[float]) -> List[float]:
    rets = []
    for i in range(1, len(series)):
        a, b = series[i-1], series[i]
        if a > 0 and b > 0:
            rets.append((b / a) - 1.0)
    return rets

def ema_list(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi_list(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    gains = gains[-period:]
    losses = losses[-period:]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr_proxy(rets: List[float], window: int = 30) -> float:
    if len(rets) < 5:
        return 0.0
    w = rets[-window:] if len(rets) >= window else rets
    return sum(abs(x) for x in w) / len(w)

def market_mode_from_score(score: float) -> str:
    if score >= 70:
        return "BULLISH"
    if score <= 35:
        return "BEARISH"
    return "NEUTRAL"

def build_trade_plan(price: float, vol: float, mode: str) -> Dict[str, Any]:
    vol_pct = clamp(vol * 100.0, 0.05, 1.50)
    if mode == "BULLISH":
        stop_pct = clamp(1.2 * vol_pct, 0.30, 2.20)
        tp1_pct  = clamp(2.0 * vol_pct, 0.60, 3.50)
        tp2_pct  = clamp(3.2 * vol_pct, 1.00, 5.50)
    elif mode == "BEARISH":
        stop_pct = clamp(1.4 * vol_pct, 0.40, 2.60)
        tp1_pct  = clamp(1.8 * vol_pct, 0.50, 3.20)
        tp2_pct  = clamp(2.8 * vol_pct, 0.90, 5.00)
    else:
        stop_pct = clamp(1.3 * vol_pct, 0.35, 2.40)
        tp1_pct  = clamp(1.9 * vol_pct, 0.55, 3.30)
        tp2_pct  = clamp(3.0 * vol_pct, 0.95, 5.20)
    entry = price
    return {
        "entry": round(entry, 4),
        "stop": round(price * (1.0 - stop_pct/100.0), 4),
        "tp1": round(price * (1.0 + tp1_pct/100.0), 4),
        "tp2": round(price * (1.0 + tp2_pct/100.0), 4),
        "stop_pct": round(stop_pct, 2),
        "tp1_pct": round(tp1_pct, 2),
        "tp2_pct": round(tp2_pct, 2),
    }

def scenario_minute_forecast(curr: float, rets: List[float], sentiment: float) -> Dict[str, Any]:
    if len(rets) < 10:
        sigma = 0.0005
    else:
        w = rets[-60:] if len(rets) >= 60 else rets
        sigma = statistics.pstdev(w) if len(w) > 3 else 0.0007
        sigma = clamp(sigma, 0.0002, 0.0060)
    drift = clamp(sentiment * 0.00015, -0.00025, 0.00025)
    horizon = 15
    path_base, path_hi, path_lo = [], [], []
    p = curr
    for _ in range(horizon):
        base = p * (1.0 + drift)
        hi = base * (1.0 + 0.9*sigma)
        lo = base * (1.0 - 0.9*sigma)
        path_base.append(round(base, 4))
        path_hi.append(round(hi, 4))
        path_lo.append(round(lo, 4))
        p = base
    return {"horizon_min": horizon, "sigma_est": round(sigma*100, 3), "drift_est": round(drift*100, 4),
            "base": path_base, "optimistic": path_hi, "pessimistic": path_lo}

def whale_like_alerts(history: List[Tuple[int,float,float,float]]) -> List[Dict[str, Any]]:
    alerts = []
    if len(history) < 6:
        return alerts
    ts, _, _, _ = history[-1]
    last = history[-6:]
    grams = [p[3] for p in last]
    xs = [p[1] for p in last]
    fxs = [p[2] for p in last]
    g_rets = compute_returns(grams)
    x_rets = compute_returns(xs)
    f_rets = compute_returns(fxs)

    def add(kind, msg, sev, data):
        alerts.append({"ts": ts, "kind": kind, "message": msg, "severity": sev, "data": data})

    g1 = g_rets[-1] if g_rets else 0.0
    x1 = x_rets[-1] if x_rets else 0.0
    f1 = f_rets[-1] if f_rets else 0.0

    if abs(g1) > 0.0045:
        add("GRAM_SHOCK", f"Gram gümüşte 1dk şok: {g1*100:.2f}%", 5, {"g1_pct": g1*100})
    elif abs(g1) > 0.0025:
        add("GRAM_SPIKE", f"Gram gümüşte hızlı hareket: {g1*100:.2f}%", 4, {"g1_pct": g1*100})

    if abs(x1) > 0.0035:
        add("XAG_SHOCK", f"XAGUSD 1dk şok: {x1*100:.2f}%", 4, {"x1_pct": x1*100})
    if abs(f1) > 0.0018:
        add("FX_SHOCK", f"USDTRY 1dk şok: {f1*100:.2f}%", 4, {"f1_pct": f1*100})

    if len(g_rets) >= 5:
        avg = sum(abs(r) for r in g_rets[:-1]) / max(1, len(g_rets[:-1]))
        if avg > 0 and abs(g1) > 3.0 * avg:
            add("ACCEL", "İvme arttı (1dk değişim, son ortalamanın >3x)", 3,
                {"g1_pct": g1*100, "avg_abs_pct": avg*100})
    return alerts[:6]

def compute_score(gram_series: List[float], usdtry_series: List[float], sentiment: float) -> Dict[str, Any]:
    if len(gram_series) < 25:
        return {"score": 50.0, "components": {"trend":0, "rsi":0, "vol":0, "fx":0, "sent":0}}

    ema20 = ema_list(gram_series[-120:] if len(gram_series) > 120 else gram_series, 20) or gram_series[-1]
    ema50 = ema_list(gram_series[-240:] if len(gram_series) > 240 else gram_series, 50) or gram_series[-1]
    r = rsi_list(gram_series[-60:] if len(gram_series) > 60 else gram_series, 14)
    r = r if r is not None else 50.0

    rets = compute_returns(gram_series[-180:] if len(gram_series) > 180 else gram_series)
    vol = atr_proxy(rets, 30)

    trend = 20.0 + (12.0 if ema20 > ema50 else -12.0)
    dist = (ema20/ema50 - 1.0) if ema50 > 0 else 0.0
    trend += clamp(dist*800.0, -8.0, 8.0)

    if r < 35: rsi_c = 8.0
    elif r < 45: rsi_c = 15.0
    elif r <= 60: rsi_c = 22.0
    elif r <= 70: rsi_c = 16.0
    else: rsi_c = 10.0

    vol_pct = vol*100
    if vol_pct < 0.08: vol_c = 10.0
    elif vol_pct < 0.35: vol_c = 18.0
    elif vol_pct < 0.70: vol_c = 12.0
    else: vol_c = 7.0

    fx_rets = compute_returns(usdtry_series[-30:] if len(usdtry_series) > 30 else usdtry_series)
    fx_vol = atr_proxy(fx_rets, 15)
    fx_c = 9.0 if fx_vol < 0.0009 else (6.0 if fx_vol < 0.0016 else 3.0)

    sent_c = sentiment * 5.0

    score = clamp(trend + rsi_c + vol_c + fx_c + sent_c, 0, 100)

    return {
        "score": round(score, 1),
        "components": {
            "trend": round(trend, 1),
            "rsi": round(rsi_c, 1),
            "vol": round(vol_c, 1),
            "fx": round(fx_c, 1),
            "sent": round(sent_c, 1),
            "ema20": round(ema20, 4),
            "ema50": round(ema50, 4),
            "rsi14": round(r, 2),
            "vol_min_abs_pct": round(vol_pct, 3),
        }
    }

def silver_update_state(force: bool = False) -> Dict[str, Any]:
    global _last_poll_ts, HISTORY
    t = now_ts()
    if not force and (t - _last_poll_ts) < POLL_MIN_SECONDS:
        snap = CACHE.get("silver_snapshot")
        if snap:
            return snap

    _last_poll_ts = t
    xag_usd = fetch_xagusd()
    usdtry = fetch_usdtry()
    gram_theo = theoretical_gram_try(xag_usd, usdtry)

    HISTORY.append((t, xag_usd, usdtry, gram_theo))
    if len(HISTORY) > HISTORY_MAX_POINTS:
        HISTORY = HISTORY[-HISTORY_MAX_POINTS:]

    news = fetch_silver_headlines()
    sentiment = sentiment_score_from_titles(news.get("titles") or [])

    grams = [p[3] for p in HISTORY]
    fxs = [p[2] for p in HISTORY]
    rets = compute_returns(grams)

    score_pack = compute_score(grams, fxs, sentiment)
    score = score_pack["score"]
    mode = market_mode_from_score(score)

    vol = atr_proxy(rets, 30)
    plan = build_trade_plan(gram_theo, vol, mode)
    forecast = scenario_minute_forecast(gram_theo, rets, sentiment)
    alerts = whale_like_alerts(HISTORY)

    bank_bid = BANK_QUOTE.get("bid")
    bank_ask = BANK_QUOTE.get("ask")
    premium = None
    if bank_ask and gram_theo > 0:
        premium = (bank_ask / gram_theo) - 1.0

    snapshot = {
        "ts": t,
        "xag_usd": round(xag_usd, 4),
        "usdtry": round(usdtry, 4),
        "gram_theoretical": round(gram_theo, 4),
        "score": score_pack,
        "market_mode": mode,
        "trade_plan": plan,
        "forecast": forecast,
        "news": {
            "ts": news.get("ts"),
            "sentiment": round(sentiment, 2),
            "titles": (news.get("titles") or [])[:8],
            "error": news.get("error")
        },
        "whale_like_alerts": alerts,
        "bank_quote": {
            "bank": BANK_QUOTE.get("bank"),
            "bid": bank_bid,
            "ask": bank_ask,
            "premium_vs_theoretical": round(premium*100, 2) if premium is not None else None
        }
    }

    CACHE.set("silver_snapshot", snapshot, 5)
    return snapshot


# ============================================================
# API ROUTES
# ============================================================
@app.get("/api/top", response_class=JSONResponse)
def api_crypto_top():
    return get_crypto_top()

@app.get("/api/whales", response_class=JSONResponse)
def api_crypto_whales():
    return whale_v2()

@app.get("/api/scalp", response_class=JSONResponse)
def api_crypto_scalp():
    return scalp_engine()

@app.get("/api/silver/state", response_class=JSONResponse)
def api_silver_state(force: int = 0):
    try:
        return silver_update_state(force=bool(force))
    except Exception as e:
        return {"ts": now_ts(), "error": repr(e), "hint": "METALS_API_KEY set etmek stabiliteyi artırır."}

@app.post("/api/bank_quote", response_class=JSONResponse)
def api_bank_quote(payload: Dict[str, Any]):
    BANK_QUOTE["bank"] = (payload.get("bank") or "FIBABANKA").strip().upper()
    BANK_QUOTE["bid"] = safe_float(payload.get("bid"), None) if payload.get("bid") is not None else None
    BANK_QUOTE["ask"] = safe_float(payload.get("ask"), None) if payload.get("ask") is not None else None
    BANK_QUOTE["ts"] = now_ts()
    return {"ok": True, "bank_quote": BANK_QUOTE}


# ============================================================
# UI PAGES
# ============================================================
@app.get("/", response_class=HTMLResponse)
def mode_select():
    # "Bana sor" ana sayfa
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Trade Radar — Mode Select</title>
  <style>
    body{font-family:Arial;margin:18px;max-width:900px}
    h2{margin:0 0 12px 0}
    .grid{display:grid;grid-template-columns:1fr;gap:12px}
    @media(min-width:720px){.grid{grid-template-columns:1fr 1fr}}
    .card{border:1px solid #e5e5e5;border-radius:18px;padding:18px;cursor:pointer;background:#fff}
    .card:hover{border-color:#bbb}
    .title{font-size:22px;font-weight:800}
    .muted{color:#666;margin-top:6px}
    .btn{display:inline-block;margin-top:14px;padding:10px 14px;border-radius:12px;border:1px solid #ddd;background:#fafafa}
  </style>
</head>
<body>
  <h2>Bugün hangi sayfayı açayım?</h2>
  <div class="grid">
    <div class="card" onclick="location.href='/crypto'">
      <div class="title">🚀 Crypto Radar</div>
      <div class="muted">Top 10 tradeable coin • Scalp fırsatları • Whale v2 • Market mode</div>
      <div class="btn">Crypto’ya git</div>
    </div>
    <div class="card" onclick="location.href='/silver'">
      <div class="title">🥈 Silver Radar</div>
      <div class="muted">XAGUSD + USDTRY → teorik gram • Senaryo bandı • Anomali “whale-like” • Haber sentiment</div>
      <div class="btn">Gümüş’e git</div>
    </div>
  </div>
</body>
</html>
    """.strip()

@app.get("/crypto", response_class=HTMLResponse)
def crypto_page():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Crypto Radar — Yiğit Mode</title>
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
    a{color:inherit}
  </style>
</head>
<body>
  <div class="row" style="justify-content:space-between">
    <h2 style="margin:0">🚀 Crypto Radar — Yiğit Mode</h2>
    <div class="row">
      <button onclick="location.href='/'">Mode seç</button>
      <button onclick="location.href='/silver'">🥈 Silver</button>
    </div>
  </div>

  <div class="row" id="pills">
    <span class="pill">Market Mode: <b id="mode">...</b></span>
    <span class="pill">BTC 24h: <b id="btc">...</b></span>
    <span class="pill">ETH 24h: <b id="eth">...</b></span>
    <span class="pill">Index: <b id="idx">...</b></span>
    <span class="pill" id="flt">...</span>
    <button onclick="reloadAll()">Yenile</button>
  </div>

  <div id="warn" class="warn"></div>

  <h3>🔥 Top 10 “Tradeable” Picks</h3>
  <div class="muted">Not: Bu bir yatırım tavsiyesi değildir. Sistem skor + risk şablonu üretir.</div>
  <div id="picks" class="grid"></div>

  <h3>⚡ Scalp Opportunities</h3>
  <div class="muted">2–3% hedefli hızlı fırsat listesi (RR + filtreler).</div>
  <div id="scalp" class="grid"></div>

  <h3>🐋 Whale v2</h3>
  <div class="muted">Pressure index: (BUY - SELL) / total * 100</div>
  <div id="pressure" class="grid"></div>
  <div id="whales"></div>

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

  try{
    const top = await (await fetch("/api/top?ts="+Date.now())).json();

    document.getElementById("mode").innerText = top.market_mode || "UNKNOWN";
    document.getElementById("mode").className = clsMode(top.market_mode || "");
    document.getElementById("btc").innerText = (top.btc_24h ?? 0).toFixed(2)+"%";
    document.getElementById("eth").innerText = (top.eth_24h ?? 0).toFixed(2)+"%";
    document.getElementById("idx").innerText = (top.index ?? 0).toFixed(2);

    const f = top.filters || {};
    document.getElementById("flt").innerText =
      `VolMin=${fmtUSD(f.vol_min_usd)} | 24h%=${f.pct_min}-${f.pct_max} | Whale=${fmtUSD(f.whale_threshold_usd)}`;

    if(top.warnings && top.warnings.length){
      document.getElementById("warn").innerText = "Warning: " + top.warnings.join(" | ");
    }

    const picks = top.top_picks || [];
    const box = document.getElementById("picks");
    if(!picks.length){
      box.innerHTML = `<div class="card muted">Top 10 boş geldi. Filtreleri gevşetebilirsin.</div>`;
    }else{
      picks.forEach(p=>{
        const ind = p.indicators || {};
        const i1h = ind["1h"]||{};
        const i4h = ind["4h"]||{};
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

    const s = await (await fetch("/api/scalp?ts="+Date.now())).json();
    const sbox = document.getElementById("scalp");
    const opps = s.opportunities || [];
    if(!opps.length){
      sbox.innerHTML = `<div class="card muted">Şu an scalp fırsatı yok (veya filtreler sıkı).</div>`;
    }else{
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

    const w = await (await fetch("/api/whales?ts="+Date.now())).json();
    const pbox = document.getElementById("pressure");
    const pres = w.pressure || [];
    if(!pres.length){
      pbox.innerHTML = `<div class="card muted">Whale pressure verisi yok.</div>`;
    }else{
      pres.forEach(x=>{
        const el = document.createElement("div");
        el.className="card";
        if(x.error){
          el.innerHTML = `<b>${x.symbol}</b> <small>(${x.pair})</small><br><span class="warn">${x.error}</span>`;
        }else{
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
    }else{
      ebox.innerHTML = `<div class="card muted">Threshold üstü whale yakalanmadı.</div>`;
    }

  }catch(e){
    document.getElementById("warn").innerText = "UI Error: " + e.message;
  }
}
reloadAll();
setInterval(reloadAll, 30000);
</script>
</body>
</html>
    """.strip()

@app.get("/silver", response_class=HTMLResponse)
def silver_page():
    return """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Silver Radar — Yiğit Mode</title>
  <style>
    body{font-family:Arial;margin:16px;max-width:980px}
    h2{margin:0 0 8px 0}
    .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
    .pill{padding:6px 10px;border:1px solid #ddd;border-radius:999px;font-size:13px;background:#fafafa}
    .btn{padding:8px 12px;border:1px solid #bbb;border-radius:10px;background:#fff;cursor:pointer}
    .card{border:1px solid #e5e5e5;border-radius:14px;padding:12px;margin:10px 0;background:#fff}
    .muted{color:#666}
    .kpi{font-size:20px;font-weight:700}
    .grid{display:grid;grid-template-columns:1fr;gap:10px}
    @media(min-width:820px){ .grid{grid-template-columns:1fr 1fr} }
    .warn{color:#b00020}
    input{padding:8px;border-radius:10px;border:1px solid #ccc;width:120px}
    .small{font-size:12px}
    .tag{display:inline-block;padding:3px 8px;border:1px solid #eee;border-radius:999px;margin-right:6px;color:#444;background:#fcfcfc}
    .sev{font-weight:700}
  </style>
</head>
<body>
  <div class="row" style="justify-content:space-between">
    <h2>🥈 Silver Radar — Yiğit Mode</h2>
    <div class="row">
      <button class="btn" onclick="location.href='/'">Mode seç</button>
      <button class="btn" onclick="location.href='/crypto'">🚀 Crypto</button>
    </div>
  </div>

  <div class="row" style="margin-bottom:10px">
    <div class="pill" id="mode">Market Mode: ...</div>
    <div class="pill" id="score">Score: ...</div>
    <div class="pill" id="gram">Teorik Gram: ...</div>
    <div class="pill" id="xag">XAGUSD: ...</div>
    <div class="pill" id="fx">USDTRY: ...</div>
    <button class="btn" onclick="loadState(1)">Yenile (force)</button>
    <button class="btn" onclick="loadState(0)">Yenile</button>
  </div>

  <div class="card">
    <b>Fiba (opsiyonel)</b> <span class="muted small">— yazarsan premium hesaplar, yazmazsan da sistem çalışır</span>
    <div class="row" style="margin-top:8px">
      <div><span class="muted small">Alış (bid)</span><br><input id="bid" placeholder="129.56" /></div>
      <div><span class="muted small">Satış (ask)</span><br><input id="ask" placeholder="135.91" /></div>
      <button class="btn" onclick="saveBank()">Kaydet</button>
      <div class="pill" id="premium">Premium: ...</div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <b>Trade Plan (Teorik Gram)</b>
      <div class="muted small">Bu bir yatırım tavsiyesi değildir. “Setup” mantığıyla risk şablonu verir.</div>
      <div style="margin-top:8px" id="plan">...</div>
    </div>

    <div class="card">
      <b>Dakika Senaryosu (15dk band)</b>
      <div class="muted small">Kesin tahmin değil; “baz / iyimser / kötümser” bandı.</div>
      <div style="margin-top:8px" id="forecast">...</div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <b>Anomali Alerts (“whale-like”)</b>
      <div class="muted small">Şok hareket / ivme / USDTRY şoku yakalanır.</div>
      <div style="margin-top:8px" id="alerts">...</div>
    </div>

    <div class="card">
      <b>Haber / Sentiment</b>
      <div class="muted small">RSS başlıklarından basit duygu skoru (cache’li).</div>
      <div style="margin-top:8px" id="news">...</div>
    </div>
  </div>

  <div class="card">
    <b>Debug</b>
    <div id="err" class="warn" style="margin-top:6px"></div>
    <pre id="raw" class="small" style="white-space:pre-wrap;background:#fafafa;border:1px solid #eee;border-radius:12px;padding:10px;overflow:auto"></pre>
  </div>

<script>
async function loadState(force){
  document.getElementById("err").innerText = "";
  try{
    const r = await fetch("/api/silver/state?force=" + (force?1:0) + "&ts=" + Date.now());
    const j = await r.json();
    if(j.error){
      document.getElementById("err").innerText = "Error: " + j.error + (j.hint ? (" | " + j.hint) : "");
      document.getElementById("raw").innerText = JSON.stringify(j, null, 2);
      return;
    }
    render(j);
    document.getElementById("raw").innerText = JSON.stringify(j, null, 2);
  }catch(e){
    document.getElementById("err").innerText = "UI error: " + e.message;
  }
}

function render(s){
  const mode = s.market_mode || "UNKNOWN";
  const score = (s.score && s.score.score!=null) ? s.score.score : 0;
  document.getElementById("mode").innerText = "Market Mode: " + mode;
  document.getElementById("score").innerText = "Score: " + score;
  document.getElementById("gram").innerText = "Teorik Gram: " + (s.gram_theoretical ?? "-");
  document.getElementById("xag").innerText  = "XAGUSD: " + (s.xag_usd ?? "-");
  document.getElementById("fx").innerText   = "USDTRY: " + (s.usdtry ?? "-");

  const prem = s.bank_quote && s.bank_quote.premium_vs_theoretical;
  document.getElementById("premium").innerText = "Premium: " + (prem==null ? "-" : (prem.toFixed(2) + "%"));

  const p = s.trade_plan || {};
  const c = (s.score && s.score.components) ? s.score.components : {};
  document.getElementById("plan").innerHTML = `
    <div class="kpi">Entry: ${p.entry ?? "-"} | Stop: ${p.stop ?? "-"} | TP1: ${p.tp1 ?? "-"} | TP2: ${p.tp2 ?? "-"}</div>
    <div class="muted small" style="margin-top:6px">
      Stop%: ${p.stop_pct ?? "-"} | TP1%: ${p.tp1_pct ?? "-"} | TP2%: ${p.tp2_pct ?? "-"}
    </div>
    <div style="margin-top:10px">
      <span class="tag">EMA20: ${c.ema20 ?? "-"}</span>
      <span class="tag">EMA50: ${c.ema50 ?? "-"}</span>
      <span class="tag">RSI14: ${c.rsi14 ?? "-"}</span>
      <span class="tag">Vol(min abs %): ${c.vol_min_abs_pct ?? "-"}</span>
      <span class="tag">Sent: ${c.sent ?? "-"}</span>
    </div>
  `;

  const f = s.forecast || {};
  const base = (f.base||[]).slice(0,8).join(", ");
  const hi = (f.optimistic||[]).slice(0,8).join(", ");
  const lo = (f.pessimistic||[]).slice(0,8).join(", ");
  document.getElementById("forecast").innerHTML = `
    <div class="muted small">Sigma est: ${f.sigma_est ?? "-"}% | Drift est: ${f.drift_est ?? "-"}% (dakika)</div>
    <div style="margin-top:8px"><b>Baz:</b> ${base}${(f.base||[]).length>8?" ...":""}</div>
    <div style="margin-top:4px"><b>İyimser:</b> ${hi}${(f.optimistic||[]).length>8?" ...":""}</div>
    <div style="margin-top:4px"><b>Kötümser:</b> ${lo}${(f.pessimistic||[]).length>8?" ...":""}</div>
  `;

  const a = s.whale_like_alerts || [];
  if(!a.length){
    document.getElementById("alerts").innerHTML = `<div class="muted">Şu an anomali yok.</div>`;
  }else{
    document.getElementById("alerts").innerHTML = a.map(x => `
      <div style="margin:6px 0">
        <span class="sev">Sev${x.severity}</span> — <b>${x.kind}</b>: ${x.message}
      </div>
    `).join("");
  }

  const n = s.news || {};
  const titles = n.titles || [];
  document.getElementById("news").innerHTML = `
    <div class="muted small">Sentiment: ${n.sentiment ?? 0} | ${n.error ? ("<span class='warn'>"+n.error+"</span>") : ""}</div>
    <div style="margin-top:8px">${titles.length ? titles.map(t=>`<div>• ${t}</div>`).join("") : "<div class='muted'>Başlık yok / rate-limit.</div>"}</div>
  `;
}

async function saveBank(){
  const bid = document.getElementById("bid").value;
  const ask = document.getElementById("ask").value;
  const payload = {bank:"FIBABANKA"};
  if(bid) payload.bid = Number(bid);
  if(ask) payload.ask = Number(ask);
  try{
    await fetch("/api/bank_quote", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(payload)});
    await loadState(1);
  }catch(e){
    document.getElementById("err").innerText = "Bank quote save error: " + e.message;
  }
}

loadState(0);
setInterval(()=>loadState(0), 30000);
</script>
</body>
</html>
    """.strip()
