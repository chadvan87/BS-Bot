# Binance Square Signal Studio: Post Review Brief

## Purpose of this document

This brief explains what our bot posts to Binance Square, how each post is produced, and where we think the quality is weak. We want you to review the post copy and suggest changes that make the posts more useful, more credible, and more engaging, without making them misleading.

## What the project is

Binance Square Signal Studio is a web app (FastAPI + SQLite/Postgres + a plain HTML/JS front end). It does four things:

1. **Scans** Binance USDT pairs and produces a trade signal for each: LONG or SHORT, an entry zone, a stop-loss, three take-profit targets, and a score from 1 to 95.
2. **Generates** a draft post for a signal. A deterministic Python template engine writes the copy. No AI or LLM writes the text, and each style is a fixed template with a few randomised phrases.
3. **Generates a chart image** for the draft: dark theme, candlesticks, EMA20/50/200, volume and RSI panels, entry/SL/TP lines, support/resistance, and a LONG/SHORT badge.
4. **Publishes** to Binance Square through Binance's official `square-post` scripts, either immediately (a human confirms each "Post now") or on a schedule set in the app.

A person always reviews and confirms a post before it goes out.

## Where the numbers in a post come from

All figures come from simple technical analysis on candle data. There is no fundamental, on-chain or news input.

- **Indicators:** EMA20, EMA50, EMA200, RSI(14), ATR, ADX, Bollinger Bands, volume versus the 20-candle average, and the 40-candle high and low as support and resistance.
- **Market regime:** ADX of 23 or more counts as "TRENDING". Anything lower counts as "RANGING".
- **Score:** Both sides start at 50. In a trending market, points are added or removed for EMA alignment, volume with candle direction, RSI band, and breakouts. In a ranging market, points are added for Bollinger band touches, proximity to support or resistance, and RSI extremes. The higher side wins, and the score is capped at 95.
- **Levels:**
  - Entry zone: LONG is roughly price minus 0.35 ATR to price plus 0.05 ATR. SHORT mirrors it.
  - Stop-loss: 1.2 ATR from price, or just beyond support or resistance if that is further.
  - Targets: TP1, TP2 and TP3 at 1.2R, 2R and 3R, capped at the nearest support or resistance.

The score is a heuristic ranking. It is not a measured probability or win rate, although some templates label it "confidence". Please keep that in mind when judging the wording.

## The 18 post styles

The engine picks a style automatically from the signal data, or the user picks one from a dropdown. It will not use the same style twice in a row.

### Trade-setup posts (new signals)

| Style | When the engine picks it | Angle |
|---|---|---|
| quick_signal | Default fallback | Short card: entry, SL, three TPs, score, "do not chase" |
| long_setup / short_setup | Fallback by side; short_setup also when RSI is above 65 on a SHORT | Plain levels plus "Why this setup?" bullets |
| creator_hype | Score of 85 or more | Alarm-style headline about a "decisive zone" |
| technical_breakdown | Score of 85 or more | Levels plus an indicator stats block |
| contrarian_hook | SHORT with RSI above 65 | "Everyone is watching X, but..." plus a debate question |
| reversal_setup | LONG with RSI below 35 | Oversold bounce angle |
| dca_zone | Entry zone wider than 0.45 ATR | Suggests splitting entries across the range |
| breakout_retest | No other rule matched | Retest of a broken level |
| debate_post | Fallback | Opens with a bull-or-bear question to drive comments |
| watchlist_post | Score below 55 | "On watch", explicitly not a signal yet |

### BTC posts

| Style | Angle |
|---|---|
| btc_market_analysis | Support and resistance ladder, a bullish and a bearish scenario, the regime and bias |
| btc_decisive_zone | Short "BTC at a decisive zone" post with key levels and a question |

### Status-update posts (follow-ups on an earlier signal)

The app checks live price against a signal's levels and sets a status. The matching template then writes the follow-up.

| Style | Trigger |
|---|---|
| entry_triggered_update | Price entered the entry zone |
| tp_hit_update | TP1, TP2 or TP3 reached; suggests trailing the stop |
| stop_loss_update | Stop-loss hit |
| invalidated_setup | Structure broke before entry |

### Round-up post

| Style | Angle |
|---|---|
| top_10_signals | Ranked list of the 10 best-scoring setups, strongest long and short, BTC regime, and "max 3 active setups" |

## Fixed building blocks in every post

- **Disclaimer:** one of 4 rotating "not financial advice / DYOR" lines.
- **Call to action:** one of 4 rotating "Trade 👇 $COIN" lines.
- **Debate questions:** 3 long-side and 3 short-side questions.
- **"Why this setup?" bullets:** 2 to 4 reasons picked at random from a pool, built from EMA structure, RSI, volume ratio, support/resistance, ATR, risk/reward, distance from price to the entry zone, and BTC regime.
- **Banned phrases** (in the code): "guaranteed win", "guaranteed profit", "easy money", "cannot lose", "100% signal", "risk-free" and similar.
- **Cashtag limit:** Binance rejects a post that mentions too many distinct coin pairs (error 220095). The Top 10 round-up therefore tags only the top 5 coins with `$` and lists the rest as plain text.

