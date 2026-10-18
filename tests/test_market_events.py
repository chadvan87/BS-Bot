from __future__ import annotations

from app.services.market_events import (
    closed_candles, detect_breakout, detect_retest, detect_reversal, evaluate_path, pct_change, volume_surge,
)
from tests.helpers import HOUR, T0, breakout_series, candles, now_during_last


def test_closed_candles_drops_live_candle():
    rows = candles(60)
    assert len(closed_candles(rows, now_during_last(rows))) == 59


def test_confirmed_breakout_uses_closed_candle_and_reports_volume():
    rows = breakout_series()
    bo = detect_breakout(rows, now_ms=now_during_last(rows))
    assert bo["state"] == "confirmed" and bo["direction"] == "LONG"
    assert bo["candles_ago"] == 0 and bo["volume_ratio"] >= 2.5
    assert bo["level"] < bo["close"]


def test_live_candle_above_level_is_only_potential():
    # the only break is the *live* candle: not a confirmed breakout
    rows = breakout_series(jump_at=59)
    bo = detect_breakout(rows, now_ms=now_during_last(rows))
    assert bo["state"] != "confirmed"


def test_breakout_needs_volume():
    rows = breakout_series(jump_vol=1000.0)
    assert detect_breakout(rows, min_volume_ratio=1.5, now_ms=now_during_last(rows))["state"] != "confirmed"


def test_old_breakout_is_not_fresh():
    rows = breakout_series(n=70, jump_at=50)
    assert detect_breakout(rows, now_ms=now_during_last(rows))["state"] != "confirmed"


def test_retest_is_tracked_separately_from_breakout():
    rows = breakout_series(n=62, jump_at=58)
    bo = detect_breakout(rows, now_ms=now_during_last(rows))
    assert bo["state"] == "confirmed"
    # candle after the break dips back to the level and closes above it
    level = bo["level"]
    prev = rows[59]
    t = int(prev[0])
    rows[59] = [t, prev[1], prev[2], level - 0.05, level + 0.5, 1000.0, prev[6], 1000.0 * (level + 0.5)]
    rt = detect_retest(rows, bo, now_ms=now_during_last(rows))
    assert rt["state"] == "holding"


def test_no_retest_without_breakout():
    assert detect_retest(candles(60), {"state": "none"})["state"] == "none"


def test_short_history_gives_no_claims():
    rows = candles(20)
    assert detect_breakout(rows, now_ms=now_during_last(rows))["state"] == "none"


def test_volume_surge_sustained_vs_one_off():
    base = lambda i: 1000.0
    sustained = candles(120, step=900_000, vol=lambda i: 4000.0 if i >= 117 else 1000.0, drift=lambda i: 0.3 if i >= 117 else (0.1 if i % 2 else -0.1))
    s = volume_surge(sustained, now_ms=now_during_last(sustained))
    assert s["detected"] and s["sustained"] and not s["one_off"]
    spike = candles(120, step=900_000, vol=lambda i: 4000.0 if i == 118 else 1000.0, rng=0.05, drift=lambda i: 0.01 if i % 2 else -0.01)
    o = volume_surge(spike, now_ms=now_during_last(spike))
    assert o["detected"] and o["one_off"] and not o["sustained"]


def test_volume_surge_ignores_thin_market():
    rows = candles(120, step=900_000, vol=lambda i: 0.0 if i % 3 else 500.0)
    out = volume_surge(rows, now_ms=now_during_last(rows))
    assert out["thin"] and not out["detected"]


def test_volume_baseline_uses_median_not_mean():
    # one earlier giant spike must not hide a later real surge
    rows = candles(120, step=900_000, vol=lambda i: 50000.0 if i == 40 else (3500.0 if i >= 117 else 1000.0))
    assert volume_surge(rows, now_ms=now_during_last(rows))["detected"]


def test_pct_change():
    rows = candles(30, drift=lambda i: 1.0)
    assert pct_change(rows, 1, now_during_last(rows)) > 0
    assert pct_change(candles(3), 5) is None


def test_reversal_requires_all_three_for_confirmation():
    rows = candles(40, drift=lambda i: -0.3)
    last = rows[-2]
    rows[-2] = [last[0], last[1], last[2], last[3], last[1] + 0.4, last[5], last[6], 1.0]  # bullish close
    ev = detect_reversal(rows, "LONG", 28.0, rows[-2][3], 130.0, 0.6, now_ms=now_during_last(rows))
    assert ev["rsi_extreme"] and ev["candle_turn"]
    assert not detect_reversal(rows, "LONG", 55.0, rows[-2][3], 130.0, 0.6, now_ms=now_during_last(rows))["confirmed"]


SIG = {"side": "LONG", "entry_low": 99.0, "entry_high": 100.0, "stop_loss": 97.0, "tp1": 102.0, "tp2": 104.0, "tp3": 106.0}


def row(i, o, h, l, c):
    t = T0 + i * HOUR
    return [t, o, h, l, c, 1.0, t + HOUR - 1, 1.0]


def test_path_tp_then_later_stop_keeps_target():
    r = evaluate_path(SIG, [row(0, 100, 100.5, 98.8, 99.5), row(1, 99.5, 102.5, 99.4, 102.2), row(2, 102, 102.1, 96.5, 97)])
    assert r["status"] == "TP1_HIT" and "returned to the stop" in r["note"]


def test_path_same_candle_stop_and_target_is_ambiguous():
    r = evaluate_path(SIG, [row(0, 100, 100.5, 98.8, 99.5), row(1, 99.5, 102.5, 96.5, 99)])
    assert r["status"] == "AMBIGUOUS" and "cannot show which came first" in r["note"]


def test_path_entry_candle_touching_stop_is_ambiguous():
    assert evaluate_path(SIG, [row(0, 100.5, 100.6, 96.5, 98)])["status"] == "AMBIGUOUS"


def test_path_gap_through_stop_is_sl_not_ambiguous():
    r = evaluate_path(SIG, [row(0, 100, 100.5, 98.8, 99.5), row(1, 96.0, 103.0, 95.0, 100)])
    assert r["status"] == "SL_HIT" and "Gap" in r["note"]


def test_path_gap_before_entry_invalidates():
    assert evaluate_path(SIG, [row(0, 96.0, 97.0, 95.0, 96)])["status"] == "INVALIDATED"


def test_path_waiting_and_entry():
    assert evaluate_path(SIG, [row(0, 103, 104, 101.5, 103)])["status"] == "NEW"
    assert evaluate_path(SIG, [row(0, 101, 101.5, 99.5, 100.4)])["status"] == "ENTRY_TRIGGERED"


def test_path_short_side():
    s = {"side": "SHORT", "entry_low": 100.0, "entry_high": 101.0, "stop_loss": 103.0, "tp1": 98.0, "tp2": 96.0, "tp3": 94.0}
    assert evaluate_path(s, [row(0, 100, 100.8, 99.5, 100), row(1, 100, 100.2, 97.5, 98)])["status"] == "TP1_HIT"
