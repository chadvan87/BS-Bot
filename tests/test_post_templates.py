from __future__ import annotations

import re

import pytest

from app.services.post_templates import ALL_TEMPLATE_NAMES, BANNED_PHRASES, PostTemplateEngine

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
    assert "TOP 10" in content.upper()


def test_duplicate_template_rotation_avoids_consecutive_repeats(engine):
    signal = make_signal(score=90, rsi=55)  # rich signal -> large candidate pool
    used = []
    last = None
    for _ in range(15):
        mc = {"recent_templates": [last] if last else []}
        engine.generate(signal, template_name="auto", market_context=mc)
        chosen = engine.last_template
        if last is not None:
            assert chosen != last, "auto-selection repeated the same template consecutively"
        used.append(chosen)
        last = chosen
    assert len(set(used)) > 1
