  // ─── Nova's avatar: a core and rings (8.79.0) ─────────────────────────
  // A bright core inside turning broken rings, the same mark as the icon,
  // drawn in plain canvas 2D in the theme's accent. It replaced the 3D
  // head. The big outer arcs are drawn by orbit.js on their own canvas; this
  // draws the core and the two rings round it, and sets how fast and how
  // bright all of them are for Nova's mood (_coreMood, which _arcsFrame
  // reads too). Moods:
  //   idle        slow turning, a calm glow that breathes
  //   listening   the rings draw in and brighten
  //   reasoning   rings spin fast, the core pulses ("Thinking…")
  //   speaking    ripples spread out from the core
  //   presenting  the rings brighten on the side of the pop-up card
  //   asleep      dim and slow, the core small
  // With less motion it is drawn once for each change of mood.
  static CORE_MOODS = {
    idle:       { speed: 1,    glow: 0.8,  pulse: 0.35, pulseHz: 0.2, size: 1,    squeeze: 1,    ripple: 0, lean: 0 },
    listening:  { speed: 0.7,  glow: 1,    pulse: 0.25, pulseHz: 0.6, size: 1.05, squeeze: 0.82, ripple: 0, lean: 0 },
    reasoning:  { speed: 3.2,  glow: 1,    pulse: 1,    pulseHz: 1.6, size: 1,    squeeze: 1,    ripple: 0, lean: 0 },
    speaking:   { speed: 1.4,  glow: 0.95, pulse: 0.6,  pulseHz: 2.4, size: 1.05, squeeze: 1,    ripple: 1, lean: 0 },
    presenting: { speed: 0.6,  glow: 0.9,  pulse: 0.3,  pulseHz: 0.3, size: 0.95, squeeze: 1,    ripple: 0, lean: 1 },
    asleep:     { speed: 0.25, glow: 0.35, pulse: 0.5,  pulseHz: 0.1, size: 0.78, squeeze: 1,    ripple: 0, lean: 0 },
  };

  // Where the avatar sits and how big it is. S is the core's scale: the
  // outer arcs in orbit.js are drawn at S × 1.55, 2.25 and 2.8.
  _coreLayout(h) {
    if (h.narrow) { const S = h.H * 0.3; return { S, hx: h.W / 2, hy: h.H * 0.5 }; }
    const S = h.H * 0.17;
    return { S, hx: h.W / 2, hy: h.H * 0.34 };   // in the middle of the orbit, clear of the greeting (8.76.0)
  }

  _initCore() {
    this._coreStop();
    this._reduceMotion = (typeof window.matchMedia === "function")
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const canvas = this.shadowRoot?.getElementById("core");
    if (!canvas) return;
    // canvas 2D can fail (some embedded webviews, jsdom in tests): the hero
    // then simply has no avatar, and the rest of the page still draws.
    let ctx = null;
    try { ctx = canvas.getContext("2d"); } catch (_) { ctx = null; }
    if (!ctx) return;
    const c = { canvas, ctx, last: 0, clock: 0, cur: Object.assign({}, this._coreMood()), stopped: false };
    this._core = c;
    const frame = now => {
      c.raf = null;
      if (c.stopped || !canvas.isConnected || this._currentTab !== "dashboard") { this._coreStop(); return; }
      if (!document.hidden) this._coreFrame(now);
      if (this._reduceMotion) return;
      c.raf = requestAnimationFrame(frame);
    };
    frame(performance.now());
  }

  _coreStop() {
    const c = this._core;
    if (!c) return;
    c.stopped = true;
    if (c.raf) cancelAnimationFrame(c.raf);
    this._core = null;
  }

  // The look for the current mood (an unknown mood is idle).
  _coreMood(state = this._coreStateKey) {
    return NovaPanel.CORE_MOODS[state] || NovaPanel.CORE_MOODS.idle;
  }

  _targetCoreState(state) {
    this._coreStateKey = NovaPanel.CORE_MOODS[state] ? state : "idle";
    // With less motion nothing animates, so draw the new mood at once.
    if (this._reduceMotion && this._core) this._coreFrame(performance.now());
    if (this._reduceMotion && this._arcsCtx) this._arcsStop(), this._arcsStart();
  }

  _coreFrame(now) {
    const c = this._core;
    if (!c) return;
    const { canvas, ctx } = c;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (!W || !H) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // Ease every value towards the mood, so changes glide.
    const dt = c.last ? Math.min(0.1, (now - c.last) / 1000) : 0;
    c.last = now;
    const target = this._coreMood();
    const k = this._reduceMotion ? 1 : 1 - Math.exp(-dt * 2.5);
    for (const key of Object.keys(target)) c.cur[key] += (target[key] - c.cur[key]) * k;
    const m = c.cur;
    c.clock += dt * m.speed;
    const t = this._reduceMotion ? 0 : c.clock, wall = this._reduceMotion ? 0 : now / 1000;

    const { S, hx, hy } = this._coreLayout({ W, H, narrow: W < 820 });
    const head = this._theme().head;
    const rgb = (v, a) => `rgba(${v.map(x => Math.round(x * 255)).join(",")},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
    const main = head.main, pale = head.pale, deep = head.deep;
    const beat = 1 + m.pulse * 0.08 * Math.sin(wall * Math.PI * 2 * m.pulseHz);
    const R = S * 0.5 * m.size * beat;

    // A soft glow behind everything.
    const halo = ctx.createRadialGradient(hx, hy, 0, hx, hy, S * 1.4);
    halo.addColorStop(0, rgb(main, 0.28 * m.glow)); halo.addColorStop(1, rgb(main, 0));
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(hx, hy, S * 1.4, 0, Math.PI * 2); ctx.fill();

    // Speaking: ripples spread out from the core and fade.
    if (m.ripple > 0.02) {
      for (let i = 0; i < 3; i++) {
        const p = ((wall * 0.9) + i / 3) % 1;
        ctx.strokeStyle = rgb(main, (1 - p) * 0.5 * m.ripple); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(hx, hy, R * (1.05 + p * 1.6), 0, Math.PI * 2); ctx.stroke();
      }
    }

    // Two rings round the core: one broken, one dotted, turning opposite ways.
    // Presenting brightens the side the card is on (the right).
    ctx.lineCap = "round";
    const rings = [
      [0.92, 1, [[0, 1.3], [1.9, 2.5], [3.2, 4.9], [5.4, 5.9]], 3, 0.85],
      [0.72, -1.6, null, 2, 0.6],
    ];
    for (const [rk, dir, pieces, w, alpha] of rings) {
      const r = S * rk * m.squeeze, rot = t * 0.5 * dir;
      if (pieces) {
        for (const [a0, a1] of pieces) {
          const mid = (a0 + a1) / 2 + rot;
          const face = 0.5 + 0.5 * Math.cos(mid);          // 1 on the right side
          const lit = alpha * m.glow * (1 + m.lean * (face - 0.5) * 1.1);
          ctx.strokeStyle = rgb(main, lit); ctx.lineWidth = w;
          ctx.beginPath(); ctx.arc(hx, hy, r, a0 + rot, a1 + rot); ctx.stroke();
        }
      } else {
        ctx.setLineDash([2, 7]); ctx.strokeStyle = rgb(pale, alpha * m.glow); ctx.lineWidth = w;
        ctx.beginPath(); ctx.arc(hx, hy, r, rot, rot + Math.PI * 2); ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // The core: a glossy ball, lit from the top left.
    const core = ctx.createRadialGradient(hx - R * 0.3, hy - R * 0.35, R * 0.05, hx, hy, R);
    core.addColorStop(0, rgb(pale, 0.95 * (0.5 + 0.5 * m.glow)));
    core.addColorStop(0.45, rgb(main, 0.95 * (0.45 + 0.55 * m.glow)));
    core.addColorStop(1, rgb(deep, 0.9));
    ctx.fillStyle = core; ctx.beginPath(); ctx.arc(hx, hy, R, 0, Math.PI * 2); ctx.fill();
    const rim = ctx.createRadialGradient(hx, hy, R * 0.9, hx, hy, R * 1.5);
    rim.addColorStop(0, rgb(main, 0.45 * m.glow)); rim.addColorStop(1, rgb(main, 0));
    ctx.fillStyle = rim; ctx.beginPath(); ctx.arc(hx, hy, R * 1.5, 0, Math.PI * 2); ctx.fill();
  }

  // ── state word: types itself out when the state changes ──

  _typeHeroWord(word) {
    const el = this.shadowRoot.getElementById("heroWordText");
    if (!el) return;
    word = this._tx(word);   // translate the whole word once, then type it
    this._heroWordTarget = word;
    if (this._reduceMotion) { this._setText(el, word); return; }
    if (this._heroTypeTimer) return;
    const tick = () => {
      const node = this.shadowRoot.getElementById("heroWordText");
      const target = this._heroWordTarget;
      if (!node || node.textContent === target) { this._heroTypeTimer = null; return; }
      const cur = node.textContent;
      node.textContent = target.startsWith(cur) ? target.slice(0, cur.length + 1) : cur.slice(0, -1);   // i18n-ok: types out a word already translated above
      this._heroTypeTimer = setTimeout(tick, target.startsWith(node.textContent) ? 85 : 35);
    };
    tick();
  }
