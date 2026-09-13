/*
 * Nova — new Command Center look (v7.95.0).
 *
 * A genuinely separate implementation from nova-panel.js's Classic UI, per
 * Abi's explicit choice — full creative freedom over ongoing maintenance
 * cost. Registered as "nova-panel-new" and mounted only when a user opts
 * into ui_style="new" (see NovaPanelShell at the bottom of nova-panel.js,
 * which dynamically imports this file).
 *
 * Scope (v7.95.0): Command Center + Settings, reorganized around what
 * you're trying to do rather than which subsystem it touches (General,
 * Voice & Speakers, Awareness & Safety, Learning & Memory, Cameras, Home
 * & Extras), plus a search box across every setting. Residence, Intrusion,
 * Suggestions, Logs, and Memory still live in Classic — the "Look"
 * selector is the way back. 25 of 26 Settings cards are real here, filled
 * in group by group; only Floor Plan Editor stays a stub — a full SVG
 * drag-and-drop editor tied to Classic's Residence 3D view, out of scope
 * for the same reason that tab is Classic-only.
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
    this._currentTab = "dashboard"; // "dashboard" | "settings"
    this._settingsSection = "general";
    this._settingsSearch = "";
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
    try {
      this._mode = await this._hass.callWS({ type: "nova/mode", action: "status" });
    } catch (_) { this._mode = null; }
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
    if (this._animHandle) { cancelAnimationFrame(this._animHandle); this._animHandle = null; }
    if (this._resizeListener) { window.removeEventListener("resize", this._resizeListener); this._resizeListener = null; }
    this._ctx = null;
    this._canvas = null;

    const root = this.shadowRoot;
    root.innerHTML = this._html();
    this._renderedOnce = true;
    this._wire();
    this._initCore();
    this._renderData();
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
              <div class="brand-tag">${tab === "settings" ? "Settings" : "Command Center"}</div>
            </div>
          </div>
          <nav class="top-nav">
            <button class="nav-tab${tab === "dashboard" ? " active" : ""}" data-tab="dashboard">Command Center</button>
            <button class="nav-tab${tab === "settings" ? " active" : ""}" data-tab="settings">Settings</button>
          </nav>
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

        ${tab === "settings" ? this._htmlSettings() : this._htmlDashboard()}

        <div class="footnote">NOVA — NEW LOOK · PREVIEW · RESIDENCE, INTRUSION, SUGGESTIONS, LOGS AND MEMORY STILL LIVE IN CLASSIC</div>
      </div>
    `;
  }

  _htmlDashboard() {
    return `
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
    `;
  }

  // ─── Settings ─────────────────────────────────────────────────────────
  // Reorganized around what you're trying to do rather than which Nova
  // subsystem it touches — the split Classic has (System Diagnostics
  // under General, a separate Diagnostics under Cameras) is merged here
  // into one place. "real:true" cards are fully built (25 of 26, filled in
  // group by group); only Floor Plan Editor stays a stub — it's a full SVG
  // drag-and-drop editor tied to Classic's Residence 3D view, the same
  // reason that tab stays Classic-only in V1. Every stub is a card naming
  // exactly where to find it today rather than being
  // silently missing, filled in card-by-card in later passes.
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
    { id: "residence_home", group: "general", title: "Residence / Home", real: true,
      desc: "Home style, stories, and layout counts that feed Classic's Residence 3D view." },
    { id: "operational_mode", group: "general", title: "Operational Mode", real: true,
      desc: "Party/movie/away modes and what each one changes while active." },
    { id: "diagnostics", group: "general", title: "Diagnostics", real: true,
      desc: "Service health checks and system status (merged from Classic's two separate diagnostics cards)." },
    { id: "room_speakers", group: "voice", title: "Room Speakers", real: true,
      desc: "Assign the one speaker Nova may use per room, plus a general fallback." },
    { id: "ai_models", group: "voice", title: "AI Models", real: true,
      desc: "Provider and model per tier — main agent, classifier, reasoning, review, vision." },
    { id: "briefings", group: "voice", title: "Briefings", real: true,
      desc: "Daily briefing schedule, content, and delivery speakers." },
    { id: "voice_confirmation", group: "voice", title: "Voice Confirmation", real: true,
      desc: "Whether risky actions need a spoken or phone confirmation before Nova acts." },
    { id: "satellite_speaker", group: "voice", title: "Satellite → Speaker", real: true,
      desc: "Per-satellite override, for a specific satellite that shouldn't use its room's assigned speaker." },
    { id: "announcement_speakers", group: "voice", title: "Announcement Speakers", real: true,
      desc: "Which speakers whole-house broadcasts (briefings, sentinel alerts) use." },
    { id: "notifications", group: "safety", title: "Notifications", real: true,
      desc: "Your phone's notify service, for alerts when nobody's home to hear a speaker." },
    { id: "sentinel_rules", group: "safety", title: "Sentinel Rules", real: true,
      desc: "Enable or disable individual door/lock/garage anomaly rules." },
    { id: "hazard_monitor", group: "safety", title: "Hazard Monitor", real: true,
      desc: "Earthquake, severe weather, and disaster feeds near your home." },
    { id: "energy_management", group: "safety", title: "Energy Management", real: true,
      desc: "Peak-draw threshold and how much say Nova has over high-draw appliances." },
    { id: "appliances", group: "safety", title: "Appliances", real: true,
      desc: "Declared appliance profiles Nova fingerprints by wattage." },
    { id: "anticipation_memory", group: "learning", title: "Anticipation & Memory", real: true,
      desc: "Cross-session memory window, continued conversation, and multi-satellite follow." },
    { id: "memory_curated", group: "learning", title: "Memory", real: true,
      desc: "Memory backend and how many memories are stored. Full review/edit stays on Classic's own Memory tab for now." },
    { id: "observer_tuning", group: "learning", title: "Observer Tuning", real: true,
      desc: "How cautious or talkative the proactive Observer is." },
    { id: "routine_learning", group: "learning", title: "Routine Learning", real: true,
      desc: "What Nova is allowed to learn from — doors, presence, button presses." },
    { id: "excluded_entities", group: "learning", title: "Excluded Entities", real: true,
      desc: "Entities, domains, or labels Nova should ignore entirely." },
    { id: "cameras", group: "cameras", title: "Cameras", real: true,
      desc: "Camera names, indoor/outdoor designation, and location overrides." },
    { id: "doorbell_training", group: "cameras", title: "Doorbell Training", real: true,
      desc: "Teach Nova to recognize regular visitors at the door." },
    { id: "floor_plan_editor", group: "home", title: "Floor Plan Editor",
      desc: "A full SVG drag-and-drop editor tied to Classic's Residence 3D view — same reason that tab itself stays Classic-only in V1, not a smaller lift than the rest of this list." },
    { id: "wellbeing_context", group: "home", title: "Wellbeing Context", real: true,
      desc: "Whether wearable heart-rate/sleep data reaches Nova, and which providers." },
    { id: "character_research", group: "home", title: "Nova Character & Research", real: true,
      desc: "Banter level and the web-research backend (DuckDuckGo or self-hosted SearXNG)." },
    { id: "document_library", group: "home", title: "Document Library", real: true,
      desc: "Manuals and receipts Nova can search and cite from." },
  ];

  _htmlSettings() {
    const groupsNav = NovaCommandCenterNew.SETTINGS_GROUPS.map(g =>
      `<button class="settings-nav-btn${this._settingsSection === g.id ? " active" : ""}" data-settings-section="${g.id}">${this._esc(g.label)}</button>`
    ).join("");
    const cards = NovaCommandCenterNew.SETTINGS_CARDS.map(c => this._settingsCardHtml(c)).join("");
    return `
        <div class="settings-toolbar">
          <input type="search" id="settingsSearch" class="settings-search" placeholder="Search settings — try “camera” or “sleep”…" value="${this._esc(this._settingsSearch)}">
          <nav class="settings-nav">${groupsNav}</nav>
        </div>
        <div class="settings-grid" id="settingsGrid">${cards}</div>
    `;
  }

  _settingsCardHtml(c) {
    const body = c.real
      ? (c.id === "general" ? this._generalCardBody()
        : c.id === "room_speakers" ? this._roomSpeakersCardBody()
        : c.id === "residence_home" ? this._residenceHomeCardBody()
        : c.id === "operational_mode" ? this._operationalModeCardBody()
        : c.id === "diagnostics" ? this._diagnosticsCardBody()
        : c.id === "ai_models" ? this._aiModelsCardBody()
        : c.id === "briefings" ? this._briefingsCardBody()
        : c.id === "voice_confirmation" ? this._voiceConfirmationCardBody()
        : c.id === "satellite_speaker" ? this._satelliteSpeakerCardBody()
        : c.id === "announcement_speakers" ? this._announcementSpeakersCardBody()
        : c.id === "notifications" ? this._notificationsCardBody()
        : c.id === "sentinel_rules" ? this._sentinelRulesCardBody()
        : c.id === "hazard_monitor" ? this._hazardMonitorCardBody()
        : c.id === "energy_management" ? this._energyManagementCardBody()
        : c.id === "appliances" ? this._appliancesCardBody()
        : c.id === "anticipation_memory" ? this._anticipationMemoryCardBody()
        : c.id === "memory_curated" ? this._memoryCardBody()
        : c.id === "observer_tuning" ? this._observerTuningCardBody()
        : c.id === "routine_learning" ? this._routineLearningCardBody()
        : c.id === "excluded_entities" ? this._excludedEntitiesCardBody()
        : c.id === "cameras" ? this._camerasCardBody()
        : c.id === "doorbell_training" ? this._doorbellTrainingCardBody()
        : c.id === "wellbeing_context" ? this._wellbeingContextCardBody()
        : c.id === "character_research" ? this._characterResearchCardBody()
        : c.id === "document_library" ? this._documentLibraryCardBody()
        : "")
      : `<div class="stub-body">${this._esc(c.desc)}<br><span class="stub-where">Not built here yet — use Classic, or Settings → Devices &amp; Services → Nova → Configure.</span></div>`;
    return `
      <div class="panel settings-card" data-settings-group="${c.group}" data-search="${this._esc((c.title + " " + c.desc).toLowerCase())}">
        <div class="panel-head">
          <div class="panel-title">${this._esc(c.title)}${c.real ? "" : '<span class="stub-tag">SOON</span>'}</div>
        </div>
        ${body}
      </div>`;
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
        <label>Sleep state</label>
        <select class="cfg-field" data-cfg-key="sleep_override">
          ${this._optSelect([["auto", "Auto (occupancy + quiet hours)"], ["awake", "Awake"], ["asleep", "Asleep"]], cfg.sleep_override || "auto")}
        </select>
      </div>
      <div class="toggle-list">
        ${onOff("announcements_enabled", "Announcements", "Master switch — all proactive speech")}
        ${onOff("sentinel_enabled", "Sentinel", "Door/garage/lock-left-open alerts")}
        ${onOff("observer_enabled", "Observer", "AI event awareness (uses API)")}
      </div>`;
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

  _residenceHomeCardBody() {
    const cfg = this._data()?.config || {};
    const styles = {
      cape_cod: "Cape Cod", colonial: "Colonial", dutch_colonial: "Dutch Colonial",
      ranch: "Ranch", two_story: "Two-Story", craftsman: "Craftsman",
      modern: "Modern", townhouse: "Townhouse", apartment: "Apartment", cabin: "Cabin",
    };
    return `
      <div class="cfg-row">
        <label>Home type</label>
        <select class="cfg-field" data-cfg-key="residence_style">
          ${this._optSelect(Object.entries(styles), cfg.residence_style || "cape_cod")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Stories</label>
        <select class="cfg-field" data-cfg-key="home_stories">
          ${this._optSelect(["1", "1.5", "2", "3"].map(v => [v, v]), String(cfg.home_stories ?? "1.5"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Garage bays</label>
        <select class="cfg-field" data-cfg-key="garage_bays">
          ${this._optSelect(["0", "1", "2", "3", "4"].map(v => [v, v]), String(cfg.garage_bays ?? "3"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Front dormers</label>
        <select class="cfg-field" data-cfg-key="dormers_front">
          ${this._optSelect(["0", "1", "2", "3"].map(v => [v, v]), String(cfg.dormers_front ?? "2"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Rear dormers</label>
        <select class="cfg-field" data-cfg-key="dormers_rear">
          ${this._optSelect(["0", "1", "2"].map(v => [v, v]), String(cfg.dormers_rear ?? "1"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Chimney</label>
        <select class="cfg-field" data-cfg-key="chimney_side">
          ${this._optSelect([["right", "East / right"], ["left", "West / left"], ["none", "None"]], cfg.chimney_side || "right")}
        </select>
      </div>
      <div class="cfg-row">
        <label>Basement</label>
        <button class="toggle-btn ${(cfg.has_basement !== false) ? "on" : "off"}" data-cfg-key="has_basement" data-cfg-val="${(cfg.has_basement !== false) ? "false" : "true"}">
          ${(cfg.has_basement !== false) ? "YES" : "NO"}
        </button>
      </div>
      <div class="cfg-row">
        <label>Bedrooms</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="12" data-cfg-key="home_bedrooms" value="${cfg.home_bedrooms ?? ""}" placeholder="3">
      </div>
      <div class="cfg-row">
        <label>Bathrooms</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="12" step="0.5" data-cfg-key="home_bathrooms" value="${cfg.home_bathrooms ?? ""}" placeholder="2">
      </div>
      <div class="cfg-row">
        <label>Square feet</label>
        <input class="cfg-field cfg-num" type="number" min="0" max="20000" step="50" data-cfg-key="floor_plan_sqft" value="${cfg.floor_plan_sqft ?? ""}" placeholder="1800">
      </div>
      <div class="stub-body">Detailed room layout is edited in the Floor Plan Editor. This feeds Classic's Residence 3D view.</div>`;
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
      <div class="stub-body">Active: <strong>${this._esc(active.toUpperCase())}</strong>${m.description ? " — " + this._esc(m.description) : ""}. Safety always stays active.</div>
      <div class="mode-grid">${modeChips}</div>
      <div class="mode-bind-head">Mode bindings — scope Lab &amp; Movie to specific rooms</div>
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
      </div>`;
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
    if (this._currentTab === "settings") this._render();
  }

  _diagStatusCls(st) {
    return { ok: "diag-ok", warn: "diag-warn", idle: "diag-idle", down: "diag-down", off: "diag-off" }[st] || "diag-off";
  }
  _diagStatusLabel(st) {
    return { ok: "OK", warn: "WARN", idle: "IDLE", down: "DOWN", off: "OFF" }[st] || "?";
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
          <label>${this._esc(s.name)} <span class="${this._diagStatusCls(s.status)}">${this._diagStatusLabel(s.status)}</span></label>
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
      <div class="mode-bind-head">Service tests</div>
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
      { role: "review", label: "Review", provKey: "review_provider", modelKey: "review_model" },
      { role: "vision", label: "Vision", provKey: "vision_provider", modelKey: "vision_model" },
      { role: "camrsn", label: "Camera Rsn", provKey: "camera_reasoning_provider", modelKey: "camera_reasoning_model" },
    ];
  }

  _aiModelsCardBody() {
    const cfg = this._data()?.config || {};
    const PROVIDERS = ["groq", "openai", "gemini", "ollama", "anthropic", "custom"];
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
          ${r.role === "vision" ? `<div class="stub-body">Needs an image-capable model — e.g. moondream on Ollama, or a Groq vision model. Text-only models will fail on camera analysis.</div>` : ""}
        </div>`;
    }).join("");
    return `<div class="new-model-list">${rows}</div>`;
  }

  _pickHealModel(models, cfgKey) {
    if (/vision/.test(cfgKey || "")) {
      return models.find(m => /vision|vl|scout|maverick|llama-4|gpt-4o|multimodal|qwen3\.\d|gemini/i.test(m)) || null;
    }
    return models[0] || null;
  }

  async _loadModelsFor(provider, selectEl) {
    if (!this._hass || !selectEl) return;
    const cur = selectEl.getAttribute("data-current") || "";
    try {
      const res = await this._hass.callWS({ type: "nova/list_models", provider });
      const models = (res && res.models) || [];
      let opts = "";
      if (models.length) {
        if (cur && !models.includes(cur)) {
          const cfgKey = selectEl.getAttribute("data-cfg-key");
          const heal = this._pickHealModel(models, cfgKey);
          if (heal && cfgKey) {
            await this._rawSaveConfig(cfgKey, heal);
            selectEl.setAttribute("data-current", heal);
            opts += models.map(m => `<option value="${this._esc(m)}"${m === heal ? " selected" : ""}>${this._esc(m)}</option>`).join("");
          } else {
            opts += `<option value="${this._esc(cur)}" selected>${this._esc(cur)} — unavailable, pick one</option>`;
            opts += models.map(m => `<option value="${this._esc(m)}">${this._esc(m)}</option>`).join("");
          }
        } else {
          opts += models.map(m => `<option value="${this._esc(m)}"${m === cur ? " selected" : ""}>${this._esc(m)}</option>`).join("");
        }
      } else {
        const err = res && res.error ? ` — ${String(res.error).slice(0, 48)}` : "";
        opts += (cur ? `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>` : "");
        opts += `<option value="" disabled>no models found${this._esc(err)}</option>`;
      }
      opts += `<option value="__custom__">✎ Custom…</option>`;
      selectEl.innerHTML = opts;
    } catch (_) { /* leave current options in place on error */ }
  }

  async _rawSaveConfig(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova (new look): failed to save ${key}`, err);
    }
  }

  _wireAiModels() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-model-row").forEach(row => {
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      const customInput = row.querySelector(".new-model-custom");
      if (!provSel || !modelSel) return;
      this._loadModelsFor(provSel.value, modelSel);
      provSel.addEventListener("change", async (e) => {
        const provider = e.target.value;
        const provKey = provSel.getAttribute("data-cfg-key");
        await this._rawSaveConfig(provKey, provider);
        if (provKey === "llm_provider" && ["groq", "openai", "gemini", "anthropic"].includes(provider)) {
          await this._rawSaveConfig("llm_base_url", "");
        }
        modelSel.setAttribute("data-current", "");
        if (customInput) customInput.style.display = "none";
        await this._loadModelsFor(provider, modelSel);
        const newModel = modelSel.value;
        if (newModel && newModel !== "__custom__" && newModel !== "") {
          await this._rawSaveConfig(modelSel.getAttribute("data-cfg-key"), newModel);
          modelSel.setAttribute("data-current", newModel);
        }
      });
      modelSel.addEventListener("change", async (e) => {
        if (e.target.value === "__custom__") {
          if (customInput) { customInput.style.display = ""; customInput.focus(); }
          return;
        }
        if (customInput) customInput.style.display = "none";
        await this._rawSaveConfig(modelSel.getAttribute("data-cfg-key"), e.target.value);
        modelSel.setAttribute("data-current", e.target.value);
      });
      if (customInput) {
        customInput.addEventListener("change", async (e) => {
          const v = (e.target.value || "").trim();
          if (v) {
            await this._rawSaveConfig(customInput.getAttribute("data-cfg-key"), v);
            modelSel.setAttribute("data-current", v);
          }
        });
      }
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
      <div class="cfg-row"><button class="mode-chip" id="newBriefNow">▶ BRIEF ME NOW</button></div>`;
  }

  _voiceConfirmationCardBody() {
    const cfg = this._data()?.config || {};
    const on = !!cfg.voice_confirm_enabled;
    return `
      <div class="stub-body">Ask out loud before sensitive actions (unlock, garage, disarm) and listen for a spoken yes/no. Native mode uses the satellite's own audio; gated mode speaks through the room speaker — run the test to see which your setup supports.</div>
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
    const opts = [["", "— none —"], ...svcs.map(s => [s, s.replace("notify.", "")])];
    return `
      <div class="toggle-row">
        <span class="toggle-label">Notify Device</span>
        <span class="toggle-desc">Phone push for high/critical alerts</span>
        <select class="cfg-field" data-cfg-key="notify_service">${this._optSelect(opts, cfg.notify_service || "")}</select>
      </div>`;
  }

  _sentinelRulesCardBody() {
    const cfg = this._data()?.config || {};
    const rules = cfg.sentinel_rules || [];
    const disabled = cfg.disabled_sentinel_rules || [];
    if (!rules.length) return `<div class="stub-body">No sentinel rules found.</div>`;
    const rows = rules.map(r => {
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
  // pattern as Diagnostics) — a manual SCAN NOW re-checks USGS/NWS/EONET.
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
    const loc = hz.center
      ? (hz.using_override ? `Location: override ${hz.center[0]}, ${hz.center[1]}.` : `Location: home ${hz.center[0]}, ${hz.center[1]}.`)
      : "Location: using home coordinates.";
    const feedChip = (key, label) => {
      const on = cfg[key] !== false;
      return `<button class="mode-chip ${on ? "mode-chip-on" : ""}" data-cfg-key="${key}" data-cfg-val="${on ? "false" : "true"}">${label}</button>`;
    };
    return `
      <div class="stub-body">Real-time nearby earthquakes (USGS), severe-weather warnings (NWS), and natural disasters like wildfires (NASA EONET). Alerts speak and push like any Nova alert.</div>
      <div class="cfg-row">
        <label>Monitor</label>
        <button class="toggle-btn ${cfg.hazard_monitor_enabled ? "on" : "off"}" data-cfg-key="hazard_monitor_enabled" data-cfg-val="${cfg.hazard_monitor_enabled ? "false" : "true"}">${cfg.hazard_monitor_enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        ${feedChip("hazard_quakes_on", "Earthquakes")}
        ${feedChip("hazard_weather_on", "Weather")}
        ${feedChip("hazard_disasters_on", "Disasters")}
      </div>
      <div class="stub-body" style="font-family:var(--font-mono);font-size:10.5px">${this._esc(loc)}</div>
      <div class="cfg-row">
        <label>Override lat / lon <span class="toggle-desc">optional</span></label>
        <div style="display:flex;gap:6px">
          <input class="cfg-field cfg-num" style="width:76px" type="text" inputmode="decimal" data-cfg-key="hazard_lat" value="${this._esc(cfg.hazard_lat || "")}" placeholder="lat">
          <input class="cfg-field cfg-num" style="width:76px" type="text" inputmode="decimal" data-cfg-key="hazard_lon" value="${this._esc(cfg.hazard_lon || "")}" placeholder="lon">
        </div>
      </div>
      <div class="cfg-row">
        <label>Quake radius (km) / min mag</label>
        <div style="display:flex;gap:6px">
          <input class="cfg-field cfg-num" style="width:56px" type="text" inputmode="numeric" data-cfg-key="hazard_quake_radius_km" value="${this._esc(cfg.hazard_quake_radius_km ?? 300)}">
          <input class="cfg-field cfg-num" style="width:56px" type="text" inputmode="decimal" data-cfg-key="hazard_quake_min_mag" value="${this._esc(cfg.hazard_quake_min_mag ?? 2.5)}">
        </div>
      </div>
      <div class="cfg-row"><button class="mode-chip" id="newHazScan">⟳ SCAN NOW</button></div>
      <div id="newHazBody" class="stub-body"></div>`;
  }

  _renderHazardScan(res) {
    if (!res || res.ok === false) {
      return `<div class="stub-body">${this._esc(res?.error || "No location configured.")}</div>`;
    }
    const q = res.earthquakes || [], w = res.weather || [], d = res.disasters || [];
    if (!q.length && !w.length && !d.length) {
      return `<div class="stub-body">✓ All clear near ${res.center ? res.center[0] + ", " + res.center[1] : "home"} — no active earthquakes, severe weather, or disasters.</div>`;
    }
    let html = "";
    for (const e of q) {
      const mag = (typeof e.mag === "number") ? `M${e.mag.toFixed(1)}` : "M?";
      html += `<div class="stub-body"><b class="diag-warn">${mag}</b> ${this._esc(e.place)} — ${e.dist_km} km away</div>`;
    }
    for (const e of w) {
      html += `<div class="stub-body"><b class="diag-down">${this._esc(e.severity)}</b> ${this._esc(e.event)}${e.area ? " — " + this._esc(e.area) : ""}</div>`;
    }
    for (const e of d) {
      html += `<div class="stub-body"><b class="diag-warn">${this._esc(e.category)}</b> ${this._esc(e.title)} — ${e.dist_km} km away</div>`;
    }
    return html;
  }

  // Energy status is fetched once per element lifetime (same on-demand
  // pattern as Diagnostics/Hazard) — set_agency re-fetches immediately after.
  async _fetchEnergyStatus() {
    if (!this._hass) return;
    try {
      this._energy = await this._hass.callWS({ type: "nova/energy", action: "status" });
    } catch (_) { this._energy = { error: true }; }
    if (this._currentTab === "settings") this._render();
  }

  _energyManagementCardBody() {
    const e = this._energy || {};
    if (e.error) {
      return `<div class="stub-body">Couldn't load energy data — restart Home Assistant after updating.</div>`;
    }
    const draw = e.kw == null
      ? `<span class="diag-off">NO METER</span>`
      : `<span class="${e.over_peak ? "diag-warn" : "diag-ok"}">${e.kw} kW${e.over_peak ? " · OVER PEAK" : ""}</span>`;
    const agencies = ["advisory", "opt_in", "autonomous"];
    const agencyChips = agencies.map(a =>
      `<button class="mode-chip ${a === e.configured_agency ? "mode-chip-on" : ""}" data-agency="${a}">${a.replace("_", "-")}</button>`).join("");
    const advice = (e.advice || []).map(a => `<div class="stub-body">${this._esc(a)}</div>`).join("");
    const running = e.running || [];
    const runRows = running.length
      ? `<div class="mode-bind-head">Running now</div>` + running.map(r =>
          `<div class="cfg-row"><label>${this._esc(r.name || r.entity)}</label><span class="${r.shed_ok ? "" : "diag-warn"}">${r.watts} W${r.shed_ok ? "" : " · protected"}</span></div>`).join("")
      : "";
    return `
      <div class="stub-body">Whole-home power, peak awareness, and load advice. Pick how much Nova may act — it never sheds critical loads (fridge, medical, network).</div>
      <div class="cfg-row"><label>Current draw</label>${draw}</div>
      <div class="mode-grid" id="newEnergyAgency">${agencyChips}</div>
      ${advice}
      ${runRows}`;
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
      || `<div class="stub-body">No appliances declared yet — Nova falls back to generic power guesses until you add some.</div>`;
    return `
      <div class="stub-body">Tell Nova which appliances exist so it names cycles correctly instead of guessing from the whole-home meter. Map a dedicated power or status entity when one exists; otherwise set typical running watts.</div>
      <div class="new-appliance-list" id="newApplianceList">${rows}</div>
      <div class="mode-grid">
        <button class="mode-chip" id="newApplianceAdd">+ Add appliance</button>
        <button class="mode-chip mode-chip-on" id="newApplianceSave">Save appliances</button>
      </div>
      <div class="cfg-row" style="margin-top:12px">
        <label>Announce unidentified loads <span class="toggle-desc">loads matching no declared appliance</span></label>
        <button class="toggle-btn ${cfg.appliance_announce_unknown ? "on" : "off"}" id="newApplianceUnknown">${cfg.appliance_announce_unknown ? "ON" : "OFF"}</button>
      </div>`;
  }

  _wireAppliances() {
    const root = this.shadowRoot;
    const apList = root.getElementById("newApplianceList");
    const apAdd = root.getElementById("newApplianceAdd");
    const apSave = root.getElementById("newApplianceSave");
    const apUnknown = root.getElementById("newApplianceUnknown");
    if (apAdd && apList) {
      apAdd.addEventListener("click", () => {
        const empty = apList.querySelector(".stub-body");
        if (empty) empty.remove();
        const tmp = document.createElement("div");
        tmp.innerHTML = this._applianceRowHtml({ name: "", type: "appliance", entity: "", watts: "" });
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
        try { await this._hass.callWS({ type: "nova/reload_appliances" }); } catch (err) { console.error("Nova (new look): appliance reload failed", err); }
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    }
    if (apUnknown) {
      apUnknown.addEventListener("click", async () => {
        const newVal = !apUnknown.classList.contains("on");
        await this._rawSaveConfig("appliance_announce_unknown", newVal);
        try { await this._hass.callWS({ type: "nova/reload_appliances" }); } catch (_) {}
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    }
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
    const onOff = (key, defaultOn, hint) => {
      const on = defaultOn ? cfg[key] !== false : !!cfg[key];
      return `
        <div class="cfg-row">
          <label>${hint.label}${hint.sub ? `<span class="toggle-desc"> — ${this._esc(hint.sub)}</span>` : ""}</label>
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
      ${onOff("routine_alerts_enabled", false, { label: "Routine alerts" })}
      ${onOff("memory_threading_enabled", false, { label: "Memory threading" })}
      ${onOff("pattern_learn_motion", false, { label: "Learn motion/presence triggers" })}
      ${num("observer_group_debounce", "Sibling-burst coalescing (sec)", "90", 0, 600, 10)}
      ${onOff("continued_conversation_enabled", false, { label: "Continued conversation" })}
      ${onOff("continued_conversation_multi_satellite", false, { label: "Follow me between rooms", sub: "reopen the mic where you moved to (needs 2+ satellites)" })}
      ${onOff("continued_conversation_speaker_reopen", true, { label: "Follow-up mic reopen (speaker-aware)" })}
      ${onOff("tts_use_ha_voice", false, { label: "Use Home Assistant default voice" })}
      ${num("departure_lead_minutes", "Departure lead (min)", "30", 0, 240, 5)}
      ${num("memory_threading_hours", "Memory window (hrs)", "48", 1, 336, 1)}
      ${num("memory_threading_max", "Memory max turns", "12", 1, 50, 1)}
      <div class="cfg-row">
        <label>Origin tracker</label>
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
      <div class="stub-body">Departure warns when to leave for calendar events using your device location + open-source routing. Routine alerts learn per-person timing over about a week. Continued conversation keeps the mic open after a question.</div>`;
  }

  _memoryCardBody() {
    const cfg = this._data()?.config || {};
    const stats = cfg.memory_stats || {};
    return `
      <div class="cfg-row"><label>Backend</label><span>${this._esc(stats.backend || "—")}</span></div>
      <div class="cfg-row"><label>Stored Memories</label><span>${this._esc(stats.total_memories ?? 0)}</span></div>
      <div class="stub-body">Full review, edit, and forget lives on Classic's own Memory tab for now.</div>`;
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
      ${row("Local Decisions", `${s.local_rate || 0}% (${s.local_decisions || 0} local / ${s.cloud_calls || 0} cloud)`)}
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
        ${onOff("pattern_learn_doors", "Learn doors & windows", "Door, window and garage contact sensors")}
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
  _allEntityDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).sort().map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
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
    const head = `
      <div class="cfg-row">
        <label>${nOn} of ${cams.length} cameras in use</label>
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
            ${chip("auto", `AUTO (${resolved})`)}
            ${chip("indoor", "⌂ INDOOR")}
            ${chip("outdoor", "▲ OUTDOOR")}
          </div>
        </div>`;
    }).join("");
    return head + rows;
  }

  _camerasCardBody() {
    const cfg = this._data()?.config || {};
    return `
      <div class="stub-body">Names are Nova-only (HA untouched; blank reverts). Location governs intrusion + outdoor-event filtering — AUTO shows what the heuristics resolve.</div>
      <div class="cfg-row">
        <label>Face recognition source</label>
        <select class="cfg-field" data-cfg-key="recognition_source">${this._optSelect([["both", "Both (Double Take + Frigate)"], ["frigate", "Frigate only (sub_label)"], ["doubletake", "Double Take only"]], cfg.recognition_source || "both")}</select>
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
    host.innerHTML = this._renderCameraSettingsRows();
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
      } catch (err) { console.error("Nova (new look): camera enable/disable failed", err); }
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
        } catch (err) { console.error("Nova (new look): camera rename failed", err); }
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
          console.error("Nova (new look): camera location failed", err);
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
    return `
      <div class="cfg-row"${e.notable ? ' style="color:var(--gold)"' : ""}>
        <label>${this._esc(ts)} · ${this._esc(src)}${cat ? " · " + this._esc(cat) : ""}</label>
        <span class="toggle-desc">${desc}</span>
      </div>`;
  }

  _doorbellTrainingCardBody() {
    const t = this._data()?.doorbellTraining || {};
    const stats = t.stats || {};
    const events = t.recent || [];
    const total = stats.total || 0;
    const notable = stats.notable || 0;
    const bySource = stats.by_source || {};
    const srcLine = Object.keys(bySource).length
      ? Object.entries(bySource).map(([k, v]) => `${k} ${v}`).join(" · ")
      : "none yet";
    const rows = events.length
      ? events.slice().reverse().map(e => this._dbTrainRow(e)).join("")
      : `<div class="stub-body">No analysed doorbell events yet. Run a backlog scan, or wait for the next doorbell press.</div>`;
    return `
      <div class="stub-body">Analysed doorbell events — Nova's visitor training data. Each press is logged automatically; run a backlog scan to mine the recorded-event history into the dataset.</div>
      <div class="cfg-row">
        <label>Scan limit</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="newDbtLimit" class="cfg-field cfg-num" type="number" min="1" max="500" value="40" title="Max events to analyse">
          <button class="mode-chip" id="newDbtScan">Scan backlog</button>
        </div>
      </div>
      <div class="stub-body">${total} analysed · ${notable} notable · ${this._esc(srcLine)}</div>
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
      ? `<span class="diag-ok">ON · ${b.found || 0} sensor${b.found === 1 ? "" : "s"}</span>`
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
          ${this._optSelect([["0", "Plain — no wit"], ["1", "Dry — occasional wit (default)"], ["2", "Full — MCU Nova"]], String(cfg.banter_level ?? "1"))}
        </select>
      </div>
      <div class="cfg-row">
        <label>Web research backend</label>
        <select class="cfg-field" data-cfg-key="search_backend">
          ${this._optSelect([["duckduckgo", "DuckDuckGo (no key, default)"], ["searxng", "SearXNG (self-hosted)"]], cfg.search_backend || "duckduckgo")}
        </select>
      </div>
      <div class="cfg-row">
        <label>SearXNG URL</label>
        <input class="cfg-field" type="text" data-cfg-key="searxng_url" value="${this._esc(cfg.searxng_url || "")}" placeholder="http://searxng.local:8080" autocomplete="off">
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
      return `<div class="stub-body">No documents ingested yet. Add PDF/.txt/.md files to <code>/config/nova/documents</code> and press Ingest.${d.chroma ? "" : " (Vector search needs ChromaDB; keyword fallback is active.)"}</div>`;
    }
    return sources.map(s => `
      <div class="cfg-row">
        <label>${this._esc(s.source)}</label>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="toggle-desc">${s.chunks} chunks</span>
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
        <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="disable">✕ DISABLE SEMANTIC SEARCH</button></div>`;
    }
    if (!v.ollama_configured) {
      return `
        <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
        <div class="stub-body">Works everywhere with no setup. Semantic search needs an Ollama host — set the LLM base URL to your Ollama server and pull an embed model (ollama pull nomic-embed-text).</div>`;
    }
    return `
      <div class="cfg-row"><label>Search</label><span class="diag-off">KEYWORD (FTS)</span></div>
      <div class="stub-body">Enable semantic search to match on meaning, using your Ollama server (${this._esc(v.model || "nomic-embed-text")}). No install, no ChromaDB. Re-ingest afterward to embed existing docs.</div>
      <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="enable">⬆ ENABLE SEMANTIC SEARCH</button></div>`;
  }

  _documentLibraryCardBody() {
    const d = this._docLib || {};
    const backend = d.chroma ? "VECTOR" : d.fts ? "KEYWORD" : "NONE";
    return `
      <div class="stub-body">Drop manuals &amp; receipts (PDF, .txt, .md) into <code>/config/nova/documents</code> or upload below, then ingest. Ask Nova "what's the furnace filter size?" and it answers from your paperwork.</div>
      <div class="cfg-row"><label>Backend</label><span>${this._esc(backend)} · ${d.chunk_count || 0} chunks</span></div>
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
    host.innerHTML = this._renderDocLibraryList();
    this._wireDocLibraryDeletes();
  }

  _wireDocLibraryDeletes() {
    this.shadowRoot?.querySelectorAll(".new-doclib-del").forEach(btn => {
      btn.addEventListener("click", async () => {
        const src = btn.getAttribute("data-src");
        if (!src || !this._hass) return;
        if (!window.confirm(`Remove "${src}" from the library? This deletes the file and its indexed chunks.`)) return;
        try {
          await this._hass.callWS({ type: "nova/documents", action: "delete", filename: src });
          await this._fetchDocLibrary(); // triggers a full _render() when in the settings tab
        } catch (err) { console.error("Nova (new look): document delete failed", err); }
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
        ingestBtn.textContent = "⟳ INGESTING…";
        try {
          await this._hass.callWS({ type: "nova/documents", action: "ingest" });
          await this._fetchDocLibrary();
        } catch (err) {
          console.error("Nova (new look): ingest failed", err);
        } finally {
          ingestBtn.disabled = false;
          ingestBtn.textContent = orig;
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
          if (host) host.innerHTML = this._renderDocSearchResults(res?.results || []);
        } catch (err) { console.error("Nova (new look): document search failed", err); }
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
        upBtn.textContent = "⬆ UPLOADING…";
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
          console.error("Nova (new look): document upload failed", err);
        } finally {
          upBtn.disabled = false;
          upBtn.textContent = orig;
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
        scanBtn.textContent = "⟳ SCANNING…";
        try {
          const res = await this._hass.callWS({ type: "nova/documents", action: "scan_watch" });
          if (res.watched > 0) {
            await this._fetchDocLibrary();
          }
        } catch (err) {
          console.error("Nova (new look): watch scan failed", err);
        } finally {
          scanBtn.disabled = false;
          scanBtn.textContent = orig;
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
          console.error("Nova (new look): semantic search toggle failed", err);
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
          <div class="area-name">${this._esc(a.name)}${a.active ? '<span class="live-dot" title="Occupied now"></span>' : ""}</div>
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

    // Top nav: Command Center / Settings
    root.querySelectorAll(".nav-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        if (tab === this._currentTab) return;
        this._currentTab = tab;
        this._render();
      });
    });

    if (this._currentTab === "settings") this._wireSettings();
  }

  _wireSettings() {
    const root = this.shadowRoot;

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
        await this._saveSetting(sel.getAttribute("data-cfg-key"), sel.value);
      });
    });
    root.querySelectorAll("input.cfg-field[data-cfg-key]").forEach(inp => {
      inp.addEventListener("change", async () => {
        const key = inp.getAttribute("data-cfg-key");
        let value = inp.value;
        if (inp.type === "number") value = (value === "" ? null : Number(value));
        await this._saveSetting(key, value);
      });
    });

    root.querySelectorAll(".new-room-speaker-select").forEach(sel => {
      sel.addEventListener("change", async () => {
        const cfg = this._data()?.config || {};
        const assigned = { ...(cfg.room_speakers || {}) };
        const areaId = sel.getAttribute("data-area-id");
        if (sel.value) assigned[areaId] = sel.value; else delete assigned[areaId];
        await this._saveSetting("room_speakers", JSON.stringify(assigned));
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
    const generalSpeakerSel = root.querySelector(".new-general-speaker-select");
    if (generalSpeakerSel) {
      generalSpeakerSel.addEventListener("change", async () => {
        await this._saveSetting("general_speaker", generalSpeakerSel.value);
      });
    }

    // Operational Mode: mode chips call nova/mode directly (not update_config —
    // same websocket contract Classic's own mode-grid already uses).
    root.querySelectorAll(".mode-grid .mode-chip[data-mode]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const mode = btn.getAttribute("data-mode");
        if (!this._hass || !mode || btn.classList.contains("mode-chip-on")) return;
        try {
          await this._hass.callWS({ type: "nova/mode", action: "set", mode });
        } catch (err) {
          console.error("Nova (new look): failed to set mode", err);
        }
        await this._fetchLiveData();
        if (this._currentTab === "settings") this._render();
      });
    });
    root.querySelectorAll(".mode-grid [data-lab-area]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-lab-area");
        let cur = this._data()?.config?.lab_areas;
        cur = Array.isArray(cur) ? cur.slice() : [];
        const i = cur.indexOf(id);
        if (i >= 0) cur.splice(i, 1); else cur.push(id);
        await this._saveSetting("lab_areas", cur);
      });
    });

    this._wireAiModels();
    this._wireAppliances();
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
          console.error("Nova (new look): doorbell backlog scan failed", err);
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
          console.warn(`Nova (new look): "${eid}" is not a known entity id`);
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
      console.warn(`Nova (new look): "${v}" is not a known entity id`);
      return false;
    });
    exclAdd("newExclDomAdd", "newExclDomInput", "excluded_domains", null);
    exclAdd("newExclLabAdd", "newExclLabInput", "excluded_labels", null);
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
        vcTest.textContent = "▶ PLAYING…";
        if (out) out.textContent = "Firing announce to your satellite — listen for it…";
        try {
          const res = await this._hass.callWS({ type: "nova/voice_confirm_test" });
          if (out) out.innerHTML = res.ok
            ? `<span class="diag-ok">✓</span> ${this._esc(res.note || "Announce fired.")} (${this._esc(res.satellite || "")})`
            : `<span class="diag-down">✕</span> ${this._esc(res.note || res.error || "Test failed.")}`;
        } catch (err) {
          if (out) out.innerHTML = `<span class="diag-down">✕</span> ${this._esc(err?.message || String(err))}`;
        } finally {
          vcTest.disabled = false;
          vcTest.textContent = orig;
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
          console.warn("Nova (new look): no announcement speakers set — choose them in Settings → Announcement Speakers");
          return;
        }
        briefNow.disabled = true;
        const orig = briefNow.textContent;
        briefNow.textContent = "▶ BRIEFING…";
        try {
          await this._hass.callService("nova", "briefing", { announce: true });
        } catch (err) {
          console.error("Nova (new look): briefing failed", err);
        } finally {
          briefNow.disabled = false;
          briefNow.textContent = orig;
        }
      });
    }

    // Diagnostics: fetch once per element lifetime (see _fetchDiagnosticsData
    // for why this isn't on the live-data poll), then RUN CHECK re-fetches
    // on demand and service-test buttons call the same HA services Classic's
    // own Diagnostics card does.
    if (!this._diagFetchedOnce) {
      this._diagFetchedOnce = true;
      this._fetchDiagnosticsData();
    }
    if (!this._hazFetchedOnce) {
      this._hazFetchedOnce = true;
      this._fetchHazardStatus();
    }
    if (!this._energyFetchedOnce) {
      this._energyFetchedOnce = true;
      this._fetchEnergyStatus();
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
          console.error("Nova (new look): wellbeing toggle failed", err);
        }
        await this._fetchBio();
      });
    }
    root.querySelectorAll("#newEnergyAgency .mode-chip[data-agency]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        const agency = btn.getAttribute("data-agency");
        try {
          await this._hass.callWS({ type: "nova/energy", action: "set_agency", agency });
        } catch (err) {
          console.error("Nova (new look): failed to set energy agency", err);
        }
        await this._fetchEnergyStatus();
      });
    });
    const hazScan = root.getElementById("newHazScan");
    if (hazScan) {
      hazScan.addEventListener("click", async () => {
        if (!this._hass) return;
        const body = root.getElementById("newHazBody");
        hazScan.disabled = true;
        const orig = hazScan.textContent;
        hazScan.textContent = "⟳ SCANNING…";
        if (body) body.innerHTML = `<div class="stub-body">Checking USGS, NWS, and NASA EONET…</div>`;
        try {
          const res = await this._hass.callWS({ type: "nova/hazard", action: "scan" });
          if (body) body.innerHTML = this._renderHazardScan(res);
        } catch (err) {
          if (body) body.innerHTML = `<div class="stub-body">Scan failed: ${this._esc(err?.message || String(err))}</div>`;
        } finally {
          hazScan.disabled = false;
          hazScan.textContent = orig;
        }
      });
    }
    const diagRefresh = root.getElementById("newDiagRefresh");
    if (diagRefresh) {
      diagRefresh.addEventListener("click", () => this._fetchDiagnosticsData());
    }
    root.querySelectorAll(".settings-card [data-svc]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const svcAttr = btn.getAttribute("data-svc");
        if (!svcAttr || !this._hass) return;
        const [domain, service] = svcAttr.split(".");
        try {
          await this._hass.callService(domain, service, {});
        } catch (err) {
          console.error(`Nova (new look): service ${svcAttr} failed`, err);
        }
      });
    });
    const camRun = root.getElementById("newDiagCameraRun");
    if (camRun) {
      camRun.addEventListener("click", async () => {
        const sel = root.getElementById("newDiagCameraSelect");
        const entity_id = sel ? sel.value : "";
        if (!entity_id || !this._hass) return;
        try {
          await this._hass.callService("nova", "analyze_camera", { entity_id, announce: true });
        } catch (err) {
          console.error("Nova (new look): camera analyze failed", err);
        }
      });
    }

    this._applySettingsFilter();
  }

  async _saveSetting(key, value) {
    if (!this._hass || !key) return;
    try {
      await this._hass.callWS({ type: "nova/update_config", key, value });
    } catch (err) {
      console.error(`Nova (new look): failed to save ${key}`, err);
    }
    await this._fetchLiveData();
    if (this._currentTab === "settings") this._render();
  }

  _applySettingsFilter() {
    const root = this.shadowRoot;
    const q = (this._settingsSearch || "").trim().toLowerCase();
    root.querySelectorAll(".settings-card").forEach(card => {
      const matchesGroup = !q && card.getAttribute("data-settings-group") === this._settingsSection;
      const matchesSearch = q && (card.getAttribute("data-search") || "").includes(q);
      card.hidden = !(matchesGroup || matchesSearch);
    });
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
      .area-tile{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:11px;padding:10px 11px;
        transition:border-color .25s,box-shadow .25s}
      .area-tile.active{border-color:#e2542f70;box-shadow:inset 0 0 14px #e2542f14,0 0 14px #e2542f12}
      .area-name{font-size:12.5px;font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:6px}
      .live-dot{width:7px;height:7px;border-radius:50%;background:#5fbf7a;box-shadow:0 0 7px 1px #5fbf7a99;
        animation:novaLivePulse 2.4s ease-in-out infinite;flex:none}
      @keyframes novaLivePulse{0%,100%{opacity:1}50%{opacity:.45}}
      @media (prefers-reduced-motion: reduce){.live-dot{animation:none}}
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

      /* Top nav (v7.94.0) */
      .top-nav{display:flex;gap:4px;background:var(--surface);border:1px solid var(--line-soft);border-radius:11px;padding:4px}
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
      .settings-grid{max-width:1100px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:14px}
      @media (max-width:720px){.settings-grid{grid-template-columns:1fr}}
      .settings-card{align-self:start}
      .stub-tag{font-family:var(--font-mono);font-size:9px;letter-spacing:.08em;color:var(--ink-faint);
        background:var(--surface-2);border:1px solid var(--line-soft);border-radius:20px;padding:2px 8px;margin-left:8px;vertical-align:middle}
      .stub-body{font-size:12.5px;color:var(--ink-dim);line-height:1.5}
      .stub-where{display:block;margin-top:6px;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint)}
      .cfg-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
      .cfg-row label{font-size:12.5px;color:var(--ink-dim)}
      select.cfg-field,input.cfg-field{background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:12px;padding:6px 9px;border-radius:8px}
      input.cfg-field:hover,input.cfg-field:focus,select.cfg-field:hover,select.cfg-field:focus{border-color:var(--gold);outline:none}
      .cfg-num{width:84px;min-width:0;text-align:right}
      .mode-grid{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
      .mode-chip{font-family:var(--font-mono);font-size:11px;text-transform:uppercase;letter-spacing:.04em;
        padding:6px 12px;border-radius:8px;border:1px solid var(--line-soft);background:var(--surface-2);color:var(--ink-dim);cursor:pointer}
      .mode-chip:hover{border-color:var(--gold)}
      .mode-chip-on{background:var(--ember);border-color:var(--ember);color:var(--gold-pale)}
      .mode-bind-head{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-faint);letter-spacing:.05em;
        text-transform:uppercase;margin:12px 0 8px;padding-top:12px;border-top:1px solid var(--line-soft)}
      .diag-ok{color:#5fbf7a} .diag-warn{color:var(--warn)} .diag-idle{color:var(--ink-dim)}
      .diag-down{color:#ff6b81} .diag-off{color:var(--ink-faint)}
      .new-model-list{display:flex;flex-direction:column;gap:10px}
      .new-model-row{display:grid;grid-template-columns:88px 1fr 1.3fr;gap:6px;align-items:center}
      .model-label{font-family:var(--font-mono);font-size:10px;letter-spacing:.1em;color:var(--ink-faint);text-transform:uppercase}
      .new-model-row .new-prov-select,.new-model-row .new-model-select{width:100%;min-width:0;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--ink);
        font-family:var(--font-body);font-size:11.5px;padding:6px 8px;border-radius:8px}
      .new-model-custom{grid-column:2/4;width:100%;box-sizing:border-box;padding:6px 9px;
        background:var(--surface-2);border:1px solid var(--line-soft);color:var(--gold);
        font-family:var(--font-mono);font-size:11px;border-radius:8px}
      .new-model-custom:focus{outline:none;border-color:var(--gold)}
      .new-model-row .stub-body{grid-column:1/-1;font-size:10.5px;margin-top:2px}
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
      .new-pl-del,.new-excl-ent-del,.new-excl-dom-del,.new-excl-lab-del{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:12px;padding:0}
      .new-pl-del:hover,.new-excl-ent-del:hover,.new-excl-dom-del:hover,.new-excl-lab-del:hover{color:#ff5a5a}
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
    `;
  }
}

if (!customElements.get("nova-panel-new")) {
  customElements.define("nova-panel-new", NovaCommandCenterNew);
}
