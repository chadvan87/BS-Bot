from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Binance Square rejects a post if it references too many distinct coin pairs
# (API error 220095). Keep round-up posts under that by only $-tagging the top coins.
MAX_CASHTAGS = 5

BANNED_PHRASES = (
    "guaranteed win", "guaranteed profit", "guaranteed return", "guaranteed gain", "easy money",
    "cannot lose", "can't lose", "100% signal", "100% win", "risk-free", "risk free", "no risk",
    "sure thing", "can't fail", "cannot fail", "will moon", "will pump", "to the moon",
    # artificial urgency
    "last chance", "don't miss", "dont miss", "act now", "hurry", "before it's too late",
    "once in a lifetime", "now or never",
    # leverage / all-in encouragement
    "all in", "all-in", "yolo", "max leverage", "high leverage", "use leverage", "lever up",
)

_LEVERAGE_RE = re.compile(r"\b\d+\s?x\s+leverage\b|\bleverage\s+(?:up\s+to\s+)?\d+\s?x\b", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"\d+\s?%\s*(?:confidence|probability|win rate|chance)", re.IGNORECASE)
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
_SPACED_PAIR_RE = re.compile(r"\$\w+\s+/\s*USDT")

DYOR_LINES = (
    "Not financial advice. Do your own research and size risk responsibly.",
    "Analysis, not advice. Trade your own plan and never risk more than you can afford to lose.",
    "Any setup can fail. DYOR and manage your risk.",
    "This is a technical read, not financial advice.",
)

HASHTAGS = "#CryptoTrading"

# The six content categories. Old style identifiers keep working through ALIASES.
CATEGORIES = (
    "breaking_momentum", "trade_setup", "technical_deep_dive",
    "reversal_watch", "signal_update", "market_radar",
)

# old identifier -> (category, variant)
ALIASES: dict[str, tuple[str, str]] = {
    "quick_signal": ("trade_setup", "compact"),
    "long_setup": ("trade_setup", ""),
    "short_setup": ("trade_setup", ""),
    "dca_zone": ("trade_setup", "dca"),
    "debate_post": ("trade_setup", "debate"),
    "breakout_retest": ("breaking_momentum", ""),
    "creator_hype": ("technical_deep_dive", ""),
    "technical_breakdown": ("technical_deep_dive", ""),
    "reversal_setup": ("reversal_watch", ""),
    "contrarian_hook": ("reversal_watch", ""),
    "tp_hit_update": ("signal_update", ""),
    "entry_triggered_update": ("signal_update", ""),
    "stop_loss_update": ("signal_update", ""),
    "invalidated_setup": ("signal_update", ""),
    "btc_market_analysis": ("market_radar", "btc"),
    "btc_decisive_zone": ("market_radar", "btc"),
    "watchlist_post": ("market_radar", "watchlist"),
    "top_10_signals": ("market_radar", "roundup"),
}

UPDATE_STATUSES = {"ENTRY_TRIGGERED", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "INVALIDATED", "EXPIRED", "AMBIGUOUS"}
MAX_SCORE = 95  # analyze() caps the technical score here


# ---- small helpers -------------------------------------------------------------

def _coin(s: dict[str, Any]) -> str:
    return str(s.get("symbol", "")).upper().replace("USDT", "")


