  // ─── Residence 3D (Phase A) ──────────────────────────────────────────────
  // Reuses the NOVA3D engine defined at the top of this file (window.NOVA3D)
  // rather than re-deriving the ~1000 lines of isometric-projection geometry
  // — this tab only shapes the small options object (spec/plan/elements/
  // garage/lit/doors/theta) NOVA3D expects, using the same floor_plan_rooms/
  // floor_plan_elements data this panel already reads for its own Floor
  // Plan Editor.
  _resStyles() {
    return {
      cape_cod: { label: "Cape Cod", roof: "gable", pitch: 1.0 },
      colonial: { label: "Colonial", roof: "gable", pitch: 0.7 },
      dutch_colonial: { label: "Dutch Colonial", roof: "gambrel", pitch: 0.6 },
      ranch: { label: "Ranch", roof: "hip", pitch: 0.5 },
      two_story: { label: "Two-Story", roof: "gable", pitch: 0.65 },
      craftsman: { label: "Craftsman", roof: "hip", pitch: 0.6 },
      modern: { label: "Modern", roof: "flat", pitch: 0 },
      townhouse: { label: "Townhouse", roof: "gable", pitch: 0.85 },
      apartment: { label: "Apartment", roof: "flat", pitch: 0 },
      cabin: { label: "Cabin", roof: "gable", pitch: 1.25 },
    };
  }
  _residenceStyleOptions(d) {
    const styles = this._resStyles();
    const cur = (d.config && d.config.residence_style) || "cape_cod";
    return Object.keys(styles).map(k => `<option value="${k}"${k === cur ? " selected" : ""}>${styles[k].label}</option>`).join("");
  }
  _styleDefaults(style) {
    const T = {
      cape_cod: { roof: "gable", pitch: 1.0, dormersFront: 2, dormersRear: 1 },
      colonial: { roof: "gable", pitch: 0.7, dormersFront: 0, dormersRear: 0 },
      dutch_colonial: { roof: "gambrel", pitch: 0.6, dormersFront: 2, dormersRear: 1 },
      ranch: { roof: "hip", pitch: 0.5, dormersFront: 0, dormersRear: 0 },
      two_story: { roof: "gable", pitch: 0.65, dormersFront: 0, dormersRear: 0 },
      craftsman: { roof: "hip", pitch: 0.6, dormersFront: 1, dormersRear: 0 },
      modern: { roof: "flat", pitch: 0.12, dormersFront: 0, dormersRear: 0 },
      townhouse: { roof: "gable", pitch: 0.85, dormersFront: 0, dormersRear: 0 },
      apartment: { roof: "flat", pitch: 0.12, dormersFront: 0, dormersRear: 0 },
      cabin: { roof: "gable", pitch: 1.25, dormersFront: 2, dormersRear: 1 },
    };
    return T[style] || T.cape_cod;
  }
  _houseSpec() {
    const c = this._data()?.config || {};
    const style = c.residence_style || "cape_cod";
    const sd = this._styleDefaults(style);
    const num = v => (v === "" || v == null ? null : Number(v));
    const fEx = num(c.dormers_front), rEx = num(c.dormers_rear);
    const spec = {};
    spec.roof = sd.roof || "gable";
    spec.stories = num(c.home_stories) != null ? num(c.home_stories) : 1.5;
    if (sd.pitch != null) spec.pitch = sd.pitch;
    spec.dormersFront = fEx != null ? fEx : sd.dormersFront;
    spec.dormersRear = rEx != null ? rEx : sd.dormersRear;
    if (num(c.garage_bays) != null) spec.garageBays = num(c.garage_bays);
    if (c.chimney_side) spec.chimney = c.chimney_side;
    return spec;
  }
  // Panel floor key -> model floor key; convert editor rooms (SVG units) to
  // the model's real feet (FT_PER_UNIT = 0.2), same as Classic.
  _planToFeet(plan) {
    const FT = 0.2, out = {};
    Object.keys(plan || {}).forEach(fk => {
      out[fk] = (((plan[fk] || {}).rooms) || []).filter(r => r.type !== "outdoor").map(r => ({
        name: (r.name || "").toLowerCase(),
        label: (r.name || "").toUpperCase(),
        x: (r.x || 0) * FT, y: (r.y || 0) * FT, w: (r.w || 0) * FT, d: (r.h || 0) * FT,
        type: r.type,
        points: (Array.isArray(r.points) && r.points.length >= 3) ? r.points.map(p => [p[0] * FT, p[1] * FT]) : undefined,
      }));
    });
    return this._snapFeet(out);
  }
  _snapMap(vals, tol) {
    const s = vals.slice().sort((a, b) => a - b), reps = [];
    let cur = null;
    s.forEach(v => { if (cur && v - cur.start <= tol) { cur.vals.push(v); } else { cur = { vals: [v], start: v }; reps.push(cur); } });
    const means = reps.map(g => g.vals.reduce((a, b) => a + b, 0) / g.vals.length);
    return v => { let best = v, bd = tol + 1e-6; means.forEach(m => { const dd = Math.abs(v - m); if (dd < bd) { bd = dd; best = m; } }); return best; };
  }
  _snapFeet(out) {
    const TOL = 1.0;
    Object.keys(out || {}).forEach(fk => {
      const rooms = out[fk] || [];
      if (rooms.length < 2) return;
      const xs = [], ys = [];
      rooms.forEach(r => { xs.push(r.x, r.x + r.w); ys.push(r.y, r.y + r.d); if (r.points) r.points.forEach(p => { xs.push(p[0]); ys.push(p[1]); }); });
      const sx = this._snapMap(xs, TOL), sy = this._snapMap(ys, TOL);
      rooms.forEach(r => {
        const x0 = sx(r.x), x1 = sx(r.x + r.w), y0 = sy(r.y), y1 = sy(r.y + r.d);
        r.x = x0; r.w = x1 - x0; r.y = y0; r.d = y1 - y0;
        if (r.points) r.points = r.points.map(p => [sx(p[0]), sy(p[1])]);
      });
    });
    return out;
  }
  _house3dPlan() { return this._planToFeet(this._getFloorPlan()); }
  _elementsToFeet(raw) {
    const FT = 0.2, states = this._hass?.states || {}, out = {};
    Object.keys(raw || {}).forEach(fk => {
      out[fk] = (raw[fk] || []).map(e => {
        let open = false;
        if (e.entity && states[e.entity]) { const s = states[e.entity].state; open = (s === "on" || s === "open"); }
        return { type: e.type, kind: e.kind, wall: e.wall, room: e.room, slope: e.slope, pos: (e.pos != null ? e.pos : 0.5), w: (e.w || 20) * FT, open };
      });
    });
    return out;
  }
  _house3dElements() { return this._elementsToFeet(this._getFloorElements()); }
  _house3dGarage() {
    const cfg = this._data()?.config || {};
    const map = cfg.door_mapping || {};
    const bays = Math.max(0, Math.min(Number(cfg.garage_bays) || 0, 8));
    const states = this._hass?.states || {};
    const out = [];
    for (let i = 1; i <= bays; i++) {
      const eid = map["garage_" + i] || (i === 1 ? (map.garage || "") : "");
      let open = false;
      if (eid && states[eid]) { const s = states[eid].state; open = (s === "on" || s === "open"); }
      out.push({ open });
    }
    return out;
  }
  _house3dFloor() {
    const f = this._currentFloor || "all";
    return f === "bsmt" ? "b" : f;
  }
  _house3dLit() {
    const d = this._data() || {};
    const lit = {};
    (d.areas || []).forEach(a => { if (a.active) lit[String(a.name).toLowerCase()] = "on"; });
    const mm = this._mmwave && this._mmwave.rooms;
    if (Array.isArray(mm)) mm.forEach(r => { if (r.detecting_count > 0) lit[String(r.name).toLowerCase()] = "mmwave"; });
    return lit;
  }
  _house3dDoors() {
    const d = this._data() || {};
    return d.doors || {};
  }
  _doorSlots() {
    const bays = Math.max(0, Math.min(Number((this._data()?.config || {}).garage_bays) || 0, 8));
    const garage = [];
    for (let i = 1; i <= bays; i++) garage.push(["garage_" + i, "Garage Door " + i]);
    if (!bays) garage.push(["garage", "Garage Door"]);
    return [["front", "Front Door"], ...garage, ["garage_rear", "Garage Side / Rear"], ["kitchen_garage", "Kitchen ↔ Garage"], ["cellar", "Cellar / Bulkhead"], ["basement", "Basement"]];
  }
  _renderDoorMappingNew(d) {
    const map = (d.config && d.config.door_mapping) || {};
    const rows = this._doorSlots().map(([slot, label]) => `
      <div class="cfg-row">
        <label>${label}</label>
        <select class="door-map-sel-new" id="resDoorMap-${slot}" data-slot="${slot}">${this._doorEntityOptions(map[slot] || "")}</select>
      </div>`).join("");
    return rows;
  }
  _renderHouse3dNew() {
    const mount = this.shadowRoot?.getElementById("resIso");
    if (!mount || typeof window.NOVA3D === "undefined") return;
    const floor = this._house3dFloor();
    const spec = this._houseSpec();
    const plan = this._house3dPlan();
    const elements = this._house3dElements();
    const garage = this._house3dGarage();
    const key = floor + "|" + JSON.stringify(spec) + "|" + JSON.stringify(plan) + "|" + JSON.stringify(elements) + "|" + JSON.stringify(garage);
    if (this._house3dBoxKey !== key) {
      this._house3dBox = window.NOVA3D.fixedBox({ floor, spec, plan, elements, garage });
      this._house3dBoxKey = key;
    }
    const base = this._house3dBox, zoom = this._house3dZoom || 1;
    const cx = base[0] + base[2] / 2, cy = base[1] + base[3] / 2;
    const w = base[2] / zoom, h = base[3] / zoom;
    const box = [cx - w / 2, cy - h / 2, w, h];
    mount.innerHTML = window.NOVA3D.renderSVG({
      theta: this._house3dTheta || 35, floor, lit: this._house3dLit(), doors: this._house3dDoors(), box, spec, plan, elements, garage,
    });
  }
  _buildResidenceAnnotationsNew() {
    const d = this._data() || {};
    const cfg = d.config || {};
    const areas = d.areas || [];
    const cfgBeds = cfg.home_bedrooms, cfgBaths = cfg.home_bathrooms;
    const beds = (cfgBeds != null && cfgBeds !== "") ? Number(cfgBeds) : (areas.filter(a => a.bedroom).length || 0);
    const baths = (cfgBaths != null && cfgBaths !== "") ? Number(cfgBaths) : areas.filter(a => /bath/i.test(a.name || "")).length;
    const bbEl = this.shadowRoot?.getElementById("resBb");
    if (bbEl) bbEl.textContent = beds + " / " + (baths || "—");
    const sqEl = this.shadowRoot?.getElementById("resSqft");
    if (sqEl) {
      let sqft = cfg.floor_plan_sqft;
      if (!sqft) {
        const plan = this._getFloorPlan();
        let u = 0;
        Object.keys(plan).forEach(fk => (plan[fk] && plan[fk].rooms || []).forEach(r => {
          if (r.type === "door" || r.type === "stairs") return;
          u += (r.w || 0) * (r.h || 0);
        }));
        sqft = Math.min(5000, Math.max(600, Math.round(u * 0.032 / 50) * 50));
      }
      sqEl.textContent = sqft ? "~" + Number(sqft).toLocaleString() : "—";
    }
    const styleTag = this.shadowRoot?.getElementById("resStyleTag");
    if (styleTag) {
      const rs = this._resStyles()[(cfg.residence_style || "cape_cod")];
      styleTag.textContent = rs ? rs.label : "—";
    }
    const occEl = this.shadowRoot?.getElementById("resOcc");
    if (occEl) {
      const occ = areas.filter(a => a.active).length;
      occEl.textContent = occ + " / " + (areas.length || 0);
    }
  }
  _build3DHouseNew() {
    const mount = this.shadowRoot?.getElementById("resIso");
    if (!mount) return;
    this._renderHouse3dNew();
    this._buildResidenceAnnotationsNew();
    this._wire3DDragNew();
  }
  _wire3DDragNew() {
    const scene = this.shadowRoot?.getElementById("resScene");
    if (!scene || scene._house3dWired) return;
    scene._house3dWired = true;
    // Touch is ambiguous between "rotate the house" and "scroll the page
    // past it" — both start as a drag on the same element. Committing to
    // rotate on touchstart (and preventDefault-ing every touchmove) hijacked
    // every vertical scroll attempt that happened to start on the house,
    // which read as "the 3D view is sluggish" (real complaint: it wouldn't
    // let go of the touch to let the page scroll at all). Mouse drag has no
    // such ambiguity — only touch needs the direction check below.
    let dragging = false, lastX = 0, raf = null;
    let isTouch = false, startX = 0, startY = 0, decided = false;
    const DIR_THRESHOLD = 6; // px of movement before committing to a direction
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = null; this._renderHouse3dNew(); }); };
    const pt = e => (e.touches && e.touches[0] ? e.touches[0] : e);
    const move = (e) => {
      if (!dragging) return;
      const p = pt(e);
      if (isTouch && !decided) {
        const dx = Math.abs(p.clientX - startX), dy = Math.abs(p.clientY - startY);
        if (dx < DIR_THRESHOLD && dy < DIR_THRESHOLD) return; // not enough movement yet to tell
        if (dy > dx) { up(); return; } // vertical swipe — let the page scroll instead
        decided = true;
      }
      if (e.cancelable) e.preventDefault();
      this._house3dTheta = (this._house3dTheta || 35) + (p.clientX - lastX) * 0.5;
      lastX = p.clientX;
      schedule();
    };
    const up = () => {
      dragging = false; scene.classList.remove("dragging");
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
      window.removeEventListener("touchmove", move); window.removeEventListener("touchend", up);
    };
    const down = (e, touch) => {
      const p = pt(e);
      dragging = true; lastX = p.clientX;
      isTouch = !!touch; startX = p.clientX; startY = p.clientY; decided = !isTouch;
      scene.classList.add("dragging");
      window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
      window.addEventListener("touchmove", move, { passive: false }); window.addEventListener("touchend", up);
    };
    scene.addEventListener("mousedown", (e) => { down(e, false); e.preventDefault(); });
    scene.addEventListener("touchstart", (e) => down(e, true), { passive: true });
    scene.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      this._house3dZoom = Math.max(0.5, Math.min(4, (this._house3dZoom || 1) * factor));
      schedule();
    }, { passive: false });
  }
  async _fetchMmwaveNew() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/mmwave_overview" });
      this._mmwave = res || { rooms: [], summary: {} };
    } catch (_) {
      this._mmwave = { rooms: [], summary: {}, error: true };
    }
    this._renderMmwaveNew();
    if (this._currentTab === "residence") this._renderHouse3dNew();
  }
  _renderMmwaveNew() {
    const list = this.shadowRoot?.getElementById("resMmwaveList");
    const sumEl = this.shadowRoot?.getElementById("resMmwaveSummary");
    if (!list) return;
    const data = this._mmwave || { rooms: [], summary: {} };
    const s = data.summary || {};
    if (sumEl) sumEl.textContent = s.rooms_with_mmwave ? `◉ ${s.rooms_detecting || 0}/${s.rooms_with_mmwave} OCCUPIED` : "◉ NONE";
    if (data.error) { list.innerHTML = `<div class="toggle-desc">Couldn't read sensors — restart Home Assistant after updating, then reopen.</div>`; return; }
    const rooms = data.rooms || [];
    if (!rooms.length) { list.innerHTML = `<div class="toggle-desc">No presence, motion, or mmWave sensors found. Assign occupancy sensors to areas in Home Assistant and they'll appear here.</div>`; return; }
    list.innerHTML = rooms.map(r => {
      const on = r.detecting_count > 0;
      const sensorLine = r.sensor_count > 1 ? `${r.detecting_count}/${r.sensor_count} sensors` : `${r.sensor_count} sensor`;
      return `<div class="cfg-row">
        <label>${this._esc(r.name)}${r.outdoor ? " ▲" : ""}</label>
        <span class="toggle-desc">${on ? "OCCUPIED" : "clear"} · ${sensorLine} · ${on ? "now" : this._esc(r.freshest)}</span>
      </div>`;
    }).join("");
  }
  _wireResidenceControlsNew() {
    const root = this.shadowRoot;
    // Not part of the Settings tab, so it can't ride _wireSettings()'s
    // generic .cfg-field autosave (only wired while that tab is current) —
    // wire this select directly, same as Intrusion's own cfg-field controls.
    const styleSel = root.querySelector('select.cfg-field[data-cfg-key="residence_style"]');
    if (styleSel && !styleSel._wired) {
      styleSel._wired = true;
      styleSel.addEventListener("change", () => this._saveSetting("residence_style", styleSel.value));
    }
    root.querySelectorAll(".res-floor-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        this._currentFloor = btn.getAttribute("data-res-floor");
        this._house3dBoxKey = null;
        root.querySelectorAll(".res-floor-tab").forEach(b => b.classList.toggle("active", b === btn));
        this._renderHouse3dNew();
      });
    });
    root.querySelectorAll(".res-view-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        this._house3dTheta = parseFloat(btn.getAttribute("data-res-theta"));
        this._renderHouse3dNew();
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
          this._house3dBoxKey = null;
          this._renderHouse3dNew();
          try { await this._hass.callWS({ type: "nova/update_config", key: "door_mapping", value: JSON.stringify(map) }); } catch (_) {}
        });
      }
    });
  }

  _htmlResidence() {
    const d = this._data() || {};
    const cfg = d.config || {};
    if (!this._currentFloor) this._currentFloor = "all";
    const floors = [["all", "All"], ["1f", "1st Floor"]];
    if (String(cfg.home_stories ?? "1.5") !== "1") floors.push(["2f", "2nd Floor"]);
    if (cfg.has_basement !== false) floors.push(["bsmt", "Basement"]);
    return `
      <div class="res-tab-new">
        <div class="res-main-new">
          <div class="cfg-row">
            <label>Home style</label>
            <select class="cfg-field" data-cfg-key="residence_style">${this._residenceStyleOptions(d)}</select>
          </div>
          <div class="fpn-toolbar">
            <div class="fpn-floor-tabs">
              ${floors.map(([fk, lbl]) => `<button class="mode-chip res-floor-tab${this._currentFloor === fk ? " active" : ""}" data-res-floor="${fk}">${lbl}</button>`).join("")}
            </div>
            <div class="fpn-actions">
              ${[["FRONT", 0], ["RIGHT", 90], ["REAR", 180], ["LEFT", 270], ["ISO", 35]].map(([lbl, th]) => `<button class="mode-chip res-view-btn" data-res-theta="${th}">${lbl}</button>`).join("")}
            </div>
          </div>
          <div class="fpn-hint">Drag to rotate · scroll to zoom</div>
          <div class="res-scene-new" id="resScene"><div id="resIso"></div></div>
          <div class="mode-grid res-stats-new">
            <div class="cfg-row"><label>Est. sq ft</label><b id="resSqft">—</b></div>
            <div class="cfg-row"><label>Bed / Bath</label><b id="resBb">—</b></div>
            <div class="cfg-row"><label>Style</label><b id="resStyleTag">—</b></div>
            <div class="cfg-row"><label>Occupied</label><b id="resOcc">—</b></div>
          </div>
          <div class="mode-bind-head">Doors <span class="toggle-desc">map to your entities — blank = auto-detect by name</span></div>
          ${this._renderDoorMappingNew(d)}
        </div>
        <div class="res-side-new">
          <div class="mode-bind-head">mmWave Presence <span class="toggle-desc" id="resMmwaveSummary">◉ scan</span></div>
          <div class="toggle-desc">Live occupancy per room from presence/motion/mmWave sensors.</div>
          <div id="resMmwaveList"><div class="toggle-desc">Reading sensors…</div></div>
        </div>
      </div>`;
  }

