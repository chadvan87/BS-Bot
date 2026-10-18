from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.services.post_templates import check_post_text
from tests.helpers import HOUR, breakout_series, candles

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    main.init_db()
    with main.conn() as c:
        for table in ("posts", "signals"):
            c.execute(f"DELETE FROM {table}")
        c.execute("DELETE FROM settings WHERE key IN ('OPPORTUNITIES_LAST','OPPORTUNITY_CONFIG')")
        c.execute("UPDATE settings SET value='mock' WHERE key='PUBLISH_MODE'")
    main._klines_cache.clear()
    monkeypatch.setattr(main, "_binance_paused_until", 0.0)
    yield


def rows(n=120):
    return breakout_series(n, jump_at=n - 2)


def scan(symbol="SOLUSDT", data=None):
    r = client.post("/api/scan", json={"symbols": [symbol], "interval": "1h", "klines": {symbol: data or rows()}})
    assert r.status_code == 200 and not r.json()["errors"], r.text
    return r.json()["results"][0]


def post_row(post_id):
    with main.conn() as c:
        return dict(c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone())


def test_scan_stores_evidence_and_market_data_timestamp():
    sig = scan()
    with main.conn() as c:
        row = dict(c.execute("SELECT * FROM signals WHERE id=?", (sig["id"],)).fetchone())
    ev = json.loads(row["evidence"])
    assert ev["candle_time"] == rows()[-1][0]
    assert {"breakout", "retest", "reversal", "regime"} <= set(ev)


