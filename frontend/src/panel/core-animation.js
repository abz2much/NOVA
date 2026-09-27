  // ─── Ember head: the hero's 3D face ─────────────────────────────────────
  // A head built from gold sparks, projected in plain canvas 2D (no library,
  // same idea as NOVA3D). It sits on the left of a full-width stage, turns
  // and looks around, glances at the state word when it changes, and its
  // eyes, brows and mouth follow the core state.

  _initCore() {
    const canvas = this.shadowRoot.getElementById("core");
    if (!canvas) return;
    this._canvas = canvas;
    this._reduceMotion = (typeof window.matchMedia === "function")
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Defensive, not just a test accommodation: canvas 2D context creation
    // can fail (exotic embedded webviews, jsdom in tests) and matchMedia
    // isn't universally present — both degrade to a static hero instead of
    // throwing and blanking the whole dashboard.
    try { this._ctx = canvas.getContext("2d"); } catch (_) { this._ctx = null; }
    if (!this._ctx) return;
    this._makeHead();
    this._resizeCore();
    if (!this._resizeListener) {
      this._resizeListener = () => { this._resizeCore(); if (this._reduceMotion) this._coreFrame(performance.now()); };
      window.addEventListener("resize", this._resizeListener);
    }
    canvas.addEventListener("pointerdown", (e) => {
      this._face.dragging = true; this._face.dragX = e.clientX;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* not all browsers */ }
    });
    canvas.addEventListener("pointermove", (e) => {
      const f = this._face;
      if (!f.dragging) return;
      f.dragYaw = Math.max(-1.6, Math.min(1.6, f.dragYaw + (e.clientX - f.dragX) * 0.012));
      f.dragX = e.clientX;
      if (this._reduceMotion) this._coreFrame(performance.now());
    });
    const endDrag = () => { this._face.dragging = false; };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    this._t0 = performance.now();
    const loop = (now) => {
      this._coreFrame(now);
      if (!this._reduceMotion) this._animHandle = requestAnimationFrame(loop);
    };
    this._animHandle = requestAnimationFrame(loop);
  }

  _resizeCore() {
    if (!this._canvas || !this._ctx) return;
    const rect = this._canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this._canvas.width = rect.width * dpr;
    this._canvas.height = rect.height * dpr;
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._coreW = rect.width; this._coreH = rect.height;
  }

  _makeHead() {
    const rnd = Math.random;
    this._particles = [];                       // sparks on the head's surface, biased toward the face
    for (let i = 0; i < 3000; i++) {
      let th = (rnd() * 2 - 1) * Math.PI; if (rnd() < 0.6) th *= 0.45;
      this._particles.push({ th, ph: Math.asin(rnd() * 2 - 1) * 0.985, tw: rnd() * 6.28, s: 0.6 + rnd() * 0.9 });
    }
    this._orbit = [];                           // cloud orbiting the head
    for (let i = 0; i < 110; i++) {
      this._orbit.push({ a: rnd() * 6.28, r: 1.15 + rnd() * 0.75, y: (rnd() * 2 - 1) * 0.9, sp: 0.6 + rnd() * 0.8, s: 0.8 + rnd() * 1.4, ph: rnd() * 6.28 });
    }
    this._dust = [];                            // sparks drifting across the whole stage
    for (let i = 0; i < 110; i++) {
      this._dust.push({ x: rnd(), y: rnd(), s: 0.5 + rnd() * 1.6, v: 0.3 + rnd(), ph: rnd() * 6.28 });
    }
    this._face = {
      t: 0, blink: 1, blinkT: -1, nextBlink: 2, gx: 0, gy: 0, gazeT: 0, gTarget: [0, 0], talk: 0,
      yaw: 0, pitch: 0, roll: 0, dragYaw: 0, dragging: false, dragX: 0, lookText: 0,
    };
  }

  _targetCoreState(state) {
    const STATES = {
      idle: { open: 1, wide: 0, brow: 0, smile: 0.45, mouth: 0, speed: 0.20, glow: 0.60, hot: 0.35, count: 60, sleep: 0 },
      reasoning: { open: 1, wide: 1, brow: 0.8, smile: 0, mouth: 0.35, speed: 0.62, glow: 1.0, hot: 0.85, count: 110, sleep: 0 },
      asleep: { open: 0, wide: 0, brow: 0, smile: 0.2, mouth: 0, speed: 0.07, glow: 0.30, hot: 0.10, count: 30, sleep: 1 },
    };
    const next = STATES[state] ? state : "idle";
    if (this._coreStateKey !== next && this._face) this._face.lookText = 1.6;
    this._coreStateKey = next;
    this._targetState = STATES[next];
    if (this._reduceMotion && this._ctx) {
      Object.assign(this._current, this._targetState);
      this._coreFrame(performance.now());
    }
  }

  _lerp(a, b, t) { return a + (b - a) * t; }

  _colorForHeat(t) {
    const stops = [[126, 36, 18], [226, 84, 47], [244, 184, 96], [255, 231, 189]];
    t = Math.max(0, Math.min(1, t));
    const seg = t * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(seg));
    const f = seg - i;
    const c0 = stops[i], c1 = stops[i + 1];
    return [Math.round(this._lerp(c0[0], c1[0], f)), Math.round(this._lerp(c0[1], c1[1], f)), Math.round(this._lerp(c0[2], c1[2], f))];
  }

  // ── head geometry: ~2 units tall, the face looks down +z, y is up ──

  _lipY(x, E) { return -0.46 + E.smile * 0.07 * Math.min(1, (x / 0.17) * (x / 0.17)); }

  _headDisp(x, y, E) {
    const g = (v, m, s) => Math.exp(-((v - m) * (v - m)) / (2 * s * s));
    const ax = Math.abs(x), mo = E.mo, ly = this._lipY(x, E);
    const browY = 0.27 + E.wide * 0.05 + Math.max(0, E.brow) * 0.03;
    return 0.2 * g(x, 0, 0.05) * g(y, -0.1, 0.13) + 0.06 * g(x, 0, 0.07) * g(y, -0.23, 0.055)   // nose
      - 0.1 * g(ax, 0.29, 0.1) * g(y, 0.11, 0.075)                                            // eye sockets
      + 0.06 * g(y, browY, 0.045) * g(ax, 0.3, 0.17)                                          // brows
      + 0.05 * g(ax, 0.37, 0.12) * g(y, -0.14, 0.1)                                           // cheeks
      + 0.055 * g(y, ly + 0.035, 0.03) * g(x, 0, 0.13)                                        // upper lip
      + 0.06 * g(y, ly - 0.045 - mo * 0.08, 0.035) * g(x, 0, 0.12)                            // lower lip
      - 0.1 * mo * g(y, ly - mo * 0.04, 0.012 + mo * 0.03) * g(x, 0, 0.11)                    // open mouth
      + 0.05 * g(y, -0.8, 0.1) * g(x, 0, 0.18);                                               // chin
  }

  _headPoint(th, ph, E) {
    const cp = Math.cos(ph), dx = Math.sin(th) * cp, dy = Math.sin(ph), dz = Math.cos(th) * cp;
    let x = dx * 0.74, y = dy * 0.98, z = dz * 0.84;
    if (y < 0) { const k = Math.pow(-y / 0.98, 1.6); x *= 1 - 0.3 * k; z *= 1 - 0.12 * k; }
    if (z < 0) z *= 1.08;
    if (y > 0) x *= 1 + 0.07 * (y / 0.98);
    if (dz > 0) z += Math.pow(dz, 1.2) * this._headDisp(x, y, E);
    return [x, y, z];
  }

  _faceZ(x, y, E) {
    const k = y < 0 ? Math.pow(-y / 0.98, 1.6) : 0, x0 = x / (1 - 0.3 * k);
    const z0 = 0.84 * Math.sqrt(Math.max(0, 1 - (x0 / 0.74) ** 2 - (y / 0.98) ** 2)) * (1 - 0.12 * k);
    return z0 + Math.pow(z0 / 0.84, 1.2) * this._headDisp(x, y, E);
  }

  // ── motion: blinks, gaze, head pose ──

  _updateFace(dt, narrow) {
    const f = this._face, c = this._current, key = this._coreStateKey || "idle";
    f.t += dt;
    if (f.blinkT < 0 && f.t > f.nextBlink) f.blinkT = 0;
    if (f.blinkT >= 0) {
      f.blinkT += dt;
      const p = f.blinkT / 0.17;
      f.blink = p < 1 ? Math.abs(1 - 2 * p) : 1;
      if (p >= 1) { f.blinkT = -1; f.nextBlink = f.t + 2.5 + Math.random() * 3.5; }
    }
    f.lookText = Math.max(0, f.lookText - dt);
    let tx, ty;
    if (f.lookText > 0 && key !== "asleep" && !narrow) { tx = 0.95; ty = 0.05; }   // glance at the word
    else if (key === "reasoning") { tx = Math.sin(f.t * 0.6) * 0.2; ty = 0.8; }      // look down at the feed
    else if (key === "asleep") { tx = 0; ty = 0; }
    else {
      if (f.t > f.gazeT) { f.gTarget = [(Math.random() * 2 - 1) * 0.8, (Math.random() * 2 - 1) * 0.35]; f.gazeT = f.t + 1.8 + Math.random() * 2.5; }
      [tx, ty] = f.gTarget;
    }
    const k = Math.min(1, dt * 3);
    f.gx += (tx - f.gx) * k; f.gy += (ty - f.gy) * k;
    if (!f.dragging) f.dragYaw *= Math.pow(0.08, dt);
    f.yaw = f.gx * 0.42 + Math.sin(f.t * 0.35) * 0.07 * (1 - c.sleep) + f.dragYaw;
    f.pitch = f.gy * 0.28 + c.sleep * 0.3 + Math.sin(f.t * 0.9) * 0.015 * c.sleep;
    f.roll = c.sleep * 0.14 + Math.sin(f.t * 0.27) * 0.02;
  }

  _coreFrame(now) {
    if (!this._ctx || !this._targetState || !this._face) return;
    const reduce = this._reduceMotion;
    const dt = reduce ? 0 : Math.min(0.05, (now - (this._t0 || now)) / 1000);
    this._t0 = now;
    const c = this._current, t = this._targetState, k = reduce ? 1 : Math.min(1, dt * 2.4);
    for (const key of Object.keys(t)) c[key] = this._lerp(c[key] ?? t[key], t[key], k);

    const ctx = this._ctx, W = this._coreW, H = this._coreH;
    if (!W || !H) { this._resizeCore(); return; }
    const narrow = W < 640;
    const f = this._face;
    if (reduce) {
      f.blink = 1; f.gx = 0; f.gy = this._coreStateKey === "reasoning" ? 0.8 : 0;
      f.yaw = f.dragYaw; f.pitch = f.gy * 0.28 + c.sleep * 0.3; f.roll = c.sleep * 0.14;
    } else this._updateFace(dt, narrow);
    const secs = reduce ? 0 : now / 1000;
    const rgba = (col, a) => `rgba(${col[0]},${col[1]},${col[2]},${a})`;
    ctx.clearRect(0, 0, W, H);

    // stage: wide glow behind the head, sparks drifting across the panel
    const size = narrow ? Math.min(W, H) : H * 1.02;
    const hx = narrow ? W / 2 : Math.max(size * 0.5, W * 0.26), hy = H / 2;
    const hc = this._colorForHeat(Math.min(1, c.hot * 0.8 + 0.2));
    const wide = ctx.createRadialGradient(hx, hy, 0, hx, hy, Math.max(W, H) * 0.75);
    wide.addColorStop(0, rgba(hc, 0.16 * c.glow)); wide.addColorStop(1, "rgba(20,12,8,0)");
    ctx.fillStyle = wide; ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    const dustN = Math.round(this._dust.length * Math.max(0.3, Math.min(1, c.count / 110)));
    const dustC = this._colorForHeat(0.55 + c.hot * 0.3);
    for (let i = 0; i < dustN; i++) {
      const d = this._dust[i];
      d.x += dt * c.speed * d.v * 0.05; if (d.x > 1.02) d.x -= 1.04;
      ctx.fillStyle = rgba(dustC, (0.1 + 0.35 * c.glow) * (0.6 + 0.4 * Math.sin(secs * 1.7 + d.ph)));
      ctx.beginPath(); ctx.arc(d.x * W, d.y * H + Math.sin(secs * 0.6 + d.ph) * 8, d.s, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";

    // camera
    const S = size * 0.32, D = 4.2, ox = hx, oy = hy + S * 0.06;
    const cyw = Math.cos(f.yaw), syw = Math.sin(f.yaw), cpt = Math.cos(f.pitch), spt = Math.sin(f.pitch), crl = Math.cos(f.roll), srl = Math.sin(f.roll);
    const rot = (p) => {
      const x = p[0] * cyw + p[2] * syw, z = -p[0] * syw + p[2] * cyw;
      const y2 = p[1] * cpt - z * spt, z2 = p[1] * spt + z * cpt;
      return [x * crl - y2 * srl, x * srl + y2 * crl, z2];
    };
    const proj = (r) => { const s = D / (D - r[2]); return [ox + r[0] * s * S, oy - r[1] * s * S, s]; };
    const E = { open: Math.max(0, c.open * f.blink), wide: c.wide, brow: c.brow, smile: c.smile, mo: Math.min(1, c.mouth) };

    const halo = ctx.createRadialGradient(hx, hy, 0, hx, hy, size * 0.52);
    halo.addColorStop(0, rgba(hc, 0.5 * c.glow)); halo.addColorStop(1, "rgba(20,12,8,0)");
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(hx, hy, size * 0.52, 0, Math.PI * 2); ctx.fill();

    ctx.globalCompositeOperation = "lighter";
    const orbitN = Math.min(this._orbit.length, Math.round(c.count * 0.8));
    const orbitC = this._colorForHeat(c.hot * 0.7 + 0.25);
    const drawOrbit = (front) => {
      for (let i = 0; i < orbitN; i++) {
        const p = this._orbit[i];
        if (!front) p.a += c.speed * p.sp * dt * 1.4;
        const r = p.r + Math.sin(secs * 1.3 + p.ph) * 0.05, z = Math.sin(p.a) * r;
        if ((z > 0) !== front) continue;
        const q = proj([Math.cos(p.a) * r, p.y * 0.8, z]), dep = (z + 2) / 4;
        ctx.fillStyle = rgba(orbitC, (0.25 + 0.55 * c.glow) * (0.4 + 0.6 * dep) * (front ? 0.9 : 0.8));
        ctx.beginPath(); ctx.arc(q[0], q[1], p.s * (0.6 + 0.6 * dep), 0, Math.PI * 2); ctx.fill();
      }
    };
    drawOrbit(false);

    // surface sparks, lit from the upper left
    // Surface points and normals only depend on the expression (not on the
    // pose or blinks), so they are rebuilt only when the expression moves.
    const geoKey = [E.wide, E.brow, E.smile, E.mo].map(v => v.toFixed(3)).join("|");
    if (geoKey !== this._headGeoKey) {
      this._headGeoKey = geoKey;
      const e = 0.004;
      for (const p of this._particles) {
        const a = this._headPoint(p.th, p.ph, E), b = this._headPoint(p.th + e, p.ph, E), cc = this._headPoint(p.th, p.ph + e, E);
        const u0 = b[0] - a[0], u1 = b[1] - a[1], u2 = b[2] - a[2], v0 = cc[0] - a[0], v1 = cc[1] - a[1], v2 = cc[2] - a[2];
        const n0 = u1 * v2 - u2 * v1, n1 = u2 * v0 - u0 * v2, n2 = u0 * v1 - u1 * v0, nl = Math.hypot(n0, n1, n2) || 1;
        p.lp = a; p.ln = [n0 / nl, n1 / nl, n2 / nl];
      }
    }
    const L = [-0.7385, 0.4431, 0.4924];
    for (const p of this._particles) {
      const ra = rot(p.lp), rn = rot(p.ln), sg = rn[2] < 0 ? -1 : 1;
      const n0 = rn[0] * sg, n1 = rn[1] * sg, n2 = rn[2] * sg;
      const q = proj(ra);
      const facing = Math.max(0, Math.min(1, (ra[2] + 0.9) / 1.8));
      const diff = Math.max(0, n0 * L[0] + n1 * L[1] + n2 * L[2]);
      const tw = 0.75 + 0.25 * Math.sin(secs * (2 + c.speed * 4) + p.tw);
      const al = Math.min(1, (0.02 + 0.95 * facing * facing * diff * diff) * (0.5 + 0.6 * c.glow) * tw);
      if (al < 0.01) continue;
      const sz = p.s * 1.15 * (0.6 + 0.6 * facing) * Math.max(0.7, size / 420);
      ctx.fillStyle = rgba(this._colorForHeat(0.35 + diff * 0.55 + c.hot * 0.2), al.toFixed(3));
      ctx.fillRect(q[0] - sz / 2, q[1] - sz / 2, sz, sz);
    }

    // mouth: a glowing seam between the lips
    const mouthC = this._colorForHeat(0.75 + c.hot * 0.25);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.strokeStyle = rgba(mouthC, Math.min(0.9, 0.25 + E.mo * 1.2));
    ctx.lineWidth = Math.max(1, S * (0.008 + E.mo * 0.05));
    ctx.beginPath();
    for (let i = 0; i <= 12; i++) {
      const x = -0.15 + 0.3 * i / 12, y = this._lipY(x, E) - E.mo * 0.04;
      const q = proj(rot([x, y, this._faceZ(x, y, E) + 0.02]));
      if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]);
    }
    ctx.stroke();
    ctx.globalCompositeOperation = "source-over";

    // eyes: glowing, squash shut to a soft arc, lids open wide on events
    const eyeC = this._colorForHeat(0.8 + c.hot * 0.2);
    for (const side of [-1, 1]) {
      const ex = side * 0.29, ey = 0.115;
      const q = proj(rot([ex, ey, this._faceZ(ex, ey, E) + 0.05]));
      const rx = 0.105 * S * q[2] * (1 + E.wide * 0.2) * 0.8, ry = rx * (0.07 + 0.93 * E.open) * (1 + E.wide * 0.25);
      ctx.save(); ctx.translate(q[0], q[1]); ctx.rotate(f.roll);
      const glow = ctx.createRadialGradient(0, 0, 0, 0, 0, rx * 2.4);
      glow.addColorStop(0, rgba(eyeC, 0.35 * c.glow * (0.3 + 0.7 * E.open))); glow.addColorStop(1, rgba(eyeC, 0));
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(0, 0, rx * 2.4, 0, Math.PI * 2); ctx.fill();
      if (E.open > 0.12) {
        ctx.scale(1, ry / rx);
        const core = ctx.createRadialGradient(f.gx * rx * 0.2, f.gy * rx * 0.25, 0, 0, 0, rx);
        core.addColorStop(0, `rgba(255,244,222,${0.95 * c.glow + 0.05})`);
        core.addColorStop(0.35, rgba(eyeC, 0.95)); core.addColorStop(1, rgba(eyeC, 0.15));
        ctx.fillStyle = core; ctx.beginPath(); ctx.arc(0, 0, rx, 0, Math.PI * 2); ctx.fill();
      } else {
        ctx.strokeStyle = rgba(eyeC, 0.55 + 0.35 * c.glow); ctx.lineWidth = Math.max(1.2, rx * 0.18); ctx.lineCap = "round";
        ctx.beginPath(); ctx.moveTo(-rx * 0.9, 0); ctx.quadraticCurveTo(0, rx * 0.55, rx * 0.9, 0); ctx.stroke();
      }
      ctx.restore();
    }

    ctx.globalCompositeOperation = "lighter";
    drawOrbit(true);
    ctx.globalCompositeOperation = "source-over";
  }

  // ── state word: types itself out when the state changes ──

  _typeHeroWord(word) {
    const el = this.shadowRoot.getElementById("heroWordText");
    if (!el) return;
    this._heroWordTarget = word;
    if (this._reduceMotion) { el.textContent = word; return; }
    if (this._heroTypeTimer) return;
    const tick = () => {
      const node = this.shadowRoot.getElementById("heroWordText");
      const target = this._heroWordTarget;
      if (!node || node.textContent === target) { this._heroTypeTimer = null; return; }
      const cur = node.textContent;
      node.textContent = target.startsWith(cur) ? target.slice(0, cur.length + 1) : cur.slice(0, -1);
      this._heroTypeTimer = setTimeout(tick, target.startsWith(node.textContent) ? 85 : 35);
    };
    tick();
  }
