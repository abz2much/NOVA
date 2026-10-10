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
    root.innerHTML = this._html();   // i18n-ok: the first render, translated just below
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
          <div class="hero-stage">
            <div class="hero-marquee" aria-hidden="true"><span id="heroMarquee"></span></div>
            <canvas class="core" id="core"></canvas>
            <div class="hero-copy">
              <div class="hero-word" id="heroWord"><span id="heroWordText"></span><span class="hero-caret" aria-hidden="true"></span></div>
              <div class="state-line" id="stateLine">Watching over the house.</div>
              <div class="state-sub" id="stateSub">—</div>
            </div>
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

