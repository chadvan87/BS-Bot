// Sliding-window request-weight limiter for Binance public endpoints.
// Keeps this browser's IP under a soft cap so a big scan can never trigger HTTP 429 / 418.
class WeightLimiter {
  constructor(cap, windowMs = 60000, now = () => Date.now(), sleep = ms => new Promise(r => setTimeout(r, ms))) {
    this.cap = cap; this.windowMs = windowMs; this.now = now; this.sleep = sleep
    this.log = []            // [timestamp, weight]
    this.blockedUntil = 0
    this.total = 0           // weight spent since construction, for the UI
  }
  used() { const t = this.now(); this.log = this.log.filter(([ts]) => t - ts < this.windowMs); return this.log.reduce((s, [, w]) => s + w, 0) }
  // Server told us to back off (Retry-After): nothing is sent until then.
  blockFor(ms) { this.blockedUntil = Math.max(this.blockedUntil, this.now() + ms) }
  async acquire(weight = 1) {
    for (;;) {
      const t = this.now()
      if (t < this.blockedUntil) { await this.sleep(this.blockedUntil - t); continue }
      const used = this.used()
      if (used + weight <= this.cap || this.log.length === 0) { this.log.push([t, weight]); this.total += weight; return }
      const wait = this.log[0][0] + this.windowMs - t + 25     // until the oldest entry leaves the window
      await this.sleep(Math.max(wait, 25))
    }
  }
}
if (typeof module !== 'undefined') module.exports = { WeightLimiter }
else window.WeightLimiter = WeightLimiter
