"""Signal Opportunities: a ranking layer on top of the existing scanner.

Pure functions only. The browser fetches Binance public data (the server's IP may be rate-limited)
and posts it here; this module detects events, scores them and returns a ranked, explainable list.

Opportunity Score (0-100) is NOT a win probability and NOT a prediction of views. It says how
well a coin combines a valid technical setup with fresh, data-backed market interest.

Score = weighted mean of the components below, over the components that are *available*.
An unavailable component (for example historical results with fewer than `min_history` resolved
signals) is dropped and the remaining weights are re-normalised to sum to 1. It never gets free points.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.services.market_events import (
    atr_simple, closed_candles, extension_atr, pct_change, relative_volume, volume_surge,
)

WEIGHTS = {
    "technical": 0.30,
    "attention": 0.25,
    "event": 0.15,
    "liquidity": 0.15,
    "timeliness": 0.10,
    "history": 0.05,
}
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "AEUR"}
INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


@dataclass
class OpportunityConfig:
    # candidate universe
    min_quote_volume: float = 20_000_000.0   # 24h quote volume (USDT) below this = thin market
    max_candidates: int = 30                 # "top" mode: coins that get full candles
    deep_max_universe: int = 250             # "all" mode: most liquid coins that get a cheap 15m look
    stage2_size: int = 40                    # "all" mode: coins with a 15m trigger that get 1h/4h/OI
    stage2_min_rel_volume: float = 1.5       # last closed 15m volume vs median
    stage2_min_move_15m_pct: float = 1.0
    # volume surge
    surge_relative_volume: float = 2.5
    surge_baseline_candles: int = 96
    # breakout / retest
    breakout_lookback: int = 40
    fresh_max_candles: int = 3
    retest_max_candles: int = 8
    extended_atr: float = 1.5                # farther than this from the broken level = chasing
    # futures positioning
    funding_extreme: float = 0.0005          # 0.05% per funding interval
    oi_change_pct: float = 5.0               # OI move over `oi_lookback` samples
    oi_divergence_pct: float = 3.0
    oi_lookback: int = 4
    # movers
    mover_min_1h_pct: float = 1.5
    mover_min_24h_pct: float = 5.0
    exhaustion_24h_pct: float = 30.0
    manipulation_24h_pct: float = 40.0
    # liquidity
    max_spread_pct: float = 0.08
    # posting decision
    high_threshold: float = 80.0
    watch_threshold: float = 65.0
    min_signal_score: float = 60.0
    min_rr: float = 1.2
    stale_minutes: int = 90                  # scan data older than this is not publish-ready
    signal_max_age_candles: int = 3          # a stored signal older than this many candles is stale
    dedupe_hours: int = 6
    min_history: int = 5

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "OpportunityConfig":
        cfg = cls()
        for f in fields(cls):
            if raw and f.name in raw and raw[f.name] is not None:
                try:
                    setattr(cfg, f.name, type(getattr(cfg, f.name))(raw[f.name]))
                except (TypeError, ValueError):
                    pass
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _fnum(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


# ---- candidate selection --------------------------------------------------------------

def _prior(t: dict[str, Any], funding: dict[str, Any] | None, cfg: OpportunityConfig) -> float:
    """Cheap interest prior from data every coin already has in the 3 bulk calls."""
    qv, chg = _fnum(t.get("quoteVolume")) or 0.0, _fnum(t.get("priceChangePercent")) or 0.0
    score = abs(chg) * math.log10(max(qv, 1.0))
    fr = _fnum((funding or {}).get("lastFundingRate"))
    if fr is not None and abs(fr) >= cfg.funding_extreme:
        score += 5
    return score


def select_candidates(
    tickers: list[dict[str, Any]],
    cfg: OpportunityConfig,
    mode: str = "top",
    funding: dict[str, Any] | None = None,
    book: dict[str, Any] | None = None,
) -> list[str]:
    """Pick the USDT perpetuals to fetch candles for.

    "top": the `max_candidates` most active liquid coins (full candle set each).
    "all": every liquid coin up to `deep_max_universe`; the browser only gets a cheap 15m look at each
           and `shortlist_stage2` decides who earns the expensive 1h/4h/OI requests.
    Coins with a spread wider than twice the limit are dropped when bid/ask is known.
    """
    ranked: list[tuple[float, str]] = []
    for t in tickers or []:
        sym = str(t.get("symbol", "")).upper()
        qv = _fnum(t.get("quoteVolume"))
        if not sym.endswith("USDT") or "_" in sym or qv is None or qv < cfg.min_quote_volume:
            continue
        if sym[:-4] in STABLE_BASES:
            continue
        spread = _spread_pct((book or {}).get(sym))
        if mode == "all" and spread is not None and spread > cfg.max_spread_pct * 2:
            continue
        ranked.append((_prior(t, (funding or {}).get(sym), cfg), sym))
    ranked.sort(reverse=True)
    limit = cfg.deep_max_universe if mode == "all" else cfg.max_candidates
    return [s for _, s in ranked[:limit]]


def shortlist_stage2(
    klines15: dict[str, list[list[Any]]],
    tickers: list[dict[str, Any]],
    funding: dict[str, Any] | None,
    cfg: OpportunityConfig,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """From 15m candles alone, keep coins showing a fresh trigger; they alone get 1h/4h/OI requests.

    Trigger = relative volume >= stage2_min_rel_volume on a non-thin baseline, or a >= stage2_min_move_15m_pct
    15m move, or a big 24h move with rising volume, or extreme funding. Ranked by relative volume.
    """
    by_t = {str(t.get("symbol", "")).upper(): t for t in tickers or []}
    picked: list[tuple[float, str, list[str]]] = []
    for sym, rows in klines15.items():
        sym = sym.upper()
        if not rows:
            continue
        surge = volume_surge(rows, cfg.surge_baseline_candles, cfg.surge_relative_volume, now_ms=now_ms)
        rel = surge.get("relative_volume") or 0.0
        chg15 = pct_change(rows, 1, now_ms)
        chg24 = _fnum((by_t.get(sym) or {}).get("priceChangePercent"))
        fr = _fnum(((funding or {}).get(sym) or {}).get("lastFundingRate"))
        why: list[str] = []
        if not surge.get("thin") and rel >= cfg.stage2_min_rel_volume:
            why.append(f"15m volume {rel}x median")
        if chg15 is not None and abs(chg15) >= cfg.stage2_min_move_15m_pct:
            why.append(f"15m move {chg15:+.1f}%")
        if chg24 is not None and abs(chg24) >= cfg.mover_min_24h_pct and rel >= 1.2:
            why.append(f"24h {chg24:+.1f}% with rising volume")
        if fr is not None and abs(fr) >= cfg.funding_extreme:
            why.append(f"funding {fr * 100:+.3f}%")
        if why:
            picked.append((rel + 0.01 * len(why), sym, why))
    picked.sort(reverse=True)
    chosen = picked[: cfg.stage2_size]
    return {"symbols": [s for _, s, _ in chosen], "why": {s: w for _, s, w in chosen},
            "checked": len(klines15), "triggered": len(picked), "dropped_by_cap": max(len(picked) - len(chosen), 0)}


# ---- staleness / duplicates -------------------------------------------------------------

def is_stale_signal(created_at: str, timeframe: str, cfg: OpportunityConfig, now: datetime | None = None) -> bool:
    """A stored signal is stale once more than `signal_max_age_candles` candles have closed since it was made."""
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    minutes = INTERVAL_MINUTES.get(timeframe, 60)
    return now - created > timedelta(minutes=minutes * cfg.signal_max_age_candles)


def age_minutes(iso: str | None, now: datetime | None = None) -> float | None:
    try:
        then = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - then).total_seconds() / 60


# ---- scoring pieces ---------------------------------------------------------------------

def _liquidity(qv: float, spread_pct: float | None, cfg: OpportunityConfig) -> tuple[float, str]:
    """0-100 from 24h quote volume (log scale around the minimum), scaled down by a wide spread."""
    ratio = qv / cfg.min_quote_volume
    if ratio < 1:
        vol_score = 40 * ratio
    elif ratio < 10:
        vol_score = 40 + 40 * math.log10(ratio)
    else:
        vol_score = 80 + 20 * min(1.0, math.log10(ratio / 10) / math.log10(5))
    if spread_pct is None:
        return vol_score, f"24h volume ${qv / 1e6:.0f}M; spread unavailable"
    good = cfg.max_spread_pct * 0.25
    factor = 1.0 if spread_pct <= good else _clamp((cfg.max_spread_pct * 1.25 - spread_pct) / cfg.max_spread_pct, 0, 1)
    return vol_score * factor, f"24h volume ${qv / 1e6:.0f}M; spread {spread_pct:.3f}%"


def _spread_pct(book: dict[str, Any] | None) -> float | None:
    if not book:
        return None
    bid, ask = _fnum(book.get("bidPrice")), _fnum(book.get("askPrice"))
    if not bid or not ask or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def _oi_change(hist: list[dict[str, Any]] | None, lookback: int) -> float | None:
    vals = [_fnum(h.get("sumOpenInterestValue") or h.get("sumOpenInterest")) for h in (hist or [])]
    vals = [v for v in vals if v is not None]
    if len(vals) <= lookback or vals[-1 - lookback] <= 0:
        return None
    return (vals[-1] - vals[-1 - lookback]) / vals[-1 - lookback] * 100


def history_score(history: dict[str, int] | None, cfg: OpportunityConfig) -> tuple[float | None, str]:
    """Share of this coin's *own past resolved signals* that reached a target (Laplace-smoothed)."""
    if not history:
        return None, "no resolved past signals for this coin"
    tp, sl = int(history.get("tp", 0)), int(history.get("sl", 0))
    if tp + sl < cfg.min_history:
        return None, f"only {tp + sl} resolved past signals (need {cfg.min_history})"
    return (tp + 1) / (tp + sl + 2) * 100, f"{tp} target hits vs {sl} stops in past signals"


