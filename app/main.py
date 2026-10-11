from __future__ import annotations

import asyncio
import base64
import os
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.services.chart_service import generate_signal_chart
from app.services.post_templates import PostTemplateEngine

ROOT = Path(__file__).resolve().parent.parent
# DATA_DIR lets a host (e.g. a Render persistent disk) keep the DB and charts outside the code dir.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT / "data")
DB_PATH = DATA_DIR / "app.db"
CHARTS_DIR = DATA_DIR / "charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))
SKILL_URL = "https://github.com/binance/binance-skills-hub/tree/main/skills/binance/square-post"

app = FastAPI(title="Binance Square Signal Studio")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
app.mount("/charts", StaticFiles(directory=CHARTS_DIR), name="charts")
publisher_task: asyncio.Task | None = None
template_engine = PostTemplateEngine()

# Optional HTTP Basic auth. Enabled when APP_PASSWORD is set (always set it on a public host).
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_EXEMPT_PATHS = {"/healthz"}


def is_authorized(header: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user.encode(), APP_USERNAME.encode()) and secrets.compare_digest(
        password.encode(), APP_PASSWORD.encode()
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD and request.url.path not in AUTH_EXEMPT_PATHS:
        if not is_authorized(request.headers.get("authorization", "")):
            return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Signal Studio"'})
    return await call_next(request)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def conn():
    return db.connect(DB_PATH)


def init_db() -> None:
    db.init_schema(DB_PATH)


def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def delete_setting(key: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "••••••••"
    return f"{value[:5]}••••••••{value[-4:]}"


def find_square_post_script(name: str = "post-text.mjs") -> Path | None:
    candidates = [
        ROOT / ".claude" / "skills" / "square-post" / "scripts" / name,
        ROOT / ".agents" / "skills" / "square-post" / "scripts" / name,
        Path.home() / ".claude" / "skills" / "square-post" / "scripts" / name,
        Path.home() / ".agents" / "skills" / "square-post" / "scripts" / name,
    ]
    return next((p for p in candidates if p.exists()), None)


def readiness() -> dict[str, Any]:
    script = find_square_post_script()
    return {
        "node_available": shutil.which("node") is not None,
        "npx_available": shutil.which("npx") is not None,
        "square_skill_available": script is not None,
        "square_skill_path": str(script) if script else "",
        "publish_mode": get_setting("PUBLISH_MODE", "mock"),
        "has_square_key": bool(get_setting("BINANCE_SQUARE_OPENAPI_KEY")),
        "masked_square_key": mask_secret(get_setting("BINANCE_SQUARE_OPENAPI_KEY")),
    }


@app.on_event("startup")
async def startup() -> None:
    global publisher_task
    init_db()
    publisher_task = asyncio.create_task(publisher_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if publisher_task:
        publisher_task.cancel()


class ScanRequest(BaseModel):
    symbols: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT"]
    interval: str = "1h"
    bullish_only: bool = False


class DiscoverRequest(BaseModel):
    limit: int = 30
    hours: int = 4


class GenerateRequest(BaseModel):
    signal_id: int
    template_name: str = "auto"


class GenerateBulkRequest(BaseModel):
    signal_ids: list[int] = []
    limit: int = 10
    template_name: str = "auto"


class GenerateBtcRequest(BaseModel):
    template_name: str = "btc_market_analysis"
    interval: str = "1h"


class GenerateTop10Request(BaseModel):
    limit: int = 10


class SignalDeleteRequest(BaseModel):
    signal_ids: list[int] = []


class ScheduleRequest(BaseModel):
    post_ids: list[int]
    start_at: str | None = None
    spacing_minutes: int = 30
    delete_after_publish: bool = False


class PostUpdate(BaseModel):
    content: str


class PostIdsRequest(BaseModel):
    post_ids: list[int] = []


async def fetch_klines(symbol: str, interval: str, limit: int = 220) -> list[list[Any]]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
        r.raise_for_status()
        return r.json()


# OI discovery: rank the most-traded USDT perpetuals by open-interest growth.
OI_CANDIDATES = 150
OI_MIN_QUOTE_VOLUME = 5_000_000  # 24h USDT volume; skips illiquid contracts
OI_CONCURRENCY = 15


async def discover_oi_movers(limit: int, hours: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15, base_url="https://fapi.binance.com") as client:
        info_r, tickers_r = await asyncio.gather(client.get("/fapi/v1/exchangeInfo"), client.get("/fapi/v1/ticker/24hr"))
        info_r.raise_for_status()
        tickers_r.raise_for_status()
        perps = {
            s["symbol"] for s in info_r.json()["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        }
        tickers = [t for t in tickers_r.json() if t["symbol"] in perps and float(t["quoteVolume"]) >= OI_MIN_QUOTE_VOLUME]
        tickers.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        tickers = tickers[:OI_CANDIDATES]
        sem = asyncio.Semaphore(OI_CONCURRENCY)

        async def oi_change(t: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    r = await client.get("/futures/data/openInterestHist", params={"symbol": t["symbol"], "period": "1h", "limit": hours + 1})
                    r.raise_for_status()
                    hist = r.json()
                except (httpx.HTTPError, ValueError):
                    return None
            if len(hist) < 2:
                return None
            first, last = float(hist[0]["sumOpenInterestValue"]), float(hist[-1]["sumOpenInterestValue"])
            if first <= 0:
                return None
            return {
                "symbol": t["symbol"],
                "oi_change_pct": round((last / first - 1) * 100, 2),
                "oi_value": round(last),
                "price_change_24h_pct": round(float(t["priceChangePercent"]), 2),
                "quote_volume_24h": round(float(t["quoteVolume"])),
            }

        rows = [r for r in await asyncio.gather(*(oi_change(t) for t in tickers)) if r]
    rows.sort(key=lambda r: r["oi_change_pct"], reverse=True)
    return rows[:limit]


async def fetch_price(symbol: str) -> float:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": symbol})
        r.raise_for_status()
        return float(r.json()["price"])


def ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def rsi(values: list[float], period: int = 14) -> float:
    ds = [b - a for a, b in zip(values[:-1], values[1:])][-period:]
    gains = sum(max(x, 0) for x in ds) / period
    losses = sum(max(-x, 0) for x in ds) / period
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return trs


def wilder_smooth(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    out = [sum(values[:period])]
    for v in values[period:]:
        out.append(out[-1] - out[-1] / period + v)
    return out


def calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if not highs:
        return 0.0
    trs = true_ranges(highs, lows, closes)
    smoothed = wilder_smooth(trs, period)
    if not smoothed:
        return sum(h - l for h, l in zip(highs, lows)) / max(len(highs), 1)
    return smoothed[-1] / period


def calculate_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    n = len(highs)
    if n < period * 2 + 1:
        return 0.0
    plus_dm, minus_dm = [], []
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    trs = true_ranges(highs, lows, closes)[1:]
    sm_tr, sm_plus, sm_minus = wilder_smooth(trs, period), wilder_smooth(plus_dm, period), wilder_smooth(minus_dm, period)
    if not sm_tr or not sm_plus or not sm_minus:
        return 0.0
    length = min(len(sm_tr), len(sm_plus), len(sm_minus))
    dx_values = []
    for i in range(length):
        tr_i = sm_tr[i]
        if tr_i <= 0:
            dx_values.append(0.0)
            continue
        plus_di, minus_di = 100 * sm_plus[i] / tr_i, 100 * sm_minus[i] / tr_i
        denom = plus_di + minus_di
        dx_values.append(0.0 if denom == 0 else 100 * abs(plus_di - minus_di) / denom)
    if len(dx_values) < period:
        return dx_values[-1] if dx_values else 0.0
    return sum(dx_values[-period:]) / period


def calculate_bollinger_bands(closes: list[float], period: int = 20, num_std: float = 2.0) -> tuple[float, float, float]:
    window = closes[-period:] if len(closes) >= period else closes
    n = len(window)
    if n == 0:
        return 0.0, 0.0, 0.0
    mid = sum(window) / n
    variance = sum((x - mid) ** 2 for x in window) / n
    std = variance ** 0.5
    return mid + num_std * std, mid, mid - num_std * std


def analyze(symbol: str, interval: str, rows: list[list[Any]]) -> dict[str, Any]:
    if len(rows) < 50:
        raise ValueError(f"Insufficient candle data for {symbol}: got {len(rows)}, need at least 50")
    opens = [float(x[1]) for x in rows]
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    volumes = [float(x[5]) for x in rows]
    price = closes[-1]
    open_now, high_now, low_now, close_now = opens[-1], highs[-1], lows[-1], closes[-1]

    e20, e50, e200 = ema(closes[-80:], 20), ema(closes[-120:], 50), ema(closes, min(200, len(closes)))
    rv = rsi(closes)
    atr = calculate_atr(highs, lows, closes)
    adx = calculate_adx(highs, lows, closes)
    bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(closes)

    vol_window = volumes[-21:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    vr = volumes[-1] / max(avg_vol, 1e-9)

    candle_range = high_now - low_now
    dvp = (close_now - open_now) / candle_range if candle_range > 0 else 0.0
    bullish_candle = close_now > open_now
    bearish_candle = close_now < open_now
    candle_body = abs(close_now - open_now)

    support = min(lows[-40:])
    resistance = max(highs[-40:])
    prev_resistance = max(highs[-41:-1]) if len(highs) > 40 else resistance
    prev_support = min(lows[-41:-1]) if len(lows) > 40 else support
    dist_to_support = abs(price - support)
    dist_to_resistance = abs(price - resistance)

    regime = "TRENDING" if adx >= 23 else "RANGING"
    long_score, short_score = 50.0, 50.0

    if regime == "TRENDING":
        if price > e20 > e50 > e200:
            long_score += 30
        elif e20 > price >= e50:
            long_score += 25
        elif price < e50:
            long_score -= 25

        if price < e20 < e50 < e200:
            short_score += 30
        elif e20 < price <= e50:
            short_score += 25
        elif price > e50:
            short_score -= 25

        if atr > 0 and abs(price - e20) > 2.2 * atr:
            long_score -= 15
            short_score -= 15

        if vr >= 1.2 and bullish_candle:
            long_score += 15
            short_score -= 15
        elif vr >= 1.2 and bearish_candle:
            short_score += 15
            long_score -= 15

        if 50 <= rv <= 75:
            long_score += 15
        elif rv > 75 and not bearish_candle:
            long_score += 5
        if 25 <= rv <= 50:
            short_score += 15
        elif rv < 25 and not bullish_candle:
            short_score += 5

        if price > prev_resistance and vr >= 1.1:
            long_score += 15
        if price < prev_support and vr >= 1.1:
            short_score += 15
    else:
        if low_now <= bb_lower and price > bb_lower:
            long_score += 30
        if high_now >= bb_upper and price < bb_upper:
            short_score += 30

        if atr > 0 and dist_to_support <= 0.4 * atr:
            long_score += 20
        if atr > 0 and dist_to_resistance <= 0.4 * atr:
            short_score += 20

        if rv <= 30:
            long_score += 25
        elif rv >= 65:
            long_score -= 20
        if rv >= 70:
            short_score += 25
        elif rv <= 35:
            short_score -= 20

        narrow_body = atr > 0 and candle_body < 0.3 * atr
        if narrow_body and vr > 1.2:
            if atr > 0 and dist_to_resistance <= 0.4 * atr:
                short_score += 15
            if atr > 0 and dist_to_support <= 0.4 * atr:
                long_score += 15

    side = "LONG" if long_score >= short_score else "SHORT"
    score = max(1, min(95, round(max(long_score, short_score))))
    confidence = round(score / 100.0, 2)

    if side == "LONG":
        lo, hi = price - atr * .35, price + atr * .05
        sl = min(price - 1.2 * atr, support - 0.2 * atr)
        risk = price - sl if price - sl > 0 else atr * 1.2
        tps = [price + risk * x for x in (1.2, 2, 3)]
        if resistance > price:
            tps = [min(tp, resistance) for tp in tps]
    else:
        lo, hi = price - atr * .05, price + atr * .35
        sl = max(price + 1.2 * atr, resistance + 0.2 * atr)
        risk = sl - price if sl - price > 0 else atr * 1.2
        tps = [price - risk * x for x in (1.2, 2, 3)]
        if support < price:
            tps = [max(tp, support) for tp in tps]

    digits = 6 if price < 1 else 4 if price < 100 else 2
    q = lambda x: round(x, digits)
    return {
        "symbol": symbol, "side": side, "timeframe": interval,
        "entry_low": q(lo), "entry_high": q(hi), "stop_loss": q(sl),
        "tp1": q(tps[0]), "tp2": q(tps[1]), "tp3": q(tps[2]), "score": score,
        "reason": (
            f"Regime={regime} (ADX={adx:.1f}); EMA20={e20:.4g}, EMA50={e50:.4g}, EMA200={e200:.4g}; "
            f"RSI={rv:.1f}; volume ratio={vr:.2f}, delta-vol={dvp:.2f}; confidence={confidence:.2f}. Verify before posting."
        ),
        "price": q(price), "ema20": q(e20), "ema50": q(e50), "ema200": q(e200),
        "rsi": round(rv, 1), "atr": q(atr), "volume_ratio": round(vr, 2),
        "support": q(support), "resistance": q(resistance),
        "regime": regime, "adx": round(adx, 1), "confidence": confidence,
    }


def btc_regime_label(btc_signal: dict[str, Any]) -> str:
    e20, e50, e200 = btc_signal.get("ema20"), btc_signal.get("ema50"), btc_signal.get("ema200")
    if None in (e20, e50, e200):
        return "neutral"
    if e20 > e50 > e200:
        return "bullish"
    if e20 < e50 < e200:
        return "bearish"
    return "mixed/neutral"


def compute_status(s: sqlite3.Row, price: float) -> str:
    side = s["side"]
    entry_low, entry_high, sl = s["entry_low"], s["entry_high"], s["stop_loss"]
    tp1, tp2, tp3 = s["tp1"], s["tp2"], s["tp3"]
    current_status = s["status"] or "NEW"
    in_or_past_entry = (price <= entry_high) if side == "LONG" else (price >= entry_low)
    triggered = current_status not in ("NEW",) or in_or_past_entry
    if side == "LONG":
        if not triggered:
            return "INVALIDATED" if price <= sl else "NEW"
        if price >= tp3:
            return "TP3_HIT"
        if price >= tp2:
            return "TP2_HIT"
        if price >= tp1:
            return "TP1_HIT"
        if price <= sl:
            return "SL_HIT"
        return "ENTRY_TRIGGERED"
    else:
        if not triggered:
            return "INVALIDATED" if price >= sl else "NEW"
        if price <= tp3:
            return "TP3_HIT"
        if price <= tp2:
            return "TP2_HIT"
        if price <= tp1:
            return "TP1_HIT"
        if price >= sl:
            return "SL_HIT"
        return "ENTRY_TRIGGERED"


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state() -> dict[str, Any]:
    with conn() as c:
        signals = [dict(x) for x in c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 1000")]
        posts = [dict(x) for x in c.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 200")]
    return {"signals": signals, "posts": posts, "config": readiness()}


@app.post("/api/settings")
def settings(binance_square_openapi_key: str = Form(default=""), publish_mode: str = Form(default="mock")) -> dict[str, Any]:
    if publish_mode not in {"mock", "square_api"}:
        raise HTTPException(400, "Invalid publish mode")
    set_setting("PUBLISH_MODE", publish_mode)
    key = binance_square_openapi_key.strip()
    if key and "••" not in key:
        set_setting("BINANCE_SQUARE_OPENAPI_KEY", key)
    return {"ok": True, "config": readiness()}


@app.delete("/api/settings/key")
def clear_key() -> dict[str, bool]:
    delete_setting("BINANCE_SQUARE_OPENAPI_KEY")
    set_setting("PUBLISH_MODE", "mock")
    return {"ok": True}


@app.post("/api/setup-square-skill")
async def setup_square_skill() -> dict[str, Any]:
    if find_square_post_script():
        return {"ok": True, "message": "Official square-post skill is already installed.", "config": readiness()}
    if not shutil.which("npx"):
        raise HTTPException(500, "Node.js/npm is missing. Install Node.js 18+ first.")
    cmd = ["npx", "--yes", "skills", "add", SKILL_URL]
    try:
        proc = await asyncio.to_thread(subprocess.run, cmd, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Skill installation timed out")
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0 or not find_square_post_script():
        raise HTTPException(500, output[-1800:] or "Could not install square-post skill")
    return {"ok": True, "message": "Official Binance square-post skill installed.", "config": readiness()}


@app.post("/api/test-square")
def test_square() -> dict[str, Any]:
    cfg = readiness()
    missing = [name for name, ok in [("Node.js", cfg["node_available"]), ("Square skill", cfg["square_skill_available"]), ("API key", cfg["has_square_key"])] if not ok]
    if missing:
        raise HTTPException(400, "Missing: " + ", ".join(missing))
    return {"ok": True, "message": "Ready to post. This check does not publish anything.", "config": cfg}


@app.post("/api/discover")
async def discover(req: DiscoverRequest) -> dict[str, Any]:
    limit = max(1, min(req.limit, 100))
    hours = max(1, min(req.hours, 24))
    try:
        movers = await discover_oi_movers(limit, hours)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Binance request failed: {exc}")
    return {"symbols": [m["symbol"] for m in movers], "movers": movers, "hours": hours}


@app.post("/api/scan")
async def scan(req: ScanRequest) -> dict[str, Any]:
    symbols = list(dict.fromkeys(raw.upper().replace("/", "").strip() for raw in req.symbols if raw.strip()))
    sem = asyncio.Semaphore(10)

    async def klines(symbol: str) -> list[list[Any]]:
        async with sem:
            return await fetch_klines(symbol, req.interval)

    fetched = await asyncio.gather(*(klines(sym) for sym in symbols), return_exceptions=True)
    results, errors = [], []
    for symbol, rows in zip(symbols, fetched):
        try:
            if isinstance(rows, BaseException):
                raise rows
            sig = analyze(symbol, req.interval, rows)
            if req.bullish_only and sig["side"] != "LONG":
                continue
            with conn() as c:
                cur = c.execute(
                    """INSERT INTO signals(symbol,side,timeframe,entry_low,entry_high,stop_loss,tp1,tp2,tp3,score,reason,
                       price,ema20,ema50,ema200,rsi,atr,volume_ratio,support,resistance,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sig["symbol"], sig["side"], sig["timeframe"], sig["entry_low"], sig["entry_high"], sig["stop_loss"],
                     sig["tp1"], sig["tp2"], sig["tp3"], sig["score"], sig["reason"], sig["price"], sig["ema20"],
                     sig["ema50"], sig["ema200"], sig["rsi"], sig["atr"], sig["volume_ratio"], sig["support"],
                     sig["resistance"], now_iso()),
                )
                sig["id"] = cur.lastrowid
            results.append(sig)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
    return {"results": results, "errors": errors}


@app.post("/api/signals/delete")
def delete_signals(req: SignalDeleteRequest) -> dict[str, Any]:
    if not req.signal_ids:
        return {"ok": True, "count": 0}
    with conn() as c:
        placeholders = ",".join("?" * len(req.signal_ids))
        cur = c.execute(f"DELETE FROM signals WHERE id IN ({placeholders})", req.signal_ids)
    return {"ok": True, "count": cur.rowcount}


@app.post("/api/signals/delete-all")
def delete_all_signals() -> dict[str, Any]:
    with conn() as c:
        cur = c.execute("DELETE FROM signals")
    return {"ok": True, "count": cur.rowcount}


@app.post("/api/signals/{signal_id}/refresh-status")
async def refresh_status(signal_id: int) -> dict[str, Any]:
    with conn() as c:
        s = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Signal not found")
    price = await fetch_price(s["symbol"])
    new_status = compute_status(s, price)
    with conn() as c:
        c.execute("UPDATE signals SET status=? WHERE id=?", (new_status, signal_id))
    return {"ok": True, "status": new_status, "price": price}


def render_chart_for_signal(s: sqlite3.Row, rows: list[list[Any]], btc_regime: str = "") -> str:
    sig = dict(s)
    if btc_regime:
        sig["btc_regime"] = btc_regime
    out_path = CHARTS_DIR / f"{s['symbol']}_{s['id']}_{int(datetime.now().timestamp())}.png"
    return generate_signal_chart(rows, sig, out_path, dark_theme=True)


def recent_template_names(limit: int = 5) -> list[str]:
    with conn() as c:
        rows = c.execute("SELECT template_name FROM posts WHERE template_name IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [r["template_name"] for r in rows if r["template_name"]][::-1]


async def generate_one(signal_id: int, template_name: str = "auto") -> dict[str, Any]:
    with conn() as c:
        s = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Signal not found")
    sig = dict(s)
    mc: dict[str, Any] = {"recent_templates": recent_template_names()}
    content = template_engine.generate(sig, template_name=template_name, market_context=mc)
    used_template = template_engine.last_template
    chart_path = ""
    try:
        rows = await fetch_klines(s["symbol"], s["timeframe"])
        chart_path = render_chart_for_signal(s, rows)
    except Exception:
        chart_path = ""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO posts(signal_id,title,content,status,created_at,chart_path,template_name) VALUES(?,?,?,?,?,?,?)",
            (s["id"], f"{s['symbol']} {s['side']}", content, "DRAFT", now_iso(), chart_path, used_template),
        )
    return {"id": cur.lastrowid, "content": content, "chart_path": chart_path, "template_name": used_template}


@app.post("/api/generate")
async def generate(req: GenerateRequest) -> dict[str, Any]:
    return await generate_one(req.signal_id, req.template_name)


@app.post("/api/generate-bulk")
async def generate_bulk(req: GenerateBulkRequest) -> dict[str, Any]:
    limit = max(1, min(req.limit, 10))
    ids = req.signal_ids[:limit]
    if not ids:
        with conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM signals ORDER BY id DESC LIMIT ?", (limit,))]
    results, errors = [], []
    for sid in ids:
        try:
            results.append(await generate_one(sid, req.template_name))
        except HTTPException as exc:
            errors.append(f"signal {sid}: {exc.detail}")
        except Exception as exc:
            errors.append(f"signal {sid}: {exc}")
    return {"results": results, "errors": errors}


@app.post("/api/generate-all")
async def generate_all(req: GenerateBulkRequest) -> dict[str, Any]:
    """Create one draft for every current signal, without the bulk action's ten-post cap."""
    ids = req.signal_ids
    if not ids:
        with conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM signals ORDER BY id DESC")]
    if not ids:
        raise HTTPException(400, "No signals yet. Scan first.")

    results, errors = [], []
    for sid in ids:
        try:
            results.append(await generate_one(sid, req.template_name))
        except HTTPException as exc:
            errors.append(f"signal {sid}: {exc.detail}")
        except Exception as exc:
            errors.append(f"signal {sid}: {exc}")
    return {"results": results, "errors": errors}


async def build_btc_context(interval: str = "1h") -> tuple[dict[str, Any], list[list[Any]]]:
    rows = await fetch_klines("BTCUSDT", interval)
    btc_sig = analyze("BTCUSDT", interval, rows)
    lows = [float(r[3]) for r in rows]
    highs = [float(r[2]) for r in rows]
    regime = btc_regime_label(btc_sig)
    mc = {
        "regime": regime,
        "bias": btc_sig["side"],
        "support_1": btc_sig["support"],
        "support_2": min(lows),
        "resistance_1": btc_sig["resistance"],
        "resistance_2": max(highs),
    }
    return {**btc_sig, "market_context": mc}, rows


@app.post("/api/generate-btc")
async def generate_btc(req: GenerateBtcRequest) -> dict[str, Any]:
    btc_sig, rows = await build_btc_context(req.interval)
    mc = btc_sig["market_context"]
    with conn() as c:
        cur = c.execute(
            """INSERT INTO signals(symbol,side,timeframe,entry_low,entry_high,stop_loss,tp1,tp2,tp3,score,reason,
               price,ema20,ema50,ema200,rsi,atr,volume_ratio,support,resistance,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                btc_sig["symbol"], btc_sig["side"], btc_sig["timeframe"], btc_sig["entry_low"], btc_sig["entry_high"],
                btc_sig["stop_loss"], btc_sig["tp1"], btc_sig["tp2"], btc_sig["tp3"], btc_sig["score"], btc_sig["reason"],
                btc_sig["price"], btc_sig["ema20"], btc_sig["ema50"], btc_sig["ema200"], btc_sig["rsi"], btc_sig["atr"],
                btc_sig["volume_ratio"], btc_sig["support"], btc_sig["resistance"], now_iso(),
            ),
        )
        signal_id = cur.lastrowid
    template_name = req.template_name if req.template_name in ("btc_market_analysis", "btc_decisive_zone") else "btc_market_analysis"
    content = template_engine.generate(btc_sig, template_name=template_name, market_context=mc)
    chart_path = render_chart_for_signal({**btc_sig, "id": signal_id}, rows, btc_regime=mc["regime"])
    with conn() as c:
        cur = c.execute(
            "INSERT INTO posts(signal_id,title,content,status,created_at,chart_path,template_name) VALUES(?,?,?,?,?,?,?)",
            (signal_id, "BTC Market Analysis", content, "DRAFT", now_iso(), chart_path, template_name),
        )
    return {"id": cur.lastrowid, "content": content, "chart_path": chart_path, "template_name": template_name}


@app.post("/api/generate-top10")
async def generate_top10(req: GenerateTop10Request) -> dict[str, Any]:
    limit = max(1, min(req.limit, 10))
    with conn() as c:
        rows = c.execute("SELECT * FROM signals ORDER BY score DESC, id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        raise HTTPException(400, "No signals yet. Scan first.")
    signals = [dict(r) for r in rows]
    try:
        _, btc_rows = await build_btc_context()
        btc_regime = btc_regime_label(analyze("BTCUSDT", "1h", btc_rows))
    except Exception:
        btc_regime = "unknown"
    mc = {"signals": signals, "btc_regime": btc_regime}
    content = template_engine.generate(signals[0], template_name="top_10_signals", market_context=mc)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO posts(signal_id,title,content,status,created_at,chart_path,template_name) VALUES(?,?,?,?,?,?,?)",
            (None, "Top 10 Watchlist", content, "DRAFT", now_iso(), "", "top_10_signals"),
        )
    return {"id": cur.lastrowid, "content": content, "template_name": "top_10_signals"}


@app.post("/api/posts/{post_id}/regenerate")
async def regenerate_post(post_id: int) -> dict[str, Any]:
    with conn() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Post not found")
    if not p["signal_id"]:
        raise HTTPException(400, "This post has no linked signal to regenerate from.")
    with conn() as c:
        s = c.execute("SELECT * FROM signals WHERE id=?", (p["signal_id"],)).fetchone()
    if not s:
        raise HTTPException(404, "Signal not found")
    sig = dict(s)
    recent = recent_template_names()
    if p["template_name"]:
        recent = recent + [p["template_name"]]
    mc = {"recent_templates": recent}
    content = template_engine.generate(sig, template_name="auto", market_context=mc)
    used_template = template_engine.last_template
    chart_path = p["chart_path"] or ""
    try:
        rows = await fetch_klines(s["symbol"], s["timeframe"])
        chart_path = render_chart_for_signal(s, rows)
    except Exception:
        pass
    with conn() as c:
        c.execute(
            "UPDATE posts SET content=?, chart_path=?, template_name=? WHERE id=?",
            (content, chart_path, used_template, post_id),
        )
    return {"id": post_id, "content": content, "chart_path": chart_path, "template_name": used_template}


@app.patch("/api/posts/{post_id}")
def update_post(post_id: int, req: PostUpdate) -> dict[str, bool]:
    with conn() as c:
        c.execute("UPDATE posts SET content=? WHERE id=?", (req.content, post_id))
    return {"ok": True}


async def publish_post(post_id: int) -> dict[str, Any]:
    with conn() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Post not found")
    mode = get_setting("PUBLISH_MODE", "mock")
    if mode == "mock":
        result = "MOCK: saved locally, nothing sent to Binance."
    else:
        api_key = get_setting("BINANCE_SQUARE_OPENAPI_KEY").strip()
        chart_path = p["chart_path"] if "chart_path" in p.keys() else ""
        has_chart = bool(chart_path) and Path(chart_path).exists()
        script = find_square_post_script("post-image.mjs" if has_chart else "post-text.mjs")
        if not api_key:
            raise HTTPException(400, "Paste and save your Binance Square OpenAPI key first.")
        if script is None:
            raise HTTPException(500, "Official square-post skill is not installed. Click Install Square Skill.")
        if not shutil.which("node"):
            raise HTTPException(500, "Node.js 18+ is required.")
        env = os.environ.copy()
        env["BINANCE_SQUARE_OPENAPI_KEY"] = api_key
        cmd = ["node", str(script), "--text", p["content"]]
        if has_chart:
            cmd += ["--images", chart_path]
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd, cwd=script.parent.parent, env=env, capture_output=True, text=True, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Binance Square post timed out")
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise HTTPException(502, output[-1800:] or f"square-post failed ({proc.returncode})")
        result = output[-1800:] or "Success"
    with conn() as c:
        if p["delete_after_publish"]:
            c.execute("DELETE FROM posts WHERE id=?", (post_id,))
        else:
            c.execute("UPDATE posts SET status='PUBLISHED', published_at=?, publish_result=? WHERE id=?", (now_iso(), result, post_id))
    return {"ok": True, "result": result}


@app.post("/api/posts/{post_id}/publish")
async def publish_now(post_id: int) -> dict[str, Any]:
    return await publish_post(post_id)


@app.post("/api/schedule")
def schedule_posts(req: ScheduleRequest) -> dict[str, Any]:
    if req.start_at:
        try:
            start = datetime.fromisoformat(req.start_at)
        except ValueError:
            raise HTTPException(400, "Invalid start_at")
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
    else:
        start = datetime.now(TZ)
    spacing = max(1, min(1440, req.spacing_minutes))
    with conn() as c:
        for i, pid in enumerate(req.post_ids[:50]):
            c.execute(
                "UPDATE posts SET status='SCHEDULED', scheduled_at=?, delete_after_publish=? WHERE id=?",
                ((start + timedelta(minutes=i * spacing)).isoformat(timespec="seconds"), int(req.delete_after_publish), pid),
            )
    return {"ok": True, "count": len(req.post_ids), "spacing": spacing}


@app.post("/api/schedule/cancel")
def cancel_schedule(req: PostIdsRequest) -> dict[str, Any]:
    ids = req.post_ids
    if not ids:
        return {"ok": True, "count": 0}
    with conn() as c:
        placeholders = ",".join("?" * len(ids))
        cur = c.execute(
            f"UPDATE posts SET status='DRAFT', scheduled_at=NULL, delete_after_publish=0 WHERE id IN ({placeholders}) AND status='SCHEDULED'",
            ids,
        )
    return {"ok": True, "count": cur.rowcount}


@app.post("/api/posts/{post_id}/delete")
def delete_post(post_id: int) -> dict[str, bool]:
    with conn() as c:
        c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    return {"ok": True}


@app.post("/api/posts/delete-all")
def delete_all_posts() -> dict[str, Any]:
    with conn() as c:
        cur = c.execute("DELETE FROM posts")
    return {"ok": True, "count": cur.rowcount}


async def process_due_posts() -> None:
    with conn() as c:
        rows = c.execute("SELECT id FROM posts WHERE status='SCHEDULED' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1", (now_iso(),)).fetchall()
    for row in rows:
        try:
            await publish_post(row["id"])
        except Exception as exc:
            with conn() as c:
                c.execute("UPDATE posts SET status='ERROR', publish_result=? WHERE id=?", (str(exc), row["id"]))


async def publisher_loop() -> None:
    while True:
        await process_due_posts()
        await asyncio.sleep(20)
