"""Deterministic market-structure detectors shared by analyze() and the opportunity scanner.

Every function works on Binance kline rows
`[open_time, open, high, low, close, volume, close_time, quote_volume, ...]`
and never invents data: if a field is missing the detector reports "none" / None.
Breakout claims use closed candles only. The last row from Binance is normally the live,
unfinished candle, so it is dropped before any "confirmed" claim.
"""
from __future__ import annotations

import time
from statistics import median
from typing import Any

Row = list[Any]


def closed_candles(rows: list[Row], now_ms: int | None = None) -> list[Row]:
    """Rows whose candle has finished. Without close times, assume the last row is live."""
    if not rows:
        return []
    if len(rows[-1]) > 6:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            return [r for r in rows if int(r[6]) < now]
        except (TypeError, ValueError):
            pass
    return rows[:-1]


def _f(row: Row, idx: int) -> float:
    return float(row[idx])


def relative_volume(volumes: list[float], lookback: int = 20) -> float | None:
    """Last volume divided by the mean of the `lookback` volumes before it."""
    if len(volumes) < 3:
        return None
    base = volumes[-1 - lookback:-1] if len(volumes) > lookback else volumes[:-1]
    mean = sum(base) / len(base) if base else 0.0
    return volumes[-1] / mean if mean > 0 else None


