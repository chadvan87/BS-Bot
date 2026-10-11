from __future__ import annotations

import random
from typing import Any

# Binance Square rejects a post if it references too many distinct coin pairs
# (API error 220095). Keep round-up posts under that by only $-tagging the top coins.
MAX_CASHTAGS = 5

BANNED_PHRASES = (
    "guaranteed win", "guaranteed profit", "easy money", "cannot lose",
    "can't lose", "100% signal", "100% win", "risk-free", "risk free",
)

DYOR_LINES = (
    "⚠️ Always DYOR — this is not financial advice.",
    "⚠️ Not financial advice. Manage your risk and size positions responsibly.",
    "⚠️ Markets can invalidate any setup — DYOR and never risk more than you can afford to lose.",
    "⚠️ Trade your own plan. This is analysis, not financial advice.",
)

CTA_LINES = (
    "Click below to trade 👇\n${coin}",
    "Trade 👇 ${coin}",
    "Full setup above — trade 👇 ${coin}",
    "Watching this closely 👀 ${coin}",
)

DEBATE_LONG = (
    "Breakout continuation or one more sweep before it runs — where do you stand on ${coin}?",
    "Is this the bottom for ${coin}, or just a relief bounce before more downside?",
    "Reclaim and go, or fakeout? What's your read on ${coin}?",
)

DEBATE_SHORT = (
    "Trap for shorts, or is ${coin} really rolling over here?",
    "Is this exhaustion at the top for ${coin}, or one more leg up first?",
    "Distribution or just a pause before continuation — thoughts on ${coin}?",
)


def _coin(s: dict[str, Any]) -> str:
    return str(s.get("symbol", "")).upper().replace("USDT", "")


def _v(s: dict[str, Any], key: str, default: str = "N/A") -> Any:
    val = s.get(key)
    return val if val is not None else default


