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
        <div class="settings-grid" id="settingsGrid">${cards}</div>
    `;
  }

  _settingsCardHtml(c) {
    const body = c.real
      ? (c.id === "general" ? this._generalCardBody()
        : c.id === "person_honorifics" ? this._personHonorificsCardBody()
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
        : c.id === "security_alarm" ? this._securityAlarmCardBody()
        : c.id === "sentinel_rules" ? this._sentinelRulesCardBody()
        : c.id === "hazard_monitor" ? this._hazardMonitorCardBody()
        : c.id === "energy_management" ? this._energyManagementCardBody()
        : c.id === "host_health" ? this._hostHealthCardBody()
        : c.id === "appliances" ? this._appliancesCardBody()
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
        <label>Language</label>
        <select id="uiLanguage" class="cfg-field" data-cfg-key="ui_language">
          ${this._optSelect([
            ["auto", "Auto (Home Assistant)"], ["en", "English"], ["cs", "Čeština"],
            ["da", "Dansk"], ["de", "Deutsch"], ["es", "Español"], ["fi", "Suomi"],
            ["fr", "Français"], ["it", "Italiano"], ["nb", "Norsk bokmål"],
            ["nl", "Nederlands"], ["pl", "Polski"], ["pt", "Português"],
            ["pt-br", "Português (Brasil)"], ["ro", "Română"], ["ru", "Русский"],
            ["sk", "Slovenčina"], ["sv", "Svenska"], ["tr", "Türkçe"],
            ["uk", "Українська"],
          ], cfg.ui_language || "auto")}
        </select>
      </div>
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
            ${opts.map(o => `<option value="${this._esc(o)}"${!isCustom && o === current ? " selected" : ""}>${this._esc(o[0].toUpperCase() + o.slice(1))}</option>`).join("")}
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
      <div class="stub-body">Detailed room layout is edited in the Floor Plan Editor. This feeds the Residence 3D view.</div>`;
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
    try {
      this._setupHealth = await this._hass.callWS({ type: "nova/get_setup_health" });
    } catch (_) { this._setupHealth = { error: true }; }
    try {
      const activity = await this._hass.callWS({ type: "nova/get_provider_activity", days: 7 });
      this._providerActivity = activity.days || [];
    } catch (_) { this._providerActivity = null; }
    if (this._currentTab === "settings") this._render();
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
        const tokens = (e.avg_input_tokens != null || e.avg_output_tokens != null)
          ? ` · avg tokens in/out ${e.avg_input_tokens ?? "—"}/${e.avg_output_tokens ?? "—"}`
          : "";
        return `<div class="stub-body" style="margin:2px 0">
            ${this._esc(e.provider)}/${this._esc(e.model)} (${this._esc(e.role)}, ${this._esc(e.location)}) —
            ${e.call_count} call${e.call_count === 1 ? "" : "s"},
            ${e.success_count} ok / ${e.failure_count} failed,
            avg ${e.avg_latency_ms ?? "—"}ms${tokens}
          </div>`;
      }).join("");
      return `<div class="cfg-row"><label>${this._esc(d.day)}</label></div>${entries}`;
    }).join("");
    return `<div class="panel-head"><div class="panel-title">Provider Activity</div></div>${rows}`;
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
        <input class="cfg-field ai-endpoint" data-endpoint-provider="ollama" type="text" value="${this._esc(ollamaEndpoint)}" placeholder="http://host:11434">
        <button class="mode-chip ai-endpoint-test" data-endpoint-provider="ollama">TEST</button>
      </div>
      <div class="stub-body ai-endpoint-status" data-endpoint-status="ollama"></div>
      <div class="cfg-row" data-endpoint-row="custom">
        <label>OpenAI-compatible</label>
        <input class="cfg-field ai-endpoint" data-endpoint-provider="custom" type="text" value="${this._esc(customEndpoint)}" placeholder="https://host/v1">
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
    warnEl.textContent = warning || "";
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
        opts += `<option value="${this._esc(cur)}" selected>${this._esc(cur)} — not in the live list</option>`;
        opts += models.map(m => `<option value="${this._esc(m)}">${this._esc(label(m))}</option>`).join("");
      } else {
        opts += models.map(m => `<option value="${this._esc(m)}"${m === cur ? " selected" : ""}>${this._esc(label(m))}</option>`).join("");
      }
    } else {
      const err = res && res.error ? ` — ${String(res.error).slice(0, 48)}` : "";
      opts += (cur ? `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>` : "");
      opts += `<option value="" disabled>no models found${this._esc(err)}</option>`;
    }
    opts += `<option value="__custom__">✎ Custom…</option>`;
    selectEl.innerHTML = opts;
    selectEl.title = (res && res.truncated)
      ? "The provider returned more models than fit in one page — list may be incomplete." : "";
    const row = selectEl.closest(".new-model-row");
    if (row) this._updateRoleWarning(row);
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

