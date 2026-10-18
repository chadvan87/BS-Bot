from __future__ import annotations

HOUR = 3_600_000
T0 = 1_700_000_000_000


def candles(n, start=100.0, step=HOUR, drift=None, vol=None, rng=0.5, t0=T0):
    """Kline rows [open_time, o, h, l, c, volume, close_time, quote_volume]."""
    drift = drift or (lambda i: 0.2 if i % 2 else -0.2)
    vol = vol or (lambda i: 1000.0)
    price, out = start, []
    for i in range(n):
        o, c = price, price + drift(i)
        t = t0 + i * step
        out.append([t, o, max(o, c) + rng, min(o, c) - rng, c, vol(i), t + step - 1, vol(i) * c])
        price = c
    return out


def now_during_last(rows):
    """A 'now' that makes the last row a live (unclosed) candle."""
    return int(rows[-1][0]) + (int(rows[-1][6]) - int(rows[-1][0])) // 2


def breakout_series(n=60, jump_at=None, jump=3.0, jump_vol=3000.0):
    """Flat range, then one strong closed candle above it, then a live candle."""
    jump_at = jump_at if jump_at is not None else n - 2
    return candles(n, drift=lambda i: jump if i == jump_at else (0.2 if i % 2 else -0.2),
                   vol=lambda i: jump_vol if i == jump_at else 1000.0)
