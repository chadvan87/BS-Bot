from __future__ import annotations

from pathlib import Path

from app.services.chart_service import generate_signal_chart


def make_candles(n=90, start_price=100.0):
    candles = []
    price = start_price
    ts = 1_700_000_000_000
    for i in range(n):
        drift = (i % 7 - 3) * 0.4
        o = price
        c = price + drift
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        v = 1000 + (i % 5) * 50
        candles.append([ts + i * 3_600_000, o, h, l, c, v])
        price = c
    return candles


def make_signal():
    return {
        "symbol": "SOLUSDT", "side": "LONG", "timeframe": "1h", "score": 72,
        "entry_low": 98.5, "entry_high": 99.5, "stop_loss": 96.0,
        "tp1": 102.0, "tp2": 104.0, "tp3": 106.0,
        "support": 95.0, "resistance": 108.0,
    }


def test_chart_file_is_generated(tmp_path):
    out_path = tmp_path / "chart.png"
    result = generate_signal_chart(make_candles(), make_signal(), out_path, dark_theme=True)
    p = Path(result)
    assert p.exists()
    assert p.stat().st_size > 1000
    assert p.suffix == ".png"


def test_chart_handles_short_candle_history(tmp_path):
    out_path = tmp_path / "chart_short.png"
    result = generate_signal_chart(make_candles(n=20), make_signal(), out_path, dark_theme=True)
    assert Path(result).exists()


def test_chart_light_theme(tmp_path):
    out_path = tmp_path / "chart_light.png"
    result = generate_signal_chart(make_candles(), make_signal(), out_path, dark_theme=False)
    assert Path(result).exists()
