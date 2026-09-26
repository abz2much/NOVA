  // ─── Stellar core animation (from the approved mockup) ──────────────────

  _initCore() {
    const canvas = this.shadowRoot.getElementById("core");
    if (!canvas) return;
    this._canvas = canvas;
    // Defensive, not just a test accommodation: canvas 2D context creation
    // can fail (exotic embedded webviews, jsdom in tests) and matchMedia
    // isn't universally present — both degrade to a static hero instead of
    // throwing and blanking the whole dashboard.
    try { this._ctx = canvas.getContext("2d"); } catch (_) { this._ctx = null; }
    if (!this._ctx) return;
    this._reduceMotion = (typeof window.matchMedia === "function")
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this._makeParticles(this._current.count);
    this._resizeCore();
    if (!this._resizeListener) {
      this._resizeListener = () => this._resizeCore();
      window.addEventListener("resize", this._resizeListener);
    }
    this._t0 = performance.now();
    this._flareT = 0;
    const loop = (now) => {
      this._coreFrame(now);
      if (!this._reduceMotion) this._animHandle = requestAnimationFrame(loop);
    };
    this._animHandle = requestAnimationFrame(loop);
  }

  _resizeCore() {
    if (!this._canvas) return;
    const rect = this._canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this._canvas.width = rect.width * dpr;
    this._canvas.height = rect.height * dpr;
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._coreW = rect.width; this._coreH = rect.height;
  }

  _makeParticles(n) {
    const particles = [];
    for (let i = 0; i < n; i++) {
      particles.push({
        a: Math.random() * Math.PI * 2, r: 0.30 + Math.random() * 0.62,
        speedMul: 0.6 + Math.random() * 0.8, size: 0.9 + Math.random() * 1.8,
        phase: Math.random() * Math.PI * 2,
      });
    }
    this._particles = particles;
  }

  _targetCoreState(state) {
    const STATES = {
      idle: { speed: 0.20, count: 70, radiusMul: 1.00, glow: 0.55, hot: 0.35, flare: 0.05 },
      reasoning: { speed: 0.62, count: 110, radiusMul: 1.12, glow: 0.95, hot: 0.85, flare: 0.55 },
      asleep: { speed: 0.07, count: 40, radiusMul: 0.78, glow: 0.30, hot: 0.10, flare: 0.0 },
    };
    this._targetState = STATES[state] || STATES.idle;
    if (this._particles.length !== this._targetState.count) this._makeParticles(this._targetState.count);
  }

  _lerp(a, b, t) { return a + (b - a) * t; }

  _colorForHeat(t) {
    const stops = [[126, 36, 18], [226, 84, 47], [244, 184, 96], [255, 231, 189]];
    const seg = t * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(seg));
    const f = seg - i;
    const c0 = stops[i], c1 = stops[i + 1];
    return [Math.round(this._lerp(c0[0], c1[0], f)), Math.round(this._lerp(c0[1], c1[1], f)), Math.round(this._lerp(c0[2], c1[2], f))];
  }

  _coreFrame(now) {
    if (!this._ctx || !this._targetState) return;
    const dt = Math.min(0.05, (now - (this._t0 || now)) / 1000);
    this._t0 = now;
    const c = this._current, t = this._targetState;
    c.speed = this._lerp(c.speed, t.speed, dt * 1.4);
    c.radiusMul = this._lerp(c.radiusMul, t.radiusMul, dt * 1.4);
    c.glow = this._lerp(c.glow, t.glow, dt * 1.4);
    c.hot = this._lerp(c.hot, t.hot, dt * 1.4);
    c.flare = this._lerp(c.flare, t.flare, dt * 1.4);

    const ctx = this._ctx, W = this._coreW, H = this._coreH;
    if (!W || !H) { this._resizeCore(); return; }
    ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2;
    const baseR = Math.min(W, H) * 0.30 * c.radiusMul;

    this._flareT += dt;
    const flareBoost = c.flare > 0.01 ? (0.5 + 0.5 * Math.sin(this._flareT * 3.1)) * c.flare : 0;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseR * 1.9);
    const hc = this._colorForHeat(Math.min(1, c.hot + flareBoost * 0.4));
    grad.addColorStop(0, `rgba(${hc[0]},${hc[1]},${hc[2]},${0.85 * c.glow + 0.15})`);
    grad.addColorStop(0.35, `rgba(${hc[0]},${hc[1]},${hc[2]},${0.35 * c.glow})`);
    grad.addColorStop(1, "rgba(20,12,8,0)");
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(cx, cy, baseR * 1.9, 0, Math.PI * 2); ctx.fill();

    ctx.globalCompositeOperation = "lighter";
    for (const p of this._particles) {
      p.a += c.speed * p.speedMul * dt;
      const wobble = Math.sin(now / 1000 * 1.3 + p.phase) * 0.06;
      const rr = (p.r + wobble) * baseR * 1.55;
      const x = cx + Math.cos(p.a) * rr, y = cy + Math.sin(p.a) * rr * 0.86;
      const heat = Math.min(1, c.hot * 0.7 + p.r * 0.5 + flareBoost * 0.5);
      const pc = this._colorForHeat(heat);
      const alpha = 0.35 + 0.5 * c.glow;
      ctx.beginPath();
      ctx.fillStyle = `rgba(${pc[0]},${pc[1]},${pc[2]},${alpha})`;
      ctx.arc(x, y, p.size * (0.8 + 0.5 * c.glow), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";
  }