def test_generated_post_has_chart_and_clean_copy():
    sig = scan()
    r = client.post("/api/generate", json={"signal_id": sig["id"], "template_name": "auto"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert Path(d["chart_path"]).exists() and Path(d["chart_path"]).stat().st_size > 1000
    assert check_post_text(d["content"]) == []
    assert "% confidence" not in d["content"]
    assert post_row(d["id"])["chart_path"] == d["chart_path"]


def test_chart_matches_the_signal_it_belongs_to(monkeypatch):
    sig = scan()
    seen = {}
    real = main.generate_signal_chart

    def spy(candles_, signal, out, dark_theme=True):
        seen["candles"], seen["signal"] = candles_, signal
        return real(candles_, signal, out, dark_theme=dark_theme)

    monkeypatch.setattr(main, "generate_signal_chart", spy)
    # newer candles arrive after the signal was made; the chart must still end on the signal's candle
    newer = rows(120) + candles(3, start=105.0, t0=rows()[-1][0] + HOUR)
    main.remember_klines("SOLUSDT", "1h", newer)
    assert client.post("/api/generate", json={"signal_id": sig["id"]}).status_code == 200
    s = seen["signal"]
    assert (s["symbol"], s["timeframe"], s["side"]) == ("SOLUSDT", "1h", sig["side"])
    assert (s["entry_low"], s["entry_high"], s["stop_loss"], s["tp1"], s["tp2"], s["tp3"]) == (
        sig["entry_low"], sig["entry_high"], sig["stop_loss"], sig["tp1"], sig["tp2"], sig["tp3"])
    assert seen["candles"][-1][0] == rows()[-1][0]


def test_chart_failure_is_surfaced_and_no_draft_is_created(monkeypatch):
    sig = scan()

    def boom(*a, **k):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setattr(main, "generate_signal_chart", boom)
    r = client.post("/api/generate", json={"signal_id": sig["id"]})
    assert r.status_code == 502 and "Chart image failed" in r.json()["detail"] and "matplotlib exploded" in r.json()["detail"]
    with main.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0


def test_bulk_generate_reports_chart_errors_per_signal(monkeypatch):
    sig = scan()
    monkeypatch.setattr(main, "generate_signal_chart", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    r = client.post("/api/generate-bulk", json={"signal_ids": [sig["id"]]})
    assert r.status_code == 200 and r.json()["results"] == [] and "Chart image failed" in r.json()["errors"][0]


def test_publish_refuses_signal_post_without_chart():
    sig = scan()
    d = client.post("/api/generate", json={"signal_id": sig["id"]}).json()
    Path(d["chart_path"]).unlink()
    r = client.post(f"/api/posts/{d['id']}/publish")
    assert r.status_code == 409 and "missing its chart image" in r.json()["detail"]
    assert post_row(d["id"])["status"] == "DRAFT"


def test_publish_uses_official_image_script_and_keeps_key_out_of_args(monkeypatch, tmp_path):
    sig = scan()
    d = client.post("/api/generate", json={"signal_id": sig["id"]}).json()
    scripts = tmp_path / "skill" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("post-text.mjs", "post-image.mjs"):
        (scripts / name).write_text("//")
    monkeypatch.setattr(main, "find_square_post_script", lambda name="post-text.mjs": scripts / name)
    monkeypatch.setattr(main.shutil, "which", lambda x: "/usr/bin/node")
    main.set_setting("PUBLISH_MODE", "square_api")
    main.set_setting("BINANCE_SQUARE_OPENAPI_KEY", "SECRET-KEY-123")
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return Done()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    r = client.post(f"/api/posts/{d['id']}/publish")
    assert r.status_code == 200, r.text
    assert seen["cmd"][1].endswith("post-image.mjs") and "--images" in seen["cmd"] and d["chart_path"] in seen["cmd"]
    assert "SECRET-KEY-123" not in " ".join(seen["cmd"]) and seen["env"]["BINANCE_SQUARE_OPENAPI_KEY"] == "SECRET-KEY-123"
    assert post_row(d["id"])["status"] == "PUBLISHED"


def test_publish_failure_preserves_draft(monkeypatch, tmp_path):
    sig = scan()
    d = client.post("/api/generate", json={"signal_id": sig["id"]}).json()
    script = tmp_path / "scripts" / "post-image.mjs"
    script.parent.mkdir()
    script.write_text("//")
    monkeypatch.setattr(main, "find_square_post_script", lambda name="post-text.mjs": script)
    monkeypatch.setattr(main.shutil, "which", lambda x: "/usr/bin/node")
    main.set_setting("PUBLISH_MODE", "square_api")
    main.set_setting("BINANCE_SQUARE_OPENAPI_KEY", "k")

    class Fail:
        returncode, stdout, stderr = 1, "", "official script error 220095"

    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: Fail())
    r = client.post(f"/api/posts/{d['id']}/publish")
    assert r.status_code == 502 and "220095" in r.json()["detail"]
    assert post_row(d["id"])["status"] == "DRAFT"


def test_schedule_and_cancel_still_work():
    sig = scan()
    d = client.post("/api/generate", json={"signal_id": sig["id"]}).json()
    assert client.post("/api/schedule", json={"post_ids": [d["id"]], "spacing_minutes": 10}).json()["count"] == 1
    assert post_row(d["id"])["status"] == "SCHEDULED"
    assert client.post("/api/schedule/cancel", json={"post_ids": [d["id"]]}).json()["count"] == 1
    assert post_row(d["id"])["status"] == "DRAFT"


def test_regenerate_keeps_chart_and_surfaces_failure(monkeypatch):
    sig = scan()
    d = client.post("/api/generate", json={"signal_id": sig["id"]}).json()
    assert client.post(f"/api/posts/{d['id']}/regenerate").status_code == 200
    monkeypatch.setattr(main, "generate_signal_chart", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.post(f"/api/posts/{d['id']}/regenerate")
    assert r.status_code == 502 and post_row(d["id"])["chart_path"]


def test_top10_and_state_endpoints_still_work():
    scan()
    assert client.get("/api/state").json()["signals"]
    r = client.post("/api/generate-top10", json={})
    assert r.status_code == 200 and check_post_text(r.json()["content"]) == []
    assert client.post("/api/generate-top10", json={}).status_code == 200


def test_generate_btc_has_chart():
    main.remember_klines("BTCUSDT", "1h", rows())
    r = client.post("/api/generate-btc", json={"template_name": "btc_market_analysis"})
    assert r.status_code == 200, r.text
    assert Path(r.json()["chart_path"]).exists() and "$BTC" in r.json()["content"]


# ---- status refresh: ambiguity and monotonic progress -----------------------------------------------------

def candle_after_signal(sig_id, offset_hours, o, h, l, c):
    with main.conn() as cur:
        created = cur.execute("SELECT created_at FROM signals WHERE id=?", (sig_id,)).fetchone()["created_at"]
    t = int(datetime.fromisoformat(created).timestamp() * 1000) + offset_hours * HOUR
    return [t, o, h, l, c, 1.0, t + HOUR - 1, 1.0]


def test_refresh_status_ambiguous_when_one_candle_touches_stop_and_target():
    sig = scan()
    lo, hi, sl, tp1 = sig["entry_low"], sig["entry_high"], sig["stop_loss"], sig["tp1"]
    entry = (lo + hi) / 2
    long = sig["side"] == "LONG"
    candles_ = [candle_after_signal(sig["id"], 1, entry, entry + 0.01 if long else entry + 0.01, entry - 0.01, entry),
                candle_after_signal(sig["id"], 2, entry, max(tp1, sl), min(tp1, sl), entry)]
    r = client.post(f"/api/signals/{sig['id']}/refresh-status", json={"price": entry, "klines": candles_})
    assert r.status_code == 200
    assert r.json()["status"] == "AMBIGUOUS" and "cannot show which came first" in r.json()["note"]
    with main.conn() as c:
        ev = json.loads(c.execute("SELECT evidence FROM signals WHERE id=?", (sig["id"],)).fetchone()["evidence"])
    assert ev["last_check"]["price"] == entry and ev["candle_time"]  # earlier evidence is kept
    # a final status is never overwritten by a later price
    r2 = client.post(f"/api/signals/{sig['id']}/refresh-status", json={"price": tp1})
    assert r2.json()["status"] == "AMBIGUOUS"


def test_refresh_status_never_regresses_after_target():
    sig = scan()
    with main.conn() as c:
        c.execute("UPDATE signals SET status='TP1_HIT' WHERE id=?", (sig["id"],))
    mid = (sig["entry_low"] + sig["entry_high"]) / 2
    assert client.post(f"/api/signals/{sig['id']}/refresh-status", json={"price": mid}).json()["status"] == "TP1_HIT"


def test_update_post_uses_stored_price_check():
    sig = scan()
    with main.conn() as c:
        c.execute("UPDATE signals SET status='TP1_HIT' WHERE id=?", (sig["id"],))
    client.post(f"/api/signals/{sig['id']}/refresh-status", json={"price": sig["tp1"]})
    d = client.post("/api/generate", json={"signal_id": sig["id"], "template_name": "auto"}).json()
    assert d["template_name"] == "signal_update"
    assert "Last checked price" in d["content"] and f"signal #{sig['id']}" in d["content"]
    assert Path(d["chart_path"]).exists()


# ---- Binance access: rate limit, failures, caching ------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._p, self.headers = status, payload, headers or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


def fake_client(responses, calls):
    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            calls.append(url)
            return responses.pop(0)
    return FakeClient


def test_429_pauses_all_further_binance_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client([FakeResponse(429, headers={"retry-after": "30"})], calls))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        asyncio.run(main.binance_get("/fapi/v1/klines", {}, 5))
    with pytest.raises(RuntimeError, match="blocked this server's IP"):
        asyncio.run(main.binance_get("/fapi/v1/klines", {}, 5))
    assert len(calls) == 1  # the second call never touched the network


def test_server_errors_are_raised_not_swallowed(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client([FakeResponse(500)], []))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main.binance_get("/fapi/v1/klines", {}, 5))


def test_klines_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client([FakeResponse(200, rows(60))], calls))

    async def twice():
        return await main.fetch_klines("ZZZUSDT", "1h"), await main.fetch_klines("ZZZUSDT", "1h")

    first, second = asyncio.run(twice())
    assert first == second and len(calls) == 1


