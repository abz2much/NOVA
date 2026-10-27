  // ─── Command Center orbit (8.76.0) ──────────────────────────────────────
  // The head sits in the middle of the hero with broken arcs turning behind
  // it, and five live cards round it. The cards use the same readings as the
  // pop-up cards (presenting.js), so they add no work of their own except
  // Energy, which is read at most once a minute. Tapping a card pops it up
  // beside the head; when a pop-up card ends it settles into its card.
  static ORBIT_KINDS = ["security", "home", "energy", "climate", "weather"];

  _orbitHtml() {
    const caps = { security: "Security", home: "Who's home", energy: "Energy", climate: "Indoor", weather: "Weather" };
    return NovaPanel.ORBIT_KINDS.map(k => `
      <button class="orbit-tile" data-orbit="${k}">
        <span class="orbit-cap">${caps[k]}</span><span class="orbit-v">—</span><span class="orbit-sub"></span>
      </button>`).join("");
  }

  _orbitWire() {
    this.shadowRoot?.querySelectorAll(".orbit-tile[data-orbit]").forEach(t => {
      t.addEventListener("click", () => this._orbitShow(t.getAttribute("data-orbit")));
    });
  }

  // The figure and the line under it for each card, or null for "—".
  _orbitValues(energy) {
    const n = (v, dp) => this._presentNum(v, dp);
    const out = {};
    const sec = this._presentSecurityData();
    if (sec) {
      const alarm = !sec.alarm ? "" : sec.alarm.startsWith("armed_") ? this._tx("ARMED")
        : sec.alarm === "disarmed" ? this._tx("Disarmed") : sec.alarm === "triggered" ? this._tx("Triggered") : this._tx("Arming");
      const doors = sec.doors_total > 0 ? (sec.doors_open > 0 ? `${n(sec.doors_open, 0)} ${this._tx("Open")}` : this._tx("Closed")) : "";
      out.security = sec.locks_total ? [`${n(sec.locks_locked, 0)}/${n(sec.locks_total, 0)}`, alarm || doors]
        : [alarm || doors, ""];
    }
    const home = this._presentHomeData();
    if (home) out.home = [`${n(home.home, 0)}/${n(home.total, 0)}`, home.people.filter(p => p.home).slice(0, 2).map(p => p.name).join(", ")];
    if (energy) {
      const has = v => typeof v === "number" && isFinite(v);
      const big = has(energy.solar_w) ? `${n(energy.solar_w, 0)} W` : has(energy.solar_today_kwh) ? `${n(energy.solar_today_kwh, 1)} kWh` : "";
      if (big) out.energy = [big, has(energy.battery_pct) ? `${this._tx("Battery")} ${n(energy.battery_pct, 0)}%` : ""];
    }
    const cl = this._presentClimateData();
    if (cl && cl.temp != null) {
      const tenths = Math.round(cl.temp * 10) / 10;
      out.climate = [`${n(tenths, Number.isInteger(tenths) ? 0 : 1)}${cl.unit || "°"}`, cl.humidity != null ? `${n(cl.humidity, 0)}%` : ""];
    }
    const w = this._presentWeatherData();
    const wst = w && this._hass?.states?.[w.id];
    if (wst) {
      const unit = wst.attributes?.temperature_unit || wst.attributes?.unit_of_measurement || "°";
      const cond = w.id.startsWith("weather.") && typeof this._hass.formatEntityState === "function" ? this._hass.formatEntityState(wst) : "";
      out.weather = [`${n(w.temp, 0)}${unit}`, cond];
    }
    return out;
  }

  // Updates the five cards. Energy is read again when `force` is set or a
  // minute has passed; the others are read from what the panel already has.
  async _orbitRefresh(force = false) {
    const root = this.shadowRoot;
    if (!root?.getElementById("orbit")) return;
    if (force || !this._orbitEnergyAt || Date.now() - this._orbitEnergyAt > 60000) {
      this._orbitEnergyAt = Date.now();
      try { this._orbitEnergy = await this._presentEnergyData(); }
      catch (_) { this._orbitEnergy = null; }   // quiet: a card with no reading shows "—"
    }
    const vals = this._orbitValues(this._orbitEnergy);
    for (const k of NovaPanel.ORBIT_KINDS) {
      const t = root.querySelector(`.orbit-tile[data-orbit="${k}"]`);
      if (!t) continue;
      const [v, sub] = vals[k] || ["—", ""];
      this._orbitSetValue(t.querySelector(".orbit-v"), v);
      this._setText(t.querySelector(".orbit-sub"), sub);
      t.setAttribute("aria-label", `${t.querySelector(".orbit-cap").textContent} ${v} ${sub}`.trim());   // i18n-ok: built from translated parts
    }
  }

  // A figure that changes counts up or down to its new value.
  _orbitSetValue(el, text) {
    if (!el) return;
    const num = s => { const m = /^([\d.,]+)(.*)$/.exec(String(s || "")); return m ? [parseFloat(m[1].replace(",", ".")), m[2], m[1]] : null; };
    const from = num(el.textContent), to = num(text);
    if (this._reduceMotion || !from || !to || from[1] !== to[1] || from[0] === to[0] || /\//.test(text)) { this._setText(el, text); return; }
    const dp = (to[2].split(/[.,]/)[1] || "").length, start = performance.now();
    const step = now => {
      const p = Math.min(1, (now - start) / 700), e = 1 - Math.pow(1 - p, 3);
      this._setText(el, p < 1 ? `${this._presentNum(from[0] + (to[0] - from[0]) * e, dp)}${to[1]}` : text);
      if (p < 1) (window.requestAnimationFrame || (f => setTimeout(() => f(performance.now()), 16)))(step);
    };
    step(start);
  }

  // Tapping a card shows its pop-up card beside the head.
  async _orbitShow(kind) {
    const def = this._presentKinds()[kind];
    if (!def) return;
    let data = null;
    try { data = await def.ambient(); } catch (_) { /* quiet: nothing to show, the card stays as it is */ }
    if (!data || !def.html(data)) return;
    const now = Date.now() / 1000;
    this._presentRestartCycle();
    this._onPresentCard({ v: 1, kind, id: `orbit-${kind}-${Date.now()}`, created: now, expires: now + 8, data });
  }

  // A pop-up card that ends shrinks into its card, which glows. A copy of the
  // card does the moving, so the hero's own state changes at once.
  _orbitSettle(kind) {
    const root = this.shadowRoot, stage = root?.getElementById("heroStage"), card = root?.getElementById("presentCard");
    const tile = root?.querySelector(`.orbit-tile[data-orbit="${kind}"]`);
    if (!stage || !card || !tile) return;
    tile.classList.add("orbit-flash");
    setTimeout(() => tile.classList.remove("orbit-flash"), 1400);
    if (this._reduceMotion) return;
    const sb = stage.getBoundingClientRect(), cb = card.getBoundingClientRect(), tb = tile.getBoundingClientRect();
    const ghost = card.cloneNode(true);
    ghost.removeAttribute("id"); ghost.removeAttribute("role"); ghost.removeAttribute("tabindex");
    ghost.className = "present-card present-ghost";
    ghost.setAttribute("aria-hidden", "true");
    Object.assign(ghost.style, { left: `${cb.left - sb.left}px`, top: `${cb.top - sb.top}px`, width: `${cb.width}px` });
    stage.appendChild(ghost);
    const dx = (tb.left + tb.width / 2) - (cb.left + cb.width / 2), dy = (tb.top + tb.height / 2) - (cb.top + cb.height / 2);
    window.requestAnimationFrame?.(() => { ghost.style.transform = `translate(${dx}px,${dy}px) scale(.45)`; ghost.style.opacity = "0"; });
    setTimeout(() => ghost.remove(), 800);
  }

  // Nova's latest line under the greeting: the newest activity entry.
  _heroLatest() {
    const el = this.shadowRoot?.getElementById("heroLatest");
    if (!el) return;
    // The newest of Nova's plain sentences (dock.js), skipping the quiet
    // "let them pass" line unless there is nothing else.
    const lines = this._novaLines();
    const top = lines.find(l => l.kind !== "passed") || lines[0];
    const sig = top ? `${top.ts}|${top.text}` : "";
    if (sig === this._heroLatestSig) return;
    this._heroLatestSig = sig;
    this._setHtml(el, top ? `<span class="dot"></span><b>${this._esc(top.ts || "")}</b> ${this._esc(top.text)}` : "");
    el.classList.remove("hero-latest-in");
    void el.offsetWidth;
    el.classList.add("hero-latest-in");
  }

  // ── The arcs round the avatar ──
  // Three broken arcs turn round Nova's core, the middle one the other way,
  // and fade out behind the greeting. With less motion they are drawn once.
  _arcsStart() {
    const canvas = this.shadowRoot?.getElementById("heroArcs");
    if (!canvas || this._arcsRaf) return;
    let ctx = null;
    try { ctx = canvas.getContext("2d"); } catch (_) { ctx = null; }
    if (!ctx) return;
    this._arcsCtx = ctx;
    const frame = now => {
      this._arcsRaf = null;
      if (!canvas.isConnected || this._currentTab !== "dashboard") return;
      if (!document.hidden) this._arcsFrame(canvas, ctx, now);
      if (this._reduceMotion) return;
      const next = window.requestAnimationFrame || (f => setTimeout(() => f(performance.now()), 33));
      this._arcsRaf = next(frame) || true;
    };
    frame(performance.now());
    if (!this._arcsVisibility) {
      this._arcsVisibility = () => { if (!document.hidden && !this._arcsRaf) this._arcsStart(); };
      document.addEventListener("visibilitychange", this._arcsVisibility);
    }
  }

  _arcsStop() {
    if (this._arcsRaf && typeof this._arcsRaf === "number" && window.cancelAnimationFrame) window.cancelAnimationFrame(this._arcsRaf);
    this._arcsRaf = null;
  }

  _arcsFrame(canvas, ctx, now) {
    this._arcsDrawn = (this._arcsDrawn || 0) + 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (!W || !H) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const { hx, hy, S } = this._coreLayout({ W, H, narrow: W < 820 });
    // The arcs turn and glow with Nova's mood (core-rings.js): fast while
    // thinking, slow and dim asleep.
    const dt = this._arcsLast ? Math.min(0.1, (now - this._arcsLast) / 1000) : 0;
    this._arcsLast = now;
    const mood = this._core?.cur || this._coreMood();
    this._arcsClock = (this._arcsClock || 0) + dt * mood.speed;
    const t = this._reduceMotion ? 0 : this._arcsClock, lit = 0.4 + 0.6 * mood.glow;
    const a = this._theme().head.main.map(v => Math.round(v * 255));
    const col = alpha => `rgba(${a[0]},${a[1]},${a[2]},${alpha})`;
    ctx.lineCap = "round";
    // [radius, turn speed, arc pieces (start, end in radians), width, alpha]
    [[1.55, 0.35, [[0, 1.1], [1.6, 2.3], [3.4, 4.6], [5.0, 5.6]], 2, 0.55],
     [2.25, -0.22, [[0.4, 1.9], [2.6, 3.0], [3.6, 5.2]], 1.5, 0.35],
     [2.8, 0.12, [[0, 0.5], [1.2, 2.8], [4.0, 4.4], [4.9, 6.0]], 1, 0.22]].forEach(([k, speed, pieces, w, alpha]) => {
      const r = S * k, rot = t * speed;
      ctx.lineWidth = w; ctx.strokeStyle = col(alpha * lit);
      for (const [a0, a1] of pieces) {
        ctx.beginPath(); ctx.arc(hx, hy, r, a0 + rot, a1 + rot); ctx.stroke();
        const ex = hx + Math.cos(a1 + rot) * r, ey = hy + Math.sin(a1 + rot) * r;
        const g = ctx.createRadialGradient(ex, ey, 0, ex, ey, 6);
        g.addColorStop(0, col(0.9)); g.addColorStop(1, col(0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(ex, ey, 6, 0, 6.283); ctx.fill();
      }
    });
    // Clear the arcs behind the greeting so the words stay easy to read.
    const copy = this.shadowRoot?.querySelector(".hero-copy"), sb = canvas.getBoundingClientRect();
    if (copy) {
      const cb = copy.getBoundingClientRect(), x = cb.left - sb.left + cb.width / 2, y = cb.top - sb.top + cb.height / 2;
      ctx.save(); ctx.globalCompositeOperation = "destination-out";
      const fade = ctx.createRadialGradient(x, y, 10, x, y, Math.max(cb.width * 0.62, 60));
      fade.addColorStop(0, "rgba(0,0,0,1)"); fade.addColorStop(0.7, "rgba(0,0,0,.85)"); fade.addColorStop(1, "rgba(0,0,0,0)");   // colour-ok: an eraser, not a colour on screen
      ctx.fillStyle = fade; ctx.fillRect(x - cb.width, y - cb.height * 1.5, cb.width * 2, cb.height * 3);
      ctx.restore();
    }
  }
