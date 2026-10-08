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
      ${credits[source] ? `<div class="toggle-desc hazard-source-credit">Source: ${this._esc(credits[source])}</div>` : ""}
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
        <div class="toggle-desc hazard-source">Source: ${this._esc(w.source_label || "")}</div>
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
          <label>${this._esc(m.label)}${m.recommended ? "" : ` <span class="toggle-desc">optional</span>`}</label>
          <span style="font-family:var(--font-mono);font-size:11px">${valueText}${stale ? " (stale)" : ""}</span>
          <span class="${STATUS_CLS[m.status] || "diag-off"}">${STATUS_LABEL[m.status] || (m.status || "").toUpperCase()}</span>
        </div>
        ${selectHtml ? `<div class="cfg-row">${selectHtml}</div>` : ""}`;
    };

    const setupNotes = metrics.filter(m => m.recommended && (m.status === "missing" || m.status === "disabled"));
    const setupGuidance = setupNotes.length ? `
      <div class="stub-body">Missing or disabled recommended readings: ${setupNotes.map(m => this._esc(m.label)).join(", ")}. In Home Assistant: Settings → Devices &amp; services → System Monitor → its entities → enable the ones you want (System Monitor disables several by default), then reopen this card.</div>` : "";

    return `
      <div class="stub-body">Reads Home Assistant's own System Monitor sensors for the machine Nova runs on — processor/memory/disk usage, memory &amp; I/O pressure, and (if your hardware exposes it) temperature. Off by default; nothing is read or reported until you turn it on. Disk usage measures capacity, not drive health; I/O pressure measures workload contention, not drive failure. Nova cannot warn you after this machine has completely frozen, since Nova runs on it too.</div>
      <div class="cfg-row">
        <label>Host health awareness</label>
        <button class="toggle-btn ${enabled ? "on" : "off"}" data-cfg-key="host_health_enabled" data-cfg-val="${enabled ? "false" : "true"}">${enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Alerts <span class="toggle-desc">speak/push only once a problem persists — turn off to silence immediately</span></label>
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
      <div class="stub-body">A separate check every 15 minutes of a fixed set of server and network switch sensors. Sensors that don't exist here are skipped. Pick a room for its alerts, or leave none to only log them.</div>
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
      return `<div class="stub-body">✓ All clear near ${res.center ? res.center[0] + ", " + res.center[1] : "home"} — no weather warnings or other hazards from the sources that are on.</div>`;
    }
    let html = warn.length ? this._renderHazardWarnings(warn) : "";
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
  _calendarDatalist() {
    const states = this._hass?.states || {};
    return Object.keys(states).filter(eid => eid.startsWith("calendar.")).sort()
      .map(eid => `<option value="${this._esc(eid)}">${this._esc(this._entName(eid))}</option>`).join("");
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
    const open = !!this._camListOpen;
    const head = `
      <div class="cfg-row">
        <button class="new-cam-collapse" id="newCamListToggle" aria-expanded="${open}">
          <span class="new-cam-caret">${open ? "▾" : "▸"}</span> ${nOn} of ${cams.length} cameras in use
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
            ${chip("auto", `AUTO (${resolved})`)}
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
        <button class="toggle-btn ${cfg.package_detection !== false ? "on" : "off"}" data-cfg-key="package_detection" data-cfg-val="${cfg.package_detection !== false ? "false" : "true"}">${cfg.package_detection !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="cfg-row">
        <label>Visitor Learning — silently log strangers seen at the door</label>
        <button class="toggle-btn ${cfg.visitor_learning !== false ? "on" : "off"}" data-cfg-key="visitor_learning" data-cfg-val="${cfg.visitor_learning !== false ? "false" : "true"}">${cfg.visitor_learning !== false ? "ON" : "OFF"}</button>
      </div>
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
      <div class="stub-body">${total} analysed · ${notable} notable · ${this._esc(srcLine)}</div>
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
          ${this._optSelect([["0", "Plain — no wit"], ["1", "Dry — occasional wit (default)"], ["2", "Full — expressive wit"]], String(cfg.banter_level ?? "1"))}
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
      return `<div class="stub-body">No documents ingested yet. Add PDF/.txt/.md files to <code>${this._esc(d.directory || "nova/documents in your config folder")}</code> and press Ingest.${d.chroma ? "" : " (Vector search needs ChromaDB; keyword fallback is active.)"}</div>`;
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
      <div class="stub-body">Enable semantic search to match on meaning, using your Ollama server (${this._esc(v.model || "nomic-embed-text")}). No install, no ChromaDB. Re-ingest afterward to embed existing docs.</div>
      <div class="cfg-row"><button class="mode-chip" id="newVecbkToggle" data-mode="enable">⬆ ENABLE SEMANTIC SEARCH</button></div>`;
  }

  _documentLibraryCardBody() {
    const d = this._docLib || {};
    const backend = d.chroma ? "VECTOR" : d.fts ? "KEYWORD" : "NONE";
    return `
      <div class="stub-body">Drop manuals &amp; receipts (PDF, .txt, .md) into <code>${this._esc(d.directory || "nova/documents in your config folder")}</code> or upload below, then ingest. Ask Nova "what's the furnace filter size?" and it answers from your paperwork.</div>
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
        ingestBtn.textContent = "⟳ INGESTING…";
        try {
          await this._hass.callWS({ type: "nova/documents", action: "ingest" });
          await this._fetchDocLibrary();
        } catch (err) {
          console.error("Nova: ingest failed", err);
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
          console.error("Nova: document upload failed", err);
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
          console.error("Nova: watch scan failed", err);
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
          console.error("Nova: semantic search toggle failed", err);
        }
        await this._fetchVectorBackend();
      });
    }
  }
