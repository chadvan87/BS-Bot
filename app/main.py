from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
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
from app.services.market_events import detect_breakout, detect_retest, detect_reversal, evaluate_path
from app.services.opportunities import OpportunityConfig, age_minutes, rank_opportunities, select_candidates, shortlist_stage2
from app.services.post_templates import PostTemplateEngine, check_post_text

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
    # Klines fetched by the browser, keyed by symbol; used instead of calling Binance from the server.
    klines: dict[str, list[list[Any]]] = {}


class KlinesUpload(BaseModel):
    interval: str = "1h"
    klines: dict[str, list[list[Any]]] = {}


class RefreshStatusRequest(BaseModel):
    price: float | None = None
    # Candles of the signal's timeframe fetched by the browser; used to replay what happened since the signal.
    klines: list[list[Any]] | None = None


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


class OpportunityCandidatesRequest(BaseModel):
    tickers: list[dict[str, Any]] = []
    mode: str = "top"  # "top" = most active coins, "all" = every liquid coin via the 15m-first funnel
    funding: dict[str, dict[str, Any]] = {}
    book: dict[str, dict[str, Any]] = {}


class OpportunityShortlistRequest(BaseModel):
    tickers: list[dict[str, Any]] = []
    klines15: dict[str, list[list[Any]]] = {}
    funding: dict[str, dict[str, Any]] = {}


class OpportunityScanRequest(BaseModel):
    tickers: list[dict[str, Any]] = []
    klines: dict[str, dict[str, list[list[Any]]]] = {}
    funding: dict[str, dict[str, Any]] = {}
    oi_hist: dict[str, list[dict[str, Any]]] = {}
    book: dict[str, dict[str, Any]] = {}
    spot_symbols: list[str] = []
    symbols: list[str] = []
    stats: dict[str, Any] = {}  # funnel counts and request weight used, shown in the UI


class OpportunityGenerateAllRequest(BaseModel):
    scope: str = "watchlist"  # ready = publish-ready only, watchlist = ready + watchlist, all = every ranked coin
    limit: int = 50
    force: bool = False


class OpportunityGenerateRequest(BaseModel):
    symbol: str
    template_name: str = "auto"
    force: bool = False


# Binance answers 429 (slow down) or 418 (IP ban, 2 minutes up to 3 days) when this server's IP sends
# too many requests, and keeps extending the ban while requests continue. After either, stop calling
# Binance from the server until Retry-After passes. The browser fetches market data itself instead
# (Binance allows CORS) and sends it along, so scans keep working from the user's own IP.
_binance_paused_until = 0.0
KLINES_CACHE_SECONDS = 3600
_klines_cache: dict[tuple[str, str], tuple[float, list[list[Any]]]] = {}


def remember_klines(symbol: str, interval: str, rows: list[list[Any]]) -> None:
    _klines_cache[(symbol, interval)] = (time.monotonic(), rows)


async def binance_get(path: str, params: dict[str, Any], timeout: float) -> Any:
    global _binance_paused_until
    wait = _binance_paused_until - time.monotonic()
    if wait > 0:
        raise RuntimeError(f"Binance has blocked this server's IP for ~{int(wait) + 1}s more and your browser could not fetch this symbol either.")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"https://fapi.binance.com{path}", params=params)
    if r.status_code in (418, 429):
        retry = int(r.headers.get("retry-after") or 120)
        _binance_paused_until = time.monotonic() + retry
        raise RuntimeError(f"Binance has blocked this server's IP (HTTP {r.status_code}) for ~{retry}s and your browser could not fetch this symbol either.")
    r.raise_for_status()
    return r.json()


async def fetch_klines(symbol: str, interval: str, limit: int = 220) -> list[list[Any]]:
    cached = _klines_cache.get((symbol, interval))
    if cached and time.monotonic() - cached[0] < KLINES_CACHE_SECONDS:
        return cached[1]
    rows = await binance_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}, 15)
    remember_klines(symbol, interval, rows)
    return rows


