/* GENERATED FILE: do not edit. Built by scripts/build_panel.py from
 * frontend/src/. Edit the sources there, then run the build. */
/*
 * Nova Command Center Panel.
 * v8.68.0
 *
 * Started life as "Command Center" — a genuinely separate implementation
 * from the original Classic UI, built with full creative freedom over
 * ongoing maintenance cost. Classic reached feature parity and was
 * deleted in v7.101.30; this is now Nova's one and only dashboard,
 * registered directly as "nova-panel" via panel_custom (no more style
 * switcher, no more dynamic import — this file loads on its own).
 *
 * Layout: Command Center + Settings, reorganized around what you're
 * trying to do rather than which subsystem it touches (General, Voice &
 * Speakers, Awareness & Safety, Learning & Memory, Cameras, Home &
 * Extras), plus a search box across every setting. All 27 Settings cards
 * are real. Intrusion, Faces, Suggestions, Logs, Memory and Energy are all
 * full nav tabs here.
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
class NovaPanel extends HTMLElement {
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
    this._holo = null;
    this._particles = [];
    this._current = { open: 1, wide: 0, brow: 0, smile: 0.45, mouth: 0, speed: 0.20, glow: 0.60, hot: 0.35, count: 60, sleep: 0 };
    this._heroTypeTimer = null;
    this._camOpen = false;
    this._cameraImages = {};
    this._cameraLoading = {};
    this._cameraDiagnostics = {};
    this._cameraInterval = null;
    this._cognitive = null;
    this._modeBindingsOpen = false;
    this._currentTab = "dashboard"; // "dashboard" | "settings" | "logs" | "diagnostics" | "memory" | "intrusion" | "faces" | "suggestions" | "energy" | "chat"
    this._logFilter = "all";
    this._logSearch = "";
    this._settingsSection = "general";
    this._settingsSearch = "";
    this._camListOpen = false;
    this._uiStrings = null;
    this._uiLangLoaded = null;
    this._uiLangRequest = 0;
  }

  // ─── HA property contract — same shape as Classic's, see nova-panel.js ──
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    if (first) {
      this._render();
      this._startIntervals();
      this._loadUiStrings();
    }
  }
  get hass() { return this._hass; }
  set panel(panel) { this._config = panel?.config || {}; }
  set narrow(narrow) { this._narrow = narrow; }
  set route(route) { this._route = route; }

  connectedCallback() {
    if (!window.__novaBannerLogged) {
      window.__novaBannerLogged = true;
      console.log("%c Nova Panel %c v8.68.0 ",
        "color: #f4b860; background: #1e0d06; padding: 2px 6px;",
        "color: #e2542f; background: #050403; padding: 2px 6px;");
    }
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
    } else if (this._renderedOnce && this._currentTab === "energy") {
      this._startEnergyFlowPoll();
    } else if (this._holoResume) {
      // Leaving the page released the hologram's graphics context, and a
      // canvas never gets a lost context back: start again on a fresh copy.
      this._holoResume = false;
      const old = this.shadowRoot.getElementById("core");
      if (old) { old.replaceWith(old.cloneNode(false)); this._initCore(); }
    }
    // Leaving the page closed the card subscription; start it again.
    if (this._renderedOnce && this._currentTab === "dashboard") this._presentStart();
  }

  disconnectedCallback() {
    if (this._fetchInterval) clearInterval(this._fetchInterval);
    if (this._sparklineInterval) clearInterval(this._sparklineInterval);
    if (this._cameraInterval) clearInterval(this._cameraInterval);
    this._holoResume = !!this._holo;
    this._holoStop();
    this._presentStop();
    if (this._animHandle) cancelAnimationFrame(this._animHandle);
    if (this._resizeListener) window.removeEventListener("resize", this._resizeListener);
    if (this._heroTypeTimer) { clearTimeout(this._heroTypeTimer); this._heroTypeTimer = null; }
    this._stopEnergyFlowPoll();
    if (this._flowVisListener) {
      document.removeEventListener("visibilitychange", this._flowVisListener);
      this._flowVisListener = null;
    }
  }

  _startIntervals() {
    this._fetchLiveData();
    if (!this._fetchInterval) {
      this._fetchInterval = setInterval(() => this._fetchLiveData(), 20000);
    }
    this._fetchAreaSparklines();
    if (!this._sparklineInterval) {
      // Trend history changes slowly — matches Classic's own 5-minute cadence
      // (nova-panel.js's _fetchAreaSparklines), no need to poll as often as
      // the main dashboard data.
      this._sparklineInterval = setInterval(() => this._fetchAreaSparklines(), 300000);
    }
  }

  async _fetchAreaSparklines() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_area_sparklines" });
      this._sparklines = res?.sparklines || {};
    } catch (err) {
      console.warn("Nova: sparkline fetch failed", err);
      return;
    }
    this._renderData();
  }

  // ─── Data ────────────────────────────────────────────────────────────────

  async _fetchLiveData() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_panel_data" });
      this._liveData = result;
      try {
        const log = await this._hass.callWS({ type: "nova/get_activity_log", hours: 24, limit: 60 });
        this._activityData = log?.entries || [];
      } catch (_) { this._activityData = []; }
    } catch (err) {
      console.warn("Nova: panel data fetch failed", err);
    }
    try {
      this._solar = await this._hass.callWS({ type: "nova/solar", action: "status" });
    } catch (_) { this._solar = null; }
    try {
      this._mode = await this._hass.callWS({ type: "nova/mode", action: "status" });
    } catch (_) { this._mode = null; }
    if (this._currentTab === "dashboard") {
      try {
        this._cognitive = await this._hass.callWS({ type: "nova/get_cognitive_status" });
      } catch (_) { this._cognitive = null; }
    }
    if (this._currentTab === "logs") this._fetchDebugLog();
    this._loadUiStrings();
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
      config: live.config || {},
      doorbellTraining: live.doorbell_training || {},
      suggestions: live.suggestions || [],
      suggestions_filtered: live.suggestions_filtered || [],
      goals: live.goals || [],
      lockdown: live.lockdown || live.config?.lockdown || {},
      onboarding: live.onboarding || live.config?.onboarding || null,
      available_labels: live.config?.available_labels || [],
    };
  }

  // ─── Render (structure once, patch data after) ──────────────────────────

  _render() {
    // Tearing down and rebuilding the DOM (now happening on every tab
    // switch, not just once) orphans the previous canvas — its
    // requestAnimationFrame loop and resize listener would otherwise keep
    // running forever on a detached, invisible canvas, compounding every
    // time someone switches tabs. Stop it before _initCore() starts a
    // fresh one.
    this._holoStop();
    this._presentStop();
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (this._resizeListener) { window.removeEventListener("resize", this._resizeListener); this._resizeListener = null; }
    this._ctx = null;
    this._canvas = null;

    const root = this.shadowRoot;
    root.innerHTML = this._html();   // i18n-ok: the first render, translated just below
    this._localizeDOM(root);
    this._renderedOnce = true;
    this._wire();
    this._initCore();
    this._renderData();
    if (this._currentTab === "dashboard") this._presentStart();
  }

  // Panel translations are keyed by exact English source strings. Dynamic
  // values (entity ids, model names, counts) therefore remain untouched, and
  // a missing key falls back to the English text already in the DOM.
  _resolveUiLang() {
    const override = this._liveData?.config?.ui_language;
    if (override && override !== "auto") return String(override);
    return String(this._hass?.language || "en");
  }

  async _loadUiStrings(force = false) {
    const full = (this._resolveUiLang() || "en").toLowerCase().replace(/_/g, "-");
    const base = full.split("-")[0];
    if (!force && this._uiLangLoaded === full) return;
    const request = ++this._uiLangRequest;
    this._uiLangLoaded = full;
    if (base === "en") {
      this._uiStrings = null;
      if (this._renderedOnce) this._render();
      return;
    }
    const grab = async (lang) => {
      const response = await fetch(`/nova_panel_static/i18n/${encodeURIComponent(lang)}.json`);
      if (!response.ok) return null;
      const value = await response.json();
      if (!value || Array.isArray(value) || typeof value !== "object") return null;
      return Object.fromEntries(Object.entries(value).filter(([k, v]) =>
        typeof k === "string" && typeof v === "string"));
    };
    try {
      let dict = await grab(full);
      if (!dict && full !== base) dict = await grab(base);
      if (request !== this._uiLangRequest) return;
      this._uiStrings = dict || null;
    } catch (_) {
      if (request !== this._uiLangRequest) return;
      this._uiStrings = null;
    }
    if (this._renderedOnce) this._render();
  }

  // One whole string, translated when a key matches it exactly (outer
  // spaces kept). The lookup every translated text node goes through.
  _tx(raw) {
    const dict = this._uiStrings;
    const text = raw == null ? "" : String(raw);
    if (!dict) return text;
    const key = text.trim();
    return key && Object.prototype.hasOwnProperty.call(dict, key) ? text.replace(key, dict[key]) : text;
  }

  _localizeDOM(root) {
    if (!this._uiStrings || !root) return;
    try {
      const walker = document.createTreeWalker(root, 4, null);
      const swaps = [];
      let node;
      while ((node = walker.nextNode())) {
        const raw = node.nodeValue;
        if (!raw) continue;
        const value = this._tx(raw);
        if (value !== raw) swaps.push([node, value]);
      }
      swaps.forEach(([textNode, value]) => { textNode.nodeValue = value; });
      root.querySelectorAll("[title],[placeholder]").forEach(el => {
        ["title", "placeholder"].forEach(attr => {
          const raw = el.getAttribute(attr);
          const value = raw == null ? raw : this._tx(raw);
          if (value !== raw) el.setAttribute(attr, value);
        });
      });
    } catch (_) { /* English DOM remains usable if localization fails. */ }
  }

  // Panel text set after the first render goes through these two, so a
  // translation reaches it the same way it reaches the first render.
  // tests/unit/test_panel_late_text.py fails on any other way of setting text.
  _setHtml(el, html) {
    if (!el) return;
    el.innerHTML = html;   // i18n-ok: the helper itself
    this._localizeDOM(el);
  }

  _setText(el, text) {
    if (!el) return;
    const value = this._tx(text);
    // Skips a set that would change nothing, as several callers did by hand.
    if (el.childElementCount || el.textContent !== value) el.textContent = value;   // i18n-ok: the helper itself
  }

  // Text with a value inside it, such as "{count} OCCUPIED", which the swap
  // above can never match. The template is the key, so a translation can move
  // the value. Values go in exactly as given: callers escape them where they
  // did before. With no translation, the English comes out exactly as before.
  //   _t      plain text: textContent, confirm()
  //   _tHtml  markup and attributes: a translation's own text is escaped
  _t(template, values) { return this._fillTemplate(template, values, false); }
  _tHtml(template, values) { return this._fillTemplate(template, values, true); }

  _fillTemplate(template, values, html) {
    const dict = this._uiStrings;
    const own = !!dict && Object.prototype.hasOwnProperty.call(dict, template);
    const text = own ? (html ? this._esc(dict[template]) : dict[template]) : template;
    const vals = values || {};
    return text.replace(/\{(\w+)\}/g, (m, k) =>
      (Object.prototype.hasOwnProperty.call(vals, k) ? String(vals[k]) : m));
  }

  _html() {
    const tab = this._currentTab;
    return `
      <style>${this._css()}</style>
      <div class="wrap">
        <div class="topbar">
          <div class="brand">
            <div class="brand-mark"></div>
            <div>
              <div class="brand-name">Nova</div>
              <div class="brand-tag">${tab === "settings" ? "Settings" : tab === "logs" ? "Logs" : tab === "memory" ? "Memory" : tab === "diagnostics" ? "Diagnostics" : tab === "intrusion" ? "Intrusion" : tab === "faces" ? "Faces" : tab === "suggestions" ? "Suggestions" : tab === "energy" ? "Energy" : tab === "chat" ? "Chat" : "Command Center"}</div>
            </div>
          </div>
          <nav class="top-nav">
            <button class="nav-tab${tab === "dashboard" ? " active" : ""}" data-tab="dashboard">Command Center</button>
            <button class="nav-tab${tab === "intrusion" ? " active" : ""}" data-tab="intrusion">Intrusion</button>
            <button class="nav-tab${tab === "chat" ? " active" : ""}" data-tab="chat">Chat</button>
            <button class="nav-tab${tab === "faces" ? " active" : ""}" data-tab="faces">Faces</button>
            <button class="nav-tab${tab === "suggestions" ? " active" : ""}" data-tab="suggestions">Suggestions</button>
            <button class="nav-tab${tab === "settings" ? " active" : ""}" data-tab="settings">Settings</button>
            <button class="nav-tab${tab === "logs" ? " active" : ""}" data-tab="logs">Logs</button>
            <button class="nav-tab${tab === "diagnostics" ? " active" : ""}" data-tab="diagnostics">Diagnostics</button>
            <button class="nav-tab${tab === "memory" ? " active" : ""}" data-tab="memory">Memory</button>
            <button class="nav-tab${tab === "energy" ? " active" : ""}" data-tab="energy">Energy</button>
          </nav>
          <button class="lockdown-control" id="lockdownControl" hidden></button>
        </div>

        ${tab === "settings" ? this._htmlSettings() : tab === "logs" ? this._htmlLogs() : tab === "memory" ? this._htmlMemory() : tab === "diagnostics" ? this._htmlDiagnostics() : tab === "intrusion" ? this._htmlIntrusion() : tab === "faces" ? this._htmlFaces() : tab === "suggestions" ? this._htmlSuggestions() : tab === "energy" ? this._htmlEnergy() : tab === "chat" ? this._htmlChat() : this._htmlDashboard()}

        <div class="footnote">NOVA COMMAND CENTER</div>
      </div>
    `;
  }

  _htmlDashboard() {
    return `
        <div id="onboardingMount"></div>
        <div class="hero">
          <div class="hero-stage" id="heroStage">
            <canvas class="core" id="core"></canvas>
            <div class="hero-copy">
              <div class="hero-word" id="heroWord"><span id="heroWordText"></span><span class="hero-caret" aria-hidden="true"></span></div>
              <div class="state-line" id="stateLine">Watching over the house.</div>
              <div class="state-sub" id="stateSub">—</div>
            </div>
            <div class="present-card" id="presentCard" role="button" tabindex="0" title="Dismiss"></div>
          </div>
          <div class="chips" id="chips"></div>
        </div>

        <div class="panel" id="operationalModePanel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Operational Mode</div>
          </div>
          <div id="operationalModeBody"></div>
        </div>
${this._htmlDashboardBody()}`;
  }

  // Setup Doctor problems (warn/down) for the welcome card, or null while
  // the one-off nova/get_setup_health fetch is still pending.
  _welcomeProblems() {
    const sh = this._setupHealth;
    if (!sh || sh.error || !Array.isArray(sh.checks)) return null;
    return sh.checks.filter(c => c.status === "warn" || c.status === "down");
  }

  _onboardingHtml(onboarding) {
    if (!onboarding || onboarding.dismissed) return "";
    const problems = this._welcomeProblems();
    // A fresh install also keeps the card up while Setup Doctor reports problems.
    const visible = onboarding.show || (onboarding.fresh && problems && problems.length > 0);
    if (!visible) return "";
    const sh = this._setupHealth;
    const active = (sh?.checks || []).filter(c => c.status !== "off");
    const checksLine = sh?.unauthorized
      ? `<small>Setup Doctor needs a Home Assistant admin account.</small>`
      : sh?.error
      ? `<small>Couldn't run Setup Doctor — restart Home Assistant after updating.</small>`
      : problems === null
        ? `<small>Checking…</small>`
        : problems.length === 0
          ? `<small>${this._tHtml("All {count} checks passed.", { count: this._esc(active.length) })}</small>`
          : `<small>${this._tHtml("{count} need attention:", { count: this._esc(problems.length) })}</small>${problems.map(c => `
            <small class="welcome-problem">• <b>${this._esc(c.name)}</b>: ${this._esc(c.detail || "")}${
              c.suggested_fix ? ` Fix: ${this._esc(c.suggested_fix)}` : ""}</small>`).join("")}`;
    const hello = this._helloState || {};
    const helloOut = hello.busy
      ? `<small>Waiting for Nova…</small>`
      : hello.reply ? `<small class="welcome-reply">Nova: ${this._esc(hello.reply)}</small>`
      : hello.error ? `<small class="welcome-error">${this._esc(hello.error)}</small>` : "";
    return `
      <div class="onboarding-card" id="onboardingCard">
        <div class="panel-head"><div><div class="panel-title">Welcome — get Nova working for you</div>
          <div class="toggle-desc">These steps are optional. Nova can already answer you.</div></div>
          <button class="camera-toggle" id="onboardingDismiss" title="Dismiss">DISMISS</button></div>
        <div class="onboarding-progress"><span>${this._tHtml("{done}/{total} DONE", { done: this._esc(onboarding.done_count || 0), total: this._esc(onboarding.total || 0) })}</span><i style="width:${Math.round(((onboarding.done_count || 0) / Math.max(1, onboarding.total || 1)) * 100)}%"></i></div>
        <div class="welcome-checks" id="welcomeChecks"><b>Setup checks</b>${checksLine}</div>
        <div class="onboarding-steps">${(onboarding.steps || []).map(step => `<div class="onboarding-step${step.done ? " done" : ""}">
          <span>${step.done ? "✓" : "○"}</span><div><b>${this._esc(this._tx(step.label))}</b><small>${this._esc(this._tx(step.hint))}</small></div>
          ${step.jump ? `<button class="mode-chip onboarding-jump" data-settings-title="${this._esc(step.jump)}">OPEN</button>` : ""}</div>`).join("")}</div>
        <div class="welcome-hello" id="welcomeHello">
          <button class="mode-chip" id="onboardingHello"${hello.busy ? " disabled" : ""}>SAY HELLO</button>
          <div>${helloOut || `<small>Sends "Hello" to Nova and shows the reply, to check it can answer.</small>`}</div>
        </div>
        <button class="mode-chip onboarding-settings">OPEN SETTINGS</button>
      </div>`;
  }

  _htmlDashboardBody() {
    return `
        <div class="panel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Areas</div>
            <div class="panel-meta" id="areasMeta">—</div>
          </div>
          <div class="areas-grid" id="areasGrid"></div>
        </div>

        <div class="dashboard-pair">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Cognitive Core</div>
              <div class="panel-meta" id="cognitiveState">—</div>
            </div>
            <div class="metric-grid" id="cognitiveMetrics"></div>
            <div class="toggle-desc" id="cognitiveAnalysis"></div>
          </div>
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Goals</div>
              <div class="panel-meta" id="goalsMeta">—</div>
            </div>
            <div class="goal-list" id="goalList"></div>
            <div class="goal-create">
              <input class="cfg-field" id="goalOutcome" maxlength="500" placeholder="Outcome Nova should work toward">
              <button class="mode-chip" id="goalCreate">ADD GOAL</button>
            </div>
            <div class="toggle-desc" id="goalResult"></div>
          </div>
        </div>

        <div class="panel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Quick Actions</div>
            <div class="panel-meta">CMD</div>
          </div>
          <div class="mode-grid">
            <button class="mode-chip" data-svc="nova.briefing">Briefing</button>
            <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":30}'>Nap 30m</button>
            <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":60}'>Nap 60m</button>
            <button class="mode-chip" data-svc="nova.unshush">Unshush All</button>
            <button class="mode-chip" data-svc="nova.observer_status">Status Dump</button>
            <button class="mode-chip" id="qaRunAnalysis">Analyze Now</button>
          </div>
          <div class="toggle-desc" id="qaAnalysisResult" style="margin-top:8px"></div>
        </div>

        <div class="panel" id="mutesPanel" style="max-width:1100px;margin:16px auto 0" hidden>
          <div class="panel-head">
            <div class="panel-title">Muted</div>
            <div class="panel-meta" id="mutesMeta">SAVED</div>
          </div>
          <div id="mutesBody"></div>
        </div>

        <div class="panel camera-panel" id="cameraPanel" style="max-width:1100px;margin:16px auto 0" hidden>
          <div class="camera-head-row">
            <div>
              <div class="panel-title" style="margin-bottom:5px">Camera Watch</div>
              <div class="camera-note">Authenticated snapshots from cameras available to Home Assistant.</div>
            </div>
            <button class="camera-toggle" id="camToggle">SHOW CAMERAS ▾</button>
          </div>
          <div class="camera-strip" id="camStrip"></div>
        </div>

        <div class="grid">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Activity</div>
              <div class="panel-meta" id="feedMeta">—</div>
            </div>
            <div class="feed" id="feed"></div>
          </div>
        </div>
    `;
  }

  // ─── Logs ─────────────────────────────────────────────────────────────
  // Ported from Classic's own _fetchDebugLog (nova-panel.js) — same single
  // nova/get_debug_log call, same client-side category+search filtering,
  // and the same escaping discipline: e.ts/e.cat/e.msg are log CONTENT
  // (entity names, states, model output can end up in them), so they are
  // attacker/LLM-influenced and go through this._esc() before innerHTML —
  // this is a real fixed-XSS surface in Classic, not decorative caution.
  // Kept in sync with every literal category string passed to nova_log()
  // across the backend (grep `nova_log("` to re-verify). ROUTE/REASON/TTS
  // removed 13 Sept 2026: nothing in the backend logs under those
  // categories any more, so their chips could never match a real entry —
  // caught live when a real "LEARN" entry (used, but missing from both
  // this list and the color map) fell through to a bare "•" bullet with
  // no way to filter for it.
  static LOG_FILTERS = ["all", "CONV", "REPLY", "LOCAL", "LEARN", "AGENT", "AUTO", "MODE", "CONFIG",
    "CLASSIFY", "CAMERA", "ENERGY", "BIO", "OFFER", "SAFETY", "ERROR", "WARNING", "GATE", "DEDUP", "OFFLINE", "VOICE"];
  static LOG_CATEGORIES = {
    CONV: { color: "#5fd0e0", icon: "💬" },
    REPLY: { color: "#4fb8ff", icon: "💭" },
    LOCAL: { color: "#5fbf7a", icon: "⚡" },
    LEARN: { color: "#8fd15c", icon: "🧠" },
    AGENT: { color: "var(--gold)", icon: "🤖" },
    AUTO: { color: "#ffb454", icon: "🔁" },
    MODE: { color: "#c9a0ff", icon: "🎚️" },
    CONFIG: { color: "#9d8cff", icon: "⚙️" },
    CLASSIFY: { color: "#9d8cff", icon: "🏷️" },
    CAMERA: { color: "#5fbf7a", icon: "📷" },
    ENERGY: { color: "#ffcf6a", icon: "🔌" },
    BIO: { color: "#ff8fc7", icon: "💓" },
    OFFER: { color: "#ffd27a", icon: "🙋" },
    SAFETY: { color: "#ff8a8a", icon: "🛡️" },
    ERROR: { color: "#ff6b81", icon: "❌" },
    WARNING: { color: "var(--warn)", icon: "⚠️" },
    GATE: { color: "var(--ink-faint)", icon: "🚧" },
    DEDUP: { color: "var(--ink-faint)", icon: "🔇" },
    OFFLINE: { color: "var(--ink-faint)", icon: "📴" },
    VOICE: { color: "#7fe0c4", icon: "🎙️" },       // Nova Voice ID result (8.36.0)
  };

  _htmlLogs() {
    const filterChips = NovaPanel.LOG_FILTERS.map(f =>
      `<button class="mode-chip new-log-filter${(this._logFilter || "all") === f ? " mode-chip-on" : ""}" data-filter="${f}">${f.toUpperCase()}</button>`).join("");
    const view = this._logView || "system";
    const title = view === "decisions" ? "Decisions" : view === "spoken_history" ? "Spoken History"
      : view === "actions" ? "Actions" : view === "shadow" ? "New path (shadow)" : "System Log";
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">${title}</div>
            <div class="panel-meta">Nova internal</div>
          </div>
          <div class="mode-grid">
            <button class="mode-chip new-logview${view === "system" ? " mode-chip-on" : ""}" data-view="system">SYSTEM LOG</button>
            <button class="mode-chip new-logview${view === "decisions" ? " mode-chip-on" : ""}" data-view="decisions">DECISIONS</button>
            <button class="mode-chip new-logview${view === "spoken_history" ? " mode-chip-on" : ""}" data-view="spoken_history">SPOKEN HISTORY</button>
            <button class="mode-chip new-logview${view === "actions" ? " mode-chip-on" : ""}" data-view="actions">ACTIONS</button>
            <button class="mode-chip new-logview${view === "shadow" ? " mode-chip-on" : ""}" data-view="shadow">SHADOW</button>
          </div>
          ${view === "decisions" ? this._htmlDecisionsView() : view === "spoken_history" ? this._htmlSpokenHistoryView()
            : view === "actions" ? this._htmlActionsView() : view === "shadow" ? this._htmlShadowView() : `
          <div class="cfg-row">
            <input id="newLogSearch" class="cfg-field" style="flex:1" type="text" placeholder="search…" autocomplete="off" value="${this._esc(this._logSearch || "")}">
          </div>
          <div class="mode-grid">${filterChips}</div>
          <div class="toggle-desc" id="newLogCount" style="margin:8px 0"></div>
          <div id="newLogEntries" class="new-log-entries">
            <div class="stub-body">Loading…</div>
          </div>`}
        </div>
    `;
  }

  // ─── Spoken History (v7.104.0) ──────────────────────────────────────────
  // The last things Nova actually sent to a speaker — welcome-home,
  // reminders, alerts, briefings, manual tests, confirmed Assist replies,
  // and repeats. Text only, bounded to the last 100, newest first. Always
  // renders its heading; a distinct empty vs. error state, same pattern as
  // Provider Activity/Installed Automations.

  _spokenSourceLabel(source) {
    return {
      welcome: "Welcome", reminder: "Reminder", alert: "Alert",
      briefing: "Briefing", camera: "Camera", manual: "Manual",
      reply: "Reply", repeat: "Repeat", routine: "Routine", scene: "Scene",
      confirm: "Confirmation", followup: "Follow-up",
    }[source] || (source ? source[0].toUpperCase() + source.slice(1) : "Other");
  }

  _speakerLabel(eid) {
    const st = this._hass?.states?.[eid];
    return (st && st.attributes && st.attributes.friendly_name) || eid;
  }

  _htmlSpokenHistoryView() {
    // Static shell only — _fetchSpokenHistory()/_renderSpokenHistoryRows()
    // update #spokenHistoryEntries directly, the same way _fetchDecisions()/
    // _renderDecisionRows() do, so a fetch never has to go through a full
    // _render() (which would re-trigger _wire() and re-fetch, looping).
    // #spokenHistoryMsg shows the outcome of a Repeat, the same inline
    // message line the Faces tab uses (#facesMsg) (8.7.24).
    return `<div class="toggle-desc" id="spokenHistoryMsg" style="margin:4px 0"></div>
      <div id="spokenHistoryEntries"><div class="stub-body">Loading…</div></div>`;
  }

  async _fetchSpokenHistory() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_spoken_history" });
      this._spokenHistory = result.entries || [];
    } catch (_) { this._spokenHistory = null; }
    this._renderSpokenHistoryRows();
  }

  _renderSpokenHistoryRows() {
    const container = this.shadowRoot?.getElementById("spokenHistoryEntries");
    if (!container) return;
    const entries = this._spokenHistory;
    if (entries === null) {
      this._setHtml(container, `<div class="stub-body">Couldn't load spoken history.</div>`);
      return;
    }
    if (!entries || !entries.length) {
      this._setHtml(container, `<div class="stub-body">No spoken messages recorded yet.</div>`);
      return;
    }
    this._setHtml(container, entries.map(e => {
      const when = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString() : "";
      const speakerNames = (e.speakers || []).map(s => this._speakerLabel(s)).join(", ") || "—";
      return `
        <div class="cfg-row">
          <label>${this._esc(this._spokenSourceLabel(e.source))} · ${this._esc(when)}</label>
          <span class="toggle-desc">${this._esc((e.delivery_state || "sent").toUpperCase())}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 4px">${this._esc(e.text)}</div>
        <div class="cfg-row">
          <span class="toggle-desc">${this._esc(speakerNames)}</span>
          <button class="mode-chip new-spoken-repeat" data-spoken-id="${e.id}">REPEAT</button>
        </div>`;
    }).join(""));
    container.querySelectorAll(".new-spoken-repeat").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = parseInt(btn.getAttribute("data-spoken-id"), 10);
        if (!isNaN(id)) this._repeatSpoken(id);
      });
    });
  }

  async _repeatSpoken(spokenId) {
    if (!this._hass) return;
    const msg = this.shadowRoot?.getElementById("spokenHistoryMsg");
    if (msg) this._setText(msg, "");
    try {
      await this._hass.callWS({ type: "nova/repeat_spoken", spoken_id: spokenId });
      this._fetchSpokenHistory();
    } catch (err) {
      // The server's own message, e.g. no_speaker: "The speaker this was
      // said on is not available, so it was not repeated".
      if (msg) this._setText(msg, (err && err.message) || "Could not repeat that.");
    }
  }

  // ─── Actions (Action Audit Log) ─────────────────────────────────────────
  // Actions Nova genuinely attempted or performed — device controls, bulk
  // controls, scene/script/automation execution, safety routines, suggested-
  // automation installation, notifications. Request-level, keyset-paginated:
  // one page is a set of COMPLETE request groups (a bulk action's targets
  // are never split across pages). Strictly read-only — no retry/replay/
  // approve/reject control here, matching Spoken History and Decisions.

  _actionStatusClass(status) {
    return {
      success: "diag-ok", verified: "diag-ok",
      partial: "diag-warn", awaiting: "diag-warn",
      failed: "diag-down", blocked: "diag-down",
    }[status] || "diag-idle";
  }

  _htmlActionsView() {
    // Static shell only — _fetchActions()/_renderActionRows() update
    // #actionEntries directly, same pattern as Spoken History/Decisions, so
    // a fetch never re-triggers a full _render().
    return `
      <div id="actionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="actionLoadMoreRow" hidden>
        <button class="mode-chip" id="newActionLoadMore">LOAD MORE</button>
      </div>
    `;
  }

  async _fetchActions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._actions = []; this._actionsCursor = null; }
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (container && reset) this._setHtml(container, `<div class="stub-body">Loading…</div>`);
    try {
      const args = { type: "nova/list_actions", limit: 20 };
      if (!reset && this._actionsCursor) {
        args.cursor_ts = this._actionsCursor.ts;
        args.cursor_request_id = this._actionsCursor.request_id;
      }
      const result = await this._hass.callWS(args);
      const page = result.requests || [];
      this._actions = reset ? page : (this._actions || []).concat(page);
      this._actionsCursor = result.next_cursor || null;
      this._renderActionRows();
    } catch (err) {
      this._actions = null;
      this._renderActionRows();
    }
  }

  _actionLabel(a) {
    return (a.action || "").replace(/_/g, " ");
  }

  _renderActionRows() {
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (!container) return;
    const requests = this._actions;
    if (requests === null) {
      this._setHtml(container, `<div class="new-log-entry-error" style="padding:12px">Couldn't load actions.</div>`);
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    if (!requests || !requests.length) {
      this._setHtml(container, `<div class="stub-body">No actions recorded yet.</div>`);
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    this._setHtml(container, requests.map(r => {
      const when = r.ts_created ? new Date(r.ts_created * 1000).toLocaleString() : "";
      const statusCls = this._actionStatusClass(r.status);
      const requester = r.requested_by_name || r.requested_by_user_id || r.request_device_id || "";
      const targets = r.targets || [];
      const spokenNote = (r.spoken_history_id !== null && r.spoken_history_id !== undefined)
        ? `<span class="toggle-desc" title="Linked Spoken History entry">🔊 spoken</span>` : "";
      const targetRows = targets.map(t => {
        const targetName = t.entity_id || [t.domain, t.service].filter(Boolean).join(".") || "—";
        return `
          <div class="cfg-row">
            <label>${this._esc(targetName)}</label>
            <span class="toggle-desc">${this._tHtml("approval: {approval} · execution: {execution}", { approval: this._esc(t.approval_result), execution: this._esc(t.execution_result) })}</span>
          </div>
          ${t.reason_text ? `<div class="stub-body" style="margin:-4px 0 6px;font-size:11px">${this._esc(t.reason_text)}</div>` : ""}`;
      }).join("");
      return `
        <details class="new-log-entry" style="display:block">
          <summary style="cursor:pointer;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="new-log-ts">${this._esc(when)}</span>
            <span class="new-log-cat">${this._esc((r.source || "").toUpperCase())}</span>
            <span class="new-log-msg">${this._esc(this._actionLabel(r))}${requester ? " · " + this._esc(requester) : ""}</span>
            <span class="${statusCls}">${this._esc((r.status || "").toUpperCase())}</span>
            ${spokenNote}
          </summary>
          <div style="margin-top:8px">${targetRows || '<div class="stub-body">No target detail.</div>'}</div>
        </details>`;
    }).join(""));
    const loadMoreRow = this.shadowRoot?.getElementById("actionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._actionsCursor;
  }

  // ─── New path in shadow (8.48.0) ────────────────────────────────────────
  // Anticipation alerts are judged twice from the same reading: by the
  // current path, which still decides, and by the new convenience path,
  // which only records what it would have done. This view shows each pair
  // with its reasons, asks whether to stop a pattern after two Not helpful
  // ratings in a row, and lets you switch the new path on per pattern once
  // the comparison is long enough. Patterns, messages and reasons are
  // household content, so everything goes through _esc().

  _htmlShadowView() {
    return `
      <div class="toggle-desc" style="margin:8px 0">Nova's anticipation alerts are judged by the current path and, in shadow, by the new one. The new path sends nothing until you switch it on for a pattern.</div>
      <div id="shadowAsks"></div>
      <div class="mode-bind-head">Patterns</div>
      <div id="shadowPatterns" class="new-log-entries"><div class="stub-body">Loading…</div></div>
      <div class="mode-bind-head">Latest verdicts: current vs new</div>
      <div id="shadowRecent" class="new-log-entries"><div class="stub-body">Loading…</div></div>
    `;
  }

  async _fetchShadow() {
    if (!this._hass) return;
    try {
      this._shadow = await this._hass.callWS({ type: "nova/convenience_status" });
      this._renderShadow();
    } catch (err) {
      const c = this.shadowRoot?.getElementById("shadowPatterns");
      if (c) this._setHtml(c, `<div class="new-log-entry-error" style="padding:12px">${this._tHtml("Error loading the comparison: {error}", { error: this._esc(err) })}</div>`);
    }
  }

  _shadowKind(kind) {
    return {
      anticipation_overdue: "Usual activity", anticipation_routine: "Routine start",
      anticipation_departure: "Departure reminder", anticipation_presence: "Comings and goings",
      anticipation_arriving: "Heading home",
    }[kind] || kind || "Pattern";
  }

  _renderShadow() {
    const root = this.shadowRoot;
    const data = this._shadow || {};
    const patterns = data.patterns || [];
    const btn = (key, choice, label) =>
      `<button class="mode-chip new-shadow-choice" data-key="${this._esc(key)}" data-choice="${choice}">${label}</button>`;
    const asks = patterns.filter(p => p.ask_stop);
    const asksEl = root?.getElementById("shadowAsks");
    if (asksEl) this._setHtml(asksEl, asks.map(p => `
      <div class="panel" style="margin:8px 0">
        <div class="toggle-desc">${this._tHtml("You rated the last two of these Not helpful. Stop this pattern? Only this one stops, and you can restart it here.")}</div>
        <div class="cfg-row"><label>${this._esc(this._shadowKind(p.kind))}</label><span>${this._esc(p.example || p.pattern_key)}</span></div>
        <div class="mode-grid">${btn(p.pattern_key, "stop", "STOP IT")}${btn(p.pattern_key, "keep", "KEEP IT")}</div>
      </div>`).join(""));
    const pEl = root?.getElementById("shadowPatterns");
    if (pEl) this._setHtml(pEl, patterns.length ? patterns.map(p => {
      const state = p.stopped ? "STOPPED BY YOU" : p.active ? "NEW PATH DECIDES" : p.ready ? "READY TO SWITCH" : "SHADOW";
      const actions = [
        p.stopped ? btn(p.pattern_key, "restart", "RESTART") : "",
        p.active ? btn(p.pattern_key, "deactivate", "BACK TO CURRENT")
          : (p.ready && !p.stopped ? btn(p.pattern_key, "activate", "SWITCH TO NEW") : ""),
      ].join("");
      return `<div class="new-log-entry">
          <span class="new-log-cat">${this._esc(this._shadowKind(p.kind))}</span>
          <span class="new-log-msg">${this._esc(p.example || p.pattern_key)}<br><small>${this._tHtml("{agree} of {n} verdicts agree", { agree: this._esc(p.agree), n: this._esc(p.decisions) })}${p.first_ts ? " · " + this._tHtml("since {date}", { date: this._esc(new Date(p.first_ts * 1000).toLocaleDateString()) }) : ""}</small></span>
          <span class="diag-idle">${state}</span>
          <span>${actions}</span>
        </div>`;
    }).join("") + `<div class="toggle-desc" style="margin:8px 0">${this._tHtml("A pattern can switch to the new path after {days} days and {n} comparisons.", { days: this._esc(data.min_days), n: this._esc(data.min_decisions) })}</div>`
      : `<div class="stub-body">No anticipation alerts compared yet.</div>`);
    const rEl = root?.getElementById("shadowRecent");
    const recent = data.recent || [];
    if (rEl) this._setHtml(rEl, recent.length ? recent.map(r => {
      const same = r.actual === r.shadow;
      return `<div class="new-log-entry">
          <span class="new-log-ts">${this._esc(new Date(r.ts * 1000).toLocaleString())}</span>
          <span class="new-log-msg">${this._esc(r.message)}<br>
            <small>${this._tHtml("Current: {v} ({why})", { v: this._esc(r.actual), why: this._esc(r.actual_reason) })}</small><br>
            <small>${this._tHtml("New: {v} ({why})", { v: this._esc(r.shadow), why: this._esc(r.shadow_reason) })}</small></span>
          <span class="${same ? "diag-ok" : "diag-warn"}">${same ? "AGREE" : "DIFFER"}</span>
        </div>`;
    }).join("") : `<div class="stub-body">Nothing yet.</div>`);
    root?.querySelectorAll(".new-shadow-choice").forEach(b => b.addEventListener("click", async () => {
      b.setAttribute("disabled", "disabled");
      try {
        await this._hass.callWS({ type: "nova/convenience_choice",
          pattern_key: b.getAttribute("data-key"), choice: b.getAttribute("data-choice") });
      } catch (err) {
        console.warn("Nova: convenience choice failed", err);
      }
      this._fetchShadow();
    }));
  }

  // ─── Decisions (Phase 1: decision explanations + feedback) ─────────────
  // A bounded, cursor-paginated browser over Nova's Decision Record store —
  // separate from the System Log above (nova/get_debug_log): these are the
  // structured observation/interpretation/evidence/outcome rows behind
  // nova/get_calibration's aggregate stats, not free-text log lines.

  _htmlDecisionsView() {
    return `
      <div class="cfg-row">
        <button class="mode-chip${this._decisionsUnjudgedOnly ? " mode-chip-on" : ""}" id="newDecUnjudged">UNJUDGED ONLY</button>
      </div>
      <div class="toggle-desc" id="newDecCount" style="margin:8px 0"></div>
      <div id="decisionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="decisionLoadMoreRow" hidden>
        <button class="mode-chip" id="newDecLoadMore">LOAD MORE</button>
      </div>
      <div id="decisionDrawer" class="new-decision-drawer" hidden></div>
    `;
  }

  async _fetchDecisions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._decisions = []; this._decisionsCursor = null; }
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (container && reset) this._setHtml(container, `<div class="stub-body">Loading…</div>`);
    try {
      const args = { type: "nova/list_decisions", limit: 50, only_unjudged: !!this._decisionsUnjudgedOnly };
      if (!reset && this._decisionsCursor) {
        args.cursor_ts = this._decisionsCursor.ts;
        args.cursor_id = this._decisionsCursor.id;
      }
      const result = await this._hass.callWS(args);
      this._decisions = reset ? (result.decisions || []) : (this._decisions || []).concat(result.decisions || []);
      this._decisionsCursor = result.next_cursor || null;
      this._renderDecisionRows();
    } catch (err) {
      if (container) this._setHtml(container, `<div class="new-log-entry-error" style="padding:12px">${this._tHtml("Error loading decisions: {error}", { error: this._esc(err) })}</div>`);
    }
  }

  _renderDecisionRows() {
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (!container) return;
    const entries = this._decisions || [];
    const countEl = this.shadowRoot?.getElementById("newDecCount");
    if (countEl) this._setText(countEl, this._t("{count} decision(s) loaded", { count: entries.length }));
    this._setHtml(container, entries.length ? entries.map(d => {
      const outcomeCls = d.outcome === "good" ? "diag-ok" : d.outcome === "wrong" ? "diag-down"
        : d.outcome === "unnecessary" ? "diag-warn" : "diag-idle";
      const outcomeLabel = d.outcome ? d.outcome.toUpperCase() : "UNJUDGED";
      const when = d.ts ? new Date(d.ts * 1000).toLocaleString() : "";
      return `<div class="new-log-entry new-decision-row" data-id="${this._esc(d.id)}">
          <span class="new-log-ts">${this._esc(when)}</span>
          <span class="new-log-cat">${this._esc((d.kind || "").toUpperCase())}</span>
          <span class="new-log-msg">${this._esc(d.decision || "")}</span>
          <span class="${outcomeCls}">${this._esc(outcomeLabel)}</span>
        </div>`;
    }).join("") : `<div class="stub-body">No decisions recorded yet.</div>`);
    const loadMoreRow = this.shadowRoot?.getElementById("decisionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._decisionsCursor;
    container.querySelectorAll(".new-decision-row").forEach(row => {
      row.addEventListener("click", () => this._openDecisionDetail(parseInt(row.getAttribute("data-id"), 10)));
    });
  }

  _decisionBlockHtml(label, value) {
    const text = value === null || value === undefined || value === ""
      ? "—" : (typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));
    return `<div class="mode-bind-head">${this._esc(label)}</div>
      <pre class="new-sug-yaml" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin:0 0 8px">${this._esc(text)}</pre>`;
  }

  async _openDecisionDetail(id) {
    const drawer = this.shadowRoot?.getElementById("decisionDrawer");
    if (!drawer) return;
    drawer.hidden = false;
    this._setHtml(drawer, `<div class="stub-body">Loading…</div>`);
    try {
      const result = await this._hass.callWS({ type: "nova/get_decision", decision_id: id });
      const d = result.decision || {};
      const judged = !!d.outcome;
      const row = (label, value) => `<div class="cfg-row"><label>${this._esc(label)}</label><span>${this._esc(
        value === null || value === undefined || value === "" ? "—" : String(value))}</span></div>`;
      this._setHtml(drawer, `
        <div class="panel-head"><div class="panel-title">${this._tHtml("Decision #{id}", { id: this._esc(d.id) })}</div>
          <button class="mode-chip" id="newDecCloseDrawer">CLOSE</button></div>
        ${row("Route", d.kind)}
        ${row("Decision", d.decision)}
        ${row("Reason", d.reason)}
        ${row("Confidence", d.confidence)}
        ${row("Model", d.model)}
        ${row("Tokens", d.tokens)}
        ${row("Latency (ms)", d.latency_ms)}
        ${row("Outcome", d.outcome)}
        ${this._decisionBlockHtml("Observation", d.observation)}
        ${this._decisionBlockHtml("Interpretation", d.interpretation)}
        ${this._decisionBlockHtml("Evidence", d.evidence)}
        <div class="mode-bind-head">Feedback</div>
        <div class="mode-grid">
          <button class="mode-chip new-dec-fb" data-verdict="good" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>HELPFUL</button>
          <button class="mode-chip new-dec-fb" data-verdict="unnecessary" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>UNNECESSARY</button>
          <button class="mode-chip new-dec-fb" data-verdict="wrong" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>WRONG</button>
        </div>
        <div class="toggle-desc" id="newDecFbStatus">${judged ? `Already judged: ${this._esc(d.outcome)}` : ""}</div>
      `);
      drawer.querySelector("#newDecCloseDrawer")?.addEventListener("click", () => {
        drawer.hidden = true; this._setHtml(drawer, "");
      });
      drawer.querySelectorAll(".new-dec-fb").forEach(btn => {
        btn.addEventListener("click", () => this._submitDecisionOutcome(
          parseInt(btn.getAttribute("data-id"), 10), btn.getAttribute("data-verdict")));
      });
    } catch (err) {
      this._setHtml(drawer, `<div class="new-log-entry-error" style="padding:12px">${this._tHtml("Error loading decision: {error}", { error: this._esc(err) })}</div>`);
    }
  }

  async _submitDecisionOutcome(id, verdict) {
    const statusEl = this.shadowRoot?.getElementById("newDecFbStatus");
    try {
      const result = await this._hass.callWS({ type: "nova/set_decision_outcome", decision_id: id, verdict });
      const disableButtons = () => this.shadowRoot?.querySelectorAll(".new-dec-fb")
        .forEach(b => b.setAttribute("disabled", "disabled"));
      if (result.status === "ok") {
        if (statusEl) this._setText(statusEl, this._t("Recorded: {verdict}", { verdict }));
        disableButtons();
        this._fetchDecisions(true);
      } else if (result.status === "already_judged") {
        if (statusEl) this._setText(statusEl, "This decision was already judged.");
        disableButtons();
      } else {
        if (statusEl) this._setText(statusEl, "Decision not found.");
      }
    } catch (err) {
      if (statusEl) this._setText(statusEl, this._t("Error: {error}", { error: this._esc(err) }));
    }
  }

  // Pure DOM-render step, given an already-fetched entries array — no
  // network I/O. Called both synchronously from cache (_wire(), on
  // re-entering the System Log view) and from _fetchDebugLog()'s network
  // result, so a cached view renders immediately and a background refresh
  // reuses the exact same render path. Keeps the existing signature-based
  // skip (avoids flicker/scroll-jump on the shared 20s poll) — safe now
  // that the container is never left holding a stale "Loading…" shell by
  // the time this runs, cache-rendered or freshly fetched alike.
  _renderDebugLogEntries(entries) {
    const container = this.shadowRoot?.getElementById("newLogEntries");
    if (!container) return;
    if (!entries || !entries.length) {
      this._setHtml(container, `<div class="stub-body">No entries yet. Talk to Nova to generate log entries.</div>`);
      return;
    }
    const cc = NovaPanel.LOG_CATEGORIES;
    const activeFilter = this._logFilter || "all";
    const categoryFiltered = activeFilter === "all" ? entries : entries.filter(e => e.cat === activeFilter);
    const search = (this._logSearch || "").trim().toLowerCase();
    const filtered = search
      ? categoryFiltered.filter(e => (e.msg || "").toLowerCase().includes(search) || (e.cat || "").toLowerCase().includes(search))
      : categoryFiltered;

    const countEl = this.shadowRoot?.getElementById("newLogCount");
    if (countEl) {
      this._setText(countEl, search || activeFilter !== "all"
        ? this._t("{shown} of {total}", { shown: filtered.length, total: entries.length })
        : this._t("{count} entries", { count: entries.length }));
    }

    const ordered = filtered.slice().reverse();

    // Skip the rebuild when nothing changed (same signature trick as
    // Classic) — avoids flicker/scroll-jump on the shared 20s poll. The
    // signature is stamped on the CONTAINER ELEMENT itself (dataset), not
    // kept as component-instance state: _render() tears down and rebuilds
    // #newLogEntries from scratch on every tab/view switch, so a fresh
    // container's dataset is naturally unstamped and this always proceeds
    // to render — instance-level state would instead persist a stale
    // "already rendered" signature across the rebuild and skip the render
    // that was needed to replace the shell's "Loading…" placeholder (the
    // exact bug this replaced).
    const first = ordered[0];
    const last = ordered[ordered.length - 1];
    const sig = ordered.length + "|" + (first ? first.ts + first.msg : "") + "|" + (last ? last.ts + last.msg : "");
    const renderSig = sig + "\0" + activeFilter + "\0" + search;
    if (renderSig === container.dataset.renderSig) {
      return;
    }
    const filterChanged = activeFilter !== container.dataset.renderFilter || search !== container.dataset.renderSearch;
    const nearTop = container.scrollTop < 40;
    const prevTop = container.scrollTop;

    this._setHtml(container, ordered.length ? ordered.map(e => {
      const cat = cc[e.cat] || { color: "var(--ink-dim)", icon: "•" };
      const isError = e.cat === "ERROR" || (e.msg || "").toLowerCase().includes("error") || (e.msg || "").toLowerCase().includes("failed");
      const safeCat = this._esc(e.cat);
      return `<div class="new-log-entry${isError ? " new-log-entry-error" : ""}">
        <span class="new-log-ts">${this._esc(e.ts)}</span>
        <span class="new-log-cat" style="color:${cat.color}">${cat.icon} ${safeCat}</span>
        <span class="new-log-msg">${this._esc(e.msg)}</span>
      </div>`;
    }).join("") : `<div class="stub-body">No entries match${search ? ` "${this._esc(search)}"` : ""}${activeFilter !== "all" ? ` in ${activeFilter}` : ""}.</div>`);

    container.dataset.renderSig = renderSig;
    container.dataset.renderFilter = activeFilter;
    container.dataset.renderSearch = search;

    container.scrollTop = (filterChanged || nearTop) ? 0 : prevTop;
  }

  async _fetchDebugLog() {
    if (!this._hass) return;
    // Repeated navigation back into System Log must not pile up concurrent
    // duplicate requests — the in-flight one will render whatever it finds
    // in #newLogEntries when it resolves, same as any other stale-view
    // guard here (container lookup by id, below).
    if (this._debugLogFetchInFlight) return;
    this._debugLogFetchInFlight = true;
    try {
      const result = await this._hass.callWS({ type: "nova/get_debug_log" });
      const entries = result?.entries || [];
      this._debugLogEntries = entries;
      this._renderDebugLogEntries(entries);
    } catch (err) {
      // Fail-open: a background refresh failure must never discard rows
      // already on screen. Only show the error state when there's nothing
      // usable cached to fall back on (a genuine first-load failure).
      if (!this._debugLogEntries || !this._debugLogEntries.length) {
        const c = this.shadowRoot?.getElementById("newLogEntries");
        if (c) this._setHtml(c, `<div class="new-log-entry-error" style="padding:12px">${this._tHtml("Error loading logs: {error}", { error: this._esc(err) })}</div>`);
      } else {
        console.warn("Nova: System Log refresh failed, keeping cached entries", err);
      }
    } finally {
      this._debugLogFetchInFlight = false;
    }
  }

  _wireLogs() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-logview").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logView = btn.getAttribute("data-view");
        this._render();
      });
    });
    if ((this._logView || "system") === "decisions") {
      const unjudgedBtn = root.getElementById("newDecUnjudged");
      if (unjudgedBtn) {
        unjudgedBtn.addEventListener("click", () => {
          this._decisionsUnjudgedOnly = !this._decisionsUnjudgedOnly;
          this._render();
        });
      }
      const loadMoreBtn = root.getElementById("newDecLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchDecisions(false));
      return;
    }
    if ((this._logView || "system") === "spoken_history") return;
    if ((this._logView || "system") === "shadow") return;
    if ((this._logView || "system") === "actions") {
      const loadMoreBtn = root.getElementById("newActionLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchActions(false));
      return;
    }
    root.querySelectorAll(".new-log-filter").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logFilter = btn.getAttribute("data-filter");
        root.querySelectorAll(".new-log-filter").forEach(b => b.classList.toggle("mode-chip-on", b === btn));
        this._fetchDebugLog();
      });
    });
    const logSearch = root.getElementById("newLogSearch");
    if (logSearch) {
      logSearch.addEventListener("input", (e) => {
        clearTimeout(this._logSearchDebounce);
        const val = e.currentTarget.value;
        this._logSearchDebounce = setTimeout(() => {
          this._logSearch = val;
          this._fetchDebugLog();
        }, 200);
      });
    }
  }

  // ─── Memory ───────────────────────────────────────────────────────────
  // Four sections: what Nova knows (with the TEACH form), facts waiting for
  // a yes, relations between things, and habits tied to one person. Each
  // section redraws only its own box after a fetch or an action, so text
  // half typed into TEACH is never wiped by a full render.
  _htmlMemory() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">What Nova Knows</div>
            <div class="panel-meta" id="newMemCount">—</div>
          </div>
          <div class="stub-body">Durable facts &amp; preferences Nova recalls in conversation. Teach it something, or forget anything with ✕.</div>
          <div class="cfg-row cfg-row-wrap">
            <input id="newMemKey" class="cfg-field" style="flex:1" placeholder="what (e.g. trash day)" autocomplete="off">
            <input id="newMemVal" class="cfg-field" style="flex:1" placeholder="is (e.g. Tuesday)" autocomplete="off">
            <select id="newMemSubject" class="cfg-field">
              <option value="household">Household</option>
              <option value="primary">About me</option>
            </select>
            <button class="mode-chip" id="newMemAdd">TEACH</button>
          </div>
          <div id="newMemList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel" id="newPendingPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Pending Confirmation</div>
            <div class="panel-meta" id="newPendingCount">—</div>
          </div>
          <div class="stub-body">Nova proposed these while talking with you — from "remember that…" — but nobody confirmed them yet, so they aren't trusted or used in conversation until you approve, edit, or reject them here.</div>
          <div id="newPendingList" class="mem-body"></div>
        </div>

        <div class="panel" id="newRelationsPanel">
          <div class="panel-head">
            <div class="panel-title">Relations</div>
            <div class="panel-meta" id="newRelationsCount">—</div>
          </div>
          <div class="stub-body">Links between things, such as "house member owns bike" or "child's room adjacent_to hallway". Nova proposes them when you tell it how things relate. A new link waits here and is not used until you confirm it. Only confirmed links are shown to Nova. Removing one is permanent: Nova will not add it back on its own.</div>
          <div class="toggle-desc" id="newRelationsMsg"></div>
          <div id="newRelationsPending" class="mem-body"></div>
          <div class="mode-bind-head">Confirmed</div>
          <div id="newRelationsList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Person Routines</div>
            <div class="panel-meta">Learned</div>
          </div>
          <div class="stub-body">Habits Nova has confidently attributed to one person, from 30 days of sole-occupant activity — separate from household-wide facts above.</div>
          <div id="newProutineList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  // One call to Nova: { reply } on success, { failed } with the error text.
  async _memAsk(msg) {
    try {
      return { reply: await this._hass.callWS(msg) };
    } catch (err) {
      return { failed: String(err), err };
    }
  }

  // Stores a new fact list, keeping the waiting list and stats unless given.
  // The tab may act before its first load, when there is nothing to keep.
  _keepKnowledge(facts, pending) {
    const was = this._knowledge || {};
    this._knowledge = { facts, pending: pending ?? was.pending ?? [], stats: was.stats ?? {} };
  }

  // Calls fn(id, button) when a button carrying a numeric data-id is clicked.
  _memBind(box, selector, fn) {
    box.querySelectorAll(selector).forEach(btn => btn.addEventListener("click", () => {
      const id = parseInt(btn.getAttribute("data-id"), 10);
      if (!isNaN(id)) fn(id, btn);
    }));
  }

  async _fetchKnowledge() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/get_knowledge" });
    this._knowledge = failed
      ? { facts: [], pending: [], stats: {}, error: failed }
      : { facts: reply?.facts || [], pending: reply?.pending || [], stats: reply?.stats || {} };
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _fetchPersonRoutines() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/get_person_routines" });
    this._personRoutines = failed ? { groups: {}, error: failed } : { groups: reply?.routines || {} };
    this._personRoutinesLoaded = true;
    this._renderPersonRoutines();
  }

  _renderPersonRoutines() {
    const box = this.shadowRoot?.getElementById("newProutineList");
    if (!box) return;
    const state = this._personRoutines || {};
    if (state.error) {
      this._setHtml(box, `<div class="stub-body">${this._tHtml("Couldn't load routines — {error}", { error: this._esc(state.error) })}</div>`);
      return;
    }
    const groups = state.groups || {}, people = Object.keys(groups).sort();
    if (!people.length) {
      this._setHtml(box, this._personRoutinesLoaded
        ? `<div class="stub-body">Nothing person-specific learned yet — Nova needs a few weeks of sole-occupant data before routines are confidently individual.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    // "person_alex" reads "Person Alex". Capitalise first, then escape, so a
    // name holding & or quotes is not turned into a broken entity.
    const title = key => this._esc(key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()));
    const surest = (a, b) => (b.confidence || 0) - (a.confidence || 0);
    const habit = r => `
            <div class="cfg-row">
              <label>${this._esc(r.description)}</label>
              <span class="toggle-desc">${Math.round((r.confidence || 0) * 100)}% · ×${r.occurrences || "?"}</span>
            </div>`;
    this._setHtml(box, people
      .map(person => `<div class="mode-bind-head">${title(person)}</div>${groups[person].slice().sort(surest).map(habit).join("")}`)
      .join(""));
  }

  _renderKnowledgeList() {
    const root = this.shadowRoot;
    const box = root?.getElementById("newMemList");
    if (!box) return;
    const known = this._knowledge || {}, facts = known.facts || [];
    const count = root.getElementById("newMemCount");
    if (count) this._setText(count, this._t(facts.length === 1 ? "{count} fact" : "{count} facts", { count: facts.length }));
    if (known.error) {
      this._setHtml(box, `<div class="stub-body">${this._tHtml("Couldn't load memory — {error}", { error: this._esc(known.error) })}</div>`);
      return;
    }
    if (!facts.length) {
      this._setHtml(box, this._knowledgeLoaded
        ? `<div class="stub-body">Nothing yet. Say "remember that…" to Nova, or teach it above.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    // Household first, then the other subjects by name.
    const bySubject = {};
    for (const f of facts) (bySubject[f.subject] = bySubject[f.subject] || []).push(f);
    const subjectNames = { household: "Household", primary: "About me" };
    const rank = s => (s === "household" ? 0 : 1);
    const subjects = Object.keys(bySubject).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    // A fact not stated outright, or held at under 90%, carries a "~" saying how sure Nova is.
    const factRow = f => {
      const sure = f.confidence ?? 1;
      const hedge = f.source !== "stated" || sure < 0.9
        ? `<span title="${this._tHtml("{source} · {percent}% sure", { source: this._esc(this._tx(f.source)), percent: Math.round(sure * 100) })}">~</span>`
        : "";
      const expiry = f.expires_at ? `<span title="expires">⌛</span>` : "";
      return `
          <div class="cfg-row" data-id="${f.id}">
            <label>${this._esc(f.key)}</label>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="toggle-desc">${this._esc(f.value)}${hedge}${expiry}</span>
              <button class="new-mem-forget" data-id="${f.id}" title="Forget this" aria-label="Forget">✕</button>
            </div>
          </div>`;
    };
    this._setHtml(box, subjects
      .map(subj => `<div class="mode-bind-head">${subjectNames[subj] || this._esc(subj.replace(/_/g, " "))}</div>${bySubject[subj].map(factRow).join("")}`)
      .join(""));
    this._memBind(box, ".new-mem-forget", id => this._forgetKnowledge(id));
  }

  async _teachKnowledge() {
    const root = this.shadowRoot;
    if (!root || !this._hass) return;
    const [keyEl, valEl, subjEl] = ["newMemKey", "newMemVal", "newMemSubject"].map(id => root.getElementById(id));
    const key = (keyEl?.value || "").trim(), value = (valEl?.value || "").trim();
    const subject = subjEl?.value || "household";
    if (!key || !value) return;
    const kind = subject === "primary" ? "preference" : "fact";
    const { reply, failed, err } = await this._memAsk({ type: "nova/add_knowledge", key, value, subject, kind });
    if (failed) {
      console.error("Nova: teach failed", err);
    } else {
      this._keepKnowledge(reply?.facts || []);
      [keyEl, valEl].forEach(el => { if (el) el.value = ""; });
      if (keyEl) keyEl.focus();
    }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
  }

  async _forgetKnowledge(id) {
    if (!this._hass) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/forget_knowledge", fact_id: id });
    if (failed) console.error("Nova: forget failed", err);
    else this._keepKnowledge(reply?.facts || []);
    this._renderKnowledgeList();
  }

  async _pendingFactAction(id, action) {
    if (!this._hass) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/pending_fact_action", fact_id: id, action });
    if (failed) console.error(`Nova: ${action} failed`, err);
    else this._keepKnowledge(reply?.facts || this._knowledge?.facts || [], reply?.pending || []);
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _editPendingFact(id, value) {
    if (!this._hass || !value) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/edit_pending_fact", fact_id: id, value });
    if (failed) console.error("Nova: edit pending fact failed", err);
    else this._keepKnowledge(this._knowledge?.facts || [], reply?.pending || []);
    this._renderPendingFacts();
  }

  _renderPendingFacts() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("newPendingPanel"), box = root?.getElementById("newPendingList");
    if (!panel || !box) return;
    const waiting = this._knowledge?.pending || [];
    panel.hidden = !waiting.length;
    if (!waiting.length) { this._setHtml(box, ""); return; }
    const countEl = root.getElementById("newPendingCount");
    if (countEl) this._setText(countEl, this._t("{count} waiting", { count: waiting.length }));
    this._setHtml(box, waiting.map(f => `
      <div class="cfg-row" data-id="${f.id}">
        <label>${this._esc(f.key)}</label>
        <input class="cfg-field new-pending-edit-val" style="flex:1" data-id="${f.id}" value="${this._esc(f.value)}">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip new-pending-confirm" data-id="${f.id}">✓ Confirm</button>
        <button class="mode-chip new-pending-reject" data-id="${f.id}">✕ Reject</button>
        <button class="mode-chip new-pending-save-edit" data-id="${f.id}">💾 Save edit</button>
      </div>`).join(""));
    this._memBind(box, ".new-pending-confirm", id => this._pendingFactAction(id, "confirm"));
    this._memBind(box, ".new-pending-reject", id => this._pendingFactAction(id, "reject"));
    this._memBind(box, ".new-pending-save-edit", id => {
      const typed = (box.querySelector(`.new-pending-edit-val[data-id="${id}"]`)?.value || "").trim();
      if (typed) this._editPendingFact(id, typed);
    });
  }

  async _fetchRelations() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/list_relations" });
    this._relations = failed
      ? { pending: [], confirmed: [], cap: 500, error: failed }
      : { pending: reply?.pending || [], confirmed: reply?.confirmed || [], cap: reply?.cap || 500 };
    this._relationsLoaded = true;
    this._renderRelations();
  }

  _relationErrorText(code) {
    return ({
      invalid_subject: "The first name must be 1 to 80 characters and cannot be 'unknown'.",
      invalid_object: "The second name must be 1 to 80 characters and cannot be 'unknown'.",
      invalid_predicate: "The link word must be lowercase with underscores, 2 to 40 characters, such as owns or adjacent_to.",
      self_relation: "A thing cannot be linked to itself.",
      duplicate: "That link already exists.",
      not_found: "That link is no longer waiting for confirmation.",
    })[code] || "Could not save that change.";
  }

  _renderRelations() {
    const root = this.shadowRoot;
    const pendingBox = root?.getElementById("newRelationsPending");
    const list = root?.getElementById("newRelationsList");
    if (!pendingBox || !list) return;
    const rel = this._relations || { pending: [], confirmed: [], cap: 500 };
    const countEl = root.getElementById("newRelationsCount");
    if (countEl) this._setText(countEl, this._t("{confirmed} confirmed · {waiting} waiting", { confirmed: rel.confirmed.length, waiting: rel.pending.length }));
    if (rel.error) {
      this._setHtml(pendingBox, "");
      this._setHtml(list, `<div class="stub-body">${this._tHtml("Couldn't load relations — {error}", { error: this._esc(rel.error) })}</div>`);
      return;
    }
    this._setHtml(pendingBox, rel.pending.length ? `
      <div class="mode-bind-head">Waiting for confirmation</div>` + rel.pending.map(r => `
      <div class="cfg-row cfg-row-wrap rel-row" data-id="${r.id}">
        <input class="cfg-field rel-subject" style="flex:1" maxlength="80" value="${this._esc(r.subject)}" aria-label="First thing">
        <input class="cfg-field rel-predicate" style="flex:1" maxlength="40" value="${this._esc(r.predicate)}" aria-label="Link">
        <input class="cfg-field rel-object" style="flex:1" maxlength="80" value="${this._esc(r.object)}" aria-label="Second thing">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip rel-confirm" data-id="${r.id}">✓ Confirm</button>
        <button class="mode-chip rel-save" data-id="${r.id}">💾 Save edit</button>
        <button class="mode-chip rel-reject" data-id="${r.id}">✕ Reject</button>
      </div>`).join("") : "");
    this._setHtml(list, rel.confirmed.length ? rel.confirmed.map(r => `
      <div class="cfg-row" data-id="${r.id}">
        <label>${this._esc(r.subject)} <b>${this._esc(r.predicate)}</b> ${this._esc(r.object)}</label>
        <button class="new-rel-remove" data-id="${r.id}" title="Remove this relation" aria-label="Remove">✕ Remove</button>
      </div>`).join("")
      : (this._relationsLoaded
        ? `<div class="stub-body">None yet. Tell Nova how things relate, for example "House member owns the bike", then confirm it here. Only you can confirm a link, Nova cannot.</div>`
        : `<div class="stub-body">Loading…</div>`));
    const idOf = (el) => parseInt(el.getAttribute("data-id"), 10);
    pendingBox.querySelectorAll(".rel-confirm").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "confirm")));
    pendingBox.querySelectorAll(".rel-reject").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "reject")));
    pendingBox.querySelectorAll(".rel-save").forEach(b => b.addEventListener("click", e => {
      const id = idOf(e.currentTarget);
      const row = pendingBox.querySelector(`.rel-row[data-id="${id}"]`);
      if (!isNaN(id) && row) this._editRelation(id, {
        subject: row.querySelector(".rel-subject").value,
        predicate: row.querySelector(".rel-predicate").value,
        object: row.querySelector(".rel-object").value,
      });
    }));
    list.querySelectorAll(".new-rel-remove").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "remove")));
  }

  async _relationAction(id, action) {
    if (!this._hass || isNaN(id)) return;
    const msg = this.shadowRoot?.getElementById("newRelationsMsg");
    try {
      const res = await this._hass.callWS({ type: "nova/relation_action", relation_id: id, action });
      this._relations = { pending: res?.pending || [], confirmed: res?.confirmed || [], cap: res?.cap || 500 };
      if (msg) this._setText(msg, res?.ok ? "" : "That link has already changed.");
    } catch (err) {
      if (msg) this._setText(msg, "Could not change that link (administrator only).");
      return;
    }
    this._renderRelations();
  }

  async _editRelation(id, fields) {
    if (!this._hass || isNaN(id)) return;
    const msg = this.shadowRoot?.getElementById("newRelationsMsg");
    try {
      const res = await this._hass.callWS({ type: "nova/edit_relation", relation_id: id, ...fields });
      if (!res?.ok) {
        // Keep what the person typed so they can fix it.
        if (msg) this._setText(msg, this._relationErrorText(res?.error));
        return;
      }
      this._relations = { pending: res?.pending || [], confirmed: res?.confirmed || [], cap: res?.cap || 500 };
      if (msg) this._setText(msg, "Saved.");
    } catch (err) {
      if (msg) this._setText(msg, "Could not save that change (administrator only).");
      return;
    }
    this._renderRelations();
  }

  _wireMemory() {
    const root = this.shadowRoot;
    const memAdd = root.getElementById("newMemAdd");
    if (memAdd) {
      memAdd.addEventListener("click", () => this._teachKnowledge());
      ["newMemKey", "newMemVal"].forEach(id => {
        const el = root.getElementById(id);
        if (el) el.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); this._teachKnowledge(); }
        });
      });
    }
    this._renderKnowledgeList();
    this._renderPendingFacts();
    this._renderPersonRoutines();
    this._fetchRelations();
  }

  // ─── Intrusion ────────────────────────────────────────────────────────
  // Safety relevant: call off and acknowledge change real escalation, so
  // this tab only shows Nova's state and sends its exact websocket actions.
  // The timeout select and vision toggle save through _saveSetting like
  // the controls on the Settings tab.
  _htmlIntrusion() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion</div>
            <div class="panel-meta" id="newIntrStatus">—</div>
          </div>
          <div class="stub-body">Last intrusion snapshot and false-alarm call-off. When Nova confirms an intruder on camera it grabs a still; if it's not real, call it off here or say "it's a false alarm".</div>
          <div id="newIntrBody"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion Log</div>
            <div class="panel-meta" id="newIlogLearn">—</div>
          </div>
          <div class="stub-body">Every intrusion event with its snapshot. Mark each one <b>real</b> or <b>false alarm</b> — Nova learns from your labels and stops firing the low-confidence alerts for patterns you keep calling false. A confirmed intrusion always alerts, no matter what it has learned.</div>
          <div class="cfg-row"><button class="mode-chip" id="newIlogRefresh">⟳ REFRESH</button></div>
          <div id="newIlogBody"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  // Sends one nova/intrusion action and returns the reply.
  _intrSend(action, extra) {
    return this._hass.callWS(Object.assign({ type: "nova/intrusion", action }, extra));
  }

  async _fetchIntrusion() {
    if (!this._hass) return;
    const timeout = this._liveData?.config?.intrusion_response_timeout;
    if (timeout != null) this._intrTimeout = timeout;
    let status;
    try { status = await this._intrSend("status"); } catch (_) { status = { error: true }; }
    this._intr = status;
    this._renderIntrusionStatus();
  }

  _renderIntrusionStatus() {
    const root = this.shadowRoot, body = root?.getElementById("newIntrBody");
    if (!body) return;
    const badge = root.getElementById("newIntrStatus"), s = this._intr || {};
    if (s.error) {
      this._setHtml(body, `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`);
      if (badge) this._setText(badge, "—");
      return;
    }
    if (badge) this._setHtml(badge, s.called_off
      ? `<span class="diag-warn">${this._tHtml("CALLED OFF · {seconds}s", { seconds: s.suppressed_for })}</span>`
      : `<span class="diag-ok">ARMED</span>`);

    const parts = [];
    const shot = s.last_snapshot;
    if (shot && shot.image_b64) {
      const camName = (shot.camera || "").replace("camera.", "").replace(/_/g, " ");
      const taken = shot.ts ? new Date(shot.ts * 1000).toLocaleString() : "";
      parts.push(`<div class="intr-snap">
        <img src="data:image/jpeg;base64,${shot.image_b64}" alt="intrusion snapshot" class="intr-img">
        <div class="toggle-desc">${this._esc(camName)} · ${this._esc(taken)}</div>
      </div>`);
    } else {
      parts.push(`<div class="stub-body">No intrusion snapshots captured. This stays empty unless Nova confirms an intruder on camera.</div>`);
    }
    const falseAlarms = s.false_alarms_24h;
    if (falseAlarms) {
      const line = falseAlarms === 1 ? "{count} false alarm called off in the last 24h" : "{count} false alarms called off in the last 24h";
      parts.push(`<div class="stub-body">${this._tHtml(line, { count: falseAlarms })}</div>`);
    }
    if (s.acknowledged) parts.push(`<div class="stub-body">✓ Acknowledged — automatic escalation held (you're handling it)</div>`);
    const waits = [["60", "1 min"], ["120", "2 min"], ["180", "3 min"], ["300", "5 min"], ["600", "10 min"]];
    const vision = this._liveData?.config?.intrusion_vision_confirm !== false;
    parts.push(`
      <div class="cfg-row">
        <label>Auto-escalate if no response after</label>
        <select class="cfg-field" data-cfg-key="intrusion_response_timeout">${this._optSelect(waits, String(this._intrTimeout || 120))}</select>
      </div>
      <div class="cfg-row">
        <label>Confirm Frigate person with Nova vision before alarming</label>
        <button class="toggle-btn ${vision ? "on" : "off"}" data-cfg-key="intrusion_vision_confirm" data-cfg-val="${vision ? "false" : "true"}">${vision ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        <button class="mode-chip new-intr-ack">✓ I'M LOOKING (HOLD)</button>
        <button class="mode-chip new-intr-dismiss">✕ CALL OFF (FALSE ALARM)</button>
      </div>`);
    this._setHtml(body, parts.join(""));

    body.querySelectorAll(".toggle-btn[data-cfg-key], select.cfg-field[data-cfg-key]").forEach(el => {
      const save = value => this._saveSetting(el.getAttribute("data-cfg-key"), value);
      if (el.tagName === "BUTTON") el.addEventListener("click", () => save(el.getAttribute("data-cfg-val") === "true"));
      else el.addEventListener("change", () => save(el.value));
    });
    // Call off and acknowledge: the button stays disabled until the fresh
    // status redraws it, and comes back only when the action failed.
    const action = (selector, name) => {
      const btn = body.querySelector(selector);
      if (!btn) return;
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        btn.disabled = true;
        try {
          await this._intrSend(name, { reason: "panel" });
          await this._fetchIntrusion();
        } catch (err) {
          console.error(`Nova: intrusion ${name} failed`, err);
          btn.disabled = false;
        }
      });
    };
    action(".new-intr-dismiss", "dismiss");
    action(".new-intr-ack", "acknowledge");
  }

  async _fetchIntrusionLog() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIlogBody"), learned = root?.getElementById("newIlogLearn");
    if (!this._hass || !body) return;
    try {
      const log = await this._intrSend("log", { limit: 40 });
      this._ilog = log;
      const tally = log?.learning || {};
      if (learned) this._setText(learned, this._t("{labelled}/{events} labelled", { labelled: tally.labeled || 0, events: tally.events || 0 }));
      this._setHtml(body, this._renderIntrusionLogHtml(log));
      this._wireIntrusionLabels();
    } catch (_) {
      this._setHtml(body, `<div class="stub-body">Could not load the log.</div>`);
    }
  }

  _renderIntrusionLogHtml(res) {
    const events = (res && res.events) || [];
    if (!events.length) return `<div class="stub-body">No intrusion events recorded yet.</div>`;
    const quiet = ((res.learning || {}).damped_patterns || []).length;
    const head = quiet
      ? `<div class="stub-body">${this._tHtml(quiet === 1 ? "Nova has learned {count} benign pattern — low-confidence alerts for these stay quiet." : "Nova has learned {count} benign patterns — low-confidence alerts for these stay quiet.", { count: quiet })}</div>`
      : "";
    const tone = { confirmed: "diag-down", unresolved: "diag-warn", investigating: "diag-off" };
    const row = e => {
      const label = e.label || "", on = v => (label === v ? " mode-chip-on" : "");
      return `<div class="new-ilog-item" data-ev="${this._esc(e.id)}">
        <div class="cfg-row">
          <span class="${tone[e.kind] || "diag-off"}">${this._esc((e.kind || "").toUpperCase())}</span>
          <span class="toggle-desc">${this._esc(new Date((e.ts || 0) * 1000).toLocaleString())}</span>
        </div>
        <div class="toggle-desc">${this._esc(e.breach || e.camera || "activity")}${e.reason ? " — " + this._esc(e.reason) : ""}</div>
        ${e.image_b64 ? `<img class="intr-img" src="data:image/jpeg;base64,${e.image_b64}" alt="snapshot">` : ""}
        <div class="mode-grid">
          <button class="mode-chip new-ilog-btn${on("real")}" data-label="real">REAL</button>
          <button class="mode-chip new-ilog-btn${on("false")}" data-label="false">FALSE ALARM</button>
          ${label ? `<button class="mode-chip new-ilog-btn" data-label="">CLEAR</button>` : ""}
        </div>
      </div>`;
    };
    return head + events.map(row).join("");
  }

  _wireIntrusionLabels() {
    const body = this.shadowRoot?.getElementById("newIlogBody");
    if (!body) return;
    body.querySelectorAll(".new-ilog-btn").forEach(btn => btn.addEventListener("click", async () => {
      const eventId = btn.closest(".new-ilog-item")?.getAttribute("data-ev");
      if (!eventId || !this._hass) return;
      try {
        await this._intrSend("label", { event_id: eventId, label: btn.getAttribute("data-label") });
        await this._fetchIntrusionLog();
      } catch (err) { console.error("Nova: intrusion label failed", err); }
    }));
  }

  _wireIntrusion() {
    this._fetchIntrusion();
    const refresh = this.shadowRoot.getElementById("newIlogRefresh");
    if (!refresh) { this._fetchIntrusionLog(); return; }
    refresh.addEventListener("click", async () => {
      const label = refresh.textContent;
      refresh.disabled = true;
      this._setText(refresh, "⟳ LOADING…");
      try { await this._fetchIntrusionLog(); } finally {
        refresh.disabled = false;
        this._setText(refresh, label);
      }
    });
  }
  // ─── Chat ─────────────────────────────────────────────────────────────
  // Type to Nova from the panel. Each message goes through nova/chat, open to
  // any Home Assistant user, to Nova's own conversation agent. The server keeps
  // one thread per user, so the panel sends no conversation id. Unlocking,
  // opening and disarming need a tap on the user's phone. NEW CHAT calls
  // nova/chat_new, which clears the thread on the server. The messages shown
  // here live in the panel only and are gone when it is reloaded.
  _htmlChat() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Chat</div>
            <div class="panel-meta"><button class="mode-chip" id="chatNew">NEW CHAT</button></div>
          </div>
          <div class="chat-log" id="chatLog"></div>
          <div class="chat-compose">
            <input class="cfg-field" id="chatInput" maxlength="1000" placeholder="Ask Nova something">
            <button class="mode-chip" id="chatSend">SEND</button>
          </div>
          <div class="toggle-desc" id="chatMsg"></div>
        </div>
    `;
  }

  _chatState() {
    if (!this._chat) this._chat = { turns: [], busy: false };
    return this._chat;
  }

  _renderChat() {
    const log = this.shadowRoot?.getElementById("chatLog");
    if (!log) return;
    const c = this._chatState();
    if (!c.turns.length) {
      this._setHtml(log, `<div class="stub-body">Ask about your home or tell Nova what to do. Unlocking, opening and disarming need a tap on your phone.</div>`);
      return;
    }
    this._setHtml(log, c.turns.map(t => `
        <div class="chat-turn ${t.role}${t.error ? " chat-error" : ""}">
          <span class="chat-who">${t.role === "user" ? this._t("You") : this._t("Nova")}</span>
          <span class="chat-text">${this._esc(t.text)}</span>
        </div>`).join("") + (c.busy ? `<div class="stub-body">${this._t("Nova is thinking…")}</div>` : ""));
    log.scrollTop = log.scrollHeight;
  }

  async _chatSend() {
    const root = this.shadowRoot;
    const input = root?.getElementById("chatInput");
    const msg = root?.getElementById("chatMsg");
    const c = this._chatState();
    const text = String(input?.value || "").trim();
    if (!this._hass || !text || c.busy) return;
    input.value = "";
    if (msg) this._setText(msg, "");
    c.turns.push({ role: "user", text });
    c.busy = true;
    this._renderChat();
    try {
      const res = await this._hass.callWS({ type: "nova/chat", text });
      if (res && res.ok) {
        c.turns.push({ role: "nova", text: res.reply });
      } else if (res && res.code === "rate_limited") {
        c.turns.push({ role: "nova", text: this._t("You're sending too fast, wait a moment"), error: true });
      } else {
        c.turns.push({ role: "nova", text: (res && res.error) || this._t("Could not reach Nova."), error: true });
      }
    } catch (err) {
      c.turns.push({ role: "nova", error: true, text: this._t("Could not reach Nova.") });
    }
    c.busy = false;
    this._renderChat();
  }

  _wireChat() {
    const root = this.shadowRoot;
    root.getElementById("chatSend")?.addEventListener("click", () => this._chatSend());
    root.getElementById("chatInput")?.addEventListener("keydown", e => {
      if (e.key === "Enter") this._chatSend();
    });
    root.getElementById("chatNew")?.addEventListener("click", async () => {
      this._chat = { turns: [], busy: false };
      this._renderChat();
      try {
        await this._hass.callWS({ type: "nova/chat_new" });
      } catch (err) {
        console.error("Nova: chat reset failed", err);
      }
    });
    this._renderChat();
    root.getElementById("chatInput")?.focus();
  }

  // ─── Faces ────────────────────────────────────────────────────────────
  // Recent faces Frigate or Double Take named, and the household resident
  // roster. Nova has no face engine of its own, and no image is shown or
  // stored here. The commands are admin only (nova/list_faces,
  // nova/add_resident, nova/remove_resident).
  _htmlFaces() {
    const on = this._liveData?.config?.face_stand_down === true;
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Faces</div>
            <div class="panel-meta" id="facesMeta">—</div>
          </div>
          <div class="stub-body">Nova does not run its own face engine. It reads names from Frigate or Double Take. No images are shown or stored here. The residents below are the people Nova treats as members of the household.</div>
          <div class="cfg-row"><button class="mode-chip" id="facesRefresh">⟳ REFRESH</button></div>
          <div id="facesBody"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Residents</div>
            <div class="panel-meta" id="facesResidentsMeta">—</div>
          </div>
          <div class="stub-body">Use the name exactly as Frigate or Double Take reports it. Case and spacing do not matter. Intrusion stand down is <b>${on ? "ON" : "OFF"}</b>${on ? "; change it under Settings → Security alarm." : " (the default); change it under Settings → Security alarm."}</div>
          <div class="cfg-row">
            <input class="cfg-field" id="facesAddName" maxlength="60" placeholder="Resident name">
            <button class="mode-chip" id="facesAdd">ADD RESIDENT</button>
          </div>
          <div class="toggle-desc" id="facesMsg"></div>
          <div id="facesResidents"></div>
        </div>
    `;
  }

  _facesAgeText(sec) {
    const s = Number(sec) || 0;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  async _fetchFaces() {
    if (!this._hass) return;
    try {
      this._faces = await this._hass.callWS({ type: "nova/list_faces", limit: 40 });
    } catch (err) {
      this._faces = { error: true, code: err && err.code };
    }
    this._renderFacesBody();
  }

  _renderFacesBody() {
    const root = this.shadowRoot;
    const body = root?.getElementById("facesBody");
    const resBox = root?.getElementById("facesResidents");
    if (!body || !resBox) return;
    const f = this._faces || {};
    const meta = root.getElementById("facesMeta");
    const resMeta = root.getElementById("facesResidentsMeta");
    if (f.error) {
      const denied = f.code === "unauthorized";
      this._setHtml(body, `<div class="stub-body">${denied ? "Faces needs a Home Assistant administrator." : "Could not load faces."}</div>`);
      this._setHtml(resBox, "");
      if (meta) this._setText(meta, "—");
      return;
    }
    const faces = f.faces || [];
    const residents = f.residents || [];
    const src = f.sources || {};
    if (meta) this._setText(meta, this._t("{count} RECENT", { count: faces.length }));
    if (resMeta) this._setText(resMeta, this._t(residents.length === 1 ? "{count} RESIDENT" : "{count} RESIDENTS", { count: residents.length }));
    if (!faces.length) {
      this._setHtml(body, src.configured === false
        ? `<div class="stub-body">No face recognition source found. Set up Frigate face recognition or Double Take, and make sure Home Assistant has MQTT. Recent faces appear here once one of them names someone.</div>`
        : `<div class="stub-body">No faces seen recently. Names appear here when Frigate or Double Take recognises someone.</div>`);
    } else {
      this._setHtml(body, faces.map(r => `
        <div class="feed-row face-row">
          <span class="feed-text"><b>${this._esc(r.name)}</b>
            <span class="${r.known ? "diag-ok" : "diag-warn"}">${r.known ? "KNOWN" : "UNKNOWN"}</span>
            ${r.resident ? `<span class="diag-ok">RESIDENT</span>` : ""}
            <span class="dim">· ${this._esc(r.camera)} · ${Math.round(r.confidence)}% · ${this._esc(this._facesAgeText(r.age_seconds))}</span>
          </span>
          ${r.known ? (r.resident
            ? `<button class="mode-chip" data-face-remove="${this._esc(r.name)}">REMOVE RESIDENT</button>`
            : `<button class="mode-chip" data-face-add="${this._esc(r.name)}">ADD RESIDENT</button>`) : ""}
        </div>`).join(""));
    }
    this._setHtml(resBox, residents.length ? residents.map(n => `
        <div class="feed-row resident-row">
          <span class="feed-text"><b>${this._esc(n)}</b></span>
          <button class="mode-chip" data-face-remove="${this._esc(n)}">REMOVE</button>
        </div>`).join("")
      : `<div class="stub-body">No residents yet. Add the people who live here.</div>`);
    root.querySelectorAll("[data-face-add]").forEach(b => {
      b.addEventListener("click", () => this._faceAdd(b.getAttribute("data-face-add")));
    });
    root.querySelectorAll("[data-face-remove]").forEach(b => {
      b.addEventListener("click", () => this._faceRemove(b.getAttribute("data-face-remove")));
    });
  }

  async _faceAdd(name) {
    const root = this.shadowRoot;
    const msg = root?.getElementById("facesMsg");
    if (!this._hass || !String(name || "").trim()) return;
    try {
      const res = await this._hass.callWS({ type: "nova/add_resident", name });
      if (msg) this._setText(msg, this._t(res.added ? "{name} added." : "{name} is already a resident.", { name }));
      const input = root?.getElementById("facesAddName");
      if (input) input.value = "";
    } catch (err) {
      if (msg) this._setText(msg, (err && err.message) || "Could not add that name.");
      return;
    }
    await this._fetchFaces();
  }

  async _faceRemove(name) {
    const msg = this.shadowRoot?.getElementById("facesMsg");
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/remove_resident", name });
      if (msg) this._setText(msg, this._t("{name} removed.", { name }));
    } catch (err) {
      if (msg) this._setText(msg, (err && err.message) || "Could not remove that name.");
      return;
    }
    await this._fetchFaces();
  }

  _wireFaces() {
    const root = this.shadowRoot;
    root.getElementById("facesRefresh")?.addEventListener("click", () => this._fetchFaces());
    root.getElementById("facesAdd")?.addEventListener("click", () => {
      this._faceAdd(root.getElementById("facesAddName")?.value || "");
    });
    root.getElementById("facesAddName")?.addEventListener("keydown", e => {
      if (e.key === "Enter") this._faceAdd(e.target.value || "");
    });
    if (this._faces) this._renderFacesBody();
    this._fetchFaces();
  }

  // ─── Energy ───────────────────────────────────────────────────────────
  // Energy Management, Solar and Appliances on one tab. Energy status is
  // fetched on every entry to the tab, after set_agency and on Refresh. It
  // is not polled. A fetch redraws only #energyStatusBody, never the whole
  // tab: a full _render() would wire the tab again, fetch again and loop,
  // and it would wipe an appliance row that has not been saved yet. Solar
  // comes from this._solar, which _fetchLiveData already refreshes.
  //
  // The Live panel polls nova/energy_flow every 5 seconds, only while this
  // tab is open and the page is visible. Its flow diagram and the four tiles
  // under it (the diagram's readable text version) are built once here and
  // only ever updated in place. The Outlook panel rides the same timer: it
  // fetches nova/energy_outlook on entry and then at most every 5 minutes.
  _htmlEnergy() {
    const cfg = this._data()?.config || {};
    const e = this._energy || {};
    const tile = (kind, label) => `
            <div class="energy-tile" data-flow="${kind}">
              <dt>${label}</dt>
              <dd class="energy-tile-w">—</dd>
              <dd class="energy-tile-state"></dd>
            </div>`;
    return `
        <div class="panel" id="energyLivePanel">
          <div class="panel-head">
            <div class="panel-title">Live</div>
            <div class="panel-meta" id="solarSufficiency">—</div>
          </div>
          <div class="stub-body" id="energyLiveMsg" hidden></div>
          <div class="energy-live-grid">
            <div class="energy-live-main">
              <div class="energy-flow-wrap" id="energyDiagram">${this._energyFlowSvg()}</div>
            </div>
            <div class="energy-live-side">
              <div class="toggle-desc energy-flow-summary" id="solarSummary" hidden></div>
              <dl class="energy-live" id="energyLive">${tile("solar", "Solar")}${tile("house", "House")}${tile("battery", "Battery")}${tile("grid", "Grid")}
              </dl>
            </div>
          </div>
          <div class="sr-only" id="energyLiveAnnounce" role="status" aria-live="polite"></div>
        </div>

        <div class="panel" id="energyOutlookPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Outlook</div>
            <div class="panel-meta" id="outlookUpdated"></div>
          </div>
          <div class="stub-body" id="outlookMsg" hidden></div>
          <div class="outlook-strip-wrap" id="outlookStripWrap" hidden>
            <div class="outlook-key" aria-hidden="true"><span class="key-sun">Sun</span><span class="key-soc">Battery</span><span class="key-price">Price</span></div>
            <div class="outlook-chart">
              <svg class="outlook-strip" id="outlookSvg" viewBox="0 0 360 100" preserveAspectRatio="none" role="img" aria-labelledby="outlookTitle outlookDesc">
                <title id="outlookTitle">Energy outlook for the next 36 hours</title>
                <desc id="outlookDesc">No outlook yet.</desc>
                <g class="outlook-bands"></g>
                <path class="outlook-solar" d=""/>
                <polyline class="outlook-soc" points=""/>
                <line class="outlook-now" x1="0" x2="0" y1="0" y2="100"/>
              </svg>
              <div class="outlook-band-labels" aria-hidden="true"></div>
            </div>
            <div class="outlook-ticks" aria-hidden="true"></div>
            <dl class="outlook-rates" id="outlookRates"></dl>
          </div>
          <ul class="outlook-advice" id="outlookAdvice" hidden></ul>
          <div class="outlook-line" id="outlookWindow" hidden></div>
          <div class="outlook-learned" id="outlookLearned" hidden></div>
        </div>

        <div class="panel" id="energyTodayPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Today</div>
          </div>
          <div class="stub-body" id="energyTodayMsg" hidden></div>
          <dl class="energy-live energy-today" id="energyToday">${[
            ["solar_kwh", "Solar made"], ["home_kwh", "Home used"],
            ["grid_import_kwh", "Bought from grid"], ["grid_export_kwh", "Sold to grid"],
            ["battery_charged_kwh", "Battery charged"], ["battery_discharged_kwh", "Battery discharged"],
            ["self_sufficiency_pct", "Self sufficiency today"],
          ].map(([key, label]) => `
            <div class="energy-tile" data-today="${key}">
              <dt>${label}</dt>
              <dd class="energy-tile-w">—</dd>
            </div>`).join("")}
          </dl>
        </div>

        <div class="panel" id="energyBatteryPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Battery</div>
          </div>
          <div class="energy-battery">
            <svg class="battery-tank" id="batteryTank" viewBox="0 0 80 140" role="img" aria-label="Battery: no reading.">
              <rect class="tank-outline" x="4" y="12" width="72" height="124" rx="12"/>
              <rect class="tank-cap" x="28" y="3" width="24" height="9" rx="3"/>
              <rect class="tank-fill" x="10" y="18" width="60" height="112" rx="7" style="--tank-pct:0"/>
            </svg>
            <div class="battery-copy">
              <div class="battery-pct" id="batteryPct">—</div>
              <div class="battery-state" id="batteryState"></div>
              <div class="battery-line" id="batteryStored" hidden></div>
              <div class="battery-line" id="batteryEta" hidden></div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Energy Management</div>
          </div>
          <div class="stub-body">Whole-home power, peak awareness, and load advice. Pick how much Nova may act — it never sheds critical loads (fridge, medical, network).</div>
          <div class="cfg-row"><button class="mode-chip" id="energyRefresh">⟳ REFRESH</button></div>
          <div id="energyStatusBody"><div class="stub-body">Loading…</div></div>
          <div class="cfg-row">
            <label>Peak threshold (kW)</label>
            <input class="cfg-field cfg-num" type="number" id="energyPeakKw" min="0.1" step="0.1" value="${this._esc(this._energyPeakKw(e))}">
          </div>
          <div class="stub-body">Daily solar report cost (optional): if you already track exact electricity cost, point Nova at your own sensor instead of its price × kWh estimate.</div>
          <div class="cfg-row">
            <label>Cost today entity</label>
            <input class="cfg-field" type="text" data-cfg-key="energy_cost_today_entity" value="${this._esc(cfg.energy_cost_today_entity || "")}" placeholder="sensor.electricity_cost_today">
          </div>
          <div class="cfg-row">
            <label>Net cost today entity (optional)</label>
            <input class="cfg-field" type="text" data-cfg-key="energy_cost_net_entity" value="${this._esc(cfg.energy_cost_net_entity || "")}" placeholder="sensor.net_electricity_cost_today">
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Appliances</div>
          </div>
          ${this._appliancesCardBody()}
        </div>
    `;
  }

  // The peak threshold lives in nova/energy's status (peak_watts), not in
  // the panel config, so the field shows it in kW from the last fetch.
  _energyPeakKw(e) {
    const w = Number(e?.peak_watts);
    return Number.isFinite(w) && w > 0 ? String(w / 1000) : "";
  }

  async _fetchEnergyStatus() {
    if (!this._hass) return;
    try {
      this._energy = await this._hass.callWS({ type: "nova/energy", action: "status" });
    } catch (_) { this._energy = { error: true }; }
    this._renderEnergyStatus();
  }

  _energyStatusHtml() {
    const e = this._energy || {};
    if (e.error) {
      return `<div class="stub-body">Couldn't load energy data — restart Home Assistant after updating.</div>`;
    }
    const draw = e.kw == null
      ? `<span class="diag-off">NO METER</span>`
      : `<span class="${e.over_peak ? "diag-warn" : "diag-ok"}">${e.over_peak ? this._tHtml("{kw} kW · OVER PEAK", { kw: e.kw }) : `${e.kw} kW`}</span>`;
    // Fixed labels, so each one is a whole string the translations can match.
    const agencyLabels = { advisory: "advisory", opt_in: "opt-in", autonomous: "autonomous" };
    const agencyChips = Object.keys(agencyLabels).map(a =>
      `<button class="mode-chip ${a === e.configured_agency ? "mode-chip-on" : ""}" data-agency="${a}">${agencyLabels[a]}</button>`).join("");
    const advice = (e.advice || []).map(a => `<div class="stub-body">${this._esc(a)}</div>`).join("");
    const running = e.running || [];
    const runRows = running.length
      ? `<div class="mode-bind-head">Running now</div>` + running.map(r =>
          `<div class="cfg-row"><label>${this._esc(r.name || r.entity)}</label><span class="${r.shed_ok ? "" : "diag-warn"}">${r.shed_ok ? `${r.watts} W` : this._tHtml("{watts} W · protected", { watts: r.watts })}</span></div>`).join("")
      : "";
    return `
      <div class="cfg-row"><label>Current draw</label>${draw}</div>
      <div class="mode-grid" id="newEnergyAgency">${agencyChips}</div>
      ${advice}
      ${runRows}`;
  }

  // Redraws only the status box and wires the agency chips inside it.
  _renderEnergyStatus() {
    const root = this.shadowRoot;
    const box = root?.getElementById("energyStatusBody");
    if (!box || !this._energy) return;
    this._setHtml(box, this._energyStatusHtml());
    this._localizeDOM(box);
    box.querySelectorAll("#newEnergyAgency .mode-chip[data-agency]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        const agency = btn.getAttribute("data-agency");
        try {
          await this._hass.callWS({ type: "nova/energy", action: "set_agency", agency });
        } catch (err) {
          console.error("Nova: failed to set energy agency", err);
        }
        await this._fetchEnergyStatus();
      });
    });
    // Keep the peak field in step with the latest status, unless it is
    // being edited right now.
    const peak = root.getElementById("energyPeakKw");
    if (peak && root.activeElement !== peak) {
      const kw = this._energyPeakKw(this._energy);
      if (kw) peak.value = kw;
    }
  }

  // Appliances — batch-edit-then-save, like Classic (see nova-panel.js's own
  // #appliance-save comment): rows are added/removed/edited locally and only
  // written on "Save appliances", so this deliberately does NOT go through
  // _saveSetting/_render on every keystroke — that would wipe an unsaved,
  // just-added row.
  _applianceTypes() {
    return ["washer", "dryer", "dishwasher", "oven", "microwave", "appliance"];
  }

  _applianceEntityOptions(selected) {
    const states = this._hass?.states || {};
    const cands = [];
    Object.keys(states).forEach(eid => {
      const s = states[eid];
      const dom = eid.split(".")[0];
      const dc = (s.attributes && s.attributes.device_class) || "";
      const unit = ((s.attributes && s.attributes.unit_of_measurement) || "").toLowerCase();
      const isPower = dc === "power" || dc === "energy" || unit === "w" || unit === "kw";
      const isStatus = (dom === "binary_sensor" || dom === "sensor") &&
        /(washer|dryer|dishwash|laundry|appliance|run_complete|cycle_complete|job_state|machine_state)/i.test(eid);
      if (isPower || isStatus) cands.push(eid);
    });
    cands.sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— no entity (use watts) —"], ...cands.map(eid => {
      const fn = (states[eid] && states[eid].attributes && states[eid].attributes.friendly_name) || eid;
      return [eid, fn];
    })];
  }

  _applianceRowHtml(a) {
    const t = a.type || "appliance";
    return `
      <div class="new-appliance-row">
        <input class="new-appliance-name cfg-field" type="text" placeholder="Name (e.g. Washer)" value="${this._esc(a.name || "")}">
        <select class="new-appliance-type cfg-field">${this._optSelect(this._applianceTypes().map(x => [x, x]), t)}</select>
        <select class="new-appliance-entity cfg-field">${this._optSelect(this._applianceEntityOptions(a.entity || ""), a.entity || "")}</select>
        <input class="new-appliance-watts cfg-field cfg-num" type="number" min="0" step="10" placeholder="watts" value="${a.watts || ""}">
        <button class="new-appliance-remove mode-chip" title="Remove appliance" aria-label="Remove appliance">✕</button>
      </div>`;
  }

  _appliancesCardBody() {
    const cfg = this._data()?.config || {};
    const prof = cfg.appliance_profile || [];
    const rows = prof.map(a => this._applianceRowHtml(a)).join("")
      || `<div class="stub-body">No appliances declared yet. Nova still tracks unidentified power sensors in the background, but only a declared or native appliance ever announces a finished cycle.</div>`;
    return `
      <div class="stub-body">Tell Nova which appliances exist so it names cycles correctly instead of guessing from the whole-home meter. Map a dedicated power or status entity when one exists; otherwise set typical running watts.</div>
      <div class="new-appliance-list" id="newApplianceList">${rows}</div>
      <div class="mode-grid">
        <button class="mode-chip" id="newApplianceAdd">+ Add appliance</button>
        <button class="mode-chip mode-chip-on" id="newApplianceSave">Save appliances</button>
      </div>`;
  }

  _wireAppliances() {
    const root = this.shadowRoot;
    const apList = root.getElementById("newApplianceList");
    const apAdd = root.getElementById("newApplianceAdd");
    const apSave = root.getElementById("newApplianceSave");
    if (apAdd && apList) {
      apAdd.addEventListener("click", () => {
        const empty = apList.querySelector(".stub-body");
        if (empty) empty.remove();
        const tmp = document.createElement("div");
        this._setHtml(tmp, this._applianceRowHtml({ name: "", type: "appliance", entity: "", watts: "" }));
        const row = tmp.firstElementChild;
        if (row) apList.appendChild(row);
      });
    }
    if (apList) {
      apList.addEventListener("click", (e) => {
        const rm = e.target.closest(".new-appliance-remove");
        if (rm) {
          e.preventDefault();
          rm.closest(".new-appliance-row")?.remove();
        }
      });
    }
    if (apSave) {
      apSave.addEventListener("click", async () => {
        const rows = Array.from(root.querySelectorAll(".new-appliance-row"));
        const out = [];
        rows.forEach(r => {
          const name = (r.querySelector(".new-appliance-name")?.value || "").trim();
          if (!name) return;
          out.push({
            name,
            type: r.querySelector(".new-appliance-type")?.value || "appliance",
            entity: r.querySelector(".new-appliance-entity")?.value || "",
            watts: parseFloat(r.querySelector(".new-appliance-watts")?.value || "0") || 0,
          });
        });
        await this._rawSaveConfig("appliance_profile", JSON.stringify(out));
        try { await this._hass.callWS({ type: "nova/reload_appliances" }); } catch (err) { console.error("Nova: appliance reload failed", err); }
        await this._fetchLiveData();
        if (this._currentTab === "energy") this._render();
      });
    }
  }

  _wireEnergy() {
    const root = this.shadowRoot;
    root.getElementById("energyRefresh")?.addEventListener("click", () => this._fetchEnergyStatus());
    // Same autosave as _wireSettings: number inputs save as a Number or null.
    root.querySelectorAll("input.cfg-field[data-cfg-key]").forEach(inp => {
      inp.addEventListener("change", async () => {
        const key = inp.getAttribute("data-cfg-key");
        let value = inp.value;
        if (inp.type === "number") value = (value === "" ? null : Number(value));
        await this._saveSetting(key, value);
      });
    });
    // Shown in kW, saved in watts. Empty or non positive input is ignored
    // and the field goes back to the saved value.
    const peak = root.getElementById("energyPeakKw");
    if (peak) {
      peak.addEventListener("change", async () => {
        const raw = peak.value.trim();
        const kw = Number(raw);
        if (raw === "" || !Number.isFinite(kw) || kw <= 0) {
          peak.value = this._energyPeakKw(this._energy);
          return;
        }
        const watts = Math.round(kw * 1000);
        if (this._energy && !this._energy.error) this._energy.peak_watts = watts;
        await this._saveSetting("energy_peak_watts", watts);
      });
    }
    this._wireAppliances();
    this._renderSolarPanel();
    if (this._energy) this._renderEnergyStatus();
    this._fetchEnergyStatus();
    if (this._flow) this._renderEnergyFlow();
    if (this._today) this._renderEnergyToday();
    if (this._outlook) this._renderOutlook();
    this._startEnergyFlowPoll();
  }

  // ─── Live readout ─────────────────────────────────────────────────────
  // Flow diagram: House in the centre, Solar upper left, Grid upper right,
  // Battery below. Each line is drawn from its source to the house; data-dir
  // "in" runs the dashes toward the house and "out" reverses the same
  // animation, so a poll never rebuilds a path. Motion, colour and the
  // static midpoint arrow all say the same thing as the node's words.
  _energyFlowSvg() {
    const nodes = {
      solar: { x: 100, y: 84, r: 38, label: "Solar" },
      grid: { x: 540, y: 84, r: 38, label: "Grid" },
      house: { x: 320, y: 150, r: 44, label: "House" },
      battery: { x: 320, y: 300, r: 38, label: "Battery" },
    };
    // Cubic curves from each source to the house: [P0, P1, P2, P3].
    const lines = {
      solar: [[138, 84], [210, 84], [230, 150], [276, 150]],
      grid: [[502, 84], [430, 84], [410, 150], [364, 150]],
      battery: [[320, 262], [320, 240], [320, 216], [320, 194]],
    };
    const icons = {
      solar: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
      grid: '<path d="M12 3L7 21M12 3l5 18M8.6 9h6.8M7.4 14h9.2M9.2 9l5.6 5M14.8 9l-5.6 5"/>',
      house: '<path d="M3 11l9-7 9 7M5.5 9.5V20h13V9.5M10 20v-5h4v5"/>',
      battery: '<rect x="3" y="7.5" width="16" height="9" rx="1.5"/><path d="M21 10.5v3M9.5 9.5l-2 3h4l-2 3"/>',
    };
    const flow = (kind) => {
      const [p0, p1, p2, p3] = lines[kind];
      const d = `M${p0[0]} ${p0[1]} C${p1[0]} ${p1[1]} ${p2[0]} ${p2[1]} ${p3[0]} ${p3[1]}`;
      // Midpoint and direction of the curve at t = 0.5, for the arrow.
      const mx = (p0[0] + 3 * p1[0] + 3 * p2[0] + p3[0]) / 8;
      const my = (p0[1] + 3 * p1[1] + 3 * p2[1] + p3[1]) / 8;
      const angle = Math.atan2(p3[1] + p2[1] - p1[1] - p0[1], p3[0] + p2[0] - p1[0] - p0[0]) * 180 / Math.PI;
      return `
            <g class="flow" data-flow="${kind}" data-dir="in" data-state="idle" style="--flow-dur:6s;--flow-w:2">
              <path class="flow-glow" d="${d}"/>
              <path class="flow-line" d="${d}"/>
              <g transform="translate(${mx} ${my}) rotate(${Math.round(angle)})"><polygon class="flow-arrow" points="-6,-5 6,0 -6,5"/></g>
            </g>`;
    };
    const node = (kind) => {
      const n = nodes[kind];
      const side = kind === "battery";
      const tx = side ? n.x + 52 : n.x;
      const anchor = side ? "start" : "middle";
      const ty = kind === "house" ? [66, 96, null]
        : side ? [n.y - 18, n.y + 10, n.y + 36]
        : [n.y + n.r + 28, n.y + n.r + 58, n.y + n.r + 84];
      return `
            <g class="flow-node" data-node="${kind}">
              <circle class="node-ring" cx="${n.x}" cy="${n.y}" r="${n.r}"/>
              <g class="node-icon" transform="translate(${n.x - 16} ${n.y - 16}) scale(1.3333)">${icons[kind]}</g>
              <text class="flow-label" x="${tx}" y="${ty[0]}" text-anchor="${anchor}">${n.label}</text>
              <text class="flow-value" x="${tx}" y="${ty[1]}" text-anchor="${anchor}">—</text>${
                kind === "grid" || kind === "battery"
                  ? `\n              <text class="flow-state" x="${tx}" y="${ty[2]}" text-anchor="${anchor}"></text>` : ""}
            </g>`;
    };
    const b = nodes.battery;
    return `
          <svg class="energy-flow" id="energyFlowSvg" viewBox="0 0 640 360" width="100%" role="img" aria-labelledby="energyFlowTitle energyFlowDesc">
            <title id="energyFlowTitle">Power flow</title>
            <desc id="energyFlowDesc">No reading yet.</desc>${flow("solar")}${flow("grid")}${flow("battery")}
            <circle class="battery-track" cx="${b.x}" cy="${b.y}" r="${b.r + 8}"/>
            <circle class="battery-arc" cx="${b.x}" cy="${b.y}" r="${b.r + 8}" pathLength="100" transform="rotate(-90 ${b.x} ${b.y})" data-pct="none" style="--batt-pct:0"/>${node("solar")}${node("grid")}${node("house")}${node("battery")}
          </svg>`;
  }

  // Watts to animation speed and line width, on one log scale: 50 W or
  // less is the slowest (6 s) and thinnest (2), 8000 W or more the fastest
  // (1.2 s) and thickest (6). Anything else, including no reading, is 50 W.
  _flowLevel(w) {
    const v = Number(w);
    if (!Number.isFinite(v) || v <= 50) return 0;
    if (v >= 8000) return 1;
    return Math.log(v / 50) / Math.log(8000 / 50);
  }

  _flowDuration(w) {
    return Math.round((6 - 4.8 * this._flowLevel(w)) * 100) / 100;
  }

  _flowWidth(w) {
    return Math.round((2 + 4 * this._flowLevel(w)) * 100) / 100;
  }

  // One timer at most: starting always stops the old one first. Leaving the
  // tab, hiding the page and disconnecting stop it. A tick is skipped while
  // a fetch is still in flight.
  _startEnergyFlowPoll() {
    this._stopEnergyFlowPoll();
    if (!this._flowVisListener) {
      this._flowVisListener = () => {
        if (document.visibilityState === "hidden") this._stopEnergyFlowPoll();
        else if (this._currentTab === "energy") this._startEnergyFlowPoll();
      };
      document.addEventListener("visibilitychange", this._flowVisListener);
    }
    if (this._currentTab !== "energy" || document.visibilityState === "hidden") return;
    this._energyFlowTick();
    this._flowTimer = setInterval(() => this._energyFlowTick(), 5000);
  }

  // One tick of the single Live timer: the status every time, today's
  // totals only when a minute has passed since the last fetch, the outlook
  // only when 5 minutes have (the tab switch resets both, so entering the
  // tab fetches them at once).
  _energyFlowTick() {
    this._fetchEnergyFlow();
    if (Date.now() - (this._todayAt || 0) >= 60000) this._fetchEnergyToday();
    if (Date.now() - (this._outlookAt || 0) >= 300000) this._fetchEnergyOutlook();
  }

  async _fetchEnergyToday() {
    if (!this._hass || this._todayInFlight) return;
    this._todayInFlight = true;
    this._todayAt = Date.now();
    try {
      this._today = await this._hass.callWS({ type: "nova/energy_flow", action: "today" });
    } catch (_) {
      this._today = { error: true };
    } finally {
      this._todayInFlight = false;
    }
    this._renderEnergyToday();
  }

  _energyKwh(v) {
    if (v == null) return "—";
    return `${v < 100 ? Number(v).toFixed(2) : Number(v).toFixed(1)} kWh`;
  }

  // Today and Battery are hidden while Live says the dashboard is not set
  // up or could not be read; Live shows that message.
  _energyLiveNote() {
    const f = this._flow;
    return !!f && (f.error || f.configured === false);
  }

  _renderEnergyToday() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("energyTodayPanel");
    const list = root?.getElementById("energyToday");
    const msg = root?.getElementById("energyTodayMsg");
    const t = this._today;
    if (!panel || !list || !msg || !t) return;
    panel.hidden = this._energyLiveNote() || (t.configured === false && !t.error);
    const note = t.error ? "Couldn't read energy data." : "";
    if (msg.textContent !== note) this._setText(msg, note);
    msg.hidden = !note;
    list.hidden = !!note;
    if (note) return;
    list.querySelectorAll(".energy-tile[data-today]").forEach(tile => {
      const key = tile.getAttribute("data-today");
      const v = t[key];
      const text = key === "self_sufficiency_pct"
        ? (v == null ? "—" : `${Math.round(v)}%`) : this._energyKwh(v);
      const dd = tile.querySelector(".energy-tile-w");
      if (dd && dd.textContent !== text) this._setText(dd, text);
    });
  }

  _energyDuration(min) {
    const h = Math.floor(min / 60);
    const m = min % 60;
    return [h ? `${h} h` : "", m ? `${m} m` : ""].filter(Boolean).join(" ");
  }

  // The Battery panel, in place: tank fill, percentage, state and power,
  // stored energy and the time estimate. Hidden when no battery reads at all.
  _renderEnergyBattery(f) {
    const root = this.shadowRoot;
    const panel = root?.getElementById("energyBatteryPanel");
    const tank = root?.getElementById("batteryTank");
    if (!panel || !tank) return;
    const b = f?.battery || {};
    const none = b.w == null && b.pct == null && !b.state;
    panel.hidden = this._energyLiveNote() || !f || none;
    if (panel.hidden) return;
    const setText = (id, t) => {
      const el = root.getElementById(id);
      if (!el) return;
      if (el.textContent !== t) this._setText(el, t);
      if (el.classList.contains("battery-line")) el.hidden = !t;
    };
    const pct = b.pct != null ? Math.round(b.pct) : null;
    const word = this._energyFlowWord(b.state);
    const power = b.w != null && b.state && b.state !== "idle" ? this._energyFlowWatts(b.w) : "";
    setText("batteryPct", pct != null ? `${pct}%` : "—");
    setText("batteryState", [word, power].filter(Boolean).join(" · "));
    setText("batteryStored", b.stored_kwh != null && b.capacity_kwh != null
      ? this._t("{stored} kWh of {capacity} kWh", {
        stored: Number(b.stored_kwh).toFixed(1), capacity: Number(b.capacity_kwh).toFixed(1) }) : "");
    setText("batteryEta", b.eta_min
      ? this._t(b.eta_to === "full" ? "About {time} to full at this rate" : "About {time} left at this rate",
        { time: this._energyDuration(b.eta_min) }) : "");
    const fill = tank.querySelector(".tank-fill");
    const level = String(pct != null ? Math.max(0, Math.min(100, pct)) : 0);
    if (fill && fill.style.getPropertyValue("--tank-pct") !== level) fill.style.setProperty("--tank-pct", level);
    // Whole sentences for the screen reader; {state} is the backend's own word.
    const active = b.state && b.state !== "idle" && b.w != null;
    const idle = !active && b.state === "idle";
    const label = this._t(pct != null
      ? (active ? "Battery {percent} percent, {state} at {power}." : idle ? "Battery {percent} percent, idle." : "Battery {percent} percent.")
      : (active ? "Battery no reading, {state} at {power}." : idle ? "Battery no reading, idle." : "Battery no reading."),
    { percent: pct, state: b.state, power: active ? this._energyFlowWatts(b.w) : "" });
    if (tank.getAttribute("aria-label") !== label) tank.setAttribute("aria-label", label);   // i18n-ok: built with _t just above
  }

  _stopEnergyFlowPoll() {
    if (this._flowTimer) { clearInterval(this._flowTimer); this._flowTimer = null; }
  }

  async _fetchEnergyFlow() {
    if (!this._hass || this._flowInFlight) return;
    this._flowInFlight = true;
    try {
      this._flow = await this._hass.callWS({ type: "nova/energy_flow", action: "status" });
    } catch (_) {
      this._flow = { error: true };
    } finally {
      this._flowInFlight = false;
    }
    this._renderEnergyFlow();
  }

  _energyFlowWatts(w) {
    if (w == null) return "—";
    return w < 1000 ? `${Math.round(w)} W` : `${(w / 1000).toFixed(2)} kW`;
  }

  _energyFlowWord(state) {
    return this._tx({ charging: "Charging", discharging: "Discharging", importing: "Importing",
      exporting: "Exporting", idle: "Idle" }[state] || "");
  }

  // Changes only the text of the existing tile nodes, never the nodes.
  _renderEnergyFlow() {
    const root = this.shadowRoot;
    const list = root?.getElementById("energyLive");
    const msg = root?.getElementById("energyLiveMsg");
    const diagram = root?.getElementById("energyDiagram");
    if (!list || !msg || !this._flow) return;
    const f = this._flow;
    const note = f.error ? "Couldn't read energy data."
      : f.configured === false ? "Set up solar, battery or grid in Home Assistant's Energy dashboard."
      : "";
    if (msg.textContent !== note) this._setText(msg, note);
    msg.hidden = !note;
    list.hidden = !!note;
    if (diagram) diagram.hidden = !!note;
    this._renderEnergyBattery(f);
    this._renderEnergyToday();
    if (note) return;
    this._renderEnergyDiagram(f);
    const pct = f.battery?.pct;
    const values = {
      solar: [f.solar?.w, ""],
      house: [f.house?.w, ""],
      battery: [f.battery?.w, [this._energyFlowWord(f.battery?.state), pct != null ? `${Math.round(pct)}%` : ""].filter(Boolean).join(" · ")],
      grid: [f.grid?.w, this._energyFlowWord(f.grid?.state)],
    };
    Object.entries(values).forEach(([kind, [w, state]]) => {
      const tile = list.querySelector(`.energy-tile[data-flow="${kind}"]`);
      if (!tile) return;
      const wEl = tile.querySelector(".energy-tile-w");
      const stEl = tile.querySelector(".energy-tile-state");
      const wText = this._energyFlowWatts(w);
      if (wEl && wEl.textContent !== wText) this._setText(wEl, wText);
      if (stEl && stEl.textContent !== state) this._setText(stEl, state);
    });
    this._announceEnergyFlow(f);
  }

  // Updates the diagram in place: text, data-dir, data-state, the two CSS
  // variables and the battery arc. Never rebuilds the SVG, which would
  // restart every animation.
  _renderEnergyDiagram(f) {
    const svg = this.shadowRoot?.getElementById("energyFlowSvg");
    if (!svg) return;
    const setText = (el, t) => { if (el && el.textContent !== t) this._setText(el, t); };
    const setData = (el, k, v) => { if (el && el.getAttribute(k) !== v) el.setAttribute(k, v); };
    const setVar = (el, k, v) => { if (el && el.style.getPropertyValue(k) !== v) el.style.setProperty(k, v); };
    const pct = f.battery?.pct;
    const pctText = pct != null ? `${Math.round(pct)}%` : "";
    const lines = {
      solar: { w: f.solar?.w, state: f.solar?.w != null && f.solar.w >= 20 ? "producing" : "idle", dir: "in" },
      grid: { w: f.grid?.w, state: f.grid?.state || "idle", dir: f.grid?.state === "exporting" ? "out" : "in" },
      battery: { w: f.battery?.w, state: f.battery?.state || "idle", dir: f.battery?.state === "charging" ? "out" : "in" },
    };
    Object.entries(lines).forEach(([kind, l]) => {
      const g = svg.querySelector(`.flow[data-flow="${kind}"]`);
      const idle = l.w == null || l.w < 20 || l.state === "idle";
      setData(g, "data-state", idle ? "idle" : l.state);
      setData(g, "data-dir", l.dir);
      setVar(g, "--flow-dur", `${this._flowDuration(idle ? 0 : l.w)}s`);
      setVar(g, "--flow-w", String(this._flowWidth(idle ? 0 : l.w)));
    });
    const node = (kind) => svg.querySelector(`.flow-node[data-node="${kind}"]`);
    ["solar", "grid", "house", "battery"].forEach(kind => {
      setText(node(kind)?.querySelector(".flow-value"), this._energyFlowWatts(f[kind]?.w));
    });
    setText(node("grid")?.querySelector(".flow-state"), this._energyFlowWord(f.grid?.state));
    setText(node("battery")?.querySelector(".flow-state"),
      [this._energyFlowWord(f.battery?.state), pctText].filter(Boolean).join(" · "));
    const arc = svg.querySelector(".battery-arc");
    setData(arc, "data-pct", pct != null ? String(Math.round(pct)) : "none");
    setVar(arc, "--batt-pct", String(pct != null ? Math.max(0, Math.min(100, pct)) : 0));
    setText(svg.querySelector("#energyFlowDesc"), this._energyFlowSentence(f));
  }

  // The diagram as one plain sentence, for the SVG's desc.
  _energyFlowSentence(f) {
    const say = (w) => w == null ? "no reading"
      : w < 1000 ? `${Math.round(w)} W` : `${(Math.round(w / 100) / 10).toFixed(1)} kW`;
    const pct = f.battery?.pct;
    const bs = f.battery?.state;
    let battery = f.battery?.w == null ? "Battery no reading"
      : bs === "idle" || !bs ? "Battery idle" : `Battery ${bs} at ${say(f.battery.w)}`;
    if (pct != null) battery += `, ${Math.round(pct)} percent`;
    const gs = f.grid?.state;
    const grid = f.grid?.w == null ? "Grid no reading"
      : gs === "idle" || !gs ? "Grid idle" : `Grid ${gs} ${say(f.grid.w)}`;
    return `Solar ${say(f.solar?.w)}. House ${say(f.house?.w)}. ${battery}. ${grid}.`;
  }

  // Speaks only a real change of battery or grid state, never the first
  // load, and the same change at most once a minute.
  _announceEnergyFlow(f) {
    const cur = { battery: f.battery?.state || null, grid: f.grid?.state || null };
    const prev = this._flowPrevStates;
    this._flowPrevStates = cur;
    if (!prev) return;
    if (!this._flowAnnounced) this._flowAnnounced = {};
    const now = Date.now();
    const lines = [];
    ["battery", "grid"].forEach(kind => {
      if (!cur[kind] || !prev[kind] || cur[kind] === prev[kind]) return;
      const key = `${kind}:${prev[kind]}>${cur[kind]}`;
      if (now - (this._flowAnnounced[key] || 0) < 60000) return;
      this._flowAnnounced[key] = now;
      // Whole sentences, so a translation never has to join two words.
      const sentence = {
        battery: { charging: "Battery charging.", discharging: "Battery discharging.",
          importing: "Battery importing.", exporting: "Battery exporting.", idle: "Battery idle." },
        grid: { charging: "Grid charging.", discharging: "Grid discharging.",
          importing: "Grid importing.", exporting: "Grid exporting.", idle: "Grid idle." },
      }[kind][cur[kind]];
      lines.push(sentence ? this._tx(sentence)
        : `${kind === "battery" ? "Battery" : "Grid"} ${this._energyFlowWord(cur[kind]).toLowerCase()}.`);
    });
    const region = this.shadowRoot?.getElementById("energyLiveAnnounce");
    if (region && lines.length) this._setText(region, lines.join(" "));
  }

  // ─── Outlook ──────────────────────────────────────────────────────────
  // The next 36 hours from nova/energy_outlook: price bands, the adjusted
  // solar forecast and the planned battery level, then the advice. The SVG
  // only draws shapes and stretches to the panel's width; every word sits in
  // HTML beside it so text keeps its size on a phone. The rates key under
  // the strip prints each price with its times, so colour is never the only
  // signal. Updated in place, never by _render().

  async _fetchEnergyOutlook() {
    if (!this._hass || this._outlookInFlight) return;
    this._outlookInFlight = true;
    this._outlookAt = Date.now();
    try {
      this._outlook = await this._hass.callWS({ type: "nova/energy_outlook" });
    } catch (_) {
      this._outlook = { error: true, configured: true };
    } finally {
      this._outlookInFlight = false;
    }
    this._renderOutlook();
  }

  // Money in the household's currency through the browser's own locale
  // formatting; a plain number with the code if the currency is unknown.
  _energyMoney(v, currency, digits = 2) {
    if (v == null || !Number.isFinite(Number(v))) return "";
    const n = Number(v);
    if (currency) {
      try {
        return new Intl.NumberFormat(this._resolveUiLang(), {
          style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits,
        }).format(n);
      } catch (_) { /* unknown code: fall through */ }
    }
    return `${n.toFixed(digits)}${currency ? ` ${currency}` : ""}`;
  }

  // "HH:MM" from the backend's ISO time, which is already in Home
  // Assistant's time zone (the browser's may differ).
  _outlookTime(iso) {
    return typeof iso === "string" && iso.length >= 16 ? iso.slice(11, 16) : "";
  }

  _outlookSetText(el, t) {
    if (el && el.textContent !== t) this._setText(el, t);
  }

  _outlookSetHtml(el, html) {
    if (el && el._novaHtml !== html) { this._setHtml(el, html); el._novaHtml = html; }
  }

  _renderOutlook() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("energyOutlookPanel");
    const o = this._outlook;
    if (!panel || !o) return;
    panel.hidden = o.configured === false && !o.error;
    if (panel.hidden) return;
    const msg = root.getElementById("outlookMsg");
    const wrap = root.getElementById("outlookStripWrap");
    const adviceEl = root.getElementById("outlookAdvice");
    const windowEl = root.getElementById("outlookWindow");
    const learnedEl = root.getElementById("outlookLearned");
    const note = o.error ? "Couldn't read energy data." : (o.messages || []).join(" ");
    this._outlookSetText(msg, note);
    if (msg) msg.hidden = !note;
    this._outlookSetText(root.getElementById("outlookUpdated"),
      o.updated_at ? this._t("Updated {time}", { time: this._outlookTime(o.updated_at) }) : "");

    const points = o.error ? [] : (o.points || []);
    const bands = o.error ? [] : (o.bands || []);
    if (wrap) wrap.hidden = !points.length && !bands.length;
    if (wrap && !wrap.hidden) this._renderOutlookStrip(o, points, bands);

    let adviceHtml = "";
    if (points.length) {
      adviceHtml = (o.advice || []).map(a => `
            <li class="outlook-item" data-kind="${this._esc(a.kind || "")}">
              <div class="outlook-item-title">${this._esc(a.title || "")}</div>
              <div class="stub-body">${this._esc(a.message || "")}</div>${
                a.saving != null ? `\n              <div class="outlook-saving">${this._tHtml("Saves about {amount}", { amount: this._esc(this._energyMoney(a.saving, o.currency)) })}</div>` : ""}
            </li>`).join("") || `<li class="outlook-calm stub-body">Nothing to change right now.</li>`;
    }
    this._outlookSetHtml(adviceEl, adviceHtml);
    if (adviceEl) adviceEl.hidden = !adviceHtml;

    const bw = o.error ? null : o.best_window;
    const bwText = bw ? this._t("Best time for a big appliance: {start} to {end} ({reason})",
      { start: this._outlookTime(bw.start), end: this._outlookTime(bw.end), reason: this._tx(bw.reason) }) : "";
    this._outlookSetText(windowEl, bwText);
    if (windowEl) windowEl.hidden = !bwText;

    const learnedText = o.error ? "" : this._outlookLearned(o.learned || {});
    this._outlookSetText(learnedEl, learnedText);
    if (learnedEl) learnedEl.hidden = !learnedText;
  }

  _outlookLearned(l) {
    const parts = [];
    if (l.tariff_days) parts.push(`Tariff from ${l.tariff_days} days`);
    if (l.load_days) parts.push(`usage from ${l.load_days} days`);
    if (l.forecast_shape && l.forecast_shape !== "none" && l.forecast_factor != null) {
      parts.push(`forecast adjusted by ${Number(l.forecast_factor).toFixed(2)}${
        l.factor_source === "default" ? " (a cautious default until it has more days)" : ""}`);
    }
    if (!parts.length) return "";
    const first = parts.join(", ");
    let text = `${first[0].toUpperCase()}${first.slice(1)}.`;
    if (l.forecast_shape === "estimated") text += " Hourly sun is estimated from the daily forecast.";
    return text;
  }

  // Shapes in the SVG (36 hours across 360 units, plot above, bands below),
  // words in HTML positioned by percentage of the same 36 hours.
  _renderOutlookStrip(o, points, bands) {
    const root = this.shadowRoot;
    const svg = root.getElementById("outlookSvg");
    if (!svg) return;
    const H = 3600000;
    const span = 36;
    const t0 = points.length ? Date.parse(points[0].t) : Math.floor(Date.now() / H) * H;
    const hoursAt = (iso) => (Date.parse(iso) - t0) / H;
    const clamp = (v) => Math.max(0, Math.min(span, v));
    const pct = (h) => `${(clamp(h) / span * 100).toFixed(2)}%`;
    const PLOT_TOP = 6, PLOT_BOTTOM = 70, BAND_TOP = 74;

    const maxSolar = Math.max(0.5, ...points.map(p => Number(p.solar_kwh) || 0));
    const sy = (v) => (PLOT_BOTTOM - (Number(v) || 0) / maxSolar * (PLOT_BOTTOM - PLOT_TOP)).toFixed(1);
    const solarD = points.length
      ? `M0 ${PLOT_BOTTOM} ` + points.map((p, i) => `L${i * 10 + 5} ${sy(p.solar_kwh)}`).join(" ") + ` L${points.length * 10} ${PLOT_BOTTOM} Z`
      : "";
    const solarPath = svg.querySelector(".outlook-solar");
    if (solarPath && solarPath.getAttribute("d") !== solarD) solarPath.setAttribute("d", solarD);

    const socY = (v) => (PLOT_BOTTOM - Math.max(0, Math.min(100, v)) / 100 * (PLOT_BOTTOM - PLOT_TOP)).toFixed(1);
    const soc = points.filter(p => p.soc_pct != null).length === points.length && points.length
      ? points.map((p, i) => `${(i + 1) * 10},${socY(p.soc_pct)}`).join(" ") : "";
    const socLine = svg.querySelector(".outlook-soc");
    if (socLine && socLine.getAttribute("points") !== soc) socLine.setAttribute("points", soc);

    const now = svg.querySelector(".outlook-now");
    const nowX = (clamp((Date.now() - t0) / H) * 10).toFixed(1);
    if (now && now.getAttribute("x1") !== nowX) { now.setAttribute("x1", nowX); now.setAttribute("x2", nowX); }

    const shown = bands.map(b => ({ ...b, h1: clamp(hoursAt(b.start)), h2: clamp(hoursAt(b.end)) }))
      .filter(b => b.h2 > b.h1);
    this._outlookSetHtml(svg.querySelector(".outlook-bands"), shown.map(b =>
      `<rect class="outlook-band" data-label="${this._esc(b.label)}" x="${(b.h1 * 10).toFixed(1)}" y="${BAND_TOP}" width="${((b.h2 - b.h1) * 10).toFixed(1)}" height="${100 - BAND_TOP}"/>`).join(""));
    // A price inside a band only where three hours or more give it room;
    // the rates key below always has every price.
    this._outlookSetHtml(root.querySelector(".outlook-band-labels"), shown.filter(b => b.h2 - b.h1 >= 3).map(b =>
      `<span class="outlook-band-label" style="left:${pct(b.h1)};width:${pct(b.h2 - b.h1)}"><b>${this._esc(this._energyMoney(b.price, o.currency))}</b> ${this._esc(this._outlookTime(b.start))}</span>`).join(""));

    const ticks = [];
    for (let h = 0; h <= span; h++) {
      const iso = points[h]?.t;
      if (iso && Number(iso.slice(11, 13)) % 6 === 0) ticks.push([h, this._outlookTime(iso)]);
    }
    this._outlookSetHtml(root.querySelector(".outlook-ticks"), ticks.map(([h, t]) =>
      `<span${h === 0 ? ' class="tick-start"' : ""} style="left:${pct(h)}">${t}</span>`).join("") + `<span class="tick-now" style="left:${pct((Date.now() - t0) / H)}">Now</span>`);

    // Rates key: one row per price, cheapest first, each with its times
    // (a time range repeated tomorrow is listed once).
    const byPrice = new Map();
    shown.forEach(b => {
      const k = String(b.price);
      if (!byPrice.has(k)) byPrice.set(k, { price: b.price, label: b.label, times: [] });
      const range = `${this._outlookTime(b.start)} to ${this._outlookTime(b.end)}`;
      const row = byPrice.get(k);
      if (!row.times.includes(range)) row.times.push(range);
    });
    const rates = [...byPrice.values()].sort((a, b) => a.price - b.price);
    this._outlookSetHtml(root.getElementById("outlookRates"), rates.map(r => `
              <div class="outlook-rate" data-label="${this._esc(r.label)}"><dt><i class="rate-swatch"></i>${this._esc(this._energyMoney(r.price, o.currency))} ${this._esc(r.label)}</dt><dd>${this._esc(r.times.join(", "))}</dd></div>`).join(""));

    this._outlookSetText(svg.querySelector("#outlookDesc"), this._outlookSentence(o, points, rates));
  }

  // The strip in words, for the SVG's desc.
  _outlookSentence(o, points, rates) {
    const out = [];
    const cheap = rates[0];
    if (cheap && rates.length > 1) {
      out.push(`Cheapest rate ${cheap.times[0]} at ${this._energyMoney(cheap.price, o.currency)}.`);
      const dear = rates[rates.length - 1];
      out.push(`Dearest rate ${dear.times[0]} at ${this._energyMoney(dear.price, o.currency)}.`);
    } else if (cheap) {
      out.push(`One rate all day at ${this._energyMoney(cheap.price, o.currency)}.`);
    }
    const sun = points.reduce((sum, p) => sum + (Number(p.solar_kwh) || 0), 0);
    if (points.length) out.push(`About ${sun.toFixed(1)} kWh of sun expected over the next 36 hours.`);
    const withSoc = points.filter(p => p.soc_pct != null);
    if (withSoc.length) {
      // soc_pct is the level at the end of its hour.
      const low = withSoc.reduce((a, b) => (b.soc_pct < a.soc_pct ? b : a));
      const hhmm = `${String((Number(low.t.slice(11, 13)) + 1) % 24).padStart(2, "0")}:00`;
      out.push(`Battery expected to reach its lowest, ${Math.round(low.soc_pct)} percent, at ${hhmm}.`);
    }
    return out.join(" ") || "No outlook yet.";
  }
  // ─── Suggestions ──────────────────────────────────────────────────────
  // Ported from Classic's own Suggestions tab. Data rides on the same
  // nova/get_panel_data payload the dashboard already polls (d.suggestions)
  // — no separate fetch. Approve/dismiss dim the card in place rather than
  // removing it or re-fetching, matching Classic's own lightweight pattern.
  static SUGGESTION_TYPE_LABEL = {
    time_routine: "Daily routine", sequence: "Action sequence",
    repeated_command: "Repeated command", temp_pref: "Temperature",
    presence: "Presence", numeric_trigger: "Sensor threshold",
  };

  _htmlSuggestions() {
    const sugs = this._data()?.suggestions || [];
    if (!sugs.length) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Learned Opportunities</div></div>
          <div class="stub-body">No suggestions right now. Nova proposes automations as it notices routines repeat — a light you turn on each evening, a scene after a button press, the heat when it's cold. As patterns build up, they'll appear here for you to review and approve. Nothing is ever created without your say-so.</div>
          <div class="mode-grid"><button class="mode-chip" id="sugRunAnalysis">Analyze Now</button></div>
          <div class="toggle-desc" id="sugAnalysisResult" style="margin-top:8px">See why nothing has qualified yet, or force a fresh pass over your history.</div>
        </div>
        ${this._htmlFilteredSuggestions()}
        ${this._htmlAutomationTrials()}
        ${this._htmlAutomationInventory()}`;
    }
    const rows = sugs.map(s => {
      const pct = Math.round((s.confidence || 0) * 100);
      const confColor = pct >= 80 ? "#5fbf7a" : pct >= 55 ? "var(--warn)" : "var(--ink-faint)";
      const label = NovaPanel.SUGGESTION_TYPE_LABEL[s.pattern_type] || "Learned pattern";
      const evidence = (s.evidence || []).map(e => `<li>${this._esc(e)}</li>`).join("");
      const chips = (s.entity_labels && s.entity_labels.length) ? s.entity_labels : (s.entities || []);
      const entities = chips.length
        ? `<div class="mode-grid">${chips.map(e => `<span class="area-cap" style="width:auto;padding:3px 8px;font-size:11px">${this._esc(e)}</span>`).join("")}</div>`
        : "";
      const match = s.automation_match || {};
      // The backend names the automations it matched in match.matches.
      const matched = (match.matches || [])[0] || {};
      const matchName = matched.name || matched.entity_id || "an existing automation";
      let overlap = "";
      if (match.status === "possible_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Possible overlap with <b>${this._esc(matchName)}</b>. Review both before creating this automation.</div>`;
      } else if (match.status === "unknown_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ ${(match.matches || []).length ? `<b>${this._esc(matchName)}</b> may control the same device, but Nova cannot fully compare its blueprint or template.` : "Nova cannot fully compare this automation's template or blueprint with existing automations."}</div>`;
      } else if (match.status === "inventory_unavailable") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Nova could not check existing automations. Review Home Assistant before creating this one.</div>`;
      }
      return `
        <div class="panel new-sug" data-sug-id="${s.id}">
          <div class="panel-head">
            <div class="panel-title">${this._esc(label)}</div>
            <div class="panel-meta" style="color:${confColor}">${this._tHtml("{percent}% confident", { percent: pct })}</div>
          </div>
          ${s.why_headline ? `<div class="stub-body"><b>${this._esc(s.why_headline)}</b></div>` : ""}
          <div class="stub-body">${this._esc(s.description)}</div>
          ${overlap}
          ${evidence ? `<div class="mode-bind-head">What Nova observed</div><ul style="margin:0 0 10px;padding-left:18px;font-size:12px;color:var(--ink-dim);line-height:1.6">${evidence}</ul>` : ""}
          ${entities}
          <div class="cfg-row"><span class="toggle-desc">${this._tHtml("seen {count}× in 30 days", { count: s.count || "?" })}</span></div>
          <div class="mode-grid">
            <button class="mode-chip new-sug-approve">✓ Create automation</button>
            <button class="mode-chip new-sug-dismiss">✕ Dismiss</button>
            <button class="mode-chip new-sug-yaml-btn">⌄ See the automation</button>
          </div>
          <pre class="new-sug-yaml" hidden style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin-top:8px">${this._esc(s.yaml || "")}</pre>
        </div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Learned Opportunities</div>
          <div class="panel-meta">${this._tHtml(sugs.length === 1 ? "{count} suggestion to review" : "{count} suggestions to review", { count: sugs.length })}</div>
        </div>
        <div class="stub-body">Automations Nova has learned from watching your routines. Review each — approve to create it in Home Assistant, or dismiss it. Nothing runs until you approve, and you can see the exact automation before deciding.</div>
      </div>
      ${rows}
      ${this._htmlFilteredSuggestions()}
      ${this._htmlAutomationTrials()}
      ${this._htmlAutomationInventory()}`;
  }

  // Suggestions the AI review turned down (v7.126.0). They are never
  // suggested again; "Suggest anyway" brings one back for you to decide.
  _htmlFilteredSuggestions() {
    const items = this._data()?.suggestions_filtered || [];
    if (!items.length) return "";
    const rows = items.map(f => `
      <div class="cfg-row new-sug-filtered" data-sug-id="${f.id}" style="align-items:flex-start;gap:10px">
        <div style="flex:1">
          <div class="stub-body" style="margin:0">${this._esc(f.description || "")}</div>
          <div class="toggle-desc">Rejected: ${this._esc(f.reason || "no reason given")}${f.model ? ` (${this._esc(f.model)})` : ""}</div>
        </div>
        <button class="mode-chip new-sug-restore">Suggest anyway</button>
      </div>`).join("");
    return `
      <details class="panel">
        <summary class="panel-head" style="cursor:pointer">
          <div class="panel-title">Filtered by AI review</div>
          <div class="panel-meta">${this._tHtml("{count} not suggested", { count: items.length })}</div>
        </summary>
        <div class="stub-body">These learned patterns were checked by the Suggestion Review model and turned down, so Nova won't suggest them again. Bring one back if you think the review got it wrong.</div>
        ${rows}
      </details>`;
  }

  _htmlAutomationInventory() {
    const result = this._automationInventory;
    if (result === null) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Couldn't load Home Assistant automations. Nova will not assume the list is empty.</div>
        </div>`;
    }
    if (result === undefined) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Loading the automation inventory…</div>
        </div>`;
    }
    const automations = result.automations || [];
    if (!result.available) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Nova's automation inventory is unavailable. Suggestions will be marked for manual review instead of assuming nothing exists.</div>
        </div>`;
    }
    if (!automations.length) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div><div class="panel-meta">0 found</div></div>
          <div class="stub-body">Home Assistant currently reports no loaded automations.</div>
        </div>`;
    }
    const rows = automations.map(a => {
      const status = a.enabled ? "enabled" : "disabled";
      const scope = a.understanding === "full" ? "fully understood"
        : a.understanding === "partial" ? "blueprint · partial comparison"
        : "metadata only";
      const triggered = a.last_triggered
        ? new Date(a.last_triggered).toLocaleString() : "never";
      const origin = a.origin === "nova" ? "created by Nova" : "existing";
      return `
        <div class="cfg-row">
          <label>${this._esc(a.name || a.entity_id)}</label>
          <span class="toggle-desc">${this._tHtml("{status} · {origin} · {scope} · last triggered {when}", { status: this._esc(status), origin: this._esc(origin), scope: this._esc(scope), when: this._esc(triggered) })}</span>
        </div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Existing Home Assistant Automations</div>
          <div class="panel-meta">${this._tHtml("{count} loaded", { count: automations.length })}</div>
        </div>
        <div class="stub-body">Nova uses this read-only inventory to avoid relearning routines Home Assistant already handles. It refreshes at startup and whenever automations are reloaded.</div>
        ${rows}
      </div>`;
  }

  // ─── Automation probation (Phase 3) ──────────────────────────────────────
  // Installing a suggestion only means it was ACCEPTED — this section shows
  // what's actually been observed running since, entirely separate from that
  // acceptance. Run counts come from Home Assistant's own automation_triggered
  // event; "Working"/"Needs adjustment" is manual feedback only, never inferred.

  _htmlAutomationTrials() {
    const trials = this._automationTrials;
    if (trials === null) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">Couldn't load installed automations.</div>
        </div>`;
    }
    if (!trials || !trials.length) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">No tracked Nova automations yet. Automations installed from new suggestions will appear here.</div>
        </div>`;
    }
    const rows = trials.map(t => {
      const when = t.last_run ? new Date(t.last_run * 1000).toLocaleString() : "never";
      const outcome = t.manual_outcome
        ? `<span class="toggle-desc">${t.manual_outcome === "working" ? "Feedback: Working" : "Feedback: Needs adjustment"}</span>`
        : "";
      return `
        <div class="cfg-row">
          <label>${this._esc(t.automation_id)}</label>
          <span class="toggle-desc">${this._tHtml("ran {count}× · last {when}", { count: t.run_count || 0, when: this._esc(when) })}</span>
        </div>
        <div class="mode-grid" data-trial-id="${t.id}">
          <button class="mode-chip new-trial-fb" data-verdict="working">WORKING</button>
          <button class="mode-chip new-trial-fb" data-verdict="needs_adjustment">NEEDS ADJUSTMENT</button>
        </div>
        <div class="cfg-row">${outcome}</div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Created by Nova</div>
          <div class="panel-meta">${this._tHtml("{count} tracked", { count: trials.length })}</div>
        </div>
        <div class="stub-body">Installing an automation means you accepted the suggestion — it isn't proof the automation works. This shows what's actually been observed running; "Working" and "Needs adjustment" are your own call, not Nova's.</div>
        ${rows}
      </div>`;
  }

  async _fetchAutomationTrials() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/list_automation_trials" });
      this._automationTrials = result.trials || [];
    } catch (_) { this._automationTrials = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _fetchAutomationInventory() {
    if (!this._hass) return;
    try {
      this._automationInventory = await this._hass.callWS({
        type: "nova/list_automation_inventory",
      });
    } catch (_) { this._automationInventory = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _submitAutomationTrialFeedback(trialId, verdict) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/automation_trial_feedback", trial_id: trialId, verdict });
      this._fetchAutomationTrials();
    } catch (err) {
      console.error("Nova: automation trial feedback failed", err);
    }
  }

  _wireSuggestions() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-sug").forEach(card => {
      const sid = parseInt(card.getAttribute("data-sug-id"), 10);
      const act = async (action) => {
        if (!this._hass || isNaN(sid)) return;
        const buttons = card.querySelectorAll("button");
        buttons.forEach(b => b.disabled = true);
        let res = null;
        try {
          res = await this._hass.callWS({ type: "nova/suggestion_action", suggestion_id: sid, action });
        } catch (err) {
          console.error(`Nova: suggestion ${action} failed`, err);
        }
        // Only a settled suggestion (installed, acknowledged, covered or
        // dismissed) greys out. A failed install stays pending on the
        // backend, so its buttons come back with the reason shown.
        if (res && res.ok) {
          card.style.opacity = "0.35";
          return;
        }
        buttons.forEach(b => b.disabled = false);
        let note = card.querySelector(".new-sug-status");
        if (!note) {
          note = document.createElement("div");
          note.className = "stub-body new-sug-status";
          note.style.color = "var(--warn)";
          card.querySelector(".new-sug-approve")?.parentElement?.before(note);
        }
        this._setText(note, `⚠ ${(res && res.reason) || this._t(action === "approve"
          ? "Could not approve this suggestion. Try again." : "Could not dismiss this suggestion. Try again.")}`);
      };
      card.querySelector(".new-sug-approve")?.addEventListener("click", () => act("approve"));
      card.querySelector(".new-sug-dismiss")?.addEventListener("click", () => act("dismiss"));
      card.querySelector(".new-sug-yaml-btn")?.addEventListener("click", () => {
        const pre = card.querySelector(".new-sug-yaml");
        if (pre) pre.hidden = !pre.hidden;
      });
    });
    root.querySelectorAll(".new-sug-filtered").forEach(row => {
      const sid = parseInt(row.getAttribute("data-sug-id"), 10);
      const btn = row.querySelector(".new-sug-restore");
      btn?.addEventListener("click", async () => {
        if (!this._hass || isNaN(sid)) return;
        btn.disabled = true;
        let res = null;
        try {
          res = await this._hass.callWS({ type: "nova/suggestion_action", suggestion_id: sid, action: "restore" });
        } catch (err) {
          console.error("Nova: suggestion restore failed", err);
        }
        if (res && res.ok) {
          row.style.opacity = "0.35";
          this._setText(btn, "Back in suggestions");
          return;
        }
        btn.disabled = false;
      });
    });
    root.querySelectorAll(".new-trial-fb").forEach(btn => {
      btn.addEventListener("click", () => {
        const group = btn.closest("[data-trial-id]");
        const trialId = parseInt(group?.getAttribute("data-trial-id"), 10);
        if (!isNaN(trialId)) this._submitAutomationTrialFeedback(trialId, btn.getAttribute("data-verdict"));
      });
    });
  }

  // ─── Settings ─────────────────────────────────────────────────────────
  // Reorganized around what you're trying to do rather than which Nova
  // subsystem it touches — the old Classic split (System Diagnostics
  // under General, a separate Diagnostics under Cameras) is merged here
  // into one place. Every card is "real:true" — nothing here is a stub.
  // Mirrors const.py's HONORIFIC_OPTIONS — kept in sync by hand, same as
  // AREA_CAP_ORDER/AREA_CAP_ICON below mirror their own backend source.
  // Regional "Nova speaks" codes that Nova names (output_language.py) but the
  // picker has no option for; a saved one is shown as its code.
  static KEPT_REGIONAL_CODES = ["fr-CA", "es-419", "de-CH"];
  static HONORIFIC_OPTIONS = ["sir", "ma'am", "boss", "friend"];
  // Whole labels, so each one can be translated.
  static HONORIFIC_LABELS = { sir: "Sir", "ma'am": "Ma'am", boss: "Boss", friend: "Friend" };

  static SETTINGS_GROUPS = [
    { id: "general", label: "General" },
    { id: "voice", label: "Voice & Speakers" },
    { id: "safety", label: "Awareness & Safety" },
    { id: "learning", label: "Learning & Memory" },
    { id: "cameras", label: "Cameras" },
    { id: "home", label: "Home & Extras" },
  ];

  static SETTINGS_CARDS = [
    { id: "general", group: "general", title: "General", real: true,
      desc: "Language, sleep state, and the core proactive-speech switches." },
    { id: "person_honorifics", group: "general", title: "Person Honorifics", real: true,
      desc: "What Nova calls each person when they're home alone. Drops the address entirely the moment more than one person — or nobody — is home." },
    { id: "room_speakers", group: "voice", title: "Room Speakers", real: true,
      desc: "Assign the one speaker Nova may use per room, plus a general fallback." },
    { id: "ai_models", group: "voice", title: "AI Models", real: true,
      desc: "Provider and model per tier — main agent, classifier, reasoning, review, vision." },
    { id: "voice_phrases", group: "voice", title: "Voice Phrases", real: true,
      desc: "Things you say, linked to a script, automation, scene or command." },
    { id: "briefings", group: "voice", title: "Briefings", real: true,
      desc: "Daily briefing schedule, content, and delivery speakers." },
    { id: "voice_confirmation", group: "voice", title: "Voice Confirmation", real: true,
      desc: "Whether risky actions need a spoken or phone confirmation before Nova acts." },
    { id: "voice_id", group: "voice", title: "Voice ID", real: true, feature: "voice_id",
      desc: "Speech to text through the Nova Voice ID app, with a fallback for when the app is down." },
    { id: "satellite_speaker", group: "voice", title: "Satellite → Speaker", real: true,
      desc: "Per-satellite override, for a specific satellite that shouldn't use its room's assigned speaker." },
    { id: "announcement_speakers", group: "voice", title: "Announcement Speakers", real: true,
      desc: "Which speakers whole-house broadcasts (briefings, sentinel alerts) use." },
    { id: "notifications", group: "safety", title: "Notifications", real: true,
      desc: "Your phone's notify service, for alerts when nobody's home to hear a speaker." },
    { id: "security_alarm", group: "safety", title: "Security Alarm", real: true,
      desc: "Choose the one alarm Nova uses for security decisions. Automatic lockdown is always opt in." },
    { id: "sentinel_rules", group: "safety", title: "Sentinel Rules", real: true,
      desc: "Enable or disable individual door and lock anomaly rules." },
    { id: "hazard_monitor", group: "safety", title: "Hazard Monitor", real: true,
      desc: "One weather-warning source for your area, with optional earthquake and NASA feeds under Advanced." },
    { id: "host_health", group: "safety", title: "Host Health", real: true, feature: "host_health",
      desc: "Home Assistant's own System Monitor readings for the machine Nova runs on — off by default." },
    { id: "anticipation_memory", group: "learning", title: "Anticipation & Memory", real: true,
      desc: "Cross-session memory window, continued conversation, and multi-satellite follow." },
    { id: "memory_curated", group: "learning", title: "Memory", real: true,
      desc: "Memory backend and how many memories are stored. Full review/edit lives on the Memory tab." },
    { id: "observer_tuning", group: "learning", title: "Observer Tuning", real: true,
      desc: "How cautious or talkative the proactive Observer is." },
    { id: "routine_learning", group: "learning", title: "Routine Learning", real: true,
      desc: "What Nova is allowed to learn from — doors, presence, button presses." },
    { id: "excluded_entities", group: "learning", title: "Excluded Entities", real: true,
      desc: "Entities, domains, or labels Nova should ignore entirely." },
    { id: "cameras", group: "cameras", title: "Cameras", real: true, feature: "cameras",
      desc: "Camera names, indoor/outdoor designation, and location overrides." },
    { id: "doorbell_training", group: "cameras", title: "Doorbell Training", real: true,
      desc: "Teach Nova to recognize regular visitors at the door." },
    { id: "home_layout", group: "home", title: "Home layout", real: true,
      desc: "Stories, garage and basement, which entity is each door, and the exit doors you leave by." },
    { id: "floor_plan_editor", group: "home", title: "Floor Plan Editor", real: true,
      desc: "Rooms, outdoor zones, property line, windows/doors/dormers, camera placement, a background image, and AI camera-coverage estimation." },
    { id: "wellbeing_context", group: "home", title: "Wellbeing Context", real: true, feature: "wellbeing",
      desc: "Whether wearable heart-rate/sleep data reaches Nova, and which providers." },
    { id: "character_research", group: "home", title: "Nova Character & Research", real: true,
      desc: "Banter level and the web-research backend (DuckDuckGo or self-hosted SearXNG)." },
    { id: "document_library", group: "home", title: "Document Library", real: true,
      desc: "Manuals and receipts Nova can search and cite from." },
  ];
  // ─── Home layout (Settings → Home & Extras) ─────────────────────────────
  // Stories, garage and basement, the door mapping and the exit doors. These
  // used to live on the Residence tab with the 3D house; the 3D house is gone
  // and they moved here unchanged. The saved keys are the same: door_mapping
  // slots feed home_doors.has_garage() and has_basement(), so the slot names
  // must never change.

  // Display only (8.26.0): hides garage words and parts, never a safety check.
  _hasGarage() { return !!(this._data()?.config?.has_garage); }
  // basement, utility (8.27.0): home_doors.home_features(). Display only.
  _hasFeature(name) { return !!(this._data()?.config?.home_features || {})[name]; }

  _homeLayoutCardBody() {
    const d = this._data() || {};
    const cfg = d.config || {};
    return `
      <div class="cfg-row">
        <label>Stories</label>
        <select class="cfg-field" data-cfg-key="home_stories">
          ${this._optSelect(["1", "1.5", "2", "3"].map(v => [v, v]), String(cfg.home_stories ?? "1.5"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Garage</label>
        <select class="cfg-field" data-cfg-key="garage_mode">
          ${this._optSelect([["auto", "Auto"], ["yes", "Yes"], ["no", "No"]], cfg.garage_mode || "auto")}
        </select>
        <span class="toggle-desc">Show garage settings and wording. Auto looks for a garage door or an area named Garage. This only changes what you see: Nova secures and checks every door the same way.</span>
      </div>
      ${cfg.has_garage ? `<div class="cfg-row">
        <label>Garage bays</label>
        <select class="cfg-field" data-cfg-key="garage_bays">
          ${this._optSelect(["0", "1", "2", "3", "4"].map(v => [v, v]), String(cfg.garage_bays ?? "3"))}
        </select>
      </div>` : ""}
      <div class="cfg-row">
        <label>Basement</label>
        <select class="cfg-field" data-cfg-key="basement_mode">
          ${this._optSelect([["auto", "Auto"], ["yes", "Yes"], ["no", "No"]], cfg.basement_mode || "auto")}
        </select>
        <span class="toggle-desc">Show the basement floor and its doors. Auto looks for an area or floor named Basement or Cellar, or a floor below ground level. This only changes what you see.</span>
      </div>
      <div class="mode-bind-head">Doors <span class="toggle-desc">map to your entities — blank = auto-detect by name</span></div>
      ${this._renderDoorMappingNew(d)}
      ${this._renderExitDoors(d)}`;
  }

  // Every slot, garage ones included. The keys never change; only labels and
  // which rows show do. "garage_rear" is shown as the back door (8.26.0).
  _doorSlots() {
    const bays = Math.max(0, Math.min(Number((this._data()?.config || {}).garage_bays) || 0, 8));
    const garage = [];
    for (let i = 1; i <= bays; i++) garage.push(["garage_" + i, "Garage Door " + i]);
    if (!bays) garage.push(["garage", "Garage Door"]);
    return [["front", "Front Door"], ...garage, ["garage_rear", "Back / Rear Door"], ["kitchen_garage", "Kitchen ↔ Garage"], ["cellar", "Cellar / Bulkhead"], ["basement", "Basement"]];
  }
  // Garage rows are hidden when there is no garage, and the Cellar /
  // Bulkhead and Basement rows when there is no basement (8.27.0). Their
  // saved values stay.
  _visibleDoorSlots() {
    const garage = this._hasGarage(), basement = this._hasFeature("basement");
    return this._doorSlots().filter(([slot]) => {
      if (/^garage(_[0-9]+)?$/.test(slot) || slot === "kitchen_garage") return garage;
      if (slot === "cellar" || slot === "basement") return basement;
      return true;
    });
  }
  _renderDoorMappingNew(d) {
    const map = (d.config && d.config.door_mapping) || {};
    const rows = this._visibleDoorSlots().map(([slot, label]) => `
      <div class="cfg-row">
        <label>${label}</label>
        <select class="door-map-sel-new" id="resDoorMap-${slot}" data-slot="${slot}">${this._doorEntityOptions(map[slot] || "")}</select>
      </div>`).join("");
    return rows;
  }
  // Exit doors the user picked (8.26.0). Display only: picking a door never
  // adds it to a safety check. Each row says which checks already cover it,
  // by the same rules those checks use (home_doors.safety_coverage).
  _exitDoorChecks() {
    return { lockdown: "Lockdown", night_sweep: "Night sweep", intrusion: "Intrusion", world_model: "House status" };
  }
  _exitDoorState(r) {
    if (r.state == null) return "Entity not found";
    const s = String(r.state).toLowerCase(), dom = String(r.entity_id || "").split(".")[0];
    if (s === "unknown" || s === "unavailable") return "Unavailable";
    if (dom === "lock") return s === "locked" ? "Locked" : s === "unlocked" ? "Unlocked" : this._esc(r.state);
    if (s === "on" || s === "open" || s === "opening") return "Open";
    if (s === "off" || s === "closed" || s === "closing") return "Closed";
    return this._esc(r.state);
  }
  _renderExitDoors(d) {
    const cfg = (d && d.config) || {};
    const status = Array.isArray(cfg.exit_door_status) ? cfg.exit_door_status : [];
    const names = this._exitDoorChecks();
    const rows = status.map((r, i) => {
      const checks = (r.checks || []).map(c => `<span class="new-pl-chip">${names[c] || this._esc(c)}</span>`).join("");
      const state = `<span class="toggle-desc">${this._exitDoorState(r)}</span>`;
      return `
      <div class="cfg-row res-exit-row">
        <label>${this._esc(r.name || r.entity_id)}</label>
        <span class="toggle-desc">${this._esc(this._entName(r.entity_id))}</span>
        ${state}
        <button class="mode-chip res-exit-remove" data-i="${i}" title="Remove">×</button>
      </div>
      <div class="toggle-desc res-exit-checks">${checks
        ? `Already checked by: ${checks}`
        : `Not in Nova's safety checks. To include it, give it a door or window device class in Home Assistant.`}</div>`;
    }).join("");
    return `
      <div class="mode-bind-head">Exit doors <span class="toggle-desc">doors you leave the house by. Nova adds nothing until you pick one.</span></div>
      ${rows}
      <div class="cfg-row res-exit-add">
        <select id="resExitPick">${this._exitDoorOptions(cfg)}</select>
        <input id="resExitName" type="text" maxlength="40" placeholder="Name (optional)">
        <button class="mode-chip" id="resExitAdd">Add another exit door</button>
      </div>`;
  }
  _exitDoorOptions(cfg) {
    const states = this._hass?.states || {};
    const taken = new Set((cfg.exit_doors || []).map(x => x.entity_id));
    const sugg = (cfg.exit_door_candidates || []).map(x => x.entity_id).filter(e => !taken.has(e));
    const all = Object.keys(states).filter(e => ["binary_sensor", "lock", "cover"].includes(e.split(".")[0])
      && !taken.has(e) && !sugg.includes(e)).sort();
    const opt = e => `<option value="${this._esc(e)}">${this._esc(this._entName(e))}</option>`;
    return `<option value="">— pick a door, lock, cover or sensor —</option>`
      + (sugg.length ? `<optgroup label="${this._esc(this._tx("Suggested"))}">${sugg.map(opt).join("")}</optgroup>` : "")
      + (all.length ? `<optgroup label="${this._esc(this._tx("All doors, locks, covers and sensors"))}">${all.map(opt).join("")}</optgroup>` : "");
  }
  async _saveExitDoors(list) {
    if (this._liveData?.config) this._liveData.config.exit_doors = list;
    await this._saveSetting("exit_doors", JSON.stringify(list));
  }
  // Wired from _wireSettings. The selects with data-cfg-key ride the generic
  // Settings autosave; the door mapping and exit doors save themselves.
  _wireHomeLayout() {
    const root = this.shadowRoot;
    const exitAdd = root.getElementById("resExitAdd");
    if (exitAdd && !exitAdd._wired) {
      exitAdd._wired = true;
      exitAdd.addEventListener("click", async () => {
        const eid = root.getElementById("resExitPick")?.value || "";
        if (!eid) return;            // nothing is added until the user picks
        const name = (root.getElementById("resExitName")?.value || "").trim().slice(0, 40);
        const list = [...((this._data()?.config || {}).exit_doors || [])];
        if (!list.some(x => x.entity_id === eid)) list.push({ entity_id: eid, name });
        await this._saveExitDoors(list);
      });
    }
    root.querySelectorAll(".res-exit-remove").forEach(btn => {
      if (btn._wired) return;
      btn._wired = true;
      btn.addEventListener("click", async () => {
        const i = Number(btn.getAttribute("data-i"));
        const list = [...((this._data()?.config || {}).exit_doors || [])];
        list.splice(i, 1);
        await this._saveExitDoors(list);
      });
    });
    this._doorSlots().forEach(([slot]) => {
      const ds = root.getElementById("resDoorMap-" + slot);
      if (ds && !ds._wired) {
        ds._wired = true;
        ds.addEventListener("change", async () => {
          const cfg = this._data()?.config || {};
          const map = { ...(cfg.door_mapping || {}) };
          if (ds.value) map[slot] = ds.value; else delete map[slot];
          if (this._liveData?.config) this._liveData.config.door_mapping = map;
          try { await this._hass.callWS({ type: "nova/update_config", key: "door_mapping", value: JSON.stringify(map) }); } catch (_) {}
        });
      }
    });
  }
  // ─── Voice Phrases (Settings → Voice & Speakers, 8.32.0) ───────────────
  // Things you say, linked to a script, automation, scene or shell command.
  // Its own websocket commands (nova/voice_phrases_*), never the generic
  // data-cfg-key autosave: the backend checks every entry. The list and the
  // editor redraw only #vpBody, so the rest of Settings keeps its state.

  static VP_KINDS = [
    ["script", "Script"], ["automation", "Automation"], ["scene", "Scene"],
    ["shell", "Shell command"], ["advanced", "Advanced"],
  ];
  static VP_SERVICE = { script: "script.turn_on", automation: "automation.trigger", scene: "scene.turn_on" };

  _voicePhrasesCardBody() {
    const vp = this._vp;
    const on = !vp || vp.enabled !== false;
    return `
      <div class="stub-body">Say something, and Nova runs a script, an automation, a scene or a shell command.</div>
      <div class="cfg-row">
        <label>Voice phrases</label>
        <button class="toggle-btn ${on ? "on" : "off"}" id="vpEnabled">${on ? "ON" : "OFF"}</button>
      </div>
      <div id="vpBody">${this._vpBodyHtml()}</div>`;
  }

  async _fetchVoicePhrases() {
    if (!this._hass) return;
    try {
      this._vp = await this._hass.callWS({ type: "nova/voice_phrases_list" });
    } catch (_) { this._vp = { error: true }; }
    this._vpRedraw();
  }

  _vpRedraw() {
    const root = this.shadowRoot;
    const host = root?.getElementById("vpBody");
    if (host) this._setHtml(host, this._vpBodyHtml());
    const sw = root?.getElementById("vpEnabled");
    if (sw && this._vp && !this._vp.error) {
      const on = this._vp.enabled !== false;
      sw.classList.toggle("on", on);
      sw.classList.toggle("off", !on);
      this._setText(sw, on ? "ON" : "OFF");
    }
  }

  _vpBodyHtml() {
    const vp = this._vp;
    if (!vp) return `<div class="stub-body">Loading voice phrases…</div>`;
    if (vp.error) return `<div class="stub-body">Couldn't load voice phrases. Restart Home Assistant after updating, then reopen Settings.</div>`;
    if (this._vpEdit) return this._vpEditorHtml();
    const entries = Array.isArray(vp.entries) ? vp.entries : [];
    const asks = new Set(vp.asks_first || []);
    const rows = entries.map(e => {
      const pid = this._esc(e.phrase_id);
      const first = (e.phrases || [])[0] || "";
      const on = e.enabled !== false;
      return `
      <div class="cfg-row vp-row">
        <label>${this._esc(e.name)}</label>
        <span class="toggle-desc">“${this._esc(first)}” → ${this._esc(this._vpTargetText(e))}</span>
        <button class="toggle-btn ${on ? "on" : "off"} vp-toggle" data-pid="${pid}">${on ? "ON" : "OFF"}</button>
        <button class="mode-chip vp-edit" data-pid="${pid}">Edit</button>
        <button class="mode-chip vp-del" data-pid="${pid}">Delete</button>
      </div>
      ${asks.has(e.phrase_id) ? `<div class="toggle-desc vp-note"><span>Asks first, because of Voice Confirmation.</span> <button class="vp-link" data-goto="voice_confirmation">Voice Confirmation settings</button></div>` : ""}`;
    }).join("");
    return `
      ${rows || `<div class="stub-body">No voice phrases yet.</div>`}
      <div class="cfg-row"><button class="mode-chip" id="vpAdd">+ Add a voice phrase</button></div>
      <div class="mode-bind-head">Try a sentence</div>
      <div class="cfg-row">
        <input id="vpTry" class="cfg-field" style="flex:1" type="text" maxlength="500" placeholder="e.g. Nova, the house is cold" autocomplete="off">
        <button class="mode-chip" id="vpTryBtn">Try</button>
      </div>
      <div class="stub-body" id="vpTryResult"></div>
      <div class="mode-bind-head">Good to know</div>
      <div class="toggle-desc">Your phrases come before Nova's own built in commands.</div>
      <div class="toggle-desc">Home Assistant's own sentence triggers with the same words win. So does anything Home Assistant answers itself when "Prefer handling commands locally" is on.</div>
      <div class="toggle-desc">Unlock, open and disarm still need a tap on your phone.</div>
      <div class="toggle-desc">A shell command runs without asking. It can be made to ask first: see Voice phrases in Nova's docs.</div>`;
  }

  _vpTargetText(e) {
    const action = e.action || {};
    const ids = this._vpIds(action);
    return ids.length ? ids.join(", ") : String(action.service || "");
  }

  _vpIds(action) {
    const t = ((action || {}).target || {}).entity_id;
    return Array.isArray(t) ? t : (t ? [t] : []);
  }

  // The editor's state from a saved entry: the panel shorthand when the
  // entry fits one, Advanced otherwise.
  _vpEditFrom(e) {
    const action = (e && e.action) || {};
    const service = String(action.service || "");
    const ids = this._vpIds(action);
    const data = action.data && Object.keys(action.data).length ? JSON.stringify(action.data, null, 2) : "";
    // A new entry starts as a Script.
    let kind = e ? "advanced" : "script", entity = "", shell = "";
    const simple = Object.entries(NovaPanel.VP_SERVICE).find(([, svc]) => svc === service);
    if (!e) { /* a new entry */ }
    else if (simple && ids.length === 1 && !data) { kind = simple[0]; entity = ids[0]; }
    else if (service.startsWith("shell_command.") && !ids.length && !data) { kind = "shell"; shell = service.slice(14); }
    return {
      phrase_id: e ? e.phrase_id : null,
      name: e ? e.name || "" : "",
      phrases: e && (e.phrases || []).length ? [...e.phrases] : [""],
      match: e && e.match === "exact" ? "exact" : "flexible",
      allow_ai: !!(e && e.allow_ai),
      enabled: !e || e.enabled !== false,
      reply: e ? e.reply || "" : "",
      kind, entity, shell,
      service: kind === "advanced" ? service : "",
      entities: kind === "advanced" ? ids.join(", ") : "",
      data,
      errors: "", warnings: [],
    };
  }

  _vpEntityOptions(domain, current) {
    const states = this._hass?.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith(domain + ".")).sort();
    if (current && !ids.includes(current)) ids.unshift(current);
    const opts = ids.map(id => [id, `${states[id]?.attributes?.friendly_name || id} (${id})`]);
    return `<option value="">— pick one —</option>` + this._optSelect(opts, current);
  }

  _vpShellOptions(current) {
    const names = Object.keys(this._hass?.services?.shell_command || {}).sort();
    if (current && !names.includes(current)) names.unshift(current);
    return `<option value="">— pick one —</option>` + this._optSelect(names.map(n => [n, n]), current);
  }

  _vpEditorHtml() {
    const ed = this._vpEdit;
    const kinds = NovaPanel.VP_KINDS;
    const phrases = ed.phrases.map((p, i) => `
      <div class="cfg-row">
        <input class="cfg-field vp-phrase" style="flex:1" type="text" maxlength="80" value="${this._esc(p)}" placeholder="e.g. the house is cold" autocomplete="off">
        ${ed.phrases.length > 1 ? `<button class="mode-chip vp-phrase-del" data-i="${i}" title="Remove this phrase">×</button>` : ""}
      </div>`).join("");
    let what = "";
    if (ed.kind === "shell") {
      what = `
      <div class="cfg-row"><label>Shell command</label>
        <select class="cfg-field" id="vpShell">${this._vpShellOptions(ed.shell)}</select></div>`;
    } else if (ed.kind === "advanced") {
      what = `
      <div class="cfg-row"><label>Service</label>
        <input class="cfg-field" style="flex:1" id="vpService" type="text" value="${this._esc(ed.service)}" placeholder="e.g. climate.set_temperature" autocomplete="off"></div>
      <div class="cfg-row"><label>Entity ids</label>
        <input class="cfg-field" style="flex:1" id="vpEntities" type="text" value="${this._esc(ed.entities)}" placeholder="e.g. climate.hall, climate.landing" autocomplete="off"></div>
      <div class="cfg-row"><label>Data (JSON)</label>
        <textarea class="cfg-field vp-json" id="vpData" rows="4" placeholder='{"temperature": 21}'>${this._esc(ed.data)}</textarea></div>`;
    } else {
      what = `
      <div class="cfg-row"><label>Run</label>
        <select class="cfg-field" id="vpEntity">${this._vpEntityOptions(ed.kind, ed.entity)}</select></div>`;
    }
    const shell = ed.kind === "shell";
    return `
      <div class="vp-editor">
        <div class="mode-bind-head">${ed.phrase_id ? "Edit voice phrase" : "New voice phrase"}</div>
        <div class="cfg-row"><label>Name</label>
          <input class="cfg-field" style="flex:1" id="vpName" type="text" maxlength="60" value="${this._esc(ed.name)}" placeholder="e.g. House is cold" autocomplete="off"></div>
        <div class="mode-bind-head">What you say</div>
        <div class="toggle-desc">Up to 10 ways of saying it.</div>
        ${phrases}
        ${ed.phrases.length < 10 ? `<div class="cfg-row"><button class="mode-chip" id="vpPhraseAdd">+ Add another way to say it</button></div>` : ""}
        <div class="cfg-row"><label>Match</label>
          <select class="cfg-field" id="vpMatch">${this._optSelect([["flexible", "Flexible: small differences are fine"], ["exact", "Exact: the same words only"]], ed.match)}</select></div>
        <div class="mode-bind-head">What to run</div>
        <div class="cfg-row"><label>Type</label>
          <select class="cfg-field" id="vpKind">${this._optSelect(kinds, ed.kind)}</select></div>
        ${what}
        ${shell ? "" : `<div class="cfg-row"><label>Let the AI pick this by meaning</label>
          <button class="toggle-btn ${ed.allow_ai ? "on" : "off"}" id="vpAllowAi">${ed.allow_ai ? "ON" : "OFF"}</button></div>
        <div class="toggle-desc">When the words don't match, the AI may still run this if what you said clearly means it.</div>`}
        <div class="cfg-row"><label>Reply</label>
          <input class="cfg-field" style="flex:1" id="vpReply" type="text" maxlength="200" value="${this._esc(ed.reply)}" placeholder="e.g. Warming the house up{addr}." autocomplete="off"></div>
        <div class="toggle-desc">${this._tHtml("Leave it empty for \"Done\". {addr} adds \", sir\" or however Nova addresses you. {honorific} adds just that word.")}</div>
        ${ed.errors ? `<div class="stub-body diag-warn" id="vpErrors">${this._esc(ed.errors)}</div>` : ""}
        <div class="cfg-row">
          <button class="mode-chip" id="vpSave">Save</button>
          <button class="mode-chip" id="vpCancel">Cancel</button>
        </div>
      </div>`;
  }

  // Read what is on screen into the editor state, before a redraw.
  _vpReadEditor() {
    const root = this.shadowRoot, ed = this._vpEdit;
    if (!root || !ed) return;
    const val = id => root.getElementById(id)?.value;
    ed.name = val("vpName") ?? ed.name;
    ed.phrases = [...root.querySelectorAll(".vp-phrase")].map(el => el.value);
    ed.match = val("vpMatch") ?? ed.match;
    ed.reply = val("vpReply") ?? ed.reply;
    if (ed.kind === "shell") ed.shell = val("vpShell") ?? ed.shell;
    else if (ed.kind === "advanced") {
      ed.service = val("vpService") ?? ed.service;
      ed.entities = val("vpEntities") ?? ed.entities;
      ed.data = val("vpData") ?? ed.data;
    } else ed.entity = val("vpEntity") ?? ed.entity;
  }

  // The entry the backend checks. Problems the panel can see first are
  // returned as text; everything else the backend checks again.
  _vpBuildEntry() {
    const ed = this._vpEdit;
    const entry = {
      name: ed.name.trim(),
      phrases: ed.phrases.map(p => p.trim()).filter(Boolean),
      match: ed.match,
      allow_ai: ed.kind === "shell" ? false : !!ed.allow_ai,
      enabled: ed.enabled,
      reply: ed.reply.trim(),
    };
    if (ed.phrase_id) entry.phrase_id = ed.phrase_id;
    if (ed.kind === "shell") {
      if (!ed.shell) return { error: this._t("Pick a shell command.") };
      entry.action = { service: "shell_command." + ed.shell, data: {} };
    } else if (ed.kind === "advanced") {
      let data = {};
      if (ed.data.trim()) {
        try { data = JSON.parse(ed.data); } catch (_) { return { error: this._t("The data is not valid JSON.") }; }
      }
      const ids = ed.entities.split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
      entry.action = { service: ed.service.trim(), data };
      if (ids.length) entry.action.target = { entity_id: ids.length === 1 ? ids[0] : ids };
    } else {
      if (!ed.entity) return { error: this._t("Pick what to run.") };
      entry.action = { service: NovaPanel.VP_SERVICE[ed.kind], target: { entity_id: ed.entity }, data: {} };
    }
    return { entry };
  }

  async _vpSave(entry) {
    try {
      const res = await this._hass.callWS({ type: "nova/voice_phrases_save", entry });
      return { ok: true, warnings: res?.warnings || [] };
    } catch (err) {
      return { ok: false, error: String(err?.message || err || this._t("Couldn't save.")) };
    }
  }

  _vpFind(pid) {
    return ((this._vp && this._vp.entries) || []).find(e => e.phrase_id === pid);
  }

  _vpShowWarnings(warnings) {
    if (!warnings || !warnings.length) return;
    const host = this.shadowRoot?.getElementById("vpTryResult");
    if (host) this._setText(host, warnings.join(" "));
  }

  // Wired from _wireSettings on every Settings render. One listener on the
  // card, so redraws of #vpBody need no rewiring.
  _wireVoicePhrases() {
    const root = this.shadowRoot;
    const card = root?.getElementById("settings-card-voice_phrases");
    if (!card) return;
    if (!this._vpFetchedOnce) {
      this._vpFetchedOnce = true;
      this._fetchVoicePhrases();
    }
    card.addEventListener("click", ev => this._vpClick(ev));
    card.addEventListener("change", ev => {
      if (ev.target?.id === "vpKind" && this._vpEdit) {
        this._vpReadEditor();
        this._vpEdit.kind = ev.target.value;
        if (this._vpEdit.kind === "shell") this._vpEdit.allow_ai = false;
        this._vpRedraw();
      }
    });
    card.addEventListener("keydown", ev => {
      if (ev.target?.id === "vpTry" && ev.key === "Enter") { ev.preventDefault(); this._vpTry(); }
    });
  }

  async _vpTry() {
    const root = this.shadowRoot;
    const text = (root?.getElementById("vpTry")?.value || "").trim();
    const out = root?.getElementById("vpTryResult");
    if (!out || !text || !this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/voice_phrases_test", text });
      const m = res && res.match;
      if (!m) {
        this._setText(out, this._vp && this._vp.enabled === false
          ? "Voice phrases are off, so nothing matches."
          : "No phrase matches. Nothing was run.");
      } else {
        this._setText(out, m.tier === "exact"
          ? this._t("Matches \"{name}\" exactly. Nothing was run.", { name: m.name })
          : this._t("Matches \"{name}\" (flexible). Nothing was run.", { name: m.name }));
      }
    } catch (_) {
      this._setText(out, "Couldn't test that sentence.");
    }
  }

  async _vpClick(ev) {
    const btn = ev.target?.closest?.("button");
    if (!btn || !this._hass) return;
    const pid = btn.getAttribute("data-pid");
    if (btn.id === "vpEnabled") {
      const on = !(this._vp && this._vp.enabled !== false);
      try {
        await this._hass.callWS({ type: "nova/voice_phrases_set_enabled", enabled: on });
      } catch (err) { console.error("Nova: voice phrases switch failed", err); }
      await this._fetchVoicePhrases();
    } else if (btn.id === "vpAdd") {
      this._vpEdit = this._vpEditFrom(null);
      this._vpRedraw();
    } else if (btn.classList.contains("vp-edit")) {
      const e = this._vpFind(pid);
      if (e) { this._vpEdit = this._vpEditFrom(e); this._vpRedraw(); }
    } else if (btn.classList.contains("vp-del")) {
      const e = this._vpFind(pid);
      if (!e || !window.confirm(this._t("Delete the voice phrase \"{name}\"?", { name: e.name }))) return;
      try {
        await this._hass.callWS({ type: "nova/voice_phrases_delete", phrase_id: pid });
      } catch (err) { console.error("Nova: voice phrase delete failed", err); }
      await this._fetchVoicePhrases();
    } else if (btn.classList.contains("vp-toggle")) {
      const e = this._vpFind(pid);
      if (!e) return;
      const res = await this._vpSave({ ...e, enabled: e.enabled === false });
      if (!res.ok) console.error("Nova: voice phrase toggle failed", res.error);
      await this._fetchVoicePhrases();
    } else if (btn.classList.contains("vp-link")) {
      const target = this.shadowRoot?.getElementById("settings-card-" + btn.getAttribute("data-goto"));
      if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (btn.id === "vpTryBtn") {
      await this._vpTry();
    } else if (btn.id === "vpPhraseAdd") {
      this._vpReadEditor();
      if (this._vpEdit.phrases.length < 10) this._vpEdit.phrases.push("");
      this._vpRedraw();
    } else if (btn.classList.contains("vp-phrase-del")) {
      this._vpReadEditor();
      this._vpEdit.phrases.splice(Number(btn.getAttribute("data-i")), 1);
      if (!this._vpEdit.phrases.length) this._vpEdit.phrases.push("");
      this._vpRedraw();
    } else if (btn.id === "vpAllowAi") {
      this._vpReadEditor();
      this._vpEdit.allow_ai = !this._vpEdit.allow_ai;
      this._vpRedraw();
    } else if (btn.id === "vpCancel") {
      this._vpEdit = null;
      this._vpRedraw();
    } else if (btn.id === "vpSave") {
      this._vpReadEditor();
      const built = this._vpBuildEntry();
      if (built.error) { this._vpEdit.errors = built.error; this._vpRedraw(); return; }
      btn.disabled = true;
      const res = await this._vpSave(built.entry);
      if (!res.ok) {
        this._vpEdit.errors = res.error;
        this._vpRedraw();
        return;
      }
      this._vpEdit = null;
      await this._fetchVoicePhrases();
      this._vpShowWarnings(res.warnings);
    }
  }
  // ─── Voice ID (Settings → Voice & Speakers, 8.35.0, 8.36.0) ───────────
  // The app's address and token, the fallback, and the People list: record
  // or delete each person's voice in the Nova Voice ID app. Voice data lives
  // only in the app; Nova keeps none. Its own websocket commands
  // (nova/voice_id_*), except the address and fallback, which use the
  // generic data-cfg-key autosave. The list and the recording progress
  // redraw only #vidPeople and #vidRecord.

  _voiceIdCardBody() {
    const cfg = this._data()?.config || {};
    const st = cfg.voice_id_status || {};
    const fallback = cfg.voice_id_fallback_stt || "";
    const sttOpts = [["", "— none —"], ...(cfg.stt_entities || []).map(e => [e.entity_id, e.name])];
    if (fallback && !sttOpts.some(o => o[0] === fallback)) sttOpts.push([fallback, fallback]);
    const meta = [st.version ? `v${st.version}` : "", (st.models || []).join(", ")].filter(Boolean).join(" · ");
    const state = st.state === "connected" ? `<span class="vid-ok">Connected</span>`
      : st.state === "unreachable" ? `<span class="vid-bad">Unreachable</span>`
      : st.state === "checking" ? `<span>Checking…</span>`
      : st.state === "not_set" ? `<span>Not set</span>`
      : `<span>Not loaded</span>`;
    // The panel data's session, unless this tab is already polling a newer one
    // (a recording may have been started from another browser).
    if (!this._vidPollTimer && cfg.voice_id_enrol
        && (!this._vidSession || this._vidSession.state !== "active" || cfg.voice_id_enrol.state === "active")) {
      this._vidSession = cfg.voice_id_enrol;
    }
    if (!this._vidSession) this._vidSession = { state: "idle" };
    const sats = (cfg.satellites || []).map(s => [s.entity_id, s.area ? `${s.name} (${s.area})` : s.name]);
    if (!sats.some(o => o[0] === this._vidSatellite)) this._vidSatellite = sats.length ? sats[0][0] : "";
    return `
      <div class="stub-body">Set Nova Voice ID as the speech to text in your voice assistant (Settings → Voice assistants). If the app isn't running, Nova uses the fallback.</div>
      <div class="cfg-row">
        <label>Status</label>
        <div>${state}${meta ? ` <span class="toggle-desc">${this._esc(meta)}</span>` : ""}</div>
      </div>
      <div class="cfg-row">
        <label>App address</label>
        <input class="cfg-field" type="text" data-cfg-key="voice_id_uri" value="${this._esc(cfg.voice_id_uri || "")}">
      </div>
      <div class="toggle-desc">Shown in the Nova Voice ID app's log</div>
      <div class="cfg-row">
        <label>App token</label>
        <div class="vid-token-row">
          <input class="vid-token" id="vidToken" type="password" autocomplete="off" spellcheck="false" value="">
          <button class="mode-chip" id="vidTokenEye" title="Show or hide the token">👁</button>
        </div>
      </div>
      <div class="toggle-desc">${cfg.voice_id_token_set ? "Saved. Filled in by the app when Nova finds it, unless you typed your own." : "Copy it from the app's Configuration tab."}</div>
      <div class="cfg-row">
        <label>Fallback speech to text</label>
        <select class="cfg-field" data-cfg-key="voice_id_fallback_stt">${this._optSelect(sttOpts, fallback)}</select>
      </div>
      <div class="cfg-row">
        <label>Use voice to tell who is speaking</label>
        <button class="toggle-btn ${cfg.voice_id_identity ? "on" : "off"}" data-cfg-key="voice_id_identity" data-cfg-val="${cfg.voice_id_identity ? "false" : "true"}">${cfg.voice_id_identity ? "ON" : "OFF"}</button>
      </div>
      <div class="toggle-desc">Off by default. Voice is a hint about who is talking, never a key: unlocking, opening and disarming still need your phone. Leave it off while the VOICE lines in the Logs tab collect scores.</div>
      <div class="cfg-row">
        <label>Improve voices from everyday use</label>
        <button class="toggle-btn ${cfg.voice_id_learning ? "on" : "off"}" data-cfg-key="voice_id_learning" data-cfg-val="${cfg.voice_id_learning ? "false" : "true"}" ${cfg.voice_id_identity ? "" : "disabled"}>${cfg.voice_id_learning ? "ON" : "OFF"}</button>
      </div>
      <div class="toggle-desc">${cfg.voice_id_identity
        ? this._tHtml("Off by default. When Nova is very sure who spoke, it adds that sample to their voice, so it keeps up with how they sound. Needs the Nova Voice ID app 0.3.0.")
        : this._tHtml("Turn on \"Use voice to tell who is speaking\" first.")}</div>
      <div class="mode-bind-head">People</div>
      <div class="stub-body">Record each person on every satellite: rooms sound different. Voice data lives only in the Nova Voice ID app, never in Nova.</div>
      <div class="cfg-row">
        <label>Record on</label>
        ${sats.length
          ? `<select id="vidSatellite">${this._optSelect(sats, this._vidSatellite)}</select>`
          : `<span class="toggle-desc">No satellites found.</span>`}
      </div>
      <div id="vidRecord">${this._vidRecordHtml()}</div>
      <div id="vidPeople">${this._vidPeopleHtml()}</div>
      <div class="stub-body" id="vidMsg"></div>
      <div class="cfg-row"><button class="mode-chip vid-danger" id="vidDeleteAll">Delete all voice data</button></div>`;
  }

  _vidRecordHtml() {
    const s = this._vidSession || { state: "idle" };
    const v = { name: this._esc(s.name || ""), done: Number(s.committed) || 0, needed: Number(s.needed) || 8 };
    if (s.state === "active") {
      return `<div class="vid-progress"><b>${this._tHtml("Recording {name}: {done} of {needed}", v)}</b>
        <button class="mode-chip" id="vidCancel">Cancel</button></div>
        <div class="toggle-desc">Say the wake word, then the sentence the satellite asks for.</div>`;
    }
    if (s.state === "done") return `<div class="vid-progress vid-ok">${this._tHtml("{name}: all {needed} samples saved.", v)}</div>`;
    if (s.state === "cancelled") return `<div class="vid-progress">${this._tHtml("Recording cancelled. {done} of {needed} samples were saved.", v)}</div>`;
    if (s.state === "expired") return `<div class="vid-progress">${this._tHtml("Recording stopped after 5 minutes. {done} of {needed} samples were saved.", v)}</div>`;
    if (s.state === "error") return `<div class="vid-progress vid-bad">${this._esc(this._tx(s.message || "Recording stopped after an error."))}</div>`;
    return "";
  }

  _vidPeopleHtml() {
    const cfg = this._data()?.config || {};
    const res = this._vidPeople;
    if (!cfg.voice_id_uri) return `<div class="toggle-desc">Set the app address to see who has a voice saved.</div>`;
    if (!res) return `<div class="toggle-desc">Loading people…</div>`;
    if (!res.ok) return `<div class="toggle-desc vid-bad">${this._esc(this._tx(res.error || "Couldn't reach the app."))}</div>`;
    const hidden = new Set(Array.isArray(cfg.voice_id_hidden_people) ? cfg.voice_id_hidden_people : []);
    const recording = (this._vidSession || {}).state === "active";
    const row = (p, isHidden) => {
      const person = this._esc(p.person);
      const recorded = Number(p.enrolled ?? p.samples) || 0;
      const learned = Number(p.learned) || 0;
      const count = this._tHtml("{recorded} recorded, {learned} learned", { recorded, learned });
      const when = p.updated ? ` · ${this._tHtml("updated {date}", { date: this._esc(new Date(p.updated * 1000).toLocaleDateString()) })}` : "";
      return `
      <div class="cfg-row vid-person">
        <label>${this._esc(p.name)}</label>
        <span class="toggle-desc">${count}${when}</span>
        <div class="vid-actions">
          ${isHidden ? "" : `<button class="mode-chip vid-rec" data-person="${person}" ${recording ? "disabled" : ""}>Record voice</button>`}
          <button class="mode-chip vid-del" data-person="${person}" data-name="${this._esc(p.name)}" ${p.samples && !recording ? "" : "disabled"}>Delete voice</button>
          ${learned ? `<button class="mode-chip vid-del-learned" data-person="${person}" data-name="${this._esc(p.name)}" ${recording ? "disabled" : ""}>Remove learned</button>` : ""}
          <button class="mode-chip vid-hide" data-person="${person}">${isHidden ? "Show" : "Hide"}</button>
        </div>
      </div>`;
    };
    const people = res.people || [];
    const shown = people.filter(p => !hidden.has(p.person));
    const kept = people.filter(p => hidden.has(p.person));
    if (!people.length) return `<div class="toggle-desc">No people in Home Assistant yet.</div>`;
    return `${shown.map(p => row(p, false)).join("")}
      ${kept.length ? `<div class="cfg-row"><button class="mode-chip" id="vidShowHidden">${this._vidShowHidden
        ? this._tHtml("Hide the hidden people")
        : this._tHtml("Show hidden people ({count})", { count: kept.length })}</button></div>` : ""}
      ${this._vidShowHidden ? kept.map(p => row(p, true)).join("") : ""}`;
  }

  _vidRedraw() {
    const root = this.shadowRoot;
    const people = root?.getElementById("vidPeople");
    if (people) this._setHtml(people, this._vidPeopleHtml());
    const rec = root?.getElementById("vidRecord");
    if (rec) this._setHtml(rec, this._vidRecordHtml());
  }

  _vidSay(text) {
    const out = this.shadowRoot?.getElementById("vidMsg");
    if (out) this._setText(out, text || "");
  }

  async _fetchVoiceIdPeople() {
    if (!this._hass) return;
    try {
      this._vidPeople = await this._hass.callWS({ type: "nova/voice_id_profiles" });
    } catch (_) {
      this._vidPeople = { ok: false, error: "Couldn't load the People list. Restart Home Assistant after updating, then reopen Settings." };
    }
    this._vidRedraw();
  }

  async _loadVoiceIdToken() {
    const cfg = this._data()?.config || {};
    const field = this.shadowRoot?.getElementById("vidToken");
    if (!field || !cfg.voice_id_token_set || !this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/voice_id_token_get" });
      // Never over what the user is typing.
      if (res && res.ok && field.isConnected && !field.value && field !== this.shadowRoot.activeElement) {
        field.value = res.token || "";
      }
    } catch (_) { /* the field just stays empty */ }
  }

  _vidPollSoon() {
    if (this._vidPollTimer) return;
    this._vidPollTimer = setTimeout(async () => {
      this._vidPollTimer = null;
      if (!this.isConnected || !this._hass) return;
      const before = (this._vidSession || {}).state;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record_status" });
        if (res && res.session) this._vidSession = res.session;
      } catch (_) {
        this._vidPollSoon();               // try again; one failed poll never freezes it
        return;
      }
      this._vidRedraw();
      if (this._vidSession.state === "active") this._vidPollSoon();
      else if (before === "active") this._fetchVoiceIdPeople();
    }, 2000);
  }

  _wireVoiceId() {
    const root = this.shadowRoot;
    const card = root?.getElementById("settings-card-voice_id");
    if (!card) return;
    if (!this._vidFetchedOnce) {
      this._vidFetchedOnce = true;
      this._fetchVoiceIdPeople();
    }
    this._loadVoiceIdToken();
    if ((this._vidSession || {}).state === "active") this._vidPollSoon();
    card.addEventListener("click", ev => this._vidClick(ev));
    card.addEventListener("change", async ev => {
      if (ev.target?.id === "vidSatellite") this._vidSatellite = ev.target.value;
      if (ev.target?.id === "vidToken") {
        try {
          const res = await this._hass.callWS({ type: "nova/voice_id_token_set", token: ev.target.value.trim() });
          this._vidSay(res.ok ? (res.set ? this._t("Token saved.") : this._t("Token cleared.")) : res.error);
          if (res.ok) await this._fetchVoiceIdPeople();
        } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      }
    });
  }

  async _vidClick(ev) {
    const btn = ev.target?.closest?.("button");
    if (!btn || !this._hass) return;
    const person = btn.getAttribute("data-person");
    if (btn.id === "vidTokenEye") {
      const field = this.shadowRoot?.getElementById("vidToken");
      if (field) field.type = field.type === "password" ? "text" : "password";
    } else if (btn.classList.contains("vid-rec")) {
      if (!this._vidSatellite) { this._vidSay(this._t("Pick a satellite first.")); return; }
      this._vidSay("");
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record", person, satellite: this._vidSatellite });
        if (!res.ok) { this._vidSay(res.error); return; }
        this._vidSession = res.session;
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); return; }
      this._vidRedraw();
      this._vidPollSoon();
    } else if (btn.id === "vidCancel") {
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record_cancel" });
        this._vidSession = res.session;
      } catch (_) { /* the poll picks it up */ }
      this._vidRedraw();
      this._fetchVoiceIdPeople();
    } else if (btn.classList.contains("vid-del")) {
      const name = btn.getAttribute("data-name") || person;
      if (!window.confirm(this._t("Delete {name}'s voice from the Nova Voice ID app? Their recordings are deleted too.", { name }))) return;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_delete", person });
        this._vidSay(!res.ok ? res.error
          : (res.deleted || []).length ? this._t("Deleted {name}'s voice.", { name })
          : this._t("The app had no voice saved for {name}.", { name }));
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      await this._fetchVoiceIdPeople();
    } else if (btn.classList.contains("vid-del-learned")) {
      const name = btn.getAttribute("data-name") || person;
      if (!window.confirm(this._t("Remove the samples Nova learned for {name}? Their recordings stay.", { name }))) return;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_delete", person, source: "learned" });
        this._vidSay(!res.ok ? res.error
          : this._t("Removed {count} learned samples for {name}.", { count: res.removed ?? 0, name }));
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      await this._fetchVoiceIdPeople();
    } else if (btn.id === "vidDeleteAll") {
      if (!window.confirm(this._t("Delete every voice saved in the Nova Voice ID app, for everyone?"))) return;
      if (!window.confirm(this._t("This can't be undone. Delete all voice data now?"))) return;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_delete", all: true });
        this._vidSay(!res.ok ? res.error : this._t("Deleted all voice data ({count} people).", { count: (res.deleted || []).length }));
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      await this._fetchVoiceIdPeople();
    } else if (btn.classList.contains("vid-hide")) {
      const cfg = this._data()?.config || {};
      const hidden = new Set(Array.isArray(cfg.voice_id_hidden_people) ? cfg.voice_id_hidden_people : []);
      if (hidden.has(person)) hidden.delete(person); else hidden.add(person);
      await this._saveSetting("voice_id_hidden_people", JSON.stringify([...hidden].sort()));
    } else if (btn.id === "vidShowHidden") {
      this._vidShowHidden = !this._vidShowHidden;
      this._vidRedraw();
    }
  }

  _htmlSettings() {
    const groupsNav = NovaPanel.SETTINGS_GROUPS.map(g =>
      `<button class="settings-nav-btn${this._settingsSection === g.id ? " active" : ""}" data-settings-section="${g.id}">${this._esc(g.label)}</button>`
    ).join("");
    const cards = NovaPanel.SETTINGS_CARDS.map(c => this._settingsCardHtml(c)).join("");
    return `
        <div class="settings-toolbar">
          <input type="search" id="settingsSearch" class="settings-search" placeholder="Search settings — try “camera” or “sleep”…" value="${this._esc(this._settingsSearch)}">
          <nav class="settings-nav">${groupsNav}</nav>
        </div>
        <div class="settings-grid" id="settingsGrid" data-section="${this._settingsSearch ? "" : this._settingsSection}">${cards}</div>
    `;
  }

  _settingsCardHtml(c) {
    const body = c.real
      ? (c.id === "general" ? this._generalCardBody()
        : c.id === "person_honorifics" ? this._personHonorificsCardBody()
        : c.id === "room_speakers" ? this._roomSpeakersCardBody()
        : c.id === "home_layout" ? this._homeLayoutCardBody()
        : c.id === "ai_models" ? this._aiModelsCardBody()
        : c.id === "briefings" ? this._briefingsCardBody()
        : c.id === "voice_phrases" ? this._voicePhrasesCardBody()
        : c.id === "voice_confirmation" ? this._voiceConfirmationCardBody()
        : c.id === "voice_id" ? this._voiceIdCardBody()
        : c.id === "satellite_speaker" ? this._satelliteSpeakerCardBody()
        : c.id === "announcement_speakers" ? this._announcementSpeakersCardBody()
        : c.id === "notifications" ? this._notificationsCardBody()
        : c.id === "security_alarm" ? this._securityAlarmCardBody()
        : c.id === "sentinel_rules" ? this._sentinelRulesCardBody()
        : c.id === "hazard_monitor" ? this._hazardMonitorCardBody()
        : c.id === "host_health" ? this._hostHealthCardBody()
        : c.id === "anticipation_memory" ? this._anticipationMemoryCardBody()
        : c.id === "memory_curated" ? this._memoryCardBody()
        : c.id === "observer_tuning" ? this._observerTuningCardBody()
        : c.id === "routine_learning" ? this._routineLearningCardBody()
        : c.id === "excluded_entities" ? this._excludedEntitiesCardBody()
        : c.id === "cameras" ? this._camerasCardBody()
        : c.id === "doorbell_training" ? this._doorbellTrainingCardBody()
        : c.id === "floor_plan_editor" ? this._floorPlanEditorCardBody()
        : c.id === "wellbeing_context" ? this._wellbeingContextCardBody()
        : c.id === "character_research" ? this._characterResearchCardBody()
        : c.id === "document_library" ? this._documentLibraryCardBody()
        : "")
      : `<div class="stub-body">${this._esc(c.desc)}<br><span class="stub-where">Not built here yet — see Settings → Devices &amp; Services → Nova → Configure.</span></div>`;
    return `
      <div class="panel settings-card" id="settings-card-${c.id}" data-settings-group="${c.group}" data-search="${this._esc((c.title + " " + c.desc).toLowerCase())}">
        <div class="panel-head">
          <div class="panel-title">${this._esc(c.title)}${c.real ? "" : '<span class="stub-tag">SOON</span>'}${this._notSetUpTag(c)}</div>
        </div>
        ${body}
      </div>`;
  }

  // An optional feature's card says when the feature is not set up yet
  // (8.43.0). Only a backend "false" marks it, so an older backend marks
  // nothing.
  _notSetUpTag(c) {
    const features = this._data()?.config?.optional_features || {};
    if (!c.feature || features[c.feature] !== false) return "";
    return `<span class="stub-tag">${this._esc(this._t("NOT SET UP"))}</span>`;
  }

  // The "Nova speaks" choice for a saved code: the base language, except
  // codes whose region or script changes the language. Traditional Chinese
  // and Brazilian Portuguese have their own options. The regional codes Nova
  // names but the picker does not offer get an option showing the code, so
  // the picker shows the true value and a save never turns it into the base.
  _outputLanguageChoice(value) {
    const v = String(value || "auto").trim().replace(/_/g, "-").toLowerCase();
    if (/^zh-(.*-)?(hant|tw|hk|mo)(-|$)/.test(v)) return "zh-Hant";
    if (v === "pt-br") return "pt-BR";
    const kept = NovaPanel.KEPT_REGIONAL_CODES.find(c => c.toLowerCase() === v);
    return kept || v.split("-")[0];
  }

  _outputLanguageExtra(value) {
    const choice = this._outputLanguageChoice(value);
    return NovaPanel.KEPT_REGIONAL_CODES.includes(choice) ? [[choice, choice]] : [];
  }

  _generalCardBody() {
    const cfg = this._data()?.config || {};
    const onOff = (key, label, desc) => `
      <div class="toggle-row">
        <span class="toggle-label">${this._esc(label)}</span>
        <span class="toggle-desc">${this._esc(desc)}</span>
        <button class="toggle-btn ${cfg[key] ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${cfg[key] ? "false" : "true"}">
          ${cfg[key] ? "ON" : "OFF"}
        </button>
      </div>`;
    return `
      <div class="cfg-row">
        <label>Language</label>
        <select id="uiLanguage" class="cfg-field" data-cfg-key="ui_language">
          ${this._optSelect([
            ["auto", "Auto (Home Assistant)"], ["en", "English"], ["cs", "Čeština"],
            ["da", "Dansk"], ["de", "Deutsch"], ["es", "Español"], ["fi", "Suomi"],
            ["fr", "Français"], ["it", "Italiano"], ["nb", "Norsk bokmål"],
            ["nl", "Nederlands"], ["pl", "Polski"], ["pt", "Português"],
            ["pt-br", "Português (Brasil)"], ["ro", "Română"], ["ru", "Русский"],
            ["sk", "Slovenčina"], ["sv", "Svenska"], ["tr", "Türkçe"],
            ["uk", "Українська"], ["zh", "中文（简体）"], ["zh-hant", "中文（繁體）"],
          ], cfg.ui_language || "auto")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Sleep state</label>
        <select class="cfg-field" data-cfg-key="sleep_override">
          ${this._optSelect([["auto", "Auto (occupancy + quiet hours)"], ["awake", "Awake"], ["asleep", "Asleep"]], cfg.sleep_override || "auto")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Nova speaks</label>
        <select class="cfg-field" data-cfg-key="output_language">
          ${this._optSelect([
            ["auto", "Auto (follow Home Assistant)"],
            ["ar", "Arabic"],
            ["pt-BR", "Brazilian Portuguese"],
            ["ca", "Catalan"],
            ["cs", "Czech"],
            ["da", "Danish"],
            ["nl", "Dutch"],
            ["en", "English"],
            ["fi", "Finnish"],
            ["fr", "French"],
            ["de", "German"],
            ["el", "Greek"],
            ["he", "Hebrew"],
            ["hu", "Hungarian"],
            ["id", "Indonesian"],
            ["it", "Italian"],
            ["ja", "Japanese"],
            ["ko", "Korean"],
            ["nb", "Norwegian Bokmål"],
            ["pl", "Polish"],
            ["pt", "Portuguese"],
            ["ro", "Romanian"],
            ["ru", "Russian"],
            ["zh", "Simplified Chinese"],
            ["sk", "Slovak"],
            ["es", "Spanish"],
            ["sv", "Swedish"],
            ["th", "Thai"],
            ["zh-Hant", "Traditional Chinese"],
            ["tr", "Turkish"],
            ["uk", "Ukrainian"],
            ["vi", "Vietnamese"],
          ].concat(this._outputLanguageExtra(cfg.output_language)), this._outputLanguageChoice(cfg.output_language))}
        </select>
        <span class="toggle-desc">The language Nova speaks and writes in. The panel's own language is set above and is not affected. Safety notifications are translated for English, French, German, Spanish, Italian, Dutch and Portuguese only; in any other language they stay in English, while text Nova generates follows this setting.</span>
      </div>
      <div class="toggle-list">
        ${onOff("hero_card_cycle", "Cards on the Command Center", "Now and then, show a card of live figures beside the head while the house is awake")}
        ${onOff("announcements_enabled", "Announcements", "Master switch — all proactive speech")}
        ${onOff("announce_notify_only", "Notifications only", "Send proactive alerts to your phone instead of speaking them. Critical safety alerts still speak")}
        ${onOff("sentinel_enabled", "Sentinel", "Doors, windows and locks left open")}
        ${onOff("observer_enabled", "Observer", "AI event awareness (uses API)")}
        ${onOff("cognition_enabled", "Cognition", "Local triage — sees telemetry and decides what deserves deeper reasoning")}
        ${onOff("rich_reasoning", "Rich Reasoning", "Use the configured reasoning model first for medium and high-priority events")}
        ${onOff("light_control_enabled", "Dashboard Light Control", "Allow room light toggles on the dashboard; status remains visible when off")}
      </div>`;
  }

  _personHonorificsCardBody() {
    const cfg = this._data()?.config || {};
    const people = cfg.all_people || [];
    const overrides = cfg.person_honorifics || {};
    const opts = NovaPanel.HONORIFIC_OPTIONS;
    if (!people.length) {
      return `<div class="stub-body">No <code>person.*</code> entities found yet — add one in Home Assistant to set a personal address here.</div>`;
    }
    const rows = people.map(p => {
      const current = overrides[p.entity_id] || "";
      const isCustom = current && !opts.includes(current);
      return `
        <div class="pairing-row person-honorific-row">
          <span class="pairing-label">${this._esc(p.name)}</span>
          <select class="person-honorific-select" data-person-id="${this._esc(p.entity_id)}">
            <option value="">— use default —</option>
            ${opts.map(o => `<option value="${this._esc(o)}"${!isCustom && o === current ? " selected" : ""}>${this._esc(NovaPanel.HONORIFIC_LABELS[o] || o)}</option>`).join("")}
            <option value="__custom__"${isCustom ? " selected" : ""}>Custom…</option>
          </select>
          <input type="text" class="person-honorific-custom" data-person-id="${this._esc(p.entity_id)}"
                 placeholder="Custom address" value="${isCustom ? this._esc(current) : ""}"
                 ${isCustom ? "" : "hidden"}>
        </div>`;
    }).join("");
    return `
      <div class="pairing-list">${rows}</div>
      <div class="camera-note">Only applies while that person is home alone. With nobody home, or more than one person home, Nova doesn't guess — it drops the address entirely. Anyone without an override here uses the global "Address me as" setting (Settings → Devices &amp; Services → Nova → Configure).</div>`;
  }

  _roomSpeakersCardBody() {
    const cfg = this._data()?.config || {};
    const areas = cfg.speaker_areas || [];
    const castDevs = cfg.cast_devices || [];
    const assigned = cfg.room_speakers || {};
    const rows = areas.length
      ? areas.map(a => `
        <div class="pairing-row">
          <span class="pairing-label">${this._esc(a.name)}</span>
          <select class="new-room-speaker-select" data-area-id="${this._esc(a.area_id)}">
            <option value="">— none —</option>
            ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === assigned[a.area_id] ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
          </select>
        </div>`).join("")
      : `<div class="stub-body">No rooms found yet.</div>`;
    return `
      <div class="pairing-list">${rows}</div>
      <div class="pairing-row">
        <span class="pairing-label">General speaker (fallback)</span>
        <select class="new-general-speaker-select">
          <option value="">— none —</option>
          ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === cfg.general_speaker ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
        </select>
      </div>`;
  }

  _operationalModeCardBody() {
    const cfg = this._data()?.config || {};
    const areas = this._data()?.areas || [];
    const m = this._mode || {};
    const active = m.active || "normal";
    const avail = m.available || [];
    const modeChips = avail.length
      ? avail.map(mo => `<button class="mode-chip ${mo.name === active ? "mode-chip-on" : ""}" data-mode="${this._esc(mo.name)}" title="${this._esc(mo.description || "")}">${this._esc(mo.name)}</button>`).join("")
      : `<div class="stub-body">Couldn't load modes — restart Home Assistant after updating.</div>`;
    const labAreas = Array.isArray(cfg.lab_areas) ? cfg.lab_areas : [];
    const labChips = areas.length
      ? areas.map(a => `<button class="mode-chip ${labAreas.includes(a.id) ? "mode-chip-on" : ""}" data-lab-area="${this._esc(a.id)}">${this._esc(a.name)}</button>`).join("")
      : `<span class="stub-body">No rooms detected yet.</span>`;
    const areaOpts = [["", "— none —"], ...areas.map(a => [a.id, a.name])];
    const mpOpts = this._mediaPlayerOptions(cfg.movie_media_player || "");
    return `
      <div class="cfg-row">
        <label>Auto (follow occupancy)</label>
        <button class="toggle-btn ${cfg.operational_mode_auto !== false ? "on" : "off"}" data-cfg-key="operational_mode_auto" data-cfg-val="${cfg.operational_mode_auto !== false ? "false" : "true"}">
          ${cfg.operational_mode_auto !== false ? "ON" : "OFF"}
        </button>
      </div>
      <div class="stub-body">Active: <strong>${this._esc(active.toUpperCase())}</strong>${m.description
        ? " " + this._tHtml("— {description}. Safety always stays active.", { description: this._esc(this._tx(m.description).replace(/[.。]\s*$/, "")) })
        : ". Safety always stays active."}</div>
      <div class="mode-grid">${modeChips}</div>
      <details class="mode-bindings"${this._modeBindingsOpen ? " open" : ""}>
      <summary class="mode-bind-head">Mode bindings — scope Lab &amp; Movie to specific rooms</summary>
      <div class="cfg-row"><label>Lab rooms (quiet only here)</label></div>
      <div class="mode-grid">${labChips}</div>
      <div class="cfg-row">
        <label>Movie room</label>
        <select class="cfg-field" data-cfg-key="movie_area">${this._optSelect(areaOpts, cfg.movie_area || "")}</select>
      </div>
      <div class="cfg-row">
        <label>Movie player <span class="toggle-desc">optional</span></label>
        <select class="cfg-field" data-cfg-key="movie_media_player">${this._optSelect(mpOpts, cfg.movie_media_player || "")}</select>
      </div>
      <div class="cfg-row">
        <label>Movie dim %</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="100" step="5" data-cfg-key="movie_dim_pct" value="${cfg.movie_dim_pct ?? ""}" placeholder="15">
      </div>
      </details>`;
  }

  // Merged from Classic's two separate diagnostics cards ("System
  // Diagnostics" under General, a per-service "Diagnostics" test panel
  // under Cameras) into the one place this section's own docstring already
  // says it should live. Fetched once per element lifetime (not on the 20s
  // live-data poll, and not on every settings re-render) — the health check
  // makes a real, if lightweight, LLM/TTS connectivity probe, matching
  // Classic's own on-demand-only behaviour.
  async _fetchDiagnosticsData() {
    if (!this._hass) return;
    try {
      this._diag = await this._hass.callWS({ type: "nova/diagnostics" });
    } catch (_) { this._diag = { error: true }; }
    try {
      this._calib = await this._hass.callWS({ type: "nova/get_calibration" });
    } catch (_) { this._calib = null; }
    try {
      this._setupHealth = await this._hass.callWS({ type: "nova/get_setup_health" });
    } catch (_) { this._setupHealth = { error: true }; }
    try {
      const activity = await this._hass.callWS({ type: "nova/get_provider_activity", days: 7 });
      this._providerActivity = activity.days || [];
    } catch (_) { this._providerActivity = null; }
    if (this._currentTab === "diagnostics") this._render();
  }

  _diagStatusCls(st) {
    return { ok: "diag-ok", warn: "diag-warn", idle: "diag-idle", down: "diag-down", off: "diag-off" }[st] || "diag-off";
  }
  _diagStatusLabel(st) {
    return { ok: "OK", warn: "WARN", idle: "IDLE", down: "DOWN", off: "OFF" }[st] || "?";
  }

  // ── Setup Doctor (Phase 2): read-only configuration health, folded into
  // the same diagnostics card. Only the setup-specific checks are shown here
  // — the 8 core-service checks (llm/embeddings/tts/stt/cameras/routines/
  // database/scheduler) already render above under "Core services"; showing
  // them a second time from the same backend payload would just be noise.
  static SETUP_HEALTH_CORE_KEYS = new Set([
    "llm", "embeddings", "tts", "stt", "cameras", "routines", "database", "scheduler",
  ]);

  _setupHealthCardBody() {
    const sh = this._setupHealth || {};
    if (sh.error) {
      return `<div class="stub-body">Couldn't run Setup Doctor — restart Home Assistant after updating.</div>`;
    }
    const checks = (sh.checks || []).filter(c => !NovaPanel.SETUP_HEALTH_CORE_KEYS.has(c.key));
    if (!checks.length) {
      return `<div class="panel-head"><div class="panel-title">Setup Doctor</div></div><div class="stub-body">Loading…</div>`;
    }
    const rows = checks.map(c => `
        <div class="cfg-row">
          <label>${this._esc(c.name)}</label>
          <span class="${this._diagStatusCls(c.status)}">${this._diagStatusLabel(c.status)}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 8px">${this._esc(c.detail || "")}${
          c.suggested_fix ? ` — ${this._esc(c.suggested_fix)}` : ""}</div>`).join("");
    return `<div class="panel-head"><div class="panel-title">Setup Doctor</div></div>${rows}`;
  }

  // ── Provider activity (Phase 5): bounded daily aggregates only — never
  // prompts, responses, tool arguments, images, or credentials. Days with no
  // recorded activity are simply absent, not shown as zero rows.
  _providerActivityCardBody() {
    const days = this._providerActivity;
    if (days === null) {
      return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div><div class="stub-body">Couldn't load provider activity.</div>`;
    }
    if (!days || !days.length) {
      return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div><div class="stub-body">No provider activity recorded yet. Activity appears after Nova uses a supported conversation or classifier path.</div>`;
    }
    const rows = days.map(d => {
      const entries = (d.entries || []).map(e => {
        // Each line's words are one template; the line breaks stay as they were.
        const tokens = (e.avg_input_tokens != null || e.avg_output_tokens != null)
          ? " · " + this._tHtml("avg tokens in/out {input}/{output}", {
            input: e.avg_input_tokens ?? "—", output: e.avg_output_tokens ?? "—" })
          : "";
        return `<div class="stub-body" style="margin:2px 0">
            ${this._esc(e.provider)}/${this._esc(e.model)} (${this._esc(e.role)}, ${this._esc(e.location)}) —
            ${this._tHtml(e.call_count === 1 ? "{count} call," : "{count} calls,", { count: e.call_count })}
            ${this._tHtml("{ok} ok / {failed} failed,", { ok: e.success_count, failed: e.failure_count })}
            ${this._tHtml("avg {latency}ms", { latency: e.avg_latency_ms ?? "—" })}${tokens}
          </div>`;
      }).join("");
      return `<div class="cfg-row"><label>${this._esc(d.day)}</label></div>${entries}`;
    }).join("");
    return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div>${rows}`;
  }

  // The Diagnostics tab (8.0.0): the card that used to sit in Settings,
  // moved here whole. Its data is fetched on first entry (see _wireDiagnostics).
  _htmlDiagnostics() {
    return `
        <div class="panel diag-panel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head"><div class="panel-title">Diagnostics</div></div>
          ${this._diagnosticsCardBody()}
        </div>`;
  }

  // Presence sensors (moved from the Residence tab when the 3D house went):
  // the per room presence, motion and mmWave sensors, from nova/mmwave_overview.
  async _fetchMmwaveNew() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/mmwave_overview" });
      this._mmwave = res || { rooms: [], summary: {} };
    } catch (_) {
      this._mmwave = { rooms: [], summary: {}, error: true };
    }
    this._renderMmwaveNew();
  }
  _renderMmwaveNew() {
    const list = this.shadowRoot?.getElementById("resMmwaveList");
    const sumEl = this.shadowRoot?.getElementById("resMmwaveSummary");
    if (!list) return;
    const data = this._mmwave || { rooms: [], summary: {} };
    const s = data.summary || {};
    if (sumEl) this._setText(sumEl, s.rooms_with_mmwave ? this._t("◉ {detecting}/{rooms} OCCUPIED", { detecting: s.rooms_detecting || 0, rooms: s.rooms_with_mmwave }) : "◉ NONE");
    if (data.error) { this._setHtml(list, `<div class="toggle-desc">Couldn't read sensors — restart Home Assistant after updating, then reopen.</div>`); return; }
    const rooms = data.rooms || [];
    if (!rooms.length) { this._setHtml(list, `<div class="toggle-desc">No presence, motion, or mmWave sensors found. Assign occupancy sensors to areas in Home Assistant and they'll appear here.</div>`); return; }
    this._setHtml(list, rooms.map(r => {
      const on = r.detecting_count > 0;
      const sensorLine = r.sensor_count > 1
        ? this._tHtml("{detecting}/{total} sensors", { detecting: r.detecting_count, total: r.sensor_count })
        : this._tHtml("{count} sensor", { count: r.sensor_count });
      return `<div class="cfg-row">
        <label>${this._esc(r.name)}${r.outdoor ? " ▲" : ""}</label>
        <span class="toggle-desc">${on
          ? this._tHtml("OCCUPIED · {sensors} · now", { sensors: sensorLine })
          : this._tHtml("clear · {sensors} · {age}", { sensors: sensorLine, age: this._esc(r.freshest) })}</span>
      </div>`;
    }).join(""));
  }

  _diagnosticsCardBody() {
    const cfg = this._data()?.config || {};
    const diag = this._diag || {};
    if (diag.error) {
      return `<div class="stub-body">Couldn't run diagnostics — restart Home Assistant after updating.</div>`;
    }
    const svcs = diag.services || [];
    const overall = svcs.length
      ? `<span class="${this._diagStatusCls(diag.overall)}">${this._esc((diag.summary || diag.overall || "").toUpperCase())}</span>`
      : "—";
    const rows = svcs.length
      ? svcs.map(s => `
        <div class="cfg-row">
          <label>${this._esc(s.name)}</label>
          <span class="${this._diagStatusCls(s.status)}">${this._diagStatusLabel(s.status)}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 8px">${this._esc(s.detail || "")}</div>`).join("")
      : `<div class="stub-body">Loading…</div>`;
    const svcTest = (svc, label) => `
      <div class="cfg-row">
        <label>${this._esc(label)}</label>
        <button class="mode-chip" data-svc="${this._esc(svc)}">RUN</button>
      </div>`;
    const camOpts = (cfg.cameras || []).filter(c => c.enabled !== false).map(c => [c.entity_id, c.name]);
    return `
      <div class="cfg-row"><label>Core services</label>${overall}</div>
      ${rows}
      <div class="cfg-row"><button class="mode-chip" id="newDiagRefresh">⟳ RUN CHECK</button></div>
      <div class="cfg-row"><label>HOMER — diagnostic sub-agent</label><span class="diag-ok">AVAILABLE</span></div>
      <div class="stub-body" style="margin:-6px 0 8px">Read-only. Nova can delegate a "why is this broken/slow" question to HOMER to investigate before answering — it can only read state, telemetry, and history, never control anything or change a setting. Always on; nothing to configure.</div>
      <div class="mode-bind-head"></div>
      ${this._setupHealthCardBody()}
      ${this._providerActivityCardBody()}
      <div class="mode-bind-head">Presence sensors <span class="toggle-desc" id="resMmwaveSummary">◉ scan</span></div>
      <div class="toggle-desc">Live occupancy per room from presence/motion/mmWave sensors.</div>
      <div id="resMmwaveList"><div class="toggle-desc">Reading sensors…</div></div>
      <div class="panel-head"><div class="panel-title">Service tests</div></div>
      ${svcTest("nova.test_tts", "TTS — Nova voice test")}
      ${svcTest("nova.observer_status", "Observer — fire status event")}
      ${svcTest("nova.briefing", "Briefing — manual trigger")}
      ${svcTest("nova.diagnose_doorbell", "Doorbell — run diagnostics")}
      ${svcTest("nova.test_notify", "Notification — test phone push")}
      ${svcTest("nova.test_routing", "Routing — dump routing state to log")}
      <div class="cfg-row">
        <label>Camera — analyze now</label>
        <select class="cfg-field" id="newDiagCameraSelect">${camOpts.length ? this._optSelect(camOpts, camOpts[0][0]) : '<option value="">— no cameras —</option>'}</select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newDiagCameraRun">RUN</button></div>`;
  }

  // ── AI Models — ported near-verbatim from Classic (see nova-panel.js's
  // own _modelRoles/_loadModelsFor/_pickHealModel). Deliberately NOT wired
  // through the generic .cfg-field autosave or _saveSetting: those trigger
  // a full _render(), which would wipe the just-populated live model
  // dropdown before the user ever sees it — the exact reason Classic's own
  // wiring comment gives for avoiding that here. ──
  _modelRoles() {
    return [
      { role: "llm", label: "Main Agent", provKey: "llm_provider", modelKey: "model" },
      { role: "classifier", label: "Classifier", provKey: "classifier_provider", modelKey: "classifier_model" },
      { role: "reasoning", label: "Reasoning", provKey: "reasoning_provider", modelKey: "reasoning_model" },
      { role: "vision", label: "Vision", provKey: "vision_provider", modelKey: "vision_model" },
      { role: "camrsn", label: "Camera Reasoning", provKey: "camera_reasoning_provider", modelKey: "camera_reasoning_model" },
      { role: "sugrev", label: "Suggestion Review", provKey: "suggestion_review_provider", modelKey: "suggestion_review_model" },
    ];
  }

  _aiModelsCardBody() {
    const cfg = this._data()?.config || {};
    const PROVIDERS = ["groq", "openai", "gemini", "ollama", "anthropic", "custom"];
    const configuredProviders = this._modelRoles().map(r => cfg[r.provKey]);
    const legacyEndpoint = cfg.self_hosted_endpoints_migrated ? "" : cfg.llm_base_url;
    const ollamaEndpoint = cfg.ollama_base_url || (configuredProviders.includes("ollama") ? legacyEndpoint : "") || "";
    const customEndpoint = cfg.custom_base_url || (configuredProviders.includes("custom") ? legacyEndpoint : "") || "";
    const rows = this._modelRoles().map(r => {
      const curProv = cfg[r.provKey] || "groq";
      const curModel = cfg[r.modelKey] || "";
      const modelOpts =
        (curModel ? `<option value="${this._esc(curModel)}" selected>${this._esc(curModel)}</option>` : "") +
        `<option value="" disabled>loading…</option><option value="__custom__">✎ Custom…</option>`;
      return `
        <div class="new-model-row" data-role="${this._esc(r.role)}">
          <span class="model-label">${this._esc(r.label)}</span>
          <select class="new-prov-select" data-role="${this._esc(r.role)}" data-cfg-key="${r.provKey}">
            ${this._optSelect(PROVIDERS.map(p => [p, p]), curProv)}
          </select>
          <select class="new-model-select" data-role="${this._esc(r.role)}" data-cfg-key="${r.modelKey}" data-current="${this._esc(curModel)}">${modelOpts}</select>
          <input class="new-model-custom" data-role="${this._esc(r.role)}" data-cfg-key="${r.modelKey}"
                 type="text" placeholder="enter model id" value="${this._esc(curModel)}" style="display:none">
          <button class="mode-chip new-model-refresh" data-role="${this._esc(r.role)}" title="Refresh the live model list (bypasses the cache)">↻</button>
          <div class="new-model-warning" data-role-warning="${this._esc(r.role)}" hidden></div>
          ${r.role === "llm" ? `<div class="stub-body">Changes are staged until you press Apply. Nova reloads itself after a successful check and save.</div>` : ""}
          ${r.role === "vision" ? `<div class="stub-body">Vision needs a model whose provider reports image support. Camera Reasoning is text-only and does not.</div>` : ""}
        </div>`;
    }).join("");
    const credRows = ["groq", "openai", "anthropic", "gemini", "custom", "ollama"].map(p => `
      <div class="cred-row" data-cred-provider="${p}">
        <span class="model-label">${this._esc(p)}</span>
        <span class="cred-status" data-cred-status="${p}">…</span>
        <input class="cred-input" type="password" data-cred-provider="${p}" placeholder="enter to set or replace" autocomplete="off">
        <button class="mode-chip cred-save" data-cred-provider="${p}">SAVE</button>
        <button class="mode-chip cred-clear" data-cred-provider="${p}">CLEAR</button>
      </div>`).join("");
    return `
      <div class="stub-body">Choose a starting profile or configure each role yourself. Profiles only stage changes; nothing is saved until Apply.</div>
      <div class="cfg-row" data-ai-profiles>
        <label>Profile</label>
        <button class="mode-chip" data-ai-profile="hybrid">HYBRID</button>
        <button class="mode-chip" data-ai-profile="local">LOCAL TEXT</button>
        <button class="mode-chip" data-ai-profile="manual">MANUAL</button>
      </div>
      <div class="stub-body">Hybrid keeps the Main Agent and Vision choices, and moves background text work to Ollama. Local Text also moves the Main Agent. Vision only moves when Ollama reports a vision-capable model.</div>
      <div class="panel-head" style="margin-top:14px"><div class="panel-title">Self-hosted endpoints</div></div>
      <div class="cfg-row" data-endpoint-row="ollama">
        <label>Ollama</label>
        <input class="cfg-field ai-endpoint" data-endpoint-provider="ollama" data-current="${this._esc(cfg.ollama_base_url || "")}" type="text" value="${this._esc(ollamaEndpoint)}" placeholder="http://host:11434">
        <button class="mode-chip ai-endpoint-test" data-endpoint-provider="ollama">TEST</button>
      </div>
      <div class="stub-body ai-endpoint-status" data-endpoint-status="ollama"></div>
      <div class="cfg-row" data-endpoint-row="custom">
        <label>OpenAI-compatible</label>
        <input class="cfg-field ai-endpoint" data-endpoint-provider="custom" data-current="${this._esc(cfg.custom_base_url || "")}" type="text" value="${this._esc(customEndpoint)}" placeholder="https://host/v1">
        <button class="mode-chip ai-endpoint-test" data-endpoint-provider="custom">TEST</button>
      </div>
      <div class="stub-body ai-endpoint-status" data-endpoint-status="custom"></div>
      <div class="cfg-row">
        <label>Ollama context length</label>
        <input class="cfg-field" id="aiOllamaNumCtx" type="number" min="512" max="262144" step="512" value="${this._esc(cfg.ollama_num_ctx || 8192)}">
      </div>
      <div class="cfg-row">
        <label>Prompt size <span class="toggle-desc">entity names per type; 0 = counts only</span></label>
        <input class="cfg-field" id="aiHomeContextMaxEntities" type="number" min="0" max="50" step="1" value="${this._esc(cfg.home_context_max_entities ?? 15)}">
      </div>
      <div class="new-model-list">${rows}</div>
      <div class="cfg-row" style="margin-top:14px">
        <button class="mode-chip" id="aiApply">APPLY</button>
        <span class="stub-body" id="aiApplyStatus">No unsaved changes.</span>
      </div>
      <div class="panel-head" style="margin-top:14px"><div class="panel-title">Provider Credentials</div></div>
      <div class="stub-body">Stored only in Home Assistant's secrets.yaml, one per provider. A saved credential is never shown here again — only whether one is set. Ollama's is optional, for a protected endpoint only.</div>
      <div class="new-model-list">${credRows}</div>`;
  }

  // Mismatch warnings (Phase 3, v7.108.0): flagged only on strong, specific
  // evidence — never inferred from an unrecognised name, never auto-applied.
  // Selecting the model is still the administrator's call either way.
  _looksOllamaTagged(model) {
    return /^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$/i.test((model || "").trim());
  }

  _providerPrefixOwners() {
    return [
      { re: /^claude-/i, owner: "anthropic" },
      { re: /^gemini-/i, owner: "gemini" },
      { re: /^(gpt-|o[1-9](-|$))/i, owner: "openai" },
    ];
  }

  // Nova's own well-known text-only defaults (const.py DEFAULT_MODEL /
  // DEFAULT_CLASSIFIER_MODEL / etc, plus a few other common text-only cloud
  // models) — selecting one of these EXACT ids for vision/camera-reasoning
  // is strong evidence of a leftover default rather than a real choice.
  // Deliberately NOT a broad "doesn't look like a vision model" regex —
  // that would warn on every model Nova simply doesn't recognise yet.
  _knownTextOnlyModels() {
    return new Set([
      "openai/gpt-oss-120b", "openai/gpt-oss-20b",
      "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
      "mixtral-8x7b-32768", "deepseek-r1-distill-llama-70b",
    ]);
  }

  _modelMismatchWarning(role, provider, model) {
    const m = (model || "").trim();
    if (!m) return null;
    if (this._looksOllamaTagged(m) && provider !== "ollama") {
      return `"${m}" looks like an Ollama-tagged model (name:tag) — ${provider} is a cloud provider and won't recognise that format.`;
    }
    for (const { re, owner } of this._providerPrefixOwners()) {
      if (re.test(m) && provider !== owner) {
        return `"${m}" looks like a ${owner} model, but the selected provider is ${provider}.`;
      }
    }
    if (role === "vision" && this._knownTextOnlyModels().has(m)) {
      return `"${m}" is one of Nova's own text-only default models — it will reject image input.`;
    }
    const detail = ((this._modelCatalog || {})[provider] || []).find(item => item.id === m);
    const caps = new Set((detail && detail.capabilities) || []);
    if (detail && role === "vision" && !caps.has("vision")) {
      return `"${m}" does not report vision capability.`;
    }
    if (detail && role === "llm" && !caps.has("tools")) {
      return `"${m}" does not report tool-calling capability, which the Main Agent needs.`;
    }
    return null;
  }

  _updateRoleWarning(row) {
    const provSel = row.querySelector(".new-prov-select");
    const modelSel = row.querySelector(".new-model-select");
    const customInput = row.querySelector(".new-model-custom");
    const warnEl = row.querySelector("[data-role-warning]");
    if (!provSel || !modelSel || !warnEl) return;
    const role = row.getAttribute("data-role");
    const model = (customInput && customInput.style.display !== "none")
      ? customInput.value : modelSel.value;
    const warning = this._modelMismatchWarning(role, provSel.value, model);
    this._setText(warnEl, warning || "");
    warnEl.hidden = !warning;
  }

  _populateModelSelect(provider, selectEl, res) {
    if (!selectEl) return;
    const cur = selectEl.getAttribute("data-current") || "";
    const models = (res && res.models) || [];
    this._modelCatalog = this._modelCatalog || {};
    this._modelCatalog[provider] = (res && res.model_details) || models.map(id => ({ id, capabilities: [] }));
    let opts = "";
    const label = model => {
      const detail = this._modelCatalog[provider].find(item => item.id === model);
      const caps = (detail && detail.capabilities) || [];
      return caps.length ? `${model} · ${caps.join(", ")}` : model;
    };
    if (models.length) {
      if (!cur) opts += `<option value="" selected disabled>choose a model…</option>`;
      if (cur && !models.includes(cur)) {
          // Never silently replace a saved model just because a live
          // discovery call didn't happen to list it — it may be private,
          // preview, newly released, or simply not returned by this
          // endpoint. Keep it selected and offer the live list alongside
          // it. (Previously this auto-picked and SAVED a different model
          // — often just the alphabetically-first one — on every render.)
        opts += `<option value="${this._esc(cur)}" selected>${this._tHtml("{model} — not in the live list", { model: this._esc(cur) })}</option>`;
        opts += models.map(m => `<option value="${this._esc(m)}">${this._esc(label(m))}</option>`).join("");
      } else {
        opts += models.map(m => `<option value="${this._esc(m)}"${m === cur ? " selected" : ""}>${this._esc(label(m))}</option>`).join("");
      }
    } else {
      const err = res && res.error ? ` — ${String(res.error).slice(0, 48)}` : "";
      opts += (cur ? `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>` : "");
      opts += `<option value="" disabled>${this._tHtml("no models found{error}", { error: this._esc(err) })}</option>`;
    }
    opts += `<option value="__custom__">✎ Custom…</option>`;
    this._setHtml(selectEl, opts);
    // When the live list failed and a built in list is shown, the reason code
    // (for example key_rejected) is in the tooltip.
    const listNote = (res && res.fallback && res.error)
      ? String(res.error).slice(0, 48)
      : this._tx((res && res.truncated)
        ? "The provider returned more models than fit in one page — list may be incomplete." : "");
    selectEl.title = this._tx(listNote);
    const row = selectEl.closest(".new-model-row");
    if (row) {
      // The model box must show whenever the dropdown is on Custom. The
      // dropdown can land on Custom by itself (an empty list, no saved model),
      // and picking an option that is already selected fires no change event,
      // so the box would stay hidden and nothing could be typed.
      const customInput = row.querySelector(".new-model-custom");
      if (customInput) {
        const onCustom = selectEl.value === "__custom__";
        customInput.style.display = onCustom ? "" : "none";
        if (onCustom && cur && !customInput.value) customInput.value = cur;
      }
      this._updateRoleWarning(row);
    }
  }

  async _loadModelsFor(provider, selectEl, { refresh = false } = {}) {
    if (!this._hass || !selectEl) return;
    try {
      const res = await this._hass.callWS({ type: "nova/list_models", provider, refresh });
      this._populateModelSelect(provider, selectEl, res);
    } catch (_) { /* keep the saved selection and let endpoint Test explain failures */ }
  }

  async _rawSaveConfig(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova: failed to save ${key}`, err);
    }
  }

  _wireAiModels() {
    const root = this.shadowRoot;
    const markDirty = message => {
      const status = root.getElementById("aiApplyStatus");
      if (status) this._setText(status, message || "Unsaved changes.");
    };
    root.querySelectorAll(".new-model-row").forEach(row => {
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      const customInput = row.querySelector(".new-model-custom");
      const refreshBtn = row.querySelector(".new-model-refresh");
      if (!provSel || !modelSel) return;
      this._loadModelsFor(provSel.value, modelSel);
      provSel.addEventListener("change", async (e) => {
        const provider = e.target.value;
        modelSel.setAttribute("data-current", "");
        if (customInput) customInput.style.display = "none";
        await this._loadModelsFor(provider, modelSel);
        markDirty();
        this._updateRoleWarning(row);
      });
      modelSel.addEventListener("change", e => {
        if (e.target.value === "__custom__") {
          if (customInput) { customInput.style.display = ""; customInput.focus(); }
          this._updateRoleWarning(row);
          markDirty();
          return;
        }
        if (customInput) customInput.style.display = "none";
        modelSel.setAttribute("data-current", e.target.value);
        markDirty();
        this._updateRoleWarning(row);
      });
      if (customInput) {
        customInput.addEventListener("input", e => {
          const v = (e.target.value || "").trim();
          if (v) modelSel.setAttribute("data-current", v);
          markDirty();
          this._updateRoleWarning(row);
        });
      }
      if (refreshBtn) {
        refreshBtn.addEventListener("click", async () => {
          refreshBtn.disabled = true;
          try {
            await this._loadModelsFor(provSel.value, modelSel, { refresh: true });
          } finally {
            refreshBtn.disabled = false;
          }
        });
      }
    });

    root.querySelectorAll(".ai-endpoint").forEach(input => {
      input.addEventListener("input", () => {
        const provider = input.getAttribute("data-endpoint-provider");
        if (this._modelCatalog) delete this._modelCatalog[provider];
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        if (status) this._setText(status, "Endpoint changed. Test it before choosing a profile.");
        markDirty();
      });
    });
    root.getElementById("aiOllamaNumCtx")?.addEventListener("input", () => markDirty());
    root.getElementById("aiHomeContextMaxEntities")?.addEventListener("input", () => markDirty());

    root.querySelectorAll(".ai-endpoint-test").forEach(button => {
      button.addEventListener("click", async () => {
        const provider = button.getAttribute("data-endpoint-provider");
        const input = root.querySelector(`.ai-endpoint[data-endpoint-provider="${provider}"]`);
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        button.disabled = true;
        if (status) this._setText(status, "Testing…");
        try {
          const res = await this._hass.callWS({
            type: "nova/test_provider_endpoint", provider,
            endpoint: (input?.value || "").trim(),
          });
          if (!res || !res.ok) throw new Error((res && res.message) || "Endpoint test failed.");
          if (input) input.value = res.endpoint;
          this._modelCatalog = this._modelCatalog || {};
          this._modelCatalog[provider] = res.model_details ||
            res.models.map(id => ({ id, capabilities: [] }));
          root.querySelectorAll(`.new-model-row`).forEach(row => {
            const provSel = row.querySelector(".new-prov-select");
            if (provSel?.value === provider) {
              this._populateModelSelect(provider, row.querySelector(".new-model-select"), res);
            }
          });
          if (status) this._setText(status, this._t(res.models.length === 1 ? "Connected. {count} model found." : "Connected. {count} models found.", { count: res.models.length }));
          markDirty("Endpoint tested. Changes are not saved yet.");
        } catch (err) {
          if (status) this._setText(status, err?.message || "Could not test this endpoint.");
        } finally {
          button.disabled = false;
        }
      });
    });

    const selectProfileModel = (row, provider, requiredCapability) => {
      const catalog = (this._modelCatalog || {})[provider] || [];
      const match = catalog.find(item =>
        !requiredCapability || (item.capabilities || []).includes(requiredCapability));
      if (!match) return false;
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      provSel.value = provider;
      modelSel.setAttribute("data-current", match.id);
      this._populateModelSelect(provider, modelSel, {
        models: catalog.map(item => item.id), model_details: catalog,
      });
      modelSel.value = match.id;
      this._updateRoleWarning(row);
      return true;
    };
    root.querySelectorAll("[data-ai-profile]").forEach(button => {
      button.addEventListener("click", () => {
        const profile = button.getAttribute("data-ai-profile");
        if (profile === "manual") {
          markDirty("Manual mode: choose each provider and model, then Apply.");
          return;
        }
        const textRoles = profile === "hybrid"
          ? ["classifier", "reasoning", "camrsn", "sugrev"]
          : ["llm", "classifier", "reasoning", "camrsn", "sugrev"];
        const missing = [];
        textRoles.forEach(role => {
          const row = root.querySelector(`.new-model-row[data-role="${role}"]`);
          const required = role === "llm" ? "tools" : "completion";
          if (!row || !selectProfileModel(row, "ollama", required)) missing.push(role);
        });
        if (profile === "local") {
          const visionRow = root.querySelector('.new-model-row[data-role="vision"]');
          if (visionRow) selectProfileModel(visionRow, "ollama", "vision");
        }
        markDirty(missing.length
          ? "Profile staged where compatible models were found. Test Ollama first to load capabilities for the remaining roles."
          : "Profile staged. Review the choices, then Apply.");
      });
    });

    root.getElementById("aiApply")?.addEventListener("click", async event => {
      const button = event.currentTarget;
      const status = root.getElementById("aiApplyStatus");
      const updates = {
        ollama_num_ctx: Number(root.getElementById("aiOllamaNumCtx")?.value || 8192),
        home_context_max_entities: Number(root.getElementById("aiHomeContextMaxEntities")?.value ?? 15),
      };
      // An endpoint field left as it showed its saved address is not sent,
      // the same way a model select keeps its data-current value: the saved
      // address stays as it is. It may be shown with its password masked,
      // and the server refuses that masked text (8.7.24). An edited, empty
      // or legacy-filled field is sent as before.
      ["ollama", "custom"].forEach(provider => {
        const input = root.querySelector(`.ai-endpoint[data-endpoint-provider="${provider}"]`);
        const value = (input?.value || "").trim();
        const shown = input?.getAttribute("data-current") || "";
        if (!(shown && value === shown)) updates[`${provider}_base_url`] = value;
      });
      root.querySelectorAll(".new-model-row").forEach(row => {
        const provSel = row.querySelector(".new-prov-select");
        const modelSel = row.querySelector(".new-model-select");
        const customInput = row.querySelector(".new-model-custom");
        updates[provSel.getAttribute("data-cfg-key")] = provSel.value;
        updates[modelSel.getAttribute("data-cfg-key")] =
          customInput && customInput.style.display !== "none"
            ? customInput.value.trim() : modelSel.value;
      });
      button.disabled = true;
      if (status) this._setText(status, "Checking models and saving…");
      try {
        const res = await this._hass.callWS({ type: "nova/apply_ai_config", updates });
        if (!res || !res.ok) throw new Error((res && res.message) || "Could not apply AI settings.");
        if (status) this._setText(status, res.message || "Saved. Nova is reloading.");
      } catch (err) {
        if (status) this._setText(status, err?.message || "Could not apply AI settings.");
        button.disabled = false;
      }
    });

    this._wireCredentials();
  }

  // Provider availability (Phase 3, v7.108.0): labels each role's provider
  // <option> as "not configured" when unavailable, WITHOUT disabling it —
  // an administrator can still pick it and add the credential/endpoint
  // right after. Never a value, just a boolean-derived label; each
  // provider's own evidence only (see websocket.py's
  // _compute_provider_availability — this only renders what it returns).
  _markProviderAvailability(available) {
    if (!available) return;
    const root = this.shadowRoot;
    root.querySelectorAll(".new-prov-select").forEach(sel => {
      Array.from(sel.options).forEach(opt => {
        const base = opt.value;
        if (!(base in available)) return;
        this._setText(opt, available[base] ? base : this._t("{provider} (not configured)", { provider: base }));
      });
    });
  }

  // Provider Credentials (Phase 2, v7.107.0): status is fetched once per
  // render (never cached across renders — a stale "configured" badge after
  // a clear elsewhere would be misleading) and a saved/cleared value is
  // never echoed back by the websocket commands, only `ok`.
  async _wireCredentials() {
    const root = this.shadowRoot;
    const rows = root.querySelectorAll("[data-cred-provider]");
    if (!rows.length || !this._hass) return;

    try {
      const res = await this._hass.callWS({ type: "nova/get_credential_status" });
      const status = (res && res.status) || {};
      root.querySelectorAll("[data-cred-status]").forEach(el => {
        const p = el.getAttribute("data-cred-status");
        const configured = !!status[p];
        this._setText(el, configured ? "configured" : "not set");
        el.classList.toggle("cred-configured", configured);
      });
      this._markProviderAvailability(res && res.available);
    } catch (_) { /* leave the "…" placeholder on error */ }

    root.querySelectorAll(".cred-save").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        const input = root.querySelector(`.cred-input[data-cred-provider="${p}"]`);
        const value = (input && input.value || "").trim();
        if (!value || !this._hass) return;
        try {
          const res = await this._hass.callWS({ type: "nova/set_credential", provider: p, value });
          if (res && res.ok) {
            input.value = "";
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { this._setText(statusEl, "configured"); statusEl.classList.add("cred-configured"); }
            this._markProviderAvailability({ [p]: true });
          }
        } catch (err) { console.error(`Nova: failed to save credential for ${p}`, err); }
      });
    });
    root.querySelectorAll(".cred-clear").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        if (!this._hass) return;
        if (!window.confirm(this._t("Clear the stored {provider} credential? Any role still using it will stop working until a new key is set.", { provider: p }))) return;
        try {
          const res = await this._hass.callWS({ type: "nova/delete_credential", provider: p });
          if (res && res.ok) {
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { this._setText(statusEl, "not set"); statusEl.classList.remove("cred-configured"); }
            // custom/ollama availability isn't credential-derived (endpoint
            // / always-on respectively) — only the four cloud providers'
            // availability tracks their own credential.
            if (["groq", "openai", "anthropic", "gemini"].includes(p)) {
              this._markProviderAvailability({ [p]: false });
            }
          }
        } catch (err) { console.error(`Nova: failed to clear credential for ${p}`, err); }
      });
    });
  }

  _briefingsCardBody() {
    const cfg = this._data()?.config || {};
    const onOff = (key, onLabel, offLabel, defaultOn) => {
      const on = defaultOn ? cfg[key] !== false : !!cfg[key];
      return `<button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${on ? onLabel : offLabel}</button>`;
    };
    const feedChip = (key, label) => {
      const on = cfg[key] !== false;
      return `<button class="mode-chip ${on ? "mode-chip-on" : ""}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${label}</button>`;
    };
    return `
      <div class="stub-body">Nova speaks a summary at the times you set — weather and forecast, your calendar, what happened overnight, power draw, and any active hazards nearby.</div>
      <div class="cfg-row">
        <label>Morning</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="cfg-field cfg-num" style="width:64px;text-align:center" type="text" data-cfg-key="briefing_morning_time" value="${this._esc(cfg.briefing_morning_time || "07:30")}" placeholder="07:30">
          ${onOff("briefing_morning_enabled", "ON", "OFF", false)}
        </div>
      </div>
      <div class="cfg-row">
        <label>Evening</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="cfg-field cfg-num" style="width:64px;text-align:center" type="text" data-cfg-key="briefing_evening_time" value="${this._esc(cfg.briefing_evening_time || "19:30")}" placeholder="19:30">
          ${onOff("briefing_evening_enabled", "ON", "OFF", false)}
        </div>
      </div>
      <div class="cfg-row">
        <label>Only when someone's home</label>
        ${onOff("briefing_require_home", "YES", "NO", true)}
      </div>
      <div class="mode-bind-head">Include</div>
      <div class="mode-grid">
        ${feedChip("briefing_include_weather", "Weather")}
        ${feedChip("briefing_include_calendar", "Calendar")}
        ${feedChip("briefing_include_events", "Overnight")}
        ${feedChip("briefing_include_energy", "Energy")}
        ${feedChip("briefing_include_hazards", "Hazards")}
      </div>
      <div class="mode-bind-head">Arrival</div>
      <div class="stub-body">A welcome briefing fires when someone gets home — but only once this door actually opens, not the moment their phone shows them nearby (still in the driveway or car). Leave unset to keep arrival briefings off entirely.</div>
      <div class="cfg-row">
        <label>Front door</label>
        <select class="cfg-field" data-cfg-key="arrival_front_door_entity">${this._optSelect(this._frontDoorOptions(cfg.arrival_front_door_entity || ""), cfg.arrival_front_door_entity || "")}</select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newBriefNow">▶ BRIEF ME NOW</button></div>`;
  }

  _voiceConfirmationCardBody() {
    const cfg = this._data()?.config || {};
    const on = !!cfg.voice_confirm_enabled;
    return `
      <div class="stub-body">Ask out loud before sensitive actions (unlock, open a door, disarm) and listen for a spoken yes/no. Native mode uses the satellite's own audio; gated mode speaks through the room speaker — run the test to see which your setup supports.</div>
      <div class="cfg-row">
        <label>Voice confirmation</label>
        <button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="voice_confirm_enabled" data-cfg-val="${on ? "false" : "true"}">${on ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Mode</label>
        <select class="cfg-field" data-cfg-key="voice_confirm_mode">
          ${this._optSelect([["auto", "Auto (try native, fall back)"], ["native", "Native (satellite audio)"], ["gated", "Gated (room speaker)"]], cfg.voice_confirm_mode || "auto")}
        </select>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newVcTest">▶ TEST SATELLITE AUDIO</button></div>
      <div class="stub-body" id="newVcTestResult"></div>`;
  }

  _satelliteSpeakerCardBody() {
    const cfg = this._data()?.config || {};
    const satellites = cfg.satellites || [];
    const castDevs = cfg.cast_devices || [];
    const pairings = cfg.satellite_pairings || {};
    if (!satellites.length) return `<div class="stub-body">No satellites found.</div>`;
    const rows = satellites.map(sat => {
      const paired = pairings[sat.entity_id] || "";
      const label = sat.area || sat.name;
      return `
        <div class="pairing-row">
          <span class="pairing-label">${this._esc(label)}</span>
          <select class="new-sat-pair-select" data-sat-id="${this._esc(sat.entity_id)}">
            <option value="">— none —</option>
            ${castDevs.map(cd => `<option value="${this._esc(cd.entity_id)}"${cd.entity_id === paired ? " selected" : ""}>${this._esc(cd.name)}</option>`).join("")}
          </select>
        </div>`;
    }).join("");
    return `<div class="pairing-list">${rows}</div>`;
  }

  _announcementSpeakersCardBody() {
    const cfg = this._data()?.config || {};
    const castDevs = cfg.cast_devices || [];
    const selected = cfg.announcement_speakers || [];
    if (!castDevs.length) return `<div class="stub-body">No Cast devices found.</div>`;
    const rows = castDevs.map(cd => {
      const on = selected.includes(cd.entity_id);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(cd.name)}</span>
          <span class="toggle-desc">${this._esc(cd.entity_id)}</span>
          <button class="toggle-btn ${on ? "on" : "off"} new-ann-speaker-toggle" data-speaker-id="${this._esc(cd.entity_id)}">${on ? "ON" : "OFF"}</button>
        </div>`;
    }).join("");
    return `<div class="toggle-list">${rows}</div>`;
  }

  _notificationsCardBody() {
    const cfg = this._data()?.config || {};
    const svcs = cfg.notify_services_available || [];
    const selected = Array.isArray(cfg.notify_services)
      ? cfg.notify_services
      : (cfg.notify_service ? [cfg.notify_service] : []);
    if (!this._notifySavePending) this._notifyServicesDraft = [...selected];
    const displayed = this._notifySavePending ? this._notifyServicesDraft : selected;
    if (!svcs.length) return `<div class="stub-body">No notification services found.</div>`;
    const rows = svcs.map(service => {
      const on = displayed.includes(service);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(service.replace("notify.", ""))}</span>
          <span class="toggle-desc">${this._esc(service)}</span>
          <button class="toggle-btn ${on ? "on" : "off"} new-notify-service-toggle" data-notify-service="${this._esc(service)}">${on ? "ON" : "OFF"}</button>
        </div>`;
    }).join("");
    return `<div class="stub-body">Normal Nova alerts go to every selected device.</div><div class="toggle-list">${rows}</div>`;
  }

  _securityAlarmCardBody() {
    const cfg = this._data()?.config || {};
    const panels = cfg.alarm_panels || [];
    const selected = cfg.security_alarm_entity || "";
    const opts = [["", "Auto detect a single Alarmo panel"], ...panels.map(p => {
      const suffix = p.platform ? ` (${p.platform})` : "";
      return [p.entity_id, `${p.name}${suffix}`];
    })];
    const automatic = !!cfg.lockdown_auto_on_arm;
    const confined = cfg.intrusion_requires_confinement === true;
    const faceDown = cfg.face_stand_down === true;
    return `
      <div class="stub-body">Nova ignores every other alarm panel for security alerts and lockdown decisions. If more than one Alarmo panel exists, choose the intended household alarm here.</div>
      <div class="cfg-row">
        <label>Security alarm</label>
        <select class="cfg-field" data-cfg-key="security_alarm_entity">${this._optSelect(opts, selected)}</select>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Automatic lockdown</span>
        <span class="toggle-desc">Allow the selected alarm and sleep mode to lock doors and close covers</span>
        <button class="toggle-btn ${automatic ? "on" : "off"}" data-cfg-key="lockdown_auto_on_arm" data-cfg-val="${automatic ? "false" : "true"}">${automatic ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-bind-head">Locks left out of lockdown</div>
      <div class="stub-body">Lockdown and the night sweep never lock these, for example a thermostat's keypad lock. They are also left out of the "unlocked" lists in briefings, status and voice answers. Empty by default.</div>
      <div class="cfg-row">
        <input id="newExemptLockInput" list="newExemptLockList" class="cfg-field" style="flex:1" placeholder="type to find a lock…" autocomplete="off">
        <datalist id="newExemptLockList">${this._lockDatalist()}</datalist>
        <button class="mode-chip" id="newExemptLockAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExemptLockChips">${(() => {
        const arr = this._exclArr(cfg.lockdown_exempt_locks);
        return arr.length
          ? arr.map((e, i) => `<span class="new-pl-chip">${this._esc(this._entName(e))}<button class="new-exempt-lock-del" data-i="${i}" title="Remove">×</button></span>`).join("")
          : `<span class="toggle-desc">None.</span>`;
      })()}</div>
      <div class="toggle-row">
        <span class="toggle-label">Require confinement for intrusion monitoring</span>
        <span class="toggle-desc">Watch for intruders only while a lockdown is on or the alarm is armed. Off keeps the automatic away and asleep behaviour</span>
        <button class="toggle-btn ${confined ? "on" : "off"}" data-cfg-key="intrusion_requires_confinement" data-cfg-val="${confined ? "false" : "true"}">${confined ? "ON" : "OFF"}</button>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Residents can stand down a new intrusion alert</span>
        <span class="toggle-desc">Off by default. When on, a resident on the Faces roster, recognised at or above the confidence threshold on a camera in the last 3 minutes, stops Nova opening a NEW intrusion investigation, but only if no unknown face or unexplained person was also seen. It never closes an investigation that is already open, and never affects critical alerts, lockdown, freeze or mutes. A face can be a photo or a lookalike, so leave this off unless you accept that. Every stand down is logged in the Actions log</span>
        <button class="toggle-btn ${faceDown ? "on" : "off"}" data-cfg-key="face_stand_down" data-cfg-val="${faceDown ? "false" : "true"}">${faceDown ? "ON" : "OFF"}</button>
      </div>`;
  }

  _sentinelRulesCardBody() {
    const cfg = this._data()?.config || {};
    const rules = cfg.sentinel_rules || [];
    const disabled = cfg.disabled_sentinel_rules || [];
    if (!rules.length) return `<div class="stub-body">No sentinel rules found.</div>`;
    // The garage rule's toggle shows only in a home with a garage (8.26.0).
    // Display only: the rule keeps running either way.
    const shown = cfg.has_garage ? rules : rules.filter(r => r.id !== "garage_left_open");
    const rows = shown.map(r => {
      const isOff = disabled.includes(r.id);
      const name = r.id.replace(/_/g, " ");
      const desc = (r.desc || "").slice(0, 60);
      return `
        <div class="toggle-row">
          <span class="toggle-label">${this._esc(name)}</span>
          <span class="toggle-desc">${this._esc(desc)}</span>
          <button class="toggle-btn ${isOff ? "off" : "on"} new-rule-toggle" data-rule-id="${this._esc(r.id)}">${isOff ? "OFF" : "ON"}</button>
        </div>`;
    }).join("");
    return `<div class="toggle-list">${rows}</div>`;
  }

  // Hazard status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics). A source, location or level change re-fetches it.
  async _fetchHazardStatus() {
    if (!this._hass) return;
    try {
      this._hazard = await this._hass.callWS({ type: "nova/hazard", action: "status" });
    } catch (_) { this._hazard = null; }
    if (this._currentTab === "settings") this._render();
  }

  _hazardMonitorCardBody() {
    const cfg = this._data()?.config || {};
    const hz = this._hazard || {};
    const source = cfg.hazard_source || hz.source || "custom";
    const chip = (key, label) => {
      const on = cfg[key] === true;
      return `<button class="mode-chip ${on ? "mode-chip-on" : ""}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${label}</button>`;
    };
    const levels = [["yellow", "Yellow"], ["orange", "Orange"], ["red", "Red"]];
    const nightLevels = [...levels, ["off", "Off"]];
    const listText = key => (Array.isArray(cfg[key]) ? cfg[key] : []).join("\n");
    const descriptions = {
      met_eireann: "Weather warnings for your area. Alerts push and speak like any Nova alert.",
      us: "US National Weather Service warnings for your area. Alerts push and speak like any Nova alert.",
      custom: "Weather warnings from your custom feed. Alerts push and speak like any Nova alert.",
    };
    const credits = { met_eireann: "Met Éireann", us: "NWS" };
    const lat = cfg.hazard_lat ?? hz.center?.[0] ?? "";
    const lon = cfg.hazard_lon ?? hz.center?.[1] ?? "";
    return `
      <div class="stub-body">${this._esc(descriptions[source] || descriptions.custom)}</div>
      <div class="cfg-row">
        <label>Monitor</label>
        <button class="toggle-btn ${cfg.hazard_monitor_enabled ? "on" : "off"}" data-cfg-key="hazard_monitor_enabled" data-cfg-val="${cfg.hazard_monitor_enabled ? "false" : "true"}">${cfg.hazard_monitor_enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row" id="hazardSources">
        <label>Source</label>
        <select class="cfg-field" data-cfg-key="hazard_source">${this._optSelect([
          ["met_eireann", "Met Éireann (Ireland)"],
          ["us", "US (National Weather Service)"],
          ["custom", "Custom feed"],
        ], source)}</select>
      </div>
      ${credits[source] ? `<div class="toggle-desc hazard-source-credit">${this._tHtml("Source: {source}", { source: this._esc(credits[source]) })}</div>` : ""}
      ${source === "custom" ? `
      <div class="panel-head" style="margin-top:10px"><div class="panel-title">Custom CAP feed (not tested by Nova)</div></div>
      <div class="stub-body">An https address of one CAP alert, or an Atom or RSS list of them. Nova only alerts when an alert's area covers your home, or matches a code or name below.</div>
      <div class="cfg-row">
        <label>Feed address</label>
        <input class="cfg-field" type="text" data-cfg-key="hazard_cap_url" value="${this._esc(cfg.hazard_cap_url || "")}" placeholder="https://" autocomplete="off">
      </div>
      <div class="cfg-row">
        <label>Area codes <span class="toggle-desc">one per line</span></label>
        <textarea class="hazard-list-field" data-list-key="hazard_cap_area_codes" rows="2">${this._esc(listText("hazard_cap_area_codes"))}</textarea>
      </div>
      <div class="cfg-row">
        <label>Area names <span class="toggle-desc">one per line</span></label>
        <textarea class="hazard-list-field" data-list-key="hazard_cap_area_names" rows="2">${this._esc(listText("hazard_cap_area_names"))}</textarea>
      </div>` : ""}
      <div class="panel-head" style="margin-top:10px"><div class="panel-title">Warnings now</div></div>
      <div id="hazardWarnings">${this._renderHazardWarnings(hz.warnings)}</div>
      <div class="cfg-row">
        <label>Override lat / lon <span class="toggle-desc">optional</span></label>
        <div style="display:flex;gap:6px">
          <input class="cfg-field cfg-num hazard-location-field" style="width:76px" type="number" min="-90" max="90" step="any" data-cfg-key="hazard_lat" value="${this._esc(lat)}">
          <input class="cfg-field cfg-num hazard-location-field" style="width:76px" type="number" min="-180" max="180" step="any" data-cfg-key="hazard_lon" value="${this._esc(lon)}">
        </div>
      </div>
      <details class="mode-bindings" id="hazardAdvanced">
        <summary class="mode-bind-head">Advanced</summary>
        <div class="cfg-row">
          <label>Push to phone from <span class="toggle-desc">lower levels are ignored</span></label>
          <select class="cfg-field" data-cfg-key="hazard_push_level">${this._optSelect(levels, cfg.hazard_push_level || "yellow")}</select>
        </div>
        <div class="cfg-row">
          <label>Also speak from <span class="toggle-desc">below this, phone only</span></label>
          <select class="cfg-field" data-cfg-key="hazard_speak_level">${this._optSelect(levels, cfg.hazard_speak_level || "orange")}</select>
        </div>
        <div class="cfg-row">
          <label>Speak during quiet hours from <span class="toggle-desc">Red by default</span></label>
          <select class="cfg-field" data-cfg-key="hazard_night_speak_level">${this._optSelect(nightLevels, cfg.hazard_night_speak_level || "red")}</select>
        </div>
        <div class="mode-grid" id="hazardLegacyFeeds">
          ${chip("hazard_quakes_on", "Earthquakes")}
          ${chip("hazard_disasters_on", "NASA disasters")}
        </div>
        <div class="cfg-row">
          <label>Quake radius (km) / min mag</label>
          <div style="display:flex;gap:6px">
            <input class="cfg-field cfg-num" style="width:56px" type="number" min="1" step="1" data-cfg-key="hazard_quake_radius_km" value="${this._esc(cfg.hazard_quake_radius_km ?? 300)}">
            <input class="cfg-field cfg-num" style="width:56px" type="number" min="0" step="0.1" data-cfg-key="hazard_quake_min_mag" value="${this._esc(cfg.hazard_quake_min_mag ?? 2.5)}">
          </div>
        </div>
      </details>
      <div class="cfg-row"><button class="mode-chip" id="newHazScan">⟳ SCAN NOW</button></div>
      <div id="newHazBody" class="stub-body"></div>`;
  }

  // Weather warnings (8.8.0): level colour, time window, the headline and
  // the full description exactly as published (as plain text), and the
  // source credit (Met Éireann's licence requires both).
  _renderHazardWarnings(list) {
    if (!Array.isArray(list)) return `<div class="stub-body">Loading…</div>`;
    if (!list.length) return `<div class="stub-body">No warnings in force for your area.</div>`;
    const colour = { yellow: "#e6b800", orange: "#f08c00", red: "#e03131" };
    return list.map(w => `
      <div class="hazard-warning" data-level="${this._esc(w.level)}" style="border-left:4px solid ${colour[w.level] || "#888"};padding:4px 8px;margin:6px 0">
        <div><b class="hazard-level" style="color:${colour[w.level] || "inherit"}">${this._esc(String(w.level || "").toUpperCase())}</b> ${this._esc(w.headline_text || "")}</div>
        <div class="toggle-desc">${this._esc((w.counties || []).join(", "))}${w.from && w.to ? ` · ${this._esc(w.from)} to ${this._esc(w.to)}` : ""}</div>
        ${w.description_text ? `<div class="stub-body hazard-description" style="white-space:pre-line">${this._esc(w.description_text)}</div>` : ""}
        <div class="toggle-desc hazard-source">${this._tHtml("Source: {source}", { source: this._esc(w.source_label || "") })}</div>
      </div>`).join("");
  }

  // Phase 10 (v7.112.0) — Host Health. Off by default; discovery/mapping
  // status is always shown (so an admin can see what's detected before
  // turning anything on), live readings only populate once enabled and the
  // periodic sampler has run at least once.
  _hostHealthCardBody() {
    const cfg = this._data()?.config || {};
    const status = cfg.host_health_status || {};
    const metrics = status.metrics || [];
    const snap = status.snapshot || {};
    const enabled = cfg.host_health_enabled === true;
    const alertsEnabled = cfg.host_health_alerts_enabled === true;
    const mappings = cfg.host_health_mappings || {};

    const STATUS_CLS = { mapped: "diag-ok", ambiguous: "diag-warn", missing: "diag-off", disabled: "diag-warn" };
    const STATUS_LABEL = { mapped: "OK", ambiguous: "PICK ONE", missing: "MISSING", disabled: "DISABLED" };

    const snapEntryFor = (key) =>
      (snap.available || []).find(a => a.key === key)
      || (snap.problems || []).find(a => a.key === key)
      || (snap.missing_or_stale || []).find(a => a.key === key);

    const metricRow = (m) => {
      const entry = snapEntryFor(m.key);
      const valueText = (entry && typeof entry.value === "number")
        ? `${entry.value.toFixed(entry.unit === "°C" ? 1 : 0)}${entry.unit ? (entry.unit === "%" ? "%" : " " + entry.unit) : ""}`
        : "—";
      const stale = entry && !("value" in entry) && entry.reason === "stale";
      const cands = m.candidates || [];
      const selectHtml = cands.length ? `
        <select class="host-health-map-select" data-metric-key="${this._esc(m.key)}">
          <option value="">${m.source === "auto" ? "— auto —" : "— none —"}</option>
          ${cands.map(c => `<option value="${this._esc(c.entity_id)}"${mappings[m.key] === c.entity_id ? " selected" : ""}>${this._esc(c.friendly_name)}${c.disabled ? " (disabled)" : ""}</option>`).join("")}
        </select>` : "";
      return `
        <div class="cfg-row">
          <label>${this._esc(this._tx(m.label))}${m.recommended ? "" : ` <span class="toggle-desc">optional</span>`}</label>
          <span style="font-family:var(--font-mono);font-size:11px">${valueText}${stale ? " (stale)" : ""}</span>
          <span class="${STATUS_CLS[m.status] || "diag-off"}">${STATUS_LABEL[m.status] || (m.status || "").toUpperCase()}</span>
        </div>
        ${selectHtml ? `<div class="cfg-row">${selectHtml}</div>` : ""}`;
    };

    const setupNotes = metrics.filter(m => m.recommended && (m.status === "missing" || m.status === "disabled"));
    const setupGuidance = setupNotes.length ? `
      <div class="stub-body">Missing or disabled recommended readings: ${setupNotes.map(m => this._esc(this._tx(m.label))).join(", ")}. In Home Assistant: Settings → Devices &amp; services → System Monitor → its entities → enable the ones you want (System Monitor disables several by default), then reopen this card.</div>` : "";

    return `
      <div class="stub-body">Reads Home Assistant's own System Monitor sensors for the machine Nova runs on — processor/memory/disk usage, memory &amp; I/O pressure, and (if your hardware exposes it) temperature. Off by default; nothing is read or reported until you turn it on. Disk usage measures capacity, not drive health; I/O pressure measures workload contention, not drive failure. Nova cannot warn you after this machine has completely frozen, since Nova runs on it too.</div>
      <div class="cfg-row">
        <label>Host health awareness</label>
        <button class="toggle-btn ${enabled ? "on" : "off"}" data-cfg-key="host_health_enabled" data-cfg-val="${enabled ? "false" : "true"}">${enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Alerts <span class="toggle-desc">speak or push a problem once it lasts longer than Persistence — off: no alerts, readings only</span></label>
        <button class="toggle-btn ${alertsEnabled ? "on" : "off"}" data-cfg-key="host_health_alerts_enabled" data-cfg-val="${alertsEnabled ? "false" : "true"}" ${enabled ? "" : "disabled"}>${alertsEnabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Announce recovery <span class="toggle-desc">bounded, optional</span></label>
        <button class="toggle-btn ${cfg.host_health_recovery_announce !== false ? "on" : "off"}" data-cfg-key="host_health_recovery_announce" data-cfg-val="${cfg.host_health_recovery_announce !== false ? "false" : "true"}" ${enabled && alertsEnabled ? "" : "disabled"}>${cfg.host_health_recovery_announce !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Persistence (minutes) <span class="toggle-desc">how long a problem must persist before the first alert</span></label>
        <input class="cfg-field cfg-num" type="number" min="2" max="120" step="1" data-cfg-key="host_health_persistence_minutes" value="${cfg.host_health_persistence_minutes ?? 10}" ${enabled ? "" : "disabled"}>
      </div>
      <div class="cfg-row">
        <label>Cooldown (minutes) <span class="toggle-desc">minimum gap between repeat alerts on the same unresolved problem</span></label>
        <input class="cfg-field cfg-num" type="number" min="5" max="720" step="1" data-cfg-key="host_health_cooldown_minutes" value="${cfg.host_health_cooldown_minutes ?? 60}" ${enabled ? "" : "disabled"}>
      </div>
      ${setupGuidance}
      <div class="mode-bind-head">Readings</div>
      ${metrics.length ? metrics.map(metricRow).join("") : `<div class="stub-body">Loading detected readings…</div>`}
      <div class="mode-bind-head">Infrastructure audit</div>
      <div class="stub-body">A separate check every 15 minutes of the sensors you list here. A sensor in percent is flagged above 90 and critical above 96. A binary sensor is flagged when it goes off, or on for a problem sensor. With no sensors listed, the audit does nothing. Pick a room for its alerts, or leave none to only log them.</div>
      <div class="cfg-row">
        <input id="newAuditSensorInput" list="newAuditSensorList" class="cfg-field" style="flex:1" placeholder="type to find a sensor…" autocomplete="off">
        <datalist id="newAuditSensorList">${this._sensorDatalist()}</datalist>
        <button class="mode-chip" id="newAuditSensorAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newAuditSensorChips">${(() => {
        const arr = this._exclArr(cfg.infrastructure_audit_sensors);
        return arr.length
          ? arr.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="new-audit-sensor-del" data-i="${i}" title="Remove">×</button></span>`).join("")
          : `<span class="toggle-desc">None.</span>`;
      })()}</div>
      <div class="cfg-row">
        <label>Audit alerts room</label>
        <select class="cfg-field" data-cfg-key="infrastructure_audit_area">${this._optSelect([["", "— none —"], ...(this._data()?.areas || []).map(a => [a.id, a.name])], cfg.infrastructure_audit_area || "")}</select>
      </div>`;
  }

  _renderHazardScan(res) {
    if (!res || res.ok === false) {
      return `<div class="stub-body">${this._esc(res?.error || "No location configured.")}</div>`;
    }
    const q = res.earthquakes || [], w = res.weather || [], d = res.disasters || [];
    const warn = res.warnings || [];
    if (!q.length && !w.length && !d.length && !warn.length) {
      return `<div class="stub-body">${res.center
        ? this._tHtml("✓ All clear near {place} — no weather warnings or other hazards from the sources that are on.", { place: res.center[0] + ", " + res.center[1] })
        : "✓ All clear near home — no weather warnings or other hazards from the sources that are on."}</div>`;
    }
    let html = warn.length ? this._renderHazardWarnings(warn) : "";
    for (const e of q) {
      const mag = (typeof e.mag === "number") ? `M${e.mag.toFixed(1)}` : "M?";
      html += `<div class="stub-body"><b class="diag-warn">${mag}</b> ${this._tHtml("{place} — {distance} km away", { place: this._esc(e.place), distance: e.dist_km })}</div>`;
    }
    for (const e of w) {
      html += `<div class="stub-body"><b class="diag-down">${this._esc(e.severity)}</b> ${this._esc(e.event)}${e.area ? " — " + this._esc(e.area) : ""}</div>`;
    }
    for (const e of d) {
      html += `<div class="stub-body"><b class="diag-warn">${this._esc(e.category)}</b> ${this._tHtml("{place} — {distance} km away", { place: this._esc(e.title), distance: e.dist_km })}</div>`;
    }
    return html;
  }

  _entName(eid) {
    const st = (this._hass && this._hass.states) ? this._hass.states[eid] : null;
    return (st && st.attributes && st.attributes.friendly_name) || eid;
  }

  _trackerOptions(selected) {
    const states = this._hass?.states || {};
    const cands = Object.keys(states).filter(eid => { const dom = eid.split(".")[0]; return dom === "person" || dom === "device_tracker"; }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _frontDoorOptions(selected) {
    const states = this._hass?.states || {};
    const OPEN_DC = ["door", "garage_door", "opening"];
    const OPEN_RE = /door|entry|front|contact/i;
    const cands = Object.keys(states).filter(eid => {
      if (eid.split(".")[0] !== "binary_sensor") return false;
      const a = states[eid].attributes || {};
      return OPEN_DC.includes(a.device_class || "") || OPEN_RE.test(eid) || OPEN_RE.test(a.friendly_name || "");
    }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none (arrival briefing stays off) —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _travelSensorOptions(selected) {
    const states = this._hass?.states || {};
    const cands = Object.keys(states).filter(eid => {
      const dom = eid.split(".")[0]; if (dom !== "sensor") return false;
      const a = states[eid].attributes || {}, dc = a.device_class || "", unit = a.unit_of_measurement || "";
      return dc === "duration" || /^(min|minutes|h|hr|hrs|hours)$/i.test(unit) || /travel|commute|duration|eta|route|waze|maps|traffic|drive_time|driving|to_work|to_home/i.test(eid);
    }).sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    return [["", "— none —"], ...cands.map(eid => [eid, this._entName(eid)])];
  }

  _anticipationMemoryCardBody() {
    const cfg = this._data()?.config || {};
    const depCals = this._exclArr(cfg.departure_excluded_calendars);
    const onOff = (key, defaultOn, hint) => {
      const on = defaultOn ? cfg[key] !== false : !!cfg[key];
      return `
        <div class="cfg-row">
          <label>${hint.label}${hint.sub ? `<span class="toggle-desc"> — <span>${this._esc(hint.sub)}</span></span>` : ""}</label>
          <button class="toggle-btn ${on ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${on ? "ON" : "OFF"}</button>
        </div>`;
    };
    const num = (key, label, placeholder, min, max, step) => `
      <div class="cfg-row">
        <label>${this._esc(label)}</label>
        <input class="cfg-field cfg-num" type="number" min="${min}" max="${max}" step="${step}" data-cfg-key="${key}" value="${cfg[key] ?? ""}" placeholder="${placeholder}">
      </div>`;
    return `
      ${onOff("departure_alerts_enabled", false, { label: "Departure alerts" })}
      ${onOff("departure_mode_walk", true, { label: "Leave alerts: walking", sub: "needs Google Maps Travel Time" })}
      ${onOff("departure_mode_transit", true, { label: "Leave alerts: public transport", sub: "needs Google Maps Travel Time" })}
      ${onOff("departure_mode_drive", true, { label: "Leave alerts: driving" })}
      ${onOff("departure_use_google", true, { label: "Time journeys with Google Maps Travel Time", sub: "when that integration is set up in Home Assistant" })}
      ${onOff("routine_alerts_enabled", false, { label: "Routine alerts" })}
      ${onOff("suggestion_review_enabled", false, { label: "Review suggestions with AI", sub: "checks each new learned suggestion with the Suggestion Review model (AI Models card) before showing it; sends device and room names to that provider, or keeps them at home with Ollama" })}
      ${onOff("memory_threading_enabled", false, { label: "Memory threading" })}
      ${onOff("pattern_learn_motion", false, { label: "Learn motion/presence triggers" })}
      ${onOff("adaptive_interruption_budget", false, { label: "Adaptive interruptions", sub: "speak less after alerts are repeatedly marked unhelpful" })}
      ${onOff("adaptive_suggestion_threshold", false, { label: "Adaptive suggestions", sub: "adjust the suggestion bar from past feedback" })}
      ${onOff("adaptive_awareness", false, { label: "Adaptive awareness", sub: "routine alerts get Helpful / Not helpful buttons on your phone; Nova adjusts its timing only from your taps" })}
      ${num("observer_group_debounce", "Sibling-burst coalescing (sec)", "90", 0, 600, 10)}
      ${onOff("continued_conversation_enabled", false, { label: "Continued conversation" })}
      ${onOff("continued_conversation_multi_satellite", false, { label: "Follow me between rooms", sub: "reopen the mic where you moved to (needs 2+ satellites)" })}
      ${onOff("continued_conversation_speaker_reopen", true, { label: "Follow-up mic reopen (speaker-aware)" })}
      ${onOff("tts_use_ha_voice", false, { label: "Use Home Assistant default voice" })}
      ${onOff("auto_bootstrap", false, { label: "Install voice add-ons", sub: "installs Piper, Whisper and openWakeWord and builds the voice pipeline at the next restart (Home Assistant OS or Supervised)" })}
      ${num("departure_lead_minutes", "Departure lead (min)", "30", 0, 240, 5)}
      ${num("routine_departure_lead_minutes", "Leave reminder (min)", "15", 0, 120, 5)}
      ${num("memory_threading_hours", "Memory window (hrs)", "48", 1, 336, 1)}
      ${num("memory_threading_max", "Memory max turns", "12", 1, 50, 1)}
      <div class="cfg-row">
        <label>Origin tracker <span class="toggle-desc">blank: home while anyone is home</span></label>
        <select class="cfg-field" data-cfg-key="departure_origin_entity">${this._optSelect(this._trackerOptions(cfg.departure_origin_entity || ""), cfg.departure_origin_entity || "")}</select>
      </div>
      <div class="cfg-row">
        <label>OSRM URL</label>
        <input class="cfg-field" type="text" data-cfg-key="departure_osrm_url" value="${this._esc(cfg.departure_osrm_url || "")}" placeholder="self-host (optional)">
      </div>
      <div class="cfg-row">
        <label>Travel sensor</label>
        <select class="cfg-field" data-cfg-key="departure_travel_sensor">${this._optSelect(this._travelSensorOptions(cfg.departure_travel_sensor || ""), cfg.departure_travel_sensor || "")}</select>
      </div>
      <div class="mode-bind-head">Calendars that never trigger a leave alert <span class="toggle-desc">e.g. birthdays or holidays</span></div>
      <div class="cfg-row">
        <input id="newDepCalInput" list="newDepCalList" class="cfg-field" style="flex:1" placeholder="type to find a calendar…" autocomplete="off">
        <datalist id="newDepCalList">${this._calendarDatalist()}</datalist>
        <button class="mode-chip" id="newDepCalAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newDepCalChips">${depCals.length
        ? depCals.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="new-dep-cal-del" data-i="${i}" title="Remove">×</button></span>`).join("")
        : `<span class="toggle-desc">None.</span>`}</div>
      <div class="stub-body">Departure warns when to leave for calendar events. It lists each way of travelling you have switched on that gives a different leave time, and reminds you again at a later leave time only if everyone who was home at the first alert still is. Walking and public transport need the Google Maps Travel Time integration; driving also works from open-source routing. Journeys start from home while anyone is home, otherwise from the Origin tracker you pick, or the first person with a position. Routine alerts learn per-person timing over about a week. Continued conversation keeps the mic open after a question.</div>`;
  }

  _memoryCardBody() {
    const cfg = this._data()?.config || {};
    const stats = cfg.memory_stats || {};
    return `
      <div class="cfg-row"><label>Backend</label><span>${this._esc(stats.backend || "—")}</span></div>
      <div class="cfg-row"><label>Stored Memories</label><span>${this._esc(stats.total_memories ?? 0)}</span></div>
      <div class="stub-body">Full review, edit, and forget lives on the Memory tab.</div>`;
  }

  _observerTuningCardBody() {
    const s = this._data()?.config?.observer_stats || {};
    const row = (label, value, cls) => `<div class="cfg-row"><label>${this._esc(label)}</label><span class="${cls || ""}">${value}</span></div>`;
    const rateLimit = s.rate_limit ?? 30;
    const presenceRows = (s.presence || []).map(p =>
      row(`${p.name}${p.gps ? " 📍" : ""}`, `${this._esc(p.zone)}${p.distance_km != null ? " · " + p.distance_km + " km" : ""}`)).join("");
    const llmLabel = s.llm_breaker === "open" ? "LOCAL-ONLY" : s.llm_breaker === "half_open" ? "PROBING" : "ONLINE";
    const llmCls = s.llm_breaker === "open" ? "diag-down" : s.llm_breaker === "half_open" ? "diag-warn" : "diag-ok";
    return `
      ${row("Status", s.running ? "RUNNING" : "STOPPED", s.running ? "diag-ok" : "diag-off")}
      ${row("Calls / Hour", `${s.calls_last_hour || 0} / ${rateLimit <= 0 ? "∞" : rateLimit}`)}
      <div class="cfg-row">
        <label>Hourly Cap <span class="toggle-desc">0 = unlimited</span></label>
        <input class="cfg-field cfg-num" type="number" min="0" step="1" id="newObserverRateLimit" value="${rateLimit}">
      </div>
      ${row("Events 24h", s.events_24h || 0)}
      ${row("Flagged 24h", s.flagged_24h || 0)}
      ${row("Spoken 24h", s.spoken_24h || 0)}
      ${row("Cognition", s.cognition_enabled ? "ACTIVE" : "OFF", s.cognition_enabled ? "diag-ok" : "diag-off")}
      ${row("Tracked Entities", s.cog_entities || 0)}
      ${row("Predictable", s.cog_predictable || 0)}
      ${row("Routines Learned", s.cog_routines || 0)}
      ${row("Presence Routines", s.cog_presence || 0)}
      ${presenceRows}
      ${row("Cog Escalated", s.cog_escalated || 0)}
      ${row("Local Decisions", this._tHtml("{rate}% ({local} local / {cloud} cloud)", { rate: s.local_rate || 0, local: s.local_decisions || 0, cloud: s.cloud_calls || 0 }))}
      ${row("Learned Patterns", s.learned_patterns || 0)}
      ${row("LLM Link", llmLabel, llmCls)}`;
  }

  _optInEntityDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => {
      const dom = eid.split(".")[0];
      return dom === "binary_sensor" || dom === "device_tracker" || dom === "person" || dom === "sensor";
    }).sort().map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }

  _plList() {
    let incl = this._data()?.config?.pattern_include_entities || [];
    if (!Array.isArray(incl)) { try { incl = JSON.parse(incl) || []; } catch (_) { incl = []; } }
    return incl;
  }

  _routineLearningCardBody() {
    const cfg = this._data()?.config || {};
    const awarenessAvailable = cfg.observer_enabled !== false && cfg.cognition_enabled !== false && cfg.camera_event_learning !== false;
    const awarenessOn = awarenessAvailable && cfg.camera_historical_awareness !== false;
    const awarenessMinimum = Math.max(3, Math.min(12, Number(cfg.camera_awareness_min_observations ?? 3) || 3));
    const onOff = (key, label, desc) => `
      <div class="toggle-row">
        <span class="toggle-label">${this._esc(label)}</span>
        <span class="toggle-desc">${this._esc(desc)}</span>
        <button class="toggle-btn ${cfg[key] ? "on" : "off"}" data-cfg-key="${key}" data-cfg-val="${cfg[key] ? "false" : "true"}">${cfg[key] ? "ON" : "OFF"}</button>
      </div>`;
    const incl = this._plList();
    const chips = incl.length
      ? incl.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="new-pl-del" data-i="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">No specific entities added.</span>`;
    return `
      <div class="stub-body">Nova learns routines from device activity (lights, locks, thermostats…) and skips noisy door/window and presence signals by default. Opt them in to build routines from them.</div>
      <div class="toggle-list">
        ${onOff("camera_event_learning", "Learn from camera detections", "Eufy, Frigate, Nest and Nova's own vision analysis — on by default, no images or faces stored")}
        <div class="toggle-row">
          <span class="toggle-label">What I've noticed lately</span>
          <span class="toggle-desc">Use repeated historical camera patterns in conversation, never as current state</span>
          <button class="toggle-btn ${awarenessOn ? "on" : "off"}" data-cfg-key="camera_historical_awareness" data-cfg-val="${awarenessOn ? "false" : "true"}" ${awarenessAvailable ? "" : "disabled"}>${awarenessOn ? "ON" : "OFF"}</button>
        </div>
        <div class="cfg-row">
          <label>Minimum observations <span class="toggle-desc">across more than one day</span></label>
          <input class="cfg-field cfg-num" type="number" min="3" max="12" step="1" data-cfg-key="camera_awareness_min_observations" value="${awarenessMinimum}" ${awarenessAvailable ? "" : "disabled"}>
        </div>
        ${onOff("scene_memory_enabled", "Scene memory", "Keep what the cameras describe so Nova can answer where it last saw something. Text only, no images. Off by default")}
        <div class="cfg-row">
          <label>Keep scene memory for <span class="toggle-desc">days (1 to 90)</span></label>
          <input class="cfg-field cfg-num" type="number" min="1" max="90" step="1" data-cfg-key="scene_memory_retention_days" value="${Math.max(1, Math.min(90, Number(cfg.scene_memory_retention_days ?? 14) || 14))}" ${cfg.scene_memory_enabled ? "" : "disabled"}>
          <button class="mode-chip" id="sceneMemoryClear">Forget everything</button>
        </div>
        ${onOff("pattern_learn_doors", "Learn doors & windows", "Door and window contact sensors")}
        ${onOff("pattern_learn_presence", "Learn presence & arrivals", "People and device trackers (home / away)")}
        ${onOff("pattern_learn_buttons", "Learn button & remote presses", "Suggest “press → scene / action” automations")}
      </div>
      <div class="mode-bind-head">Also learn specific entities <span class="toggle-desc">e.g. a bay occupancy sensor</span></div>
      <div class="cfg-row">
        <input id="newPlEntityInput" list="newPlEntityList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="newPlEntityList">${this._optInEntityDatalist()}</datalist>
        <button class="mode-chip" id="newPlAddEntity">+ Add</button>
      </div>
      <div class="mode-grid" id="newPlChips">${chips}</div>`;
  }

  _exclArr(v) {
    if (!v) return [];
    if (Array.isArray(v)) return v;
    try { const j = JSON.parse(v); return Array.isArray(j) ? j : []; } catch (_) { return []; }
  }
  _calendarDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => eid.startsWith("calendar.")).sort()
      .map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }
  _allEntityDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).sort().map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }
  // The home's own locks, for the lockdown exempt list (8.29.0).
  _lockDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => eid.startsWith("lock.")).sort()
      .map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }
  // Sensors and binary sensors, for the infrastructure audit list (8.28.0).
  _sensorDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => /^(binary_)?sensor\./.test(eid)).sort()
      .map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
  }
  _domainDatalist() {
    const states = this._hass?.states || {};
    const doms = [...new Set(Object.keys(states).map(e => e.split(".")[0]))].sort();
    return doms.map(dm => `<option value="${this._esc(dm)}">${this._esc(dm)}</option>`).join("");
  }
  _labelDatalist() {
    const labels = this._data()?.available_labels || [];
    return labels.map(l => `<option value="${this._esc(l.name)}">${this._esc(l.name)}</option>`).join("");
  }
  async _exclSave(key, arr) {
    if (this._liveData && this._liveData.config) this._liveData.config[key] = arr;
    await this._saveSetting(key, JSON.stringify(arr));
  }

  _excludedEntitiesCardBody() {
    const cfg = this._data()?.config || {};
    const ents = this._exclArr(cfg.excluded_entities);
    const doms = this._exclArr(cfg.excluded_domains);
    const labs = this._exclArr(cfg.excluded_labels);
    const chipRow = (arr, cls) => arr.length
      ? arr.map((e, i) => `<span class="new-pl-chip">${this._esc(e)}<button class="${cls}" data-i="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">None.</span>`;
    return `
      <div class="stub-body">Entities you exclude are removed from Nova's awareness — presence detection, room routing, the observer and routine learning all skip them. Home Assistant still has the entity, and Nova can still control it if you ask by name.</div>
      <div class="mode-bind-head">Exclude specific entities</div>
      <div class="cfg-row">
        <input id="newExclEntInput" list="newExclEntList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="newExclEntList">${this._allEntityDatalist()}</datalist>
        <button class="mode-chip" id="newExclEntAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclEntChips">${chipRow(ents, "new-excl-ent-del")}</div>
      <div class="mode-bind-head">Exclude whole domains <span class="toggle-desc">e.g. light, switch — every entity in the domain</span></div>
      <div class="cfg-row">
        <input id="newExclDomInput" list="newExclDomList" class="cfg-field" style="flex:1" placeholder="type a domain…" autocomplete="off">
        <datalist id="newExclDomList">${this._domainDatalist()}</datalist>
        <button class="mode-chip" id="newExclDomAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclDomChips">${chipRow(doms, "new-excl-dom-del")}</div>
      <div class="mode-bind-head">Exclude by label <span class="toggle-desc">every entity carrying a Home Assistant label</span></div>
      <div class="cfg-row">
        <input id="newExclLabInput" list="newExclLabList" class="cfg-field" style="flex:1" placeholder="type a label…" autocomplete="off">
        <datalist id="newExclLabList">${this._labelDatalist()}</datalist>
        <button class="mode-chip" id="newExclLabAdd">+ Add</button>
      </div>
      <div class="mode-grid" id="newExclLabChips">${chipRow(labs, "new-excl-lab-del")}</div>`;
  }

  // Cameras — enable/rename/location settings deliberately avoid the
  // generic _saveSetting/_render round-trip (see Classic's own
  // _rerenderCameraSettings comment): a full re-render would blow away
  // whatever a user is mid-typing in the rename input, so only the
  // #newCamsetBody sub-tree is patched, matching Classic's #camset-body.
  _renderCameraSettingsRows() {
    const cfg = this._data()?.config || {};
    const cams = cfg.cameras || [];
    if (!cams.length) return `<div class="stub-body">No camera entities in Home Assistant.</div>`;
    const names = cfg.camera_names || {};
    const nOn = cams.filter(c => c.enabled !== false).length;
    const open = !!this._camListOpen;
    const head = `
      <div class="cfg-row">
        <button class="new-cam-collapse" id="newCamListToggle" aria-expanded="${open}">
          <span class="new-cam-caret">${open ? "▾" : "▸"}</span> ${this._tHtml("{on} of {total} cameras in use", { on: nOn, total: cams.length })}
        </button>
        <div style="display:flex;gap:6px">
          <button class="mode-chip" id="newCamEnableAll">Enable all</button>
          <button class="mode-chip" id="newCamDisableAll">Disable all</button>
        </div>
      </div>`;
    const rows = cams.map(c => {
      const enabled = c.enabled !== false;
      const custom = names[c.entity_id] || "";
      const mode = c.location_mode || "auto";
      const resolved = c.outdoor ? "outdoor" : "indoor";
      const chip = (m, label) => `<button class="mode-chip new-cam-loc-chip ${mode === m ? "mode-chip-on" : ""}" data-loc="${m}" data-cam="${this._esc(c.entity_id)}">${label}</button>`;
      return `
        <div class="new-camset-row" data-cam="${this._esc(c.entity_id)}">
          <div class="cfg-row">
            <label>${this._esc(c.entity_id)}</label>
            <button class="toggle-btn ${enabled ? "on" : "off"} new-cam-enable-toggle" data-cam="${this._esc(c.entity_id)}">${enabled ? "ON" : "OFF"}</button>
          </div>
          <div class="cfg-row">
            <input class="cfg-field new-camset-name" style="flex:1" type="text" data-cam="${this._esc(c.entity_id)}" value="${this._esc(custom)}" placeholder="${this._esc(c.raw_name || c.entity_id)}" autocomplete="off">
          </div>
          <div class="mode-grid">
            ${chip("auto", resolved === "outdoor" ? "AUTO (outdoor)" : "AUTO (indoor)")}
            ${chip("indoor", "⌂ INDOOR")}
            ${chip("outdoor", "▲ OUTDOOR")}
          </div>
        </div>`;
    }).join("");
    return head + `<div id="newCamList"${open ? "" : " hidden"}>${rows}</div>`;
  }

  _camerasCardBody() {
    const cfg = this._data()?.config || {};
    return `
      <div class="stub-body">Names are Nova-only (HA untouched; blank reverts). Location governs intrusion + outdoor-event filtering — AUTO shows what the heuristics resolve.</div>
      <div class="cfg-row">
        <label>Camera Watch — auto-analyze doorbell and person events</label>
        <button class="toggle-btn ${cfg.camera_auto_analyze !== false ? "on" : "off"}" data-cfg-key="camera_auto_analyze" data-cfg-val="${cfg.camera_auto_analyze !== false ? "false" : "true"}">${cfg.camera_auto_analyze !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Also analyze motion events <span class="toggle-desc">noisier; off by default</span></label>
        <button class="toggle-btn ${cfg.camera_auto_analyze_motion === true ? "on" : "off"}" data-cfg-key="camera_auto_analyze_motion" data-cfg-val="${cfg.camera_auto_analyze_motion === true ? "false" : "true"}">${cfg.camera_auto_analyze_motion === true ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Package Watch — detect packages and mail at the door</label>
        <button class="toggle-btn ${cfg.package_detection === true ? "on" : "off"}" data-cfg-key="package_detection" data-cfg-val="${cfg.package_detection === true ? "false" : "true"}">${cfg.package_detection === true ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Visitor Learning — silently log strangers seen at the door</label>
        <button class="toggle-btn ${cfg.visitor_learning !== false ? "on" : "off"}" data-cfg-key="visitor_learning" data-cfg-val="${cfg.visitor_learning !== false ? "false" : "true"}">${cfg.visitor_learning !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Face recognition source</label>
        <select class="cfg-field" data-cfg-key="recognition_source">${this._optSelect([["off", "Off"], ["both", "Both (Double Take + Frigate)"], ["frigate", "Frigate only (sub_label)"], ["doubletake", "Double Take only"]], cfg.recognition_source || "off")}</select>
      </div>
      <div class="cfg-row">
        <label>Recognition confidence</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="1" step="0.05" data-cfg-key="identity_min_confidence" value="${cfg.identity_min_confidence ?? ""}" placeholder="0.45">
      </div>
      <div id="newCamsetBody">${this._renderCameraSettingsRows()}</div>`;
  }

  _rerenderCameraSettings() {
    const host = this.shadowRoot?.getElementById("newCamsetBody");
    if (!host) return;
    this._setHtml(host, this._renderCameraSettingsRows());
    this._wireCameraSettings();
  }

  _wireCameraSettings() {
    const root = this.shadowRoot;
    const applyDisabled = async (next) => {
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "disabled_cameras", value: JSON.stringify(next) });
        if (this._liveData?.config) {
          this._liveData.config.disabled_cameras = next;
          const off = new Set(next);
          (this._liveData.config.cameras || []).forEach(c => { c.enabled = !off.has(c.entity_id); });
        }
        this._rerenderCameraSettings();
      } catch (err) { console.error("Nova: camera enable/disable failed", err); }
    };
    const curDisabled = () => {
      const v = (this._data()?.config || {}).disabled_cameras;
      return Array.isArray(v) ? v.slice() : [];
    };
    root.querySelectorAll(".new-cam-enable-toggle[data-cam]").forEach(btn => {
      btn.addEventListener("click", () => {
        const cam = btn.getAttribute("data-cam"), cur = curDisabled(), isOff = cur.includes(cam);
        applyDisabled(isOff ? cur.filter(c => c !== cam) : [...cur, cam]);
      });
    });
    root.getElementById("newCamListToggle")?.addEventListener("click", () => {
      this._camListOpen = !this._camListOpen;
      this._rerenderCameraSettings();
    });
    const enAll = root.getElementById("newCamEnableAll");
    if (enAll) enAll.addEventListener("click", () => applyDisabled([]));
    const disAll = root.getElementById("newCamDisableAll");
    if (disAll) disAll.addEventListener("click", () => applyDisabled(((this._data()?.config || {}).cameras || []).map(c => c.entity_id)));

    root.querySelectorAll(".new-camset-name").forEach(input => {
      input.dataset.saved = input.value;
      const save = async () => {
        const entity = input.getAttribute("data-cam");
        const name = input.value;
        if (name === input.dataset.saved) return;
        try {
          const res = await this._hass.callWS({ type: "nova/rename_camera", entity_id: entity, name });
          input.dataset.saved = name;
          if (this._liveData?.config) {
            this._liveData.config.camera_names = res?.camera_names || {};
            if (Array.isArray(res?.cameras)) this._liveData.config.cameras = res.cameras;
          }
        } catch (err) { console.error("Nova: camera rename failed", err); }
      };
      input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); input.blur(); } });
      input.addEventListener("blur", save);
    });

    root.querySelectorAll(".new-cam-loc-chip").forEach(chipEl => {
      chipEl.addEventListener("click", async () => {
        const entity = chipEl.getAttribute("data-cam");
        const m = chipEl.getAttribute("data-loc");
        chipEl.disabled = true;
        try {
          const res = await this._hass.callWS({ type: "nova/camera_location", entity_id: entity, mode: m });
          if (Array.isArray(res?.cameras) && this._liveData?.config) this._liveData.config.cameras = res.cameras;
          this._rerenderCameraSettings();
        } catch (err) {
          console.error("Nova: camera location failed", err);
          chipEl.disabled = false;
        }
      });
    });
  }

  _dbTrainRow(e) {
    const ts = String(e.ts || "").replace("T", " ").replace("Z", "").slice(5, 16);
    const src = String(e.image_source || "?");
    const cat = e.category || "";
    const desc = this._esc(e.summary || e.analysis || "");
    // "speak" is what Nova would actually say aloud for this event — logged
    // regardless of whether announcements_enabled let it through, so you can
    // see after the fact what a notable event would have sounded like.
    const speak = (e.speak || "").trim();
    const speakLine = speak ? `<div class="toggle-desc" style="margin-top:2px"><i>"${this._esc(speak)}"</i></div>` : "";
    return `
      <div class="cfg-row"${e.notable ? ' style="color:var(--gold)"' : ""}>
        <label>${this._esc(ts)} · ${this._esc(src)}${cat ? " · " + this._esc(cat) : ""}</label>
        <span class="toggle-desc">${desc}</span>
      </div>
      ${speakLine}`;
  }

  _doorbellTrainingCardBody() {
    const t = this._data()?.doorbellTraining || {};
    const stats = t.stats || {};
    const events = t.recent || [];
    const patterns = t.patterns || [];
    const total = stats.total || 0;
    const notable = stats.notable || 0;
    const bySource = stats.by_source || {};
    const srcLine = Object.keys(bySource).length
      ? Object.entries(bySource).map(([k, v]) => `${k} ${v}`).join(" · ")
      : "none yet";
    const rows = events.length
      ? events.slice().reverse().map(e => this._dbTrainRow(e)).join("")
      : `<div class="stub-body">No analysed doorbell events yet. Run a backlog scan, or wait for the next doorbell press.</div>`;
    // Patterns are timing-only — no names, no face matching. Nova has no
    // local face model; that needs Frigate or DoubleTake (recognition.py),
    // neither configured here. This just clusters the vision model's own
    // category label (delivery/mail/person/...) by camera and time of day.
    const patternsBlock = patterns.length
      ? `<div class="mode-bind-head">Recurring patterns (timing only — not face recognition)</div>
         <ul style="margin:0 0 10px;padding-left:18px;font-size:12px;color:var(--ink-dim);line-height:1.7">
           ${patterns.map(p => `<li>${this._esc(p.description)}</li>`).join("")}
         </ul>`
      : `<div class="stub-body">No recurring patterns yet — needs a few more days of data, or nothing repeats at a consistent time yet.</div>`;
    return `
      <div class="stub-body">Analysed doorbell events — Nova's visitor training data. Each press is logged automatically; run a backlog scan to mine the recorded-event history into the dataset.</div>
      <div class="cfg-row">
        <label>Scan limit</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="newDbtLimit" class="cfg-field cfg-num" type="number" min="1" max="500" value="40" title="Max events to analyse">
          <button class="mode-chip" id="newDbtScan">Scan backlog</button>
        </div>
      </div>
      <div class="stub-body">${this._tHtml("{total} analysed · {notable} notable · {sources}", { total, notable, sources: this._esc(srcLine) })}</div>
      ${patternsBlock}
      ${rows}`;
  }

  // Wellbeing status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics/Hazard/Energy).
  async _fetchBio() {
    if (!this._hass) return;
    try {
      this._bio = await this._hass.callWS({ type: "nova/biometrics", action: "status" });
    } catch (_) { this._bio = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _wellbeingContextCardBody() {
    const b = this._bio || {};
    if (b.error) {
      return `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`;
    }
    const status = b.enabled
      ? `<span class="diag-ok">${this._tHtml(b.found === 1 ? "ON · {count} sensor" : "ON · {count} sensors", { count: b.found || 0 })}</span>`
      : `<span class="diag-off">OFF</span>`;
    const ents = b.entities || [];
    let body;
    if (!b.enabled) {
      body = `<div class="stub-body">Off — enable to let Nova use wearable context. Health readings are never diagnosed or alarmed on.</div>`;
    } else if (!ents.length) {
      body = `<div class="stub-body">No wearable entities found. Connect a wearable integration (Withings, Google Fit, Oura, etc.) to Home Assistant.</div>`;
    } else {
      body = ents.map(e =>
        `<div class="cfg-row"><label>${this._esc((e.kind || "").replace(/_/g, " "))}</label><span>${this._esc(e.value)}${e.unit ? " " + this._esc(e.unit) : ""}</span></div>`).join("");
    }
    return `
      <div class="stub-body">Lets Nova read a connected wearable (heart rate, sleep, steps) so it can be quieter when you're resting. Context only — not medical. Off by default; health data stays private.</div>
      <div class="cfg-row">
        <label>Status</label>
        <div style="display:flex;align-items:center;gap:8px">${status}<button class="mode-chip" id="newBioToggle">${b.enabled ? "✕ DISABLE" : "◉ ENABLE"}</button></div>
      </div>
      ${body}`;
  }

  _characterResearchCardBody() {
    const cfg = this._data()?.config || {};
    return `
      <div class="cfg-row">
        <label>Banter level</label>
        <select class="cfg-field" data-cfg-key="banter_level">
          ${this._optSelect([["0", "Plain — no wit"], ["1", "Dry — occasional wit (default)"], ["2", "Full — expressive wit"]], String(cfg.banter_level ?? "1"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Web research backend</label>
        <select class="cfg-field" data-cfg-key="search_backend">
          ${this._optSelect([["off", "Off"], ["duckduckgo", "DuckDuckGo (no key)"], ["searxng", "SearXNG (self-hosted)"]], cfg.search_backend || "off")}
        </select>
      </div>
      <div class="cfg-row">
        <label>SearXNG URL</label>
        <input class="cfg-field" type="text" data-cfg-key="searxng_url" value="${this._esc(cfg.searxng_url || "")}" placeholder="http://searxng.local:8080" autocomplete="off">
      </div>
      <div class="cfg-row">
        <label>Calendar tight gap <span class="toggle-desc">minutes between events treated as back-to-back</span></label>
        <input class="cfg-field cfg-num" type="number" min="0" max="120" step="5" data-cfg-key="calendar_tight_gap_min" value="${this._esc(cfg.calendar_tight_gap_min ?? 15)}">
      </div>`;
  }

  // Document Library (RAG) — fetched once per element lifetime, like the
  // other on-demand cards (Diagnostics/Hazard/Energy/Wellbeing).
  async _fetchDocLibrary() {
    if (!this._hass) return;
    try {
      this._docLib = await this._hass.callWS({ type: "nova/documents", action: "status" });
    } catch (_) { this._docLib = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  async _fetchVectorBackend() {
    if (!this._hass) return;
    try {
      this._vecbk = await this._hass.callWS({ type: "nova/semantic_search", action: "status" });
    } catch (_) { this._vecbk = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _renderDocLibraryList() {
    const d = this._docLib || {};
    if (d.error) return `<div class="stub-body">Couldn't reach the library — restart Home Assistant after updating, then reopen.</div>`;
    const sources = d.sources || [];
    if (!sources.length) {
      return `<div class="stub-body">No documents ingested yet. Add PDF/.txt/.md files to <code>${this._esc(d.directory || "nova/documents in your config folder")}</code> and press Ingest.</div>`;
    }
    return sources.map(s => `
      <div class="cfg-row">
        <label>${this._esc(s.source)}</label>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="toggle-desc">${this._tHtml("{count} chunks", { count: s.chunks })}</span>
          <button class="new-doclib-del" data-src="${this._esc(s.source)}" title="Remove document">✕</button>
        </div>
      </div>`).join("");
  }

  _renderDocSearchResults(hits) {
    if (!hits || !hits.length) return `<div class="stub-body">No matches. Try different words, or ingest more documents.</div>`;
    return hits.map(h => {
      const score = (h.score != null) ? ` · ${Math.round(h.score * 100)}%` : "";
      const excerpt = (h.text || "").slice(0, 220);
      return `<div class="stub-body"><b>${this._esc(h.source || "?")}${score}</b><br>${this._esc(excerpt)}${h.text && h.text.length > 220 ? "…" : ""}</div>`;
    }).join("");
  }

  _renderVectorBackendBody() {
    const v = this._vecbk || {};
    if (v.error) return "";
    if (v.enabled) {
      return `
        <div class="cfg-row"><label>Search</label><span class="diag-ok">◉ SEMANTIC (Ollama)</span></div>
        <div class="stub-body">Meaning-based matching via Ollama ${this._esc(v.model || "nomic-embed-text")}${v.vector_count ? ` · ${v.vector_count} vectors` : " · re-ingest to embed your documents"}.</div>
        <div class="stub-body">Saved facts are matched by meaning too. Their text goes to the same Ollama server.</div>
        <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="disable">✕ DISABLE SEMANTIC SEARCH</button></div>`;
    }
    if (!v.ollama_configured) {
      return `
        <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
        <div class="stub-body">Works everywhere with no setup. Semantic search needs an Ollama host — set the LLM base URL to your Ollama server and pull an embed model (ollama pull nomic-embed-text).</div>`;
    }
    return `
      <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
      <div class="stub-body">${this._tHtml("Enable semantic search to match on meaning, using your Ollama server ({model}). No install, no ChromaDB. Re-ingest afterward to embed existing docs.", { model: this._esc(v.model || "nomic-embed-text") })}</div>
      <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="enable">⬆ ENABLE SEMANTIC SEARCH</button></div>`;
  }

  _documentLibraryCardBody() {
    const d = this._docLib || {};
    const backend = d.fts ? "KEYWORD" : "NONE";
    return `
      <div class="stub-body">Drop manuals &amp; receipts (PDF, .txt, .md) into <code>${this._esc(d.directory || "nova/documents in your config folder")}</code> or upload below, then ingest. Ask Nova "what's the furnace filter size?" and it answers from your paperwork.</div>
      <div class="cfg-row"><label>Backend</label><span>${this._tHtml("{backend} · {count} chunks", { backend: this._esc(backend), count: d.chunk_count || 0 })}</span></div>
      ${this._renderVectorBackendBody()}
      <div class="mode-bind-head">Library</div>
      <div class="cfg-row">
        <button class="mode-chip" id="newDoclibUpload">⬆ UPLOAD FILE</button>
        <input type="file" id="newDoclibFile" accept=".pdf,.txt,.md" style="display:none">
        <button class="mode-chip" id="newDoclibIngest">⟳ INGEST FOLDER</button>
      </div>
      <div class="cfg-row">
        <input id="newDoclibSearch" class="cfg-field" style="flex:1" type="text" placeholder="test a search — e.g. furnace filter size" autocomplete="off">
      </div>
      <div id="newDoclibBody">${this._renderDocLibraryList()}</div>
      <div class="mode-bind-head">Watch folders <span class="toggle-desc">one per line/comma, e.g. /media/downloads</span></div>
      <div class="cfg-row">
        <input id="newDoclibWatch" class="cfg-field" style="flex:1" type="text" value="${this._esc(this._docLibWatchValue())}" autocomplete="off">
        <button class="mode-chip" id="newDoclibScan">⟳ SCAN WATCH</button>
      </div>`;
  }

  _docLibWatchValue() {
    const wf = this._data()?.config?.document_watch_folders;
    if (!wf) return "";
    return Array.isArray(wf) ? wf.join("\n") : wf;
  }

  _rerenderDocLibraryBody() {
    const host = this.shadowRoot?.getElementById("newDoclibBody");
    if (!host) return;
    this._setHtml(host, this._renderDocLibraryList());
    this._wireDocLibraryDeletes();
  }

  _wireDocLibraryDeletes() {
    this.shadowRoot?.querySelectorAll(".new-doclib-del").forEach(btn => {
      btn.addEventListener("click", async () => {
        const src = btn.getAttribute("data-src");
        if (!src || !this._hass) return;
        if (!window.confirm(this._t("Remove \"{name}\" from the library? This deletes the file and its indexed chunks.", { name: src }))) return;
        try {
          await this._hass.callWS({ type: "nova/documents", action: "delete", filename: src });
          await this._fetchDocLibrary(); // triggers a full _render() when in the settings tab
        } catch (err) { console.error("Nova: document delete failed", err); }
      });
    });
  }

  _wireDocLibrary() {
    const root = this.shadowRoot;
    this._wireDocLibraryDeletes();

    const ingestBtn = root.getElementById("newDoclibIngest");
    if (ingestBtn) {
      ingestBtn.addEventListener("click", async () => {
        if (!this._hass) return;
        ingestBtn.disabled = true;
        const orig = ingestBtn.textContent;
        this._setText(ingestBtn, "⟳ INGESTING…");
        try {
          await this._hass.callWS({ type: "nova/documents", action: "ingest" });
          await this._fetchDocLibrary();
        } catch (err) {
          console.error("Nova: ingest failed", err);
        } finally {
          ingestBtn.disabled = false;
          this._setText(ingestBtn, orig);
        }
      });
    }

    const q = root.getElementById("newDoclibSearch");
    if (q) {
      q.addEventListener("keydown", async (ev) => {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        const query = q.value.trim();
        if (!query || !this._hass) { this._rerenderDocLibraryBody(); return; }
        try {
          const res = await this._hass.callWS({ type: "nova/documents", action: "search", query });
          const host = root.getElementById("newDoclibBody");
          if (host) this._setHtml(host, this._renderDocSearchResults(res?.results || []));
        } catch (err) { console.error("Nova: document search failed", err); }
      });
    }

    const upBtn = root.getElementById("newDoclibUpload");
    const fileInput = root.getElementById("newDoclibFile");
    if (upBtn && fileInput) {
      upBtn.addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file || !this._hass) return;
        if (file.size > 25 * 1024 * 1024) { fileInput.value = ""; return; }
        upBtn.disabled = true;
        const orig = upBtn.textContent;
        this._setText(upBtn, "⬆ UPLOADING…");
        try {
          const b64 = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result).split(",")[1] || "");
            r.onerror = () => reject(new Error("read failed"));
            r.readAsDataURL(file);
          });
          const res = await this._hass.callWS({ type: "nova/documents", action: "upload", filename: file.name, content: b64 });
          if (res.ok) {
            await this._fetchDocLibrary();
          }
        } catch (err) {
          console.error("Nova: document upload failed", err);
        } finally {
          upBtn.disabled = false;
          this._setText(upBtn, orig);
          fileInput.value = "";
        }
      });
    }

    const watchField = root.getElementById("newDoclibWatch");
    const saveWatch = async () => {
      if (!this._hass || !watchField) return;
      try { await this._hass.callWS({ type: "nova/update_config", key: "document_watch_folders", value: watchField.value.trim() }); } catch (_) {}
    };
    if (watchField) watchField.addEventListener("blur", saveWatch);

    const scanBtn = root.getElementById("newDoclibScan");
    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        if (!this._hass) return;
        await saveWatch();
        scanBtn.disabled = true;
        const orig = scanBtn.textContent;
        this._setText(scanBtn, "⟳ SCANNING…");
        try {
          const res = await this._hass.callWS({ type: "nova/documents", action: "scan_watch" });
          if (res.watched > 0) {
            await this._fetchDocLibrary();
          }
        } catch (err) {
          console.error("Nova: watch scan failed", err);
        } finally {
          scanBtn.disabled = false;
          this._setText(scanBtn, orig);
        }
      });
    }

    const vecbkToggle = root.getElementById("newVecbkToggle");
    if (vecbkToggle) {
      vecbkToggle.addEventListener("click", async () => {
        if (!this._hass) return;
        const mode = vecbkToggle.getAttribute("data-mode") || "enable";
        try {
          await this._hass.callWS({ type: "nova/semantic_search", action: mode });
        } catch (err) {
          console.error("Nova: semantic search toggle failed", err);
        }
        await this._fetchVectorBackend();
      });
    }
  }
  _mediaPlayerOptions(selected) {
    const states = this._hass?.states || {};
    const eids = Object.keys(states).filter(e => e.startsWith("media_player.")).sort();
    if (selected && !eids.includes(selected)) eids.unshift(selected);
    return [["", "— none —"], ...eids.map(e => {
      const st = states[e];
      const fn = (st && st.attributes && st.attributes.friendly_name) || e;
      return [e, fn];
    })];
  }

  _optSelect(pairs, current) {
    return pairs.map(([v, label]) => `<option value="${this._esc(v)}"${v === current ? " selected" : ""}>${this._esc(label)}</option>`).join("");
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  _renderData() {
    if (!this._renderedOnce) return;
    const d = this._data();
    const root = this.shadowRoot;
    if (!d) return;

    const onboardingMount = root.getElementById("onboardingMount");
    if (onboardingMount) {
      // The welcome card's Setup Doctor line: fetched once, not on every
      // 20s poll, because the check makes a real LLM/TTS probe.
      const ob = d.onboarding;
      if (this._hass && ob && !ob.dismissed && (ob.show || ob.fresh) && !this._setupHealth && !this._welcomeHealthPending) {
        this._welcomeHealthPending = true;
        this._hass.callWS({ type: "nova/get_setup_health" })
          .then(res => { this._setupHealth = res; },
                err => { this._setupHealth = { error: true, unauthorized: err?.code === "unauthorized" }; })
          .finally(() => { this._welcomeHealthPending = false; this._renderData(); });
      }
      this._setHtml(onboardingMount, this._onboardingHtml(d.onboarding));
      this._wireOnboarding();
    }

    this._renderOperationalMode();

    // hero state line
    const state = this._coreState();
    const lineEl = root.getElementById("stateLine");
    const subEl = root.getElementById("stateSub");
    const lines = {
      idle: ["Watching over the house.", "ALL QUIET · NOTHING NEEDS YOU RIGHT NOW"],
      reasoning: ["Something just happened.", "CHECK THE ACTIVITY FEED BELOW"],
      asleep: ["Everyone's asleep. Staying quiet.", "A GROUND-FLOOR BREACH WOULD STILL WAKE ME"],
    };
    if (lineEl) this._setText(lineEl, lines[state][0]);
    if (subEl) this._setText(subEl, lines[state][1]);
    // At rest the hero word is a greeting, picked once here and changed by the
    // rotation (see _presentAmbient), so it is no longer always "Hello".
    if (!this._heroGreeting) this._heroGreeting = this._pickGreeting();
    const words = { idle: this._heroGreeting, reasoning: "Thinking…", asleep: "Goodnight." };
    const wordEl = root.getElementById("heroWord");
    if (wordEl) wordEl.classList.toggle("dim", state === "asleep");
    this._typeHeroWord(words[state]);
    // A card on show keeps the head turned towards it, unless the house sleeps.
    this._targetCoreState(this._presenting && state !== "asleep" ? "presenting" : state);

    // status chips
    const chipDefs = [
      ["Observer", d.status.observer], ["Sleep", d.status.sleep], ["Broadcast", d.status.broadcast],
      ["Notify", d.status.notify], ["Satellites", d.status.satellites],
    ];
    const chipsEl = root.getElementById("chips");
    if (chipsEl) {
      this._setHtml(chipsEl, chipDefs.map(([label, s]) => {
        const warn = (s?.level === "warn") ? " warn" : "";
        return `<div class="chip${warn}"><span class="dot"></span> ${this._esc(label)} <b>${this._esc(s?.state ?? "—")}</b></div>`;
      }).join(""));
    }

    // Formal lockdown is deliberately separate from the alarm controls. It
    // only calls Nova's guarded lockdown command and always asks for a human
    // confirmation before changing state.
    const lockdown = d.lockdown || {};
    const lockdownBtn = root.getElementById("lockdownControl");
    if (lockdownBtn) {
      lockdownBtn.hidden = false;
      lockdownBtn.classList.toggle("active", !!lockdown.active);
      this._setText(lockdownBtn, lockdown.active ? "LOCKDOWN ACTIVE" : "LOCKDOWN OFF");
      lockdownBtn.title = this._tx(lockdown.reason || "Nova formal lockdown");
    }

    // activity feed
    const entries = (this._activityData && this._activityData.length)
      ? this._activityData
      : [{ ts: "--:--", tag: "SYSTEM", msg: "No activity yet." }];
    const feedEl = root.getElementById("feed");
    if (feedEl) {
      this._setHtml(feedEl, entries.map(e => `
        <div class="feed-row">
          <div class="feed-text"><b>${this._esc(e.tag || "")}</b> · <span class="dim">${this._esc(e.msg || "")}</span></div>
          <div class="feed-time">${this._esc(e.ts || "")}</div>
        </div>`).join(""));
    }
    const feedMeta = root.getElementById("feedMeta");
    if (feedMeta) this._setText(feedMeta, this._t("LAST {count}", { count: entries.length }));

    // areas
    const areasGridEl = root.getElementById("areasGrid");
    if (areasGridEl) {
      this._setHtml(areasGridEl, (d.areas || []).map(a => this._areaTileHtml(a)).join(""));
      // Re-wire on every patch — innerHTML above just replaced these nodes,
      // so any listeners from a previous _renderData() are already gone.
      areasGridEl.querySelectorAll(".area-light-toggle[data-light-area]").forEach(btn => {
        btn.addEventListener("click", () => {
          const areaId = btn.getAttribute("data-light-area");
          const name = btn.getAttribute("data-area-name") || "Area";
          const isOn = btn.classList.contains("on");
          this._toggleAreaLights(areaId, name, isOn);
        });
      });
    }
    const areasMeta = root.getElementById("areasMeta");
    if (areasMeta) this._setText(areasMeta, this._t("{occupied} OCCUPIED · {monitored} MONITORED", { occupied: d.occupied, monitored: d.areasMonitored }));

    this._renderSolarPanel();
    this._renderMutesPanel();

    const cog = this._cognitive || {};
    const learning = cog.learning || {};
    const cognitiveState = root.getElementById("cognitiveState");
    if (cognitiveState) this._setText(cognitiveState, cog.running === false ? "STOPPED" : (cog.running ? "RUNNING" : "UNAVAILABLE"));
    const cognitiveMetrics = root.getElementById("cognitiveMetrics");
    if (cognitiveMetrics) {
      const metrics = [
        ["Days learned", learning.days_of_data ?? 0],
        ["State changes", learning.state_changes ?? 0],
        ["Commands", learning.commands ?? 0],
        ["Suggestions", learning.suggestions ?? 0],
        ["Actions", cog.actions_taken ?? 0],
        ["Ignore rules", cog.ignore_rules ?? 0],
      ];
      this._setHtml(cognitiveMetrics, metrics.map(([label, value]) =>
        `<div class="metric"><b>${this._esc(value)}</b><span>${this._esc(label)}</span></div>`).join(""));
    }
    const cognitiveAnalysis = root.getElementById("cognitiveAnalysis");
    if (cognitiveAnalysis) {
      const analysis = cog.last_analysis || {};
      this._setText(cognitiveAnalysis, analysis.summary || analysis.message || "Nova learns from household patterns locally.");
    }

    const goals = d.goals || [];
    const goalList = root.getElementById("goalList");
    const goalsMeta = root.getElementById("goalsMeta");
    if (goalsMeta) this._setText(goalsMeta, this._t("{count} ACTIVE", { count: goals.filter(g => g.status === "active").length }));
    if (goalList) {
      this._setHtml(goalList, goals.length ? goals.map(g => {
        const active = g.status === "active";
        const progress = g.steps_total ? this._t("{done}/{total} STEPS", { done: g.steps_done || 0, total: g.steps_total }) : "OPEN OUTCOME";
        return `<div class="goal-row">
          <div class="goal-copy"><b>${this._esc(g.title || g.outcome || `Goal ${g.id}`)}</b>
            <span>${this._esc(g.outcome || "")}</span>
            <small>${this._esc(String(g.status || "active").toUpperCase())} · ${this._esc(progress)}</small></div>
          <button class="mode-chip goal-action" data-goal-id="${this._esc(g.id)}" data-goal-action="${active ? "cancel" : "delete"}">${active ? "CANCEL" : "DELETE"}</button>
        </div>`;
      }).join("") : `<div class="empty-state">No goals yet.</div>`);
      this._wireGoalActions();
    }

    // camera — collapsed, optional, honest
    const camPanel = root.getElementById("cameraPanel");
    const camStrip = root.getElementById("camStrip");
    if (camPanel && camStrip) {
      const cams = d.cameras || [];
      camPanel.hidden = cams.length === 0;
      const camToggle = root.getElementById("camToggle");
      if (camToggle) this._setText(camToggle, this._camOpen ? "HIDE CAMERAS ▴" : this._t(cams.length === 1 ? "SHOW {count} CAMERA ▾" : "SHOW {count} CAMERAS ▾", { count: cams.length }));
      camStrip.classList.toggle("open", this._camOpen);
      this._setHtml(camStrip, cams.map(c => {
        const eid = c.entity_id;
        const image = this._cameraImages[eid];
        const diag = this._cameraDiagnostics[eid];
        const body = image
          ? `<img src="data:image/jpeg;base64,${image}" alt="${this._esc(c.name || eid)} snapshot">`
          : `<div class="camera-empty">${this._cameraLoading[eid] ? "LOADING…" : "NO SNAPSHOT"}</div>`;
        return `<div class="camera-slot" data-camera="${this._esc(eid)}">
          ${body}<div class="camera-caption"><b>${this._esc(c.name || eid)}</b><span>${this._esc(eid)}</span></div>
          <div class="camera-actions"><button class="mode-chip camera-refresh" data-camera="${this._esc(eid)}">REFRESH</button>
            <button class="mode-chip camera-analyze" data-camera="${this._esc(eid)}">ANALYZE</button>
            <button class="mode-chip camera-diagnose" data-camera="${this._esc(eid)}">DIAGNOSE</button></div>
          ${diag ? `<div class="camera-diagnostic">${this._esc(diag)}</div>` : ""}
        </div>`;
      }).join(""));
      this._wireCameraActions();
    }
    this._localizeDOM(root);
  }

  async _refreshCameraSnapshot(entityId) {
    if (!this._hass || !entityId || this._cameraLoading[entityId]) return;
    this._cameraLoading[entityId] = true;
    this._renderData();
    try {
      const res = await this._hass.callWS({ type: "nova/camera_snapshot", entity_id: entityId });
      this._cameraImages[entityId] = res?.image || null;
    } catch (err) {
      this._cameraImages[entityId] = null;
      this._cameraDiagnostics[entityId] = err?.message || "Snapshot failed";
    } finally {
      this._cameraLoading[entityId] = false;
      this._renderData();
    }
  }

  _wireCameraActions() {
    const root = this.shadowRoot;
    root.querySelectorAll(".camera-refresh").forEach(btn => btn.addEventListener("click", () =>
      this._refreshCameraSnapshot(btn.getAttribute("data-camera"))));
    root.querySelectorAll(".camera-analyze").forEach(btn => btn.addEventListener("click", async () => {
      const entityId = btn.getAttribute("data-camera");
      btn.disabled = true;
      try {
        await this._hass.callService("nova", "analyze_camera", { entity_id: entityId, announce: false });
        this._cameraDiagnostics[entityId] = "Analysis requested. Results will appear in Activity.";
      } catch (err) { this._cameraDiagnostics[entityId] = err?.message || "Analysis failed"; }
      btn.disabled = false;
      this._renderData();
    }));
    root.querySelectorAll(".camera-diagnose").forEach(btn => btn.addEventListener("click", async () => {
      const entityId = btn.getAttribute("data-camera");
      btn.disabled = true;
      try {
        const res = await this._hass.callWS({ type: "nova/camera_diagnostics", entity_id: entityId });
        this._cameraDiagnostics[entityId] = res?.probe?.verdict || "No diagnostic result.";
      } catch (err) { this._cameraDiagnostics[entityId] = err?.message || "Diagnostics failed"; }
      btn.disabled = false;
      this._renderData();
    }));
  }

  _wireGoalActions() {
    this.shadowRoot.querySelectorAll(".goal-action").forEach(btn => btn.addEventListener("click", async () => {
      const action = btn.getAttribute("data-goal-action");
      if (!window.confirm(this._tx(action === "cancel" ? "Cancel this goal?" : "Delete this goal?"))) return;
      await this._goalAction({ action, goal_id: Number(btn.getAttribute("data-goal-id")) });
    }));
  }

  async _goalAction(payload) {
    const out = this.shadowRoot.getElementById("goalResult");
    try {
      const res = await this._hass.callWS({ type: "nova/goal_action", ...payload });
      if (this._liveData && Array.isArray(res?.goals)) this._liveData.goals = res.goals;
      if (out) this._setText(out, "Saved.");
      this._renderData();
    } catch (err) {
      if (out) this._setText(out, err?.message || "Goal action failed.");
    }
  }

  // Canonical order + icon per capability, matching the backend's own
  // ordering (websocket.py's area-caps builder) so a room with many
  // capabilities always shows them in the same, sensible sequence.
  static AREA_CAP_ORDER = ["sat", "spkr", "mmwave", "cam", "light", "switch", "lock", "climate", "door", "leak", "alarm"];
  static AREA_CAP_ICON = {
    sat: "🛰️", spkr: "🔊", mmwave: "📡", cam: "📷", light: "💡", switch: "🔌",
    lock: "🔒", climate: "🌡️", door: "🚪", leak: "💧", alarm: "🔔",
  };

  _areaSparklineSvg(values, color) {
    if (!values || values.length < 2) return "";
    const w = 60, h = 16, pad = 1;
    const min = Math.min(...values), max = Math.max(...values), range = (max - min) || 1;
    const step = (w - pad * 2) / (values.length - 1);
    const pts = values.map((v, i) => {
      const x = pad + i * step;
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }

  _areaTileHtml(a) {
    const caps = (a.caps || []).slice().sort((x, y) =>
      NovaPanel.AREA_CAP_ORDER.indexOf(x) - NovaPanel.AREA_CAP_ORDER.indexOf(y)).slice(0, 5);
    const capsRow = caps.length
      ? `<div class="area-caps">${caps.map(c => `<div class="area-cap" title="${this._esc(c)}">${NovaPanel.AREA_CAP_ICON[c] || "•"}</div>`).join("")}</div>`
      : "";
    const spark = this._sparklines?.[a.id] || {};
    const tempSpark = spark.temp ? this._areaSparklineSvg(spark.temp, "var(--gold)") : "";
    const humSpark = spark.humidity ? this._areaSparklineSvg(spark.humidity, "#6ea8ff") : "";
    const climateRow = (a.temp || a.humidity) ? `
      <div class="area-climate">
        ${a.temp ? `<div class="area-climate-item"><div class="area-climate-num">${this._esc(a.temp)}</div>${tempSpark}</div>` : ""}
        ${a.humidity ? `<div class="area-climate-item"><div class="area-climate-num">${this._esc(a.humidity)}</div>${humSpark}</div>` : ""}
      </div>` : "";
    const hasLights = (a.lights_total || 0) > 0;
    const lit = hasLights && (a.lights_on || 0) > 0;
    const ctlOn = (this._liveData?.config?.light_control_enabled) !== false;
    const lightCtl = hasLights
      ? `<button class="area-light-toggle${lit ? " on" : ""}"${ctlOn ? ` data-light-area="${this._esc(a.id || "")}" data-area-name="${this._esc(a.name)}"` : " disabled"} title="${this._tHtml(ctlOn ? "{on}/{total} lights on — tap to toggle" : "{on}/{total} lights on", { on: a.lights_on, total: a.lights_total })}">${lit ? "ON" : "OFF"}</button>`
      : "";
    return `
      <div class="area-tile${a.active ? " active" : ""}${(a.temp || a.humidity) ? "" : " no-temp"}">
        <div class="area-top">
          <div class="area-name">${this._esc(a.name)}${a.active ? '<span class="live-dot" title="Occupied now"></span>' : ""}</div>
        </div>
        ${capsRow}
        ${climateRow}
        <div class="area-bottom">
          <div class="area-stat">lights <b>${a.lights_on ?? 0}/${a.lights_total ?? 0}</b></div>
          ${lightCtl}
        </div>
      </div>`;
  }

  // Solar summary for the Energy tab's Live panel (8.11.0): nova/solar's
  // self-sufficiency in the header meta and the first sentence of its advice
  // as one line under the flow diagram. The power numbers themselves come
  // from nova/energy_flow. Called from every _renderData(), so it does
  // nothing when the Energy tab is not open.
  _renderSolarPanel() {
    const root = this.shadowRoot;
    const summary = root?.getElementById("solarSummary");
    const sufficiencyEl = root?.getElementById("solarSufficiency");
    if (!summary && !sufficiencyEl) return;
    const s = this._solar;
    const pct = s && !s.error && s.configured && s.self_sufficiency_pct != null
      ? `${s.self_sufficiency_pct}% self-sufficient` : "—";
    const first = String((s && !s.error && (s.advice || [])[0]) || "");
    const end = first.indexOf(". ");
    const line = end >= 0 ? first.slice(0, end + 1) : first;
    if (sufficiencyEl && sufficiencyEl.textContent !== pct) this._setText(sufficiencyEl, pct);
    if (summary) {
      if (summary.textContent !== line) this._setText(summary, line);
      summary.hidden = !line;
    }
  }

  // Muted card: what Nova has been told to stop announcing. Mutes are saved
  // across restarts, so a blanket shush gets a loud banner. Critical safety
  // alerts are never muted. Unmute uses the existing nova.unshush service.
  _renderMutesPanel() {
    const root = this.shadowRoot;
    const panel = root.getElementById("mutesPanel");
    const body = root.getElementById("mutesBody");
    if (!panel || !body) return;
    const m = this._liveData?.config?.output_mutes || {};
    const entities = Array.isArray(m.entities) ? m.entities : [];
    const categories = Array.isArray(m.categories) ? m.categories : [];
    const all = m.all === true;
    if (!all && !entities.length && !categories.length) { panel.hidden = true; this._setHtml(body, ""); return; }
    panel.hidden = false;
    const row = (label, kind, value) => `
      <div class="feed-row">
        <span class="feed-text">${label}${value ? ` <b>${this._esc(value)}</b>` : ""}</span>
        <button class="mode-chip" data-unmute="${kind}" data-unmute-value="${this._esc(value || "")}">Unmute</button>
      </div>`;
    const banner = all ? `
      <div class="mute-banner" role="alert" style="border:1px solid var(--warn,#d9a300);border-radius:8px;padding:10px 12px;margin-bottom:10px">
        <b>Blanket shush is on.</b> Nova is not announcing anything except critical safety alerts, and this stays on after a restart until you turn it off.
        <div style="margin-top:8px"><button class="mode-chip" data-unmute="all">Unshush</button> <span class="toggle-desc">clears every mute below as well</span></div>
      </div>` : "";
    this._setHtml(body, banner
      + entities.map(e => row("Entity", "entity", e)).join("")
      + categories.map(c => row("Category", "category", c)).join("")
      + `<div class="toggle-desc" style="margin-top:8px">Critical safety alerts always speak.</div>`);
    body.querySelectorAll("[data-unmute]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        const kind = btn.getAttribute("data-unmute");
        const value = btn.getAttribute("data-unmute-value") || "";
        const data = kind === "entity" ? { entity_id: value }
          : kind === "category" ? { category: value } : {};
        btn.disabled = true;
        try {
          await this._hass.callService("nova", "unshush", data);
        } catch (err) {
          console.error("Nova: unshush failed", err);
        }
        this._fetchLiveData();
      });
    });
  }

  // Shared by the dashboard's Quick Actions card and the Suggestions tab's
  // empty state — same "Analyze Now" behavior Classic exposes, wired to
  // whichever button/result-div ids the caller passes.
  _wireAnalyzeButton(btnId, resultId) {
    const root = this.shadowRoot;
    const btn = root.getElementById(btnId);
    if (!btn || btn._wired) return;
    btn._wired = true;
    btn.addEventListener("click", async () => {
      if (!this._hass) return;
      const out = root.getElementById(resultId);
      btn.disabled = true;
      const orig = btn.textContent;
      this._setText(btn, "Analyzing…");
      if (out) this._setText(out, "Running pattern analysis over your history…");
      try {
        const res = await this._hass.callWS({ type: "nova/run_analysis" });
        const bf = res.backfill || {};
        const bfNote = bf.imported ? "<br>" + this._tHtml(bf.imported === 1
          ? (bf.entities === 1 ? "Imported {events} past event from history for {entities} new entity." : "Imported {events} past event from history for {entities} new entities.")
          : (bf.entities === 1 ? "Imported {events} past events from history for {entities} new entity." : "Imported {events} past events from history for {entities} new entities."),
        { events: bf.imported, entities: bf.entities }) : "";
        if (out) {
          if (res.ran) {
            const nf = res.patterns_found ?? 0;
            const ns = res.new_suggestions ?? 0;
            const covered = res.already_automated ?? 0;
            let msg = `✓ Found ${nf} pattern${nf === 1 ? "" : "s"}, ${ns} new suggestion${ns === 1 ? "" : "s"}.`;
            if (covered > 0) {
              msg += ` ${covered} already handled by Home Assistant.`;
            }
            if (ns > 0) {
              msg += ` Check Suggestions.`;
            } else {
              msg += ` Nothing cleared the confidence bar this pass.`;
              const dg = res.diagnostic || {};
              const cand = (dg.candidates || [])[0];
              if (cand) {
                const hr = String(cand.hour).padStart(2, "0");
                const remaining = Math.max(0, (dg.min_days || 0) - (cand.days || 0));
                const progress = remaining > 0
                  ? `${remaining} more qualifying day${remaining === 1 ? "" : "s"} needed`
                  : "day coverage met; confidence or evidence is still below the threshold";
                msg += `<br>Closest routine: <b>${this._esc(cand.name || cand.entity_id)}</b> → ${this._esc(cand.state)} ~${hr}:00, seen ${cand.days}/${dg.total_days} days (${progress}).`;
              }
              const src = (dg.top_sources || [])[0];
              if (src) msg += `<br>Busiest source: ${this._esc(src.name || src.entity_id)} (${src.changes} changes).`;
            }
            const nm = Array.isArray(res.near_misses) ? res.near_misses : [];
            if (nm.length) {
              msg += `<br>Building toward suggestions:`;
              msg += nm.slice(0, 5).map(m => {
                const prog = m.needed ? ` (${m.occurrences}/${m.needed})` : ` (${m.occurrences}×)`;
                return `<br>• ${this._esc(m.description || m.type)}${prog}`;
              }).join("");
            }
            this._setHtml(out, msg + bfNote);
          } else {
            this._setHtml(out, `✕ ${this._esc(res.reason || res.error || "Analysis did not run.")}` + bfNote);
          }
        }
        try { await this._fetchLiveData(); } catch (_) {}
      } catch (err) {
        if (out) this._setHtml(out, `✕ ${this._esc(err?.message || String(err))}`);
      } finally {
        btn.disabled = false;
        this._setText(btn, orig);
      }
    });
  }

  async _toggleAreaLights(areaId, roomName, isOn) {
    if (!this._hass || !areaId) return;
    const turnOn = !isOn;
    try {
      await this._hass.callService("light", turnOn ? "turn_on" : "turn_off", {}, { area_id: areaId });
      setTimeout(() => { try { this._fetchLiveData(); } catch (_) {} }, 500);
    } catch (err) {
      console.error(`Nova: ${roomName} lights toggle failed`, err);
    }
  }

  _wire() {
    const root = this.shadowRoot;
    const presentCard = root.getElementById("presentCard");
    if (presentCard) {
      presentCard.addEventListener("click", () => this._presentEnd());
      presentCard.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " " || e.key === "Escape") this._presentEnd(); });
    }
    const camToggle = root.getElementById("camToggle");
    if (camToggle) {
      camToggle.addEventListener("click", () => {
        this._camOpen = !this._camOpen;
        this._renderData();
        if (this._camOpen) {
          const refresh = () => (this._data()?.cameras || []).forEach(c =>
            this._refreshCameraSnapshot(c.entity_id));
          refresh();
          if (!this._cameraInterval) this._cameraInterval = setInterval(refresh, 15000);
        } else if (this._cameraInterval) {
          clearInterval(this._cameraInterval);
          this._cameraInterval = null;
        }
      });
    }
    const lockdownBtn = root.getElementById("lockdownControl");
    if (lockdownBtn) lockdownBtn.addEventListener("click", async () => {
      const active = !!this._data()?.lockdown?.active;
      if (!window.confirm(this._tx(active ? "Lift Nova lockdown?" : "Engage Nova lockdown?"))) return;
      lockdownBtn.disabled = true;
      try {
        const res = await this._hass.callWS({ type: "nova/set_lockdown", on: !active });
        if (this._liveData && res?.lockdown) {
          this._liveData.lockdown = res.lockdown;
          if (this._liveData.config) this._liveData.config.lockdown = res.lockdown;
        }
      } catch (err) { console.error("Nova: lockdown change failed", err); }
      lockdownBtn.disabled = false;
      this._renderData();
    });
    // Top nav: Command Center / Settings
    root.querySelectorAll(".nav-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        if (tab === this._currentTab) return;
        if (this._cameraInterval) { clearInterval(this._cameraInterval); this._cameraInterval = null; }
        this._stopEnergyFlowPoll();
        this._flowPrevStates = null;
        this._todayAt = 0;
        this._outlookAt = 0;
        this._camOpen = false;
        this._currentTab = tab;
        this._render();
      });
    });

    if (this._currentTab === "settings") this._wireSettings();
    if (this._currentTab === "logs") {
      this._wireLogs();
      const logView = this._logView || "system";
      if (logView === "decisions") this._fetchDecisions();
      else if (logView === "spoken_history") this._fetchSpokenHistory();
      else if (logView === "actions") this._fetchActions();
      else if (logView === "shadow") this._fetchShadow();
      else {
        // The shell always starts each render on "Loading…" (torn down and
        // rebuilt fresh on every tab/view switch), but the entries fetched
        // last time are still sitting in _debugLogEntries — render them
        // immediately so re-entering System Log shows the cached rows
        // instantly instead of a blocking spinner, then refresh in the
        // background exactly as a first visit would.
        if (this._debugLogEntries) this._renderDebugLogEntries(this._debugLogEntries);
        this._fetchDebugLog();
      }
    }
    if (this._currentTab === "diagnostics") this._wireDiagnostics();
    if (this._currentTab === "memory") { this._wireMemory(); this._fetchKnowledge(); this._fetchPersonRoutines(); }
    if (this._currentTab === "intrusion") this._wireIntrusion();
    if (this._currentTab === "faces") this._wireFaces();
    if (this._currentTab === "chat") this._wireChat();
    if (this._currentTab === "energy") this._wireEnergy();
    if (this._currentTab === "suggestions") {
      this._wireSuggestions();
      this._wireAnalyzeButton("sugRunAnalysis", "sugAnalysisResult");
      if (this._automationInventory === undefined) this._fetchAutomationInventory();
      if (this._automationTrials === undefined) this._fetchAutomationTrials();
    }
    if (this._currentTab === "dashboard") {
      this._wireAnalyzeButton("qaRunAnalysis", "qaAnalysisResult");
      const createGoal = root.getElementById("goalCreate");
      const goalOutcome = root.getElementById("goalOutcome");
      if (createGoal && goalOutcome) createGoal.addEventListener("click", async () => {
        const outcome = goalOutcome.value.trim();
        if (!outcome) { goalOutcome.focus(); return; }
        createGoal.disabled = true;
        await this._goalAction({ action: "create", outcome });
        goalOutcome.value = "";
        createGoal.disabled = false;
      });
      root.querySelectorAll(".panel [data-svc]").forEach(btn => {
        if (btn._wired) return;
        btn._wired = true;
        btn.addEventListener("click", async () => {
          const svcAttr = btn.getAttribute("data-svc");
          if (!svcAttr || !this._hass) return;
          const [domain, service] = svcAttr.split(".");
          let data = {};
          const dataAttr = btn.getAttribute("data-svc-data");
          if (dataAttr) {
            try { data = JSON.parse(dataAttr); } catch (_) { data = {}; }
          }
          try {
            await this._hass.callService(domain, service, data);
          } catch (err) {
            console.error(`Nova: service ${svcAttr} failed`, err);
          }
        });
      });
    }
  }

  _wireOnboarding() {
    const root = this.shadowRoot;
    root.getElementById("onboardingDismiss")?.addEventListener("click", async () => {
      for (const ob of [this._liveData?.onboarding, this._liveData?.config?.onboarding]) {
        if (ob) { ob.show = false; ob.dismissed = true; }
      }
      this._renderData();
      try { await this._hass.callWS({ type: "nova/update_config", key: "onboarding_dismissed", value: true }); } catch (_) {}
    });
    root.getElementById("onboardingHello")?.addEventListener("click", async () => {
      if (!this._hass || this._helloState?.busy) return;
      this._helloState = { busy: true };
      this._renderData();
      try {
        const res = await this._hass.callWS({ type: "nova/say_hello" });
        this._helloState = res?.ok ? { reply: res.reply } : { error: res?.error || "Nova didn't reply." };
      } catch (err) {
        this._helloState = { error: err?.code === "unauthorized"
          ? "Say hello needs a Home Assistant admin account."
          : "Couldn't reach Nova — restart Home Assistant after updating." };
      }
      this._renderData();
    });
    root.querySelector(".onboarding-settings")?.addEventListener("click", () => {
      this._currentTab = "settings";
      this._render();
    });
    root.querySelectorAll(".onboarding-jump").forEach(btn => btn.addEventListener("click", () =>
      this._openSettingsCard(btn.getAttribute("data-settings-title"))));
  }

  _openSettingsCard(title) {
    this._currentTab = "settings";
    this._render();
    const cards = Array.from(this.shadowRoot.querySelectorAll(".settings-card"));
    const needle = String(title || "").toLowerCase();
    const card = cards.find(c => (c.getAttribute("data-search") || "").includes(needle));
    if (!card) return;
    this._settingsSection = card.getAttribute("data-settings-group") || "general";
    this._applySettingsFilter();
    this.shadowRoot.querySelectorAll(".settings-nav-btn").forEach(b =>
      b.classList.toggle("active", b.getAttribute("data-settings-section") === this._settingsSection));
    if (typeof card.scrollIntoView === "function") card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  _wireSettings() {
    const root = this.shadowRoot;
    this._wireHomeLayout();
    this._wireVoicePhrases();
    this._wireVoiceId();

    root.querySelectorAll(".settings-nav-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._settingsSection = btn.getAttribute("data-settings-section");
        this._settingsSearch = "";
        this._applySettingsFilter();
        root.querySelectorAll(".settings-nav-btn").forEach(b =>
          b.classList.toggle("active", b === btn));
        const search = root.getElementById("settingsSearch");
        if (search) search.value = "";
      });
    });

    const search = root.getElementById("settingsSearch");
    if (search) {
      search.addEventListener("input", (e) => {
        this._settingsSearch = e.target.value;
        this._applySettingsFilter();
      });
    }

    // Generic settings autosave — same shape as Classic's own .cfg-field
    // handler: any toggle/select tagged data-cfg-key writes straight
    // through nova/update_config, then a live re-fetch refreshes state.
    root.querySelectorAll(".toggle-btn[data-cfg-key], .mode-chip[data-cfg-key]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await this._saveSetting(btn.getAttribute("data-cfg-key"), btn.getAttribute("data-cfg-val") === "true");
      });
    });
    root.querySelectorAll("select.cfg-field[data-cfg-key]").forEach(sel => {
      sel.addEventListener("change", async () => {
        const key = sel.getAttribute("data-cfg-key");
        await this._saveSetting(key, sel.value);
        if (key === "ui_language") {
          this._uiLangLoaded = null;
          await this._loadUiStrings(true);
        }
      });
    });
    root.querySelectorAll("input.cfg-field[data-cfg-key]:not(.hazard-location-field)").forEach(inp => {
      inp.addEventListener("change", async () => {
        const key = inp.getAttribute("data-cfg-key");
        let value = inp.value;
        if (inp.type === "number") value = (value === "" ? null : Number(value));
        await this._saveSetting(key, value);
      });
    });

    root.querySelectorAll(".person-honorific-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const customInput = sel.closest(".person-honorific-row")?.querySelector(".person-honorific-custom");
        if (sel.value === "__custom__") {
          if (customInput) { customInput.hidden = false; customInput.focus(); }
          return;  // wait for an actual value before saving anything
        }
        if (customInput) { customInput.hidden = true; customInput.value = ""; }
        const personId = sel.getAttribute("data-person-id");
        const cfg = this._data()?.config || {};
        const overrides = { ...(cfg.person_honorifics || {}) };
        if (sel.value) overrides[personId] = sel.value; else delete overrides[personId];
        await this._saveSetting("person_honorifics", JSON.stringify(overrides));
      });
    });
    root.querySelectorAll(".person-honorific-custom").forEach(inp => {
      inp.addEventListener("change", async () => {
        const personId = inp.getAttribute("data-person-id");
        const cfg = this._data()?.config || {};
        const overrides = { ...(cfg.person_honorifics || {}) };
        const val = inp.value.trim();
        if (val) overrides[personId] = val; else delete overrides[personId];
        await this._saveSetting("person_honorifics", JSON.stringify(overrides));
      });
    });
    this._wireFloorPlanEditor();

    root.querySelectorAll(".new-room-speaker-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const assigned = { ...(cfg.room_speakers || {}) };
        const areaId = sel.getAttribute("data-area-id");
        if (sel.value) assigned[areaId] = sel.value; else delete assigned[areaId];
        await this._saveSetting("room_speakers", JSON.stringify(assigned));
      });
    });
    root.querySelectorAll(".host-health-map-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const mappings = { ...(cfg.host_health_mappings || {}) };
        const metricKey = sel.getAttribute("data-metric-key");
        if (sel.value) mappings[metricKey] = sel.value; else delete mappings[metricKey];
        await this._saveSetting("host_health_mappings", JSON.stringify(mappings));
      });
    });
    root.querySelectorAll(".new-sat-pair-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const pairings = { ...(cfg.satellite_pairings || {}) };
        const satId = sel.getAttribute("data-sat-id");
        if (sel.value) pairings[satId] = sel.value; else delete pairings[satId];
        await this._saveSetting("satellite_pairings", JSON.stringify(pairings));
      });
    });
    root.querySelectorAll(".new-rule-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const ruleId = btn.getAttribute("data-rule-id");
        if (!ruleId) return;
        const current = this._data()?.config?.disabled_sentinel_rules || [];
        const isDisabled = current.includes(ruleId);
        const updated = isDisabled ? current.filter(id => id !== ruleId) : [...current, ruleId];
        await this._saveSetting("disabled_sentinel_rules", JSON.stringify(updated));
      });
    });
    root.querySelectorAll(".new-ann-speaker-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const spkId = btn.getAttribute("data-speaker-id");
        if (!spkId) return;
        const current = this._data()?.config?.announcement_speakers || [];
        const isOn = current.includes(spkId);
        const updated = isOn ? current.filter(id => id !== spkId) : [...current, spkId];
        await this._saveSetting("announcement_speakers", JSON.stringify(updated));
      });
    });
    root.querySelectorAll(".new-notify-service-toggle").forEach(btn => {
      btn.addEventListener("click", async () => {
        const service = btn.getAttribute("data-notify-service");
        if (!service) return;
        const cfg = this._data()?.config || {};
        const current = Array.isArray(this._notifyServicesDraft)
          ? this._notifyServicesDraft
          : (Array.isArray(cfg.notify_services)
            ? cfg.notify_services
            : (cfg.notify_service ? [cfg.notify_service] : []));
        const isOn = current.includes(service);
        const updated = isOn
          ? current.filter(item => item !== service)
          : [...current, service];
        this._notifyServicesDraft = updated;
        this._notifySavePending = (this._notifySavePending || 0) + 1;
        const previous = this._notifySaveQueue || Promise.resolve();
        this._notifySaveQueue = previous.then(() =>
          this._saveSetting("notify_services", JSON.stringify(updated)));
        try {
          await this._notifySaveQueue;
        } finally {
          this._notifySavePending -= 1;
        }
      });
    });
    const generalSpeakerSel = root.querySelector(".new-general-speaker-select");
    if (generalSpeakerSel) {
      generalSpeakerSel.addEventListener("change", async () => {
        await this._saveSetting("general_speaker", generalSpeakerSel.value);
      });
    }

    this._wireAiModels();
    this._wireCameraSettings();

    const dbtScan = root.getElementById("newDbtScan");
    if (dbtScan) {
      dbtScan.addEventListener("click", async () => {
        if (!this._hass) return;
        const limInput = root.getElementById("newDbtLimit");
        let limit = limInput ? parseInt(limInput.value, 10) : 40;
        if (isNaN(limit) || limit < 1) limit = 40;
        try {
          await this._hass.callService("nova", "train_doorbell_backlog", { limit });
          setTimeout(() => this._fetchLiveData(), 4000);
        } catch (err) {
          console.error("Nova: doorbell backlog scan failed", err);
        }
      });
    }

    const sceneClearBtn = root.getElementById("sceneMemoryClear");
    if (sceneClearBtn) {
      sceneClearBtn.addEventListener("click", async () => {
        if (!window.confirm(this._tx("Forget everything scene memory has kept?"))) return;
        try {
          await this._hass.callWS({ type: "nova/clear_scene_memory" });
        } catch (err) {
          console.error("Nova: clearing scene memory failed", err);
        }
      });
    }

    const plAddBtn = root.getElementById("newPlAddEntity");
    if (plAddBtn) {
      plAddBtn.addEventListener("click", async () => {
        const inp = root.getElementById("newPlEntityInput");
        const eid = inp && inp.value.trim();
        if (!eid) return;
        if (this._hass && this._hass.states && this._hass.states[eid]) {
          const arr = this._plList();
          if (!arr.includes(eid)) {
            arr.push(eid);
            // Mirror Classic's own _plSave: write the array onto _liveData
            // directly, not just to the backend — otherwise the re-render
            // right after this still shows the pre-save list, since the
            // live-data refetch has no way to know the write landed.
            if (this._liveData && this._liveData.config) this._liveData.config.pattern_include_entities = arr;
            await this._saveSetting("pattern_include_entities", JSON.stringify(arr));
          }
        } else {
          console.warn(`Nova: "${eid}" is not a known entity id`);
        }
      });
    }
    root.querySelectorAll(".new-pl-del").forEach(b => {
      b.addEventListener("click", async () => {
        const arr = this._plList();
        arr.splice(parseInt(b.getAttribute("data-i"), 10), 1);
        if (this._liveData && this._liveData.config) this._liveData.config.pattern_include_entities = arr;
        await this._saveSetting("pattern_include_entities", JSON.stringify(arr));
      });
    });

    const exclAdd = (addId, inpId, key, validate) => {
      const btn = root.getElementById(addId);
      if (!btn) return;
      btn.addEventListener("click", async () => {
        const inp = root.getElementById(inpId);
        const val = inp && inp.value.trim();
        if (!val) return;
        if (validate && !validate(val)) return;
        const arr = this._exclArr((this._data()?.config || {})[key]);
        if (!arr.includes(val)) { arr.push(val); await this._exclSave(key, arr); }
      });
    };
    exclAdd("newExclEntAdd", "newExclEntInput", "excluded_entities", (v) => {
      if (this._hass && this._hass.states && this._hass.states[v]) return true;
      console.warn(`Nova: "${v}" is not a known entity id`);
      return false;
    });
    exclAdd("newAuditSensorAdd", "newAuditSensorInput", "infrastructure_audit_sensors", (v) => {
      if (this._hass && this._hass.states && this._hass.states[v] && /^(binary_)?sensor\./.test(v)) return true;
      console.warn(`Nova: "${v}" is not a known sensor`);
      return false;
    });
    exclAdd("newExemptLockAdd", "newExemptLockInput", "lockdown_exempt_locks", (v) => {
      if (this._hass && this._hass.states && this._hass.states[v] && v.startsWith("lock.")) return true;
      console.warn(`Nova: "${v}" is not a known lock`);
      return false;
    });
    exclAdd("newExclDomAdd", "newExclDomInput", "excluded_domains", null);
    exclAdd("newExclLabAdd", "newExclLabInput", "excluded_labels", null);
    exclAdd("newDepCalAdd", "newDepCalInput", "departure_excluded_calendars", (v) => {
      if (this._hass && this._hass.states && this._hass.states[v] && v.startsWith("calendar.")) return true;
      console.warn(`Nova: "${v}" is not a known calendar`);
      return false;
    });
    const exclDel = (cls, key) => root.querySelectorAll("." + cls).forEach(b => {
      b.addEventListener("click", async () => {
        const arr = this._exclArr((this._data()?.config || {})[key]);
        arr.splice(parseInt(b.getAttribute("data-i"), 10), 1);
        await this._exclSave(key, arr);
      });
    });
    exclDel("new-excl-ent-del", "excluded_entities");
    exclDel("new-excl-dom-del", "excluded_domains");
    exclDel("new-excl-lab-del", "excluded_labels");
    exclDel("new-dep-cal-del", "departure_excluded_calendars");
    exclDel("new-audit-sensor-del", "infrastructure_audit_sensors");
    exclDel("new-exempt-lock-del", "lockdown_exempt_locks");

    const rateLimitInput = root.getElementById("newObserverRateLimit");
    if (rateLimitInput) {
      rateLimitInput.addEventListener("change", async () => {
        let v = parseInt(rateLimitInput.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        rateLimitInput.value = v;
        await this._rawSaveConfig("classifier_rate_limit", v);
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    }

    const vcTest = root.getElementById("newVcTest");
    if (vcTest) {
      vcTest.addEventListener("click", async () => {
        if (!this._hass) return;
        const out = root.getElementById("newVcTestResult");
        vcTest.disabled = true;
        const orig = vcTest.textContent;
        this._setText(vcTest, "▶ PLAYING…");
        if (out) this._setText(out, "Firing announce to your satellite — listen for it…");
        try {
          const res = await this._hass.callWS({ type: "nova/voice_confirm_test" });
          if (out) this._setHtml(out, res.ok
            ? `<span class="diag-ok">✓</span> ${this._esc(res.note || "Announce fired.")} (${this._esc(res.satellite || "")})`
            : `<span class="diag-down">✕</span> ${this._esc(res.note || res.error || "Test failed.")}`);
        } catch (err) {
          if (out) this._setHtml(out, `<span class="diag-down">✕</span> ${this._esc(err?.message || String(err))}`);
        } finally {
          vcTest.disabled = false;
          this._setText(vcTest, orig);
        }
      });
    }

    const briefNow = root.getElementById("newBriefNow");
    if (briefNow) {
      briefNow.addEventListener("click", async () => {
        if (!this._hass) return;
        const cfg = this._data()?.config || {};
        const spk = cfg.announcement_speakers;
        const hasTargets = (Array.isArray(spk) && spk.length > 0) || !!cfg.broadcast_group;
        if (!hasTargets) {
          console.warn("Nova: no announcement speakers set — choose them in Settings → Announcement Speakers");
          return;
        }
        briefNow.disabled = true;
        const orig = briefNow.textContent;
        this._setText(briefNow, "▶ BRIEFING…");
        try {
          await this._hass.callService("nova", "briefing", { announce: true });
        } catch (err) {
          console.error("Nova: briefing failed", err);
        } finally {
          briefNow.disabled = false;
          this._setText(briefNow, orig);
        }
      });
    }

    if (!this._hazFetchedOnce) {
      this._hazFetchedOnce = true;
      this._fetchHazardStatus();
    }
    if (!this._bioFetchedOnce) {
      this._bioFetchedOnce = true;
      this._fetchBio();
    }
    if (!this._docLibFetchedOnce) {
      this._docLibFetchedOnce = true;
      this._fetchDocLibrary();
      this._fetchVectorBackend();
    }
    this._wireDocLibrary();
    const bioToggle = root.getElementById("newBioToggle");
    if (bioToggle) {
      bioToggle.addEventListener("click", async () => {
        if (!this._hass) return;
        const enabling = !(this._bio && this._bio.enabled);
        bioToggle.disabled = true;
        try {
          await this._hass.callWS({ type: "nova/biometrics", action: enabling ? "enable" : "disable" });
        } catch (err) {
          console.error("Nova: wellbeing toggle failed", err);
        }
        await this._fetchBio();
      });
    }
    const hazScan = root.getElementById("newHazScan");
    if (hazScan) {
      hazScan.addEventListener("click", async () => {
        if (!this._hass) return;
        const body = root.getElementById("newHazBody");
        hazScan.disabled = true;
        const orig = hazScan.textContent;
        this._setText(hazScan, "⟳ SCANNING…");
        if (body) this._setHtml(body, `<div class="stub-body">Checking the hazard sources that are on…</div>`);
        try {
          const res = await this._hass.callWS({ type: "nova/hazard", action: "scan" });
          if (body) this._setHtml(body, this._renderHazardScan(res));
        } catch (err) {
          if (body) this._setHtml(body, `<div class="stub-body">${this._tHtml("Scan failed: {error}", { error: this._esc(err?.message || String(err)) })}</div>`);
        } finally {
          hazScan.disabled = false;
          this._setText(hazScan, orig);
        }
      });
    }
    // Hazard Monitor (8.8.2): CAP area fields keep their one-entry-per-line
    // contract. Location fields are a pair: the backend saves both, and an
    // empty field deletes the whole override before the card re-renders home.
    root.querySelectorAll("textarea.hazard-list-field[data-list-key]").forEach(area => {
      area.addEventListener("change", async () => {
        const items = area.value.split("\n").map(v => v.trim()).filter(Boolean);
        await this._saveSetting(area.getAttribute("data-list-key"), JSON.stringify(items));
      });
    });
    root.querySelectorAll(".hazard-location-field[data-cfg-key]").forEach(inp => {
      inp.addEventListener("change", async () => {
        const raw = inp.value.trim();
        const value = raw === "" ? "" : Number(raw);
        await this._saveSetting(inp.getAttribute("data-cfg-key"), value);
        await this._fetchHazardStatus();
      });
    });
    root.querySelectorAll("#hazardSources [data-cfg-key], select.cfg-field[data-cfg-key^=\"hazard_\"]").forEach(el => {
      el.addEventListener(el.tagName === "SELECT" ? "change" : "click", () => {
        setTimeout(() => this._fetchHazardStatus(), 50);
      });
    });
    root.querySelectorAll(".settings-card [data-svc]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const svcAttr = btn.getAttribute("data-svc");
        if (!svcAttr || !this._hass) return;
        const [domain, service] = svcAttr.split(".");
        try {
          await this._hass.callService(domain, service, {});
        } catch (err) {
          console.error(`Nova: service ${svcAttr} failed`, err);
        }
      });
    });
    this._applySettingsFilter();
  }

  // Diagnostics tab: fetched once per element lifetime (see
  // _fetchDiagnosticsData for why this isn't on the live-data poll), then RUN
  // CHECK re-fetches on demand and the service-test buttons call the same HA
  // services the old Settings card did.
  _wireDiagnostics() {
    const root = this.shadowRoot;
    if (!this._diagFetchedOnce) {
      this._diagFetchedOnce = true;
      this._fetchDiagnosticsData();
    }
    // Presence sensors: draw what we have, refresh at most every 10 seconds
    // (Diagnostics re-renders after each of its own fetches).
    if (this._mmwave) this._renderMmwaveNew();
    if (!this._mmwaveAt || Date.now() - this._mmwaveAt > 10000) {
      this._mmwaveAt = Date.now();
      this._fetchMmwaveNew();
    }
    root.getElementById("newDiagRefresh")?.addEventListener("click", () => this._fetchDiagnosticsData());
    root.querySelectorAll(".diag-panel [data-svc]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const svcAttr = btn.getAttribute("data-svc");
        if (!svcAttr || !this._hass) return;
        const [domain, service] = svcAttr.split(".");
        try {
          await this._hass.callService(domain, service, {});
        } catch (err) {
          console.error(`Nova: service ${svcAttr} failed`, err);
        }
      });
    });
    root.getElementById("newDiagCameraRun")?.addEventListener("click", async () => {
      const sel = root.getElementById("newDiagCameraSelect");
      const entity_id = sel ? sel.value : "";
      if (!entity_id || !this._hass) return;
      try {
        await this._hass.callService("nova", "analyze_camera", { entity_id, announce: true });
      } catch (err) {
        console.error("Nova: camera analyze failed", err);
      }
    });
  }

  // Operational Mode on the Command Center (8.0.0). Redrawn only when its own
  // content changed, so the 20 second poll never closes an open menu or the
  // Mode bindings section. Handlers are wired to this block alone: the
  // dashboard never runs the Settings wiring.
  _renderOperationalMode() {
    const body = this.shadowRoot.getElementById("operationalModeBody");
    if (!body) return;
    const html = this._operationalModeCardBody();
    if (body._html === html) return;
    body._html = html;
    this._setHtml(body, html);
    this._wireOperationalMode(body);
  }

  _wireOperationalMode(scope) {
    scope.querySelectorAll(".toggle-btn[data-cfg-key]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await this._saveSetting(btn.getAttribute("data-cfg-key"), btn.getAttribute("data-cfg-val") === "true");
      });
    });
    scope.querySelectorAll("select.cfg-field[data-cfg-key]").forEach(sel => {
      sel.addEventListener("change", async () => {
        await this._saveSetting(sel.getAttribute("data-cfg-key"), sel.value);
      });
    });
    scope.querySelectorAll("input.cfg-field[data-cfg-key]").forEach(inp => {
      inp.addEventListener("change", async () => {
        let value = inp.value;
        if (inp.type === "number") value = (value === "" ? null : Number(value));
        await this._saveSetting(inp.getAttribute("data-cfg-key"), value);
      });
    });
    // Mode chips call nova/mode directly (not update_config).
    scope.querySelectorAll(".mode-chip[data-mode]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const mode = btn.getAttribute("data-mode");
        if (!this._hass || !mode || btn.classList.contains("mode-chip-on")) return;
        try {
          await this._hass.callWS({ type: "nova/mode", action: "set", mode });
        } catch (err) {
          console.error("Nova: failed to set mode", err);
        }
        await this._fetchLiveData();
      });
    });
    scope.querySelectorAll("[data-lab-area]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-lab-area");
        let cur = this._data()?.config?.lab_areas;
        cur = Array.isArray(cur) ? cur.slice() : [];
        const i = cur.indexOf(id);
        if (i >= 0) cur.splice(i, 1); else cur.push(id);
        await this._saveSetting("lab_areas", JSON.stringify(cur));
      });
    });
    const det = scope.querySelector("details.mode-bindings");
    if (det) det.addEventListener("toggle", () => { this._modeBindingsOpen = det.open; });
  }

  async _saveSetting(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova: failed to save ${key}`, err);
    }
    await this._fetchLiveData();
    // Re-render on any tab except the dashboard — a full _render() there
    // would tear down and restart the stellar-core canvas animation for no
    // reason, since the dashboard never calls this helper anyway. Broadened
    // from "settings" only so Intrusion's own cfg-field/toggle-btn fields
    // (which reuse this same generic autosave) actually refresh.
    if (this._currentTab !== "dashboard") this._render();
  }

  _applySettingsFilter() {
    const root = this.shadowRoot;
    const q = (this._settingsSearch || "").trim().toLowerCase();
    root.getElementById("settingsGrid")?.setAttribute("data-section", q ? "" : this._settingsSection);
    root.querySelectorAll(".settings-card").forEach(card => {
      const matchesGroup = !q && card.getAttribute("data-settings-group") === this._settingsSection;
      const matchesSearch = q && (card.getAttribute("data-search") || "").includes(q);
      card.hidden = !(matchesGroup || matchesSearch);
    });
  }
  // ─── Hologram: the hero's WebGL bust ────────────────────────────────────
  // A head, neck and shoulders drawn as glowing contour lines of gold
  // points, with a soft bloom. It needs WebGL 2; without it _initCore()
  // falls back to the canvas 2D ember head in core-animation.js.
  //
  // States: idle, reasoning (thinking), asleep are driven by the panel
  // today. listening, speaking and presenting are ready for when Nova sends
  // the live voice state and data cards; until then nothing selects them.

  _holoTargets(state) {
    // glow, warm core, eyes open, lift (thinking sparks), scan, flow speed,
    // speech waveform, pose bias, breathing, bloom
    const T = {
      idle:       { glow: 0.85, warm: 0.45, open: 1,    lift: 0, scan: 0,   flow: 0.35, wave: 0, yaw: 0,   pitch: 0,     roll: 0,    breath: 1,   bloom: 1.0 },
      listening:  { glow: 1.0,  warm: 0.55, open: 1.12, lift: 0, scan: 0.7, flow: 0.55, wave: 0, yaw: 0,   pitch: -0.04, roll: 0.07, breath: 0.7, bloom: 1.15 },
      reasoning:  { glow: 1.1,  warm: 1.0,  open: 1,    lift: 1, scan: 1.5, flow: 1.4,  wave: 0, yaw: 0,   pitch: 0.16,  roll: 0,    breath: 0.5, bloom: 1.35 },
      speaking:   { glow: 1.0,  warm: 0.75, open: 1,    lift: 0, scan: 0,   flow: 0.7,  wave: 1, yaw: 0,   pitch: 0,     roll: 0,    breath: 0.6, bloom: 1.2 },
      presenting: { glow: 0.9,  warm: 0.5,  open: 1,    lift: 0, scan: 0,   flow: 0.45, wave: 0, yaw: 0.3, pitch: 0.04,  roll: 0,    breath: 1,   bloom: 1.0 },
      asleep:     { glow: 0.38, warm: 0.15, open: 0,    lift: 0, scan: 0,   flow: 0.08, wave: 0, yaw: 0,   pitch: 0.3,   roll: 0.05, breath: 1.6, bloom: 0.75 },
    };
    const key = T[state] ? state : "idle";
    return { key, target: T[key], rate: { asleep: 0.5, listening: 3, speaking: 3, presenting: 2.2, reasoning: 2.6 }[key] || 2.4 };
  }

  // The particles, eight floats each: type, three shape values, seed, size,
  // contour line, flag. Types: 0 head, 1 neck, 2 shoulders, 3 eyes, 4 lips,
  // 5 drifting dust, 6 speech waveform.
  _holoParticles(narrow) {
    const out = [], rnd = Math.random, k = narrow ? 0.55 : 1;
    const add = (type, a, b, c, size, line, flag) => out.push(type, a, b, c, rnd(), size, line, flag);
    for (let i = 0; i < 160; i++) add(5, rnd(), rnd(), 0.3 + rnd(), 0.5 + rnd() * 1.5, 0, 0);
    const waveN = Math.round(520 * k);
    for (let i = 0; i < waveN; i++) add(6, i / waveN, 0, 0, 1.1, 0, 0);
    for (const side of [-1, 1]) {
      for (let i = 0; i < 170; i++) {
        const r = Math.sqrt(rnd()), a = rnd() * 2 * Math.PI;
        add(3, Math.cos(a) * r, Math.sin(a) * r, side, 0.8 + rnd() * 0.5, 0, 0);
      }
    }
    for (let i = 0; i < 300; i++) add(4, rnd(), (rnd() + rnd() + rnd()) / 1.5 - 1, 0, 0.8 + rnd() * 0.4, 0, 0);
    const headLines = 64, headPer = Math.round(270 * k);
    for (let j = 0; j < headLines; j++) {
      const ph = -1.35 + 2.75 * j / (headLines - 1);
      for (let i = 0; i < headPer; i++) {
        const th = -Math.PI + 2 * Math.PI * (i + rnd() * 0.5) / headPer;
        add(0, th, ph, 0, 1, j, (ph > 0.55 && rnd() < 0.14) ? 1 : 0);
      }
    }
    for (let i = 0, n = Math.round(4500 * k); i < n; i++) {
      let th = (rnd() * 2 - 1) * Math.PI;
      if (rnd() < 0.6) th *= 0.45;
      add(0, th, Math.asin(rnd() * 2 - 1) * 0.985, 0, 0.7, -1, 0);
    }
    const neckPer = Math.round(120 * k);
    for (let j = 0; j < 16; j++) for (let i = 0; i < neckPer; i++) add(1, 2 * Math.PI * (i + rnd() * 0.5) / neckPer, j / 15, 0, 1, j, 0);
    const bodyLines = 46, bodyPer = Math.round(380 * k);
    for (let j = 0; j < bodyLines; j++) for (let i = 0; i < bodyPer; i++) add(2, 2 * Math.PI * (i + rnd() * 0.5) / bodyPer, j / (bodyLines - 1), 0, 1, j, 0);
    for (let i = 0, n = Math.round(3500 * k); i < n; i++) add(2, rnd() * 2 * Math.PI, rnd(), 0, 0.7, -1, 0);
    return { data: new Float32Array(out), count: out.length / 8 };
  }

  _holoShaders() {
    const points = `#version 300 es
    precision highp float;
    in vec4 aA; in vec4 aB;
    uniform vec2 uRes, uC; uniform float uS, uDpr, uT, uFlow, uGlow, uWarm, uOpen, uMouth, uLift, uWave, uVoice, uBreathY, uScanY, uScanK, uSizeK, uDensity;
    uniform vec3 uHead, uNeck, uBody, cA, cB, cC, cW;
    out vec4 vCol; out float vSoft;
    const vec3 L = normalize(vec3(-0.55, 0.55, 0.62));
    float bump(float v, float m, float s) { float d = v - m; return exp(-d * d / (2. * s * s)); }
    float lipLine(float x) { float q = x / 0.17; return -0.46 + 0.007 * min(1., q * q); }
    // how far the face stands out from a plain egg: nose, eye sockets, brows, cheeks, lips, chin, jaw
    float relief(float x, float y) {
      float ax = abs(x), lip = lipLine(x), mo = uMouth;
      return 0.27 * bump(x, 0., 0.055) * bump(y, -0.1, 0.14) + 0.07 * bump(x, 0., 0.08) * bump(y, -0.24, 0.055)
        - 0.14 * bump(ax, 0.29, 0.1) * bump(y, 0.1, 0.075) + 0.11 * bump(y, 0.25, 0.05) * bump(ax, 0.28, 0.2)
        + 0.05 * bump(ax, 0.38, 0.12) * bump(y, -0.12, 0.1) + 0.045 * bump(y, lip + 0.035, 0.03) * bump(x, 0., 0.14)
        + 0.05 * bump(y, lip - 0.045 - mo * 0.08, 0.035) * bump(x, 0., 0.13)
        - 0.1 * mo * bump(y, lip - mo * 0.04, 0.012 + mo * 0.03) * bump(x, 0., 0.11)
        + 0.07 * bump(y, -0.8, 0.11) * bump(x, 0., 0.24) + 0.04 * bump(ax, 0.5, 0.09) * bump(y, -0.55, 0.13);
    }
    vec3 headAt(float th, float ph) {
      float cp = cos(ph), dx = sin(th) * cp, dy = sin(ph), dz = cos(th) * cp;
      float x = dx * 0.76, y = dy, z = dz * 0.86;
      if (y < 0.) { float k = pow(-y, 1.6); x *= 1. - 0.22 * k; z *= 1. - 0.1 * k; }
      if (z < 0.) z *= 1.1;
      if (y > 0.) x *= 1. + 0.05 * y;
      if (dz > 0.) z += pow(dz, 1.2) * relief(x, y);
      return vec3(x, y, z);
    }
    float frontZ(float x, float y) {
      float k = y < 0. ? pow(-y, 1.6) : 0.; float x0 = x / (1. - 0.22 * k);
      float z0 = 0.86 * sqrt(max(0., 1. - (x0 / 0.76) * (x0 / 0.76) - y * y)) * (1. - 0.1 * k);
      return z0 + pow(max(z0, 1e-4) / 0.86, 1.2) * relief(x, y);
    }
    vec3 turn(vec3 p, vec3 pose) {
      float cy = cos(pose.x), sy = sin(pose.x), cp = cos(pose.y), sp = sin(pose.y), cr = cos(pose.z), sr = sin(pose.z);
      float x = p.x * cy + p.z * sy, z = -p.x * sy + p.z * cy;
      float y2 = p.y * cp - z * sp, z2 = p.y * sp + z * cp;
      return vec3(x * cr - y2 * sr, x * sr + y2 * cr, z2);
    }
    vec3 turnAbout(vec3 p, vec3 pose, vec3 pivot) { return turn(p - pivot, pose) + pivot; }
    void main() {
      int type = int(aA.x + 0.5); float seed = aB.x, size = aB.y, soft = 0., al = 0.; vec3 col = cA; vec2 px;
      float twinkle = 0.8 + 0.2 * sin(uT * 2.3 + seed * 6.283);
      if (type == 5) {
        px = vec2(fract(aA.y + uFlow * aA.w * 0.03) * uRes.x, aA.z * uRes.y + sin(uT * 0.5 + seed * 6.283) * 8.);
        al = (0.08 + 0.25 * uGlow) * (0.6 + 0.4 * sin(uT * 1.7 + seed * 6.283)) * 0.7; col = mix(cA, cC, 0.4); soft = 0.6; size *= 1.2;
      } else {
        vec3 r, rn = vec3(0., 0., 1.), p = vec3(0.); bool body = false;
        float warmK = 0., fade = 1., feature = 0., line = aB.z;
        if (type == 0) {
          float th = aA.y, ph = aA.z, e = 0.004;
          p = headAt(th, ph);
          vec3 n = normalize(cross(headAt(th + e, ph) - p, headAt(th, ph + e) - p));
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.)); rn = turn(n, uHead); body = true;
          // light the features: where the surface bends away from the plain egg
          if (cos(th) > 0.) feature = clamp((1. - dot(n, normalize(vec3(p.x / 0.58, p.y, p.z / 0.74)))) * 9., 0., 1.);
          warmK = exp(-dot(p - vec3(0., -0.12, 0.72), p - vec3(0., -0.12, 0.72)) * 14.) * 0.6;
          if (aB.w > 0.5 && uLift > 0.01) {
            float k = fract(uT * (0.14 + 0.2 * fract(seed * 3.7)) + seed);
            r.y += k * 1.6 * uLift; r.x += sin(k * 5. + seed * 20.) * 0.18 * k * uLift; fade = mix(1., (1. - k) * 1.4, uLift);
          }
        } else if (type == 1) {
          float u = aA.y, v = aA.z;
          p = vec3(0.33 * cos(u), -0.62 - v * 0.55, -0.06 + 0.3 * sin(u));
          r = turnAbout(p, uNeck, vec3(0., -1., 0.)); rn = turn(normalize(vec3(cos(u), 0., sin(u))), uNeck); body = true;
          warmK = exp(-pow(p.y + 0.95, 2.) * 20.) * 0.35 * step(0., sin(u));
        } else if (type == 2) {
          float u = aA.y, v = aA.z, cu = cos(u), su = sin(u);
          float w = 0.32 + 1.0 * smoothstep(0.0, 0.6, v) * (1. - 0.05 * v), d = 0.27 + 0.22 * smoothstep(0.0, 0.5, v);
          float x = w * sign(cu) * pow(abs(cu), 0.8), z = -0.1 + d * sign(su) * pow(abs(su), 0.8);
          z += 0.05 * bump(abs(x), 0.35, 0.18) * bump(v, 0.55, 0.15) * step(0., su);
          float drop = 0.32 * pow(clamp(abs(x) / 1.3, 0., 1.), 1.6) * (1. - smoothstep(0.35, 0.85, v));
          p = vec3(x * (1. + uBreathY * 0.4), -1.08 - v * 1.25 - drop, z);
          float ny = 0.9 * (1. - smoothstep(0.05, 0.45, v)) * abs(x) / w;
          vec3 n = normalize(vec3(x / (w * w), ny, (z + 0.1) / (d * d)));
          r = turnAbout(p, uBody, vec3(0., -1.4, 0.)); rn = turn(n, uBody); body = true;
          fade = 1. - smoothstep(0.55, 1., v);
          warmK = exp(-dot(p - vec3(0., -1.55, 0.3), p - vec3(0., -1.55, 0.3)) * 6.) * 0.45;
        } else if (type == 3) {
          float side = aA.w, u = aA.y, v = aA.z, op = uOpen;
          float rx = 0.062, ry = rx * (0.08 + 0.92 * min(op, 1.) * 0.75) * (0.9 + 0.2 * max(0., op - 1.) * 5.);
          float x = side * 0.285 + u * rx, y = 0.1 + v * ry - (1. - min(op, 1.)) * (1. - u * u) * 0.2 * rx;
          p = vec3(x, y, frontZ(x, y) + 0.03);
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.));
          float rr = length(vec2(u, v));
          col = mix(cA, cC, clamp(1. - rr, 0., 1.));
          al = (0.35 + 0.5 * uGlow) * (1. - 0.5 * rr) * 0.5 * mix(0.05, 1., min(op, 1.));
        } else if (type == 4) {
          float x = -0.15 + 0.3 * aA.y, y = lipLine(x) - uMouth * 0.04 + aA.z * (0.005 + uMouth * 0.04);
          p = vec3(x, y, frontZ(x, y) + 0.02);
          r = turnAbout(p, uHead, vec3(0., -0.65, 0.));
          col = mix(cC, cA, 0.5);
          al = (0.45 + uMouth * 0.9) * 0.4 * uGlow * smoothstep(0., 0.15, aA.y) * smoothstep(1., 0.85, aA.y);
        } else {
          float s = aA.y, env = sin(s * 3.14159);
          float y = -2.12 + uWave * env * (0.13 * uVoice * sin(s * 34. + uT * 11.) + 0.05 * sin(s * 13. - uT * 6.));
          r = vec3(-1.75 + 3.5 * s, y, 0.55);
          col = mix(cA, cW, 0.35 + 0.4 * uVoice * env); al = uWave * 0.5 * env; size *= 1. + uVoice * env * 0.6;
        }
        if (body) {
          float facing = clamp((r.z + 0.9) / 1.8, 0., 1.), diff = max(0., dot(rn, L));
          float rim = pow(1. - max(0., rn.z), 2.5) * smoothstep(-0.3, 0.2, r.z);
          float bands = 0.65 + 0.35 * sin(p.y * 24. - uFlow * 3.2 + line * 0.15);
          float scan = exp(-pow(p.y - uScanY, 2.) * 35.) * uScanK;
          al = (0.07 + 0.62 * facing * diff * diff + 0.8 * rim + 0.9 * feature * facing) * bands * uGlow + scan * 0.55 * facing;
          if (line < -0.5) al *= 0.55;
          if (r.z < -0.12) al *= 0.28;
          al *= uDensity * twinkle * fade;
          col = mix(mix(cB, cA, 0.35 + 0.65 * clamp(diff * 0.6 + rim, 0., 1.)), cC, clamp(rim * 0.45 + scan * 0.6, 0., 1.));
          float wk = clamp(warmK * uWarm, 0., 0.95);
          col = mix(col, cW, wk); al += wk * 0.85 * uDensity * fade;
          float blur = clamp(0.05 - r.z, 0., 1.); soft = blur; size *= 1. + blur * 1.3; al *= 1. - 0.55 * blur;
        }
        float persp = 5. / (5. - r.z);
        px = uC + vec2(r.x, -(r.y + uBreathY)) * persp * uS;
        size *= persp;
      }
      vCol = vec4(col, al); vSoft = soft;
      gl_Position = vec4(px.x / uRes.x * 2. - 1., 1. - px.y / uRes.y * 2., 0., 1.);
      gl_PointSize = max(1., size * uSizeK * 2.2 * uDpr);
    }`;
    const dot = `#version 300 es
    precision mediump float; in vec4 vCol; in float vSoft; out vec4 o;
    void main() {
      vec2 d = gl_PointCoord * 2. - 1.; float r2 = dot(d, d); if (r2 > 1.) discard;
      float a = exp(-r2 * mix(5., 2.2, vSoft)) * vCol.a; o = vec4(vCol.rgb * a, a);
    }`;
    const quad = `#version 300 es
    in vec2 p; out vec2 uv; void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0., 1.); }`;
    const blur = `#version 300 es
    precision mediump float; in vec2 uv; out vec4 o; uniform sampler2D t; uniform vec2 dir;
    void main() {
      vec3 c = texture(t, uv).rgb * 0.227027;
      c += (texture(t, uv + dir).rgb + texture(t, uv - dir).rgb) * 0.1945946;
      c += (texture(t, uv + dir * 2.).rgb + texture(t, uv - dir * 2.).rgb) * 0.1216216;
      c += (texture(t, uv + dir * 3.).rgb + texture(t, uv - dir * 3.).rgb) * 0.054054;
      c += (texture(t, uv + dir * 4.).rgb + texture(t, uv - dir * 4.).rgb) * 0.016216;
      o = vec4(c, 1.);
    }`;
    // The canvas is transparent, so the hero's own background
    // shows through; the light is written premultiplied.
    const compose = `#version 300 es
    precision highp float; in vec2 uv; out vec4 o;
    uniform sampler2D scene, bloom; uniform vec2 uRes, uC, uF; uniform float uDpr, uR, uGlow, uWarm, uBloomK, uS; uniform vec3 cA, cW;
    void main() {
      vec2 css = vec2(gl_FragCoord.x, uRes.y * uDpr - gl_FragCoord.y) / uDpr;
      vec3 c = cA * 0.10 * uGlow * pow(max(0., 1. - distance(css, uC) / uR), 1.8);
      c += cW * 0.08 * uWarm * pow(max(0., 1. - distance(css, uF) / (uS * 0.6)), 2.);
      c = min(c + texture(scene, uv).rgb + texture(bloom, uv).rgb * uBloomK, vec3(1.));
      o = vec4(c, max(c.r, max(c.g, c.b)));
    }`;
    return { points, dot, quad, blur, compose };
  }

  _holoProgram(gl, vs, fs) {
    const shader = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, vs));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || "link");
    const u = {};
    for (let i = 0, n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS); i < n; i++) {
      const name = gl.getActiveUniform(prog, i).name;
      u[name] = gl.getUniformLocation(prog, name);
    }
    return { prog, u };
  }

  _holoTarget(gl, w, h) {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex, fb, w, h };
  }

  // Compile and link everything on the hologram's context. Throws when the
  // browser cannot.
  _holoSetupGL(h) {
    const gl = h.canvas.getContext("webgl2", { antialias: false, alpha: true, premultipliedAlpha: true });
    if (!gl) throw new Error("WebGL 2 is not available");
    const src = this._holoShaders();
    h.gl = gl;
    h.points = this._holoProgram(gl, src.points, src.dot);
    h.blur = this._holoProgram(gl, src.quad, src.blur);
    h.compose = this._holoProgram(gl, src.quad, src.compose);
    h.quad = gl.createVertexArray();
    gl.bindVertexArray(h.quad);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    for (const p of [h.blur.prog, h.compose.prog]) {
      const loc = gl.getAttribLocation(p, "p");
      gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }
    h.vao = null; h.built = null; h.fbo = {};
  }

  _holoUpload(h) {
    const gl = h.gl;
    h.built = this._holoParticles(h.narrow);
    if (h.vao) gl.deleteVertexArray(h.vao);
    h.vao = gl.createVertexArray();
    gl.bindVertexArray(h.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, h.built.data, gl.STATIC_DRAW);
    const la = gl.getAttribLocation(h.points.prog, "aA"), lb = gl.getAttribLocation(h.points.prog, "aB");
    gl.enableVertexAttribArray(la); gl.vertexAttribPointer(la, 4, gl.FLOAT, false, 32, 0);
    gl.enableVertexAttribArray(lb); gl.vertexAttribPointer(lb, 4, gl.FLOAT, false, 32, 16);
  }

  _holoResize() {
    const h = this._holo;
    if (!h || !h.gl || h.stopped) return;
    const rect = h.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const wasNarrow = h.narrow;
    h.W = rect.width; h.H = rect.height;
    h.dpr = Math.min(window.devicePixelRatio || 1, 2);
    h.narrow = h.W < 640;
    h.canvas.width = Math.round(h.W * h.dpr); h.canvas.height = Math.round(h.H * h.dpr);
    if (!h.built || wasNarrow !== h.narrow) this._holoUpload(h);
    const gl = h.gl;
    for (const k of Object.keys(h.fbo)) { gl.deleteTexture(h.fbo[k].tex); gl.deleteFramebuffer(h.fbo[k].fb); }
    h.fbo.scene = this._holoTarget(gl, h.canvas.width, h.canvas.height);
    const bw = Math.max(1, h.canvas.width >> 1), bh = Math.max(1, h.canvas.height >> 1);
    h.fbo.a = this._holoTarget(gl, bw, bh); h.fbo.b = this._holoTarget(gl, bw, bh);
    if (this._reduceMotion) this._holoFrame(performance.now());
  }

  _holoLayout(h) {
    if (h.narrow) { const S = h.H * 0.25; return { S, hx: h.W / 2, hy: h.H * 0.33 }; }
    const S = h.H * 0.27;
    return { S, hx: Math.max(h.W * 0.27, S * 1.9), hy: h.H * 0.3 };
  }

  // Try to start the hologram on this canvas. False when WebGL 2 is missing
  // or fails, so the caller can fall back to the 2D head.
  _initHologram(canvas) {
    this._holoStop();
    const h = {
      canvas, narrow: false, W: 0, H: 0, dpr: 1, stopped: false, visible: true,
      cur: Object.assign({}, this._holoTargets(this._coreStateKey).target),
      face: { t: 0, blink: 1, blinkT: -1, nextBlink: 2, gx: 0, gy: 0, gazeT: 0, gTarget: [0, 0],
        yaw: 0, pitch: 0, roll: 0, voice: 0, mouth: 0, lookText: 0, dragYaw: 0, dragging: false, dragX: 0 },
      pointer: { inside: false, x: 0, y: 0 },
      flow: 0, scanY: 1.2, clock: 0, last: 0,
    };
    try { this._holoSetupGL(h); } catch (_) {
      // A canvas that gave a WebGL context can never give a 2D one, so the
      // fallback head gets a fresh copy of it.
      if (h.gl) canvas.replaceWith(canvas.cloneNode(false));
      return false;
    }
    this._holo = h;
    const fine = typeof window.matchMedia === "function" && window.matchMedia("(pointer: fine)").matches;
    h.fine = fine;
    canvas.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect(), f = h.face;
      h.pointer.x = e.clientX - r.left; h.pointer.y = e.clientY - r.top; h.pointer.inside = true;
      if (f.dragging) {
        f.dragYaw = Math.max(-1.2, Math.min(1.2, f.dragYaw + (e.clientX - f.dragX) * 0.012));
        f.dragX = e.clientX;
      }
    });
    canvas.addEventListener("pointerleave", () => { h.pointer.inside = false; });
    canvas.addEventListener("pointerdown", (e) => {
      h.face.dragging = true; h.face.dragX = e.clientX;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* not all browsers */ }
    });
    const endDrag = () => { h.face.dragging = false; };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    // A wall tablet can drop the graphics context; rebuild it when it comes back.
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      if (h.stopped || this._holo !== h) return;
      if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    });
    canvas.addEventListener("webglcontextrestored", () => {
      if (h.stopped || this._holo !== h) return;
      try { this._holoSetupGL(h); } catch (_) { return; }
      this._holoResize();
      this._holoKick();
    });
    if (typeof ResizeObserver === "function") {
      h.resizeObs = new ResizeObserver(() => this._holoResize());
      h.resizeObs.observe(canvas);
    } else {
      h.onResize = () => this._holoResize();
      window.addEventListener("resize", h.onResize);
    }
    if (typeof IntersectionObserver === "function") {
      h.seenObs = new IntersectionObserver((entries) => {
        h.visible = entries[entries.length - 1].isIntersecting;
        this._holoKick();
      });
      h.seenObs.observe(canvas);
    }
    h.onVisibility = () => this._holoKick();
    document.addEventListener("visibilitychange", h.onVisibility);
    this._holoResize();
    if (this._reduceMotion) this._holoFrame(performance.now()); else this._holoKick();
    return true;
  }

  // Run the animation loop while the hologram is on screen and the tab is
  // visible; it stops by itself otherwise and this starts it again.
  _holoKick() {
    const h = this._holo;
    if (!h || h.stopped || this._reduceMotion || this._animHandle) return;
    if (!h.visible || document.hidden) return;
    h.last = 0;
    const loop = (now) => {
      this._animHandle = null;
      if (this._holo !== h || h.stopped) return;
      this._holoFrame(now);
      if (h.visible && !document.hidden) this._animHandle = requestAnimationFrame(loop);
    };
    this._animHandle = requestAnimationFrame(loop);
  }

  // Release everything the hologram holds: observers, listeners and the
  // graphics context (browsers allow only a few live ones).
  _holoStop() {
    const h = this._holo;
    if (!h) return;
    h.stopped = true;
    this._holo = null;
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (h.resizeObs) h.resizeObs.disconnect();
    if (h.seenObs) h.seenObs.disconnect();
    if (h.onVisibility) document.removeEventListener("visibilitychange", h.onVisibility);
    if (h.onResize) window.removeEventListener("resize", h.onResize);
    try { h.gl?.getExtension("WEBGL_lose_context")?.loseContext(); } catch (_) { /* already gone */ }
  }

  _holoSetState(state) {
    const h = this._holo;
    if (!h) return;
    if (this._reduceMotion) {
      Object.assign(h.cur, this._holoTargets(state).target);
      this._holoFrame(performance.now());
    }
  }

  _holoUpdateFace(h, dt, key) {
    const f = h.face, cur = h.cur, lay = this._holoLayout(h);
    f.t += dt;
    if (f.blinkT < 0 && f.t > f.nextBlink) f.blinkT = 0;
    if (f.blinkT >= 0) {
      f.blinkT += dt;
      const p = f.blinkT / 0.16;
      f.blink = p < 1 ? Math.abs(1 - 2 * p) : 1;
      if (p >= 1) { f.blinkT = -1; f.nextBlink = f.t + 2.8 + Math.random() * 3.5; }
    }
    f.lookText = Math.max(0, f.lookText - dt);
    let tx = 0, ty = 0;
    if (key === "asleep" || key === "presenting") { tx = 0; ty = 0; }
    else if (f.lookText > 0 && !h.narrow) { tx = 0.95; ty = 0.05; }               // glance at the new word
    else if (h.pointer.inside && h.fine && !f.dragging) {
      tx = Math.max(-1, Math.min(1, (h.pointer.x - lay.hx) / (h.W * 0.4)));
      ty = Math.max(-0.8, Math.min(0.8, (h.pointer.y - lay.hy) / (h.H * 0.6)));
    } else if (key === "reasoning") { tx = Math.sin(f.t * 0.5) * 0.35; }
    else if (key === "idle") {
      if (f.t > f.gazeT) { f.gTarget = [(Math.random() * 2 - 1) * 0.6, (Math.random() * 2 - 1) * 0.25]; f.gazeT = f.t + 2.2 + Math.random() * 3; }
      [tx, ty] = f.gTarget;
    }
    const k = Math.min(1, dt * 2.5);
    f.gx += (tx - f.gx) * k; f.gy += (ty - f.gy) * k;
    if (!f.dragging) f.dragYaw *= Math.pow(0.08, dt);
    f.yaw = cur.yaw + f.gx * 0.5 + Math.sin(f.t * 0.3) * 0.05 * (1 - cur.lift) + f.dragYaw;
    f.pitch = cur.pitch + f.gy * 0.25 + Math.sin(f.t * 0.7) * 0.01;
    f.roll = cur.roll + Math.sin(f.t * 0.23) * 0.015;
    // A made up voice envelope moves the mouth while speaking, until real
    // audio levels are wired in.
    const v = Math.max(0, Math.sin(f.t * 8.7) * Math.sin(f.t * 3.3 + 1) * 0.8 + 0.25 * Math.sin(f.t * 17) + 0.1);
    const speaking = this._holoTargets(key).target.wave;
    f.voice += (v * speaking - f.voice) * Math.min(1, dt * 14);
    f.mouth += ((speaking ? Math.min(0.9, v * 0.9) : 0) - f.mouth) * Math.min(1, dt * 16);
  }

  _holoFrame(now) {
    const h = this._holo;
    if (!h || h.stopped || !h.gl || !h.W || !h.fbo.scene) return;
    const gl = h.gl, reduce = this._reduceMotion, f = h.face, cur = h.cur;
    const dt = reduce ? 0 : Math.min(0.05, (now - (h.last || now)) / 1000);
    h.last = now; h.clock += dt;
    const { key, target, rate } = this._holoTargets(this._coreStateKey);
    if (h.key !== key) { if (h.key) f.lookText = 1.6; h.key = key; }
    const kk = reduce ? 1 : Math.min(1, dt * rate);
    for (const name of Object.keys(target)) cur[name] += (target[name] - cur[name]) * kk;
    if (reduce) { f.yaw = cur.yaw + f.dragYaw; f.pitch = cur.pitch; f.roll = cur.roll; f.blink = 1; }
    else this._holoUpdateFace(h, dt, key);
    h.flow += dt * cur.flow;
    h.scanY -= dt * cur.scan * 0.9; if (h.scanY < -2.4) h.scanY = 1.2;
    const breathY = reduce ? 0 : Math.sin(h.clock * 2 * Math.PI / (key === "asleep" ? 7 : 5)) * 0.018 * cur.breath;
    const lay = this._holoLayout(h), t = reduce ? 0 : h.clock;
    const gold = [0.957, 0.722, 0.376], deep = [0.62, 0.27, 0.12], pale = [1.0, 0.89, 0.68], ember = [0.886, 0.33, 0.18];

    gl.bindFramebuffer(gl.FRAMEBUFFER, h.fbo.scene.fb); gl.viewport(0, 0, h.fbo.scene.w, h.fbo.scene.h);
    gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    const P = h.points.u;
    gl.useProgram(h.points.prog); gl.bindVertexArray(h.vao);
    gl.uniform2f(P.uRes, h.W, h.H); gl.uniform2f(P.uC, lay.hx, lay.hy); gl.uniform1f(P.uS, lay.S); gl.uniform1f(P.uDpr, h.dpr);
    gl.uniform1f(P.uT, t); gl.uniform1f(P.uFlow, h.flow); gl.uniform1f(P.uGlow, cur.glow);
    gl.uniform1f(P.uWarm, cur.warm * (1 + f.voice * 0.6)); gl.uniform1f(P.uOpen, Math.max(0, cur.open * f.blink));
    gl.uniform1f(P.uMouth, f.mouth); gl.uniform1f(P.uLift, cur.lift); gl.uniform1f(P.uWave, cur.wave); gl.uniform1f(P.uVoice, f.voice);
    gl.uniform1f(P.uBreathY, breathY); gl.uniform1f(P.uScanY, h.scanY); gl.uniform1f(P.uScanK, Math.min(1, cur.scan));
    gl.uniform1f(P.uSizeK, Math.max(0.75, lay.S / 125)); gl.uniform1f(P.uDensity, h.narrow ? 0.6 : 0.42);
    gl.uniform3f(P.uHead, f.yaw, f.pitch, f.roll);
    gl.uniform3f(P.uNeck, f.yaw * 0.55, f.pitch * 0.4, f.roll * 0.5);
    gl.uniform3f(P.uBody, f.yaw * 0.25, 0, f.roll * 0.2);
    gl.uniform3f(P.cA, ...gold); gl.uniform3f(P.cB, ...deep); gl.uniform3f(P.cC, ...pale); gl.uniform3f(P.cW, ...ember);
    gl.drawArrays(gl.POINTS, 0, h.built.count);
    gl.disable(gl.BLEND);

    // bloom: two blurs of the scene at half size
    gl.useProgram(h.blur.prog); gl.bindVertexArray(h.quad); gl.activeTexture(gl.TEXTURE0); gl.uniform1i(h.blur.u.t, 0);
    const pass = (from, to, dx, dy) => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, to.fb); gl.viewport(0, 0, to.w, to.h);
      gl.bindTexture(gl.TEXTURE_2D, from.tex); gl.uniform2f(h.blur.u.dir, dx / to.w, dy / to.h);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };
    pass(h.fbo.scene, h.fbo.a, 1, 0); pass(h.fbo.a, h.fbo.b, 0, 1); pass(h.fbo.b, h.fbo.a, 2.5, 0); pass(h.fbo.a, h.fbo.b, 0, 2.5);

    const C = h.compose.u;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, h.canvas.width, h.canvas.height);
    gl.useProgram(h.compose.prog);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, h.fbo.scene.tex); gl.uniform1i(C.scene, 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, h.fbo.b.tex); gl.uniform1i(C.bloom, 1);
    gl.uniform2f(C.uRes, h.W, h.H); gl.uniform2f(C.uC, lay.hx, lay.hy + lay.S * 0.6);
    gl.uniform2f(C.uF, lay.hx + f.yaw * lay.S * 0.55, lay.hy + lay.S * 0.05); gl.uniform1f(C.uDpr, h.dpr);
    gl.uniform1f(C.uR, Math.max(h.W, h.H) * 0.6); gl.uniform1f(C.uS, lay.S); gl.uniform1f(C.uGlow, cur.glow); gl.uniform1f(C.uWarm, cur.warm);
    gl.uniform1f(C.uBloomK, cur.bloom * 1.5); gl.uniform3f(C.cA, ...gold); gl.uniform3f(C.cW, ...ember);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  // ─── Ember head: the hero's 3D face ─────────────────────────────────────
  // The fallback when the WebGL hologram (core-hologram.js) cannot start.
  // A head built from gold sparks, projected in plain canvas 2D (no library,
  // same idea as NOVA3D). It sits on the left of a full-width stage, turns
  // and looks around, glances at the state word when it changes, and its
  // eyes, brows and mouth follow the core state.

  _initCore() {
    let canvas = this.shadowRoot.getElementById("core");
    if (!canvas) return;
    this._reduceMotion = (typeof window.matchMedia === "function")
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (this._initHologram(canvas)) return;
    canvas = this.shadowRoot.getElementById("core");
    if (!canvas) return;
    this._canvas = canvas;
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
    // New particles have no surface points yet. Forget the cached expression
    // key so the next frame rebuilds them; otherwise coming back to the
    // Command Center after another tab left every particle without geometry
    // and each animation frame threw.
    this._headGeoKey = null;
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
    // The hologram knows more states (listening, speaking, presenting) than
    // the fallback head, which shows those as idle.
    const next = this._holo ? this._holoTargets(state).key : (STATES[state] ? state : "idle");
    if (this._coreStateKey !== next && this._face) this._face.lookText = 1.6;
    this._coreStateKey = next;
    this._targetState = STATES[next] || STATES.idle;
    if (this._holo) { this._holoSetState(next); return; }
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
  // ─── Presenting ───────────────────────────────────────────────────────
  // While the Command Center is open and the house is awake, the cards also
  // come round by themselves, one kind after another (the "Cards on the
  // Command Center" setting, on by default), so the hero is not only ever
  // one greeting. The greeting is a step in the same rotation: every few
  // cards the hero word changes to a fresh one (see _pickGreeting). Energy is
  // the first kind; its figures are read with the Energy tab's own commands.
  // When Nova answers an energy question it publishes a small card of the
  // real figures (nova/subscribe_presentation). The head turns towards the
  // card and the hero copy gives way to it; after the card expires, or when
  // it is tapped, the hero goes back to whatever _coreState() says. Asleep
  // wins: the head stays asleep, though the card can still be read.
  _presentStart() {
    const conn = this._hass?.connection;
    if (!conn || typeof conn.subscribeMessage !== "function") return;
    const token = this._presentToken = this._presentToken || 0;
    if (this._presentLive === token) return;   // already started for this round
    this._presentLive = token;
    Promise.resolve(conn.subscribeMessage((card) => this._onPresentCard(card), { type: "nova/subscribe_presentation" }))
      .then((unsub) => {
        if (this._presentToken === token) this._presentUnsub = unsub;
        else if (typeof unsub === "function") unsub();   // stopped, or drawn again, while connecting
      })
      .catch(() => { if (this._presentLive === token) this._presentLive = null; });   // the card is optional
    this._presentCycleStart();
  }

  _presentCycleStart() {
    if (this._presentCycleTimer) return;
    const every = this._presentCycleMs || 9000;
    this._presentCycleTimer = setInterval(() => this._presentAmbient(), every);
    // The first card comes soon after the page opens, not a full round later.
    this._presentFirstTimer = setTimeout(() => this._presentAmbient(), this._presentFirstMs || 15000);
  }

  // Every kind of card Nova can show is listed here: how to draw it (html)
  // and how to read the figures for the rotation (ambient, which gives the
  // card's data or null when there is nothing to show). New kinds join the
  // rotation by being added here.
  _presentKinds() {
    return {
      // `refresh` (optional) redraws a card on show every `every` ms.
      datetime: { html: (d) => this._presentClockHtml(d.now), ambient: async () => ({ now: Date.now() }),
        refresh: () => this._presentClockHtml(Date.now()), every: 1000 },
      weather: { html: (d) => this._presentWeatherHtml(d), ambient: async () => this._presentWeatherData() },
      climate: { html: (d) => this._presentClimateHtml(d), ambient: async () => this._presentClimateData() },
      home: { html: (d) => this._presentHomeHtml(d), ambient: async () => this._presentHomeData() },
      calendar: { html: (d) => this._presentCalendarHtml(d), ambient: async () => this._presentCalendarData() },
      reminders: { html: (d) => this._presentRemindersHtml(d), ambient: () => this._presentRemindersData() },
      energy: { html: (d) => this._presentEnergyHtml(d), ambient: () => this._presentEnergyData() },
      security: { html: (d) => this._presentSecurityHtml(d), ambient: async () => this._presentSecurityData() },
    };
  }

  // The time and date, in the panel's language. Ticks while it is on show.
  _presentClockHtml(ms) {
    const lang = this._resolveUiLang(), t = new Date(ms);
    let time, date;
    try {
      time = t.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
      date = t.toLocaleDateString(lang, { weekday: "long", day: "numeric", month: "long" });
    } catch (_) {
      time = t.toTimeString().slice(0, 5);
      date = t.toDateString();
    }
    return `<div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(time)}</span></div><div class="pc-cap">${this._esc(date)}</div>`;
  }

  // Who is home, from Home Assistant's person entities. Null with nobody set up.
  _presentHomeData() {
    const people = [];
    for (const [id, st] of Object.entries(this._hass?.states || {})) {
      if (!id.startsWith("person.") || !st || st.state === "unavailable" || st.state === "unknown") continue;
      people.push({ name: String(st.attributes?.friendly_name || id.slice(7)), home: st.state === "home" });
    }
    if (!people.length) return null;
    people.sort((a, b) => (b.home - a.home) || a.name.localeCompare(b.name));
    return { people: people.slice(0, 6), total: people.length, home: people.filter(p => p.home).length };
  }

  _presentHomeHtml(d) {
    if (!d.total) return "";
    const n = (v) => this._esc(this._presentNum(v, 0));
    return `<div class="pc-cap">HOME</div><div class="pc-big"><span class="pc-fig" data-unit="">${n(d.home)}/${n(d.total)}</span></div>`
      + `<div class="pc-rows">${(d.people || []).map(p => `<div class="pc-row"><span>${this._esc(p.name)}</span><b>${this._esc(p.home ? this._tx("HOME") : this._tx("AWAY"))}</b></div>`).join("")}</div>`;
  }

  // "today", "tomorrow" or a weekday and date, in the panel's language, from
  // the browser's own date names (so no words of ours to translate).
  _presentDayWord(date) {
    const lang = this._resolveUiLang();
    const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((midnight(date) - midnight(new Date())) / 86400000);
    let word;
    try {
      word = Math.abs(days) <= 1 ? new Intl.RelativeTimeFormat(lang, { numeric: "auto" }).format(days, "day")
        : date.toLocaleDateString(lang, { weekday: "long", day: "numeric", month: "long" });
    } catch (_) {
      word = date.toDateString();
    }
    return word.charAt(0).toLocaleUpperCase(lang) + word.slice(1);
  }

  _presentTime(date) {
    try {
      return date.toLocaleTimeString(this._resolveUiLang(), { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return date.toTimeString().slice(0, 5);
    }
  }

  // The next event across the Home Assistant calendars: each calendar entity
  // shows its current or next event in its attributes. Null when none starts
  // within a week.
  _presentCalendarData() {
    const week = Date.now() + 7 * 86400000;
    let best = null;
    for (const [id, st] of Object.entries(this._hass?.states || {})) {
      if (!id.startsWith("calendar.") || !st || st.state === "unavailable") continue;
      const a = st.attributes || {};
      if (!a.message || !a.start_time) continue;
      const start = new Date(String(a.start_time).replace(" ", "T")).getTime();
      const end = a.end_time ? new Date(String(a.end_time).replace(" ", "T")).getTime() : start;
      if (!isFinite(start) || start > week || (end <= Date.now() && start < Date.now())) continue;
      if (!best || start < best.start) best = { title: String(a.message), start, all_day: !!a.all_day };
    }
    return best;
  }

  _presentCalendarHtml(d) {
    if (!d || !d.title || !isFinite(d.start)) return "";
    const when = new Date(d.start), started = d.start <= Date.now();
    const big = started ? this._tx("Now") : d.all_day ? this._presentDayWord(when) : this._presentTime(when);
    const sub = started || d.all_day ? "" : `${this._presentDayWord(when)} · `;
    return `<div class="pc-cap">Calendar</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(big)}</span></div>`
      + `<div class="pc-now">${this._esc(sub + d.title)}</div>`;
  }

  // The reminders still to come, from Nova's own store.
  async _presentRemindersData() {
    const res = await this._hass.callWS({ type: "nova/get_reminders" });
    const items = Array.isArray(res?.items) ? res.items.filter(r => r && r.label && r.at) : [];
    return items.length ? { items } : null;
  }

  _presentRemindersHtml(d) {
    const items = (d && Array.isArray(d.items) ? d.items : []).slice(0, 4);
    if (!items.length) return "";
    const first = items[0], at = new Date(first.at);
    const big = first.due || !isFinite(at.getTime()) ? this._tx("Now") : this._presentTime(at);
    return `<div class="pc-cap">Reminder</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(big)}</span></div>`
      + `<div class="pc-rows">${items.map(r => {
        const t = new Date(r.at);
        const when = r.due || !isFinite(t.getTime()) ? this._tx("Now") : this._presentTime(t);
        return `<div class="pc-row"><span>${this._esc(r.label)}</span><b>${this._esc(when)}</b></div>`;
      }).join("")}</div>`;
  }

  // Indoor temperature and humidity: the room cards' own readings, averaged,
  // with up to four rooms listed. Null when no room reports either.
  _presentClimateData() {
    const rooms = [];
    for (const a of this._liveData?.areas || []) {
      const t = parseFloat(a.temp), h = parseFloat(a.humidity);
      if (!isFinite(t) && !isFinite(h)) continue;   // "—" when a room has neither
      rooms.push({ name: String(a.name || ""), temp: isFinite(t) ? t : null, humidity: isFinite(h) ? h : null,
        unit: isFinite(t) ? String(a.temp).replace(/^[-\d.]+/, "") : "" });
    }
    if (!rooms.length) return null;
    const avg = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);
    const temps = rooms.filter(r => r.temp !== null), hums = rooms.filter(r => r.humidity !== null);
    return { temp: avg(temps.map(r => r.temp)), unit: temps.length ? temps[0].unit : "",
      humidity: avg(hums.map(r => r.humidity)), rooms: rooms.slice(0, 4) };
  }

  _presentClimateHtml(d) {
    if (!d || (d.temp == null && d.humidity == null)) return "";
    const n = (v, dp) => this._esc(this._presentNum(v, dp));
    const tenths = d.temp != null ? Math.round(d.temp * 10) / 10 : 0;   // 21 not 21.0, but 20.8 stays
    const big = d.temp != null ? `${n(tenths, Number.isInteger(tenths) ? 0 : 1)}${this._esc(d.unit || "°")}` : `${n(d.humidity, 0)}%`;
    const sub = d.temp != null && d.humidity != null ? `<div class="pc-now">${n(d.humidity, 0)}%</div>` : "";
    const rows = (d.rooms || []).map(r => {
      const parts = [r.temp != null ? `${n(r.temp, 0)}${this._esc(r.unit || "°")}` : "", r.humidity != null ? `${n(r.humidity, 0)}%` : ""].filter(Boolean);
      return `<div class="pc-row"><span>${this._esc(r.name)}</span><b>${parts.join(" · ")}</b></div>`;
    }).join("");
    return `<div class="pc-cap">INDOOR</div><div class="pc-big"><span class="pc-fig" data-unit="">${big}</span></div>${sub}`
      + (rows ? `<div class="pc-rows">${rows}</div>` : "");
  }

  // Outside: the first weather entity, else an outdoor temperature sensor
  // (as Nova's own weather answer does).
  _presentWeatherData() {
    const states = this._hass?.states || {};
    const live = (st) => st && st.state !== "unavailable" && st.state !== "unknown";
    const wid = Object.keys(states).sort().find(k => k.startsWith("weather.") && live(states[k]));
    if (wid) {
      const a = states[wid].attributes || {};
      const t = parseFloat(a.temperature);
      if (isFinite(t)) return { id: wid, temp: t };
    }
    const sid = Object.keys(states).sort().find(k => {
      const st = states[k], low = k.toLowerCase();
      return k.startsWith("sensor.") && live(st) && st.attributes?.device_class === "temperature"
        && (low.includes("outdoor") || low.includes("outside")) && isFinite(parseFloat(st.state));
    });
    return sid ? { id: sid, temp: parseFloat(states[sid].state) } : null;
  }

  _presentWeatherHtml(d) {
    const st = d && this._hass?.states?.[d.id];
    if (!st || !isFinite(d.temp)) return "";
    const h = this._hass, a = st.attributes || {};
    const plain = (s) => { const t = String(s || "").replace(/[_-]+/g, " ").trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
    const unit = a.temperature_unit || (st.attributes?.unit_of_measurement) || "°";
    // Home Assistant names its own states and attributes in the user's language.
    const cond = id => id.startsWith("weather.")
      ? (typeof h.formatEntityState === "function" ? h.formatEntityState(st) : plain(st.state)) : "";
    const attrName = k => (typeof h.formatEntityAttributeName === "function" ? h.formatEntityAttributeName(st, k) : plain(k));
    const attrVal = (k, fallbackUnit) => (typeof h.formatEntityAttributeValue === "function"
      ? h.formatEntityAttributeValue(st, k) : `${this._presentNum(a[k], 0)}${fallbackUnit ? (fallbackUnit === "%" ? "" : " ") + fallbackUnit : ""}`);
    const rows = [];
    if (isFinite(parseFloat(a.humidity))) rows.push([attrName("humidity"), attrVal("humidity", "%")]);
    if (isFinite(parseFloat(a.wind_speed))) rows.push([attrName("wind_speed"), attrVal("wind_speed", a.wind_speed_unit || "")]);
    const condition = cond(d.id);
    return `<div class="pc-cap">Weather</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(this._presentNum(d.temp, 0))}${this._esc(unit)}</span></div>`
      + (condition ? `<div class="pc-now">${this._esc(condition)}</div>` : "")
      + (rows.length ? `<div class="pc-rows">${rows.map(([l, v]) => `<div class="pc-row"><span>${this._esc(l)}</span><b>${this._esc(v)}</b></div>`).join("")}</div>` : "");
  }

  async _presentEnergyData() {
    const [status, today] = await Promise.all([
      this._hass.callWS({ type: "nova/energy_flow", action: "status" }),
      this._hass.callWS({ type: "nova/energy_flow", action: "today" }),
    ]);
    return {
      solar_today_kwh: today?.solar_kwh ?? null, battery_pct: status?.battery?.pct ?? null,
      export_kwh: today?.grid_export_kwh ?? null, home_kwh: today?.home_kwh ?? null,
      solar_w: status?.solar?.w ?? null, curve: [],
    };
  }

  // The security card is a calm summary from Home Assistant's live states,
  // counts only (no names): locks, doors, the alarm and lockdown. Null when
  // there is nothing to summarise, so the rotation skips it.
  _presentSecurityData() {
    const states = this._hass?.states || {};
    const cfg = this._liveData?.config || {};
    let locks = 0, locked = 0, doors = 0, open = 0;
    const alarms = [];
    for (const [id, st] of Object.entries(states)) {
      const s = st && st.state;
      if (id.startsWith("lock.")) {
        if (s === "locked") { locks++; locked++; }
        else if (["unlocked", "unlocking", "locking", "jammed", "open", "opening"].includes(s)) locks++;
      } else if (id.startsWith("binary_sensor.")) {
        const cls = st?.attributes?.device_class;
        if ((cls === "door" || cls === "garage_door") && (s === "on" || s === "off")) { doors++; if (s === "on") open++; }
      } else if (id.startsWith("alarm_control_panel.")) {
        alarms.push(id);
      }
    }
    // The alarm Nova watches: the one chosen in Settings, else the only one.
    const alarmId = cfg.security_alarm_entity && states[cfg.security_alarm_entity] ? cfg.security_alarm_entity
      : (alarms.length === 1 ? alarms[0] : null);
    const alarm = alarmId ? String(states[alarmId].state || "") : "";
    const data = {
      locks_total: locks || null, locks_locked: locks ? locked : null,
      doors_total: doors || null, doors_open: doors ? open : null,
      alarm: ["disarmed", "arming", "pending", "triggered"].includes(alarm) || alarm.startsWith("armed_") ? alarm : null,
      lockdown: !!this._liveData?.lockdown?.active,
    };
    return data.locks_total || data.doors_total || data.alarm || data.lockdown ? data : null;
  }

  _presentSecurityHtml(d) {
    const n = (v) => this._esc(this._presentNum(v, 0));
    const big = d.locks_total > 0;
    const alarmWord = !d.alarm ? null
      : d.alarm.startsWith("armed_") ? this._tx("ARMED")
      : d.alarm === "disarmed" ? this._tx("Disarmed")
      : d.alarm === "triggered" ? this._tx("Triggered") : this._tx("Arming");
    const rows = [
      alarmWord ? ["Security alarm", this._esc(alarmWord)] : null,
      d.doors_total > 0 ? ["Doors", d.doors_open > 0 ? `${n(d.doors_open)} ${this._esc(this._tx("Open"))}` : this._esc(this._tx("Closed"))] : null,
      d.lockdown ? ["Lockdown", this._esc(this._tx("ON"))] : null,
    ].filter(Boolean);
    if (!big && !rows.length) return "";
    return `${big ? `<div class="pc-cap">Locked</div><div class="pc-big"><span class="pc-fig" data-unit="">${n(d.locks_locked)}/${n(d.locks_total)}</span></div>` : ""}`
      + (rows.length ? `<div class="pc-rows">${rows.map(([label, value]) => `<div class="pc-row"><span>${label}</span><b>${value}</b></div>`).join("")}</div>` : "");
  }

  // The hero word's greetings. The hour decides which time of day greeting can
  // come up (none late at night), and it is never the same one twice in a row.
  _pickGreeting(hour = new Date().getHours()) {
    // A map with a label each, so the panel's string list picks every greeting up.
    const g = { hello: "Hello.", intro: "Hi, I'm Nova.", here: "Nova here.", service: "At your service.",
      calm: "All good here.", morning: "Good morning.", afternoon: "Good afternoon.", evening: "Good evening." };
    const pool = [g.hello, g.intro, g.here, g.service, g.calm];
    if (hour >= 5 && hour < 12) pool.push(g.morning);
    else if (hour >= 12 && hour < 18) pool.push(g.afternoon);
    else if (hour >= 18 && hour < 22) pool.push(g.evening);
    const options = pool.filter((g) => g !== this._heroGreeting);
    return options[Math.floor(Math.random() * options.length)];
  }

  // One round of the rotation: every kind of card, with a fresh greeting in
  // front of every third one, so the hero's own word is part of the rotation
  // and is not just what the head says between cards.
  _presentSteps() {
    const every = this._presentGreetEvery === undefined ? 3 : this._presentGreetEvery;
    const steps = [];
    Object.entries(this._presentKinds()).forEach(([kind, def], i) => {
      if (every > 0 && i % every === 0) steps.push(["greeting", null]);
      steps.push([kind, def]);
    });
    return steps;
  }

  // The next step comes one cycle after this one started (the timer starts
  // over here). A card stays 8 seconds and the cycle is 9, so the gap
  // between two steps is only a second.
  _presentRestartCycle() {
    if (!this._presentCycleTimer) return;
    clearInterval(this._presentCycleTimer);
    this._presentCycleTimer = setInterval(() => this._presentAmbient(), this._presentCycleMs || 9000);
  }

  async _presentAmbient() {
    const cfg = this._liveData?.config || {};
    if (cfg.hero_card_cycle === false || this._currentTab !== "dashboard") return;
    if (this._presenting || this._coreState() !== "idle" || document.hidden) return;
    const token = this._presentToken;
    const steps = this._presentSteps();
    // Each round starts at the next step; a card with nothing to show is skipped.
    for (let i = 0; i < steps.length; i++) {
      const at = ((this._presentTurn || 0) + i) % steps.length;
      const [kind, def] = steps[at];
      if (kind === "greeting") {
        this._presentTurn = at + 1;
        this._heroGreeting = this._pickGreeting();
        this._typeHeroWord(this._heroGreeting);
        this._presentRestartCycle();
        return;
      }
      let data = null;
      try { data = await def.ambient(); } catch (_) { /* no card of this kind this time */ }
      // Gone, or busy with a real card, while the figures were being read.
      if (this._presentToken !== token || this._presenting || this._coreState() !== "idle") return;
      if (!data || !def.html(data)) continue;
      this._presentTurn = at + 1;
      const now = Date.now() / 1000;
      this._presentRestartCycle();
      this._onPresentCard({ v: 1, kind, id: `ambient-${kind}-${Date.now()}`, created: now, expires: now + 8, data });
      return;
    }
  }

  _presentStop() {
    this._presentToken = (this._presentToken || 0) + 1;
    if (this._presentCycleTimer) { clearInterval(this._presentCycleTimer); this._presentCycleTimer = null; }
    if (this._presentFirstTimer) { clearTimeout(this._presentFirstTimer); this._presentFirstTimer = null; }
    const unsub = this._presentUnsub;
    this._presentUnsub = null;
    if (typeof unsub === "function") { try { unsub(); } catch (_) { /* already closed */ } }
    if (this._presenting) this._presentEnd();
  }

  _onPresentCard(card) {
    const def = card && card.v === 1 && card.data ? this._presentKinds()[card.kind] : null;
    if (!def) return;
    if (card.id && card.id === this._presentId) return;   // the same card twice
    const ttl = Math.max(0, (Number(card.expires) - Number(card.created)) * 1000) || 30000;
    let left = Number(card.expires) * 1000 - Date.now();
    if (!(left > 0)) return;                 // already expired
    if (left > ttl) left = ttl;              // this clock runs behind the server's
    const html = def.html(card.data);
    if (!html) return;
    const root = this.shadowRoot, el = root.getElementById("presentCard");
    if (!el) return;
    this._presentId = card.id || null;
    this._setHtml(el, html);
    root.getElementById("heroStage")?.classList.add("presenting");
    this._presenting = true;
    if (this._coreState() !== "asleep") this._targetCoreState("presenting");
    if (this._presentTimer) clearTimeout(this._presentTimer);
    this._presentTimer = setTimeout(() => this._presentEnd(), left);
    if (this._presentTick) { clearInterval(this._presentTick); this._presentTick = null; }
    if (def.refresh) {
      this._presentTick = setInterval(() => {
        const next = def.refresh(card.data);
        if (next) this._setHtml(el, next);
      }, def.every || 1000);
    }
  }

  _presentEnd() {
    if (this._presentTimer) { clearTimeout(this._presentTimer); this._presentTimer = null; }
    if (this._presentTick) { clearInterval(this._presentTick); this._presentTick = null; }
    if (!this._presenting) return;
    this._presenting = false;
    this.shadowRoot?.getElementById("heroStage")?.classList.remove("presenting");
    this._targetCoreState(this._coreState());
  }

  _presentNum(v, digits) {
    try {
      return Number(v).toLocaleString(this._resolveUiLang(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
    } catch (_) {
      return Number(v).toFixed(digits);
    }
  }

  // The card's markup, or "" when none of its figures can be shown. A
  // figure Nova could not read is null and its row is left out.
  _presentEnergyHtml(d) {
    const has = (v) => typeof v === "number" && isFinite(v);
    const n = (v, dp) => this._esc(this._presentNum(v, dp));
    const rows = [
      has(d.battery_pct) ? ["Battery", `${n(d.battery_pct, 0)}%`] : null,
      has(d.export_kwh) ? ["Sold to grid", `${n(d.export_kwh, 1)} kWh`] : null,
      has(d.home_kwh) ? ["Home used", `${n(d.home_kwh, 1)} kWh`] : null,
    ].filter(Boolean);
    const big = has(d.solar_today_kwh);
    if (!big && !rows.length && !has(d.solar_w)) return "";
    const curve = Array.isArray(d.curve) ? d.curve.filter(has) : [];
    let svg = "";
    if (curve.length >= 2) {
      const max = Math.max(...curve, 0.001);
      const pts = curve.map((v, i) => `${(i / (curve.length - 1) * 120).toFixed(1)},${(26 - v / max * 24).toFixed(1)}`).join(" ");
      svg = `<svg class="pc-curve" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--gold)" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
    }
    const now = has(d.solar_w) ? `<div class="pc-now"><span>Now</span> <span>· ${n(d.solar_w, 0)} W</span></div>` : "";
    return `${big ? `<div class="pc-cap">Solar made</div><div class="pc-big"><span class="pc-fig" data-unit="kWh">${n(d.solar_today_kwh, 1)}</span></div>` : ""}${now}${svg}`
      + (rows.length ? `<div class="pc-rows">${rows.map(([label, value]) => `<div class="pc-row"><span>${label}</span><b>${value}</b></div>`).join("")}</div>` : "");
  }
  // ─── Floor plan: stored layout, working copies and shared helpers ──────
  // The plan is kept in config as floor_plan_rooms (rooms and outdoor zones
  // per floor), floor_plan_entities (device pins), floor_plan_cameras,
  // floor_plan_elements (windows, doors, dormers) and floor_plan_property
  // (the lot outline). The editor changes deep copies of each and writes
  // them back only on Save.

  // The starting plan for a home with no saved plan. It shows only what the
  // home has (8.26.0 garage, 8.27.0 the rest). A saved plan is never changed.
  _defaultFloorPlan() {
    const cfg = this._data()?.config || {};
    const has = cfg.home_features || {};
    const plan = this._defaultFloorPlanBase();
    // A basement floor only where there is a basement, an upper floor only
    // when Stories is more than 1, and stairs only with a floor to reach.
    // (The basement's old guessed device labels went in 8.27.0.)
    if (!has.basement) delete plan.bsmt;
    if (String(cfg.home_stories ?? "1.5") === "1") delete plan["2f"];
    const ground = plan["1f"];
    if (!plan["2f"] && !plan.bsmt) ground.rooms = ground.rooms.filter(r => r.type !== "stairs");
    if (cfg.has_garage) return plan;
    // With no garage the back door opens from the kitchen at the rear, and
    // the garage's space is a utility room only when Home Assistant has one.
    ground.viewBox = "0 0 320 162";
    ground.rooms = ground.rooms
      .filter(r => r.name !== "Garage" || has.utility)
      .map(r => (r.name === "Garage" ? { ...r, name: "Utility Room", y: r.y + 12 } : { ...r, y: r.y + 12 }));
    ground.rooms.push({ name: "Back Door", x: 125, y: 3, w: 45, h: 10, type: "door" });
    return plan;
  }
  _defaultFloorPlanBase() {
    const ground = [
      { name: "Garage", x: 5, y: 5, w: 100, h: 88, type: "room" },
      { name: "Kitchen", x: 115, y: 5, w: 65, h: 40, type: "room" },
      { name: "Bath", x: 185, y: 5, w: 28, h: 22, type: "bath" },
      { name: "Guest Room", x: 218, y: 5, w: 95, h: 40, type: "room" },
      { name: "Dining Room", x: 115, y: 50, w: 65, h: 38, type: "room" },
      { name: "Stairs", x: 185, y: 32, w: 28, h: 32, type: "stairs" },
      { name: "Living Room", x: 218, y: 50, w: 95, h: 38, type: "room" },
      { name: "Downstairs Hallway", x: 115, y: 93, w: 198, h: 20, type: "room" },
      { name: "Front Door", x: 185, y: 117, w: 50, h: 12, type: "door" },
    ];
    const upper = [
      { name: "Bedroom 2", x: 50, y: 25, w: 95, h: 80, type: "room" },
      { name: "Bath", x: 150, y: 25, w: 30, h: 40, type: "bath" },
      { name: "Master Bedroom", x: 185, y: 25, w: 85, h: 80, type: "room" },
      { name: "Upstairs Hallway", x: 150, y: 70, w: 30, h: 35, type: "room" },
      { name: "Stairs", x: 150, y: 108, w: 25, h: 20, type: "stairs" },
    ];
    const lower = [
      { name: "Basement", x: 50, y: 10, w: 220, h: 90, type: "room" },
      { name: "Stairs", x: 120, y: 20, w: 28, h: 35, type: "stairs" },
    ];
    const floor = (label, height, rooms) => ({ label, viewBox: `0 0 320 ${height}`, rooms });
    return { "1f": floor("1st Floor", 150, ground), "2f": floor("2nd Floor", 140, upper), "bsmt": floor("Basement", 130, lower) };
  }

  // A config value that may be stored as JSON text or as an object. Empty
  // or unreadable text gives null.
  _fpStored(key) {
    const raw = this._data()?.config?.[key];
    try {
      if (typeof raw === "string") return raw ? JSON.parse(raw) : null;
      return raw || null;
    } catch (_) {
      return null;
    }
  }
  // A working copy of one stored part, made on first use and kept until Save.
  _fpWorking(field, read) {
    if (!this[field]) this[field] = JSON.parse(JSON.stringify(read()));
    return this[field];
  }
  // One floor's list inside a per-floor store, created empty when missing.
  _fpFloorList(store, floor) {
    if (!Array.isArray(store[floor])) store[floor] = [];
    return store[floor];
  }

  _getFloorPlan() {
    const saved = this._fpStored("floor_plan_rooms");
    if (saved && typeof saved === "object" && Object.keys(saved).length) return saved;
    return this._defaultFloorPlan();
  }
  _getEditingPlan() { return this._fpWorking("_editingPlan", () => this._getFloorPlan()); }

  // Plan units are a fifth of a foot: one 50 unit grid square is 10 ft.
  _fpUnits() { return this._data()?.config?.floor_plan_units === "metric" ? "metric" : "imperial"; }
  _fpUnitLabel() { return this._fpUnits() === "metric" ? "m" : "ft"; }
  _fpToReal(u) {
    const feet = (u || 0) * 0.2;
    const shown = this._fpUnits() === "metric" ? feet * 0.3048 : feet;
    return Math.round(shown * 10) / 10;
  }
  // The other way: feet or metres typed in a field back to plan units, at
  // least 8 so an opening or a camera's reach never shrinks to nothing.
  _fpFromReal(v) {
    const feet = this._fpUnits() === "metric" ? v / 0.3048 : v;
    return Math.max(8, Math.round(feet * 5));
  }
  _fpDim(u) { return `${this._fpToReal(u)}${this._fpUnits() === "metric" ? "m" : "'"}`; }

  // The box around a list of rooms, from each room's x, y, w and h.
  _fpRoomBox(r) { return { x0: r.x, y0: r.y, x1: r.x + r.w, y1: r.y + r.h }; }
  _fpBounds(rooms) {
    const box = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
    for (const r of rooms) {
      const own = this._fpRoomBox(r);
      box.x0 = Math.min(box.x0, own.x0); box.y0 = Math.min(box.y0, own.y0);
      box.x1 = Math.max(box.x1, own.x1); box.y1 = Math.max(box.y1, own.y1);
    }
    return box;
  }
  // The box around a list of [x, y] points.
  _fpPointBounds(pts) {
    const box = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
    for (const p of pts) {
      box.x0 = Math.min(box.x0, p[0]); box.y0 = Math.min(box.y0, p[1]);
      box.x1 = Math.max(box.x1, p[0]); box.y1 = Math.max(box.y1, p[1]);
    }
    return box;
  }

  // Device pins: live state markers, drag to move, tap for HA's more-info.
  _getFloorEntities() { return this._fpStored("floor_plan_entities") || {}; }
  _getEditingEntities() { return this._fpWorking("_editingEntities", () => this._getFloorEntities()); }
  _entsFor(floor) { return this._fpFloorList(this._getEditingEntities(), floor); }
  // A pin's colour, name and short state. Unknown entities show their id.
  _entMarkerStyle(eid) {
    const dim = "var(--ink-faint)", gold = "var(--gold)", warn = "var(--warn)";
    const [domain, objectId] = eid.split(".");
    const st = this._hass && this._hass.states ? this._hass.states[eid] : null;
    if (!st) return { color: dim, name: objectId || eid, val: "—" };
    const s = st.state, at = st.attributes || {}, name = at.friendly_name || eid;
    const lit = (on, color, val) => ({ color: on ? color : dim, name, val });
    switch (domain) {
      case "sensor":
        return { color: gold, name, val: s === "unknown" || s === "unavailable" ? "—" : s + (at.unit_of_measurement || "") };
      case "binary_sensor": {
        const on = s === "on", dc = at.device_class || "";
        if (["door", "window", "garage_door", "opening"].includes(dc)) return lit(on, warn, on ? "OPEN" : "SHUT");
        if (["motion", "occupancy", "presence"].includes(dc)) return lit(on, gold, on ? "DET" : "—");
        return lit(on, gold, on ? "ON" : "OFF");
      }
      case "lock": {
        const locked = s === "locked";
        return { color: locked ? dim : "#ff5a5a", name, val: locked ? "LOCK" : "OPEN" };
      }
      case "cover": {
        const open = s === "open" || s === "opening";
        return lit(open, warn, open ? "OPEN" : "SHUT");
      }
      case "person":
      case "device_tracker": {
        const home = s === "home";
        return lit(home, gold, home ? "HOME" : "AWAY");
      }
      case "climate": {
        const t = at.current_temperature;
        return { color: gold, name, val: t != null ? t + "°" : s };
      }
      default: {
        const on = !["off", "unavailable", "unknown", "idle", "standby", "none"].includes(s);
        return lit(on, gold, on ? "ON" : "OFF");
      }
    }
  }
  _fpBgOpacity() {
    const v = parseFloat(this._data()?.config?.floor_plan_bg_opacity);
    return v >= 0 && v <= 1 ? v : 0.2;
  }

  // Outdoor zones are rooms with a points outline; plain rooms are boxes.
  _fpHasOutline(r) { return !!r && Array.isArray(r.points) && r.points.length >= 3; }
  _zonePoints(r) {
    if (this._fpHasOutline(r)) return r.points;
    const left = r.x || 0, top = r.y || 0, right = left + (r.w || 40), bottom = top + (r.h || 40);
    return [[left, top], [right, top], [right, bottom], [left, bottom]];
  }
  _ensureZonePoints(rm) {
    if (!this._fpHasOutline(rm)) rm.points = this._zonePoints(rm).map(p => p.slice(0, 2));
    return rm.points;
  }
  // Keeps a shaped room's x, y, w and h on the box around its outline.
  _syncRoomBBox(rm) {
    if (!this._fpHasOutline(rm)) return;
    const b = this._fpPointBounds(rm.points);
    rm.x = Math.round(b.x0); rm.y = Math.round(b.y0);
    rm.w = Math.round(b.x1 - b.x0); rm.h = Math.round(b.y1 - b.y0);
  }
  _propPathD(pts) { return pts.map((p, k) => `${k ? "L" : "M"}${p[0]} ${p[1]}`).join(" ") + " Z"; }

  // The lot outline, stored as { points: [...] }.
  _getProperty() {
    const stored = this._fpStored("floor_plan_property");
    return stored && Array.isArray(stored.points) ? stored.points : [];
  }
  _propertyPts() { return this._fpWorking("_editingProperty", () => this._getProperty()); }
  _setProperty(pts) { this._editingProperty = pts; }
  // The lot's size in the chosen units (shoelace area, 0.04 sq ft per unit²).
  _propertyArea(pts) {
    if (!pts || pts.length < 3) return "";
    const twice = pts.reduce((sum, p, i) => {
      const q = pts[(i + 1) % pts.length];
      return sum + (p[0] * q[1] - q[0] * p[1]);
    }, 0);
    const sqFt = Math.abs(twice) / 2 * 0.04;
    if (this._fpUnits() !== "metric") {
      return sqFt >= 43560 ? (sqFt / 43560).toFixed(2) + " acres" : Math.round(sqFt).toLocaleString() + " sq ft";
    }
    const sqM = sqFt * 0.092903;
    return sqM >= 10000 ? (sqM / 10000).toFixed(2) + " ha" : Math.round(sqM).toLocaleString() + " m²";
  }

  // Windows, doors and dormers ("openings"). Their gaps let camera coverage
  // see through doorways rather than treating every wall as solid.
  _getFloorElements() { return this._fpStored("floor_plan_elements") || {}; }
  _getEditingElements() { return this._fpWorking("_editingElements", () => this._getFloorElements()); }
  _elemsFor(floor) { return this._fpFloorList(this._getEditingElements(), floor); }
  // Entities a door or window can be bound to: covers, locks, and opening
  // sensors by device class or by a door-like word in the id or name.
  _doorEntityOptions(selected) {
    const states = this._hass?.states || {};
    const doorWords = /door|garage|gate|cellar|bulkhead|hatch|window|contact|entry|slider|sash|casement|patio|french|skylight|opening|sliding/i;
    const fits = eid => {
      const domain = eid.split(".")[0];
      if (domain === "cover" || domain === "lock") return true;
      const at = states[eid].attributes || {};
      return domain === "binary_sensor" && (["door", "window", "garage_door", "opening"].includes(at.device_class || "")
        || doorWords.test(eid) || doorWords.test(at.friendly_name || ""));
    };
    const picks = Object.keys(states).filter(fits).sort();
    if (selected && !picks.includes(selected)) picks.unshift(selected);
    const opts = picks.map(eid => `<option value="${this._esc(eid)}"${eid === selected ? " selected" : ""}>${this._esc(this._entName(eid))}</option>`).join("");
    return `<option value=""${selected ? "" : " selected"}>— auto-detect —</option>${opts}`;
  }

  // Cameras placed on the plan, for the cone and AI coverage.
  _getFloorCameras() { return this._fpStored("floor_plan_cameras") || {}; }
  _getEditingCameras() { return this._fpWorking("_editingCameras", () => this._getFloorCameras()); }
  _camsFor(floor) { return this._fpFloorList(this._getEditingCameras(), floor); }
  _cameraEntityOptions(selected) {
    const states = this._hass?.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith("camera.")).sort();
    if (selected && !ids.includes(selected)) ids.unshift(selected);
    const label = id => (states[id] && states[id].attributes && states[id].attributes.friendly_name) || id;
    const opts = ids.map(id => `<option value="${this._esc(id)}"${id === selected ? " selected" : ""}>${this._esc(label(id))}</option>`).join("");
    return `<option value="">— camera —</option>${opts}`;
  }
  // ─── Floor plan maths: walls, sight lines and camera coverage ──────────
  // Every result here is pinned in scripts/smoke_panel.js, so keep the
  // arithmetic in the same order when changing anything below.

  // The crossing point of segment A (x1,y1 to x2,y2) and segment B
  // (x3,y3 to x4,y4), or null when they are parallel or do not meet.
  _segIntersect(x1, y1, x2, y2, x3, y3, x4, y4) {
    const ax = x1 - x2, ay = y1 - y2, bx = x3 - x4, by = y3 - y4, cx = x1 - x3, cy = y1 - y3;
    const cross = ax * by - ay * bx;
    if (Math.abs(cross) < 1e-9) return null;
    const along = (cx * by - cy * bx) / cross;
    const onB = (cx * ay - cy * ax) / cross;
    if (along < 0 || along > 1 || onB < 0 || onB > 1) return null;
    return { x: x1 + along * (x2 - x1), y: y1 + along * (y2 - y1) };
  }

  // Solid walls (the outline of every walled room) and the gaps doors and
  // windows leave in them. Stairs, door rooms and outdoor zones add no walls.
  _planGeometry(floor) {
    const rooms = (this._getEditingPlan()[floor] || {}).rooms || [];
    const walls = [];
    for (const r of rooms) {
      if (r.type === "stairs" || r.type === "door" || r.type === "outdoor") continue;
      const corners = this._zonePoints(r);
      corners.forEach((a, k) => {
        const b = corners[(k + 1) % corners.length];
        walls.push({ x0: a[0], y0: a[1], x1: b[0], y1: b[1] });
      });
    }
    // Outside openings sit on the whole floor's outline, inside ones on their room.
    const outline = rooms.length ? this._fpBounds(rooms) : null;
    const span = (lo, hi, along, half) => { const mid = lo + along * (hi - lo); return [mid - half, mid + half]; };
    const gaps = [];
    for (const e of this._elemsFor(floor) || []) {
      if (e.type !== "door" && e.type !== "window") continue;
      let box = outline;
      if ((e.kind === "interior" || e.kind === "cased") && e.room) {
        const own = rooms.find(r => r.name === e.room);
        if (own) box = this._fpRoomBox(own);
      }
      if (!box) continue;
      const half = (e.w || 20) / 2, along = e.pos != null ? e.pos : 0.5;
      if (e.wall === "front" || e.wall === "back") {
        const [from, to] = span(box.x0, box.x1, along, half), y = e.wall === "front" ? box.y0 : box.y1;
        gaps.push({ x0: from, y0: y, x1: to, y1: y });
      } else {
        const [from, to] = span(box.y0, box.y1, along, half), x = e.wall === "left" ? box.x0 : box.x1;
        gaps.push({ x0: x, y0: from, x1: x, y1: to });
      }
    }
    return { walls, gaps };
  }

  // True when (x, y) lies in a door or window gap, give or take 2.5 units.
  _inGap(x, y, gaps) {
    const within = (v, a, b) => v >= Math.min(a, b) - 2.5 && v <= Math.max(a, b) + 2.5;
    return gaps.some(g => within(x, g.x0, g.x1) && within(y, g.y0, g.y1));
  }

  // True when no solid wall stands between the two points.
  _losClear(x1, y1, x2, y2, geo) {
    return geo.walls.every(w => {
      const hit = this._segIntersect(x1, y1, x2, y2, w.x0, w.y0, w.x1, w.y1);
      return !hit || this._inGap(hit.x, hit.y, geo.gaps);
    });
  }

  // True when the point is in range, inside the cone and in plain sight.
  _pointCovered(px, py, cx, cy, ang, half, rng, geo) {
    const offX = px - cx, offY = py - cy;
    if (Math.hypot(offX, offY) > rng) return false;
    const bearing = Math.atan2(offY, offX) * 180 / Math.PI;
    if (Math.abs(((bearing - ang + 540) % 360) - 180) > half) return false;
    return this._losClear(cx, cy, px, py, geo);
  }

  // A camera's aim with the editor's defaults: facing up the plan, 90° wide,
  // reaching 55 units and never less than 5.
  _camAim(cam) {
    const or = (v, fallback) => (v != null ? v : fallback);
    return { x: cam.x, y: cam.y, ang: or(cam.angle, 270), fov: or(cam.fov, 90), rng: Math.max(or(cam.range, 55), 5) };
  }

  // Calls visit(x, y) at the centre of each cell of an n by n grid over a box.
  _fpGrid(x0, y0, w, h, n, visit) {
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) visit(x0 + (i + 0.5) / n * w, y0 + (j + 0.5) / n * h);
    }
  }

  // The share of each room one camera sees, to two places: a 4 by 4 sample
  // for a plain room, 6 by 6 (inside the outline only) for a shaped room or
  // zone. Rooms it cannot see at all are left out.
  _computeCoverage(floor, cam, geo) {
    const rooms = (this._getEditingPlan()[floor] || {}).rooms || [];
    if (!rooms.length) return {};
    const sight = geo || this._planGeometry(floor), aim = this._camAim(cam), half = aim.fov / 2;
    const share = {};
    for (const r of rooms) {
      if (r.type === "door" || r.type === "stairs") continue;
      let seen = 0, tried = 0;
      const tally = (px, py) => {
        tried++;
        if (this._pointCovered(px, py, aim.x, aim.y, aim.ang, half, aim.rng, sight)) seen++;
      };
      if (r.type === "outdoor" || (r.points && r.points.length >= 3)) {
        const outline = this._zonePoints(r), box = this._fpPointBounds(outline);
        this._fpGrid(box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0, 6, (px, py) => {
          if (this._pointInPoly(px, py, outline)) tally(px, py);
        });
      } else {
        this._fpGrid(r.x, r.y, r.w, r.h, 4, tally);
      }
      if (seen > 0 && tried > 0) share[r.name] = Math.round(seen / tried * 100) / 100;
    }
    return share;
  }

  // Even-odd test: a ray from the point crosses the outline an odd number of times.
  _pointInPoly(x, y, pts) {
    let inside = false;
    pts.forEach((p, i) => {
      const q = pts[(i || pts.length) - 1];
      if ((p[1] > y) !== (q[1] > y) && x < (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]) + p[0]) inside = !inside;
    });
    return inside;
  }

  // The full cone as an SVG path: two edges and the arc at full range.
  _coneD(cam) {
    const aim = this._camAim(cam);
    const rim = deg => {
      const rad = deg * Math.PI / 180;
      return `${(aim.x + aim.rng * Math.cos(rad)).toFixed(1)} ${(aim.y + aim.rng * Math.sin(rad)).toFixed(1)}`;
    };
    const wide = aim.fov > 180 ? 1 : 0;
    return `M ${aim.x} ${aim.y} L ${rim(aim.ang - aim.fov / 2)} A ${aim.rng} ${aim.rng} 0 ${wide} 1 ${rim(aim.ang + aim.fov / 2)} Z`;
  }

  // How far a ray from (cx, cy) at angle ang (radians) gets before a solid wall.
  _rayCast(cx, cy, ang, range, geo) {
    const endX = cx + range * Math.cos(ang), endY = cy + range * Math.sin(ang);
    let nearest = range;
    for (const w of geo.walls) {
      const hit = this._segIntersect(cx, cy, endX, endY, w.x0, w.y0, w.x1, w.y1);
      if (!hit || this._inGap(hit.x, hit.y, geo.gaps)) continue;
      const dist = Math.hypot(hit.x - cx, hit.y - cy);
      if (dist < nearest) nearest = dist;
    }
    return nearest;
  }

  // The cone cut short by walls: one ray at least every 3 degrees, 24 at least.
  _clippedCone(cam, geo) {
    if (!geo || !geo.walls || !geo.walls.length) return this._coneD(cam);
    const aim = this._camAim(cam);
    const rays = Math.max(24, Math.round(aim.fov / 3));
    const start = (aim.ang - aim.fov / 2) * Math.PI / 180, step = (aim.fov * Math.PI / 180) / rays;
    const path = [`M ${aim.x} ${aim.y}`];
    for (let k = 0; k <= rays; k++) {
      const a = start + k * step, reach = this._rayCast(aim.x, aim.y, a, aim.rng, geo);
      path.push(`L ${(aim.x + reach * Math.cos(a)).toFixed(1)} ${(aim.y + reach * Math.sin(a)).toFixed(1)}`);
    }
    return path.join(" ") + " Z";
  }

  // The room a point is in, or else the room whose centre is nearest.
  _roomAt(floor, x, y) {
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const holder = rooms.find(r => r.type !== "stairs" && r.type !== "door"
      && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
    if (holder) return holder.name;
    let nearest = null, best = 1e18;
    for (const r of rooms) {
      const dx = r.x + r.w / 2 - x, dy = r.y + r.h / 2 - y, d2 = dx * dx + dy * dy;
      if (d2 < best) { best = d2; nearest = r.name; }
    }
    return nearest || "the area";
  }

  // Plain notes on inside openings and stairs, sent with a coverage request.
  _openingDescriptions(floor) {
    const lead = { cased: "cased opening at ", interior: "interior door at " };
    const notes = (this._elemsFor(floor) || [])
      .filter(e => e.type === "door" && (e.kind === "cased" || e.kind === "interior") && e.room)
      .map(e => lead[e.kind] + e.room);
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    if (rooms.some(r => r.type === "stairs")) notes.push("open staircase");
    return notes;
  }
  // An imported layout file is text from outside, and the editor writes its
  // names, floor keys and values into HTML and SVG attributes. Drop the
  // characters that can close an attribute or open a tag, in keys and values
  // alike, and skip the keys that change an object's prototype.
  _fpCleanImport(value, depth = 0) {
    if (depth > 12) return null;
    if (typeof value === "string") return value.replace(/[<>"`]/g, "");
    if (Array.isArray(value)) return value.map(v => this._fpCleanImport(v, depth + 1));
    if (value && typeof value === "object") {
      const out = {};
      for (const key of Object.keys(value)) {
        if (key === "__proto__" || key === "constructor" || key === "prototype") continue;
        out[this._fpCleanImport(key, depth + 1)] = this._fpCleanImport(value[key], depth + 1);
      }
      return out;
    }
    return value;
  }

  _floorPlanEditorCardBody() {
    const plan = this._getEditingPlan();
    const floors = Object.keys(plan);
    if (!this._editorFloor || !plan[this._editorFloor]) this._editorFloor = floors[0] || "1f";
    const floor = this._editorFloor;
    return `
      <div class="fpn-toolbar">
        <div class="fpn-floor-tabs">
          ${floors.map(fk => `<button class="mode-chip fpn-floor-tab${fk === floor ? " active" : ""}" data-fpn-floor="${fk}">${this._esc(plan[fk].label || fk)}</button>`).join("")}
        </div>
        <div class="fpn-actions">
          <button class="mode-chip" id="fpnAddRoom">+ Add Room</button>
          <button class="mode-chip" id="fpnAddZone">+ Outdoor Zone</button>
          ${this._fpnAddPropertyButton()}
          <button class="mode-chip" id="fpnUnits">${this._fpUnits() === "metric" ? "Units: Metric" : "Units: Imperial"}</button>
          <button class="mode-chip" id="fpnZoomFit">⤢ Fit</button>
        </div>
      </div>
      <div class="fpn-hint">Drag to move · bottom-right handle to resize · right-click to delete · double-click an edge to add a corner · scroll to zoom · drag empty space to pan</div>
      <div class="fpn-canvas" id="fpnCanvas">${this._renderFloorPlanSVG(plan, floor)}</div>
      ${this._renderOpeningsNew(floor)}
      ${this._renderCamerasNew(floor)}
      ${this._renderPlanEntitiesNew(floor)}
      <div class="fpn-actions">
        <button class="mode-chip" id="fpnSave">Save Layout</button>
        <button class="mode-chip" id="fpnReset">Reset Default</button>
        <button class="mode-chip" id="fpnExport">⬇ Export</button>
        <button class="mode-chip" id="fpnImport">⬆ Import</button>
        <input type="file" id="fpnImportFile" accept=".json,application/json" style="display:none">
      </div>`;
  }

  _fpnAddPropertyButton() {
    const pp = this._propertyPts();
    const has = pp.length >= 3;
    return `<button class="mode-chip" id="fpnAddProperty">${has ? "Clear Property" : "+ Property Line"}</button>`
      + (has ? `<span class="toggle-desc">${this._tHtml("Lot: {area}", { area: this._propertyArea(pp) })}</span>` : "");
  }

  _renderPlanEntitiesNew(floor) {
    const ents = this._entsFor(floor);
    const op = this._fpBgOpacity();
    const hasBg = !!(this._fpStored("floor_plan_bg") || {})[floor];
    const chips = ents.length
      ? ents.map((e, i) => `<span class="new-pl-chip">${this._esc(this._entMarkerStyle(e.e).name)}<button class="fpn-ent-del" data-ei="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">No devices placed on this floor yet.</span>`;
    return `
      <div class="mode-bind-head">Devices on plan <span class="toggle-desc">add a device, drag its pin on the canvas, tap it to open controls</span></div>
      <div class="cfg-row">
        <input id="fpnEntInput" list="fpnEntList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="fpnEntList">${this._allEntityDatalist()}</datalist>
        <button class="mode-chip" id="fpnEntAdd">+ Add</button>
      </div>
      <div class="mode-grid">${chips}</div>
      <div class="mode-bind-head">Imported plan <span class="toggle-desc">${hasBg ? "opacity of the uploaded floor-plan image behind the rooms" : "upload a real floor-plan image to trace rooms over"}</span></div>
      <div class="cfg-row">
        <button class="mode-chip" id="fpnBgUpload">${hasBg ? "⬆ Replace Image" : "⬆ Upload Image"}</button>
        <input type="file" id="fpnBgFile" accept="image/*" style="display:none">
        <label>opacity</label>
        <input id="fpnBgOp" type="range" min="0" max="1" step="0.05" value="${op}">
        <span id="fpnBgOpVal">${Math.round(op * 100)}%</span>
      </div>`;
  }

  _renderOpeningsNew(floor) {
    const els = this._elemsFor(floor), uL = this._fpUnitLabel();
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const walls = [["front", "Front"], ["back", "Back"], ["left", "Left"], ["right", "Right"]];
    const wsel = (e, i) => `<select class="op-field-new" data-op="wall" data-i="${i}">${walls.map(w => `<option value="${w[0]}"${e.wall === w[0] ? " selected" : ""}>${w[1]}</option>`).join("")}</select>`;
    const rsel = (e, i) => `<select class="op-field-new" data-op="room" data-i="${i}"><option value="">— room —</option>${rooms.filter(r => r.type !== "outdoor").map(r => `<option value="${this._esc(r.name)}"${e.room === r.name ? " selected" : ""}>${this._esc(r.name)}</option>`).join("")}</select>`;
    const rows = els.map((e, i) => {
      const isD = e.type === "dormer";
      const t = isD ? (e.slope === "rear" ? "REAR DORMER" : "FRONT DORMER") : (e.type === "window" ? "WINDOW" : (e.kind === "interior" ? "INT DOOR" : (e.kind === "cellar" ? "CELLAR" : (e.kind === "cased" ? "CASED OPENING" : "EXT DOOR"))));
      const place = isD
        ? `<select class="op-field-new" data-op="slope" data-i="${i}"><option value="front"${e.slope !== "rear" ? " selected" : ""}>Front slope</option><option value="rear"${e.slope === "rear" ? " selected" : ""}>Rear slope</option></select>`
        : ((e.kind === "interior" || e.kind === "cased") ? (rsel(e, i) + " " + wsel(e, i)) : wsel(e, i));
      return `
        <div class="cfg-row op-row-new" data-i="${i}">
          <span class="new-pl-chip">${t}</span>
          ${place}
          <input class="op-field-new" data-op="pos" data-i="${i}" type="range" min="0" max="1" step="0.02" value="${e.pos != null ? e.pos : 0.5}" title="position along the wall">
          ${isD ? "" : `<input class="op-field-new op-num-new" data-op="w" data-i="${i}" type="number" min="1" step="0.5" value="${this._fpToReal(e.w || 20)}" style="width:56px"> ${uL}`}
          ${e.kind === "cased" ? `<span class="toggle-desc">open passage · no sensor</span>` : `<select class="op-field-new op-ent-new" data-op="entity" data-i="${i}">${this._doorEntityOptions(e.entity || "")}</select>`}
          <button class="fpn-ent-del op-del-new" data-i="${i}" title="Remove">×</button>
        </div>`;
    }).join("");
    const dBtns = floor === "2f" ? `<button class="mode-chip" id="opAddFdormer">+ Front Dormer</button><button class="mode-chip" id="opAddRdormer">+ Rear Dormer</button>` : "";
    return `
      <div class="mode-bind-head">Windows, doors &amp; dormers <span class="toggle-desc">interior doors &amp; cased openings attach to a room · a cased opening is a doorway with no door · dormers on the 2nd floor</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="opAddWindow">+ Window</button>
        <button class="mode-chip" id="opAddExtdoor">+ Exterior Door</button>
        <button class="mode-chip" id="opAddCellar">+ Cellar Door</button>
        <button class="mode-chip" id="opAddIntdoor">+ Interior Door</button>
        <button class="mode-chip" id="opAddCased">+ Cased Opening</button>
        ${dBtns}
      </div>
      ${rows || `<div class="toggle-desc">No openings placed on this floor yet — add one above.</div>`}`;
  }

  _renderCamerasNew(floor) {
    const cams = this._camsFor(floor), uL = this._fpUnitLabel();
    const geo = cams.length ? this._planGeometry(floor) : null;
    const zoneNames = new Set((((this._getEditingPlan()[floor] || {}).rooms) || []).filter(r => r.type === "outdoor").map(r => r.name));
    const rows = cams.map((c, i) => {
      const cov = geo ? this._computeCoverage(floor, c, geo) : {};
      const order = Object.keys(cov).sort((a, b) => cov[b] - cov[a]);
      const covLine = order.length
        ? `<div class="toggle-desc">sees: ${order.map(rn => `${this._esc(rn)} ${Math.round(cov[rn] * 100)}%${zoneNames.has(rn) ? " (zone)" : ""}`).join(" · ")}</div>`
        : `<div class="toggle-desc">nothing in view — aim it, widen the FOV, or extend the range</div>`;
      const cvg = c.coverage;
      const llmLine = (cvg && cvg.reason)
        ? `<div class="toggle-desc">${(cvg.covered && cvg.covered.length) ? `✓ confirms ${cvg.covered.map(r => this._esc(r)).join(", ")} — ` : ""}${this._esc(cvg.reason)}</div>`
        : "";
      return `
        <div class="cfg-row cam-row-new" data-ci="${i}">
          <span class="new-pl-chip">${this._tHtml("CAM {number}", { number: i + 1 })}</span>
          <select class="cam-field-new" data-cam="entity" data-ci="${i}">${this._cameraEntityOptions(c.entity || "")}</select>
          <label class="fpn-inline-lbl">aim <input class="cam-field-new" data-cam="angle" data-ci="${i}" type="range" min="0" max="359" step="1" value="${c.angle != null ? c.angle : 270}"></label>
          <label class="fpn-inline-lbl">FOV <input class="cam-field-new" data-cam="fov" data-ci="${i}" type="range" min="20" max="170" step="5" value="${c.fov != null ? c.fov : 90}"></label>
          <label class="fpn-inline-lbl">range <input class="cam-field-new cam-num-new" data-cam="range" data-ci="${i}" type="number" min="5" step="5" value="${this._fpToReal(c.range != null ? c.range : 55)}"> ${uL}</label>
          <button class="mode-chip cam-io-new" data-ci="${i}" title="indoor = bounded by walls, outdoor = by range">${c.indoor === false ? "OUTDOOR" : "INDOOR"}</button>
          <button class="fpn-ent-del cam-del-new" data-ci="${i}" title="Remove">×</button>
        </div>
        ${covLine}${llmLine}`;
    }).join("");
    return `
      <div class="mode-bind-head">Cameras · field of view <span class="toggle-desc">drop a camera, bind its entity, aim it — drag the dot on the plan to move, right-click to delete</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="fpnCamAdd">+ Camera</button>
        ${cams.length ? `<button class="mode-chip" id="fpnCamCompute" title="AI: judge what each camera can confirm">Compute coverage</button>` : ""}
      </div>
      ${rows || `<div class="toggle-desc">No cameras placed on this floor yet — add one above.</div>`}`;
  }

  _renderFloorPlanSVG(plan, floor) {
    const floorData = plan[floor];
    if (!floorData) return "";
    const vb = this._editVB
      ? `${this._editVB.x} ${this._editVB.y} ${this._editVB.w} ${this._editVB.h}`
      : (floorData.viewBox || "0 0 320 150");
    let svg = `<svg viewBox="${vb}" class="fpn-svg" id="fpnSvg" style="width:100%;height:100%;min-height:520px;background:var(--bg);border:1px solid var(--line-soft);border-radius:10px;cursor:crosshair;">`;
    svg += '<defs>'
      + '<pattern id="fpn-grid-sm" width="10" height="10" patternUnits="userSpaceOnUse"><path d="M 10 0 L 0 0 0 10" fill="none" stroke="rgba(244,184,96,0.05)" stroke-width="0.2"/></pattern>'
      + '<pattern id="fpn-grid-lg" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M 50 0 L 0 0 0 50" fill="none" stroke="rgba(244,184,96,0.12)" stroke-width="0.3"/></pattern>'
      + '</defs>';
    const vbp = vb.split(" ").map(Number);
    const gx = vbp[0], gy = vbp[1], gw = vbp[2], gh = vbp[3];
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-sm)"/>`;
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-lg)"/>`;

    // A traced-over image of the real plan, escaped like any stored text.
    const traced = (this._fpStored("floor_plan_bg") || {})[floor];
    if (traced) {
      svg += `<image href="${this._esc(traced)}" x="0" y="0" width="100%" height="100%" opacity="${this._fpBgOpacity()}" preserveAspectRatio="xMidYMid meet"/>`;
    }

    // Property boundary (the lot) — draw behind rooms; vertices are draggable.
    let prop = [];
    try { prop = this._propertyPts() || []; } catch (_) { prop = []; }
    if (prop.length >= 2) {
      svg += `<path class="fpn-prop-path" d="${this._propPathD(prop)}" fill="rgba(244,184,96,0.03)" stroke="var(--gold)" stroke-width="1" stroke-dasharray="6 4" pointer-events="none"/>`;
      for (let vi = 0; vi < prop.length; vi++) {
        const a = prop[vi], b = prop[(vi + 1) % prop.length];
        svg += `<circle class="fpn-prop-mid" data-prop-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2.2" fill="none" stroke="var(--gold)" stroke-width="0.7" opacity="0.5" style="cursor:copy"/>`;
      }
      for (let pi = 0; pi < prop.length; pi++) {
        svg += `<circle class="fpn-prop-vtx" data-prop-vtx="${pi}" cx="${prop[pi][0]}" cy="${prop[pi][1]}" r="3" fill="var(--gold)" stroke="var(--bg)" stroke-width="0.7" style="cursor:grab"/>`;
      }
    }

    for (let i = 0; i < (floorData.rooms || []).length; i++) {
      const rm = floorData.rooms[i];
      const colors = { room: "var(--gold)", bath: "var(--ink-faint)", stairs: "var(--ember)", door: "var(--warn)", outdoor: "#8fdba8" };
      const c = colors[rm.type] || "var(--gold)";
      const out = rm.type === "outdoor";
      const fs = rm.w > 80 ? 7 : (rm.w > 50 ? 5.5 : (rm.w > 25 ? 4 : 3));
      if (out || (rm.points && rm.points.length >= 3)) {
        const zpts = this._zonePoints(rm);
        let zcx = 0, zcy = 0; zpts.forEach(p => { zcx += p[0]; zcy += p[1]; }); zcx /= zpts.length; zcy /= zpts.length;
        svg += `<g class="fpn-zone" data-zone-idx="${i}">`;
        svg += `<path class="fpn-zone-path" data-zone-idx="${i}" d="${this._propPathD(zpts)}" fill="${c}" fill-opacity="0.06" stroke="${c}" stroke-width="1"${out ? ' stroke-dasharray="4 3"' : ""} style="cursor:move"/>`;
        svg += `<text x="${zcx.toFixed(1)}" y="${zcy.toFixed(1)}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        for (let vi = 0; vi < zpts.length; vi++) { const a = zpts[vi], b = zpts[(vi + 1) % zpts.length]; svg += `<circle class="fpn-zone-mid" data-zone-idx="${i}" data-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2" fill="none" stroke="${c}" stroke-width="0.6" opacity="0.5" style="cursor:copy"/>`; }
        for (let vi = 0; vi < zpts.length; vi++) { svg += `<circle class="fpn-zone-vtx" data-zone-idx="${i}" data-vtx="${vi}" cx="${zpts[vi][0]}" cy="${zpts[vi][1]}" r="2.8" fill="${c}" stroke="var(--bg)" stroke-width="0.6" style="cursor:grab"/>`; }
        svg += "</g>";
      } else {
        svg += `<g class="fpn-drag-room" data-idx="${i}" style="cursor:move">`;
        svg += `<rect x="${rm.x}" y="${rm.y}" width="${rm.w}" height="${rm.h}" rx="2" fill="${out ? "rgba(143,219,168,0.06)" : "rgba(244,184,96,0.08)"}" stroke="${c}" stroke-width="1" class="fpn-drag-rect"/>`;
        svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        if (rm.w > 30 && rm.h > 24) svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2 + fs + 2.5}" text-anchor="middle" fill="${c}" opacity="0.6" font-size="${(fs * 0.72).toFixed(1)}" font-family="var(--font-mono)" pointer-events="none">${this._fpDim(rm.w)} × ${this._fpDim(rm.h)}</text>`;
        svg += `<rect x="${rm.x + rm.w - 8}" y="${rm.y + rm.h - 8}" width="8" height="8" fill="${c}" opacity="0.35" rx="1" class="fpn-resize-handle" data-idx="${i}" style="cursor:nwse-resize"/>`;
        svg += "</g>";
      }
    }
    for (const lbl of (floorData.labels || [])) {
      svg += `<text x="${lbl.x}" y="${lbl.y}" text-anchor="middle" fill="var(--ink-faint)" font-size="4" font-family="var(--font-mono)">${this._esc(lbl.text)}</text>`;
    }

    // Placed openings as wall markers.
    const els = this._elemsFor(floor) || [];
    if (els.length && floorData.rooms && floorData.rooms.length) {
      const house = this._fpBounds(floorData.rooms.filter(r => r.type !== "outdoor"));
      const mnx = house.x0, mny = house.y0, mxx = house.x1, mxy = house.y1;
      els.forEach((e, i) => {
        if (e.type === "dormer") {
          const dp = e.pos != null ? e.pos : 0.5, dcx = mnx + dp * (mxx - mnx), dcy = e.slope === "rear" ? mxy : mny;
          svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${dcx - 4}" y="${dcy - 3}" width="8" height="6" fill="#b06aff" opacity="0.9" rx="1.5" pointer-events="none"/>`;
          return;
        }
        const w = e.w || 20, p = e.pos != null ? e.pos : 0.5, horiz = (e.wall === "front" || e.wall === "back");
        let bx0 = mnx, by0 = mny, bx1 = mxx, by1 = mxy;
        if ((e.kind === "interior" || e.kind === "cased") && e.room) {
          const rr = floorData.rooms.filter(r => r.name === e.room)[0];
          if (rr) { bx0 = rr.x; by0 = rr.y; bx1 = rr.x + rr.w; by1 = rr.y + rr.h; }
        }
        let cx, cy;
        if (e.wall === "front") { cx = bx0 + p * (bx1 - bx0); cy = by0; }
        else if (e.wall === "back") { cx = bx0 + p * (bx1 - bx0); cy = by1; }
        else if (e.wall === "left") { cx = bx0; cy = by0 + p * (by1 - by0); }
        else { cx = bx1; cy = by0 + p * (by1 - by0); }
        const col = e.type === "window" ? "var(--gold)" : (e.kind === "interior" ? "#5a7a8a" : (e.kind === "cellar" ? "#c98a2a" : (e.kind === "cased" ? "#78b9d7" : "var(--warn)")));
        const ex = horiz ? cx - w / 2 : cx - 2, ey = horiz ? cy - 2 : cy - w / 2, ew = horiz ? w : 4, eh = horiz ? 4 : w;
        svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${ex}" y="${ey}" width="${ew}" height="${eh}" fill="${col}" opacity="0.9" rx="1" pointer-events="none"/>`;
      });
    }

    // Cameras — icon + FOV cone, clipped to walls.
    const cams = this._camsFor(floor);
    const camGeo = cams.length ? this._planGeometry(floor) : null;
    for (let ci = 0; ci < cams.length; ci++) {
      const cam = cams[ci], out = cam.indoor === false;
      svg += `<g class="fpn-cam" data-cam-idx="${ci}">`
        + `<path class="fpn-cam-cone" d="${this._clippedCone(cam, camGeo)}" fill="${out ? "rgba(232,178,61,0.10)" : "rgba(226,84,47,0.10)"}" stroke="${out ? "rgba(232,178,61,0.6)" : "rgba(226,84,47,0.6)"}" stroke-width="0.7" pointer-events="none"/>`
        + `<circle class="fpn-cam-dot" cx="${cam.x}" cy="${cam.y}" r="3.2" fill="${out ? "var(--warn)" : "var(--ember)"}" stroke="var(--ink)" stroke-width="0.7" style="cursor:grab"/>`
        + `<text x="${cam.x}" y="${cam.y - 5}" text-anchor="middle" fill="${out ? "var(--warn)" : "var(--ember)"}" font-size="5" font-family="var(--font-mono)" pointer-events="none">${ci + 1}</text>`
        + "</g>";
    }

    const ents = this._entsFor(floor);
    for (let ei = 0; ei < ents.length; ei++) {
      const ent = ents[ei];
      if (!ent || !ent.e) continue;
      const ms = this._entMarkerStyle(ent.e);
      const nm = ms.name.length > 16 ? (ms.name.slice(0, 15) + "…") : ms.name;
      svg += `<g class="fpn-ent" data-ent-idx="${ei}" data-ent-id="${this._esc(ent.e)}" style="cursor:pointer">`
        + `<circle class="fpn-ent-dot" cx="${ent.x}" cy="${ent.y}" r="3" fill="${ms.color}" stroke="var(--bg)" stroke-width="0.7"/>`
        + `<text class="fpn-ent-nm" x="${ent.x}" y="${ent.y - 4}" text-anchor="middle" fill="${ms.color}" font-size="3.4" font-family="var(--font-mono)" pointer-events="none">${this._esc(nm)}</text>`
        + `<text class="fpn-ent-val" x="${ent.x}" y="${ent.y + 6.5}" text-anchor="middle" fill="var(--ink-dim)" font-size="3" font-family="var(--font-mono)" pointer-events="none">${this._esc(ms.val)}</text>`
        + "</g>";
    }

    svg += "</svg>";
    return svg;
  }

  // Redraws only this card, so an edit in progress elsewhere on the
  // settings page is left alone and the scroll position holds.
  _rerenderFloorPlanCard() {
    const card = this.shadowRoot?.getElementById("settings-card-floor_plan_editor");
    if (!card) return;
    const meta = NovaPanel.SETTINGS_CARDS.find(x => x.id === "floor_plan_editor");
    this._setHtml(card, `
        <div class="panel-head"><div class="panel-title">${this._esc(meta.title)}</div></div>
        ${this._floorPlanEditorCardBody()}`);
    this._wireFloorPlanEditor();
  }

  // A layout file is usable when it has at least one floor and every floor
  // is an object whose rooms, if any, are a list of objects.
  _fpLayoutUsable(layout) {
    const isObj = v => !!v && typeof v === "object" && !Array.isArray(v);
    if (!isObj(layout) || !Object.keys(layout).length) return false;
    return Object.values(layout).every(f => isObj(f)
      && (f.rooms === undefined || (Array.isArray(f.rooms) && f.rooms.every(isObj))));
  }

  // Saves one config key and mirrors it into the live data.
  async _fpSaveKey(key, value, live = value) {
    await this._hass.callWS({ type: "nova/update_config", key, value });
    if (this._liveData?.config) this._liveData.config[key] = live;
  }

  _wireFloorPlanEditor() {
    const root = this.shadowRoot;
    if (!root || !root.getElementById("fpnCanvas")) return;   // the card is not on screen
    const on = (id, type, fn) => { const el = root.getElementById(id); if (el) el.addEventListener(type, fn); return el; };
    const each = (sel, type, fn) => root.querySelectorAll(sel).forEach(el => el.addEventListener(type, () => fn(el)));
    const redraw = () => this._rerenderFloorPlanCard();
    const floorRooms = () => ((this._getEditingPlan()[this._editorFloor] || {}).rooms) || [];
    const idx = (el, attr) => parseInt(el.getAttribute(attr));
    const stamp = () => Date.now().toString(36);
    // The middle of everything on the floor, where new pins and cameras land.
    const middle = () => {
      const rooms = floorRooms();
      if (!rooms.length) return [100, 80];
      const b = this._fpBounds(rooms);
      return [Math.round((b.x0 + b.x1) / 2), Math.round((b.y0 + b.y1) / 2)];
    };

    // Floors, zoom and units.
    each(".fpn-floor-tab", "click", btn => {
      this._editorFloor = btn.getAttribute("data-fpn-floor");
      this._editVB = null;
      redraw();
    });
    on("fpnZoomFit", "click", () => { this._editVB = null; redraw(); });
    on("fpnUnits", "click", async () => {
      try { await this._fpSaveKey("floor_plan_units", this._fpUnits() === "metric" ? "imperial" : "metric"); }
      catch (err) { console.error("Nova: floor plan units save failed", err); }
      redraw();
    });

    // Rooms, outdoor zones and the lot.
    on("fpnAddRoom", "click", () => {
      const level = this._getEditingPlan()[this._editorFloor];
      if (!level) return;
      const name = window.prompt(this._tx("Room name:"));
      if (!name) return;
      const type = window.prompt(this._tx("Type (room, bath, stairs, door):"), "room") || "room";
      (level.rooms = level.rooms || []).push({ name, x: 50, y: 50, w: 60, h: 40, type });
      redraw();
    });
    on("fpnAddZone", "click", () => {
      const level = this._getEditingPlan()[this._editorFloor];
      if (!level) return;
      const name = window.prompt(this._tx("Outdoor zone name (e.g. Front Yard, Driveway, Backyard):"));
      if (!name) return;
      level.rooms = level.rooms || [];
      // A new zone sits just below the house, at least 90 units wide.
      const house = level.rooms.filter(r => r.type !== "outdoor");
      let [x, y, w] = [40, 40, 90];
      if (house.length) {
        const b = this._fpBounds(house);
        x = Math.round(b.x0); y = Math.round(b.y1 + 25); w = Math.round(Math.max(b.x1 - b.x0, 90));
      }
      const h = 70;
      level.rooms.push({ name, x, y, w, h, type: "outdoor", points: [[x, y], [x + w, y], [x + w, y + h], [x, y + h]] });
      redraw();
    });
    on("fpnAddProperty", "click", () => {
      if (this._propertyPts().length >= 3) {
        if (window.confirm(this._tx("Remove the property boundary?"))) { this._setProperty([]); redraw(); }
        return;
      }
      // A rectangle around the floor with a generous margin; a floor with
      // no rooms gets one around a 200 by 150 box near the origin.
      const rooms = floorRooms();
      let b = this._fpBounds(rooms);
      if (!rooms.length || !isFinite(b.x0)) b = { x0: 20, y0: 20, x1: 220, y1: 170 };
      const m = Math.max(150, Math.max(b.x1 - b.x0, b.y1 - b.y0) * 0.7);
      const [l, t, r, btm] = [b.x0 - m, b.y0 - m, b.x1 + m, b.y1 + m].map(Math.round);
      this._setProperty([[l, t], [r, t], [r, btm], [l, btm]]);
      redraw();
    });

    // Windows, doors and dormers.
    const addOpening = item => { this._elemsFor(this._editorFloor).push(item); redraw(); };
    [["opAddWindow", "window", null], ["opAddExtdoor", "door", "exterior"], ["opAddCellar", "door", "cellar"],
      ["opAddIntdoor", "door", "interior"], ["opAddCased", "door", "cased"]].forEach(([id, type, kind]) => on(id, "click",
      () => addOpening({ id: "e" + stamp(), type, kind, wall: "front", pos: 0.5, w: 20, entity: "" })));
    [["opAddFdormer", "front"], ["opAddRdormer", "rear"]].forEach(([id, slope]) => on(id, "click",
      () => addOpening({ id: "e" + stamp(), type: "dormer", slope, pos: 0.5, entity: "" })));
    each(".op-field-new", "change", f => {
      const item = this._elemsFor(this._editorFloor)[idx(f, "data-i")];
      if (!item) return;
      const key = f.getAttribute("data-op");
      if (key === "w") item.w = this._fpFromReal(parseFloat(f.value) || 4);
      else item[key] = key === "pos" ? parseFloat(f.value) : f.value;
      redraw();
    });
    each(".op-del-new", "click", b => { this._elemsFor(this._editorFloor).splice(idx(b, "data-i"), 1); redraw(); });
    // Pointing at an opening's row lights up its marker on the plan.
    root.querySelectorAll(".op-row-new").forEach(row => {
      const marker = () => root.querySelector(`.fpn-op-marker[data-op-marker="${row.getAttribute("data-i")}"]`);
      const glow = lit => () => { const m = marker(); if (m) m.classList.toggle("op-glow", lit); };
      row.addEventListener("mouseenter", glow(true));
      row.addEventListener("mouseleave", glow(false));
      const pick = row.querySelector(".op-ent-new");
      if (pick) { pick.addEventListener("focus", glow(true)); pick.addEventListener("blur", glow(false)); }
    });

    // Cameras.
    on("fpnCamAdd", "click", () => {
      const [x, y] = middle();
      this._camsFor(this._editorFloor).push({ id: "c" + stamp(), x, y, angle: 270, fov: 90, range: 55, entity: "", indoor: true });
      redraw();
    });
    const camOf = el => this._camsFor(this._editorFloor)[idx(el, "data-ci")];
    const applyCamField = f => {
      const cam = camOf(f);
      if (!cam) return null;
      const key = f.getAttribute("data-cam");
      if (key === "range") cam.range = this._fpFromReal(parseFloat(f.value) || 10);
      else cam[key] = key === "angle" || key === "fov" ? parseFloat(f.value) : f.value;
      return cam;
    };
    root.querySelectorAll(".cam-field-new").forEach(f => {
      // While a slider moves only the cone is redrawn; letting go redraws the card.
      f.addEventListener("input", () => {
        const cam = applyCamField(f);
        if (!cam || f.getAttribute("data-cam") === "entity") return;
        const cone = root.querySelector(`.fpn-cam[data-cam-idx="${f.getAttribute("data-ci")}"] .fpn-cam-cone`);
        if (cone) cone.setAttribute("d", this._clippedCone(cam, this._planGeometry(this._editorFloor)));
      });
      f.addEventListener("change", () => { applyCamField(f); redraw(); });
    });
    each(".cam-io-new", "click", b => { const cam = camOf(b); if (!cam) return; cam.indoor = cam.indoor === false; redraw(); });
    each(".cam-del-new", "click", b => { this._camsFor(this._editorFloor).splice(idx(b, "data-ci"), 1); redraw(); });
    const compute = on("fpnCamCompute", "click", async () => {
      const floor = this._editorFloor, cams = this._camsFor(floor);
      if (!cams.length) return;
      compute.disabled = true;
      const geo = this._planGeometry(floor), openings = this._openingDescriptions(floor);
      for (const cam of cams) {
        const candidates = this._computeCoverage(floor, cam, geo);
        const camera = { entity: cam.entity || "", room: this._roomAt(floor, cam.x, cam.y), fov: cam.fov || 90,
          range_ft: this._fpToReal(cam.range || 55), indoor: cam.indoor !== false, candidates, openings };
        try { cam.coverage = await this._hass.callWS({ type: "nova/compute_camera_coverage", camera }); } catch (_) { /* the next camera still runs */ }
      }
      redraw();
    });

    // Save, reset, export and import.
    on("fpnSave", "click", async () => {
      const plan = this._editingPlan, ents = this._editingEntities, cams = this._editingCameras,
        els = this._editingElements, prop = this._editingProperty, hasProp = prop != null;
      if (!plan && !ents && !cams && !els && !hasProp) return;
      const writes = [["floor_plan_rooms", plan], ["floor_plan_entities", ents], ["floor_plan_cameras", cams], ["floor_plan_elements", els]]
        .filter(([, v]) => v);
      if (hasProp) writes.push(["floor_plan_property", { points: prop }]);
      try {
        for (const [key, v] of writes) await this._hass.callWS({ type: "nova/update_config", key, value: JSON.stringify(v) });
        if (this._liveData?.config) writes.forEach(([key, v]) => { this._liveData.config[key] = v; });
        this._editingPlan = this._editingEntities = this._editingCameras = this._editingElements = this._editingProperty = null;
      } catch (err) { console.error("Nova: floor plan save failed", err); }
    });
    on("fpnReset", "click", async () => {
      try { await this._fpSaveKey("floor_plan_rooms", "", {}); }
      catch (err) { console.error("Nova: floor plan reset failed", err); }
      this._editingPlan = null;
      this._editorFloor = null;
      redraw();
    });
    // A layout file is a manual backup, or a way to move a plan to another install.
    on("fpnExport", "click", () => {
      try {
        const link = document.createElement("a");
        const url = URL.createObjectURL(new Blob([JSON.stringify(this._getEditingPlan(), null, 2)], { type: "application/json" }));
        link.href = url;
        link.download = "nova-floor-plan.json";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (err) { console.error("Nova: floor plan export failed", err); }
    });
    const importFile = root.getElementById("fpnImportFile");
    if (importFile && root.getElementById("fpnImport")) {
      on("fpnImport", "click", () => importFile.click());
      importFile.addEventListener("change", () => {
        const file = importFile.files && importFile.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = ev => {
          try {
            const layout = this._fpCleanImport(JSON.parse(ev.target.result));
            if (!this._fpLayoutUsable(layout)) throw new Error("no usable floors");
            this._editingPlan = layout;
            if (!layout[this._editorFloor]) this._editorFloor = Object.keys(layout)[0];
            redraw();
          } catch (err) { console.error("Nova: floor plan import — invalid layout file", err); }
        };
        reader.readAsText(file);
        importFile.value = "";
      });
    }

    // Device pins and the traced-over image.
    on("fpnEntAdd", "click", () => {
      const input = root.getElementById("fpnEntInput");
      const eid = input && input.value.trim();
      if (!eid || !(this._hass && this._hass.states && this._hass.states[eid])) return;
      const pins = this._entsFor(this._editorFloor);
      if (pins.some(p => p.e === eid)) return;
      const [x, y] = middle();
      pins.push({ e: eid, x, y });
      redraw();
    });
    // Camera and opening rows share this button style; only pin chips carry data-ei.
    each(".fpn-ent-del[data-ei]", "click", b => { this._entsFor(this._editorFloor).splice(idx(b, "data-ei"), 1); redraw(); });
    const bgFile = root.getElementById("fpnBgFile");
    if (bgFile && root.getElementById("fpnBgUpload")) {
      on("fpnBgUpload", "click", () => bgFile.click());
      bgFile.addEventListener("change", async () => {
        const file = bgFile.files && bgFile.files[0];
        if (!file) return;
        const floor = this._editorFloor;
        try {
          const image = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => reject(new Error("read failed"));
            reader.readAsDataURL(file);
          });
          const all = this._fpStored("floor_plan_bg") || {};
          all[floor] = image;
          await this._fpSaveKey("floor_plan_bg", JSON.stringify(all));
          redraw();
        } catch (err) {
          console.error("Nova: floor plan background upload failed", err);
        } finally {
          bgFile.value = "";
        }
      });
    }
    const opacity = root.getElementById("fpnBgOp");
    if (opacity) {
      const shown = root.getElementById("fpnBgOpVal");
      opacity.addEventListener("input", () => {
        if (shown) this._setText(shown, Math.round(parseFloat(opacity.value) * 100) + "%");
        const img = root.querySelector("#fpnSvg image");
        if (img) img.setAttribute("opacity", opacity.value);
      });
      opacity.addEventListener("change", async () => {
        try { await this._fpSaveKey("floor_plan_bg_opacity", String(parseFloat(opacity.value))); }
        catch (err) { console.error("Nova: floor plan bg opacity save failed", err); }
      });
    }

    this._wireFloorPlanDrag();
  }

  // Pointer handling on the plan. One listener per event type on the SVG
  // looks at what was pressed; jsdom cannot run the screen maths, so
  // scripts/smoke_panel.js drives this with stand-ins for them.
  _wireFloorPlanDrag() {
    const svgEl = this.shadowRoot.getElementById("fpnSvg");
    if (!svgEl) return;
    const floor = this._editorFloor, level = this._getEditingPlan()[floor];
    if (!level) return;
    const rooms = level.rooms || [];   // an imported floor may have none yet
    let drag = null, pan = null;

    const toPlan = e => {
      const pt = svgEl.createSVGPoint();
      pt.x = e.clientX; pt.y = e.clientY;
      return pt.matrixTransform(svgEl.getScreenCTM().inverse());
    };
    const viewOf = () => {
      const [x, y, w, h] = (svgEl.getAttribute("viewBox") || "0 0 320 150").split(" ").map(Number);
      return { x, y, w, h };
    };
    const showView = () => {
      const v = this._editVB;
      if (!v) return;
      svgEl.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
      svgEl.querySelectorAll(".fpn-grid-rect").forEach(r => {
        r.setAttribute("x", v.x); r.setAttribute("y", v.y); r.setAttribute("width", v.w); r.setAttribute("height", v.h);
      });
    };
    // Quick redraw of the plan alone; the card is redrawn where lists change.
    const redrawPlan = () => {
      const canvas = this.shadowRoot.getElementById("fpnCanvas");
      if (!canvas) return;
      this._setHtml(canvas, this._renderFloorPlanSVG(this._getEditingPlan(), floor));
      setTimeout(() => this._wireFloorPlanDrag(), 10);
    };
    const num = (el, attr) => parseInt(el.getAttribute(attr));
    const place = (el, attrs) => { if (el) Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); };
    const moved = (from, start, now) => Math.round(from + (now - start));
    const midpoint = (pts, edge) => {
      const a = pts[edge], b = pts[(edge + 1) % pts.length];
      if (!a || !b) return false;
      pts.splice(edge + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
      return true;
    };
    const begin = (e, state) => { const pt = toPlan(e); drag = Object.assign({ startX: pt.x, startY: pt.y }, state); };

    // Pressing the left button on an item starts its drag. Return true when
    // the press was used and must go no further than the plan.
    const press = (e, t) => {
      if (t.classList.contains("fpn-drag-rect") && t.closest(".fpn-drag-room")) {
        const i = num(t.closest(".fpn-drag-room"), "data-idx"), rm = rooms[i];
        if (!rm) return false;
        begin(e, { idx: i, origX: rm.x, origY: rm.y, resize: false });
        t.setAttribute("stroke-width", "2.5");
        return false;   // a room drag still lets the press bubble on
      }
      if (t.classList.contains("fpn-resize-handle")) {
        const i = num(t, "data-idx"), rm = rooms[i];
        if (rm) begin(e, { idx: i, origW: rm.w, origH: rm.h, resize: true });
        return true;
      }
      if (t.classList.contains("fpn-zone-path")) {
        const zi = num(t, "data-zone-idx"), rm = rooms[zi];
        if (rm) { this._ensureZonePoints(rm); begin(e, { zoneBody: true, zi, ddx: 0, ddy: 0 }); }
        return true;
      }
      if (t.classList.contains("fpn-zone-vtx")) {
        const zi = num(t, "data-zone-idx"), vi = num(t, "data-vtx"), rm = rooms[zi];
        const p = rm && this._ensureZonePoints(rm)[vi];
        if (p) begin(e, { zoneVtx: true, zi, vi, origX: p[0], origY: p[1] });
        return true;
      }
      if (t.classList.contains("fpn-zone-mid")) {
        const rm = rooms[num(t, "data-zone-idx")];
        if (rm && midpoint(this._ensureZonePoints(rm), num(t, "data-edge"))) this._rerenderFloorPlanCard();
        return true;
      }
      if (t.classList.contains("fpn-prop-vtx")) {
        const pi = num(t, "data-prop-vtx"), p = (this._propertyPts() || [])[pi];
        if (p) begin(e, { propVtx: pi, origX: p[0], origY: p[1], property: true });
        return true;
      }
      if (t.classList.contains("fpn-prop-mid")) {
        if (midpoint(this._propertyPts(), num(t, "data-prop-edge"))) this._rerenderFloorPlanCard();
        return true;
      }
      if (t.classList.contains("fpn-cam-dot") && t.closest(".fpn-cam")) {
        const ci = num(t.closest(".fpn-cam"), "data-cam-idx"), cam = (this._camsFor(floor) || [])[ci];
        if (cam) begin(e, { camIdx: ci, origX: cam.x, origY: cam.y, camera: true, geo: this._planGeometry(floor) });
        return true;
      }
      const pin = t.closest(".fpn-ent");
      if (pin) {
        const ei = num(pin, "data-ent-idx"), ent = (this._entsFor(floor) || [])[ei];
        if (ent) begin(e, { entIdx: ei, origX: ent.x, origY: ent.y, entity: true, moved: false, entId: pin.getAttribute("data-ent-id") });
        return true;
      }
      return false;
    };
    const ITEMS = ".fpn-drag-room, .fpn-resize-handle, .fpn-zone, .fpn-zone-vtx, .fpn-zone-mid, .fpn-zone-path, .fpn-prop-vtx, .fpn-prop-mid, .fpn-cam, .fpn-ent";
    svgEl.addEventListener("mousedown", e => {
      const t = e.target;
      if (e.button === 0 && t !== svgEl && t.closest && svgEl.contains(t)) {
        const before = drag;
        const stop = press(e, t);
        if (stop || drag !== before) {
          e.preventDefault();
          if (stop) e.stopPropagation();
          return;
        }
      }
      if (drag) return;
      // The middle button anywhere, or the left button on empty space, pans.
      const empty = e.button === 0 && !t.closest(ITEMS);
      if (e.button !== 1 && !empty) return;
      e.preventDefault();
      const v = this._editVB || viewOf();
      this._editVB = { x: v.x, y: v.y, w: v.w, h: v.h };
      pan = { sx: e.clientX, sy: e.clientY, vbX: v.x, vbY: v.y, w: v.w, h: v.h };
    });

    // Right-click removes rooms, corners, cameras and pins.
    svgEl.addEventListener("contextmenu", e => {
      const t = e.target;
      const room = t.closest(".fpn-drag-room"), cam = t.closest(".fpn-cam"), pin = t.closest(".fpn-ent");
      if (t.classList.contains("fpn-zone-vtx")) {
        e.preventDefault();
        const rm = rooms[num(t, "data-zone-idx")];
        if (!rm) return;
        const pts = this._ensureZonePoints(rm);
        if (pts.length <= 3) return;
        pts.splice(num(t, "data-vtx"), 1); this._syncRoomBBox(rm); this._rerenderFloorPlanCard();
      } else if (t.classList.contains("fpn-prop-vtx")) {
        e.preventDefault();
        const pts = this._propertyPts();
        if (pts.length <= 3) return;
        pts.splice(num(t, "data-prop-vtx"), 1); this._rerenderFloorPlanCard();
      } else if (room) {
        e.preventDefault();
        const i = num(room, "data-idx"), rm = rooms[i];
        if (rm && window.confirm(this._t("Delete '{name}' from floor plan?", { name: rm.name }))) { rooms.splice(i, 1); redrawPlan(); }
      } else if (cam) {
        e.preventDefault();
        const list = this._camsFor(floor), ci = num(cam, "data-cam-idx");
        if (list[ci] && window.confirm(this._tx("Delete this camera?"))) { list.splice(ci, 1); redrawPlan(); }
      } else if (pin) {
        e.preventDefault();
        const list = this._entsFor(floor), ei = num(pin, "data-ent-idx");
        if (list[ei] && window.confirm(this._tx("Remove this device from the plan?"))) { list.splice(ei, 1); redrawPlan(); }
      }
    });

    // Moving updates the dragged item's own SVG nodes; the model moves with it.
    const follow = pt => {
      const d = drag, dx = pt.x - d.startX, dy = pt.y - d.startY;
      if (d.zoneVtx) {
        const rm = rooms[d.zi], p = rm && rm.points && rm.points[d.vi];
        if (!p) return;
        p[0] = moved(d.origX, d.startX, pt.x); p[1] = moved(d.origY, d.startY, pt.y);
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${d.zi}"]`);
        if (g) {
          place(g.querySelector(".fpn-zone-path"), { d: this._propPathD(rm.points) });
          place(g.querySelector(`.fpn-zone-vtx[data-vtx="${d.vi}"]`), { cx: p[0], cy: p[1] });
        }
      } else if (d.zoneBody) {
        d.ddx = Math.round(dx); d.ddy = Math.round(dy);
        place(svgEl.querySelector(`.fpn-zone[data-zone-idx="${d.zi}"]`), { transform: `translate(${d.ddx},${d.ddy})` });
      } else if (d.property) {
        const p = (this._propertyPts() || [])[d.propVtx];
        if (!p) return;
        p[0] = moved(d.origX, d.startX, pt.x); p[1] = moved(d.origY, d.startY, pt.y);
        place(svgEl.querySelector(`.fpn-prop-vtx[data-prop-vtx="${d.propVtx}"]`), { cx: p[0], cy: p[1] });
        place(svgEl.querySelector(".fpn-prop-path"), { d: this._propPathD(this._propertyPts()) });
      } else if (d.camera) {
        const cam = (this._camsFor(floor) || [])[d.camIdx];
        if (!cam) return;
        cam.x = moved(d.origX, d.startX, pt.x); cam.y = moved(d.origY, d.startY, pt.y);
        const g = svgEl.querySelector(`.fpn-cam[data-cam-idx="${d.camIdx}"]`);
        if (g) {
          place(g.querySelector(".fpn-cam-dot"), { cx: cam.x, cy: cam.y });
          place(g.querySelector(".fpn-cam-cone"), { d: this._clippedCone(cam, d.geo) });
          place(g.querySelector("text"), { x: cam.x, y: cam.y - 5 });
        }
      } else if (d.entity) {
        const ent = (this._entsFor(floor) || [])[d.entIdx];
        if (!ent) return;
        const x = moved(d.origX, d.startX, pt.x), y = moved(d.origY, d.startY, pt.y);
        if (Math.abs(x - d.origX) > 1 || Math.abs(y - d.origY) > 1) d.moved = true;
        ent.x = x; ent.y = y;
        const g = svgEl.querySelector(`.fpn-ent[data-ent-idx="${d.entIdx}"]`);
        if (g) {
          place(g.querySelector(".fpn-ent-dot"), { cx: x, cy: y });
          place(g.querySelector(".fpn-ent-nm"), { x, y: y - 4 });
          place(g.querySelector(".fpn-ent-val"), { x, y: y + 6.5 });
        }
      } else {
        const rm = rooms[d.idx];
        if (!rm) return;
        if (d.resize) {
          rm.w = Math.max(15, Math.round(d.origW + dx)); rm.h = Math.max(10, Math.round(d.origH + dy));
        } else {
          rm.x = moved(d.origX, d.startX, pt.x); rm.y = moved(d.origY, d.startY, pt.y);
        }
        const g = svgEl.querySelector(`.fpn-drag-room[data-idx="${d.idx}"]`);
        if (g) {
          place(g.querySelector(".fpn-drag-rect"), { x: rm.x, y: rm.y, width: rm.w, height: rm.h });
          place(g.querySelector("text"), { x: rm.x + rm.w / 2, y: rm.y + rm.h / 2 });
          place(g.querySelector(".fpn-resize-handle"), { x: rm.x + rm.w - 8, y: rm.y + rm.h - 8 });
        }
      }
    };
    svgEl.addEventListener("mousemove", e => {
      if (pan) {
        const box = svgEl.getBoundingClientRect();
        this._editVB = { x: pan.vbX - (e.clientX - pan.sx) * (pan.w / box.width), y: pan.vbY - (e.clientY - pan.sy) * (pan.h / box.height), w: pan.w, h: pan.h };
        showView();
      } else if (drag) {
        follow(toPlan(e));
      }
    });

    const finish = () => {
      if (pan) { pan = null; return; }
      if (!drag) return;
      const d = drag, zone = rooms[d.zi];
      if (d.zoneBody && zone && zone.points && (d.ddx || d.ddy)) zone.points.forEach(p => { p[0] += d.ddx; p[1] += d.ddy; });
      if ((d.zoneVtx || d.zoneBody) && zone) this._syncRoomBBox(zone);
      // A pin pressed without moving opens its Home Assistant controls.
      if (d.entity && !d.moved && d.entId) {
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: d.entId }, bubbles: true, composed: true }));
      }
      drag = null;
      if (d.camera || d.property || d.zoneVtx || d.zoneBody) this._rerenderFloorPlanCard(); else redrawPlan();
    };
    svgEl.addEventListener("mouseup", finish);
    svgEl.addEventListener("mouseleave", finish);

    // The wheel zooms about the pointer, within fixed limits.
    svgEl.addEventListener("wheel", e => {
      e.preventDefault();
      const v = this._editVB || viewOf(), at = toPlan(e), f = e.deltaY < 0 ? 0.85 : 1.18;
      const w = Math.max(60, Math.min(8000, v.w * f)), h = Math.max(42, Math.min(8000, v.h * f));
      this._editVB = { x: at.x - (at.x - v.x) * (w / v.w), y: at.y - (at.y - v.y) * (h / v.h), w, h };
      showView();
    }, { passive: false });
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
      .topbar{display:flex;align-items:center;justify-content:flex-start;gap:24px;margin-bottom:22px;flex-wrap:wrap;max-width:1100px;margin-inline:auto}
      .brand{display:flex;align-items:center;gap:11px}
      .brand-mark{width:26px;height:26px;border-radius:50%;flex:none;
        background:radial-gradient(circle at 34% 30%, var(--gold-pale), var(--gold) 42%, var(--ember) 78%, #7a2513 100%);
        box-shadow:0 0 14px 1px #e2542f55;}
      .brand-name{font-family:var(--font-display);font-size:18px;font-weight:600}
      .brand-tag{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.1em;text-transform:uppercase}
      .hero{max-width:1100px;margin:0 auto;background:linear-gradient(180deg,var(--surface),#19140fdd);
        border:1px solid var(--line-soft);border-radius:22px;overflow:hidden;container-type:inline-size;
        display:flex;flex-direction:column;align-items:stretch}
      .hero-stage{position:relative;height:clamp(300px,36cqw,440px)}
      .present-card{position:absolute;left:50%;right:5%;top:50%;transform:translateY(-50%);display:none;flex-direction:column;gap:8px;
        padding:14px 18px;border:1px solid var(--line-soft);border-radius:16px;background:var(--surface-2);cursor:pointer;text-align:left}
      .hero-stage.presenting .present-card{display:flex}
      .hero-stage.presenting .hero-copy{display:none}
      .pc-cap{font-family:var(--font-mono);font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-dim)}
      .pc-big{font-family:var(--font-display);font-weight:500;line-height:1}
      .pc-fig{font-size:clamp(44px,8cqw,84px);letter-spacing:-.02em;
        background:linear-gradient(100deg,var(--gold-pale),var(--gold) 55%,var(--ember));-webkit-background-clip:text;background-clip:text;color:transparent}
      .pc-fig::after{content:" " attr(data-unit);font-size:clamp(16px,2.4cqw,22px);letter-spacing:0;-webkit-text-fill-color:var(--gold-pale)}
      .pc-now{font-family:var(--font-mono);font-size:11.5px;letter-spacing:.05em;color:var(--ink-dim)}
      .pc-curve{width:100%;height:28px;display:block}
      .pc-rows{display:flex;flex-direction:column;gap:4px;border-top:1px solid var(--line-soft);padding-top:8px}
      .pc-row{display:flex;justify-content:space-between;gap:12px;font-family:var(--font-mono);font-size:13px;color:var(--ink-dim)}
      .pc-row b{font-weight:500;color:var(--gold-pale)}
      canvas.core{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;touch-action:pan-y}
      canvas.core:active{cursor:grabbing}
      .hero-copy{position:absolute;left:50%;right:5%;top:50%;transform:translateY(-50%);pointer-events:none;
        display:flex;flex-direction:column;gap:8px;text-align:left}
      .hero-word{font-family:var(--font-display);font-weight:500;font-size:clamp(44px,8cqw,100px);line-height:1.05;letter-spacing:-.02em;min-height:1.05em;
        background:linear-gradient(100deg,var(--gold-pale),var(--gold) 55%,var(--ember));-webkit-background-clip:text;background-clip:text;color:transparent;transition:filter .6s}
      .hero-word.dim{filter:brightness(.62)}
      .hero-caret{display:inline-block;width:.06em;height:.8em;margin-left:.06em;background:var(--gold);vertical-align:-.04em;animation:hero-caret 1s steps(1) infinite}
      @keyframes hero-caret{50%{opacity:0}}
      .state-line{font-family:var(--font-display);font-size:clamp(17px,2cqw,21px);font-weight:500;margin:0;text-wrap:balance}
      .state-sub{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em}
      @container (max-width:639px){
        .hero-stage{height:auto;display:flex;flex-direction:column}
        canvas.core{position:relative;height:300px}
        .present-card{position:relative;left:auto;right:auto;top:auto;transform:none;margin:0 16px 20px;align-items:center;text-align:center}
        .pc-row{width:100%}
        .hero-copy{position:relative;left:auto;right:auto;top:auto;transform:none;align-items:center;text-align:center;padding:0 16px 20px}
      }
      @media (prefers-reduced-motion: reduce){.hero-caret{animation:none}}
      .chips{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;padding:16px 20px 20px;border-top:1px solid var(--line-soft);width:100%}
      .chip{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;background:var(--surface-2);
        font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);border:1px solid var(--line-soft)}
      .chip .dot{width:6px;height:6px;border-radius:50%;background:#6fbf8a}
      .chip.warn .dot{background:var(--warn)}
      .chip b{color:var(--ink);font-weight:600}
      .grid{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr;gap:16px}
      .panel{background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:16px 16px 14px}
      .panel-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
      .panel-title{font-family:var(--font-display);font-size:15px;font-weight:600}
      .panel-meta{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
      .lockdown-control{margin-left:auto;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:.06em;
        padding:8px 12px;border-radius:9px;border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-faint);cursor:pointer}
      .lockdown-control.active{color:#ffd7d7;background:#7d2028;border-color:#d95b65;box-shadow:0 0 16px #d95b6533}
      .onboarding-card{max-width:1100px;margin:0 auto 16px;background:linear-gradient(135deg,#f4b86012,var(--surface));border:1px solid #f4b86066;border-radius:16px;padding:16px}
      .onboarding-progress{position:relative;height:20px;background:var(--surface-2);border-radius:8px;overflow:hidden;margin:12px 0}
      .onboarding-progress i{position:absolute;inset:0 auto 0 0;background:#f4b86033}.onboarding-progress span{position:relative;z-index:1;display:block;padding:4px 8px;font-family:var(--font-mono);font-size:9px}
      .onboarding-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:7px;margin-bottom:10px}
      .onboarding-step{display:grid;grid-template-columns:18px 1fr auto;align-items:center;gap:7px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:8px;color:var(--ink-dim)}
      .onboarding-step.done{opacity:.62}.onboarding-step b{display:block;font-size:11px}.onboarding-step small{display:block;font-size:9px;color:var(--ink-faint);margin-top:2px}
      .welcome-checks,.welcome-hello{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:8px;margin-bottom:10px;color:var(--ink-dim)}
      .welcome-checks b{display:block;font-size:11px}.welcome-checks small,.welcome-hello small{display:block;font-size:9px;color:var(--ink-faint);margin-top:2px}
      .welcome-hello{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:10px}.welcome-hello .welcome-reply{color:var(--ink-dim);font-size:11px}.welcome-hello .welcome-error{color:#d95b65}
      .dashboard-pair{max-width:1100px;margin:16px auto 0;display:grid;grid-template-columns:1fr 1fr;gap:16px}
      @media (max-width:760px){.dashboard-pair{grid-template-columns:1fr}}
      .metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
      .metric{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px;display:flex;flex-direction:column;gap:2px}
      .metric b{font-family:var(--font-mono);font-size:14px}.metric span{font-size:10px;color:var(--ink-faint)}
      .energy-live{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:0}
      .energy-live[hidden]{display:none}
      @media (max-width:760px){.energy-live{grid-template-columns:repeat(2,1fr)}}
      .energy-tile{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px;display:flex;flex-direction:column;gap:2px;min-width:0}
      .energy-tile dt{font-size:10px;color:var(--ink-faint)}
      .energy-tile dd{margin:0}
      .energy-tile .energy-tile-w{font-family:var(--font-mono);font-size:16px}
      .energy-tile .energy-tile-state{font-size:11px;color:var(--ink-dim);min-height:1em}
      .energy-flow-wrap{margin:0 0 10px;display:flex;justify-content:center}
      .energy-flow,#energyOutlookPanel{--c-solar:var(--gold);--c-grid:#6ea8ff;--c-house:var(--ember);--c-battery:#2aa198}
      .energy-flow{display:block;width:100%;height:auto;max-height:clamp(220px,38vh,320px);margin:0 auto}
      .energy-flow .flow[data-flow="solar"]{--flow-c:var(--c-solar)}
      .energy-flow .flow[data-flow="grid"]{--flow-c:var(--c-grid)}
      .energy-flow .flow[data-flow="battery"]{--flow-c:var(--c-battery)}
      .energy-flow .flow-glow{fill:none;stroke:var(--flow-c);stroke-width:calc(var(--flow-w,2) * 3);stroke-linecap:round;opacity:.12}
      .energy-flow .flow-line{fill:none;stroke:var(--flow-c);stroke-width:var(--flow-w,2);stroke-linecap:round;stroke-dasharray:1 12;
        animation:nova-flow var(--flow-dur,6s) linear infinite}
      .energy-flow .flow[data-dir="out"] .flow-line{animation-direction:reverse}
      .energy-flow .flow-arrow{fill:var(--flow-c)}
      .energy-flow .flow[data-dir="out"] .flow-arrow{transform:rotate(180deg)}
      .energy-flow .flow[data-state="idle"] .flow-line{animation:none;opacity:.25}
      .energy-flow .flow[data-state="idle"] .flow-glow{opacity:.05}
      .energy-flow .flow[data-state="idle"] .flow-arrow{visibility:hidden}
      @keyframes nova-flow{to{stroke-dashoffset:-26}}
      .energy-flow .node-ring{fill:var(--surface-2);stroke-width:2.5}
      .energy-flow .node-icon{fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
      .energy-flow .flow-node[data-node="solar"] .node-ring,.energy-flow .flow-node[data-node="solar"] .node-icon{stroke:var(--c-solar)}
      .energy-flow .flow-node[data-node="grid"] .node-ring,.energy-flow .flow-node[data-node="grid"] .node-icon{stroke:var(--c-grid)}
      .energy-flow .flow-node[data-node="house"] .node-ring,.energy-flow .flow-node[data-node="house"] .node-icon{stroke:var(--c-house)}
      .energy-flow .flow-node[data-node="battery"] .node-ring,.energy-flow .flow-node[data-node="battery"] .node-icon{stroke:var(--c-battery)}
      .energy-flow .flow-label{font-family:var(--font-body);font-size:20px;fill:var(--ink-dim)}
      .energy-flow .flow-value{font-family:var(--font-mono);font-size:26px;fill:var(--ink)}
      .energy-flow .flow-state{font-family:var(--font-body);font-size:20px;fill:var(--ink-dim)}
      .energy-flow .battery-track{fill:none;stroke:var(--line-soft);stroke-width:4}
      .energy-flow .battery-arc{fill:none;stroke:var(--c-battery);stroke-width:4;stroke-linecap:round;
        stroke-dasharray:var(--batt-pct,0) 100;transition:stroke-dasharray .6s ease}
      .energy-flow .battery-arc[data-pct="none"]{opacity:0}
      .energy-flow-summary{margin:0 0 10px}
      @media (min-width:900px){
        .energy-live-grid{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;align-items:center}
        .energy-live-grid:has(> .energy-live-main > .energy-flow-wrap[hidden]){display:block}
        .energy-live-side .energy-live{grid-template-columns:repeat(2,1fr)}
        .energy-live-main .energy-flow-wrap{margin:0}
      }
      .energy-flow-summary[hidden],.energy-flow-wrap[hidden]{display:none}
      #energyTodayPanel[hidden],#energyBatteryPanel[hidden],.energy-today[hidden],.battery-line[hidden]{display:none}
      .energy-today{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
      .energy-battery{display:flex;align-items:center;gap:18px}
      .battery-tank{width:72px;height:auto;flex:none}
      .battery-tank .tank-outline{fill:var(--surface-2);stroke:#2aa198;stroke-width:3}
      .battery-tank .tank-cap{fill:#2aa198}
      .battery-tank .tank-fill{fill:#2aa198;opacity:.85;transform-box:fill-box;transform-origin:50% 100%;
        transform:scaleY(calc(var(--tank-pct,0) / 100));transition:transform .6s ease}
      .battery-copy{display:flex;flex-direction:column;gap:4px;min-width:0}
      .battery-pct{font-family:var(--font-mono);font-size:26px;color:var(--ink)}
      .battery-state{font-size:13px;color:var(--ink-dim)}
      .battery-line{font-size:12px;color:var(--ink-dim)}
      #energyOutlookPanel[hidden],.outlook-strip-wrap[hidden],.outlook-advice[hidden],.outlook-line[hidden],.outlook-learned[hidden]{display:none}
      .outlook-key{display:flex;gap:14px;font-size:11px;color:var(--ink-dim);margin-bottom:4px}
      .outlook-key span::before{content:"";display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
      .outlook-key .key-sun::before{background:var(--c-solar)}.outlook-key .key-soc::before{background:var(--c-battery)}.outlook-key .key-price::before{background:var(--c-grid);height:8px;opacity:.6}
      .outlook-chart{position:relative}
      .outlook-strip{display:block;width:100%;height:116px}
      .outlook-strip .outlook-band{fill:var(--c-grid);opacity:.18}
      .outlook-strip .outlook-band[data-label="mid"]{opacity:.4}
      .outlook-strip .outlook-band[data-label="high"]{opacity:.6}
      .outlook-strip .outlook-solar{fill:var(--c-solar);fill-opacity:.2;stroke:var(--c-solar);stroke-opacity:.7;stroke-width:1.5;vector-effect:non-scaling-stroke}
      .outlook-strip .outlook-soc{fill:none;stroke:var(--c-battery);stroke-width:2.5;stroke-linejoin:round;vector-effect:non-scaling-stroke}
      .outlook-strip .outlook-now{stroke:var(--ink-dim);stroke-width:1;stroke-dasharray:3 4;vector-effect:non-scaling-stroke}
      .outlook-band-labels{position:absolute;left:0;right:0;bottom:0;height:26%;pointer-events:none}
      .outlook-band-label{position:absolute;top:0;bottom:0;display:flex;align-items:center;justify-content:center;gap:4px;overflow:hidden;white-space:nowrap;font-size:10.5px;color:var(--ink)}
      .outlook-band-label b{font-family:var(--font-mono);font-weight:500;color:var(--ink)}
      .outlook-ticks{position:relative;height:16px;font-family:var(--font-mono);font-size:10px;color:var(--ink-faint)}
      .outlook-ticks span{position:absolute;top:2px;transform:translateX(-50%);white-space:nowrap}
      .outlook-ticks .tick-start{transform:none}
      .outlook-ticks .tick-now{top:auto;bottom:-14px;color:var(--ink-dim)}
      .outlook-rates{display:grid;grid-template-columns:1fr;gap:3px 16px;margin:20px 0 0;font-size:11px}
      .outlook-rate{display:flex;gap:8px;min-width:0}
      .outlook-rate dt{font-family:var(--font-mono);color:var(--ink);white-space:nowrap}
      .outlook-rate dd{margin:0;color:var(--ink-dim);min-width:0}
      .outlook-rate .rate-swatch{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:-1px;background:var(--c-grid);opacity:.18}
      .outlook-rate[data-label="mid"] .rate-swatch{opacity:.4}.outlook-rate[data-label="high"] .rate-swatch{opacity:.6}
      @media (min-width:600px){.outlook-rates{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media (max-width:599px){.outlook-band-labels{display:none}}
      .outlook-advice{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
      .outlook-item{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px 10px}
      .outlook-item-title{font-size:13px;color:var(--ink);margin-bottom:2px}
      .outlook-saving{font-family:var(--font-mono);font-size:11px;color:var(--gold-pale);margin-top:3px}
      .outlook-line{font-size:12.5px;color:var(--ink-dim);margin-top:10px}
      .outlook-learned{font-size:11px;color:var(--ink-faint);margin-top:6px}
      @media (prefers-reduced-motion: reduce){.energy-flow .flow-line{animation:none}.energy-flow .battery-arc{transition:none}.battery-tank .tank-fill{transition:none}}
      .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
      .goal-list{display:flex;flex-direction:column;gap:7px;max-height:300px;overflow:auto}
      .goal-row{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:9px;padding:9px}
      .goal-copy{min-width:0;display:flex;flex-direction:column;gap:2px}.goal-copy b{font-size:12px}.goal-copy span{font-size:11px;color:var(--ink-dim);overflow-wrap:anywhere}
      .goal-copy small{font-family:var(--font-mono);font-size:9px;color:var(--ink-faint)}
      .goal-create{display:flex;gap:8px;margin-top:10px}.goal-create .cfg-field{flex:1;min-width:0}.empty-state{font-size:12px;color:var(--ink-faint);padding:12px 0}
      .feed{max-height:420px;overflow-y:auto}
      .feed::-webkit-scrollbar{width:3px}
      .feed::-webkit-scrollbar-track{background:var(--surface-2)}
      .feed::-webkit-scrollbar-thumb{background:var(--line-soft);border-radius:3px}
      .feed-row{padding:9px 0;border-bottom:1px solid var(--line-soft);display:flex;justify-content:space-between;gap:10px}
      .feed-row:last-child{border-bottom:none}
      .feed-text{font-size:12.8px;line-height:1.4}
      .feed-text .dim{color:var(--ink-dim)}
      .feed-time{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);white-space:nowrap}
      .areas-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}
      @media (max-width:1100px){.areas-grid{grid-template-columns:repeat(4,1fr)}}
      @media (max-width:560px){.areas-grid{grid-template-columns:repeat(2,1fr)}}
      .area-tile{position:relative;background:var(--surface-2);border:1px solid var(--line-soft);border-radius:13px;
        padding:12px 12px 10px;overflow:hidden;transition:border-color .25s,box-shadow .25s}
      .area-tile::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
        background:linear-gradient(90deg,#6ea8ff,var(--gold) 55%,var(--ember));opacity:.55}
      .area-tile.no-temp::before{display:none}
      .area-tile.active{border-color:#e2542f70;box-shadow:inset 0 0 14px #e2542f14,0 0 14px #e2542f12}
      .area-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
      .area-name{font-family:var(--font-display);font-size:14.5px;font-weight:600;display:flex;align-items:center;gap:6px}
      .live-dot{width:6px;height:6px;border-radius:50%;background:#5fbf7a;box-shadow:0 0 6px 1px #5fbf7a99;
        animation:novaLivePulse 2.4s ease-in-out infinite;flex:none}
      @keyframes novaLivePulse{0%,100%{opacity:1}50%{opacity:.45}}
      @media (prefers-reduced-motion: reduce){.live-dot{animation:none}}
      .area-caps{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:9px;min-height:20px}
      .area-cap{width:21px;height:21px;border-radius:6px;background:var(--surface);border:1px solid var(--line-soft);
        display:flex;align-items:center;justify-content:center;font-size:10.5px;opacity:.85}
      .area-climate{display:flex;gap:10px;margin-bottom:9px}
      .area-climate-item{flex:1;min-width:0}
      .area-climate-num{font-family:var(--font-mono);font-size:12.5px;font-weight:500;display:flex;align-items:baseline;gap:3px}
      .area-climate-num .unit{font-size:9px;color:var(--ink-faint)}
      .area-climate svg{display:block;width:100%;height:16px;margin-top:2px}
      .area-bottom{display:flex;align-items:center;justify-content:space-between;padding-top:8px;border-top:1px solid var(--line-soft)}
      .area-stat{font-family:var(--font-mono);font-size:10px;color:var(--ink-dim)}
      .area-stat b{color:var(--ink);font-weight:600}
      .area-light-toggle{font-family:var(--font-mono);font-size:9px;font-weight:600;letter-spacing:.05em;
        padding:3px 9px;border-radius:7px;border:1px solid var(--line-soft);background:var(--surface);
        color:var(--ink-faint);cursor:pointer}
      .area-light-toggle.on{background:#f4b8602a;border-color:#f4b86070;color:var(--gold-pale)}
      .camera-panel{grid-column:1/-1}
      .camera-head-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
      .camera-note{font-size:11.5px;color:var(--ink-dim);max-width:46ch}
      .camera-toggle{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);background:var(--surface-2);
        border:1px solid var(--line-soft);border-radius:8px;padding:6px 10px;cursor:pointer}
      .camera-strip{display:none;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-top:12px}
      .camera-strip.open{display:grid}
      .camera-slot{border-radius:10px;background:var(--surface-2);border:1px solid var(--line-soft);overflow:hidden;padding-bottom:9px}
      .camera-slot img,.camera-empty{width:100%;aspect-ratio:16/9;object-fit:cover;display:flex;align-items:center;justify-content:center;background:#080706;color:var(--ink-faint);font-family:var(--font-mono);font-size:10px}
      .camera-caption{padding:8px 9px 4px;display:flex;flex-direction:column;gap:2px}.camera-caption b{font-size:12px}.camera-caption span{font-family:var(--font-mono);font-size:9px;color:var(--ink-faint)}
      .camera-actions{display:flex;flex-wrap:wrap;gap:5px;padding:4px 9px}.camera-actions .mode-chip{padding:4px 7px;font-size:9px}
      .camera-diagnostic{font-size:10px;color:var(--ink-dim);line-height:1.35;padding:5px 9px 0;overflow-wrap:anywhere}
      .footnote{max-width:1100px;margin:20px auto 0;text-align:center;font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);letter-spacing:.05em}
      .new-log-entries{max-height:65vh;overflow-y:auto;display:flex;flex-direction:column;gap:1px;margin-top:8px}
      .intr-snap{margin-bottom:10px}
      .intr-img{width:100%;max-width:320px;border-radius:10px;border:1px solid var(--line-soft);display:block;margin-bottom:6px}
      .new-ilog-item{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-ilog-item:first-of-type{border-top:none}
      .new-log-entry{display:grid;grid-template-columns:70px 110px 1fr;gap:10px;padding:7px 8px;
        font-family:var(--font-mono);font-size:11px;border-bottom:1px solid var(--line-soft);align-items:baseline}
      .new-log-entry-error{background:#ff5a5a14}
      .new-log-ts{color:var(--ink-faint)}
      .new-log-cat{white-space:nowrap;font-weight:600}
      .new-log-msg{color:var(--ink-dim);word-break:break-word}
      @media (max-width:560px){.new-log-entry{grid-template-columns:1fr;gap:2px}}
      .new-decision-row{grid-template-columns:150px minmax(150px,230px) minmax(0,1fr) auto;align-items:start}
      .new-decision-row .new-log-cat{white-space:normal;overflow-wrap:anywhere}
      @media (max-width:820px){
        .new-decision-row{grid-template-columns:minmax(0,1fr) auto;gap:2px 10px}
        .new-decision-row .new-log-ts{grid-column:1 / -1;grid-row:1}
        .new-decision-row .new-log-cat{grid-column:1;grid-row:2}
        .new-decision-row > span:last-child{grid-column:2;grid-row:2;justify-self:end}
        .new-decision-row .new-log-msg{grid-column:1 / -1;grid-row:3}
      }

      /* Top nav (v7.94.0) */
      .top-nav{display:flex;flex-wrap:wrap;gap:4px;background:var(--surface);border:1px solid var(--line-soft);border-radius:11px;padding:4px}
      .nav-tab{font-family:var(--font-body);font-size:12.5px;font-weight:600;padding:7px 14px;border-radius:8px;
        border:none;background:transparent;color:var(--ink-dim);cursor:pointer}
      .nav-tab.active{background:var(--ember);color:#1e0d06}
      .nav-tab:not(.active):hover{color:var(--ink)}

      /* Settings (v7.94.0) */
      .settings-toolbar{max-width:1100px;margin:0 auto 16px;display:flex;flex-direction:column;gap:10px}
      .settings-search{width:100%;background:var(--surface);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:13px;padding:10px 14px;border-radius:10px}
      .settings-search::placeholder{color:var(--ink-faint)}
      .settings-nav{display:flex;flex-wrap:wrap;gap:6px}
      .settings-nav-btn{font-family:var(--font-body);font-size:11.5px;font-weight:600;padding:6px 12px;border-radius:20px;
        border:1px solid var(--line-soft);background:var(--surface);color:var(--ink-dim);cursor:pointer}
      .settings-nav-btn.active{background:var(--ember);border-color:var(--ember);color:#1e0d06}
      .settings-grid{max-width:1100px;margin:0 auto;column-count:2;column-gap:14px}
      @media (max-width:720px){.settings-grid{column-count:1}}
      .settings-card{break-inside:avoid;margin-bottom:14px;display:inline-block;width:100%}
      .settings-card[hidden]{display:none}
      /* The Cameras group is only two cards, so lay it out as a plain two
         column grid. Column breaks inside CSS columns are not supported in
         Safari, so Doorbell Training could not be pushed beside Cameras
         that way. */
      @media (min-width:721px){
        .settings-grid[data-section="cameras"]{column-count:auto;display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
        .settings-grid[data-section="cameras"] .settings-card{margin-bottom:0}
      }
      .new-cam-collapse{background:none;border:none;padding:0;cursor:pointer;font:inherit;font-size:12.5px;color:var(--ink-dim);text-align:left}
      .new-cam-collapse:hover{color:var(--ink)}
      .new-cam-caret{display:inline-block;width:1em}
      .stub-tag{font-family:var(--font-mono);font-size:9px;letter-spacing:.08em;color:var(--ink-faint);
        background:var(--surface-2);border:1px solid var(--line-soft);border-radius:20px;padding:2px 8px;margin-left:8px;vertical-align:middle}
      .stub-body{font-size:12.5px;color:var(--ink-dim);line-height:1.5}
      .stub-where{display:block;margin-top:6px;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint)}
      .cfg-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
      .cfg-row-wrap{flex-wrap:wrap;justify-content:flex-start}
      .cfg-row label{font-size:12.5px;color:var(--ink-dim)}
      select.cfg-field,input.cfg-field{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px}
      input.cfg-field:hover,input.cfg-field:focus,select.cfg-field:hover,select.cfg-field:focus{border-color:var(--gold);outline:none}
      .cfg-num{width:84px;min-width:0;text-align:right}
      .door-map-sel-new{flex:1;min-width:0;max-width:220px}
      textarea.cfg-field.vp-json{flex:1;min-width:0;background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-mono);font-size:12px;padding:6px 9px;border-radius:8px;resize:vertical}
      textarea.cfg-field.vp-json:focus{border-color:var(--gold);outline:none}
      .vp-row{flex-wrap:wrap}.vp-row .toggle-desc{flex:1;min-width:0;overflow-wrap:anywhere}
      .vp-editor select.cfg-field{flex:1;min-width:0;max-width:100%}
      .vp-note{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:-4px 0 8px}
      .vid-ok{color:var(--gold)}.vid-bad{color:var(--warn)}
      .vid-token-row{display:flex;gap:6px;align-items:center;min-width:0}.vid-token{flex:1;min-width:0}
      .vid-person{flex-wrap:wrap}.vid-actions{display:flex;flex-wrap:wrap;gap:6px}
      .vid-progress{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
      .vid-danger{color:var(--warn)}
      .vp-link{background:none;border:none;padding:0;color:var(--gold);font:inherit;cursor:pointer;text-decoration:underline}
      .mode-grid{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
      .mode-chip{font-family:var(--font-mono);font-size:11px;text-transform:uppercase;letter-spacing:.04em;
        padding:6px 12px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim);cursor:pointer}
      .mode-chip:hover{border-color:var(--gold)}
      .mode-chip-on{background:var(--ember);border-color:var(--ember);color:var(--gold-pale)}
      .mode-bindings>summary{cursor:pointer;list-style:none}
      .mode-bindings>summary::-webkit-details-marker{display:none}
      .mode-bindings>summary::before{content:"▸ ";color:var(--ink-faint)}
      .mode-bindings[open]>summary::before{content:"▾ "}
      .mode-bind-head{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em;
        text-transform:uppercase;margin:12px 0 8px;padding-top:12px;border-top:1px solid var(--line-soft)}
      .diag-ok{color:#5fbf7a} .diag-warn{color:var(--warn)} .diag-idle{color:var(--ink-dim)}
      .diag-down{color:#ff6b81} .diag-off{color:var(--ink-faint)}
      .new-model-list{display:flex;flex-direction:column;gap:10px}
      .new-model-row{display:grid;grid-template-columns:88px 1fr 1.3fr auto;gap:6px;align-items:center}
      .model-label{font-family:var(--font-mono);font-size:10px;letter-spacing:.1em;color:var(--ink-faint);text-transform:uppercase}
      .new-model-row .new-prov-select,.new-model-row .new-model-select{width:100%;min-width:0;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:11.5px;padding:6px 8px;border-radius:8px}
      .new-model-custom{grid-column:2/4;width:100%;box-sizing:border-box;padding:6px 9px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--gold);
        font-family:var(--font-mono);font-size:11px;border-radius:8px}
      .new-model-custom:focus{outline:none;border-color:var(--gold)}
      .new-model-refresh{padding:5px 9px;font-size:12px;line-height:1}
      .new-model-warning{grid-column:1/-1;font-size:10.5px;color:var(--warn);margin-top:2px}
      .new-model-row .stub-body{grid-column:1/-1;font-size:10.5px;margin-top:2px}
      .cred-row{display:grid;grid-template-columns:88px 74px 1fr auto auto;gap:6px;align-items:center}
      .cred-status{font-family:var(--font-mono);font-size:10px;color:var(--ink-faint);text-transform:uppercase}
      .cred-status.cred-configured{color:var(--gold)}
      .cred-input{width:100%;min-width:0;box-sizing:border-box;padding:6px 8px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:11.5px;border-radius:8px}
      .cred-input:focus{outline:none;border-color:var(--gold)}
      .new-appliance-list{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
      .new-appliance-row{display:grid;grid-template-columns:1.1fr .9fr 1.3fr 64px 28px;gap:6px;align-items:center}
      .new-appliance-row input,.new-appliance-row select{background:var(--surface-2);border:1px solid var(--line-soft);
        color:var(--ink);font-family:var(--font-body);font-size:11px;padding:5px 7px;border-radius:7px;min-width:0;width:100%;box-sizing:border-box}
      .new-appliance-row input:focus,.new-appliance-row select:focus{outline:none;border-color:var(--gold)}
      .new-appliance-remove{flex:none;width:26px;height:26px;padding:0;font-size:11px;color:#ff8a8a;
        border:1px solid #ff5a5a4d;background:transparent;border-radius:7px;cursor:pointer}
      .new-appliance-remove:hover{border-color:#ff5a5a;background:#ff5a5a14}
      .new-pl-chip{display:inline-flex;align-items:center;gap:5px;font-family:var(--font-mono);font-size:10.5px;
        padding:5px 8px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim)}
      .new-pl-del,.new-excl-ent-del,.new-excl-dom-del,.new-excl-lab-del,.new-dep-cal-del,.new-mem-forget{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:12px;padding:0}
      .new-pl-del:hover,.new-excl-ent-del:hover,.new-excl-dom-del:hover,.new-excl-lab-del:hover,.new-dep-cal-del:hover,.new-mem-forget:hover{color:#ff5a5a}
      .new-camset-row{padding:10px 0;border-top:1px solid var(--line-soft)}
      .new-camset-row:first-of-type{border-top:none}
      .toggle-list{display:flex;flex-direction:column;gap:2px}
      .toggle-row{display:grid;grid-template-columns:1fr auto;grid-template-rows:auto auto;gap:2px 10px;
        padding:9px 0;border-top:1px solid var(--line-soft)}
      .toggle-row:first-child{border-top:none}
      .toggle-label{font-size:12.5px;font-weight:600;grid-column:1;grid-row:1}
      .toggle-desc{font-size:11px;color:var(--ink-faint);grid-column:1;grid-row:2}
      .toggle-btn{grid-column:2;grid-row:1/3;align-self:center;font-family:var(--font-mono);font-size:10.5px;font-weight:600;
        letter-spacing:.05em;padding:6px 12px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);
        color:var(--ink-faint);cursor:pointer;min-width:44px}
      .toggle-btn.on{background:#5fbf7a2a;border-color:#5fbf7a70;color:#8fdba8}
      .toggle-row select.cfg-field{grid-column:2;grid-row:1/3;align-self:center}
      .pairing-list{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
      .pairing-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
      .pairing-label{font-size:12.5px;color:var(--ink-dim)}
      .pairing-row select{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px;max-width:56%}
      .person-honorific-row{flex-wrap:wrap}
      .person-honorific-custom{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px;width:100%;margin-top:6px}
      .fpn-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
      .fpn-floor-tabs,.fpn-actions{display:flex;gap:6px;flex-wrap:wrap}
      .fpn-hint{font-size:10px;color:var(--ink-faint);font-family:var(--font-mono);letter-spacing:0.04em;margin-bottom:6px}
      .fpn-canvas{min-height:520px;margin-bottom:10px}
      .fpn-actions{margin-top:4px}
      .fpn-op-marker.op-glow{opacity:1;stroke:var(--ink);stroke-width:2.5;filter:drop-shadow(0 0 6px var(--gold-pale))}
      /* Openings/cameras rows pack more controls than a plain cfg-row (chip,
         wall/room select, slider, size, entity select, delete). flex-wrap
         alone isn't enough: a native <input type=range>/<select> has no
         intrinsic width limit, so two of them can already be wider than a
         settings-card's column before wrapping even has a reason to kick
         in -- the settings-grid uses CSS columns, which don't clip
         horizontal overflow, so a too-wide row bleeds into the next card
         over instead of being clipped. Give every control in these rows an
         explicit cap so the row actually has narrow enough pieces to wrap. */
      .op-row-new,.cam-row-new{flex-wrap:wrap;row-gap:6px;max-width:100%}
      .op-row-new select,.cam-row-new select{flex:0 1 auto;max-width:110px}
      .op-row-new input[type="range"],.cam-row-new input[type="range"]{flex:0 0 auto;width:70px}
      .op-row-new input[type="number"],.cam-row-new input[type="number"]{flex:0 0 auto;width:44px}
      .chat-log{max-height:55vh;overflow-y:auto;margin-bottom:10px}
      .chat-turn{padding:8px 0;border-bottom:1px solid var(--line-soft);display:flex;gap:10px;font-size:13px;line-height:1.5}
      .chat-turn:last-child{border-bottom:none}
      .chat-who{flex:0 0 52px;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);padding-top:2px}
      .chat-turn.nova .chat-who{color:var(--gold)}
      .chat-text{flex:1;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere}
      .chat-error .chat-text{color:var(--warn)}
      .chat-compose{display:flex;gap:8px}.chat-compose .cfg-field{flex:1;min-width:0}
      .fpn-inline-lbl{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--ink-dim)}
        margin-bottom:10px;cursor:grab;touch-action:none;display:flex;align-items:center;justify-content:center;overflow:hidden}
    `;
  }
}

if (!customElements.get("nova-panel")) {
  customElements.define("nova-panel", NovaPanel);
}
