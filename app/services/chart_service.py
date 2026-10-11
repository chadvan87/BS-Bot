from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


def generate_signal_chart(
    candles: list[list[Any]],
    signal: dict[str, Any],
    output_path: str | Path,
    dark_theme: bool = True,
) -> str:
    """Render a social-preview-friendly candlestick chart with signal levels.

    The post card is usually viewed at a small size, so this intentionally acts as
    a clean signal card rather than a full technical-analysis terminal: recent,
    large candles first; trade zone and direction second.
    """
    # Twenty-four bars leave enough context for a setup, but make each candle readable
    # in Binance Square's small feed preview.
    visible_bars = 24
    candles = candles[-visible_bars:]
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    n = len(candles)

    bg = "#0b0f17" if dark_theme else "#ffffff"
    panel_bg = "#141a25" if dark_theme else "#f5f5f5"
    grid_color = "#283246" if dark_theme else "#dddddd"
    text_color = "#e9edf5" if dark_theme else "#111111"
    up_color, down_color = "#26a69a", "#ef5350"

    # A large square image earns more physical pixels in the feed and stays sharp
    # when Binance Square scales it down.
    fig, ax = plt.subplots(figsize=(8, 8), dpi=180)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(panel_bg)
    ax.grid(color=grid_color, linewidth=0.5, alpha=0.5)
    ax.tick_params(colors=text_color, labelsize=11)
    for spine in ax.spines.values():
        spine.set_color(grid_color)

    width = 0.76
    for i, (o, h, l, cl) in enumerate(zip(opens, highs, lows, closes)):
        color = up_color if cl >= o else down_color
        ax.vlines(i, l, h, color=color, linewidth=1.8)
        ax.add_patch(Rectangle((i - width / 2, min(o, cl)), width, max(abs(cl - o), 1e-9), color=color))

    entry_low, entry_high = signal.get("entry_low"), signal.get("entry_high")
    stop_loss = signal.get("stop_loss")

    if entry_low is not None and entry_high is not None:
        ax.axhspan(entry_low, entry_high, color="#42a5f5", alpha=0.32, label="Entry zone")
    if stop_loss is not None:
        ax.axhline(stop_loss, color=down_color, linestyle="--", linewidth=1.35, label="SL")
    for key in ("tp1", "tp2", "tp3"):
        tp = signal.get(key)
        if tp is not None:
            ax.axhline(tp, color=up_color, linestyle="--", linewidth=1.15, alpha=0.82)

    side = signal.get("side", "LONG")
    zone_color = up_color if side == "LONG" else down_color

    price = closes[-1]
    ax.annotate(
        f" {price:g}", xy=(n - 1, price), xytext=(4, -10 if side == "LONG" else 10), textcoords="offset points",
        color=text_color, fontsize=12, va="center", fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.25", facecolor=panel_bg, edgecolor="#70809b", linewidth=0.9),
    )

    tp3 = signal.get("tp3")
    right_margin = max(10, int(n * 0.42))
    x_end = n - 1 + right_margin
    if tp3 is not None and price:
        zone_label = "PUMP ZONE" if side == "LONG" else "BEAR ZONE"
        lo, hi = (price, tp3) if side == "LONG" else (tp3, price)
        ax.add_patch(Rectangle(
            (n - 1, lo), right_margin, max(hi - lo, 1e-9),
            facecolor=zone_color, alpha=0.22, edgecolor=zone_color, linewidth=0.8,
        ))
        ax.annotate(
            "", xy=(x_end - 0.5, tp3), xytext=(n - 1, price),
            arrowprops=dict(arrowstyle="-|>", color=zone_color, linewidth=3.2, mutation_scale=22),
        )
        pct = (tp3 - price) / price * 100 if side == "LONG" else (price - tp3) / price * 100
        ax.text(
            x_end - 0.5, tp3, f"  +{pct:.1f}%", color=zone_color, fontsize=13, fontweight="bold",
            va="bottom" if side == "LONG" else "top", ha="left",
        )
        ax.text(
            n - 1 + right_margin * 0.5, (lo + hi) / 2, zone_label, color=zone_color,
            fontsize=13, fontweight="bold", ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=bg, edgecolor=zone_color, linewidth=1.1, alpha=0.92),
        )
        ax.set_xlim(-1, x_end + 1)
    else:
        ax.set_xlim(-1, n)

    badge_color = up_color if side == "LONG" else down_color
    ax.text(
        0.02, 0.965, f" {side} ", transform=ax.transAxes, color="white",
        backgroundcolor=badge_color, fontsize=15, fontweight="bold", va="top", zorder=10,
    )

    coin = str(signal.get("symbol", "")).replace("USDT", "")
    ax.text(
        0, 1.02, f"${coin}  ·  {signal.get('timeframe', '')}", transform=ax.transAxes,
        color=text_color, fontsize=18, fontweight="bold", ha="left", va="bottom",
        zorder=10, clip_on=False,
    )
    ax.set_xticks([])

    fig.tight_layout()
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return str(out_path)
