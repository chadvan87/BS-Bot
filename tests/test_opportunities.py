from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.opportunities import (
    WEIGHTS, OpportunityConfig, combine, history_score, is_stale_signal, rank_opportunities, select_candidates,
    shortlist_stage2,
)
from tests.helpers import HOUR, breakout_series, candles, now_during_last

CFG = OpportunityConfig()


def make_signal(symbol="AAAUSDT", side="LONG", score=82, **kw):
    sig = {
        "symbol": symbol, "side": side, "timeframe": "1h", "score": score, "price": 103.0, "atr": 1.0, "rsi": 60.0,
        "entry_low": 102.7, "entry_high": 103.1, "stop_loss": 101.5, "tp1": 104.5, "tp2": 106.0, "tp3": 108.0,
        "evidence": {"breakout": {"state": "confirmed", "direction": side, "level": 102.5, "candles_ago": 0, "volume_ratio": 2.4},
                     "retest": {"state": "none"}, "reversal": {}},
    }
    sig.update(kw)
    return sig


def analyze_stub(signals):
    def fn(symbol, interval, rows):
        return signals[symbol]
    return fn


def surge_15m(end_open, sustained=True, n=130):
    t0 = end_open - (n - 1) * 900_000
    if sustained:
        return candles(n, step=900_000, t0=t0, vol=lambda i: 4500.0 if i >= n - 3 else 1000.0, drift=lambda i: 0.3 if i >= n - 3 else (0.1 if i % 2 else -0.1))
    return candles(n, step=900_000, t0=t0, vol=lambda i: 4500.0 if i == n - 2 else 1000.0, rng=0.05, drift=lambda i: 0.01 if i % 2 else -0.01)


def raw_for(symbols, qv="90000000", **extra):
    h = breakout_series(120, jump_at=118)
    now = now_during_last(h)
    end = int(h[-1][0]) + 900_000  # the 15m candle that is live right now
    raw = {
        "tickers": [{"symbol": s, "quoteVolume": qv, "priceChangePercent": "6.0"} for s in symbols],
        "klines": {s: {"1h": h, "15m": surge_15m(end), "4h": candles(100, step=4 * HOUR, drift=lambda i: 0.3)} for s in symbols},
        "funding": {s: {"lastFundingRate": "0.0001"} for s in symbols},
        "oi_hist": {s: [{"sumOpenInterestValue": str(1000 + i * 5)} for i in range(12)] for s in symbols},
        "book": {s: {"bidPrice": "100.00", "askPrice": "100.01"} for s in symbols},
        "spot_symbols": list(symbols),
    }
    raw.update(extra)
    return raw, now


def test_ready_breakout_ranks_above_watchlist_coin():
    sigs = {"AAAUSDT": make_signal("AAAUSDT"), "BBBUSDT": make_signal("BBBUSDT", score=50, evidence={"breakout": {"state": "none"}})}
    raw, now = raw_for(list(sigs))
    out = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)
    top = out["results"][0]
    assert top["symbol"] == "AAAUSDT" and top["ready"] and top["direction"] == "LONG"
    other = next(r for r in out["results"] if r["symbol"] == "BBBUSDT")
    assert not other["ready"] and other["direction"] == "WATCH" and other["group"] != "publish_ready"
    assert "breakout" in top["labels"] and "unusual_volume" in top["labels"]
    assert any("Closed 1h candle above" in r for r in top["reasons"])


def test_high_attention_cannot_promote_a_low_quality_setup():
    sigs = {"AAAUSDT": make_signal(score=40)}
    raw, now = raw_for(list(sigs))
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert not item["ready"] and item["opportunity_score"] < CFG.high_threshold
    assert item["group"] != "publish_ready" and any("technical score" in b for b in item["blockers"])


def test_invalid_trade_geometry_is_not_publish_ready():
    sigs = {"AAAUSDT": make_signal(stop_loss=104.0)}
    raw, now = raw_for(list(sigs))
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert not item["ready"] and any("geometry" in b for b in item["blockers"])


def test_extended_move_is_penalised_and_blocked():
    sigs = {"AAAUSDT": make_signal(price=110.0)}  # level 102.5, atr 1 -> 7.5 ATR away
    raw, now = raw_for(list(sigs))
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert not item["ready"] and any("extended" in b for b in item["blockers"])