# ---- Signal Opportunities endpoints -----------------------------------------------------------------------------------

def opp_payload(symbols=("AAAUSDT",)):
    h = rows()
    return {
        "tickers": [{"symbol": s, "quoteVolume": "90000000", "priceChangePercent": "6"} for s in symbols],
        "klines": {s: {"1h": h, "15m": candles(130, step=900_000, t0=h[-1][0] - 129 * 900_000), "4h": candles(100, step=4 * HOUR)} for s in symbols},
        "funding": {s: {"lastFundingRate": "0.0001"} for s in symbols},
        "oi_hist": {}, "book": {}, "spot_symbols": list(symbols), "symbols": list(symbols),
    }


def test_candidates_endpoint_uses_config():
    r = client.post("/api/opportunities/candidates", json={"tickers": [
        {"symbol": "AAAUSDT", "quoteVolume": "90000000", "priceChangePercent": "6"},
        {"symbol": "THINUSDT", "quoteVolume": "10", "priceChangePercent": "50"}]})
    assert r.json()["symbols"] == ["AAAUSDT"]
    main.set_setting("OPPORTUNITY_CONFIG", json.dumps({"min_quote_volume": 1}))
    lowered = client.post("/api/opportunities/candidates", json={"tickers": [{"symbol": "THINUSDT", "quoteVolume": "10", "priceChangePercent": "50"}]})
    assert lowered.json()["symbols"] == ["THINUSDT"] and lowered.json()["config"]["min_quote_volume"] == 1