async def fetch_price(symbol: str) -> float:
    return float((await binance_get("/fapi/v1/ticker/price", {"symbol": symbol}, 10))["price"])


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
    # Closed-candle evidence for the post templates and the chart/market-data timestamp.
    breakout = detect_breakout(rows)
    evidence = {
        "regime": regime, "adx": round(adx, 1), "breakout": breakout,
        "retest": detect_retest(rows, breakout),
        "reversal": detect_reversal(rows, side, rv, support, resistance, atr),
        "candle_time": int(rows[-1][0]),
    }
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
        "regime": regime, "adx": round(adx, 1), "confidence": confidence, "evidence": evidence,
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


@app.post("/api/klines")
def upload_klines(req: KlinesUpload) -> dict[str, int]:
    """Cache browser-fetched klines so charts and BTC context don't need the server to call Binance."""
    for symbol, rows in req.klines.items():
        if rows:
            remember_klines(symbol.upper(), req.interval, rows)
    return {"count": len(req.klines)}


def insert_signal(sig: dict[str, Any], event_key: str | None = None) -> int:
    evidence = dict(sig.get("evidence") or {})
    if event_key:
        evidence["event_key"] = event_key
    with conn() as c:
        cur = c.execute(
            """INSERT INTO signals(symbol,side,timeframe,entry_low,entry_high,stop_loss,tp1,tp2,tp3,score,reason,
               price,ema20,ema50,ema200,rsi,atr,volume_ratio,support,resistance,created_at,evidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sig["symbol"], sig["side"], sig["timeframe"], sig["entry_low"], sig["entry_high"], sig["stop_loss"],
             sig["tp1"], sig["tp2"], sig["tp3"], sig["score"], sig["reason"], sig["price"], sig["ema20"],
             sig["ema50"], sig["ema200"], sig["rsi"], sig["atr"], sig["volume_ratio"], sig["support"],
             sig["resistance"], now_iso(), json.dumps(evidence, separators=(",", ":"))),
        )
    return cur.lastrowid


@app.post("/api/scan")
async def scan(req: ScanRequest) -> dict[str, Any]:
    results, errors = [], []
    for raw in req.symbols:
        symbol = raw.upper().replace("/", "").strip()
        try:
            rows = req.klines.get(symbol)
            if rows:
                remember_klines(symbol, req.interval, rows)
            else:
                rows = await fetch_klines(symbol, req.interval)
            sig = analyze(symbol, req.interval, rows)
            if req.bullish_only and sig["side"] != "LONG":
                continue
            sig["id"] = insert_signal(sig)
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


SIGNAL_EXPIRY_CANDLES = 24
TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
STATUS_RANK = {"NEW": 0, "ENTRY_TRIGGERED": 1, "TP1_HIT": 2, "TP2_HIT": 3, "TP3_HIT": 4}
FINAL_STATUSES = {"SL_HIT", "INVALIDATED", "EXPIRED", "AMBIGUOUS", "TP3_HIT"}


def parse_evidence(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def resolve_status(s: Any, price: float, candles: list[list[Any]] | None) -> tuple[str, str]:
    """Combine the live price with a candle replay. Never regress, and never guess an ambiguous order."""
    current = s["status"] or "NEW"
    if current in FINAL_STATUSES:
        return current, ""
    new = compute_status(s, price)
    note = ""
    if candles:
        created = datetime.fromisoformat(s["created_at"])
        created_ms = int(created.timestamp() * 1000)
        after = [c for c in candles if int(c[0]) > created_ms]  # skip the candle the signal was born in
        path = evaluate_path(dict(s), after)
        if path["status"] in ("AMBIGUOUS", "SL_HIT", "INVALIDATED") or STATUS_RANK.get(path["status"], 0) > STATUS_RANK.get(new, 0):
            new, note = path["status"], path["note"]
    if new in STATUS_RANK and STATUS_RANK[new] < STATUS_RANK.get(current, 0):
        new = current  # price drifting back does not undo a target already reached
    if new == "NEW":
        age_ms = datetime.now(TZ).timestamp() * 1000 - datetime.fromisoformat(s["created_at"]).timestamp() * 1000
        if age_ms > SIGNAL_EXPIRY_CANDLES * TF_MS.get(s["timeframe"], 3_600_000):
            new, note = "EXPIRED", "Price never reached the entry zone within the setup window."
    return new, note


@app.post("/api/signals/{signal_id}/refresh-status")
async def refresh_status(signal_id: int, req: RefreshStatusRequest | None = None) -> dict[str, Any]:
    with conn() as c:
        s = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Signal not found")
    price = req.price if req and req.price else await fetch_price(s["symbol"])
    new_status, note = resolve_status(s, price, req.klines if req else None)
    evidence = parse_evidence(s["evidence"] if "evidence" in s.keys() else None)
    evidence["last_check"] = {"price": price, "time": now_iso(), "note": note}
    with conn() as c:
        c.execute("UPDATE signals SET status=?, evidence=? WHERE id=?", (new_status, json.dumps(evidence, separators=(",", ":")), signal_id))
    return {"ok": True, "status": new_status, "price": price, "note": note}


def render_chart_for_signal(s: sqlite3.Row, rows: list[list[Any]], btc_regime: str = "") -> str:
    """Render the chart for exactly this signal: its symbol, timeframe, side and levels, and the
    candles up to the market-data timestamp stored with it (so a later refetch can't shift the chart)."""
    sig = dict(s)
    if btc_regime:
        sig["btc_regime"] = btc_regime
    candle_time = parse_evidence(sig.get("evidence")).get("candle_time")
    if candle_time:
        rows = [r for r in rows if int(r[0]) <= int(candle_time)]
        if not rows or int(rows[-1][0]) != int(candle_time):
            raise ValueError("candle data does not include the candle this signal was made on")
    out_path = CHARTS_DIR / f"{s['symbol']}_{s['id']}_{int(datetime.now().timestamp())}.png"
    path = generate_signal_chart(rows, sig, out_path, dark_theme=True)
    if not Path(path).exists() or Path(path).stat().st_size < 1000:
        raise ValueError("chart file was not written")
    return path


async def chart_for_signal(s: sqlite3.Row, btc_regime: str = "") -> str:
    """Chart for a signal post, or HTTP 502 — a signal post must never silently lose its image."""
    try:
        rows = await fetch_klines(s["symbol"], s["timeframe"])
        return render_chart_for_signal(s, rows, btc_regime)
    except Exception as exc:
        raise HTTPException(502, f"Chart image failed for {s['symbol']} {s['timeframe']}: {exc}. No draft was changed or created.")


def recent_template_names(limit: int = 5) -> list[str]:
    with conn() as c:
        rows = c.execute("SELECT template_name FROM posts WHERE template_name IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [r["template_name"] for r in rows if r["template_name"]][::-1]


async def generate_one(signal_id: int, template_name: str = "auto", extra_context: dict[str, Any] | None = None) -> dict[str, Any]:
    with conn() as c:
        s = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Signal not found")
    sig = dict(s)
    mc: dict[str, Any] = {"recent_templates": recent_template_names(), **(extra_context or {})}
    content = template_engine.generate(sig, template_name=template_name, market_context=mc)
    used_template = template_engine.last_template
    problems = check_post_text(content)
    if problems:
        raise HTTPException(500, "Generated post failed content checks: " + "; ".join(problems))
    chart_path = await chart_for_signal(s)
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
    signal_id = insert_signal(btc_sig)
    template_name = req.template_name if req.template_name in ("btc_market_analysis", "btc_decisive_zone") else "btc_market_analysis"
    content = template_engine.generate(btc_sig, template_name=template_name, market_context=mc)
    try:
        chart_path = render_chart_for_signal({**btc_sig, "id": signal_id, "evidence": json.dumps(btc_sig.get("evidence") or {})}, rows, btc_regime=mc["regime"])
    except Exception as exc:
        raise HTTPException(502, f"Chart image failed for BTCUSDT {req.interval}: {exc}. No draft was created.")
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
    chart_path = await chart_for_signal(s)
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
    chart_file = p["chart_path"] if "chart_path" in p.keys() else ""
    if p["signal_id"] and not (chart_file and Path(chart_file).exists()):
        raise HTTPException(409, "This signal post is missing its chart image, so it was not published. "
                                 "Use 'Another style' to regenerate the chart, or delete the draft.")
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


# ---- Signal Opportunities (ranking layer on top of the scanner) ----------------------------------

OPPORTUNITIES_KEY = "OPPORTUNITIES_LAST"


def opportunity_config() -> OpportunityConfig:
    try:
        raw = json.loads(get_setting("OPPORTUNITY_CONFIG", "") or "{}")
    except ValueError:
        raw = {}
    return OpportunityConfig.from_dict(raw if isinstance(raw, dict) else {})


def signal_history() -> dict[str, dict[str, int]]:
    """Per symbol: how many of our own past signals reached a target versus the stop."""
    out: dict[str, dict[str, int]] = {}
    with conn() as c:
        rows = c.execute("SELECT symbol, status FROM signals WHERE status IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT')").fetchall()
    for r in rows:
        h = out.setdefault(r["symbol"], {"tp": 0, "sl": 0})
        h["sl" if r["status"] == "SL_HIT" else "tp"] += 1
    return out


def load_opportunities() -> dict[str, Any]:
    try:
        return json.loads(get_setting(OPPORTUNITIES_KEY, "") or "{}")
    except ValueError:
        return {}


@app.get("/api/opportunities")
def get_opportunities() -> dict[str, Any]:
    data = load_opportunities()
    return data or {"results": [], "skipped": [], "scanned_at": None, "config": opportunity_config().to_dict()}


@app.post("/api/opportunities/candidates")
def opportunity_candidates(req: OpportunityCandidatesRequest) -> dict[str, Any]:
    """Which symbols the browser should fetch candles for (liquid and moving), per the configured thresholds."""
    cfg = opportunity_config()
    mode = "all" if req.mode == "all" else "top"
    return {"symbols": select_candidates(req.tickers, cfg, mode, req.funding, req.book), "mode": mode, "config": cfg.to_dict()}


@app.post("/api/opportunities/shortlist")
def opportunity_shortlist(req: OpportunityShortlistRequest) -> dict[str, Any]:
    """Stage 2 of the "all" funnel: which coins showed a fresh 15m trigger and deserve 1h/4h/OI requests."""
    return shortlist_stage2(req.klines15, req.tickers, req.funding, opportunity_config())


@app.post("/api/opportunities/scan")
def opportunity_scan(req: OpportunityScanRequest) -> dict[str, Any]:
    cfg = opportunity_config()
    for sym, by_tf in req.klines.items():
        for tf, rows in by_tf.items():
            if rows:
                remember_klines(sym.upper(), tf, rows)
    result = rank_opportunities(req.model_dump(), cfg, analyze, signal_history())
    result["stats"] = req.stats
    set_setting(OPPORTUNITIES_KEY, json.dumps(result, separators=(",", ":")))
    return result


def check_scan_fresh(last: dict[str, Any], cfg: OpportunityConfig, force: bool) -> None:
    age = age_minutes(last.get("scanned_at"))
    if (age is None or age > cfg.stale_minutes) and not force:
        raise HTTPException(409, f"This opportunity scan is stale (older than {cfg.stale_minutes} minutes). Scan again before posting.")


async def draft_from_opportunity(item: dict[str, Any], cfg: OpportunityConfig, template: str, force: bool) -> dict[str, Any]:
    symbol = item["symbol"]
    cutoff = (datetime.now(TZ) - timedelta(hours=cfg.dedupe_hours)).isoformat(timespec="seconds")
    with conn() as c:
        dupes = c.execute(
            """SELECT p.id FROM posts p JOIN signals s ON s.id=p.signal_id
               WHERE s.evidence LIKE ? AND p.created_at>=? LIMIT 1""",
            (f'%"event_key":"{item["event_key"]}"%', cutoff),
        ).fetchall()
    if dupes and not force:
        raise HTTPException(409, f"A post for this same market event already exists (draft #{dupes[0]['id']}). Edit that one, or force a new draft.")
    rows = _klines_cache.get((symbol, "1h"), (0, []))[1]
    if not rows:
        raise HTTPException(409, f"No cached 1h candles for {symbol}. Scan again.")
    sig = analyze(symbol, "1h", rows)
    if sig["side"] != item["signal_side"]:
        raise HTTPException(409, f"The {symbol} signal changed direction since the scan. Scan again.")
    signal_id = insert_signal(sig, item["event_key"])
    if template == "auto" and not item["ready"]:
        template = "watchlist_post"
    ctx = {"opportunity": {"labels": item["labels"], "rel_vol_15m": item["relative_volume"],
                           "change_1h": item["change_1h"], "change_24h": item["change_24h"]}}
    try:
        draft = await generate_one(signal_id, template, ctx)
    except Exception:
        with conn() as c:  # no orphan signal when the draft or its chart failed
            c.execute("DELETE FROM signals WHERE id=?", (signal_id,))
        raise
    return {**draft, "symbol": symbol, "signal_id": signal_id, "ready": item["ready"], "warnings": item["blockers"]}


@app.post("/api/opportunities/generate")
async def opportunity_generate(req: OpportunityGenerateRequest) -> dict[str, Any]:
    """Turn one ranked opportunity into a normal DRAFT (text + chart). Publishing stays manual."""
    cfg = opportunity_config()
    symbol = req.symbol.upper()
    last = load_opportunities()
    item = next((r for r in last.get("results", []) if r["symbol"] == symbol), None)
    if not item:
        raise HTTPException(404, f"{symbol} is not in the latest opportunity scan. Scan again.")
    check_scan_fresh(last, cfg, req.force)
    return await draft_from_opportunity(item, cfg, req.template_name, req.force)


@app.post("/api/opportunities/generate-all")
async def opportunity_generate_all(req: OpportunityGenerateAllRequest) -> dict[str, Any]:
    """One DRAFT (text + chart) per ranked opportunity in `scope`. Never publishes; duplicates are skipped."""
    cfg = opportunity_config()
    last = load_opportunities()
    check_scan_fresh(last, cfg, req.force)
    scopes = {"ready": {"publish_ready"}, "watchlist": {"publish_ready", "watchlist"}, "all": {"publish_ready", "watchlist", "low"}}
    if req.scope not in scopes:
        raise HTTPException(400, "scope must be ready, watchlist or all")
    items = [r for r in last.get("results", []) if r["group"] in scopes[req.scope]][: max(1, min(req.limit, 100))]
    if not items:
        raise HTTPException(400, "No opportunities in that scope. Scan first, or pick a wider scope.")
    results, skipped, errors = [], [], []
    for item in items:
        try:
            results.append(await draft_from_opportunity(item, cfg, "auto", req.force))
        except HTTPException as exc:
            (skipped if exc.status_code == 409 and "same market event" in str(exc.detail) else errors).append(f"{item['symbol']}: {exc.detail}")
        except Exception as exc:
            errors.append(f"{item['symbol']}: {exc}")
    return {"results": results, "skipped": skipped, "errors": errors, "considered": len(items)}


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