def test_breakout_against_signal_direction_is_blocked():
    sigs = {"AAAUSDT": make_signal(side="SHORT", entry_low=102.9, entry_high=103.3, stop_loss=104.5, tp1=101.5, tp2=100.0, tp3=98.0,
                                   evidence={"breakout": {"state": "confirmed", "direction": "LONG", "level": 102.5, "candles_ago": 0, "volume_ratio": 2.0}})}
    raw, now = raw_for(list(sigs))
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert not item["ready"] and "breakout" not in item["labels"]


def test_one_off_spike_is_flagged_and_not_trusted():
    sigs = {"AAAUSDT": make_signal(evidence={"breakout": {"state": "none"}})}
    raw, now = raw_for(list(sigs))
    raw["klines"]["AAAUSDT"]["15m"] = surge_15m(int(raw["klines"]["AAAUSDT"]["1h"][-1][0]) + 900_000, sustained=False)
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert "one_off_spike" in item["labels"] and "unusual_volume" not in item["labels"]
    assert not item["ready"]


def test_thin_market_is_skipped_with_reason():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs), qv="1000000")
    out = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)
    assert out["results"] == [] and "thin market" in out["skipped"][0]["reason"]


def test_missing_futures_and_15m_data_degrades_gracefully():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs))
    raw["tickers"][0]["priceChangePercent"] = "0.5"  # no big 24h move either, so there is nothing to build attention from
    raw["funding"], raw["oi_hist"], raw["book"] = {}, {}, {}
    del raw["klines"]["AAAUSDT"]["15m"]
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    for gap in ("funding rate", "open interest", "15m candles", "bid/ask spread"):
        assert gap in item["missing"]
    assert item["components"]["attention"]["value"] is None            # excluded, not zero-filled with free points
    assert item["components"]["attention"]["effective_weight"] == 0
    total = sum(c["effective_weight"] for c in item["components"].values())
    assert total == pytest.approx(1.0, abs=0.01)


def test_missing_1h_candles_is_skipped_not_crashing():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs))
    raw["klines"]["AAAUSDT"]["1h"] = raw["klines"]["AAAUSDT"]["1h"][:10]
    out = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)
    assert out["results"] == [] and "missing 1h" in out["skipped"][0]["reason"]


def test_one_bad_symbol_does_not_sink_the_scan():
    def boom(symbol, interval, rows):
        if symbol == "BADUSDT":
            raise ValueError("bad data")
        return make_signal(symbol)
    raw, now = raw_for(["BADUSDT", "AAAUSDT"])
    out = rank_opportunities(raw, CFG, boom, now_ms=now)
    assert [r["symbol"] for r in out["results"]] == ["AAAUSDT"] and "bad data" in out["skipped"][0]["reason"]


def test_stale_candle_data_blocks_publish_ready():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs))
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now + 6 * HOUR)["results"][0]
    assert not item["ready"] and "stale data" in item["blockers"]


def test_combine_renormalises_without_free_points():
    score, parts = combine({"technical": 80, "attention": 60, "event": 40, "liquidity": 100, "timeliness": 50, "history": None})
    expected = (0.30 * 80 + 0.25 * 60 + 0.15 * 40 + 0.15 * 100 + 0.10 * 50) / 0.95
    assert score == pytest.approx(expected, abs=0.1)
    assert parts["history"]["value"] is None and parts["history"]["contribution"] == 0
    assert sum(p["contribution"] for p in parts.values()) == pytest.approx(score, abs=0.2)
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_history_needs_enough_resolved_signals():
    assert history_score(None, CFG)[0] is None
    assert history_score({"tp": 2, "sl": 1}, CFG)[0] is None
    value, note = history_score({"tp": 6, "sl": 2}, CFG)
    assert value == pytest.approx(7 / 10 * 100) and "6 target hits" in note


def test_select_candidates_filters_and_ranks():
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "9000000000", "priceChangePercent": "1"},
        {"symbol": "THINUSDT", "quoteVolume": "1000000", "priceChangePercent": "40"},
        {"symbol": "USDCUSDT", "quoteVolume": "500000000", "priceChangePercent": "0"},
        {"symbol": "BTCUSDT_240628", "quoteVolume": "900000000", "priceChangePercent": "3"},
        {"symbol": "ETHBTC", "quoteVolume": "900000000", "priceChangePercent": "3"},
        {"symbol": "MOVEUSDT", "quoteVolume": "60000000", "priceChangePercent": "-12"},
        {"symbol": "BADUSDT", "quoteVolume": "oops", "priceChangePercent": "5"},
    ]
    assert select_candidates(tickers, CFG) == ["MOVEUSDT", "BTCUSDT"]
    assert len(select_candidates(tickers, OpportunityConfig(max_candidates=1))) == 1