def atr_simple(rows: list[Row], period: int = 14) -> float:
    """Plain average true range over the last `period` candles (enough for distance checks)."""
    sub = rows[-(period + 1):]
    if len(sub) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(sub[:-1], sub[1:]):
        h, l, pc = _f(cur, 2), _f(cur, 3), _f(prev, 4)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def detect_breakout(
    rows: list[Row],
    lookback: int = 40,
    fresh_max_candles: int = 3,
    near_atr: float = 0.3,
    min_volume_ratio: float = 1.2,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Find a fresh, closed-candle break of the prior `lookback`-candle high/low.

    state:
      "confirmed" – a closed candle within the last `fresh_max_candles` closed above the prior high
                    (direction LONG) or below the prior low (SHORT) with volume >= min_volume_ratio x average
      "potential" – no closed break, but the live price is within `near_atr` x ATR of the level
      "none"      – neither
    """
    closed = closed_candles(rows, now_ms)
    out: dict[str, Any] = {"state": "none", "direction": None, "level": None, "candles_ago": None,
                           "volume_ratio": None, "close": None, "closed_count": len(closed)}
    if len(closed) < lookback + 2:
        return out
    atr = atr_simple(closed)
    volumes = [_f(r, 5) for r in closed]
    for ago in range(fresh_max_candles):  # 0 = latest closed candle
        idx = len(closed) - 1 - ago
        window = closed[idx - lookback:idx]
        if len(window) < lookback:
            break
        prior_high = max(_f(r, 2) for r in window)
        prior_low = min(_f(r, 3) for r in window)
        close = _f(closed[idx], 4)
        prev_close = _f(closed[idx - 1], 4)
        vr = relative_volume(volumes[:idx + 1])
        vol_ok = vr is not None and vr >= min_volume_ratio
        if close > prior_high and prev_close <= prior_high and vol_ok:
            return {**out, "state": "confirmed", "direction": "LONG", "level": prior_high,
                    "candles_ago": ago, "volume_ratio": round(vr, 2), "close": close}
        if close < prior_low and prev_close >= prior_low and vol_ok:
            return {**out, "state": "confirmed", "direction": "SHORT", "level": prior_low,
                    "candles_ago": ago, "volume_ratio": round(vr, 2), "close": close}
    # Not yet broken on a closed candle: report a potential breakout if price is pressing a level.
    window = closed[-lookback:]
    hi, lo = max(_f(r, 2) for r in window), min(_f(r, 3) for r in window)
    live = _f(rows[-1], 4)
    if atr > 0:
        if 0 <= hi - live <= near_atr * atr or (live > hi and _f(closed[-1], 4) <= hi):
            return {**out, "state": "potential", "direction": "LONG", "level": hi}
        if 0 <= live - lo <= near_atr * atr or (live < lo and _f(closed[-1], 4) >= lo):
            return {**out, "state": "potential", "direction": "SHORT", "level": lo}
    return out


def detect_retest(
    rows: list[Row],
    breakout: dict[str, Any],
    retest_max_candles: int = 8,
    tolerance_atr: float = 0.3,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """After a confirmed break, did a later closed candle come back to the level and hold?

    A holding retest: low (LONG) / high (SHORT) reaches within `tolerance_atr` x ATR of the broken
    level while the candle closes back on the breakout side. Reported separately from the break.
    """
    none = {"state": "none", "candles_ago": None}
    if breakout.get("state") != "confirmed" or breakout.get("candles_ago") is None:
        return none
    closed = closed_candles(rows, now_ms)
    atr = atr_simple(closed)
    if atr <= 0:
        return none
    level, direction = breakout["level"], breakout["direction"]
    break_idx = len(closed) - 1 - breakout["candles_ago"]
    for idx in range(break_idx + 1, min(len(closed), break_idx + 1 + retest_max_candles)):
        low, high, close = _f(closed[idx], 3), _f(closed[idx], 2), _f(closed[idx], 4)
        if direction == "LONG" and low <= level + tolerance_atr * atr and close > level:
            return {"state": "holding", "candles_ago": len(closed) - 1 - idx}
        if direction == "SHORT" and high >= level - tolerance_atr * atr and close < level:
            return {"state": "holding", "candles_ago": len(closed) - 1 - idx}
        if (direction == "LONG" and close < level - tolerance_atr * atr) or (
            direction == "SHORT" and close > level + tolerance_atr * atr
        ):
            return {"state": "failed", "candles_ago": len(closed) - 1 - idx}
    return none


def extension_atr(price: float, level: float | None, atr: float) -> float | None:
    """How many ATRs the price sits away from a reference level (None if unknown)."""
    if level is None or atr <= 0:
        return None
    return abs(price - level) / atr


def detect_reversal(rows: list[Row], side: str, rsi_value: float | None, support: float, resistance: float,
                    atr: float, now_ms: int | None = None) -> dict[str, Any]:
    """Reversal evidence for `side`: stretched RSI, price at the matching level, closed-candle turn."""
    closed = closed_candles(rows, now_ms)
    ev: dict[str, Any] = {"rsi_extreme": False, "at_level": False, "candle_turn": False, "confirmed": False}
    if rsi_value is not None:
        ev["rsi_extreme"] = rsi_value < 35 if side == "LONG" else rsi_value > 65
    if len(closed) >= 2 and atr > 0:
        last, prev = closed[-1], closed[-2]
        if side == "LONG":
            ev["at_level"] = _f(last, 3) - support <= 0.5 * atr
            ev["candle_turn"] = _f(last, 4) > _f(last, 1) and _f(last, 4) > _f(prev, 4)
        else:
            ev["at_level"] = resistance - _f(last, 2) <= 0.5 * atr
            ev["candle_turn"] = _f(last, 4) < _f(last, 1) and _f(last, 4) < _f(prev, 4)
    ev["confirmed"] = ev["rsi_extreme"] and ev["at_level"] and ev["candle_turn"]
    return ev


def volume_surge(rows: list[Row], baseline_candles: int = 96, threshold: float = 2.5,
                 min_active_fraction: float = 0.8, now_ms: int | None = None) -> dict[str, Any]:
    """Unusual-volume detector on closed candles with thin-market / one-off-spike filters.

    Baseline is the *median* of the previous `baseline_candles` closed candles, so earlier spikes
    do not inflate it. A surge counts as `sustained` only when 2 of the last 3 closed candles are
    >= 1.5x baseline, or the spike candle moved price by >= 0.5 ATR. Otherwise it is flagged
    `one_off`. A baseline where fewer than `min_active_fraction` of candles traded is `thin`.
    """
    closed = closed_candles(rows, now_ms)
    out: dict[str, Any] = {"detected": False, "relative_volume": None, "sustained": False,
                           "one_off": False, "thin": False, "baseline": None, "available": False}
    if len(closed) < 30:
        return out
    vols = [_f(r, 5) for r in closed]
    base = vols[-1 - baseline_candles:-1] if len(vols) > baseline_candles else vols[:-1]
    if not base:
        return out
    out["available"] = True
    base_med = median(base)
    active = sum(1 for v in base if v > 0) / len(base)
    if base_med <= 0 or active < min_active_fraction:
        out["thin"] = True
        return out
    out["baseline"] = base_med
    rel = vols[-1] / base_med
    out["relative_volume"] = round(rel, 2)
    if rel < threshold:
        return out
    out["detected"] = True
    rel3 = [v / base_med for v in vols[-3:]]
    atr = atr_simple(closed)
    last = closed[-1]
    moved = atr > 0 and abs(_f(last, 4) - _f(last, 1)) >= 0.5 * atr
    if sum(1 for r in rel3 if r >= 1.5) >= 2 or moved:
        out["sustained"] = True
    else:
        out["one_off"] = True
    return out


def pct_change(rows: list[Row], candles_back: int, now_ms: int | None = None) -> float | None:
    """Percent change of the live price versus the close of the candle `candles_back` closed candles ago."""
    closed = closed_candles(rows, now_ms)
    if candles_back < 1 or len(closed) < candles_back or not rows:
        return None
    ref = _f(closed[-candles_back], 4)
    return (_f(rows[-1], 4) - ref) / ref * 100 if ref else None


def evaluate_path(signal: dict[str, Any], candles: list[Row]) -> dict[str, Any]:
    """Walk candles *after* the signal was created and decide what happened, without guessing order.

    `candles` must start at or after the signal time. Returns:
      status  – NEW / ENTRY_TRIGGERED / TP1_HIT / TP2_HIT / TP3_HIT / SL_HIT / INVALIDATED / AMBIGUOUS
      note    – explanation for ambiguous, gap or stop-after-target cases
      at      – open_time (ms) of the candle that produced the status
    A candle that touches both the stop and a target (or entry and stop) is AMBIGUOUS: candle data
    cannot say which came first. A candle that *opens* beyond a level (gap) resolves on that side.
    After a target is reached, a later stop touch does not erase it; the note records it.
    """
    long = signal["side"] == "LONG"
    lo, hi, sl = float(signal["entry_low"]), float(signal["entry_high"]), float(signal["stop_loss"])
    tps = [float(signal[k]) for k in ("tp1", "tp2", "tp3")]
    entered = False
    best = -1  # index of the furthest target reached
    result: dict[str, Any] = {"status": "NEW", "note": "", "at": None}
    for c in candles:
        o, h, l = _f(c, 1), _f(c, 2), _f(c, 3)
        t = int(c[0])
        entered_now = False
        if not entered:
            if (long and o <= sl) or (not long and o >= sl):
                return {"status": "INVALIDATED", "note": "Gap through the stop before entry.", "at": t}
            if not (l <= hi if long else h >= lo):
                if (long and l <= sl) or (not long and h >= sl):
                    return {"status": "INVALIDATED", "note": "Stop level traded before entry.", "at": t}
                continue
            entered = entered_now = True
            result = {"status": "ENTRY_TRIGGERED", "note": "", "at": t}
        hit_sl = l <= sl if long else h >= sl
        gap_sl = o <= sl if long else o >= sl
        reached = [i for i, tp in enumerate(tps) if (h >= tp if long else l <= tp)]
        new_reached = [i for i in reached if i > best]
        if hit_sl and not gap_sl and best < 0 and (new_reached or entered_now):
            what = f"TP{new_reached[-1] + 1}" if new_reached else "entry"
            return {"status": "AMBIGUOUS", "at": t,
                    "note": f"Price touched both the stop and {what} within one candle; "
                            "candle data cannot show which came first."}
        if hit_sl and best < 0:
            return {"status": "SL_HIT", "at": t, "note": "Gap through the stop." if gap_sl else ""}
        if new_reached:
            best = new_reached[-1]
            result = {"status": f"TP{best + 1}_HIT", "at": t, "note": ""}
        if hit_sl and best >= 0:
            result["note"] = "Price later returned to the stop level after the target was reached."
            return result
    return result
