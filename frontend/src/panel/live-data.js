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
      onboardingMount.innerHTML = this._onboardingHtml(d.onboarding);
      this._wireOnboarding();
    }

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

    // Formal lockdown is deliberately separate from the alarm controls. It
    // only calls Nova's guarded lockdown command and always asks for a human
    // confirmation before changing state.
    const lockdown = d.lockdown || {};
    const lockdownBtn = root.getElementById("lockdownControl");
    if (lockdownBtn) {
      lockdownBtn.hidden = false;
      lockdownBtn.classList.toggle("active", !!lockdown.active);
      lockdownBtn.textContent = lockdown.active ? "LOCKDOWN ACTIVE" : "LOCKDOWN OFF";
      lockdownBtn.title = lockdown.reason || "Nova formal lockdown";
    }

    // activity feed
    const entries = (this._activityData && this._activityData.length)
      ? this._activityData
      : [{ ts: "--:--", tag: "SYSTEM", msg: "No activity yet." }];
    const feedEl = root.getElementById("feed");
    if (feedEl) {
      feedEl.innerHTML = entries.map(e => `
        <div class="feed-row">
          <div class="feed-text"><b>${this._esc(e.tag || "")}</b> · <span class="dim">${this._esc(e.msg || "")}</span></div>
          <div class="feed-time">${this._esc(e.ts || "")}</div>
        </div>`).join("");
    }
    const feedMeta = root.getElementById("feedMeta");
    if (feedMeta) feedMeta.textContent = `LAST ${entries.length}`;

    // areas
    const areasGridEl = root.getElementById("areasGrid");
    if (areasGridEl) {
      areasGridEl.innerHTML = (d.areas || []).map(a => this._areaTileHtml(a)).join("");
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
    if (areasMeta) areasMeta.textContent = `${d.occupied} OCCUPIED · ${d.areasMonitored} MONITORED`;

    this._renderSolarPanel();

    const cog = this._cognitive || {};
    const learning = cog.learning || {};
    const cognitiveState = root.getElementById("cognitiveState");
    if (cognitiveState) cognitiveState.textContent = cog.running === false ? "STOPPED" : (cog.running ? "RUNNING" : "UNAVAILABLE");
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
      cognitiveMetrics.innerHTML = metrics.map(([label, value]) =>
        `<div class="metric"><b>${this._esc(value)}</b><span>${this._esc(label)}</span></div>`).join("");
    }
    const cognitiveAnalysis = root.getElementById("cognitiveAnalysis");
    if (cognitiveAnalysis) {
      const analysis = cog.last_analysis || {};
      cognitiveAnalysis.textContent = analysis.summary || analysis.message || "Nova learns from household patterns locally.";
    }

    const goals = d.goals || [];
    const goalList = root.getElementById("goalList");
    const goalsMeta = root.getElementById("goalsMeta");
    if (goalsMeta) goalsMeta.textContent = `${goals.filter(g => g.status === "active").length} ACTIVE`;
    if (goalList) {
      goalList.innerHTML = goals.length ? goals.map(g => {
        const active = g.status === "active";
        const progress = g.steps_total ? `${g.steps_done || 0}/${g.steps_total} STEPS` : "OPEN OUTCOME";
        return `<div class="goal-row">
          <div class="goal-copy"><b>${this._esc(g.title || g.outcome || `Goal ${g.id}`)}</b>
            <span>${this._esc(g.outcome || "")}</span>
            <small>${this._esc(String(g.status || "active").toUpperCase())} · ${this._esc(progress)}</small></div>
          <button class="mode-chip goal-action" data-goal-id="${this._esc(g.id)}" data-goal-action="${active ? "cancel" : "delete"}">${active ? "CANCEL" : "DELETE"}</button>
        </div>`;
      }).join("") : `<div class="empty-state">No goals yet.</div>`;
      this._wireGoalActions();
    }

    // camera — collapsed, optional, honest
    const camPanel = root.getElementById("cameraPanel");
    const camStrip = root.getElementById("camStrip");
    if (camPanel && camStrip) {
      const cams = d.cameras || [];
      camPanel.hidden = cams.length === 0;
      const camToggle = root.getElementById("camToggle");
      if (camToggle) camToggle.textContent = this._camOpen ? "HIDE CAMERAS ▴" : `SHOW ${cams.length} CAMERA${cams.length === 1 ? "" : "S"} ▾`;
      camStrip.classList.toggle("open", this._camOpen);
      camStrip.innerHTML = cams.map(c => {
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
      }).join("");
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
      if (!window.confirm(`${action === "cancel" ? "Cancel" : "Delete"} this goal?`)) return;
      await this._goalAction({ action, goal_id: Number(btn.getAttribute("data-goal-id")) });
    }));
  }

  async _goalAction(payload) {
    const out = this.shadowRoot.getElementById("goalResult");
    try {
      const res = await this._hass.callWS({ type: "nova/goal_action", ...payload });
      if (this._liveData && Array.isArray(res?.goals)) this._liveData.goals = res.goals;
      if (out) out.textContent = "Saved.";
      this._renderData();
    } catch (err) {
      if (out) out.textContent = err?.message || "Goal action failed.";
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
      ? `<button class="area-light-toggle${lit ? " on" : ""}"${ctlOn ? ` data-light-area="${this._esc(a.id || "")}" data-area-name="${this._esc(a.name)}"` : " disabled"} title="${a.lights_on}/${a.lights_total} lights on${ctlOn ? " — tap to toggle" : ""}">${lit ? "ON" : "OFF"}</button>`
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

  // Solar (ported from Classic's own Solar card — data was already fetched
  // into this._solar by _fetchLiveData but never rendered anywhere; the new
  // look never actually showed it despite pulling the data every poll).
  _renderSolarPanel() {
    const root = this.shadowRoot;
    const body = root.getElementById("solarBody");
    const sufficiencyEl = root.getElementById("solarSufficiency");
    if (!body) return;
    const s = this._solar || {};
    if (!s || s.error) {
      body.innerHTML = `<div class="stub-body">Couldn't load solar data — restart Home Assistant after updating.</div>`;
      if (sufficiencyEl) sufficiencyEl.textContent = "—";
      return;
    }
    if (!s.configured) {
      body.innerHTML = `<div class="stub-body">${this._esc((s.advice || [])[0] || "No solar source configured yet.")}</div>`;
      if (sufficiencyEl) sufficiencyEl.textContent = "—";
      return;
    }
    if (sufficiencyEl) {
      sufficiencyEl.textContent = s.self_sufficiency_pct != null
        ? `${s.self_sufficiency_pct}% self-sufficient` : "—";
    }
    const rows = [];
    if (s.solar_w != null) {
      rows.push(`<div class="feed-row"><span class="feed-text">Solar</span><span class="feed-time">${(s.solar_w / 1000).toFixed(2)} kW</span></div>`);
    }
    if (s.grid_w != null) {
      const dirLabel = s.grid_direction === "export" ? "Exporting" : s.grid_direction === "import" ? "Importing" : "Balanced";
      rows.push(`<div class="feed-row"><span class="feed-text">Grid</span><span class="feed-time">${dirLabel} ${(Math.abs(s.grid_w) / 1000).toFixed(2)} kW</span></div>`);
    }
    if (s.battery_w != null || s.battery_pct != null) {
      const pct = s.battery_pct != null ? `${s.battery_pct}%` : "no % available";
      rows.push(`<div class="feed-row"><span class="feed-text">Battery</span><span class="feed-time">${pct}${s.battery_w != null ? ` · ${(s.battery_w / 1000).toFixed(2)} kW` : ""}</span></div>`);
    }
    const advice = (s.advice || []).map(a => `<div class="toggle-desc" style="margin-bottom:6px">${this._esc(a)}</div>`).join("");
    body.innerHTML = advice + rows.join("");
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
      btn.textContent = "Analyzing…";
      if (out) out.textContent = "Running pattern analysis over your history…";
      try {
        const res = await this._hass.callWS({ type: "nova/run_analysis" });
        const bf = res.backfill || {};
        const bfNote = bf.imported ? `<br>Imported ${bf.imported} past event${bf.imported === 1 ? "" : "s"} from history for ${bf.entities} new entit${bf.entities === 1 ? "y" : "ies"}.` : "";
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
                msg += `<br>Closest routine: <b>${this._esc(cand.entity_id)}</b> → ${this._esc(cand.state)} ~${hr}:00, seen ${cand.days}/${dg.total_days} days (${progress}).`;
              }
              const src = (dg.top_sources || [])[0];
              if (src) msg += `<br>Busiest source: ${this._esc(src.entity_id)} (${src.changes} changes).`;
            }
            const nm = Array.isArray(res.near_misses) ? res.near_misses : [];
            if (nm.length) {
              msg += `<br>Building toward suggestions:`;
              msg += nm.slice(0, 5).map(m => {
                const prog = m.needed ? ` (${m.occurrences}/${m.needed})` : ` (${m.occurrences}×)`;
                return `<br>• ${this._esc(m.description || m.type)}${prog}`;
              }).join("");
            }
            out.innerHTML = msg + bfNote;
          } else {
            out.innerHTML = `✕ ${this._esc(res.reason || res.error || "Analysis did not run.")}` + bfNote;
          }
        }
        try { await this._fetchLiveData(); } catch (_) {}
      } catch (err) {
        if (out) out.innerHTML = `✕ ${this._esc(err?.message || String(err))}`;
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
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