## Sample output

These are real outputs from the engine for a sample SOL LONG signal (score 78, 1h timeframe, RSI 33.4). The chart image is attached to the post when one is generated.

**quick_signal**

```
🟢 $SOL LONG SETUP

Entry: 142.1 – 143.4
SL: 139.8
TP: 146.2 / 149.5 / 154.0

⚡ 78% confidence | 1h
Do not chase outside the entry zone.

Trade 👇 $SOL
```

**technical_breakdown**

```
📈 $SOL TECHNICAL BREAKDOWN

$SOL /USDT — LONG

📍 Entry: 142.1 – 143.4
🛑 Stop-loss: 139.8

Targets 🎯
TP1: 146.2
TP2: 149.5
TP3: 154.0

📊 Technicals
EMA20/50/200: 143.5 / 141.9 / 136.2
RSI: 33.4 | ATR: 2.9 | Vol ratio: 1.45
Score: 78/100 | TF: 1h

Why this setup?
• BTC regime reads bullish, a backdrop worth weighing on altcoin setups
• Risk/reward to TP2 works out to roughly 2.29:1

⚠️ Trade your own plan. This is analysis, not financial advice.

Watching this closely 👀 $SOL
```

**reversal_setup**

```
🔄 $SOL REVERSAL SETUP

Momentum has stretched far enough that a bounce is worth watching for.

$SOL /USDT — LONG

📍 Entry: 142.1 – 143.4
🛑 Stop-loss: 139.8

Targets 🎯
TP1: 146.2
TP2: 149.5
TP3: 154.0

Why this setup?
• EMA20 (143.5) > EMA50 (141.9) > EMA200 (136.2) — clean bullish trend structure
• Key support at 140.5, resistance at 149.0
• Volume running 1.45x the 20-period average — real participation behind the move
• ATR of 2.9 — use this to size positions for current volatility

⚠️ Always DYOR — this is not financial advice.

Click below to trade 👇
$SOL
```

**tp_hit_update**

```
🎯 TP1 HIT on $SOL!

Entry was 142.1–143.4, and price has now tagged this target.

Remaining targets: TP2 149.5 | TP3 154.0
Consider trailing stop to protect gains.

⚠️ Not financial advice. Manage your risk and size positions responsibly.
```

## Problems we already see

1. **Posts look alike.** Most styles share one skeleton: headline, levels block, bullets, disclaimer, "Trade 👇". Readers on Square will see the same shape on every post.
2. **Bullets can contradict the headline.** The reasons are picked at random from a pool. The sample reversal post is about an oversold RSI, yet its bullets never mention RSI and instead describe a "clean bullish trend structure". The sample technical breakdown shows two weak bullets out of a possible six.
3. **Filler bullets.** "ATR of 2.9 — use this to size positions" and "BTC regime reads bullish, a backdrop worth weighing" carry no argument for the trade.
4. **"Confidence %" is misleading.** It is a heuristic score and not a probability. We would like wording that does not imply a win rate.
5. **Generic hooks.** "IS MOVING INTO A DECISIVE ZONE" and "Everyone is watching the crowd..." are used with no coin-specific content behind them.
6. **No coin context.** The posts say nothing about what the coin is, recent news, or why it matters now.
7. **Small text glitch.** The levels block prints `$SOL /USDT` with a stray space before the slash.
8. **Status updates are thin.** There is no price, no percent gain or loss, no time held, and no link back to the original post.
9. **Little variety in the call to action and disclaimer** (4 lines each), and numbers are shown without percentages (distance to stop, distance to each target).
10. **No Vietnamese or other-language version.** Posts are English only. Tell us if you think a second language would help reach.

## What we would like from you

Please review the styles and the samples above and tell us:

1. Which styles you would keep, merge, or drop.
2. A better structure for a trade-setup post. For example: a hook, the one-line thesis, the levels, why now, what invalidates it, and the risk note.
3. Better hooks and headlines for each style.
4. How to word the score so it is honest but still persuasive.
5. A better way to choose the "why" bullets so they always support the post's angle.
6. Ideas to raise engagement on Square: the right length, emoji use, hashtags, and questions that get comments.
7. A review of the disclaimers and any phrases that could look like financial advice or a guaranteed result.
8. Rewrites of two or three of the samples above as you would want them to read.

## Constraints to keep in mind

- All copy must be produced by deterministic code. Template text, fill-in fields, and rule-based selection are fine, and so is any rule you can describe clearly. We cannot call an AI model at runtime.
- Binance Square rejects posts that mention too many distinct coin pairs, so round-up posts have to stay under that limit.
- We never promise results. The banned-phrase list above must stay in force.
- Publishing goes through Binance's official `square-post` scripts only.

## Where the code lives

- Post templates and selection rules: `app/services/post_templates.py`
- Signal scoring and levels: the `analyze()` function in `app/main.py`
- Chart image: `app/services/chart_service.py`
- Template tests: `tests/test_post_templates.py`
