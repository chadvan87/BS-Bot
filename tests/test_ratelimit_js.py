from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "ratelimit.js"

SCRIPT = r"""
const { WeightLimiter } = require(process.argv[1])
const assert = require('assert')
;(async () => {
  let clock = 0, slept = 0
  const now = () => clock
  const sleep = async ms => { slept += ms; clock += ms }
  // 1) never exceeds the cap inside any sliding window, even for 10x the cap in requests
  let lim = new WeightLimiter(100, 60000, now, sleep), stamps = []
  for (let i = 0; i < 1000; i++) { await lim.acquire(1); stamps.push(clock) }
  for (let i = 0; i < stamps.length; i++) {
    const inWin = stamps.filter(t => t <= stamps[i] && stamps[i] - t < 60000).length
    assert(inWin <= 100, 'window exceeded: ' + inWin)
  }
  assert(slept > 0 && lim.total === 1000)
  // 2) under the cap there is no waiting
  clock = 0; slept = 0; lim = new WeightLimiter(100, 60000, now, sleep)
  for (let i = 0; i < 100; i++) await lim.acquire(1)
  assert.strictEqual(slept, 0); assert.strictEqual(lim.used(), 100)
  // 3) weights are honoured (weight 2 calls)
  clock = 0; lim = new WeightLimiter(10, 60000, now, sleep)
  for (let i = 0; i < 5; i++) await lim.acquire(2)
  assert.strictEqual(lim.used(), 10)
  const before = clock; await lim.acquire(2); assert(clock > before)
  // 4) Retry-After block holds everything back
  clock = 0; lim = new WeightLimiter(100, 60000, now, sleep); lim.blockFor(30000)
  await lim.acquire(1); assert(clock >= 30000)
  // 5) a single request heavier than the cap still goes through (no deadlock)
  clock = 0; lim = new WeightLimiter(5, 60000, now, sleep); await lim.acquire(40)
  // 6) window expires
  clock = 0; lim = new WeightLimiter(10, 60000, now, sleep)
  for (let i = 0; i < 10; i++) await lim.acquire(1)
  clock = 61000; assert.strictEqual(lim.used(), 0)
  console.log('ok')
})().catch(e => { console.error(e); process.exit(1) })
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_weight_limiter_never_exceeds_cap():
    r = subprocess.run(["node", "-e", SCRIPT, str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