def combine(components: dict[str, float | None]) -> tuple[float, dict[str, dict[str, Any]]]:
    """Weighted mean over available components. Returns the score and a per-component breakdown."""
    avail = {k: v for k, v in components.items() if v is not None}
    total_w = sum(WEIGHTS[k] for k in avail)
    breakdown: dict[str, dict[str, Any]] = {}
    score = 0.0
    for k, w in WEIGHTS.items():
        v = components.get(k)
        if v is None or total_w <= 0:
            breakdown[k] = {"value": None, "weight": w, "effective_weight": 0.0, "contribution": 0.0}
            continue
        eff = w / total_w
        score += eff * v
        breakdown[k] = {"value": round(v, 1), "weight": w, "effective_weight": round(eff, 3), "contribution": round(eff * v, 1)}
    return round(score, 1), breakdown


# ---- main evaluation ---------------------------------------------------------------------

def evaluate_symbol(
    symbol: str,
    data: dict[str, Any],
    cfg: OpportunityConfig,
    analyze_fn: Callable[[str, str, list[list[Any]]], dict[str, Any]],
    history: dict[str, int] | None = None,
    now_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return (opportunity, "") or (None, reason it was skipped)."""
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    ticker = data["tickers"].get(symbol) or {}
    qv = _fnum(ticker.get("quoteVolume"))
    if qv is None:
        return None, "no 24h ticker"
    if qv < cfg.min_quote_volume:
        return None, f"thin market: 24h volume ${qv / 1e6:.1f}M below ${cfg.min_quote_volume / 1e6:.0f}M"
    k = data["klines"].get(symbol) or {}
    rows1h, rows15, rows4h = k.get("1h") or [], k.get("15m") or [], k.get("4h") or []
    if len(rows1h) < 50:
        return None, "missing 1h candles"
    try:
        signal = analyze_fn(symbol, "1h", rows1h)
    except Exception as exc:
        return None, f"analysis failed: {exc}"
    ev = signal.get("evidence") or {}
    side = signal["side"]
    price = signal["price"]
    atr = signal.get("atr") or atr_simple(closed_candles(rows1h))
    reasons: list[str] = []
    risks: list[str] = []
    missing: list[str] = []
    labels: list[str] = []

    # ---- detectors ----
    surge = volume_surge(rows15, cfg.surge_baseline_candles, cfg.surge_relative_volume, now_ms=now_ms) if rows15 else {"available": False}
    if not rows15:
        missing.append("15m candles")
    br, rt = ev.get("breakout") or {}, ev.get("retest") or {}
    breakout_ok = br.get("state") == "confirmed" and br.get("direction") == side
    ext = extension_atr(price, br.get("level"), atr) if br.get("level") is not None and br.get("state") == "confirmed" else None
    extended = ext is not None and ext > cfg.extended_atr

    change_24h = _fnum(ticker.get("priceChangePercent"))
    change_1h = pct_change(rows1h, 1, now_ms)
    change_15m = pct_change(rows15, 1, now_ms) if rows15 else None
    meaningful_move = (change_1h is not None and abs(change_1h) >= cfg.mover_min_1h_pct) or (change_24h is not None and abs(change_24h) >= cfg.mover_min_24h_pct)
    rv3: list[float] = []
    if surge.get("baseline"):
        rv3 = [float(r[5]) / surge["baseline"] for r in closed_candles(rows15, now_ms)[-3:]]
    accelerating = len(rv3) == 3 and rv3[-1] >= 1.5 and rv3[-1] >= rv3[-2] and not surge.get("one_off") and not surge.get("thin")
    rsi_v = signal.get("rsi")
    exhausted = (change_24h is not None and abs(change_24h) >= cfg.exhaustion_24h_pct) or (rsi_v is not None and (rsi_v > 85 or rsi_v < 15))
    spread = _spread_pct(data["book"].get(symbol))
    if spread is None:
        missing.append("bid/ask spread")
    manipulation = (spread is not None and spread > cfg.max_spread_pct) or (
        change_24h is not None and abs(change_24h) >= cfg.manipulation_24h_pct and qv < cfg.min_quote_volume * 5)

    funding = _fnum((data["funding"].get(symbol) or {}).get("lastFundingRate"))
    oi_chg = _oi_change(data["oi_hist"].get(symbol), cfg.oi_lookback)
    futures_missing = funding is None and oi_chg is None
    if funding is None:
        missing.append("funding rate")
    if oi_chg is None:
        missing.append("open interest")
    price_chg_window = pct_change(rows1h, cfg.oi_lookback, now_ms)
    funding_extreme = funding is not None and abs(funding) >= cfg.funding_extreme
    oi_anomaly = oi_chg is not None and abs(oi_chg) >= cfg.oi_change_pct
    divergence = (oi_chg is not None and price_chg_window is not None and (
        (price_chg_window >= 1.0 and oi_chg <= -cfg.oi_divergence_pct) or (price_chg_window <= -1.0 and oi_chg >= cfg.oi_divergence_pct)))
    crowded = funding_extreme and ((funding > 0 and side == "LONG") or (funding < 0 and side == "SHORT"))

    # ---- labels, reasons, risks ----
    if surge.get("detected") and surge.get("sustained"):
        labels.append("unusual_volume")
        reasons.append(f"15m volume is {surge['relative_volume']}x its {cfg.surge_baseline_candles}-candle median, sustained over several candles.")
    elif surge.get("detected"):
        labels.append("one_off_spike")
        risks.append(f"15m volume spiked {surge['relative_volume']}x but on a single candle with little price move; treated as weak.")
    if surge.get("thin"):
        risks.append("15m baseline has many empty candles: thin market, volume ratios are unreliable.")
    if breakout_ok:
        labels.append("breakout")
        reasons.append(f"Closed 1h candle {'above' if side == 'LONG' else 'below'} {br['level']} ({br.get('candles_ago', 0)} candle(s) ago, volume {br.get('volume_ratio')}x).")
        if rt.get("state") == "holding":
            labels.append("retest")
            reasons.append("A retest of the broken level held on a closed candle.")
    elif br.get("state") == "potential" and br.get("direction") == side:
        labels.append("potential_breakout")
        reasons.append(f"Price is pressing {br['level']}; no closed 1h candle beyond it yet.")
    elif br.get("state") == "confirmed":
        risks.append("A breakout exists but against the signal direction.")
    if (ev.get("reversal") or {}).get("confirmed"):
        labels.append("reversal")
        reasons.append("Stretched RSI at a key level with a closed-candle turn.")
    if meaningful_move and (accelerating or surge.get("sustained")):
        labels.append("mover")
        reasons.append(f"Meaningful move ({_fmt(change_1h)} 1h, {_fmt(change_24h)} 24h) with accelerating volume.")
    if oi_anomaly:
        labels.append("oi_anomaly")
        reasons.append(f"Open interest changed {oi_chg:+.1f}% over {cfg.oi_lookback}h.")
    if divergence:
        labels.append("price_oi_divergence")
        risks.append("Price and open interest are moving in opposite directions.")
    if funding_extreme:
        labels.append("funding_extreme")
        risks.append(f"Funding rate is {funding * 100:+.3f}%, an extreme reading.")
    if crowded:
        risks.append(f"Positioning looks crowded on the {side} side.")
    if extended:
        risks.append(f"Price is already {ext:.1f} ATR past the broken level; chasing risk.")
    if exhausted:
        labels.append("exhausted")
        risks.append("The move looks exhausted (very large 24h move or extreme RSI).")
    if manipulation:
        labels.append("manipulation_risk")
        risks.append("Wide spread or a huge move on modest volume: manipulation risk.")
    if rows15 and not surge.get("available"):
        missing.append("15m volume baseline")

    # 4h agreement
    htf = None
    if len(rows4h) >= 50:
        closes4 = [float(r[4]) for r in rows4h]
        e20, e50 = _ema(closes4[-80:], 20), _ema(closes4[-120:], 50)
        htf = "LONG" if e20 > e50 else "SHORT"
        (reasons if htf == side else risks).append(f"4h EMA20/50 {'agrees with' if htf == side else 'disagrees with'} the {side} bias.")
    else:
        missing.append("4h candles")

    # ---- components ----
    tech = signal["score"] / 95 * 100
    tech += 8 if htf == side else (-10 if htf and htf != side else 0)
    tech -= 10 if crowded else 0
    technical = _clamp(tech)

    attention: float | None = None
    if surge.get("available") or meaningful_move:
        rel = surge.get("relative_volume") or 1.0
        a = _clamp((rel - 1) / 3 * 100)
        if meaningful_move:
            a += 20 if accelerating else 10
        if surge.get("one_off"):
            a *= 0.5
        if surge.get("thin"):
            a *= 0.6
        attention = _clamp(a)

    event = 0.0
    if breakout_ok:
        event = 80 + (15 if rt.get("state") == "holding" else 0)
    elif "reversal" in labels:
        event = 60
    elif "potential_breakout" in labels:
        event = 35
    if oi_anomaly:
        event = max(event, 50)
    if funding_extreme:
        event = max(event, 30)
    event = _clamp(event)

    liq, liq_note = _liquidity(qv, spread, cfg)
    last_close = closed_candles(rows1h, now_ms)
    data_age_min = (now_ms - int(last_close[-1][6])) / 60000 if last_close and len(last_close[-1]) > 6 else None
    # candle close times can sit up to one candle in the past; only data older than a candle + slack is stale
    stale = data_age_min is not None and data_age_min > 60 + cfg.stale_minutes
    time_v = 20.0
    if breakout_ok:
        time_v = {0: 100, 1: 75, 2: 50}.get(br.get("candles_ago", 3), 25)
    elif surge.get("detected") and surge.get("sustained"):
        time_v = 85
    elif "mover" in labels:
        time_v = 70
    if extended:
        time_v *= 0.4
    if exhausted:
        time_v *= 0.5
    if stale:
        time_v *= 0.3
        risks.append("Candle data is stale.")

    hist_v, hist_note = history_score(history, cfg)
    components = {"technical": technical, "attention": attention, "event": event, "liquidity": liq,
                  "timeliness": _clamp(time_v), "history": hist_v}
    score, breakdown = combine(components)
    breakdown["technical"]["note"] = f"signal score {signal['score']}/95" + (f", 4h {'agrees' if htf == side else 'disagrees'}" if htf else "")
    breakdown["attention"]["note"] = "needs 15m candles" if attention is None else f"relative volume {surge.get('relative_volume')}x"
    breakdown["event"]["note"] = "Binance market events only (breakout, retest, OI, funding). No news source is connected, so no catalyst is credited."
    breakdown["liquidity"]["note"] = liq_note
    breakdown["timeliness"]["note"] = f"event age {br.get('candles_ago')} candle(s)" if breakout_ok else "no fresh price event"
    breakdown["history"]["note"] = hist_note

    # ---- publish-ready gate ----
    geometry = _geometry_ok(signal, cfg)
    trigger = breakout_ok or "unusual_volume" in labels or "mover" in labels or "reversal" in labels
    blockers: list[str] = []
    if not geometry[0]:
        blockers.append(geometry[1])
    if signal["score"] < cfg.min_signal_score:
        blockers.append(f"technical score {signal['score']} below {cfg.min_signal_score:.0f}")
    if not trigger:
        blockers.append("no fresh event: confirmed breakout, sustained volume surge, mover or reversal")
    for flag, text in ((extended, "extended past the entry"), (exhausted, "move exhausted"), (manipulation, "manipulation risk"),
                       (stale, "stale data"), (br.get("state") == "confirmed" and not breakout_ok, "breakout against signal direction")):
        if flag:
            blockers.append(text)
    ready = not blockers
    if not ready:
        score = min(score, cfg.high_threshold - 1)  # attention can never promote an invalid setup
    tier = "high" if score >= cfg.high_threshold else "watchlist" if score >= cfg.watch_threshold else "low"
    group = "publish_ready" if ready and tier == "high" else ("watchlist" if tier != "low" else "low")

    if breakout_ok:
        event_key = f"{symbol}:breakout:{side}:{br['level']}"
    elif surge.get("detected") and rows15:
        event_key = f"{symbol}:surge:{closed_candles(rows15, now_ms)[-1][0]}"
    else:
        event_key = f"{symbol}:signal:{side}:{closed_candles(rows1h, now_ms)[-1][0] if closed_candles(rows1h, now_ms) else 0}"

    return {
        "symbol": symbol, "direction": side if ready else "WATCH", "signal_side": side, "ready": ready,
        "tier": tier, "group": group, "opportunity_score": score, "technical_score": signal["score"],
        "relative_volume": surge.get("relative_volume"),
        "change_15m": _r(change_15m), "change_1h": _r(change_1h), "change_24h": _r(change_24h),
        "labels": labels, "blockers": blockers, "reasons": reasons, "risks": risks, "missing": missing,
        "liquidity": {"quote_volume_24h": qv, "spread_pct": _r(spread, 4), "spot": symbol in data["spot"], "futures": True},
        "funding_rate": funding, "oi_change_pct": _r(oi_chg), "components": breakdown,
        "event_key": event_key, "price": price, "futures_data_missing": futures_missing,
        "signal": {k: signal.get(k) for k in ("side", "timeframe", "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3", "score")},
        "stale": stale,
    }, ""


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _geometry_ok(signal: dict[str, Any], cfg: OpportunityConfig) -> tuple[bool, str]:
    from app.services.post_templates import risk_reward
    rr = risk_reward(signal)
    if rr is None:
        return False, "levels do not form a valid stop/target geometry"
    if rr < cfg.min_rr:
        return False, f"risk/reward {rr}:1 below {cfg.min_rr}:1"
    return True, ""


def rank_opportunities(
    raw: dict[str, Any],
    cfg: OpportunityConfig,
    analyze_fn: Callable[[str, str, list[list[Any]]], dict[str, Any]],
    history: dict[str, dict[str, int]] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Score every symbol present in `raw` and return them ranked, split into groups."""
    data = {
        "tickers": {str(t.get("symbol", "")).upper(): t for t in raw.get("tickers", [])},
        "klines": {k.upper(): v for k, v in (raw.get("klines") or {}).items()},
        "funding": {k.upper(): v for k, v in (raw.get("funding") or {}).items()},
        "oi_hist": {k.upper(): v for k, v in (raw.get("oi_hist") or {}).items()},
        "book": {k.upper(): v for k, v in (raw.get("book") or {}).items()},
        "spot": {s.upper() for s in raw.get("spot_symbols", [])},
    }
    results, skipped = [], []
    symbols = [s.upper() for s in (raw.get("symbols") or data["klines"].keys())]
    for sym in symbols:
        try:
            item, reason = evaluate_symbol(sym, data, cfg, analyze_fn, (history or {}).get(sym), now_ms)
        except Exception as exc:  # one bad symbol must not sink the scan
            item, reason = None, f"error: {exc}"
        if item:
            results.append(item)
        else:
            skipped.append({"symbol": sym, "reason": reason})
    order = {"publish_ready": 0, "watchlist": 1, "low": 2}
    results.sort(key=lambda r: (order[r["group"]], -r["opportunity_score"]))
    return {"results": results, "skipped": skipped,
            "scanned_at": datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="seconds"),
            "config": cfg.to_dict(),
            "notes": ["Opportunity Score is not a win probability or a prediction of views.",
                      "No news or on-chain source is connected; catalysts are not credited."]}
