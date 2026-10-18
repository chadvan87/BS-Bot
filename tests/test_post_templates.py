from __future__ import annotations

import re

import pytest

import json

from app.services.post_templates import (
    ALIASES, ALL_TEMPLATE_NAMES, BANNED_PHRASES, CATEGORIES, MAX_CASHTAGS, PostTemplateEngine,
    check_post_text, collect_evidence, risk_reward,
)

STANDARD_TEMPLATES = (
    "quick_signal", "creator_hype", "technical_breakdown", "contrarian_hook",
    "dca_zone", "breakout_retest", "reversal_setup", "short_setup", "long_setup", "debate_post",
)


def make_signal(side="LONG", **overrides):
    base = {
        "id": 1, "symbol": "SOLUSDT", "side": side, "timeframe": "1h",
        "entry_low": 73.20, "entry_high": 73.40, "stop_loss": 72.80,
        "tp1": 73.95, "tp2": 74.35, "tp3": 74.80, "score": 72,
        "reason": "test reason", "status": "NEW",
        "price": 73.30, "ema20": 73.1, "ema50": 72.9, "ema200": 71.5,
        "rsi": 55.0, "atr": 0.9, "volume_ratio": 1.2, "support": 71.0, "resistance": 75.0,
    }
    base.update(overrides)
    return base


def make_short_signal(**overrides):
    s = make_signal(side="SHORT", entry_low=73.10, entry_high=73.30, stop_loss=73.80,
                     tp1=72.50, tp2=72.10, tp3=71.60, ema20=73.5, ema50=73.9, ema200=74.5, rsi=72.0)
    s.update(overrides)
    return s


@pytest.fixture
def engine():
    return PostTemplateEngine()


@pytest.mark.parametrize("name", ALL_TEMPLATE_NAMES)
def test_every_template_returns_non_empty_content(engine, name):
    mc = {}
    signal = make_signal()
    if name.startswith("btc"):
        signal = make_signal(symbol="BTCUSDT")
        mc = {"regime": "bullish", "bias": "LONG", "support_1": 60000, "support_2": 58000,
              "resistance_1": 64000, "resistance_2": 66000}
    if name == "top_10_signals":
        mc = {"signals": [make_signal(symbol="SOLUSDT"), make_short_signal(symbol="ETHUSDT"),
                           make_signal(symbol="BNBUSDT", score=88)], "btc_regime": "bullish"}
    content = engine.generate(signal, template_name=name, market_context=mc)
    assert isinstance(content, str)
    assert len(content.strip()) > 0


def test_correct_long_short_icon(engine):
    long_content = engine.generate(make_signal(side="LONG"), template_name="quick_signal")
    short_content = engine.generate(make_short_signal(), template_name="quick_signal")
    assert "🟢" in long_content
    assert "🔴" in short_content


@pytest.mark.parametrize("name", STANDARD_TEMPLATES)
def test_entry_sl_tp_values_included(engine, name):
    s = make_signal()
    content = engine.generate(s, template_name=name)
    assert str(s["entry_low"]) in content
    assert str(s["entry_high"]) in content
    assert str(s["stop_loss"]) in content
    assert str(s["tp1"]) in content


@pytest.mark.parametrize("name", STANDARD_TEMPLATES)
def test_cashtag_included(engine, name):
    s = make_signal(symbol="SOLUSDT")
    content = engine.generate(s, template_name=name)
    assert "$SOL" in content


@pytest.mark.parametrize("name", ALL_TEMPLATE_NAMES)
def test_no_guaranteed_profit_language(engine, name):
    mc = {}
    signal = make_signal()
    if name.startswith("btc"):
        signal = make_signal(symbol="BTCUSDT")
        mc = {"regime": "bullish", "bias": "LONG"}
    if name == "top_10_signals":
        mc = {"signals": [make_signal(), make_short_signal()], "btc_regime": "bullish"}
    content = engine.generate(signal, template_name=name, market_context=mc).lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in content


