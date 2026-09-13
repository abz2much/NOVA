/*
 * Nova — new Command Center look (v7.93.0).
 *
 * A genuinely separate implementation from nova-panel.js's Classic UI, per
 * Abi's explicit choice — full creative freedom over ongoing maintenance
 * cost. Registered as "nova-panel-new" and mounted only when a user opts
 * into ui_style="new" (see NovaPanelShell at the bottom of nova-panel.js,
 * which dynamically imports this file).
 *
 * V1 scope: the Command Center dashboard only. Every other surface
 * (Residence, Intrusion, Suggestions, Settings, Logs, Memory) still lives
 * in Classic — the "Look" selector in the top bar here is the way back.
 *
 * Design: an animated "stellar core" (Nova = a star's sudden brightening)
 * replaces a camera feed as the dashboard's visual anchor — it works
 * identically whether someone has zero cameras or twelve, and doesn't
 * repeat the cyan sci-fi-HUD look this project's name already evokes.
 * Camera Watch becomes an optional, collapsed card instead, since not
 * every camera integration (e.g. Eufy) streams live into Home Assistant.
 * Approved from a static mockup (nova-command-center-mockup.html) before
 * this real, live-data build.
 */
class NovaCommandCenterNew extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._liveData = null;
    this._activityData = null;
    this._renderedOnce = false;
    this._fetchInterval = null;
    this._lastActivitySig = null;
    this._flareUntil = 0;
    this._animHandle = null;
    this._particles = [];
    this._current = { speed: 0.20, count: 70, radiusMul: 1, glow: 0.55, hot: 0.35, flare: 0.05 };
    this._camOpen = false;
  }

  // ─── HA property contract — same shape as Classic's, see nova-panel.js ──
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    if (first) {
      this._render();
      this._startIntervals();
    }
  }
  get hass() { return this._hass; }
  set panel(panel) { this._config = panel?.config || {}; }
  set narrow(narrow) { this._narrow = narrow; }
  set route(route) { this._route = route; }

  connectedCallback() {
    if (!document.getElementById("nova-new-fonts")) {
      const l = document.createElement("link");
      l.id = "nova-new-fonts";
      l.rel = "stylesheet";
      l.href = "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Manrope:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap";
      document.head.appendChild(l);
    }
    if (this._hass && !this._renderedOnce) {
      this._render();
      this._startIntervals();
    }
  }

  disconnectedCallback() {
    if (this._fetchInterval) clearInterval(this._fetchInterval);
    if (this._animHandle) cancelAnimationFrame(this._animHandle);
    if (this._resizeListener) window.removeEventListener("resize", this._resizeListener);
  }

  _startIntervals() {
    this._fetchLiveData();
    if (!this._fetchInterval) {
      this._fetchInterval = setInterval(() => this._fetchLiveData(), 20000);
    }
  }

  // ─── Data ────────────────────────────────────────────────────────────────

  async _fetchLiveData() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_panel_data" });
      this._liveData = result;
      try {
        const log = await this._hass.callWS({ type: "nova/get_activity_log", hours: 2, limit: 8 });
        this._activityData = log?.entries || [];
      } catch (_) { this._activityData = []; }
    } catch (err) {
      console.warn("Nova (new look): panel data fetch failed", err);
    }
    try {
      this._solar = await this._hass.callWS({ type: "nova/solar", action: "status" });
    } catch (_) { this._solar = null; }
    this._detectFlare();
    this._renderData();
  }

  _detectFlare() {
    const top = (this._activityData || [])[0];
    const sig = top ? `${top.ts}|${top.tag}|${top.msg}` : null;
    if (sig && this._lastActivitySig && sig !== this._lastActivitySig) {
      this._flareUntil = Date.now() + 4000; // a real event just landed — brief flare
    }
    this._lastActivitySig = sig;
  }

  _coreState() {
    const sleepState = String(this._liveData?.status?.sleep?.state || "").toUpperCase();
    if (sleepState === "ASLEEP") return "asleep";
    if (Date.now() < this._flareUntil) return "reasoning";
    return "idle";
  }

  _data() {
    const live = this._liveData;
    if (!live) return null;
    return {
      status: live.status || {},
      areas: live.areas || [],
      cameras: live.config?.cameras || [],
      areasMonitored: live.meta?.areas_monitored ?? "—",
      occupied: (live.areas || []).filter(a => a.active).length,
    };
  }

  // ─── Render (structure once, patch data after) ──────────────────────────

  _render() {
    const root = this.shadowRoot;
    root.innerHTML = this._html();
    this._renderedOnce = true;
    this._wire();
    this._initCore();
    this._renderData();
  }

  _html() {
    return `
      <style>${this._css()}</style>
      <div class="wrap">
        <div class="topbar">
          <div class="brand">
            <div class="brand-mark"></div>
            <div>
              <div class="brand-name">Nova</div>
              <div class="brand-tag">Command Center</div>
            </div>
          </div>
          <div class="top-controls">
            <div class="look-switch">
              Look
              <select class="look-select" id="lookSelect">
                <option value="new" selected>New</option>
                <option value="classic">Classic</option>
              </select>
            </div>
          </div>
        </div>

        <div class="hero">
          <div class="core-wrap"><canvas class="core" id="core"></canvas></div>
          <div class="state-line" id="stateLine">Watching over the house.</div>
          <div class="state-sub" id="stateSub">—</div>
          <div class="chips" id="chips"></div>
        </div>

        <div class="grid">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Activity</div>
              <div class="panel-meta" id="feedMeta">—</div>
            </div>
            <div class="feed" id="feed"></div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Areas</div>
              <div class="panel-meta" id="areasMeta">—</div>
            </div>
            <div class="areas-grid" id="areasGrid"></div>
          </div>

          <div class="panel camera-panel" id="cameraPanel" hidden>
            <div class="camera-head-row">
              <div>
                <div class="panel-title" style="margin-bottom:5px">Camera Watch</div>
                <div class="camera-note">Optional — only shown for cameras that actually stream live into Home Assistant.</div>
              </div>
              <button class="camera-toggle" id="camToggle">SHOW CAMERAS ▾</button>
            </div>
            <div class="camera-strip" id="camStrip"></div>
          </div>
        </div>

        <div class="footnote">NOVA — NEW LOOK · PREVIEW · SETTINGS AND OTHER TABS LIVE IN CLASSIC FOR NOW</div>
      </div>
    `;
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  _renderData() {
    if (!this._renderedOnce) return;
    const d = this._data();
    const root = this.shadowRoot;
    if (!d) return;

    // hero state line
    const state = this._coreState();
    const lineEl = root.getElementById("stateLine");
    const subEl = root.getElementById("stateSub");
    const lines = {
      idle: ["Watching over the house.", "ALL QUIET · NOTHING NEEDS YOU RIGHT NOW"],
      reasoning: ["Something just happened.", "CHECK THE ACTIVITY FEED BELOW"],
      asleep: ["Everyone's asleep. Staying quiet.", "A GROUND-FLOOR BREACH WOULD STILL WAKE ME"],
    };
    if (lineEl) lineEl.textContent = lines[state][0];
    if (subEl) subEl.textContent = lines[state][1];
    this._targetCoreState(state);

    // status chips
    const chipDefs = [
      ["Observer", d.status.observer], ["Sleep", d.status.sleep], ["Broadcast", d.status.broadcast],
      ["Notify", d.status.notify], ["Satellites", d.status.satellites],
    ];
    const chipsEl = root.getElementById("chips");
    if (chipsEl) {
      chipsEl.innerHTML = chipDefs.map(([label, s]) => {
        const warn = (s?.level === "warn") ? " warn" : "";
        return `<div class="chip${warn}"><span class="dot"></span> ${this._esc(label)} <b>${this._esc(s?.state ?? "—")}</b></div>`;
      }).join("");
    }

    // activity feed
    const entries = (this._activityData && this._activityData.length)
      ? this._activityData
      : [{ ts: "--:--", tag: "SYSTEM", msg: "No activity yet." }];
    const feedEl = root.getElementById("feed");
    if (feedEl) {
      feedEl.innerHTML = entries.slice(0, 6).map(e => `
        <div class="feed-row">
          <div class="feed-text"><b>${this._esc(e.tag || "")}</b> · <span class="dim">${this._esc(e.msg || "")}</span></div>
          <div class="feed-time">${this._esc(e.ts || "")}</div>
        </div>`).join("");
    }
    const feedMeta = root.getElementById("feedMeta");
    if (feedMeta) feedMeta.textContent = `LAST ${Math.min(entries.length, 6)}`;

    // areas
    const areasGridEl = root.getElementById("areasGrid");
    if (areasGridEl) {
      areasGridEl.innerHTML = (d.areas || []).slice(0, 6).map(a => `
        <div class="area-tile${a.active ? " active" : ""}">
          <div class="area-name">${this._esc(a.name)}</div>
          <div class="area-stat"><span>lights</span><span>${a.lights_on ?? 0}/${a.lights_total ?? 0}</span></div>
          ${a.temp ? `<div class="area-stat"><span>temp</span><span>${this._esc(a.temp)}</span></div>` : ""}
        </div>`).join("");
    }
    const areasMeta = root.getElementById("areasMeta");
    if (areasMeta) areasMeta.textContent = `${d.occupied} OCCUPIED · ${d.areasMonitored} MONITORED`;

    // camera — collapsed, optional, honest
    const camPanel = root.getElementById("cameraPanel");
    const camStrip = root.getElementById("camStrip");
    if (camPanel && camStrip) {
      const cams = d.cameras || [];
      camPanel.hidden = cams.length === 0;
      const camToggle = root.getElementById("camToggle");
      if (camToggle) camToggle.textContent = this._camOpen ? "HIDE CAMERAS ▴" : `SHOW ${cams.length} CAMERA${cams.length === 1 ? "" : "S"} ▾`;
      camStrip.classList.toggle("open", this._camOpen);
      camStrip.innerHTML = cams.map(c => `<div class="camera-slot">${this._esc(c.name || c.entity_id)}</div>`).join("");
    }
  }

  _wire() {
    const root = this.shadowRoot;
    const camToggle = root.getElementById("camToggle");
    if (camToggle) {
      camToggle.addEventListener("click", () => {
        this._camOpen = !this._camOpen;
        this._renderData();
      });
    }
    const lookSelect = root.getElementById("lookSelect");
    if (lookSelect) {
      lookSelect.addEventListener("change", async (e) => {
        const style = e.target.value;
        if (!this._hass) return;
        try {
          await this._hass.callWS({ type: "nova/update_config", key: "ui_style", value: style });
        } catch (err) {
          console.error("Nova (new look): failed to save ui_style", err);
        }
        window.location.reload();
      });
    }
  }

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

  _css() {
    return `
      :host{
        --bg:#15110d; --surface:#1e1712; --surface-2:#2a2119; --line-soft:#33291f;
        --ink:#f3ece1; --ink-dim:#a89a89; --ink-faint:#7a6d5e;
        --ember:#e2542f; --gold:#f4b860; --gold-pale:#ffe3ad; --warn:#e8b23d;
        --font-display:'Fraunces',ui-serif,Georgia,serif;
        --font-body:'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif;
        --font-mono:'IBM Plex Mono',ui-monospace,'SF Mono',monospace;
      }
      *{box-sizing:border-box}
      .wrap{background:var(--bg);color:var(--ink);font-family:var(--font-body);
        padding:20px 16px 40px;min-height:100vh;
        background-image:radial-gradient(ellipse 900px 500px at 50% -8%, #2a1c1180 0%, transparent 60%);}
      .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px;flex-wrap:wrap;max-width:1100px;margin-inline:auto}
      .brand{display:flex;align-items:center;gap:11px}
      .brand-mark{width:26px;height:26px;border-radius:50%;flex:none;
        background:radial-gradient(circle at 34% 30%, var(--gold-pale), var(--gold) 42%, var(--ember) 78%, #7a2513 100%);
        box-shadow:0 0 14px 1px #e2542f55;}
      .brand-name{font-family:var(--font-display);font-size:18px;font-weight:600}
      .brand-tag{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.1em;text-transform:uppercase}
      .look-switch{display:flex;align-items:center;gap:8px;font-family:var(--font-mono);font-size:11px;color:var(--ink-faint)}
      select.look-select{background:var(--surface);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;font-weight:600;padding:6px 9px;border-radius:8px}
      .hero{max-width:1100px;margin:0 auto;background:linear-gradient(180deg,var(--surface),#19140fdd);
        border:1px solid var(--line-soft);border-radius:22px;padding:32px 20px 24px;
        display:flex;flex-direction:column;align-items:center;text-align:center}
      .core-wrap{width:min(70vw,280px);aspect-ratio:1/1;margin-bottom:4px}
      canvas.core{width:100%;height:100%;display:block}
      .state-line{font-family:var(--font-display);font-size:19px;font-weight:500;margin:4px 0 2px;text-wrap:balance}
      .state-sub{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em;margin-bottom:18px}
      .chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;padding-top:16px;border-top:1px solid var(--line-soft);width:100%}
      .chip{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;background:var(--surface-2);
        font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);border:1px solid var(--line-soft)}
      .chip .dot{width:6px;height:6px;border-radius:50%;background:#6fbf8a}
      .chip.warn .dot{background:var(--warn)}
      .chip b{color:var(--ink);font-weight:600}
      .grid{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1.15fr 1fr;gap:16px}
      @media (max-width:720px){.grid{grid-template-columns:1fr}}
      .panel{background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:16px 16px 14px}
      .panel-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
      .panel-title{font-family:var(--font-display);font-size:15px;font-weight:600}
      .panel-meta{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
      .feed-row{padding:9px 0;border-bottom:1px solid var(--line-soft);display:flex;justify-content:space-between;gap:10px}
      .feed-row:last-child{border-bottom:none}
      .feed-text{font-size:12.8px;line-height:1.4}
      .feed-text .dim{color:var(--ink-dim)}
      .feed-time{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);white-space:nowrap}
      .areas-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
      .area-tile{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:11px;padding:10px 11px}
      .area-tile.active{border-color:#e2542f4a}
      .area-name{font-size:12.5px;font-weight:600;margin-bottom:4px}
      .area-stat{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);display:flex;justify-content:space-between}
      .camera-panel{grid-column:1/-1}
      .camera-head-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      .camera-note{font-size:11.5px;color:var(--ink-dim);max-width:46ch}
      .camera-toggle{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);background:var(--surface-2);
        border:1px solid var(--line-soft);border-radius:8px;padding:6px 10px;cursor:pointer}
      .camera-strip{display:none;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:12px}
      .camera-strip.open{display:grid}
      .camera-slot{aspect-ratio:16/10;border-radius:9px;background:var(--surface-2);border:1px solid var(--line-soft);
        display:flex;align-items:center;justify-content:center;font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);text-align:center;padding:6px}
      .footnote{max-width:1100px;margin:20px auto 0;text-align:center;font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
    `;
  }
}

if (!customElements.get("nova-panel-new")) {
  customElements.define("nova-panel-new", NovaCommandCenterNew);
}
