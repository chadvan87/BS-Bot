# Binance Square Signal Studio

## Run

```bash
chmod +x run.sh
./run.sh
```

Open `http://127.0.0.1:8000`.

## First-time setup

1. Click **Install official Square skill**. This installs only Binance's `square-post` skill from `binance/binance-skills-hub`.
2. Paste your Binance Square OpenAPI key and select **Real Binance Square post**.
3. Click **Save**, then **Check ready**.
4. Scan, generate/edit a draft, and click **Post now**.

The key is stored only in the local SQLite database and passed to the official script through `BINANCE_SQUARE_OPENAPI_KEY`. It is not passed in command arguments or printed by the app.

## Post generation

Post copy is written by a deterministic Python template engine (`app/services/post_templates.py`) —
no AI/LLM calls, no randomness, no external API. Six content categories: **Breaking Momentum**,
**Trade Setup**, **Technical Deep Dive**, **Reversal Watch**, **Signal Update** and **Market Radar**
(BTC analysis, watchlist, Top 10). The old 18 style names still work as aliases. Each post follows
hook → why now → trade setup → evidence → invalidation → CTA, shows stop/target distances in % and
R:R only when the levels are valid, and says "Technical Setup Score: N/95" (a rule-based rating, never a
win probability). Supporting reasons come only from the signal's real data, and a breakout is called
"confirmed" only after a closed candle. **Auto** picks the category from that evidence.

Every generated draft also gets a dark-theme candlestick chart (`app/services/chart_service.py`)
with EMA20/50/200, volume and RSI panels, entry zone, SL/TP lines, and support/resistance —
published via Binance's official `post-image.mjs` instead of text-only when a chart is attached.

**Signal Opportunities** ranks liquid Futures coins by technical quality plus fresh activity (volume surge,
closed-candle breakout/retest, funding / open interest). Binance data is fetched by the browser; the server
scores it (`app/services/opportunities.py`, thresholds editable via the `OPPORTUNITY_CONFIG` JSON setting).
Mode **Deep** scans every liquid coin with a 15m-first funnel: 3 bulk calls, one weight-1 15m call per coin,
then 1h/4h/OI only for coins with a fresh trigger (about 315 request weight total, Binance allows 2400/min).
A sliding-window limiter (`app/static/ratelimit.js`, soft cap 1000/min), caching and bounded concurrency keep it
under the limit; HTTP 418/429 stops the scan. The Opportunity Score is not a win probability. "Generate post + chart" creates a normal draft; publishing stays
human-confirmed. Signal posts always carry their chart: if the image can't be made, no draft is created and
the error is shown. Signals now store `evidence` (JSON) for this.

**Generate BTC Market Analysis** and **Generate Top 10 Watchlist** are dedicated buttons for
BTC-specific and multi-signal round-up posts. **Refresh status** on a signal checks the live
price against its entry/SL/TP levels (`ENTRY_TRIGGERED`, `TP1_HIT`, `SL_HIT`, `INVALIDATED`, ...),
which the Auto style picks up for status-update posts.

## Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/ -q
```

## Deploy (Render)

Deployed as a Docker web service via `render.yaml` (Python 3.12 + Node 22). Required settings:

| Env var | Purpose |
|---|---|
| `APP_PASSWORD` | Enables HTTP Basic auth on every page/API (except `/healthz`). **Required on a public host.** |
| `APP_USERNAME` | Basic auth username (default `admin`). |
| `DATA_DIR` | Where the SQLite DB and charts live. On Render: the persistent disk mount, `/var/data`. |
| `DATABASE_URL` | Optional. A Postgres URL (e.g. Supabase's **Session pooler** string). When set, signals/posts/settings are stored there instead of SQLite, so they survive restarts on Render's free plan. Charts still live in `DATA_DIR`. |

The Binance Square OpenAPI key is still entered in the UI and stored in the database (SQLite or Postgres) — not as an env var.
Run a single instance/worker only: the scheduler lives in-process.