def test_btc_mode_works(engine):
    mc = {"regime": "bullish", "bias": "LONG", "support_1": 60000, "support_2": 58000,
          "resistance_1": 64000, "resistance_2": 66000}
    content = engine.generate(make_signal(symbol="BTCUSDT"), template_name="btc_market_analysis", market_context=mc)
    assert "$BTC" in content
    assert "Support" in content and "Resistance" in content


def test_top10_mode_works(engine):
    signals = [make_signal(symbol="SOLUSDT", score=90), make_short_signal(symbol="ETHUSDT", score=85),
               make_signal(symbol="BNBUSDT", score=70)]
    content = engine.generate(signals[0], template_name="top_10_signals", market_context={"signals": signals, "btc_regime": "bullish"})
    assert "$SOL" in content and "$ETH" in content and "$BNB" in content
    assert "3 setups" in content
    assert "not win probabilities" in content


def test_top10_stays_within_cashtag_limit_and_dedupes(engine):
    names = ["SOL", "ETH", "BNB", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "LTC", "SOL"]
    signals = [make_signal(symbol=f"{n}USDT", score=90 - i) for i, n in enumerate(names)]
    content = engine.generate(signals[0], template_name="top_10_signals", market_context={"signals": signals})
    assert check_post_text(content) == []
    assert len(set(re.findall(r"\$([A-Z]+)", content))) <= MAX_CASHTAGS
    assert content.count("1. $SOL") == 1 and "2. $SOL" not in content  # duplicate symbol listed once


# ---- consolidation / aliases ---------------------------------------------------------------

def test_every_old_identifier_still_resolves_to_a_category(engine):
    assert set(CATEGORIES) == {"breaking_momentum", "trade_setup", "technical_deep_dive", "reversal_watch", "signal_update", "market_radar"}
    for old, (category, _) in ALIASES.items():
        assert category in CATEGORIES
        assert old in ALL_TEMPLATE_NAMES
    for name in ("quick_signal", "creator_hype", "dca_zone", "debate_post", "reversal_setup", "contrarian_hook", "breakout_retest"):
        engine.generate(make_signal(), template_name=name)
        assert engine.last_template == ALIASES[name][0]


def test_unknown_style_falls_back_to_auto(engine):
    assert engine.generate(make_signal(), template_name="nope")
    assert engine.last_template in CATEGORIES


def evidence(**kw):
    base = {"regime": "TRENDING", "adx": 28.0, "breakout": {"state": "none"}, "retest": {"state": "none"},
            "reversal": {"rsi_extreme": False, "at_level": False, "candle_turn": False, "confirmed": False}}
    base.update(kw)
    return json.dumps(base)


CONFIRMED_LONG = {"state": "confirmed", "direction": "LONG", "level": 73.0, "candles_ago": 1, "volume_ratio": 2.2}


def test_auto_picks_breaking_momentum_only_with_confirmed_breakout(engine):
    s = make_signal(evidence=evidence(breakout=CONFIRMED_LONG))
    engine.generate(s)
    assert engine.last_template == "breaking_momentum"
    potential = make_signal(evidence=evidence(breakout={**CONFIRMED_LONG, "state": "potential"}))
    engine.generate(potential)
    assert engine.last_template != "breaking_momentum"


def test_breakout_post_distinguishes_confirmed_from_potential(engine):
    confirmed = engine.generate(make_signal(evidence=evidence(breakout=CONFIRMED_LONG)), "breaking_momentum")
    assert "closed above 73.0" in confirmed and "2.2x" in confirmed
    assert "potential" not in confirmed.lower()
    potential = engine.generate(make_signal(evidence=evidence(breakout={**CONFIRMED_LONG, "state": "potential"})), "breaking_momentum")
    assert "No confirmed breakout" in potential and "potential breakout" in potential.lower()
    assert "closed above" not in potential