def run_opp_scan(symbols=("AAAUSDT",)):
    r = client.post("/api/opportunities/scan", json=opp_payload(symbols))
    assert r.status_code == 200, r.text
    return r.json()


def test_scan_persists_and_get_returns_ranked_results():
    out = run_opp_scan(("AAAUSDT", "BBBUSDT"))
    assert {r["symbol"] for r in out["results"]} == {"AAAUSDT", "BBBUSDT"}
    saved = client.get("/api/opportunities").json()
    assert saved["scanned_at"] == out["scanned_at"]
    order = {"publish_ready": 0, "watchlist": 1, "low": 2}
    keys = [(order[r["group"]], -r["opportunity_score"]) for r in saved["results"]]
    assert keys == sorted(keys)
    item = saved["results"][0]
    assert set(item["components"]) == {"technical", "attention", "event", "liquidity", "timeliness", "history"}
    assert "not a win probability" in saved["notes"][0]


def test_scan_with_no_data_is_empty_not_an_error():
    r = client.post("/api/opportunities/scan", json={"tickers": [], "klines": {}})
    assert r.status_code == 200 and r.json()["results"] == []


def test_generate_from_opportunity_makes_normal_draft_with_chart():
    run_opp_scan()
    r = client.post("/api/opportunities/generate", json={"symbol": "aaausdt"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert Path(d["chart_path"]).exists() and check_post_text(d["content"]) == []
    row = post_row(d["id"])
    assert row["status"] == "DRAFT" and row["signal_id"] == d["signal_id"]   # goes through the normal approval flow
    with main.conn() as c:
        ev = json.loads(c.execute("SELECT evidence FROM signals WHERE id=?", (d["signal_id"],)).fetchone()["evidence"])
    assert ev["event_key"].startswith("AAAUSDT:")


def test_same_market_event_is_not_posted_twice_unless_forced():
    run_opp_scan()
    assert client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT"}).status_code == 200
    dup = client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT"})
    assert dup.status_code == 409 and "same market event" in dup.json()["detail"]
    assert client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT", "force": True}).status_code == 200


def test_stale_scan_is_refused():
    out = run_opp_scan()
    out["scanned_at"] = (datetime.now(main.TZ) - timedelta(hours=5)).isoformat(timespec="seconds")
    main.set_setting(main.OPPORTUNITIES_KEY, json.dumps(out))
    r = client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT"})
    assert r.status_code == 409 and "stale" in r.json()["detail"]


def test_unknown_symbol_and_missing_candles_are_refused():
    run_opp_scan()
    assert client.post("/api/opportunities/generate", json={"symbol": "NOPEUSDT"}).status_code == 404
    main._klines_cache.clear()
    assert client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT"}).status_code == 409


def test_opportunity_chart_failure_is_surfaced(monkeypatch):
    run_opp_scan()
    monkeypatch.setattr(main, "generate_signal_chart", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chart")))
    r = client.post("/api/opportunities/generate", json={"symbol": "AAAUSDT"})
    assert r.status_code == 502 and "Chart image failed" in r.json()["detail"]


def test_history_from_past_signals_feeds_the_score():
    for status in ["TP1_HIT"] * 5 + ["SL_HIT"] * 2:
        s = scan("AAAUSDT")
        with main.conn() as c:
            c.execute("UPDATE signals SET status=? WHERE id=?", (status, s["id"]))
    assert main.signal_history()["AAAUSDT"] == {"tp": 5, "sl": 2}
    item = run_opp_scan()["results"][0]
    assert item["components"]["history"]["value"] == pytest.approx(6 / 9 * 100, abs=0.1)
