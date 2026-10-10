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