def test_breakout_claim_requires_matching_direction(engine):
    wrong = make_signal(evidence=evidence(breakout={**CONFIRMED_LONG, "direction": "SHORT"}))
    assert not any(e.kind == "structure" for e in collect_evidence(wrong))
    engine.generate(wrong)
    assert engine.last_template != "breaking_momentum"


def test_reversal_post_mentions_reversal_evidence(engine):
    s = make_signal(rsi=28.0, support=73.1, evidence=evidence(reversal={"rsi_extreme": True, "at_level": True, "candle_turn": True, "confirmed": True}))
    engine.generate(s)
    assert engine.last_template == "reversal_watch"
    text = engine.generate(s, "reversal_watch")
    assert "RSI 28.0 is oversold" in text and "support 73.1" in text and "turned up" in text
    unconfirmed = engine.generate(make_signal(rsi=28.0), "reversal_watch")
    assert "Confirmation to look for" in unconfirmed and "not confirmed yet" in unconfirmed


def test_support_evidence_never_contradicts_the_side():
    # LONG with an overbought RSI and a bearish EMA stack: neither may be listed as support
    s = make_signal(rsi=78.0, ema20=70.0, ema50=71.0, ema200=72.0, volume_ratio=0.6)
    kinds = {e.kind for e in collect_evidence(s)}
    assert "trend" not in kinds and "rsi" not in kinds and "reversal" not in kinds
    short = make_short_signal(rsi=25.0, ema20=75.0, ema50=74.0, ema200=73.0)
    assert not {e.kind for e in collect_evidence(short)} & {"trend", "rsi", "reversal"}


def test_deep_dive_reports_watch_outs_honestly(engine):
    s = make_signal(score=88, rsi=78.0, volume_ratio=0.6, price=75.0)
    text = engine.generate(s, "technical_deep_dive")
    assert "Watch-outs" in text and "overbought" in text and "thin" in text


def test_output_is_deterministic(engine):
    s = make_signal(id=9, evidence=evidence(breakout=CONFIRMED_LONG))
    assert engine.generate(s) == PostTemplateEngine().generate(s)


def test_recent_template_shifts_wording_not_category(engine):
    s = make_signal(id=3)
    first = engine.generate(s, "trade_setup")
    again = engine.generate(s, "trade_setup", {"recent_templates": ["trade_setup"]})
    assert engine.last_template == "trade_setup"
    assert first != again


# ---- numbers ------------------------------------------------------------------------------------

def test_percentages_and_rr_are_correct(engine):
    s = make_signal(entry_low=100.0, entry_high=102.0, stop_loss=98.0, tp1=104.0, tp2=106.0, tp3=110.0)  # mid 101
    text = engine.generate(s, "trade_setup")
    assert "Stop: 98.0 (3.0% from entry)" in text           # (101-98)/101
    assert "TP1: 104.0 (+3.0%)" in text                     # (104-101)/101
    assert "TP2: 106.0 (+5.0%)" in text
    assert "R:R to TP2: 1.67:1" in text                     # 5 / 3
    assert risk_reward(s) == 1.67


def test_short_percentages(engine):
    s = make_short_signal(entry_low=100.0, entry_high=102.0, stop_loss=104.0, tp1=98.0, tp2=96.0, tp3=94.0)
    text = engine.generate(s, "trade_setup")
    assert "Stop: 104.0 (3.0% from entry)" in text and "TP1: 98.0 (+3.0%)" in text and "R:R to TP2: 1.67:1" in text


