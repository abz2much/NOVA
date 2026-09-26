  // ─── Render (structure once, patch data after) ──────────────────────────

  _render() {
    // Tearing down and rebuilding the DOM (now happening on every tab
    // switch, not just once) orphans the previous canvas — its
    // requestAnimationFrame loop and resize listener would otherwise keep
    // running forever on a detached, invisible canvas, compounding every
    // time someone switches tabs. Stop it before _initCore() starts a
    // fresh one.
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (this._resizeListener) { window.removeEventListener("resize", this._resizeListener); this._resizeListener = null; }
    this._ctx = null;
    this._canvas = null;

    const root = this.shadowRoot;
    root.innerHTML = this._html();
    this._localizeDOM(root);
    this._renderedOnce = true;
    this._wire();
    this._initCore();
    this._renderData();
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

  _localizeDOM(root) {
    const dict = this._uiStrings;
    if (!dict || !root) return;
    try {
      const walker = document.createTreeWalker(root, 4, null);
      const swaps = [];
      let node;
      while ((node = walker.nextNode())) {
        const raw = node.nodeValue;
        if (!raw) continue;
        const key = raw.trim();
        if (key && Object.prototype.hasOwnProperty.call(dict, key)) {
          swaps.push([node, raw.replace(key, dict[key])]);
        }
      }
      swaps.forEach(([textNode, value]) => { textNode.nodeValue = value; });
      root.querySelectorAll("[title],[placeholder]").forEach(el => {
        ["title", "placeholder"].forEach(attr => {
          const raw = el.getAttribute(attr);
          const key = raw?.trim();
          if (key && Object.prototype.hasOwnProperty.call(dict, key)) {
            el.setAttribute(attr, raw.replace(key, dict[key]));
          }
        });
      });
    } catch (_) { /* English DOM remains usable if localization fails. */ }
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
              <div class="brand-tag">${tab === "settings" ? "Settings" : tab === "logs" ? "Logs" : tab === "memory" ? "Memory" : tab === "intrusion" ? "Intrusion" : tab === "suggestions" ? "Suggestions" : tab === "residence" ? "Residence" : "Command Center"}</div>
            </div>
          </div>
          <nav class="top-nav">
            <button class="nav-tab${tab === "dashboard" ? " active" : ""}" data-tab="dashboard">Command Center</button>
            <button class="nav-tab${tab === "residence" ? " active" : ""}" data-tab="residence">Residence</button>
            <button class="nav-tab${tab === "intrusion" ? " active" : ""}" data-tab="intrusion">Intrusion</button>
            <button class="nav-tab${tab === "suggestions" ? " active" : ""}" data-tab="suggestions">Suggestions</button>
            <button class="nav-tab${tab === "settings" ? " active" : ""}" data-tab="settings">Settings</button>
            <button class="nav-tab${tab === "logs" ? " active" : ""}" data-tab="logs">Logs</button>
            <button class="nav-tab${tab === "memory" ? " active" : ""}" data-tab="memory">Memory</button>
          </nav>
          <button class="lockdown-control" id="lockdownControl" hidden></button>
        </div>

        ${tab === "settings" ? this._htmlSettings() : tab === "logs" ? this._htmlLogs() : tab === "memory" ? this._htmlMemory() : tab === "intrusion" ? this._htmlIntrusion() : tab === "suggestions" ? this._htmlSuggestions() : tab === "residence" ? this._htmlResidence() : this._htmlDashboard()}

        <div class="footnote">NOVA COMMAND CENTER</div>
      </div>
    `;
  }

  _htmlDashboard() {
    return `
        <div id="onboardingMount"></div>
        <div class="hero">
          <div class="core-wrap"><canvas class="core" id="core"></canvas></div>
          <div class="state-line" id="stateLine">Watching over the house.</div>
          <div class="state-sub" id="stateSub">—</div>
          <div class="chips" id="chips"></div>
        </div>
${this._htmlDashboardBody()}`;
  }

  _onboardingHtml(onboarding) {
    return onboarding?.show ? `
      <div class="onboarding-card" id="onboardingCard">
        <div class="panel-head"><div><div class="panel-title">Welcome — get Nova working for you</div>
          <div class="toggle-desc">These steps are optional. Nova can already answer you.</div></div>
          <button class="camera-toggle" id="onboardingDismiss" title="Dismiss">DISMISS</button></div>
        <div class="onboarding-progress"><span>${this._esc(onboarding.done_count || 0)}/${this._esc(onboarding.total || 0)} DONE</span><i style="width:${Math.round(((onboarding.done_count || 0) / Math.max(1, onboarding.total || 1)) * 100)}%"></i></div>
        <div class="onboarding-steps">${(onboarding.steps || []).map(step => `<div class="onboarding-step${step.done ? " done" : ""}">
          <span>${step.done ? "✓" : "○"}</span><div><b>${this._esc(step.label)}</b><small>${this._esc(step.hint)}</small></div>
          ${step.jump ? `<button class="mode-chip onboarding-jump" data-settings-title="${this._esc(step.jump)}">OPEN</button>` : ""}</div>`).join("")}</div>
        <button class="mode-chip onboarding-settings">OPEN SETTINGS</button>
      </div>` : "";
  }

  _htmlDashboardBody() {
    return `
        <div class="grid">
          <div class="panel">
            <div class="panel-head">
              <div class="panel-title">Activity</div>
              <div class="panel-meta" id="feedMeta">—</div>
            </div>
            <div class="feed" id="feed"></div>
          </div>
        </div>

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

        <div class="panel" id="solarPanel" style="max-width:1100px;margin:16px auto 0">
          <div class="panel-head">
            <div class="panel-title">Solar</div>
            <div class="panel-meta" id="solarSufficiency">—</div>
          </div>
          <div id="solarBody" class="stub-body">Loading…</div>
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
    `;
  }

