from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import time, math, os, requests
from datetime import datetime, timezone

app = FastAPI(title="Trade Radar (MVP++) — Yiğit Mode")

# ----------------------------
# Global config / cache
# ----------------------------
CACHE_TTL = 35  # seconds
_cache = {
    "silver": {"ts": 0, "data": None},
    "crypto": {"ts": 0, "data": None},
}

OZ_TO_GRAM = 31.1034768

def now_ts() -> int:
    return int(time.time())

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def http_get_json(url, params=None, headers=None, timeout=20):
    r = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def parse_td_datetime_to_epoch_ms(dt_str: str) -> int:
    """
    TwelveData datetime usually like: '2026-02-28 10:15:00'
    Treat as UTC to keep charts consistent (OK for our purposes).
    """
    # robust parse
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

# ----------------------------
# Silver data (TwelveData)
# ----------------------------
def calc_theoretical_gram_try(xag_usd: float, usd_try: float) -> float:
    return (xag_usd * usd_try) / OZ_TO_GRAM

def fetch_twelvedata_series(symbol: str, interval="1min", outputsize=240):
    """
    Returns: (pts, err)
    pts: [{"t": epoch_ms, "v": float}, ...] ascending
    """
    api_key = os.getenv("TWELVEDATA_KEY", "").strip()
    if not api_key:
        return None, "TWELVEDATA_KEY yok (Render Env)."

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
        "format": "JSON",
    }
    try:
        data = http_get_json(url, params=params, timeout=25)
        if data.get("status") == "error":
            return None, data.get("message", "TwelveData error")

        values = data.get("values") or []
        if not values:
            return None, "TwelveData: boş seri"

        pts = []
        # newest-first -> reverse
        for row in reversed(values):
            dt = row.get("datetime")
            v = safe_float(row.get("close"))
            if not dt or v is None:
                continue
            try:
                pts.append({"t": parse_td_datetime_to_epoch_ms(dt), "v": float(v)})
            except Exception:
                continue

        if len(pts) < 8:
            return None, "TwelveData: yeterli veri yok"

        return pts, None
    except Exception as e:
        return None, f"TwelveData hata: {repr(e)}"

def build_silver_payload():
    c = _cache["silver"]
    if now_ts() - c["ts"] < CACHE_TTL and c["data"]:
        return c["data"]

    warnings = []

    xag_pts, xag_err = fetch_twelvedata_series("XAG/USD", interval="1min", outputsize=360)
    usd_pts, usd_err = fetch_twelvedata_series("USD/TRY", interval="1min", outputsize=360)

    if xag_err:
        warnings.append(f"XAG/USD: {xag_err}")
    if usd_err:
        warnings.append(f"USD/TRY: {usd_err}")

    gram_pts = []
    xag_last = usd_last = gram_last = None

    if xag_pts and usd_pts:
        # Build map for usd and do nearest match within 2 minutes
        usd_map = {p["t"]: p["v"] for p in usd_pts}
        usd_times = sorted(usd_map.keys())

        def nearest_usd(t_ms):
            # binary search nearest
            lo, hi = 0, len(usd_times) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if usd_times[mid] == t_ms:
                    return usd_map[t_ms]
                if usd_times[mid] < t_ms:
                    lo = mid + 1
                else:
                    hi = mid - 1
            cand = []
            if 0 <= hi < len(usd_times): cand.append(usd_times[hi])
            if 0 <= lo < len(usd_times): cand.append(usd_times[lo])
            if not cand:
                return None
            best_t = min(cand, key=lambda x: abs(x - t_ms))
            if abs(best_t - t_ms) <= 120_000:
                return usd_map[best_t]
            return None

        for p in xag_pts:
            t = p["t"]
            xag = p["v"]
            usd = nearest_usd(t)
            if usd is None:
                continue
            gram_pts.append({"t": t, "v": calc_theoretical_gram_try(xag, usd)})

        if gram_pts:
            xag_last = xag_pts[-1]["v"]
            usd_last = usd_pts[-1]["v"]
            gram_last = gram_pts[-1]["v"]

    payload = {
        "ts": now_ts(),
        "xag_usd_last": xag_last,
        "usd_try_last": usd_last,
        "gram_try_last": gram_last,
        "gram_series": gram_pts,   # [{"t":ms,"v":float}]
        "warnings": warnings
    }

    c["ts"] = now_ts()
    c["data"] = payload
    return payload

# ----------------------------
# Crypto (CryptoCompare)
# ----------------------------
def score_coin(p24, vol24_usdt, spread=0.002):
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