def _icon(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def _pick(seq: tuple[str, ...], coin: str) -> str:
    return random.choice(seq).replace("${coin}", f"${coin}")


def _risk_reward(s: dict[str, Any]) -> float | None:
    try:
        mid = (s["entry_low"] + s["entry_high"]) / 2
        risk = abs(mid - s["stop_loss"])
        reward = abs(s["tp2"] - mid)
        return round(reward / risk, 2) if risk else None
    except Exception:
        return None


def _reasons_pool(s: dict[str, Any], mc: dict[str, Any]) -> list[str]:
    pool: list[str] = []
    side = s.get("side", "LONG")
    e20, e50, e200 = s.get("ema20"), s.get("ema50"), s.get("ema200")
    if e20 is not None and e50 is not None and e200 is not None:
        if side == "LONG":
            if e20 > e50 > e200:
                pool.append(f"EMA20 ({e20}) > EMA50 ({e50}) > EMA200 ({e200}) — clean bullish trend structure")
            elif e20 > e50:
                pool.append(f"EMA20 ({e20}) has reclaimed EMA50 ({e50}), early trend shift building")
            else:
                pool.append(f"Price testing EMA200 ({e200}) as dynamic support")
        else:
            if e20 < e50 < e200:
                pool.append(f"EMA20 ({e20}) < EMA50 ({e50}) < EMA200 ({e200}) — clean bearish trend structure")
            elif e20 < e50:
                pool.append(f"EMA20 ({e20}) has lost EMA50 ({e50}), momentum turning down")
            else:
                pool.append(f"Price rejected at EMA200 ({e200}) resistance")

    rsi_v = s.get("rsi")
    if rsi_v is not None:
        if rsi_v < 35:
            pool.append(f"RSI at {rsi_v} — oversold zone, mean-reversion potential")
        elif rsi_v > 65:
            pool.append(f"RSI at {rsi_v} — overbought zone, exhaustion risk")
        else:
            pool.append(f"RSI at {rsi_v} — neutral, room to move either direction")

    vr = s.get("volume_ratio")
    if vr is not None:
        if vr > 1.3:
            pool.append(f"Volume running {vr}x the 20-period average — real participation behind the move")
        elif vr < 0.8:
            pool.append(f"Volume at only {vr}x average — low participation, confirmation still needed")
        else:
            pool.append(f"Volume roughly in line with average at {vr}x")

    support, resistance = s.get("support"), s.get("resistance")
    if support is not None and resistance is not None:
        pool.append(f"Key support at {support}, resistance at {resistance}")

    atr = s.get("atr")
    if atr is not None:
        pool.append(f"ATR of {atr} — use this to size positions for current volatility")

    rr = _risk_reward(s)
    if rr and rr >= 1.3:
        pool.append(f"Risk/reward to TP2 works out to roughly {rr}:1")

    price = s.get("price")
    if price is not None and s.get("entry_low") is not None and s.get("entry_high") is not None:
        mid = (s["entry_low"] + s["entry_high"]) / 2
        dist_pct = abs(price - mid) / price * 100 if price else 0
        if dist_pct < 0.15:
            pool.append("Price is currently sitting right inside the entry zone")
        else:
            pool.append(f"Price is about {dist_pct:.2f}% away from the entry zone right now")

    if mc.get("regime"):
        pool.append(f"BTC regime reads {mc['regime']}, a backdrop worth weighing on altcoin setups")
    if mc.get("mtf_agree"):
        pool.append(f"Higher timeframe structure agrees with this {side.lower()} bias")
    if mc.get("liquidity_sweep"):
        pool.append("Recent liquidity sweep beyond the prior swing before this move")

    return pool


def _reasons(s: dict[str, Any], mc: dict[str, Any], n_min: int = 2, n_max: int = 4) -> list[str]:
    pool = _reasons_pool(s, mc)
    if not pool:
        tf = s.get("timeframe", "the current timeframe")
        pool = [f"Setup aligns with {tf} structure", "Levels derived directly from recent price action"]
    random.shuffle(pool)
    k = min(len(pool), random.randint(n_min, n_max))
    return pool[:k]


def _reasons_block(reasons: list[str], header: str = "Why this setup?") -> str:
    if not reasons:
        return ""
    bullets = "\n".join(f"• {r}" for r in reasons)
    return f"{header}\n{bullets}"


def _levels_block(s: dict[str, Any]) -> str:
    coin = _coin(s)
    return (
        f"${coin} /USDT — {s.get('side', '')}\n\n"
        f"📍 Entry: {_v(s, 'entry_low')} – {_v(s, 'entry_high')}\n"
        f"🛑 Stop-loss: {_v(s, 'stop_loss')}\n\n"
        f"Targets 🎯\n"
        f"TP1: {_v(s, 'tp1')}\n"
        f"TP2: {_v(s, 'tp2')}\n"
        f"TP3: {_v(s, 'tp3')}"
    )


class PostTemplateEngine:
    """Deterministic, algorithmic post generator. No LLM calls."""

    def __init__(self) -> None:
        self.last_template: str = ""

    # ---- public API -----------------------------------------------------

    def generate(
        self,
        signal: dict[str, Any],
        template_name: str = "auto",
        market_context: dict[str, Any] | None = None,
    ) -> str:
        mc = market_context or {}
        name = template_name
        if name == "auto":
            name = self._select_auto(signal, mc, mc.get("recent_templates", []))
        fn = self._DISPATCH.get(name, self._quick_signal)
        reasons = [] if name in self._NO_REASON_TEMPLATES else _reasons(signal, mc)
        body = fn(self, signal, mc, reasons)
        self.last_template = name
        return body.strip()

    # ---- auto selection ---------------------------------------------------

    def _select_auto(self, s: dict[str, Any], mc: dict[str, Any], recent: list[str]) -> str:
        candidates: list[str] = []
        status = s.get("status", "NEW") or "NEW"
        symbol = str(s.get("symbol", "")).upper()

        if status == "TP1_HIT" or status in ("TP2_HIT", "TP3_HIT"):
            candidates.append("tp_hit_update")
        elif status == "ENTRY_TRIGGERED":
            candidates.append("entry_triggered_update")
        elif status == "SL_HIT":
            candidates.append("stop_loss_update")
        elif status == "INVALIDATED":
            candidates.append("invalidated_setup")
        else:
            if symbol.startswith("BTC"):
                candidates += ["btc_market_analysis", "btc_decisive_zone"]

            rsi_v = s.get("rsi")
            side = s.get("side")
            if side == "LONG" and rsi_v is not None and rsi_v < 35:
                candidates.append("reversal_setup")
            if side == "SHORT" and rsi_v is not None and rsi_v > 65:
                candidates += ["short_setup", "contrarian_hook"]

            entry_low, entry_high, atr = s.get("entry_low"), s.get("entry_high"), s.get("atr")
            if entry_low is not None and entry_high is not None and atr:
                if abs(entry_high - entry_low) > atr * 0.45:
                    candidates.append("dca_zone")

            score = s.get("score", 0) or 0
            if score >= 85:
                candidates += ["creator_hype", "technical_breakdown"]
            elif score < 55:
                candidates.append("watchlist_post")

            if not candidates:
                candidates.append("breakout_retest")
            candidates += ["quick_signal", "long_setup" if side == "LONG" else "short_setup", "debate_post"]

        pool = list(dict.fromkeys(candidates))
        if len(pool) > 1 and recent:
            filtered = [c for c in pool if c != recent[-1]]
            if filtered:
                pool = filtered
        return random.choice(pool) if pool else "quick_signal"

    # ---- standard trade-setup templates ------------------------------------

    def _quick_signal(self, s, mc, reasons) -> str:
        coin, icon = _coin(s), _icon(s.get("side", "LONG"))
        return (
            f"{icon} ${coin} {s.get('side')} SETUP\n\n"
            f"Entry: {_v(s,'entry_low')} – {_v(s,'entry_high')}\n"
            f"SL: {_v(s,'stop_loss')}\n"
            f"TP: {_v(s,'tp1')} / {_v(s,'tp2')} / {_v(s,'tp3')}\n\n"
            f"⚡ {_v(s,'score')}% confidence | {_v(s,'timeframe')}\n"
            f"Do not chase outside the entry zone.\n\n"
            f"Trade 👇 ${coin}"
        )

    def _creator_hype(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"🚨 ${coin} IS MOVING INTO A DECISIVE ZONE\n\n"
            f"The market is approaching an important liquidity area. A confirmed rejection or "
            f"breakout here could set up the next real move.\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _technical_breakdown(self, s, mc, reasons) -> str:
        coin = _coin(s)
        stats = (
            f"📊 Technicals\n"
            f"EMA20/50/200: {_v(s,'ema20')} / {_v(s,'ema50')} / {_v(s,'ema200')}\n"
            f"RSI: {_v(s,'rsi')} | ATR: {_v(s,'atr')} | Vol ratio: {_v(s,'volume_ratio')}\n"
            f"Score: {_v(s,'score')}/100 | TF: {_v(s,'timeframe')}"
        )
        return (
            f"📈 ${coin} TECHNICAL BREAKDOWN\n\n"
            f"{_levels_block(s)}\n\n"
            f"{stats}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _contrarian_hook(self, s, mc, reasons) -> str:
        coin = _coin(s)
        trend_word = "the crowd chasing the trend" if s.get("side") == "SHORT" else "the crowd calling for more downside"
        debate = _pick(DEBATE_LONG if s.get("side") == "LONG" else DEBATE_SHORT, coin)
        return (
            f"Everyone is watching {trend_word}, but the {_v(s,'timeframe')} structure just produced "
            f"a {_v(s,'score')}% {s.get('side')} setup on ${coin}.\n\n"
            f"Trade Plan:\n"
            f"Entry: {_v(s,'entry_low')} – {_v(s,'entry_high')}\n"
            f"SL: {_v(s,'stop_loss')}\n"
            f"TP1: {_v(s,'tp1')}\nTP2: {_v(s,'tp2')}\nTP3: {_v(s,'tp3')}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"Debate:\n{debate}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"Click here to trade 👇 ${coin}"
        )

    def _dca_zone(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"💰 ${coin} DCA ZONE FORMING\n\n"
            f"Entry range is wide enough to scale in rather than force one fill.\n\n"
            f"{_levels_block(s)}\n\n"
            f"Suggested approach: split entries across the {_v(s,'entry_low')}–{_v(s,'entry_high')} range "
            f"instead of one lump entry.\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _breakout_retest(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"⚡ ${coin} RETESTING BROKEN STRUCTURE\n\n"
            f"Price is coming back to retest a level it recently broke — a classic continuation spot "
            f"if it holds.\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _reversal_setup(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"🔄 ${coin} REVERSAL SETUP\n\n"
            f"Momentum has stretched far enough that a bounce is worth watching for.\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _short_setup(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"🔴📉 ${coin} SHORT SETUP\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _long_setup(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"🟢📈 ${coin} LONG SETUP\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    def _debate_post(self, s, mc, reasons) -> str:
        coin = _coin(s)
        debate = _pick(DEBATE_LONG if s.get("side") == "LONG" else DEBATE_SHORT, coin)
        return (
            f"👀 {debate}\n\n"
            f"Here's the setup building on ${coin}:\n\n"
            f"{_levels_block(s)}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"{_pick(DYOR_LINES, coin)}\n\n"
            f"{_pick(CTA_LINES, coin)}"
        )

    # ---- BTC-specific templates ---------------------------------------------

    def _btc_market_analysis(self, s, mc, reasons) -> str:
        price = _v(s, "price", _v(s, "entry_high"))
        support_1 = mc.get("support_1", _v(s, "support"))
        support_2 = mc.get("support_2", "N/A")
        resistance_1 = mc.get("resistance_1", _v(s, "resistance"))
        resistance_2 = mc.get("resistance_2", "N/A")
        regime = mc.get("regime", "neutral")
        bias = mc.get("bias", s.get("side", "NEUTRAL"))
        return (
            f"₿ BTC MARKET UPDATE — DECISIVE ZONE AHEAD\n\n"
            f"BTC is trading near {price} and approaching a key decision area.\n\n"
            f"Key levels:\n"
            f"🟢 Support: {support_1} / {support_2}\n"
            f"🔴 Resistance: {resistance_1} / {resistance_2}\n\n"
            f"Bullish scenario:\n"
            f"A reclaim above {resistance_1} opens the door toward {resistance_2}.\n\n"
            f"Bearish scenario:\n"
            f"Losing {support_1} risks a slide toward {support_2}.\n\n"
            f"Current regime: {regime}\n"
            f"Bias: {bias}\n"
            f"Confidence: {_v(s,'score')}%\n\n"
            f"Are you expecting a breakout, or another liquidity sweep first?\n\n"
            f"{_pick(DYOR_LINES, 'BTC')}\n"
            f"Trade BTC 👇 $BTC"
        )

    def _btc_decisive_zone(self, s, mc, reasons) -> str:
        price = _v(s, "price", _v(s, "entry_high"))
        regime = mc.get("regime", "neutral")
        return (
            f"🚨 $BTC AT A DECISIVE ZONE — {price}\n\n"
            f"This is the level that decides the next leg. Regime reads {regime}.\n\n"
            f"🟢 Support: {mc.get('support_1', _v(s,'support'))}\n"
            f"🔴 Resistance: {mc.get('resistance_1', _v(s,'resistance'))}\n\n"
            f"Confidence: {_v(s,'score')}%\n\n"
            f"Breakout or fakeout — where's BTC headed from here?\n\n"
            f"{_pick(DYOR_LINES, 'BTC')}\n"
            f"Trade BTC 👇 $BTC"
        )

    # ---- status-driven update templates --------------------------------------

    def _tp_hit_update(self, s, mc, reasons) -> str:
        coin = _coin(s)
        hit = s.get("status", "TP1_HIT").replace("_HIT", "").replace("_", " ")
        return (
            f"🎯 {hit} HIT on ${coin}!\n\n"
            f"Entry was {_v(s,'entry_low')}–{_v(s,'entry_high')}, and price has now tagged this target.\n\n"
            f"Remaining targets: TP2 {_v(s,'tp2')} | TP3 {_v(s,'tp3')}\n"
            f"Consider trailing stop to protect gains.\n\n"
            f"{_pick(DYOR_LINES, coin)}"
        )

    def _entry_triggered_update(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"⚡ ENTRY TRIGGERED ${coin}\n\n"
            f"Price has entered the {_v(s,'entry_low')}–{_v(s,'entry_high')} zone.\n"
            f"🛑 SL: {_v(s,'stop_loss')} | 🎯 TP1: {_v(s,'tp1')}\n\n"
            f"Watching for confirmation from here.\n\n"
            f"{_pick(DYOR_LINES, coin)}"
        )

    def _stop_loss_update(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"🛑 STOPPED OUT ${coin}\n\n"
            f"This setup hit stop-loss at {_v(s,'stop_loss')}. Not every setup works — risk management is "
            f"what keeps you in the game.\n\n"
            f"Original entry was {_v(s,'entry_low')}–{_v(s,'entry_high')}.\n\n"
            f"{_pick(DYOR_LINES, coin)}"
        )

    def _invalidated_setup(self, s, mc, reasons) -> str:
        coin = _coin(s)
        return (
            f"❌ SETUP INVALIDATED ${coin}\n\n"
            f"Structure broke down before entry was ever triggered — the {_v(s,'entry_low')}–"
            f"{_v(s,'entry_high')} plan is no longer valid.\n\n"
            f"Staying disciplined and waiting for the next clean setup.\n\n"
            f"{_pick(DYOR_LINES, coin)}"
        )

    def _watchlist_post(self, s, mc, reasons) -> str:
        coin = _coin(s)
        icon = _icon(s.get("side", "LONG"))
        return (
            f"👀 ON WATCH: ${coin}\n\n"
            f"{icon} Potential {s.get('side')} setup forming on {_v(s,'timeframe')}, but confidence is "
            f"still moderate at {_v(s,'score')}%.\n\n"
            f"Watching: {_v(s,'entry_low')}–{_v(s,'entry_high')}\n\n"
            f"{_reasons_block(reasons)}\n\n"
            f"Not a signal yet — just on the radar.\n\n"
            f"{_pick(DYOR_LINES, coin)}"
        )

    # ---- aggregate templates ---------------------------------------------

    def _top_10_signals(self, s, mc, reasons) -> str:
        # Binance Square rejects posts referencing too many distinct coin pairs at once
        # (API error 220095), so only the top-scoring coins get a real $CASHTAG; the
        # rest are listed as plain text to keep the coin-pair count under the limit.
        signals = mc.get("signals", [])[:10]
        ranked = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)
        tagged = {_coin(sig) for sig in ranked[:MAX_CASHTAGS]}

        def label(sig: dict[str, Any]) -> str:
            coin = _coin(sig)
            return f"${coin}" if coin in tagged else coin

        lines = []
        for i, sig in enumerate(signals, 1):
            lines.append(
                f"{i}. {label(sig)} — {sig.get('side')} — Score {sig.get('score')}\n"
                f"   Entry: {_v(sig,'entry_low')} – {_v(sig,'entry_high')}"
            )
        longs = [sig for sig in signals if sig.get("side") == "LONG"]
        shorts = [sig for sig in signals if sig.get("side") == "SHORT"]
        strongest_long = max(longs, key=lambda x: x.get("score", 0)) if longs else None
        strongest_short = max(shorts, key=lambda x: x.get("score", 0)) if shorts else None
        btc_regime = mc.get("btc_regime", "neutral")
        summary = []
        if strongest_long:
            summary.append(f"Strongest LONG: {label(strongest_long)} (score {strongest_long.get('score')})")
        if strongest_short:
            summary.append(f"Strongest SHORT: {label(strongest_short)} (score {strongest_short.get('score')})")
        summary.append(f"BTC regime: {btc_regime}")
        summary.append("Max 3 active setups recommended at once — don't overextend.")
        return (
            f"🔥 TOP 10 CRYPTO SETUPS TO WATCH\n\n"
            + "\n".join(lines)
            + "\n\n"
            + "\n".join(summary)
            + "\n\n"
            + _pick(DYOR_LINES, "crypto")
        )

    _DISPATCH: dict[str, Any] = {}
    _NO_REASON_TEMPLATES = {
        "tp_hit_update", "entry_triggered_update", "stop_loss_update",
        "invalidated_setup", "top_10_signals", "btc_market_analysis", "btc_decisive_zone",
    }


PostTemplateEngine._DISPATCH = {
    "quick_signal": PostTemplateEngine._quick_signal,
    "creator_hype": PostTemplateEngine._creator_hype,
    "technical_breakdown": PostTemplateEngine._technical_breakdown,
    "contrarian_hook": PostTemplateEngine._contrarian_hook,
    "dca_zone": PostTemplateEngine._dca_zone,
    "breakout_retest": PostTemplateEngine._breakout_retest,
    "reversal_setup": PostTemplateEngine._reversal_setup,
    "short_setup": PostTemplateEngine._short_setup,
    "long_setup": PostTemplateEngine._long_setup,
    "debate_post": PostTemplateEngine._debate_post,
    "btc_market_analysis": PostTemplateEngine._btc_market_analysis,
    "btc_decisive_zone": PostTemplateEngine._btc_decisive_zone,
    "tp_hit_update": PostTemplateEngine._tp_hit_update,
    "entry_triggered_update": PostTemplateEngine._entry_triggered_update,
    "stop_loss_update": PostTemplateEngine._stop_loss_update,
    "invalidated_setup": PostTemplateEngine._invalidated_setup,
    "watchlist_post": PostTemplateEngine._watchlist_post,
    "top_10_signals": PostTemplateEngine._top_10_signals,
}

ALL_TEMPLATE_NAMES = tuple(PostTemplateEngine._DISPATCH.keys())