def test_invalid_levels_do_not_show_rr_or_wrong_side_targets(engine):
    wrong_stop = make_signal(stop_loss=74.0)                       # stop above a LONG entry
    assert risk_reward(wrong_stop) is None
    assert "R:R" not in engine.generate(wrong_stop, "trade_setup")
    capped = make_signal(tp1=73.9, tp2=73.9, tp3=73.9)             # targets collapsed by a resistance cap
    text = engine.generate(capped, "trade_setup")
    assert text.count("🎯") == 1
    below = make_signal(tp1=73.0, tp2=72.9, tp3=72.0)              # all targets below a LONG entry
    assert risk_reward(below) is None and "🎯" not in engine.generate(below, "trade_setup")


def test_no_misleading_confidence_percent(engine):
    for name in ALL_TEMPLATE_NAMES:
        text = engine.generate(make_signal(score=78), name, {"signals": [make_signal()]})
        assert "% confidence" not in text.lower()
    quick = engine.generate(make_signal(score=78), "quick_signal")
    assert "Technical Setup Score: 78/95" in quick
    assert "not a win probability" in engine.generate(make_signal(score=88), "technical_deep_dive")


def test_no_malformed_pair_and_single_cashtag(engine):
    for name in CATEGORIES:
        text = engine.generate(make_signal(), name, {"signals": [make_signal()], "regime": "bullish"})
        assert "/USDT" not in text or " /USDT" not in text
        assert check_post_text(text) == [], (name, check_post_text(text))
    assert engine.generate(make_signal(), "trade_setup").count("$SOL") == 1


def test_price_formatting_has_no_exponent(engine):
    s = make_signal(symbol="PEPEUSDT", entry_low=0.00000123, entry_high=0.00000125, stop_loss=0.0000012,
                    tp1=0.0000013, tp2=0.0000014, tp3=0.0000015)
    text = engine.generate(s, "trade_setup")
    assert "e-" not in text and "0.00000123" in text


# ---- text checks ----------------------------------------------------------------------------------

def test_check_post_text_flags_bad_copy():
    assert check_post_text("Guaranteed profit on $SOL")
    assert check_post_text("Last chance, act now $SOL")
    assert check_post_text("Use 50x leverage on $SOL")
    assert check_post_text("78% confidence on $SOL")
    assert check_post_text("$SOL /USDT long")
    assert check_post_text("$SOL and $SOL again")
    assert check_post_text("$A1 $B1 $C1 $D1 $E1 $F1")
    assert check_post_text("Volume 1.5x average on $SOL with a close above 100") == []


# ---- signal updates ---------------------------------------------------------------------------------

def update_signal(status, **kw):
    s = make_signal(id=12, status=status, created_at="2026-10-18T10:00:00+07:00",
                    evidence=json.dumps({"last_check": {"price": 74.0, "time": "2026-10-18T13:20:00+07:00", "note": kw.pop("note", "")}}))
    s.update(kw)
    return s


def test_update_has_price_elapsed_reference_and_realized_move(engine):
    text = engine.generate(update_signal("TP1_HIT"))
    assert engine.last_template == "signal_update"
    assert "reached TP1" in text and "signal #12" in text and "3h 20m ago" in text
    assert "Last checked price: 74.0" in text and "move from the middle of the entry zone" in text
    assert "Remaining: TP2 74.35" in text


def test_update_without_price_makes_no_price_claim(engine):
    s = make_signal(id=5, status="SL_HIT", created_at="2026-10-18T10:00:00+07:00")
    text = engine.generate(s)
    assert "Last checked price" not in text and "hit the stop" in text


def test_ambiguous_update_does_not_claim_an_order(engine):
    text = engine.generate(update_signal("AMBIGUOUS", note="Price touched both the stop and TP1 within one candle; candle data cannot show which came first."))
    assert "cannot show which came first" in text
    assert "reached TP" not in text and "hit the stop" not in text


def test_all_update_statuses_render_clean(engine):
    for status in ("ENTRY_TRIGGERED", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "INVALIDATED", "EXPIRED", "AMBIGUOUS", "NEW"):
        text = engine.generate(update_signal(status), "signal_update")
        assert check_post_text(text) == [], (status, check_post_text(text))