def test_config_is_overridable_and_ignores_bad_values():
    cfg = OpportunityConfig.from_dict({"surge_relative_volume": "3.5", "high_threshold": "oops", "nonsense": 1})
    assert cfg.surge_relative_volume == 3.5 and cfg.high_threshold == 80.0
    assert OpportunityConfig.from_dict(None).watch_threshold == 65.0


def test_stale_signal_detection():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=5)).isoformat()
    assert not is_stale_signal(fresh, "1h", CFG, now)
    assert is_stale_signal(old, "1h", CFG, now)
    assert not is_stale_signal(old, "4h", CFG, now)
    assert is_stale_signal("garbage", "1h", CFG, now)


def test_thresholds_set_tiers():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs))
    strict = rank_opportunities(raw, OpportunityConfig(high_threshold=101, watch_threshold=99), analyze_stub(sigs), now_ms=now)["results"][0]
    assert strict["tier"] == "low" and strict["group"] == "low"


# ---- deep ("all") mode funnel ------------------------------------------------------------------------

def test_all_mode_takes_every_liquid_coin_up_to_the_cap_and_drops_wide_spreads():
    tickers = [{"symbol": f"C{i:03d}USDT", "quoteVolume": "50000000", "priceChangePercent": str(i % 7)} for i in range(300)]
    tickers.append({"symbol": "WIDEUSDT", "quoteVolume": "50000000", "priceChangePercent": "9"})
    book = {"WIDEUSDT": {"bidPrice": "1.00", "askPrice": "1.01"}}  # ~1% spread
    top = select_candidates(tickers, CFG)
    deep = select_candidates(tickers, CFG, "all", None, book)
    assert len(top) == CFG.max_candidates and len(deep) == CFG.deep_max_universe
    assert "WIDEUSDT" not in deep
    assert select_candidates(tickers[:5], CFG, "all") != []


def test_funding_extreme_raises_a_coins_prior():
    tickers = [{"symbol": "AUSDT", "quoteVolume": "50000000", "priceChangePercent": "1"},
               {"symbol": "BUSDT", "quoteVolume": "50000000", "priceChangePercent": "1"}]
    out = select_candidates(tickers, CFG, "top", {"BUSDT": {"lastFundingRate": "0.001"}})
    assert out[0] == "BUSDT"


def test_shortlist_keeps_only_coins_with_a_15m_trigger():
    h = breakout_series(120, jump_at=118)
    now = now_during_last(h)
    end = int(h[-1][0]) + 900_000
    quiet = candles(99, step=900_000, t0=end - 98 * 900_000, vol=lambda i: 1000.0)
    loud = surge_15m(end, n=99)
    thin = candles(99, step=900_000, t0=end - 98 * 900_000, vol=lambda i: 0.0 if i % 3 else 500.0)
    out = shortlist_stage2({"QUIETUSDT": quiet, "LOUDUSDT": loud, "THINUSDT": thin, "EMPTYUSDT": []},
                           [{"symbol": "QUIETUSDT", "priceChangePercent": "0.5"}], None, CFG, now)
    assert out["symbols"] == ["LOUDUSDT"] and out["checked"] == 4 and out["triggered"] == 1
    assert "15m volume" in out["why"]["LOUDUSDT"][0]


def test_shortlist_respects_stage2_cap_and_orders_by_relative_volume():
    h = breakout_series(120)
    now = now_during_last(h)
    end = int(h[-1][0]) + 900_000
    klines = {f"C{i}USDT": candles(99, step=900_000, t0=end - 98 * 900_000, vol=lambda j, k=i: 3000.0 + 500 * k if j == 97 else 1000.0) for i in range(5)}
    out = shortlist_stage2(klines, [], None, OpportunityConfig(stage2_size=3), now)
    assert out["symbols"] == ["C4USDT", "C3USDT", "C2USDT"] and out["dropped_by_cap"] == 2


def test_deep_scan_works_from_99_fifteen_minute_candles():
    sigs = {"AAAUSDT": make_signal()}
    raw, now = raw_for(list(sigs))
    raw["klines"]["AAAUSDT"]["15m"] = surge_15m(int(raw["klines"]["AAAUSDT"]["1h"][-1][0]) + 900_000, n=99)
    item = rank_opportunities(raw, CFG, analyze_stub(sigs), now_ms=now)["results"][0]
    assert "unusual_volume" in item["labels"] and "15m candles" not in item["missing"]
