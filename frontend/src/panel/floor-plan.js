  // ─── Floor plan: stored layout, working copies and shared helpers ──────
  // The plan is kept in config as floor_plan_rooms (rooms and outdoor zones
  // per floor), floor_plan_entities (device pins), floor_plan_cameras,
  // floor_plan_elements (windows, doors, dormers) and floor_plan_property
  // (the lot outline). The editor changes deep copies of each and writes
  // them back only on Save.

  // The starting plan for a home with no saved plan. It shows only what the
  // home has (8.26.0 garage, 8.27.0 the rest). A saved plan is never changed.
  _defaultFloorPlan() {
    const cfg = this._data()?.config || {};
    const has = cfg.home_features || {};
    const plan = this._defaultFloorPlanBase();
    // A basement floor only where there is a basement, an upper floor only
    // when Stories is more than 1, and stairs only with a floor to reach.
    // (The basement's old guessed device labels went in 8.27.0.)
    if (!has.basement) delete plan.bsmt;
    if (String(cfg.home_stories ?? "1.5") === "1") delete plan["2f"];
    const ground = plan["1f"];
    if (!plan["2f"] && !plan.bsmt) ground.rooms = ground.rooms.filter(r => r.type !== "stairs");
    if (cfg.has_garage) return plan;
    // With no garage the back door opens from the kitchen at the rear, and
    // the garage's space is a utility room only when Home Assistant has one.
    ground.viewBox = "0 0 320 162";
    ground.rooms = ground.rooms
      .filter(r => r.name !== "Garage" || has.utility)
      .map(r => (r.name === "Garage" ? { ...r, name: "Utility Room", y: r.y + 12 } : { ...r, y: r.y + 12 }));
    ground.rooms.push({ name: "Back Door", x: 125, y: 3, w: 45, h: 10, type: "door" });
    return plan;
  }
  _defaultFloorPlanBase() {
    const ground = [
      { name: "Garage", x: 5, y: 5, w: 100, h: 88, type: "room" },
      { name: "Kitchen", x: 115, y: 5, w: 65, h: 40, type: "room" },
      { name: "Bath", x: 185, y: 5, w: 28, h: 22, type: "bath" },
      { name: "Guest Room", x: 218, y: 5, w: 95, h: 40, type: "room" },
      { name: "Dining Room", x: 115, y: 50, w: 65, h: 38, type: "room" },
      { name: "Stairs", x: 185, y: 32, w: 28, h: 32, type: "stairs" },
      { name: "Living Room", x: 218, y: 50, w: 95, h: 38, type: "room" },
      { name: "Downstairs Hallway", x: 115, y: 93, w: 198, h: 20, type: "room" },
      { name: "Front Door", x: 185, y: 117, w: 50, h: 12, type: "door" },
    ];
    const upper = [
      { name: "Bedroom 2", x: 50, y: 25, w: 95, h: 80, type: "room" },
      { name: "Bath", x: 150, y: 25, w: 30, h: 40, type: "bath" },
      { name: "Master Bedroom", x: 185, y: 25, w: 85, h: 80, type: "room" },
      { name: "Upstairs Hallway", x: 150, y: 70, w: 30, h: 35, type: "room" },
      { name: "Stairs", x: 150, y: 108, w: 25, h: 20, type: "stairs" },
    ];
    const lower = [
      { name: "Basement", x: 50, y: 10, w: 220, h: 90, type: "room" },
      { name: "Stairs", x: 120, y: 20, w: 28, h: 35, type: "stairs" },
    ];
    const floor = (label, height, rooms) => ({ label, viewBox: `0 0 320 ${height}`, rooms });
    return { "1f": floor("1st Floor", 150, ground), "2f": floor("2nd Floor", 140, upper), "bsmt": floor("Basement", 130, lower) };
  }

  // A config value that may be stored as JSON text or as an object. Empty
  // or unreadable text gives null.
  _fpStored(key) {
    const raw = this._data()?.config?.[key];
    try {
      if (typeof raw === "string") return raw ? JSON.parse(raw) : null;
      return raw || null;
    } catch (_) {
      return null;
    }
  }
  // A working copy of one stored part, made on first use and kept until Save.
  _fpWorking(field, read) {
    if (!this[field]) this[field] = JSON.parse(JSON.stringify(read()));
    return this[field];
  }
  // One floor's list inside a per-floor store, created empty when missing.
  _fpFloorList(store, floor) {
    if (!Array.isArray(store[floor])) store[floor] = [];
    return store[floor];
  }

  _getFloorPlan() {
    const saved = this._fpStored("floor_plan_rooms");
    if (saved && typeof saved === "object" && Object.keys(saved).length) return saved;
    return this._defaultFloorPlan();
  }
  _getEditingPlan() { return this._fpWorking("_editingPlan", () => this._getFloorPlan()); }

  // Plan units are a fifth of a foot: one 50 unit grid square is 10 ft.
  _fpUnits() { return this._data()?.config?.floor_plan_units === "metric" ? "metric" : "imperial"; }
  _fpUnitLabel() { return this._fpUnits() === "metric" ? "m" : "ft"; }
  _fpToReal(u) {
    const feet = (u || 0) * 0.2;
    const shown = this._fpUnits() === "metric" ? feet * 0.3048 : feet;
    return Math.round(shown * 10) / 10;
  }
  // The other way: feet or metres typed in a field back to plan units, at
  // least 8 so an opening or a camera's reach never shrinks to nothing.
  _fpFromReal(v) {
    const feet = this._fpUnits() === "metric" ? v / 0.3048 : v;
    return Math.max(8, Math.round(feet * 5));
  }
  _fpDim(u) { return `${this._fpToReal(u)}${this._fpUnits() === "metric" ? "m" : "'"}`; }

  // The box around a list of rooms, from each room's x, y, w and h.
  _fpRoomBox(r) { return { x0: r.x, y0: r.y, x1: r.x + r.w, y1: r.y + r.h }; }
  _fpBounds(rooms) {
    const box = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
    for (const r of rooms) {
      const own = this._fpRoomBox(r);
      box.x0 = Math.min(box.x0, own.x0); box.y0 = Math.min(box.y0, own.y0);
      box.x1 = Math.max(box.x1, own.x1); box.y1 = Math.max(box.y1, own.y1);
    }
    return box;
  }
  // The box around a list of [x, y] points.
  _fpPointBounds(pts) {
    const box = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
    for (const p of pts) {
      box.x0 = Math.min(box.x0, p[0]); box.y0 = Math.min(box.y0, p[1]);
      box.x1 = Math.max(box.x1, p[0]); box.y1 = Math.max(box.y1, p[1]);
    }
    return box;
  }

  // Device pins: live state markers, drag to move, tap for HA's more-info.
  _getFloorEntities() { return this._fpStored("floor_plan_entities") || {}; }
  _getEditingEntities() { return this._fpWorking("_editingEntities", () => this._getFloorEntities()); }
  _entsFor(floor) { return this._fpFloorList(this._getEditingEntities(), floor); }
  // A pin's colour, name and short state. Unknown entities show their id.
  _entMarkerStyle(eid) {
    const dim = "var(--ink-faint)", gold = "var(--gold)", warn = "var(--warn)";
    const [domain, objectId] = eid.split(".");
    const st = this._hass && this._hass.states ? this._hass.states[eid] : null;
    if (!st) return { color: dim, name: objectId || eid, val: "—" };
    const s = st.state, at = st.attributes || {}, name = at.friendly_name || eid;
    const lit = (on, color, val) => ({ color: on ? color : dim, name, val });
    switch (domain) {
      case "sensor":
        return { color: gold, name, val: s === "unknown" || s === "unavailable" ? "—" : s + (at.unit_of_measurement || "") };
      case "binary_sensor": {
        const on = s === "on", dc = at.device_class || "";
        if (["door", "window", "garage_door", "opening"].includes(dc)) return lit(on, warn, on ? "OPEN" : "SHUT");
        if (["motion", "occupancy", "presence"].includes(dc)) return lit(on, gold, on ? "DET" : "—");
        return lit(on, gold, on ? "ON" : "OFF");
      }
      case "lock": {
        const locked = s === "locked";
        return { color: locked ? dim : "var(--bad-strong)", name, val: locked ? "LOCK" : "OPEN" };
      }
      case "cover": {
        const open = s === "open" || s === "opening";
        return lit(open, warn, open ? "OPEN" : "SHUT");
      }
      case "person":
      case "device_tracker": {
        const home = s === "home";
        return lit(home, gold, home ? "HOME" : "AWAY");
      }
      case "climate": {
        const t = at.current_temperature;
        return { color: gold, name, val: t != null ? t + "°" : s };
      }
      default: {
        const on = !["off", "unavailable", "unknown", "idle", "standby", "none"].includes(s);
        return lit(on, gold, on ? "ON" : "OFF");
      }
    }
  }
  _fpBgOpacity() {
    const v = parseFloat(this._data()?.config?.floor_plan_bg_opacity);
    return v >= 0 && v <= 1 ? v : 0.2;
  }

  // Outdoor zones are rooms with a points outline; plain rooms are boxes.
  _fpHasOutline(r) { return !!r && Array.isArray(r.points) && r.points.length >= 3; }
  _zonePoints(r) {
    if (this._fpHasOutline(r)) return r.points;
    const left = r.x || 0, top = r.y || 0, right = left + (r.w || 40), bottom = top + (r.h || 40);
    return [[left, top], [right, top], [right, bottom], [left, bottom]];
  }
  _ensureZonePoints(rm) {
    if (!this._fpHasOutline(rm)) rm.points = this._zonePoints(rm).map(p => p.slice(0, 2));
    return rm.points;
  }
  // Keeps a shaped room's x, y, w and h on the box around its outline.
  _syncRoomBBox(rm) {
    if (!this._fpHasOutline(rm)) return;
    const b = this._fpPointBounds(rm.points);
    rm.x = Math.round(b.x0); rm.y = Math.round(b.y0);
    rm.w = Math.round(b.x1 - b.x0); rm.h = Math.round(b.y1 - b.y0);
  }
  _propPathD(pts) { return pts.map((p, k) => `${k ? "L" : "M"}${p[0]} ${p[1]}`).join(" ") + " Z"; }

  // The lot outline, stored as { points: [...] }.
  _getProperty() {
    const stored = this._fpStored("floor_plan_property");
    return stored && Array.isArray(stored.points) ? stored.points : [];
  }
  _propertyPts() { return this._fpWorking("_editingProperty", () => this._getProperty()); }
  _setProperty(pts) { this._editingProperty = pts; }
  // The lot's size in the chosen units (shoelace area, 0.04 sq ft per unit²).
  _propertyArea(pts) {
    if (!pts || pts.length < 3) return "";
    const twice = pts.reduce((sum, p, i) => {
      const q = pts[(i + 1) % pts.length];
      return sum + (p[0] * q[1] - q[0] * p[1]);
    }, 0);
    const sqFt = Math.abs(twice) / 2 * 0.04;
    if (this._fpUnits() !== "metric") {
      return sqFt >= 43560 ? (sqFt / 43560).toFixed(2) + " acres" : Math.round(sqFt).toLocaleString() + " sq ft";
    }
    const sqM = sqFt * 0.092903;
    return sqM >= 10000 ? (sqM / 10000).toFixed(2) + " ha" : Math.round(sqM).toLocaleString() + " m²";
  }

  // Windows, doors and dormers ("openings"). Their gaps let camera coverage
  // see through doorways rather than treating every wall as solid.
  _getFloorElements() { return this._fpStored("floor_plan_elements") || {}; }
  _getEditingElements() { return this._fpWorking("_editingElements", () => this._getFloorElements()); }
  _elemsFor(floor) { return this._fpFloorList(this._getEditingElements(), floor); }
  // Entities a door or window can be bound to: covers, locks, and opening
  // sensors by device class or by a door-like word in the id or name.
  _doorEntityOptions(selected) {
    const states = this._hass?.states || {};
    const doorWords = /door|garage|gate|cellar|bulkhead|hatch|window|contact|entry|slider|sash|casement|patio|french|skylight|opening|sliding/i;
    const fits = eid => {
      const domain = eid.split(".")[0];
      if (domain === "cover" || domain === "lock") return true;
      const at = states[eid].attributes || {};
      return domain === "binary_sensor" && (["door", "window", "garage_door", "opening"].includes(at.device_class || "")
        || doorWords.test(eid) || doorWords.test(at.friendly_name || ""));
    };
    const picks = Object.keys(states).filter(fits).sort();
    if (selected && !picks.includes(selected)) picks.unshift(selected);
    const opts = picks.map(eid => `<option value="${this._esc(eid)}"${eid === selected ? " selected" : ""}>${this._esc(this._entName(eid))}</option>`).join("");
    return `<option value=""${selected ? "" : " selected"}>— auto-detect —</option>${opts}`;
  }

  // Cameras placed on the plan, for the cone and AI coverage.
  _getFloorCameras() { return this._fpStored("floor_plan_cameras") || {}; }
  _getEditingCameras() { return this._fpWorking("_editingCameras", () => this._getFloorCameras()); }
  _camsFor(floor) { return this._fpFloorList(this._getEditingCameras(), floor); }
  _cameraEntityOptions(selected) {
    const states = this._hass?.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith("camera.")).sort();
    if (selected && !ids.includes(selected)) ids.unshift(selected);
    const label = id => (states[id] && states[id].attributes && states[id].attributes.friendly_name) || id;
    const opts = ids.map(id => `<option value="${this._esc(id)}"${id === selected ? " selected" : ""}>${this._esc(label(id))}</option>`).join("");
    return `<option value="">— camera —</option>${opts}`;
  }