def build_crypto_payload():
    c = _cache["crypto"]
    if now_ts() - c["ts"] < CACHE_TTL and c["data"]:
        return c["data"]

    url = "https://min-api.cryptocompare.com/data/top/totalvolfull"
    params = {"limit": 80, "tsym": "USD"}
    headers = {"User-Agent": "trade-radar-mvp"}

    try:
        payload = http_get_json(url, params=params, headers=headers, timeout=25)
        data = payload.get("Data", []) or []
        if not data:
            out = {"ts": now_ts(), "market_mode": "UNKNOWN", "top_picks": [], "warning": "CryptoCompare boş data"}
            c["ts"] = now_ts(); c["data"] = out
            return out
    except Exception as e:
        out = {"ts": now_ts(), "market_mode": "UNKNOWN", "top_picks": [], "warning": f"CryptoCompare hata: {repr(e)}"}
        c["ts"] = now_ts(); c["data"] = out
        return out

    # simple filters (you can tune later)
    VOL_MIN = 60_000_000
    P24_MIN = 2
    P24_MAX = 25

    rows = []
    for item in data:
        coin_info = item.get("CoinInfo", {}) or {}
        raw = (item.get("RAW", {}) or {}).get("USD", {}) or {}

        symbol = coin_info.get("Name", "") or ""
        name = coin_info.get("FullName", "") or ""
        price = safe_float(raw.get("PRICE"))
        p24 = safe_float(raw.get("CHANGEPCT24HOUR"), 0.0) or 0.0
        vol24 = (safe_float(raw.get("TOTALVOLUME24H"), 0.0) or 0.0) * (price or 0.0)

        if not symbol or not price or price <= 0:
            continue
        if vol24 < VOL_MIN:
            continue
        if p24 < P24_MIN or p24 > P24_MAX:
            continue

        sc = score_coin(p24=p24, vol24_usdt=vol24, spread=0.002)
        rows.append({
            "symbol": symbol,
            "name": name,
            "price_usd": round(price, 6),
            "chg24_pct": round(p24, 2),
            "vol24_usd": int(vol24),
            "score": sc,
            "plan": build_trade_plan(price)
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    # market mode from BTC change (if present)
    btc = next((x for x in rows if x["symbol"] == "BTC"), None)
    mode = "NEUTRAL"
    btc_p24 = 0.0
    if btc:
        btc_p24 = btc["chg24_pct"]
        if btc_p24 > 1.0:
            mode = "RISK-ON"
        elif btc_p24 < -1.0:
            mode = "RISK-OFF"

    out = {
        "ts": now_ts(),
        "market_mode": mode,
        "btc_24h": btc_p24,
        "filters": {"vol_min_usd": VOL_MIN, "p24_min": P24_MIN, "p24_max": P24_MAX},
        "top_picks": rows[:10],
    }
    c["ts"] = now_ts()
    c["data"] = out
    return out

# ----------------------------
# API endpoints
# ----------------------------
@app.get("/api/silver", response_class=JSONResponse)
def api_silver():
    return build_silver_payload()

@app.get("/api/crypto", response_class=JSONResponse)
def api_crypto():
    return build_crypto_payload()

# ----------------------------
# UI pages
# ----------------------------
HOME_HTML = """
<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trade Radar — Yiğit Mode</title>
<style>
 body{font-family:Arial;margin:18px;max-width:980px}
 .card{border:1px solid #e6e6e6;border-radius:14px;padding:14px;margin:12px 0}
 .muted{color:#666}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #ddd;background:#fff;cursor:pointer}
 .btn:hover{border-color:#bbb}
 h2{margin:6px 0 10px}
</style>
</head><body>
<h2>Trade Radar (MVP++) — Yiğit Mode</h2>
<div class="muted">Ne açalım?</div>
<div class="row" style="margin-top:12px">
  <button class="btn" onclick="location.href='/silver'">🥈 Gümüş (Gram TRY)</button>
  <button class="btn" onclick="location.href='/crypto'">🪙 Kripto (Top 10 Picks)</button>
</div>
<div class="card">
  <b>Not</b>
  <div class="muted" style="margin-top:6px">
    Bu uygulama yatırım tavsiyesi vermez. Sinyal/puan/şablon üretir.
    TwelveData key yoksa “Teorik Gram” grafiği boş kalır; sayfa yine açılır.
  </div>
</div>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HOME_HTML

SILVER_HTML = """
<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Silver Radar — Yiğit Mode</title>
<style>
 body{font-family:Arial;margin:18px;max-width:1080px}
 .card{border:1px solid #e6e6e6;border-radius:14px;padding:14px;margin:12px 0}
 .muted{color:#666}
 .warn{color:#b00020}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #ddd;background:#fff;cursor:pointer}
 .btn:hover{border-color:#bbb}
 h2{margin:6px 0 10px}
 .grid{display:grid;grid-template-columns:1fr;gap:12px}
 @media(min-width:900px){ .grid{grid-template-columns:1fr 1fr} }
 .tvbox{height:420px}
 #chartBox{height:420px}
 input{padding:10px 12px;border:1px solid #ddd;border-radius:12px; width:160px}
 .kv{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
 .pill{border:1px solid #eee;border-radius:999px;padding:8px 12px}
</style>
<script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
</head><body>

<div class="row" style="align-items:center;justify-content:space-between">
  <h2>🥈 Silver Radar — Yiğit Mode</h2>
  <div class="row">
    <button class="btn" onclick="location.href='/'">Ana Menü</button>
    <button class="btn" onclick="loadSilver()">Yenile</button>
  </div>
</div>

<div id="status" class="muted">Loading...</div>
<div id="warn" class="warn" style="margin-top:6px"></div>

<div class="card">
  <b>FIBA (manuel)</b>
  <div class="muted" style="margin-top:6px">FIBA alış/satış fiyatını buraya yaz, sistem premium/makas hesabı yapsın.</div>
  <div class="kv">
    <div class="pill">Alış (TRY/gr): <input id="fibaBuy" placeholder="örn 121.011" /></div>
    <div class="pill">Satış (TRY/gr): <input id="fibaSell" placeholder="örn 125.500" /></div>
    <button class="btn" onclick="recalcPremium()">Hesapla</button>
  </div>
  <div class="kv" id="premiumOut" style="margin-top:10px"></div>
</div>

<div class="grid">
  <div class="card">
    <b>TradingView — Gram Gümüş (TRY)</b>
    <div class="muted" style="margin-top:6px">Sembol: FX_IDC:XAGTRYG</div>
    <div class="tvbox" id="tvGram"></div>
  </div>

  <div class="card">
    <b>TradingView — XAGUSD (Global)</b>
    <div class="muted" style="margin-top:6px">Sembol: FX_IDC:XAGUSD</div>
    <div class="tvbox" id="tvXag"></div>
  </div>
</div>

<div class="card">
  <b>Teorik Gram Gümüş (TRY) — Bizim Hesap</b>
  <div class="muted" style="margin-top:6px">
    Formül: (XAGUSD × USDTRY) / 31.1034768.
    TwelveData key yoksa çizim boş kalır.
  </div>
  <div id="chartBox"></div>
</div>

<script>
  function mountTV(containerId, symbol){
    const el = document.getElementById(containerId);
    el.innerHTML = '';
    const s = document.createElement('script');
    s.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    s.async = true;
    s.innerHTML = JSON.stringify({
      "autosize": true,
      "symbol": symbol,
      "interval": "5",
      "timezone": "Europe/Istanbul",
      "theme": "light",
      "style": "1",
      "locale": "tr",
      "hide_side_toolbar": false,
      "allow_symbol_change": false,
      "save_image": false,
      "calendar": false,
      "support_host": "https://www.tradingview.com"
    });
    el.appendChild(s);
  }

  let chart, series;
  function mountChart(){
    const box = document.getElementById('chartBox');
    box.innerHTML = '';
    chart = LightweightCharts.createChart(box, {
      width: box.clientWidth,
      height: 420,
      layout: { textColor: '#333', background: { type: 'solid', color: '#fff' } },
      timeScale: { timeVisible: true, secondsVisible: false }
    });
    series = chart.addLineSeries();
    window.addEventListener('resize', () => chart.applyOptions({ width: box.clientWidth }));
  }

  function toLWData(pts){
    return pts.map(p => ({ time: Math.floor(p.t/1000), value: p.v }));
  }

  let lastGram = null;

  async function loadSilver(){
    document.getElementById('warn').innerText = '';
    document.getElementById('status').innerText = 'Loading...';
    try{
      const r = await fetch('/api/silver');
      const j = await r.json();

      lastGram = j.gram_try_last;

      const gram = j.gram_try_last;
      const xag  = j.xag_usd_last;
      const usd  = j.usd_try_last;

      document.getElementById('status').innerText =
        `TS: ${j.ts} | Teorik Gram: ${gram ? gram.toFixed(4) : '-'} TRY | XAGUSD: ${xag ? xag.toFixed(4) : '-'} | USDTRY: ${usd ? usd.toFixed(4) : '-'}`;

      if (j.warnings && j.warnings.length){
        document.getElementById('warn').innerText = 'Warning: ' + j.warnings.join(' | ');
      }

      if (j.gram_series && j.gram_series.length){
        series.setData(toLWData(j.gram_series));
      }

      recalcPremium();
    }catch(e){
      document.getElementById('status').innerText = 'Market Mode: UNKNOWN';
      document.getElementById('warn').innerText = 'Error: ' + e.message;
    }
  }

  function recalcPremium(){
    const out = document.getElementById('premiumOut');
    out.innerHTML = '';
    const buy = Number((document.getElementById('fibaBuy').value || '').replace(',', '.'));
    const sell = Number((document.getElementById('fibaSell').value || '').replace(',', '.'));

    if(!lastGram){
      out.innerHTML = '<span class="muted">Teorik gram yoksa premium hesabı yapılamaz (TwelveData key yok / veri yok).</span>';
      return;
    }

    const parts = [];
    parts.push(`<div class="pill">Teorik Gram: <b>${lastGram.toFixed(4)} TRY</b></div>`);

    if (buy > 0){
      const premBuy = ((buy - lastGram) / lastGram) * 100;
      parts.push(`<div class="pill">FIBA Alış Premium: <b>${premBuy.toFixed(2)}%</b></div>`);
    }
    if (sell > 0){
      const premSell = ((sell - lastGram) / lastGram) * 100;
      parts.push(`<div class="pill">FIBA Satış Premium: <b>${premSell.toFixed(2)}%</b></div>`);
    }
    if (buy > 0 && sell > 0){
      const spread = ((sell - buy) / buy) * 100;
      parts.push(`<div class="pill">FIBA Makas: <b>${spread.toFixed(2)}%</b></div>`);
    }
    out.innerHTML = parts.join('');
  }

  // init
  mountTV('tvGram', 'FX_IDC:XAGTRYG');
  mountTV('tvXag',  'FX_IDC:XAGUSD');
  mountChart();
  loadSilver();

  // auto refresh each 45s
  setInterval(loadSilver, 45000);
</script>

</body></html>
"""

@app.get("/silver", response_class=HTMLResponse)
def silver_page():
    return SILVER_HTML

CRYPTO_HTML = """
<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Crypto Radar — Yiğit Mode</title>
<style>
 body{font-family:Arial;margin:18px;max-width:980px}
 .card{border:1px solid #e6e6e6;border-radius:14px;padding:14px;margin:12px 0}
 .muted{color:#666}
 .warn{color:#b00020}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #ddd;background:#fff;cursor:pointer}
 .btn:hover{border-color:#bbb}
 h2{margin:6px 0 10px}
</style>
</head><body>

<div class="row" style="align-items:center;justify-content:space-between">
  <h2>🪙 Crypto Radar — Yiğit Mode</h2>
  <div class="row">
    <button class="btn" onclick="location.href='/'">Ana Menü</button>
    <button class="btn" onclick="loadCrypto()">Yenile</button>
  </div>
</div>

<div id="mode" class="muted">Loading...</div>
<div id="warn" class="warn" style="margin-top:6px"></div>
<div id="list"></div>

<script>
  async function loadCrypto(){
    document.getElementById('warn').innerText = '';
    document.getElementById('mode').innerText = 'Loading...';
    document.getElementById('list').innerHTML = '';

    try{
      const r = await fetch('/api/crypto');
      const j = await r.json();

      document.getElementById('mode').innerText =
        `Market Mode: ${j.market_mode || 'UNKNOWN'} | BTC 24h: ${(j.btc_24h ?? 0).toFixed(2)}% | Source: CryptoCompare`;

      if (j.warning){
        document.getElementById('warn').innerText = 'Warning: ' + j.warning;
      }

      const list = document.getElementById('list');

      if (!j.top_picks || !j.top_picks.length){
        const el = document.createElement('div');
        el.className = 'card';
        el.innerHTML = `<b>Top 10 boş geldi.</b>
          <div class="muted" style="margin-top:6px">
          Filtreler sıkı olabilir. (VolMin=60M, 24h%=2-25)</div>`;
        list.appendChild(el);
        return;
      }

      j.top_picks.forEach(x => {
        const el = document.createElement('div');
        el.className = 'card';
        el.innerHTML = `
          <b>${(x.symbol||'').toUpperCase()}</b> <span class="muted">${x.name||''}</span><br>
          Price: ${x.price_usd} USD | 24h: ${x.chg24_pct}% | Vol24: ${Math.round(x.vol24_usd).toLocaleString()} USD<br>
          Score: <b>${x.score}</b><br>
          Plan: entry ${x.plan.entry} | stop ${x.plan.stop} | tp1 ${x.plan.tp1} | tp2 ${x.plan.tp2}
        `;
        list.appendChild(el);
      });

    }catch(e){
      document.getElementById('mode').innerText = 'Market Mode: UNKNOWN';
      document.getElementById('warn').innerText = 'Error: ' + e.message;
    }
  }

  loadCrypto();
  setInterval(loadCrypto, 45000);
</script>

</body></html>
"""

@app.get("/crypto", response_class=HTMLResponse)
def crypto_page():
    return CRYPTO_HTML
