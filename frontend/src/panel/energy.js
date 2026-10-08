  // ─── Energy ───────────────────────────────────────────────────────────
  // Energy Management, Solar and Appliances on one tab. Energy status is
  // fetched on every entry to the tab, after set_agency and on Refresh. It
  // is not polled. A fetch redraws only #energyStatusBody, never the whole
  // tab: a full _render() would wire the tab again, fetch again and loop,
  // and it would wipe an appliance row that has not been saved yet. Solar
  // comes from this._solar, which _fetchLiveData already refreshes.
  _htmlEnergy() {
    const cfg = this._data()?.config || {};
    const e = this._energy || {};
    return `
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

        <div class="panel" id="solarPanel">
          <div class="panel-head">
            <div class="panel-title">Solar</div>
            <div class="panel-meta" id="solarSufficiency">—</div>
          </div>
          <div id="solarBody" class="stub-body">Loading…</div>
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
    box.innerHTML = this._energyStatusHtml();
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
  }