def _num(x: Any) -> str:
    """Price as text: no exponent, no float noise."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "N/A"
    r = repr(v)
    return format(v, ".10f").rstrip("0").rstrip(".") if "e" in r.lower() else r


def _pct(x: float) -> str:
    return f"{x:.1f}%" if abs(x) >= 1 else f"{x:.2f}%"


def _ago(n: Any) -> str:
    return "on the latest closed candle" if not n else f"{n} closed candle{'s' if n != 1 else ''} ago"


def _icon(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def _tf(s: dict[str, Any]) -> str:
    return str(s.get("timeframe") or "").upper() or "chart"


def _evidence(s: dict[str, Any]) -> dict[str, Any]:
    raw = s.get("evidence")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


def _variant(s: dict[str, Any], category: str, n: int, shift: int = 0) -> int:
    """Stable pick in [0, n): same signal + category always gives the same wording."""
    key = f"{s.get('symbol', '')}|{s.get('id', '')}|{category}".encode()
    return (zlib.crc32(key) + shift) % n


def _pick(seq: tuple[str, ...], s: dict[str, Any], category: str, shift: int = 0) -> str:
    return seq[_variant(s, category, len(seq), shift)]


def _geometry(s: dict[str, Any]) -> dict[str, Any] | None:
    """Entry midpoint plus risk/reward maths; None when the levels are unusable."""
    try:
        lo, hi, sl = float(s["entry_low"]), float(s["entry_high"]), float(s["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return None
    if lo > hi:
        lo, hi = hi, lo
    mid = (lo + hi) / 2
    if mid <= 0:
        return None
    long = s.get("side") == "LONG"
    risk = mid - sl if long else sl - mid
    stop_ok = (sl < lo if long else sl > hi) and risk > 0
    targets: list[tuple[str, float, float]] = []  # (label, price, gain%)
    prev = mid
    for label in ("tp1", "tp2", "tp3"):
        try:
            tp = float(s[label])
        except (KeyError, TypeError, ValueError):
            continue
        beyond = tp > prev if long else tp < prev
        if beyond and (tp > hi if long else tp < lo):
            targets.append((label.upper(), tp, abs(tp - mid) / mid * 100))
            prev = tp
    rr = rr_label = None
    if stop_ok and targets:
        pick = next((t for t in targets if t[0] == "TP2"), targets[-1])
        rr, rr_label = round(abs(pick[1] - mid) / risk, 2), pick[0]
    return {
        "lo": lo, "hi": hi, "mid": mid, "sl": sl, "stop_ok": stop_ok,
        "stop_pct": abs(mid - sl) / mid * 100, "targets": targets, "rr": rr, "rr_label": rr_label,
    }


def risk_reward(s: dict[str, Any]) -> float | None:
    g = _geometry(s)
    return g["rr"] if g else None


def _score_line(s: dict[str, Any]) -> str:
    return f"Technical Setup Score: {_v(s, 'score')}/{MAX_SCORE}"


def _v(s: dict[str, Any], key: str, default: str = "N/A") -> Any:
    val = s.get(key)
    return val if val is not None else default


def _setup_block(s: dict[str, Any], bias_line: str | None = None) -> str:
    g = _geometry(s)
    side = s.get("side", "LONG")
    lines = [bias_line or f"{_icon(side)} Bias: {side}"]
    lines.append(f"📍 Entry: {_num(s.get('entry_low'))} – {_num(s.get('entry_high'))}")
    if g:
        stop_txt = f"🛑 Stop: {_num(s.get('stop_loss'))} ({_pct(g['stop_pct'])} from entry)" if g["stop_ok"] else f"🛑 Stop: {_num(s.get('stop_loss'))}"
        lines.append(stop_txt)
        for label, price, gain in g["targets"]:
            lines.append(f"🎯 {label}: {_num(price)} (+{_pct(gain)})")
        if g["rr"] is not None:
            lines.append(f"📊 R:R to {g['rr_label']}: {g['rr']}:1")
    else:
        lines.append(f"🛑 Stop: {_num(s.get('stop_loss'))}")
        lines += [f"🎯 {k.upper()}: {_num(s.get(k))}" for k in ("tp1", "tp2", "tp3") if s.get(k) is not None]
    return "\n".join(lines)


def _invalidation(s: dict[str, Any], level: float | None = None) -> str:
    side = s.get("side", "LONG")
    word = "below" if side == "LONG" else "above"
    extra = ""
    if level is not None:
        extra = f", or on a close back {'under' if side == 'LONG' else 'over'} {_num(level)}"
    return f"⚠️ Invalid {word} {_num(s.get('stop_loss'))}{extra}. Do not chase outside the entry zone."


# ---- evidence ------------------------------------------------------------------

@dataclass
class Evidence:
    kind: str
    text: str
    short: str
    priority: int  # lower = stronger


def collect_evidence(s: dict[str, Any], mc: dict[str, Any] | None = None) -> list[Evidence]:
    """Facts that support the signal's side, strongest first. Built only from stored data.

    Facts that argue against the side are never returned here (see `watch_outs`), so a post can't
    list contradictory support. Every item maps to a field of the signal or its evidence block.
    """
    mc = mc or {}
    ev = _evidence(s)
    side = s.get("side", "LONG")
    long = side == "LONG"
    out: list[Evidence] = []
    tf = _tf(s)

    br = ev.get("breakout") or {}
    if br.get("direction") == side and br.get("level") is not None:
        level = _num(br["level"])
        verb = "above" if long else "below"
        if br.get("state") == "confirmed":
            when = _ago(br.get("candles_ago"))
            vr = f" with volume {br['volume_ratio']}x its 20-candle average" if br.get("volume_ratio") else ""
            out.append(Evidence("structure", f"Confirmed breakout: a closed {tf} candle finished {verb} {level} {when}{vr}",
                                f"closed {verb} {level}", 1))
        elif br.get("state") == "potential":
            out.append(Evidence("structure", f"Price is pressing {level}, but no closed {tf} candle is {verb} it yet. "
                                "This is a potential breakout, not a confirmed one",
                                f"pressing {level}, breakout not confirmed", 3))
    rt = ev.get("retest") or {}
    if rt.get("state") == "holding" and br.get("level") is not None and br.get("direction") == side:
        out.append(Evidence("retest", f"A retest of {_num(br['level'])} held on a closed candle", "retest holding", 2))

    rsi_v = s.get("rsi")
    rv = ev.get("reversal") or {}
    if rsi_v is not None:
        if long and rsi_v < 35 or (not long and rsi_v > 65):
            bits = [f"RSI {rsi_v} is {'oversold' if long else 'overbought'}"]
            level = s.get("support") if long else s.get("resistance")
            if rv.get("at_level") and level is not None:
                bits.append(f"price is testing {'support' if long else 'resistance'} {_num(level)}")
            if rv.get("candle_turn"):
                bits.append(f"the last closed candle turned {'up' if long else 'down'}")
            out.append(Evidence("reversal", "; ".join(bits), f"RSI {rsi_v} {'oversold' if long else 'overbought'}", 2))
        elif (long and 50 <= rsi_v <= 70) or (not long and 30 <= rsi_v <= 50):
            out.append(Evidence("rsi", f"RSI {rsi_v} shows {'bullish' if long else 'bearish'} momentum without being stretched",
                                f"RSI {rsi_v}", 5))

    vr_live = s.get("volume_ratio")
    if vr_live is not None and vr_live >= 1.3 and br.get("state") != "confirmed":
        out.append(Evidence("volume", f"Current {tf} volume is {vr_live}x its 20-candle average", f"volume {vr_live}x average", 3))

    e20, e50, e200 = s.get("ema20"), s.get("ema50"), s.get("ema200")
    if None not in (e20, e50, e200):
        if (long and e20 > e50 > e200) or (not long and e20 < e50 < e200):
            sign = ">" if long else "<"
            out.append(Evidence("trend", f"EMA20 {_num(e20)} {sign} EMA50 {_num(e50)} {sign} EMA200 {_num(e200)}: "
                                f"{'bullish' if long else 'bearish'} trend structure", "EMA stack aligned", 4))
        elif (long and e20 > e50) or (not long and e20 < e50):
            out.append(Evidence("trend", f"EMA20 {_num(e20)} has crossed {'above' if long else 'below'} EMA50 {_num(e50)}",
                                "EMA20/50 cross", 6))

    g = _geometry(s)
    if g and g["rr"] is not None and g["rr"] >= 1.5:
        out.append(Evidence("rr", f"Risk/reward to {g['rr_label']} is {g['rr']}:1 from the middle of the entry zone", f"R:R {g['rr']}:1", 4))

    price = s.get("price")
    far = s.get("resistance") if long else s.get("support")
    if g and far is not None and price:
        room = (far - g["mid"]) / g["mid"] * 100 if long else (g["mid"] - far) / g["mid"] * 100
        if room > 0.3:
            out.append(Evidence("room", f"Nearest {'resistance' if long else 'support'} {_num(far)} is {_pct(room)} from entry", "room to the next level", 7))

    adx = ev.get("adx")
    if ev.get("regime") == "TRENDING" and adx:
        out.append(Evidence("regime", f"{tf} regime is trending (ADX {adx})", "trending regime", 8))
    if mc.get("regime") and ((long and str(mc["regime"]).startswith("bull")) or (not long and str(mc["regime"]).startswith("bear"))):
        out.append(Evidence("btc", f"BTC regime is {mc['regime']}, in line with this {side}", "BTC regime aligned", 9))

    best: dict[str, Evidence] = {}
    for item in sorted(out, key=lambda e: e.priority):
        best.setdefault(item.kind, item)
    return sorted(best.values(), key=lambda e: e.priority)


def watch_outs(s: dict[str, Any]) -> list[str]:
    """Facts that argue against the setup. Shown honestly in deep dives."""
    notes: list[str] = []
    long = s.get("side") == "LONG"
    rsi_v, vr = s.get("rsi"), s.get("volume_ratio")
    if rsi_v is not None and ((long and rsi_v > 70) or (not long and rsi_v < 30)):
        notes.append(f"RSI {rsi_v} is already {'overbought' if long else 'oversold'}, so the move may be stretched.")
    if vr is not None and vr < 0.8:
        notes.append(f"Volume is only {vr}x average, so participation is thin.")
    price, g = s.get("price"), _geometry(s)
    if price and g:
        away = (price - g["hi"]) / price * 100 if long else (g["lo"] - price) / price * 100
        if away > 0.5:
            notes.append(f"Price is {_pct(away)} {'above' if long else 'below'} the entry zone; waiting for a pullback avoids chasing.")
    return notes


def _why_now(items: list[Evidence], s: dict[str, Any], n: int) -> str:
    if items:
        lines = [f"• {e.text}" for e in items[:n]]
    else:
        lines = [f"• Levels come from recent {_tf(s)} price action: support {_num(s.get('support'))}, resistance {_num(s.get('resistance'))}."]
    return "Why now?\n" + "\n".join(lines)


# ---- text checks -------------------------------------------------------------------

def check_post_text(text: str) -> list[str]:
    """Return every rule the text breaks; an empty list means it is clean."""
    problems: list[str] = []
    low = text.lower()
    problems += [f"banned phrase: {p}" for p in BANNED_PHRASES if p in low]
    if _LEVERAGE_RE.search(text):
        problems.append("leverage suggestion")
    if _CONFIDENCE_RE.search(text):
        problems.append("score presented as a percentage chance")
    if _SPACED_PAIR_RE.search(text):
        problems.append("malformed cashtag/pair")
    tags = [t.upper() for t in _CASHTAG_RE.findall(text)]
    if len(set(tags)) > MAX_CASHTAGS:
        problems.append(f"more than {MAX_CASHTAGS} distinct cashtags")
    if len(tags) != len(set(tags)):
        problems.append("duplicated cashtag")
    return problems


# ---- engine -----------------------------------------------------------------------------

class PostTemplateEngine:
    """Deterministic, rule-based post generator. No LLM calls and no randomness."""

    def __init__(self) -> None:
        self.last_template: str = ""
        self.last_variant: str = ""

    # ---- public API ----

    def generate(
        self,
        signal: dict[str, Any],
        template_name: str = "auto",
        market_context: dict[str, Any] | None = None,
    ) -> str:
        mc = market_context or {}
        category, variant = self.resolve(signal, template_name, mc)
        recent = mc.get("recent_templates") or []
        shift = 1 if recent and recent[-1] == category else 0  # vary wording after a repeat
        body = getattr(self, f"_{category}")(signal, mc, variant, shift)
        self.last_template, self.last_variant = category, variant
        return body.strip()

    def resolve(self, s: dict[str, Any], name: str, mc: dict[str, Any]) -> tuple[str, str]:
        if name in CATEGORIES:
            return name, ""
        if name in ALIASES:
            return ALIASES[name]
        return self._select_auto(s, mc)

    # ---- auto selection (rule based) ----

    def _select_auto(self, s: dict[str, Any], mc: dict[str, Any]) -> tuple[str, str]:
        status = s.get("status") or "NEW"
        if status in UPDATE_STATUSES:
            return "signal_update", ""
        if str(s.get("symbol", "")).upper().startswith("BTC") and mc.get("regime"):
            return "market_radar", "btc"
        score = s.get("score") or 0
        if score < 55:
            return "market_radar", "watchlist"
        ev = _evidence(s)
        side = s.get("side")
        br = ev.get("breakout") or {}
        opp = mc.get("opportunity") or {}
        if (br.get("state") == "confirmed" and br.get("direction") == side) or "unusual_volume" in (opp.get("labels") or []):
            return "breaking_momentum", ""
        rsi_v = s.get("rsi")
        if rsi_v is not None and ((side == "LONG" and rsi_v < 35) or (side == "SHORT" and rsi_v > 65)):
            return "reversal_watch", ""
        if score >= 80 and len(collect_evidence(s, mc)) >= 4:
            return "technical_deep_dive", ""
        return "trade_setup", ""

    # ---- category: trade setup ----

    def _trade_setup(self, s, mc, variant, shift) -> str:
        coin, side, items = _coin(s), s.get("side", "LONG"), collect_evidence(s, mc)
        lead = items[0].short if items else "levels to watch"
        hook = f"{_icon(side)} ${coin} {side} setup on the {_tf(s)} — {lead}"
        if variant == "debate":
            hook = f"👀 {self._question(s, 'debate', shift)}\n\n{hook}"
        parts = [hook]
        if variant == "compact":
            parts.append(_setup_block(s))
            parts.append(_score_line(s))
        else:
            parts.append(_why_now(items, s, 2))
            parts.append(_setup_block(s))
            if variant == "dca":
                g = _geometry(s)
                atr = s.get("atr")
                if g and atr and (g["hi"] - g["lo"]) > atr * 0.45:
                    parts.append(f"The entry zone is wide ({_num(g['lo'])}–{_num(g['hi'])}), so scaling in across it is an option instead of one fill.")
        parts += [_invalidation(s), self._question(s, "trade_setup", shift) + "\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    # ---- category: technical deep dive ----

    def _technical_deep_dive(self, s, mc, variant, shift) -> str:
        coin, side, items = _coin(s), s.get("side", "LONG"), collect_evidence(s, mc)
        hook = f"📈 ${coin} {side} on the {_tf(s)}: {len(items)} data points line up" if len(items) >= 2 else f"📈 ${coin} {side} on the {_tf(s)} — technical breakdown"
        stats = [f"EMA20/50/200: {_num(s.get('ema20'))} / {_num(s.get('ema50'))} / {_num(s.get('ema200'))}",
                 f"RSI {_v(s, 'rsi')} | ATR {_num(s.get('atr'))} | Volume {_v(s, 'volume_ratio')}x average"]
        parts = [hook, _setup_block(s)]
        parts.append("Technical evidence:\n" + "\n".join(f"• {e.text}" for e in items[:5]) if items else "Technical evidence:\n" + "\n".join(f"• {x}" for x in stats))
        if items:
            parts.append("Indicators:\n" + "\n".join(stats))
        outs = watch_outs(s)
        if outs:
            parts.append("Watch-outs:\n" + "\n".join(f"• {x}" for x in outs))
        parts.append(f"{_score_line(s)} (a rule-based rating of the setup, not a win probability)")
        parts += [_invalidation(s), self._question(s, "technical_deep_dive", shift) + "\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    # ---- category: breaking momentum ----

    def _breaking_momentum(self, s, mc, variant, shift) -> str:
        coin, side = _coin(s), s.get("side", "LONG")
        long = side == "LONG"
        ev = _evidence(s)
        br, rt = ev.get("breakout") or {}, ev.get("retest") or {}
        opp = mc.get("opportunity") or {}
        confirmed = br.get("state") == "confirmed" and br.get("direction") == side and br.get("level") is not None
        verb = "above" if long else "below"
        facts: list[str] = []
        if confirmed:
            level = _num(br["level"])
            hook = f"👀 ${coin} closed {verb} {level} on the {_tf(s)}. Is the {'retest' if rt.get('state') != 'holding' else 'follow-through'} worth trading?"
            vr = f" with volume {br['volume_ratio']}x its 20-candle average" if br.get("volume_ratio") else ""
            when = _ago(br.get("candles_ago"))
            facts.append(f"{coin} closed {verb} {level} {when}{vr}.")
            if rt.get("state") == "holding":
                facts.append(f"A later retest of {level} held on a closed candle.")
            bias = f"{_icon(side)} Bias: {side} while price holds {verb} {level}"
        else:
            rel = opp.get("rel_vol_15m") or s.get("volume_ratio")
            hook = f"👀 ${coin} volume is running at {rel}x its baseline. No confirmed breakout yet" if rel else f"👀 ${coin} is moving. No confirmed breakout yet"
            if br.get("state") == "potential" and br.get("level") is not None:
                facts.append(f"Price is pressing {_num(br['level'])}, but no closed {_tf(s)} candle is {verb} it. Treat it as a potential breakout.")
            facts.append("Entry is planned inside the zone below, not at the current price.")
            bias = f"{_icon(side)} Bias: potential {side}, needs confirmation"
        if opp.get("rel_vol_15m") and confirmed:
            facts.append(f"15m volume is {opp['rel_vol_15m']}x its recent baseline.")
        if opp.get("change_1h") is not None:
            facts.append(f"Price is {opp['change_1h']:+.1f}% over the last hour.")
        extra = [e for e in collect_evidence(s, mc) if e.kind not in ("structure", "retest", "volume")][:1]
        parts = [hook, " ".join(facts), _setup_block(s, bias)]
        if extra:
            parts.append(_why_now(extra, s, 1))
        parts += [_invalidation(s, br["level"] if confirmed else None),
                  self._question(s, "breaking_momentum", shift) + "\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    # ---- category: reversal watch ----

    def _reversal_watch(self, s, mc, variant, shift) -> str:
        coin, side = _coin(s), s.get("side", "LONG")
        long = side == "LONG"
        items = [e for e in collect_evidence(s, mc) if e.kind == "reversal"]
        rest = [e for e in collect_evidence(s, mc) if e.kind != "reversal"]
        rv = (_evidence(s).get("reversal") or {})
        state = "confirmed on a closed candle" if rv.get("confirmed") else "not confirmed yet"
        lead = items[0].short if items else "possible turn"
        hook = f"🔄 ${coin} reversal watch on the {_tf(s)}: {lead} (turn {state})"
        trigger_level = s.get("entry_high") if long else s.get("entry_low")
        confirm = (f"Confirmation to look for: a {_tf(s)} candle closing {'back above' if long else 'back below'} {_num(trigger_level)}. "
                   "Until then this is a watch, not an entry.")
        if rv.get("confirmed"):
            confirm = "The closed-candle turn has already printed; the entry zone below is the plan."
        parts = [hook]
        if items:
            parts.append(_why_now(items + rest, s, 2))
        parts += [_setup_block(s, f"{_icon(side)} Bias: {side} reversal attempt"), confirm, _invalidation(s),
                  self._question(s, "reversal_watch", shift) + "\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    # ---- category: signal update ----

    def _signal_update(self, s, mc, variant, shift) -> str:
        coin, side = _coin(s), s.get("side", "LONG")
        status = s.get("status") or "NEW"
        g = _geometry(s)
        ev = _evidence(s)
        check = ev.get("last_check") or {}
        price = check.get("price")
        ref = f"Original signal: {coin} {side}, {_tf(s)}"
        if s.get("id"):
            ref += f" (signal #{s['id']})"
        created = self._parse_time(s.get("created_at"))
        now = self._parse_time(check.get("time")) or mc.get("now") or datetime.now(timezone.utc)
        if created:
            ref += f", posted {created.strftime('%Y-%m-%d %H:%M')} UTC{self._offset(created)}"
            ref += f", {self._elapsed(now - created)} ago" if now > created else ""
        lines = [ref]
        plan = f"Plan: entry {_num(s.get('entry_low'))}–{_num(s.get('entry_high'))}, stop {_num(s.get('stop_loss'))}"
        if price is not None:
            lines.append(f"Last checked price: {_num(price)}" + (f" ({check['time'][:16].replace('T', ' ')})" if check.get("time") else ""))
        move_to = lambda p: (abs(p - g["mid"]) / g["mid"] * 100) if g else None
        if status.endswith("_HIT") and status.startswith("TP"):
            idx = int(status[2])
            tp = s.get(f"tp{idx}")
            gain = move_to(float(tp)) if tp is not None else None
            hook = f"🎯 ${coin} reached TP{idx}"
            body = [f"The {side} plan reached its TP{idx} level at {_num(tp)}" + (f", a {_pct(gain)} move from the middle of the entry zone." if gain is not None else ".")]
            rest = [t for t in (g["targets"] if g else []) if int(t[0][2]) > idx]
            if rest:
                body.append("Remaining: " + " | ".join(f"{t[0]} {_num(t[1])}" for t in rest) + ". Managing the stop after a target is your call.")
        elif status == "ENTRY_TRIGGERED":
            hook = f"⚡ ${coin} entered the {_num(s.get('entry_low'))}–{_num(s.get('entry_high'))} zone"
            body = [plan + (f", first target {_num(s.get('tp1'))}." if s.get("tp1") is not None else "."),
                    "Entering the zone does not confirm the setup; the stop still defines it."]
        elif status == "SL_HIT":
            hook = f"🛑 ${coin} hit the stop at {_num(s.get('stop_loss'))}"
            loss = move_to(float(s["stop_loss"])) if g else None
            body = ["The setup failed" + (f": {_pct(loss)} against the middle of the entry zone." if loss is not None else ".") + " Losses like this are part of the plan, which is why the stop was set."]
        elif status == "INVALIDATED":
            hook = f"❌ ${coin} setup invalidated before entry"
            body = [f"Price traded through the stop level {_num(s.get('stop_loss'))} before the entry zone filled, so the plan is off."]
        elif status == "EXPIRED":
            hook = f"⌛ ${coin} setup expired"
            body = ["Price never reached the entry zone within the setup window, so it is closed without a trade."]
        elif status == "AMBIGUOUS":
            hook = f"❓ ${coin} outcome unclear"
            body = [check.get("note") or "A single candle touched both the stop and a target, so the order of events cannot be confirmed from candle data."]
        else:
            hook = f"⏳ ${coin} still waiting for entry"
            body = [plan + ". Price has not reached the entry zone yet."]
        parts = [hook, "\n".join(lines), " ".join(body), HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _offset(dt: datetime) -> str:
        off = dt.utcoffset()
        hours = int(off.total_seconds() // 3600) if off else 0
        return f"{hours:+d}"

    @staticmethod
    def _elapsed(delta: Any) -> str:
        minutes = max(int(delta.total_seconds() // 60), 0)
        days, rem = divmod(minutes, 1440)
        hours, mins = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h"
        return f"{hours}h {mins}m" if hours else f"{mins}m"

    # ---- category: market radar ----

    def _market_radar(self, s, mc, variant, shift) -> str:
        if variant == "roundup":
            return self._roundup(s, mc)
        if variant == "watchlist":
            return self._watchlist(s, mc, shift)
        return self._btc(s, mc, shift)

    def _btc(self, s, mc, shift) -> str:
        price = s.get("price") if s.get("price") is not None else s.get("entry_high")
        r1 = mc.get("resistance_1", s.get("resistance"))
        s1 = mc.get("support_1", s.get("support"))
        r2, s2 = mc.get("resistance_2"), mc.get("support_2")
        regime, bias = mc.get("regime", "neutral"), mc.get("bias", s.get("side", "NEUTRAL"))
        try:
            up = (float(r1) - float(price)) / float(price) * 100
            down = (float(price) - float(s1)) / float(price) * 100
            hook = f"₿ $BTC trades at {_num(price)}, {_pct(up)} below resistance {_num(r1)} and {_pct(down)} above support {_num(s1)}"
        except (TypeError, ValueError, ZeroDivisionError):
            hook = f"₿ $BTC market update at {_num(price)}"
        lines = [hook, f"Key levels on the {_tf(s)}:\n🟢 Support: {_num(s1)}" + (f" / {_num(s2)}" if s2 is not None else "")
                 + f"\n🔴 Resistance: {_num(r1)}" + (f" / {_num(r2)}" if r2 is not None else "")]
        up_t = f"Bullish: a {_tf(s)} close above {_num(r1)} opens the way toward {_num(r2)}." if r2 is not None else f"Bullish: a {_tf(s)} close above {_num(r1)} is the first sign of strength."
        dn_t = f"Bearish: losing {_num(s1)} puts {_num(s2)} in play." if s2 is not None else f"Bearish: losing {_num(s1)} would weaken the structure."
        lines += [up_t + "\n" + dn_t, f"EMA regime: {regime} | Model bias: {bias}\n{_score_line(s)}",
                  self._question(s, "btc", shift) + "\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(lines)

    def _watchlist(self, s, mc, shift) -> str:
        coin, side = _coin(s), s.get("side", "LONG")
        items = collect_evidence(s, mc)
        low = (s.get("score") or 0) < 55
        status = ("That is below the level this studio treats as a trade setup." if low
                  else "It still needs confirmation before it counts as a trade setup.")
        parts = [f"👀 ${coin} is on the watchlist: a possible {side} on the {_tf(s)}, not a confirmed setup yet",
                 f"{_score_line(s)}. {status}",
                 _why_now(items, s, 2),
                 f"Area to watch: {_num(s.get('entry_low'))} – {_num(s.get('entry_high'))}. No stop or targets are offered until it is confirmed.",
                 "Would you add it to your watchlist?\n\n" + HASHTAGS, _pick(DYOR_LINES, s, "dyor", shift)]
        return "\n\n".join(parts)

    def _roundup(self, s, mc) -> str:
        # Only the top MAX_CASHTAGS coins get a real $CASHTAG; the rest are plain text so the post
        # stays under Binance Square's distinct-coin-pair limit (error 220095).
        seen: set[str] = set()
        signals = []
        for sig in sorted(mc.get("signals", []), key=lambda x: x.get("score", 0), reverse=True):
            if _coin(sig) not in seen:
                seen.add(_coin(sig))
                signals.append(sig)
        signals = signals[:10]
        tagged = {_coin(sig) for sig in signals[:MAX_CASHTAGS]}

        def label(sig: dict[str, Any]) -> str:
            coin = _coin(sig)
            return f"${coin}" if coin in tagged else coin

        lines = []
        for i, sig in enumerate(signals, 1):
            lines.append(f"{i}. {label(sig)} {sig.get('side')} | Technical Setup Score {sig.get('score')}/{MAX_SCORE}\n"
                         f"   Entry {_num(sig.get('entry_low'))} – {_num(sig.get('entry_high'))}")
        longs = [x for x in signals if x.get("side") == "LONG"]
        shorts = [x for x in signals if x.get("side") == "SHORT"]
        summary = []
        if longs:
            summary.append(f"Highest-scoring LONG: {_coin(longs[0])} ({longs[0].get('score')}/{MAX_SCORE})")
        if shorts:
            summary.append(f"Highest-scoring SHORT: {_coin(shorts[0])} ({shorts[0].get('score')}/{MAX_SCORE})")
        summary.append(f"BTC regime: {mc.get('btc_regime', 'neutral')}")
        hook = f"🔥 {len(signals)} setups on the radar: {len(longs)} long, {len(shorts)} short"
        return "\n\n".join([hook, "\n".join(lines), "\n".join(summary),
                            "Scores rank setups by technical rules. They are not win probabilities.",
                            HASHTAGS, _pick(DYOR_LINES, s, "dyor")])

    # ---- shared copy ----

    def _question(self, s: dict[str, Any], key: str, shift: int = 0) -> str:
        coin = _coin(s)
        long = s.get("side") == "LONG"
        pools = {
            "trade_setup": ("Would you wait for the entry zone or skip this one?",
                            f"Is this a level you'd trade, or would you wait for more confirmation on {coin}?",
                            "Where would you place your stop on this one?"),
            "technical_deep_dive": (f"Do these signals convince you, or is something missing for {coin}?",
                                    "Which of these data points would you weigh most?",
                                    "Would you take this setup as is, or wait for a pullback?"),
            "breaking_momentum": ("Would you wait for confirmation or skip this setup?",
                                  f"Chase it, wait for the retest, or pass on {coin}?",
                                  "Does the volume convince you?"),
            "reversal_watch": ("Would you wait for the confirming close or pass?",
                               f"Is this a real turn for {coin} or just a pause?",
                               "What would make you trust this reversal?"),
            "btc": ("Do you expect a breakout above resistance or another test of support?",
                    "Which side gives way first?",
                    "Where are you watching BTC next?"),
            "debate": (f"Breakout continuation or one more sweep before it runs. Where do you stand on {coin}?" if long else f"Rolling over, or one more leg up first for {coin}?",
                       f"Is this the bottom for {coin}, or only a bounce?" if long else f"Distribution or just a pause for {coin}?",
                       f"Reclaim and go, or fakeout for {coin}?" if long else f"Trap for shorts, or is {coin} really rolling over?"),
        }
        return _pick(pools[key if key in pools else "trade_setup"], s, key, shift)


ALL_TEMPLATE_NAMES = CATEGORIES + tuple(ALIASES.keys())
