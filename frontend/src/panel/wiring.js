  _wire() {
    const root = this.shadowRoot;
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
      if (!window.confirm(`${active ? "Lift" : "Engage"} Nova lockdown?`)) return;
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
    if (this._currentTab === "memory") { this._wireMemory(); this._fetchKnowledge(); this._fetchPersonRoutines(); }
    if (this._currentTab === "intrusion") this._wireIntrusion();
    if (this._currentTab === "residence") {
      this._build3DHouseNew();
      this._wireResidenceControlsNew();
      this._fetchMmwaveNew();
    }
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
      if (this._liveData?.onboarding) this._liveData.onboarding.show = false;
      if (this._liveData?.config?.onboarding) this._liveData.config.onboarding.show = false;
      this._renderData();
      try { await this._hass.callWS({ type: "nova/update_config", key: "onboarding_dismissed", value: true }); } catch (_) {}
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
    root.querySelectorAll("input.cfg-field[data-cfg-key]").forEach(inp => {
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

    // Operational Mode: mode chips call nova/mode directly (not update_config —
    // same websocket contract Classic's own mode-grid already uses).
    root.querySelectorAll(".mode-grid .mode-chip[data-mode]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const mode = btn.getAttribute("data-mode");
        if (!this._hass || !mode || btn.classList.contains("mode-chip-on")) return;
        try {
          await this._hass.callWS({ type: "nova/mode", action: "set", mode });
        } catch (err) {
          console.error("Nova: failed to set mode", err);
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
          console.error("Nova: doorbell backlog scan failed", err);
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
          console.warn("Nova: no announcement speakers set — choose them in Settings → Announcement Speakers");
          return;
        }
        briefNow.disabled = true;
        const orig = briefNow.textContent;
        briefNow.textContent = "▶ BRIEFING…";
        try {
          await this._hass.callService("nova", "briefing", { announce: true });
        } catch (err) {
          console.error("Nova: briefing failed", err);
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
          console.error("Nova: wellbeing toggle failed", err);
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
          console.error("Nova: failed to set energy agency", err);
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
          console.error(`Nova: service ${svcAttr} failed`, err);
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
          console.error("Nova: camera analyze failed", err);
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
    root.querySelectorAll(".settings-card").forEach(card => {
      const matchesGroup = !q && card.getAttribute("data-settings-group") === this._settingsSection;
      const matchesSearch = q && (card.getAttribute("data-search") || "").includes(q);
      card.hidden = !(matchesGroup || matchesSearch);
    });
  }

