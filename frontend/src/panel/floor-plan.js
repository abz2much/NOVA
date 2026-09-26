  // ─── Floor Plan Editor: rooms only (v7.101.16) ──────────────────────────
  // Ported from Classic's _renderFloorPlanEditor/_renderEditableSVG/
  // _wireFloorPlanDrag — same floor_plan_rooms config, same working-copy
  // pattern, room drag/resize/add/remove/save/reset. Property line, outdoor
  // zones, camera placement, and the AI camera-coverage feature stay
  // Classic-only for now ("Edit advanced layout in Classic" below) — ported
  // separately later if it turns out to matter.

  _defaultFloorPlan() {
    return {
      "1f": {
        label: "1st Floor", viewBox: "0 0 320 150",
        rooms: [
          { name: "Garage", x: 5, y: 5, w: 100, h: 88, type: "room" },
          { name: "Kitchen", x: 115, y: 5, w: 65, h: 40, type: "room" },
          { name: "Bath", x: 185, y: 5, w: 28, h: 22, type: "bath" },
          { name: "Guest Room", x: 218, y: 5, w: 95, h: 40, type: "room" },
          { name: "Dining Room", x: 115, y: 50, w: 65, h: 38, type: "room" },
          { name: "Stairs", x: 185, y: 32, w: 28, h: 32, type: "stairs" },
          { name: "Living Room", x: 218, y: 50, w: 95, h: 38, type: "room" },
          { name: "Downstairs Hallway", x: 115, y: 93, w: 198, h: 20, type: "room" },
          { name: "Front Door", x: 185, y: 117, w: 50, h: 12, type: "door" },
        ],
      },
      "2f": {
        label: "2nd Floor", viewBox: "0 0 320 140",
        rooms: [
          { name: "Bedroom 2", x: 50, y: 25, w: 95, h: 80, type: "room" },
          { name: "Bath", x: 150, y: 25, w: 30, h: 40, type: "bath" },
          { name: "Master Bedroom", x: 185, y: 25, w: 85, h: 80, type: "room" },
          { name: "Upstairs Hallway", x: 150, y: 70, w: 30, h: 35, type: "room" },
          { name: "Stairs", x: 150, y: 108, w: 25, h: 20, type: "stairs" },
        ],
      },
      "bsmt": {
        label: "Basement", viewBox: "0 0 320 130",
        rooms: [
          { name: "Basement", x: 50, y: 10, w: 220, h: 90, type: "room" },
          { name: "Stairs", x: 120, y: 20, w: 28, h: 35, type: "stairs" },
        ],
        labels: [
          { text: "SUMP PUMP", x: 95, y: 55 }, { text: "DEHUMIDIFIER", x: 95, y: 75 },
          { text: "HOME ENERGY", x: 235, y: 55 }, { text: "WASHER", x: 235, y: 75 },
        ],
      },
    };
  }

  _getFloorPlan() {
    try {
      const raw = this._data()?.config?.floor_plan_rooms;
      if (raw) {
        const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (parsed && typeof parsed === "object" && Object.keys(parsed).length) return parsed;
      }
    } catch (_) {}
    return this._defaultFloorPlan();
  }

  _getEditingPlan() {
    if (this._editingPlan) return this._editingPlan;
    this._editingPlan = JSON.parse(JSON.stringify(this._getFloorPlan()));
    return this._editingPlan;
  }

  _fpUnits() { return (this._data()?.config?.floor_plan_units === "metric") ? "metric" : "imperial"; }
  _fpUnitLabel() { return this._fpUnits() === "metric" ? "m" : "ft"; }
  _fpToReal(u) {
    const ft = (u || 0) * 0.2;
    return this._fpUnits() === "metric" ? Math.round(ft * 0.3048 * 10) / 10 : Math.round(ft * 10) / 10;
  }
  _fpDim(u) { return this._fpToReal(u) + (this._fpUnits() === "metric" ? "m" : "'"); }

  // Devices pinned on the floor plan (Floor Plan Editor Phase 2, v7.101.18) —
  // live-state markers, drag to move, tap to open HA's more-info.
  _getFloorEntities() {
    const raw = this._data()?.config?.floor_plan_entities;
    let e = {};
    try { e = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { e = {}; }
    return e || {};
  }
  _getEditingEntities() {
    if (this._editingEntities) return this._editingEntities;
    this._editingEntities = JSON.parse(JSON.stringify(this._getFloorEntities()));
    return this._editingEntities;
  }
  _entsFor(floor) {
    const e = this._getEditingEntities();
    if (!Array.isArray(e[floor])) e[floor] = [];
    return e[floor];
  }
  _entMarkerStyle(eid) {
    const st = this._hass && this._hass.states ? this._hass.states[eid] : null;
    const dom = (eid.split(".")[0] || "");
    const dim = "var(--ink-faint)";
    if (!st) return { color: dim, name: (eid.split(".")[1] || eid), val: "—" };
    const s = st.state, dc = (st.attributes && st.attributes.device_class) || "";
    const name = (st.attributes && st.attributes.friendly_name) || eid;
    let color = dim, val = s;
    const offish = ["off", "unavailable", "unknown", "idle", "standby", "none"];
    if (dom === "sensor") {
      const u = (st.attributes && st.attributes.unit_of_measurement) || "";
      val = (s === "unknown" || s === "unavailable") ? "—" : (s + u);
      color = "var(--gold)";
    } else if (dom === "binary_sensor") {
      const on = s === "on";
      if (["door", "window", "garage_door", "opening"].indexOf(dc) >= 0) { color = on ? "var(--warn)" : dim; val = on ? "OPEN" : "SHUT"; }
      else if (["motion", "occupancy", "presence"].indexOf(dc) >= 0) { color = on ? "var(--gold)" : dim; val = on ? "DET" : "—"; }
      else { color = on ? "var(--gold)" : dim; val = on ? "ON" : "OFF"; }
    } else if (dom === "lock") { const locked = s === "locked"; color = locked ? dim : "#ff5a5a"; val = locked ? "LOCK" : "OPEN"; }
    else if (dom === "cover") { const open = s === "open" || s === "opening"; color = open ? "var(--warn)" : dim; val = open ? "OPEN" : "SHUT"; }
    else if (dom === "person" || dom === "device_tracker") { const home = s === "home"; color = home ? "var(--gold)" : dim; val = home ? "HOME" : "AWAY"; }
    else if (dom === "climate") { color = "var(--gold)"; const t = st.attributes && st.attributes.current_temperature; val = (t != null) ? (t + "°") : s; }
    else { const on = offish.indexOf(s) < 0; color = on ? "var(--gold)" : dim; val = on ? "ON" : "OFF"; }
    return { color, name, val };
  }
  _fpBgOpacity() {
    const op = parseFloat(this._data()?.config?.floor_plan_bg_opacity);
    return (isFinite(op) && op >= 0 && op <= 1) ? op : 0.2;
  }

  // Property line + outdoor zones (Phase 3a) — same geometry helpers as
  // Classic, same floor_plan_property config, same zone-as-polygon-room
  // representation in floor_plan_rooms.
  _zonePoints(r) {
    if (r && Array.isArray(r.points) && r.points.length >= 3) return r.points;
    const x = r.x || 0, y = r.y || 0, w = r.w || 40, h = r.h || 40;
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
  }
  _ensureZonePoints(rm) {
    if (!Array.isArray(rm.points) || rm.points.length < 3) rm.points = this._zonePoints(rm).map(p => [p[0], p[1]]);
    return rm.points;
  }
  _syncRoomBBox(rm) {
    if (!rm || !Array.isArray(rm.points) || rm.points.length < 3) return;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    rm.points.forEach(p => { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); });
    rm.x = Math.round(x0); rm.y = Math.round(y0); rm.w = Math.round(x1 - x0); rm.h = Math.round(y1 - y0);
  }
  _propPathD(pts) { return pts.map((p, k) => (k ? "L" : "M") + p[0] + " " + p[1]).join(" ") + " Z"; }
  _getProperty() {
    const raw = this._data()?.config?.floor_plan_property;
    let p = null;
    try { p = typeof raw === "string" ? (raw ? JSON.parse(raw) : null) : (raw || null); } catch (_) { p = null; }
    return (p && Array.isArray(p.points)) ? p.points : [];
  }
  _propertyPts() {
    if (!this._editingProperty) this._editingProperty = JSON.parse(JSON.stringify(this._getProperty()));
    return this._editingProperty;
  }
  _setProperty(pts) { this._editingProperty = pts; }
  _propertyArea(pts) {
    if (!pts || pts.length < 3) return "";
    let a = 0;
    for (let i = 0; i < pts.length; i++) { const p = pts[i], q = pts[(i + 1) % pts.length]; a += p[0] * q[1] - q[0] * p[1]; }
    const sqFt = Math.abs(a) / 2 * 0.04;
    if (this._fpUnits() === "metric") {
      const sqM = sqFt * 0.092903;
      return sqM >= 10000 ? (sqM / 10000).toFixed(2) + " ha" : Math.round(sqM).toLocaleString() + " m²";
    }
    return sqFt >= 43560 ? (sqFt / 43560).toFixed(2) + " acres" : Math.round(sqFt).toLocaleString() + " sq ft";
  }

  // Windows/doors/dormers ("openings", Phase 3c) — same floor_plan_elements
  // config and geometry as Classic. Feeds _planGeometry's wall gaps below,
  // so AI camera-coverage now accounts for doorways instead of treating
  // every wall as solid.
  _getFloorElements() {
    const raw = this._data()?.config?.floor_plan_elements;
    let el = {};
    try { el = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { el = {}; }
    return el || {};
  }
  _getEditingElements() {
    if (this._editingElements) return this._editingElements;
    this._editingElements = JSON.parse(JSON.stringify(this._getFloorElements()));
    return this._editingElements;
  }
  _elemsFor(floor) {
    const el = this._getEditingElements();
    if (!Array.isArray(el[floor])) el[floor] = [];
    return el[floor];
  }
  _doorEntityOptions(selected) {
    const states = this._hass?.states || {};
    const cands = [];
    const OPEN_DC = ["door", "window", "garage_door", "opening"];
    const OPEN_RE = /door|garage|gate|cellar|bulkhead|hatch|window|contact|entry|slider|sash|casement|patio|french|skylight|opening|sliding/i;
    Object.keys(states).forEach(eid => {
      const dom = eid.split(".")[0];
      const at = states[eid].attributes || {};
      const dc = at.device_class || "";
      const fn = at.friendly_name || "";
      const ok = dom === "cover" || dom === "lock"
        || (dom === "binary_sensor" && (OPEN_DC.includes(dc) || OPEN_RE.test(eid) || OPEN_RE.test(fn)));
      if (ok) cands.push(eid);
    });
    cands.sort();
    if (selected && !cands.includes(selected)) cands.unshift(selected);
    const opts = cands.map(eid => `<option value="${this._esc(eid)}"${eid === selected ? " selected" : ""}>${this._esc(this._entName(eid))}</option>`).join("");
    return `<option value=""${selected ? "" : " selected"}>— auto-detect —</option>${opts}`;
  }

  // Cameras + AI coverage (Phase 3b) — same config/geometry as Classic.
  _getFloorCameras() {
    const raw = this._data()?.config?.floor_plan_cameras;
    let c = {};
    try { c = typeof raw === "string" ? (raw ? JSON.parse(raw) : {}) : (raw || {}); } catch (_) { c = {}; }
    return c || {};
  }
  _getEditingCameras() {
    if (this._editingCameras) return this._editingCameras;
    this._editingCameras = JSON.parse(JSON.stringify(this._getFloorCameras()));
    return this._editingCameras;
  }
  _camsFor(floor) {
    const c = this._getEditingCameras();
    if (!Array.isArray(c[floor])) c[floor] = [];
    return c[floor];
  }
  _cameraEntityOptions(selected) {
    const states = this._hass?.states || {};
    const eids = Object.keys(states).filter(e => e.startsWith("camera.")).sort();
    if (selected && !eids.includes(selected)) eids.unshift(selected);
    const opts = eids.map(e => {
      const st = states[e];
      const fn = (st && st.attributes && st.attributes.friendly_name) || e;
      return `<option value="${this._esc(e)}"${e === selected ? " selected" : ""}>${this._esc(fn)}</option>`;
    }).join("");
    return `<option value="">— camera —</option>${opts}`;
  }
