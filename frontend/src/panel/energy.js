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
    if (msg.textContent !== note) msg.textContent = note;
    msg.hidden = !note;
    list.hidden = !!note;
    if (note) return;
    list.querySelectorAll(".energy-tile[data-today]").forEach(tile => {
      const key = tile.getAttribute("data-today");
      const v = t[key];
      const text = key === "self_sufficiency_pct"
        ? (v == null ? "—" : `${Math.round(v)}%`) : this._energyKwh(v);
      const dd = tile.querySelector(".energy-tile-w");
      if (dd && dd.textContent !== text) dd.textContent = text;
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
      if (el.textContent !== t) el.textContent = t;
      if (el.classList.contains("battery-line")) el.hidden = !t;
    };
    const pct = b.pct != null ? Math.round(b.pct) : null;
    const word = this._energyFlowWord(b.state);
    const power = b.w != null && b.state && b.state !== "idle" ? this._energyFlowWatts(b.w) : "";
    setText("batteryPct", pct != null ? `${pct}%` : "—");
    setText("batteryState", [word, power].filter(Boolean).join(" · "));
    setText("batteryStored", b.stored_kwh != null && b.capacity_kwh != null
      ? `${Number(b.stored_kwh).toFixed(1)} kWh of ${Number(b.capacity_kwh).toFixed(1)} kWh` : "");
    setText("batteryEta", b.eta_min
      ? `About ${this._energyDuration(b.eta_min)} ${b.eta_to === "full" ? "to full" : "left"} at this rate` : "");
    const fill = tank.querySelector(".tank-fill");
    const level = String(pct != null ? Math.max(0, Math.min(100, pct)) : 0);
    if (fill && fill.style.getPropertyValue("--tank-pct") !== level) fill.style.setProperty("--tank-pct", level);
    const label = `Battery ${pct != null ? `${pct} percent` : "no reading"}${
      b.state && b.state !== "idle" && b.w != null ? `, ${b.state} at ${this._energyFlowWatts(b.w)}`
        : b.state === "idle" ? ", idle" : ""}.`;
    if (tank.getAttribute("aria-label") !== label) tank.setAttribute("aria-label", label);
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
    return { charging: "Charging", discharging: "Discharging", importing: "Importing",
      exporting: "Exporting", idle: "Idle" }[state] || "";
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
    if (msg.textContent !== note) msg.textContent = note;
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
      if (wEl && wEl.textContent !== wText) wEl.textContent = wText;
      if (stEl && stEl.textContent !== state) stEl.textContent = state;
    });
    this._announceEnergyFlow(f);
  }

  // Updates the diagram in place: text, data-dir, data-state, the two CSS
  // variables and the battery arc. Never rebuilds the SVG, which would
  // restart every animation.
  _renderEnergyDiagram(f) {
    const svg = this.shadowRoot?.getElementById("energyFlowSvg");
    if (!svg) return;
    const setText = (el, t) => { if (el && el.textContent !== t) el.textContent = t; };
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
      lines.push(`${kind === "battery" ? "Battery" : "Grid"} ${this._energyFlowWord(cur[kind]).toLowerCase()}.`);
    });
    const region = this.shadowRoot?.getElementById("energyLiveAnnounce");
    if (region && lines.length) region.textContent = lines.join(" ");
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
    if (el && el.textContent !== t) el.textContent = t;
  }

  _outlookSetHtml(el, html) {
    if (el && el._novaHtml !== html) { el.innerHTML = html; el._novaHtml = html; }
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
    const bwText = bw ? `Best time for a big appliance: ${this._outlookTime(bw.start)} to ${this._outlookTime(bw.end)} (${bw.reason})` : "";
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
