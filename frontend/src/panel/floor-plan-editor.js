  _floorPlanEditorCardBody() {
    const plan = this._getEditingPlan();
    const floors = Object.keys(plan);
    if (!this._editorFloor || !plan[this._editorFloor]) this._editorFloor = floors[0] || "1f";
    const floor = this._editorFloor;
    return `
      <div class="fpn-toolbar">
        <div class="fpn-floor-tabs">
          ${floors.map(fk => `<button class="mode-chip fpn-floor-tab${fk === floor ? " active" : ""}" data-fpn-floor="${fk}">${this._esc(plan[fk].label || fk)}</button>`).join("")}
        </div>
        <div class="fpn-actions">
          <button class="mode-chip" id="fpnAddRoom">+ Add Room</button>
          <button class="mode-chip" id="fpnAddZone">+ Outdoor Zone</button>
          ${this._fpnAddPropertyButton()}
          <button class="mode-chip" id="fpnUnits">Units: ${this._fpUnits() === "metric" ? "Metric" : "Imperial"}</button>
          <button class="mode-chip" id="fpnZoomFit">⤢ Fit</button>
        </div>
      </div>
      <div class="fpn-hint">Drag to move · bottom-right handle to resize · right-click to delete · double-click an edge to add a corner · scroll to zoom · drag empty space to pan</div>
      <div class="fpn-canvas" id="fpnCanvas">${this._renderFloorPlanSVG(plan, floor)}</div>
      ${this._renderOpeningsNew(floor)}
      ${this._renderCamerasNew(floor)}
      ${this._renderPlanEntitiesNew(floor)}
      <div class="fpn-actions">
        <button class="mode-chip" id="fpnSave">Save Layout</button>
        <button class="mode-chip" id="fpnReset">Reset Default</button>
        <button class="mode-chip" id="fpnExport">⬇ Export</button>
        <button class="mode-chip" id="fpnImport">⬆ Import</button>
        <input type="file" id="fpnImportFile" accept=".json,application/json" style="display:none">
      </div>`;
  }

  _fpnAddPropertyButton() {
    const pp = this._propertyPts();
    const has = pp.length >= 3;
    return `<button class="mode-chip" id="fpnAddProperty">${has ? "Clear Property" : "+ Property Line"}</button>`
      + (has ? `<span class="toggle-desc">Lot: ${this._propertyArea(pp)}</span>` : "");
  }

  _renderPlanEntitiesNew(floor) {
    const ents = this._entsFor(floor);
    const op = this._fpBgOpacity();
    let hasBg = false;
    try {
      const b = this._data()?.config?.floor_plan_bg;
      const bd = typeof b === "string" ? JSON.parse(b || "{}") : (b || {});
      hasBg = !!bd[floor];
    } catch (_) {}
    const chips = ents.length
      ? ents.map((e, i) => `<span class="new-pl-chip">${this._esc(this._entMarkerStyle(e.e).name)}<button class="fpn-ent-del" data-ei="${i}" title="Remove">×</button></span>`).join("")
      : `<span class="toggle-desc">No devices placed on this floor yet.</span>`;
    return `
      <div class="mode-bind-head">Devices on plan <span class="toggle-desc">add a device, drag its pin on the canvas, tap it to open controls</span></div>
      <div class="cfg-row">
        <input id="fpnEntInput" list="fpnEntList" class="cfg-field" style="flex:1" placeholder="type to find an entity…" autocomplete="off">
        <datalist id="fpnEntList">${this._allEntityDatalist()}</datalist>
        <button class="mode-chip" id="fpnEntAdd">+ Add</button>
      </div>
      <div class="mode-grid">${chips}</div>
      <div class="mode-bind-head">Imported plan <span class="toggle-desc">${hasBg ? "opacity of the uploaded floor-plan image behind the rooms" : "upload a real floor-plan image to trace rooms over"}</span></div>
      <div class="cfg-row">
        <button class="mode-chip" id="fpnBgUpload">⬆ ${hasBg ? "Replace" : "Upload"} Image</button>
        <input type="file" id="fpnBgFile" accept="image/*" style="display:none">
        <label>opacity</label>
        <input id="fpnBgOp" type="range" min="0" max="1" step="0.05" value="${op}">
        <span id="fpnBgOpVal">${Math.round(op * 100)}%</span>
      </div>`;
  }

  _renderOpeningsNew(floor) {
    const els = this._elemsFor(floor), uL = this._fpUnitLabel();
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const walls = [["front", "Front"], ["back", "Back"], ["left", "Left"], ["right", "Right"]];
    const wsel = (e, i) => `<select class="op-field-new" data-op="wall" data-i="${i}">${walls.map(w => `<option value="${w[0]}"${e.wall === w[0] ? " selected" : ""}>${w[1]}</option>`).join("")}</select>`;
    const rsel = (e, i) => `<select class="op-field-new" data-op="room" data-i="${i}"><option value="">— room —</option>${rooms.filter(r => r.type !== "outdoor").map(r => `<option value="${this._esc(r.name)}"${e.room === r.name ? " selected" : ""}>${this._esc(r.name)}</option>`).join("")}</select>`;
    const rows = els.map((e, i) => {
      const isD = e.type === "dormer";
      const t = isD ? (e.slope === "rear" ? "REAR DORMER" : "FRONT DORMER") : (e.type === "window" ? "WINDOW" : (e.kind === "interior" ? "INT DOOR" : (e.kind === "cellar" ? "CELLAR" : (e.kind === "cased" ? "CASED OPENING" : "EXT DOOR"))));
      const place = isD
        ? `<select class="op-field-new" data-op="slope" data-i="${i}"><option value="front"${e.slope !== "rear" ? " selected" : ""}>Front slope</option><option value="rear"${e.slope === "rear" ? " selected" : ""}>Rear slope</option></select>`
        : ((e.kind === "interior" || e.kind === "cased") ? (rsel(e, i) + " " + wsel(e, i)) : wsel(e, i));
      return `
        <div class="cfg-row op-row-new" data-i="${i}">
          <span class="new-pl-chip">${t}</span>
          ${place}
          <input class="op-field-new" data-op="pos" data-i="${i}" type="range" min="0" max="1" step="0.02" value="${e.pos != null ? e.pos : 0.5}" title="position along the wall">
          ${isD ? "" : `<input class="op-field-new op-num-new" data-op="w" data-i="${i}" type="number" min="1" step="0.5" value="${this._fpToReal(e.w || 20)}" style="width:56px"> ${uL}`}
          ${e.kind === "cased" ? `<span class="toggle-desc">open passage · no sensor</span>` : `<select class="op-field-new op-ent-new" data-op="entity" data-i="${i}">${this._doorEntityOptions(e.entity || "")}</select>`}
          <button class="fpn-ent-del op-del-new" data-i="${i}" title="Remove">×</button>
        </div>`;
    }).join("");
    const dBtns = floor === "2f" ? `<button class="mode-chip" id="opAddFdormer">+ Front Dormer</button><button class="mode-chip" id="opAddRdormer">+ Rear Dormer</button>` : "";
    return `
      <div class="mode-bind-head">Windows, doors &amp; dormers <span class="toggle-desc">interior doors &amp; cased openings attach to a room · a cased opening is a doorway with no door · dormers on the 2nd floor</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="opAddWindow">+ Window</button>
        <button class="mode-chip" id="opAddExtdoor">+ Exterior Door</button>
        <button class="mode-chip" id="opAddCellar">+ Cellar Door</button>
        <button class="mode-chip" id="opAddIntdoor">+ Interior Door</button>
        <button class="mode-chip" id="opAddCased">+ Cased Opening</button>
        ${dBtns}
      </div>
      ${rows || `<div class="toggle-desc">No openings placed on this floor yet — add one above.</div>`}`;
  }

  _renderCamerasNew(floor) {
    const cams = this._camsFor(floor), uL = this._fpUnitLabel();
    const geo = cams.length ? this._planGeometry(floor) : null;
    const zoneNames = new Set((((this._getEditingPlan()[floor] || {}).rooms) || []).filter(r => r.type === "outdoor").map(r => r.name));
    const rows = cams.map((c, i) => {
      const cov = geo ? this._computeCoverage(floor, c, geo) : {};
      const order = Object.keys(cov).sort((a, b) => cov[b] - cov[a]);
      const covLine = order.length
        ? `<div class="toggle-desc">sees: ${order.map(rn => `${this._esc(rn)} ${Math.round(cov[rn] * 100)}%${zoneNames.has(rn) ? " (zone)" : ""}`).join(" · ")}</div>`
        : `<div class="toggle-desc">nothing in view — aim it, widen the FOV, or extend the range</div>`;
      const cvg = c.coverage;
      const llmLine = (cvg && cvg.reason)
        ? `<div class="toggle-desc">${(cvg.covered && cvg.covered.length) ? `✓ confirms ${cvg.covered.map(r => this._esc(r)).join(", ")} — ` : ""}${this._esc(cvg.reason)}</div>`
        : "";
      return `
        <div class="cfg-row cam-row-new" data-ci="${i}">
          <span class="new-pl-chip">CAM ${i + 1}</span>
          <select class="cam-field-new" data-cam="entity" data-ci="${i}">${this._cameraEntityOptions(c.entity || "")}</select>
          <label class="fpn-inline-lbl">aim <input class="cam-field-new" data-cam="angle" data-ci="${i}" type="range" min="0" max="359" step="1" value="${c.angle != null ? c.angle : 270}"></label>
          <label class="fpn-inline-lbl">FOV <input class="cam-field-new" data-cam="fov" data-ci="${i}" type="range" min="20" max="170" step="5" value="${c.fov != null ? c.fov : 90}"></label>
          <label class="fpn-inline-lbl">range <input class="cam-field-new cam-num-new" data-cam="range" data-ci="${i}" type="number" min="5" step="5" value="${this._fpToReal(c.range != null ? c.range : 55)}"> ${uL}</label>
          <button class="mode-chip cam-io-new" data-ci="${i}" title="indoor = bounded by walls, outdoor = by range">${c.indoor === false ? "OUTDOOR" : "INDOOR"}</button>
          <button class="fpn-ent-del cam-del-new" data-ci="${i}" title="Remove">×</button>
        </div>
        ${covLine}${llmLine}`;
    }).join("");
    return `
      <div class="mode-bind-head">Cameras · field of view <span class="toggle-desc">drop a camera, bind its entity, aim it — drag the dot on the plan to move, right-click to delete</span></div>
      <div class="cfg-row cfg-row-wrap">
        <button class="mode-chip" id="fpnCamAdd">+ Camera</button>
        ${cams.length ? `<button class="mode-chip" id="fpnCamCompute" title="AI: judge what each camera can confirm">Compute coverage</button>` : ""}
      </div>
      ${rows || `<div class="toggle-desc">No cameras placed on this floor yet — add one above.</div>`}`;
  }

  _renderFloorPlanSVG(plan, floor) {
    const floorData = plan[floor];
    if (!floorData) return "";
    const vb = this._editVB
      ? `${this._editVB.x} ${this._editVB.y} ${this._editVB.w} ${this._editVB.h}`
      : (floorData.viewBox || "0 0 320 150");
    let svg = `<svg viewBox="${vb}" class="fpn-svg" id="fpnSvg" style="width:100%;height:100%;min-height:520px;background:var(--bg);border:1px solid var(--line-soft);border-radius:10px;cursor:crosshair;">`;
    svg += '<defs>'
      + '<pattern id="fpn-grid-sm" width="10" height="10" patternUnits="userSpaceOnUse"><path d="M 10 0 L 0 0 0 10" fill="none" stroke="rgba(244,184,96,0.05)" stroke-width="0.2"/></pattern>'
      + '<pattern id="fpn-grid-lg" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M 50 0 L 0 0 0 50" fill="none" stroke="rgba(244,184,96,0.12)" stroke-width="0.3"/></pattern>'
      + '</defs>';
    const vbp = vb.split(" ").map(Number);
    const gx = vbp[0], gy = vbp[1], gw = vbp[2], gh = vbp[3];
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-sm)"/>`;
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-lg)"/>`;

    const bgs = this._data()?.config?.floor_plan_bg;
    if (bgs) {
      try {
        const bgData = typeof bgs === "string" ? JSON.parse(bgs) : bgs;
        if (bgData && bgData[floor]) {
          svg += `<image href="${bgData[floor]}" x="0" y="0" width="100%" height="100%" opacity="${this._fpBgOpacity()}" preserveAspectRatio="xMidYMid meet"/>`;
        }
      } catch (_) {}
    }

    // Property boundary (the lot) — draw behind rooms; vertices are draggable.
    let prop = [];
    try { prop = this._propertyPts() || []; } catch (_) { prop = []; }
    if (prop.length >= 2) {
      svg += `<path class="fpn-prop-path" d="${this._propPathD(prop)}" fill="rgba(244,184,96,0.03)" stroke="var(--gold)" stroke-width="1" stroke-dasharray="6 4" pointer-events="none"/>`;
      for (let vi = 0; vi < prop.length; vi++) {
        const a = prop[vi], b = prop[(vi + 1) % prop.length];
        svg += `<circle class="fpn-prop-mid" data-prop-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2.2" fill="none" stroke="var(--gold)" stroke-width="0.7" opacity="0.5" style="cursor:copy"/>`;
      }
      for (let pi = 0; pi < prop.length; pi++) {
        svg += `<circle class="fpn-prop-vtx" data-prop-vtx="${pi}" cx="${prop[pi][0]}" cy="${prop[pi][1]}" r="3" fill="var(--gold)" stroke="var(--bg)" stroke-width="0.7" style="cursor:grab"/>`;
      }
    }

    for (let i = 0; i < (floorData.rooms || []).length; i++) {
      const rm = floorData.rooms[i];
      const colors = { room: "var(--gold)", bath: "var(--ink-faint)", stairs: "var(--ember)", door: "var(--warn)", outdoor: "#8fdba8" };
      const c = colors[rm.type] || "var(--gold)";
      const out = rm.type === "outdoor";
      const fs = rm.w > 80 ? 7 : (rm.w > 50 ? 5.5 : (rm.w > 25 ? 4 : 3));
      if (out || (rm.points && rm.points.length >= 3)) {
        const zpts = this._zonePoints(rm);
        let zcx = 0, zcy = 0; zpts.forEach(p => { zcx += p[0]; zcy += p[1]; }); zcx /= zpts.length; zcy /= zpts.length;
        svg += `<g class="fpn-zone" data-zone-idx="${i}">`;
        svg += `<path class="fpn-zone-path" data-zone-idx="${i}" d="${this._propPathD(zpts)}" fill="${c}" fill-opacity="0.06" stroke="${c}" stroke-width="1"${out ? ' stroke-dasharray="4 3"' : ""} style="cursor:move"/>`;
        svg += `<text x="${zcx.toFixed(1)}" y="${zcy.toFixed(1)}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        for (let vi = 0; vi < zpts.length; vi++) { const a = zpts[vi], b = zpts[(vi + 1) % zpts.length]; svg += `<circle class="fpn-zone-mid" data-zone-idx="${i}" data-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2" fill="none" stroke="${c}" stroke-width="0.6" opacity="0.5" style="cursor:copy"/>`; }
        for (let vi = 0; vi < zpts.length; vi++) { svg += `<circle class="fpn-zone-vtx" data-zone-idx="${i}" data-vtx="${vi}" cx="${zpts[vi][0]}" cy="${zpts[vi][1]}" r="2.8" fill="${c}" stroke="var(--bg)" stroke-width="0.6" style="cursor:grab"/>`; }
        svg += "</g>";
      } else {
        svg += `<g class="fpn-drag-room" data-idx="${i}" style="cursor:move">`;
        svg += `<rect x="${rm.x}" y="${rm.y}" width="${rm.w}" height="${rm.h}" rx="2" fill="${out ? "rgba(143,219,168,0.06)" : "rgba(244,184,96,0.08)"}" stroke="${c}" stroke-width="1" class="fpn-drag-rect"/>`;
        svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        if (rm.w > 30 && rm.h > 24) svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2 + fs + 2.5}" text-anchor="middle" fill="${c}" opacity="0.6" font-size="${(fs * 0.72).toFixed(1)}" font-family="var(--font-mono)" pointer-events="none">${this._fpDim(rm.w)} × ${this._fpDim(rm.h)}</text>`;
        svg += `<rect x="${rm.x + rm.w - 8}" y="${rm.y + rm.h - 8}" width="8" height="8" fill="${c}" opacity="0.35" rx="1" class="fpn-resize-handle" data-idx="${i}" style="cursor:nwse-resize"/>`;
        svg += "</g>";
      }
    }
    for (const lbl of (floorData.labels || [])) {
      svg += `<text x="${lbl.x}" y="${lbl.y}" text-anchor="middle" fill="var(--ink-faint)" font-size="4" font-family="var(--font-mono)">${this._esc(lbl.text)}</text>`;
    }

    // Placed openings as wall markers.
    const els = this._elemsFor(floor) || [];
    if (els.length && floorData.rooms && floorData.rooms.length) {
      let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
      floorData.rooms.forEach(r => { if (r.type === "outdoor") return; mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
      els.forEach((e, i) => {
        if (e.type === "dormer") {
          const dp = e.pos != null ? e.pos : 0.5, dcx = mnx + dp * (mxx - mnx), dcy = e.slope === "rear" ? mxy : mny;
          svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${dcx - 4}" y="${dcy - 3}" width="8" height="6" fill="#b06aff" opacity="0.9" rx="1.5" pointer-events="none"/>`;
          return;
        }
        const w = e.w || 20, p = e.pos != null ? e.pos : 0.5, horiz = (e.wall === "front" || e.wall === "back");
        let bx0 = mnx, by0 = mny, bx1 = mxx, by1 = mxy;
        if ((e.kind === "interior" || e.kind === "cased") && e.room) {
          const rr = floorData.rooms.filter(r => r.name === e.room)[0];
          if (rr) { bx0 = rr.x; by0 = rr.y; bx1 = rr.x + rr.w; by1 = rr.y + rr.h; }
        }
        let cx, cy;
        if (e.wall === "front") { cx = bx0 + p * (bx1 - bx0); cy = by0; }
        else if (e.wall === "back") { cx = bx0 + p * (bx1 - bx0); cy = by1; }
        else if (e.wall === "left") { cx = bx0; cy = by0 + p * (by1 - by0); }
        else { cx = bx1; cy = by0 + p * (by1 - by0); }
        const col = e.type === "window" ? "var(--gold)" : (e.kind === "interior" ? "#5a7a8a" : (e.kind === "cellar" ? "#c98a2a" : (e.kind === "cased" ? "#78b9d7" : "var(--warn)")));
        const ex = horiz ? cx - w / 2 : cx - 2, ey = horiz ? cy - 2 : cy - w / 2, ew = horiz ? w : 4, eh = horiz ? 4 : w;
        svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${ex}" y="${ey}" width="${ew}" height="${eh}" fill="${col}" opacity="0.9" rx="1" pointer-events="none"/>`;
      });
    }

    // Cameras — icon + FOV cone, clipped to walls.
    const cams = this._camsFor(floor);
    const camGeo = cams.length ? this._planGeometry(floor) : null;
    for (let ci = 0; ci < cams.length; ci++) {
      const cam = cams[ci], out = cam.indoor === false;
      svg += `<g class="fpn-cam" data-cam-idx="${ci}">`
        + `<path class="fpn-cam-cone" d="${this._clippedCone(cam, camGeo)}" fill="${out ? "rgba(232,178,61,0.10)" : "rgba(226,84,47,0.10)"}" stroke="${out ? "rgba(232,178,61,0.6)" : "rgba(226,84,47,0.6)"}" stroke-width="0.7" pointer-events="none"/>`
        + `<circle class="fpn-cam-dot" cx="${cam.x}" cy="${cam.y}" r="3.2" fill="${out ? "var(--warn)" : "var(--ember)"}" stroke="var(--ink)" stroke-width="0.7" style="cursor:grab"/>`
        + `<text x="${cam.x}" y="${cam.y - 5}" text-anchor="middle" fill="${out ? "var(--warn)" : "var(--ember)"}" font-size="5" font-family="var(--font-mono)" pointer-events="none">${ci + 1}</text>`
        + "</g>";
    }

    const ents = this._entsFor(floor);
    for (let ei = 0; ei < ents.length; ei++) {
      const ent = ents[ei];
      if (!ent || !ent.e) continue;
      const ms = this._entMarkerStyle(ent.e);
      const nm = ms.name.length > 16 ? (ms.name.slice(0, 15) + "…") : ms.name;
      svg += `<g class="fpn-ent" data-ent-idx="${ei}" data-ent-id="${this._esc(ent.e)}" style="cursor:pointer">`
        + `<circle class="fpn-ent-dot" cx="${ent.x}" cy="${ent.y}" r="3" fill="${ms.color}" stroke="var(--bg)" stroke-width="0.7"/>`
        + `<text class="fpn-ent-nm" x="${ent.x}" y="${ent.y - 4}" text-anchor="middle" fill="${ms.color}" font-size="3.4" font-family="var(--font-mono)" pointer-events="none">${this._esc(nm)}</text>`
        + `<text class="fpn-ent-val" x="${ent.x}" y="${ent.y + 6.5}" text-anchor="middle" fill="var(--ink-dim)" font-size="3" font-family="var(--font-mono)" pointer-events="none">${this._esc(ms.val)}</text>`
        + "</g>";
    }

    svg += "</svg>";
    return svg;
  }

  // Re-renders just this one card (not the whole settings grid) so an
  // in-progress edit elsewhere on the page isn't disturbed and scroll
  // position is preserved — mirrors Classic's _rerenderFloorEditor().
  _rerenderFloorPlanCard() {
    const card = this.shadowRoot?.getElementById("settings-card-floor_plan_editor");
    if (!card) return;
    const c = NovaPanel.SETTINGS_CARDS.find(x => x.id === "floor_plan_editor");
    card.innerHTML = `
        <div class="panel-head"><div class="panel-title">${this._esc(c.title)}</div></div>
        ${this._floorPlanEditorCardBody()}`;
    this._wireFloorPlanEditor();
  }

  _wireFloorPlanEditor() {
    const root = this.shadowRoot;
    if (!root || !root.getElementById("fpnCanvas")) return;   // card not in the DOM right now

    root.querySelectorAll(".fpn-floor-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        this._editorFloor = btn.getAttribute("data-fpn-floor");
        this._editVB = null;
        this._rerenderFloorPlanCard();
      });
    });
    const fit = root.getElementById("fpnZoomFit");
    if (fit) fit.addEventListener("click", () => { this._editVB = null; this._rerenderFloorPlanCard(); });

    const units = root.getElementById("fpnUnits");
    if (units) units.addEventListener("click", async () => {
      const next = this._fpUnits() === "metric" ? "imperial" : "metric";
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_units", value: next });
        if (this._liveData?.config) this._liveData.config.floor_plan_units = next;
      } catch (err) { console.error("Nova: floor plan units save failed", err); }
      this._rerenderFloorPlanCard();
    });

    const addRoom = root.getElementById("fpnAddRoom");
    if (addRoom) addRoom.addEventListener("click", () => {
      const plan = this._getEditingPlan();
      const floor = this._editorFloor;
      if (!plan[floor]) return;
      const name = window.prompt("Room name:");
      if (!name) return;
      const type = window.prompt("Type (room, bath, stairs, door):", "room") || "room";
      plan[floor].rooms = plan[floor].rooms || [];
      plan[floor].rooms.push({ name, x: 50, y: 50, w: 60, h: 40, type });
      this._rerenderFloorPlanCard();
    });

    const addZone = root.getElementById("fpnAddZone");
    if (addZone) addZone.addEventListener("click", () => {
      const plan = this._getEditingPlan();
      const floor = this._editorFloor;
      if (!plan[floor]) return;
      const name = window.prompt("Outdoor zone name (e.g. Front Yard, Driveway, Backyard):");
      if (!name) return;
      plan[floor].rooms = plan[floor].rooms || [];
      const house = plan[floor].rooms.filter(r => r.type !== "outdoor");
      let zx = 40, zy = 40, zw = 90, zh = 70;
      if (house.length) {
        let hx0 = 1e9, hx1 = -1e9, hy1 = -1e9;
        house.forEach(r => { hx0 = Math.min(hx0, r.x); hx1 = Math.max(hx1, r.x + r.w); hy1 = Math.max(hy1, r.y + r.h); });
        zx = Math.round(hx0); zy = Math.round(hy1 + 25); zw = Math.round(Math.max(hx1 - hx0, 90));
      }
      plan[floor].rooms.push({ name, x: zx, y: zy, w: zw, h: zh, type: "outdoor", points: [[zx, zy], [zx + zw, zy], [zx + zw, zy + zh], [zx, zy + zh]] });
      this._rerenderFloorPlanCard();
    });

    const addProperty = root.getElementById("fpnAddProperty");
    if (addProperty) addProperty.addEventListener("click", () => {
      const cur = this._propertyPts();
      if (cur.length >= 3) {
        if (window.confirm("Remove the property boundary?")) { this._setProperty([]); this._rerenderFloorPlanCard(); }
        return;
      }
      const floor = this._editorFloor;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      rooms.forEach(r => { x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y); x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h); });
      if (!isFinite(x0)) { x0 = 20; y0 = 20; x1 = 220; y1 = 170; }
      const m = Math.max(150, Math.max(x1 - x0, y1 - y0) * 0.7);
      this._setProperty([[Math.round(x0 - m), Math.round(y0 - m)], [Math.round(x1 + m), Math.round(y0 - m)], [Math.round(x1 + m), Math.round(y1 + m)], [Math.round(x0 - m), Math.round(y1 + m)]]);
      this._rerenderFloorPlanCard();
    });

    const addElem = (type, kind) => {
      const floor = this._editorFloor;
      this._elemsFor(floor).push({ id: "e" + Date.now().toString(36), type, kind, wall: "front", pos: 0.5, w: 20, entity: "" });
      this._rerenderFloorPlanCard();
    };
    const opAddWindow = root.getElementById("opAddWindow"); if (opAddWindow) opAddWindow.addEventListener("click", () => addElem("window", null));
    const opAddExtdoor = root.getElementById("opAddExtdoor"); if (opAddExtdoor) opAddExtdoor.addEventListener("click", () => addElem("door", "exterior"));
    const opAddCellar = root.getElementById("opAddCellar"); if (opAddCellar) opAddCellar.addEventListener("click", () => addElem("door", "cellar"));
    const opAddIntdoor = root.getElementById("opAddIntdoor"); if (opAddIntdoor) opAddIntdoor.addEventListener("click", () => addElem("door", "interior"));
    const opAddCased = root.getElementById("opAddCased"); if (opAddCased) opAddCased.addEventListener("click", () => addElem("door", "cased"));
    const opAddFdormer = root.getElementById("opAddFdormer");
    if (opAddFdormer) opAddFdormer.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).push({ id: "e" + Date.now().toString(36), type: "dormer", slope: "front", pos: 0.5, entity: "" });
      this._rerenderFloorPlanCard();
    });
    const opAddRdormer = root.getElementById("opAddRdormer");
    if (opAddRdormer) opAddRdormer.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).push({ id: "e" + Date.now().toString(36), type: "dormer", slope: "rear", pos: 0.5, entity: "" });
      this._rerenderFloorPlanCard();
    });
    root.querySelectorAll(".op-field-new").forEach(f => {
      f.addEventListener("change", () => {
        const arr = this._elemsFor(this._editorFloor), e = arr[parseInt(f.getAttribute("data-i"))];
        if (!e) return;
        const op = f.getAttribute("data-op");
        if (op === "w") e.w = this._fpFromReal(parseFloat(f.value) || 4);
        else if (op === "pos") e.pos = parseFloat(f.value);
        else e[op] = f.value;
        this._rerenderFloorPlanCard();
      });
    });
    root.querySelectorAll(".op-del-new").forEach(b => b.addEventListener("click", () => {
      this._elemsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-i")), 1);
      this._rerenderFloorPlanCard();
    }));
    const glowMarker = (i, on) => {
      const m = root.querySelector(`.fpn-op-marker[data-op-marker="${i}"]`);
      if (m) m.classList.toggle("op-glow", on);
    };
    root.querySelectorAll(".op-row-new").forEach(row => {
      const i = row.getAttribute("data-i");
      row.addEventListener("mouseenter", () => glowMarker(i, true));
      row.addEventListener("mouseleave", () => glowMarker(i, false));
      const ent = row.querySelector(".op-ent-new");
      if (ent) {
        ent.addEventListener("focus", () => glowMarker(i, true));
        ent.addEventListener("blur", () => glowMarker(i, false));
      }
    });

    const camAdd = root.getElementById("fpnCamAdd");
    if (camAdd) camAdd.addEventListener("click", () => {
      const floor = this._editorFloor;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let ccx = 100, ccy = 80;
      if (rooms.length) {
        let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
        rooms.forEach(r => { mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
        ccx = Math.round((mnx + mxx) / 2); ccy = Math.round((mny + mxy) / 2);
      }
      this._camsFor(floor).push({ id: "c" + Date.now().toString(36), x: ccx, y: ccy, angle: 270, fov: 90, range: 55, entity: "", indoor: true });
      this._rerenderFloorPlanCard();
    });
    const setCamField = (f) => {
      const cam = this._camsFor(this._editorFloor)[parseInt(f.getAttribute("data-ci"))];
      if (!cam) return null;
      const k = f.getAttribute("data-cam");
      if (k === "range") cam.range = this._fpFromReal(parseFloat(f.value) || 10);
      else if (k === "angle" || k === "fov") cam[k] = parseFloat(f.value);
      else cam[k] = f.value;
      return cam;
    };
    root.querySelectorAll(".cam-field-new").forEach(f => {
      f.addEventListener("input", () => {
        const cam = setCamField(f);
        if (!cam || f.getAttribute("data-cam") === "entity") return;
        const g = root.querySelector(`.fpn-cam[data-cam-idx="${f.getAttribute("data-ci")}"]`);
        if (g) { const cone = g.querySelector(".fpn-cam-cone"); if (cone) cone.setAttribute("d", this._clippedCone(cam, this._planGeometry(this._editorFloor))); }
      });
      f.addEventListener("change", () => { setCamField(f); this._rerenderFloorPlanCard(); });
    });
    root.querySelectorAll(".cam-io-new").forEach(b => b.addEventListener("click", () => {
      const cam = this._camsFor(this._editorFloor)[parseInt(b.getAttribute("data-ci"))];
      if (!cam) return; cam.indoor = (cam.indoor === false); this._rerenderFloorPlanCard();
    }));
    root.querySelectorAll(".cam-del-new").forEach(b => b.addEventListener("click", () => {
      this._camsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-ci")), 1);
      this._rerenderFloorPlanCard();
    }));
    const camCompute = root.getElementById("fpnCamCompute");
    if (camCompute) camCompute.addEventListener("click", async () => {
      const floor = this._editorFloor;
      const cams = this._camsFor(floor);
      if (!cams.length) return;
      camCompute.disabled = true;
      const geo = this._planGeometry(floor), openings = this._openingDescriptions(floor);
      for (const cam of cams) {
        const cand = this._computeCoverage(floor, cam, geo);
        const ctx = { entity: cam.entity || "", room: this._roomAt(floor, cam.x, cam.y), fov: cam.fov || 90, range_ft: this._fpToReal(cam.range || 55), indoor: cam.indoor !== false, candidates: cand, openings };
        try { cam.coverage = await this._hass.callWS({ type: "nova/compute_camera_coverage", camera: ctx }); } catch (err) { /* keep going */ }
      }
      this._rerenderFloorPlanCard();
    });

    const save = root.getElementById("fpnSave");
    if (save) save.addEventListener("click", async () => {
      const hasProperty = this._editingProperty !== null && this._editingProperty !== undefined;
      if (!this._editingPlan && !this._editingEntities && !this._editingCameras && !this._editingElements && !hasProperty) return;
      const savedPlan = this._editingPlan, savedEnts = this._editingEntities,
        savedCams = this._editingCameras, savedEls = this._editingElements, savedProp = this._editingProperty;
      try {
        if (savedPlan) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_rooms", value: JSON.stringify(savedPlan) });
        if (savedEnts) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_entities", value: JSON.stringify(savedEnts) });
        if (savedCams) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_cameras", value: JSON.stringify(savedCams) });
        if (savedEls) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_elements", value: JSON.stringify(savedEls) });
        if (hasProperty) await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_property", value: JSON.stringify({ points: savedProp }) });
        if (this._liveData?.config) {
          if (savedPlan) this._liveData.config.floor_plan_rooms = savedPlan;
          if (savedEnts) this._liveData.config.floor_plan_entities = savedEnts;
          if (savedCams) this._liveData.config.floor_plan_cameras = savedCams;
          if (savedEls) this._liveData.config.floor_plan_elements = savedEls;
          if (hasProperty) this._liveData.config.floor_plan_property = { points: savedProp };
        }
        this._editingPlan = null;
        this._editingEntities = null;
        this._editingCameras = null;
        this._editingElements = null;
        this._editingProperty = null;
      } catch (err) { console.error("Nova: floor plan save failed", err); }
    });

    const reset = root.getElementById("fpnReset");
    if (reset) reset.addEventListener("click", async () => {
      try {
        await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_rooms", value: "" });
        if (this._liveData?.config) this._liveData.config.floor_plan_rooms = {};
      } catch (err) { console.error("Nova: floor plan reset failed", err); }
      this._editingPlan = null;
      this._editorFloor = null;
      this._rerenderFloorPlanCard();
    });

    // Export/Import the floor plan layout as JSON — a manual backup/restore,
    // or a way to copy a layout between installs.
    const fpExport = root.getElementById("fpnExport");
    if (fpExport) fpExport.addEventListener("click", () => {
      try {
        const data = JSON.stringify(this._getEditingPlan(), null, 2);
        const blob = new Blob([data], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "nova-floor-plan.json";
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (err) { console.error("Nova: floor plan export failed", err); }
    });
    const fpImportBtn = root.getElementById("fpnImport");
    const fpImportFile = root.getElementById("fpnImportFile");
    if (fpImportBtn && fpImportFile) {
      fpImportBtn.addEventListener("click", () => fpImportFile.click());
      fpImportFile.addEventListener("change", () => {
        const file = fpImportFile.files && fpImportFile.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
          try {
            const parsed = JSON.parse(ev.target.result);
            if (!parsed || typeof parsed !== "object" || !Object.keys(parsed).length) throw new Error("empty");
            this._editingPlan = parsed;
            if (!this._editingPlan[this._editorFloor]) this._editorFloor = Object.keys(parsed)[0];
            this._rerenderFloorPlanCard();
          } catch (err) { console.error("Nova: floor plan import — invalid layout file", err); }
        };
        reader.readAsText(file);
        fpImportFile.value = "";
      });
    }

    const entAdd = root.getElementById("fpnEntAdd");
    if (entAdd) entAdd.addEventListener("click", () => {
      const inp = root.getElementById("fpnEntInput");
      const val = inp && inp.value.trim();
      if (!val) return;
      if (!(this._hass && this._hass.states && this._hass.states[val])) return;
      const floor = this._editorFloor;
      if (this._entsFor(floor).some(x => x.e === val)) return;
      const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
      let ecx = 100, ecy = 80;
      if (rooms.length) {
        let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
        rooms.forEach(r => { mnx = Math.min(mnx, r.x); mny = Math.min(mny, r.y); mxx = Math.max(mxx, r.x + r.w); mxy = Math.max(mxy, r.y + r.h); });
        ecx = Math.round((mnx + mxx) / 2); ecy = Math.round((mny + mxy) / 2);
      }
      this._entsFor(floor).push({ e: val, x: ecx, y: ecy });
      this._rerenderFloorPlanCard();
    });
    root.querySelectorAll(".fpn-ent-del").forEach(b => b.addEventListener("click", () => {
      this._entsFor(this._editorFloor).splice(parseInt(b.getAttribute("data-ei")), 1);
      this._rerenderFloorPlanCard();
    }));
    const bgUpBtn = root.getElementById("fpnBgUpload");
    const bgFileInput = root.getElementById("fpnBgFile");
    if (bgUpBtn && bgFileInput) {
      bgUpBtn.addEventListener("click", () => bgFileInput.click());
      bgFileInput.addEventListener("change", async () => {
        const file = bgFileInput.files && bgFileInput.files[0];
        if (!file) return;
        const floor = this._editorFloor;
        try {
          const dataUrl = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result));
            r.onerror = () => reject(new Error("read failed"));
            r.readAsDataURL(file);
          });
          let bgs = {};
          try {
            const raw = this._data()?.config?.floor_plan_bg;
            if (raw) bgs = typeof raw === "string" ? JSON.parse(raw) : raw;
          } catch (_) {}
          bgs[floor] = dataUrl;
          await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_bg", value: JSON.stringify(bgs) });
          if (this._liveData?.config) this._liveData.config.floor_plan_bg = JSON.stringify(bgs);
          this._rerenderFloorPlanCard();
        } catch (err) {
          console.error("Nova: floor plan background upload failed", err);
        } finally {
          bgFileInput.value = "";
        }
      });
    }

    const bgOp = root.getElementById("fpnBgOp");
    if (bgOp) {
      const bgVal = root.getElementById("fpnBgOpVal");
      bgOp.addEventListener("input", () => {
        if (bgVal) bgVal.textContent = Math.round(parseFloat(bgOp.value) * 100) + "%";
        const img = root.querySelector("#fpnSvg image");
        if (img) img.setAttribute("opacity", bgOp.value);
      });
      bgOp.addEventListener("change", async () => {
        const v = String(parseFloat(bgOp.value));
        try {
          await this._hass.callWS({ type: "nova/update_config", key: "floor_plan_bg_opacity", value: v });
          if (this._liveData?.config) this._liveData.config.floor_plan_bg_opacity = v;
        } catch (err) { console.error("Nova: floor plan bg opacity save failed", err); }
      });
    }

    this._wireFloorPlanDrag();
  }

  _wireFloorPlanDrag() {
    const svgEl = this.shadowRoot.getElementById("fpnSvg");
    if (!svgEl) return;
    const self = this;
    const plan = this._getEditingPlan();
    const floor = this._editorFloor;
    const rooms = plan[floor]?.rooms;
    if (!rooms) return;

    let dragging = null, panning = null;

    function svgPoint(e) {
      const pt = svgEl.createSVGPoint();
      const ctm = svgEl.getScreenCTM().inverse();
      pt.x = e.clientX; pt.y = e.clientY;
      return pt.matrixTransform(ctm);
    }
    const vbFromAttr = () => {
      const p = (svgEl.getAttribute("viewBox") || "0 0 320 150").split(" ").map(Number);
      return { x: p[0], y: p[1], w: p[2], h: p[3] };
    };
    function applyVB() {
      const v = self._editVB; if (!v) return;
      svgEl.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
      svgEl.querySelectorAll(".fpn-grid-rect").forEach(r => {
        r.setAttribute("x", v.x); r.setAttribute("y", v.y); r.setAttribute("width", v.w); r.setAttribute("height", v.h);
      });
    }
    function redraw() {
      const canvas = self.shadowRoot.getElementById("fpnCanvas");
      if (canvas) {
        canvas.innerHTML = self._renderFloorPlanSVG(plan, floor);
        setTimeout(() => self._wireFloorPlanDrag(), 10);
      }
    }

    svgEl.querySelectorAll(".fpn-drag-room").forEach(g => {
      const rect = g.querySelector(".fpn-drag-rect");
      if (!rect) return;
      rect.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        const idx = parseInt(g.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        const pt = svgPoint(e);
        dragging = { idx, startX: pt.x, startY: pt.y, origX: rm.x, origY: rm.y, resize: false };
        rect.setAttribute("stroke-width", "2.5");
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const idx = parseInt(g.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        if (window.confirm(`Delete '${rm.name}' from floor plan?`)) { rooms.splice(idx, 1); redraw(); }
      });
    });

    svgEl.querySelectorAll(".fpn-resize-handle").forEach(handle => {
      handle.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const idx = parseInt(handle.getAttribute("data-idx"));
        const rm = rooms[idx]; if (!rm) return;
        const pt = svgPoint(e);
        dragging = { idx, startX: pt.x, startY: pt.y, origW: rm.w, origH: rm.h, resize: true };
      });
    });

    // Outdoor zone polygons: drag body (move), drag corner (reshape), add/remove corners
    svgEl.querySelectorAll(".fpn-zone-path").forEach(pth => {
      pth.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(pth.getAttribute("data-zone-idx"));
        const rm = rooms[zi]; if (!rm) return;
        self._ensureZonePoints(rm);
        const pt = svgPoint(e);
        dragging = { zoneBody: true, zi, startX: pt.x, startY: pt.y, ddx: 0, ddy: 0 };
      });
    });
    svgEl.querySelectorAll(".fpn-zone-vtx").forEach(v => {
      v.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(v.getAttribute("data-zone-idx")), vi = parseInt(v.getAttribute("data-vtx"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm); const p = pts[vi]; if (!p) return;
        const pt = svgPoint(e);
        dragging = { zoneVtx: true, zi, vi, startX: pt.x, startY: pt.y, origX: p[0], origY: p[1] };
      });
      v.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const zi = parseInt(v.getAttribute("data-zone-idx")), vi = parseInt(v.getAttribute("data-vtx"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm);
        if (pts.length <= 3) return;
        pts.splice(vi, 1); self._syncRoomBBox(rm); self._rerenderFloorPlanCard();
      });
    });
    svgEl.querySelectorAll(".fpn-zone-mid").forEach(m => {
      m.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const zi = parseInt(m.getAttribute("data-zone-idx")), ei = parseInt(m.getAttribute("data-edge"));
        const rm = rooms[zi]; if (!rm) return;
        const pts = self._ensureZonePoints(rm);
        const a = pts[ei], b = pts[(ei + 1) % pts.length]; if (!a || !b) return;
        pts.splice(ei + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
        self._rerenderFloorPlanCard();
      });
    });

    // Property boundary: drag a corner, add a corner (edge midpoint), remove (right-click)
    svgEl.querySelectorAll(".fpn-prop-vtx").forEach(v => {
      v.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const pi = parseInt(v.getAttribute("data-prop-vtx"));
        const p = (self._propertyPts() || [])[pi];
        if (!p) return;
        const pt = svgPoint(e);
        dragging = { propVtx: pi, startX: pt.x, startY: pt.y, origX: p[0], origY: p[1], property: true };
      });
      v.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const pi = parseInt(v.getAttribute("data-prop-vtx"));
        const pts = self._propertyPts();
        if (pts.length <= 3) return;
        pts.splice(pi, 1); self._rerenderFloorPlanCard();
      });
    });
    svgEl.querySelectorAll(".fpn-prop-mid").forEach(m => {
      m.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ei = parseInt(m.getAttribute("data-prop-edge"));
        const pts = self._propertyPts();
        const a = pts[ei], b = pts[(ei + 1) % pts.length];
        if (!a || !b) return;
        pts.splice(ei + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
        self._rerenderFloorPlanCard();
      });
    });

    // Camera drag + right-click delete
    svgEl.querySelectorAll(".fpn-cam").forEach(g => {
      const dot = g.querySelector(".fpn-cam-dot");
      if (dot) dot.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ci = parseInt(g.getAttribute("data-cam-idx"));
        const cam = (self._camsFor(floor) || [])[ci];
        if (!cam) return;
        const pt = svgPoint(e);
        dragging = { camIdx: ci, startX: pt.x, startY: pt.y, origX: cam.x, origY: cam.y, camera: true, geo: self._planGeometry(floor) };
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const ci = parseInt(g.getAttribute("data-cam-idx"));
        const arr = self._camsFor(floor);
        if (arr[ci] && window.confirm("Delete this camera?")) { arr.splice(ci, 1); redraw(); }
      });
    });

    // Device pins — drag to move (saved with the plan); a tap with no drag
    // opens the entity's controls; right-click removes it.
    svgEl.querySelectorAll(".fpn-ent").forEach(g => {
      g.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const ei = parseInt(g.getAttribute("data-ent-idx"));
        const ent = (self._entsFor(floor) || [])[ei];
        if (!ent) return;
        const pt = svgPoint(e);
        dragging = { entIdx: ei, startX: pt.x, startY: pt.y, origX: ent.x, origY: ent.y, entity: true, moved: false, entId: g.getAttribute("data-ent-id") };
      });
      g.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const ei = parseInt(g.getAttribute("data-ent-idx"));
        const arr = self._entsFor(floor);
        if (arr[ei] && window.confirm("Remove this device from the plan?")) { arr.splice(ei, 1); redraw(); }
      });
    });

    svgEl.addEventListener("mousemove", (e) => {
      if (panning) {
        const rect = svgEl.getBoundingClientRect();
        const sx = panning.w / rect.width, sy = panning.h / rect.height;
        self._editVB = {
          x: panning.vbX - (e.clientX - panning.sx) * sx, y: panning.vbY - (e.clientY - panning.sy) * sy,
          w: panning.w, h: panning.h,
        };
        applyVB();
        return;
      }
      if (!dragging) return;
      const pt = svgPoint(e);
      if (dragging.zoneVtx) {
        const rm = rooms[dragging.zi]; if (!rm || !rm.points) return;
        const p = rm.points[dragging.vi]; if (!p) return;
        p[0] = Math.round(dragging.origX + (pt.x - dragging.startX));
        p[1] = Math.round(dragging.origY + (pt.y - dragging.startY));
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${dragging.zi}"]`);
        if (g) {
          const path = g.querySelector(".fpn-zone-path"); if (path) path.setAttribute("d", self._propPathD(rm.points));
          const dot = g.querySelector(`.fpn-zone-vtx[data-vtx="${dragging.vi}"]`); if (dot) { dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); }
        }
        return;
      }
      if (dragging.zoneBody) {
        dragging.ddx = Math.round(pt.x - dragging.startX); dragging.ddy = Math.round(pt.y - dragging.startY);
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${dragging.zi}"]`);
        if (g) g.setAttribute("transform", `translate(${dragging.ddx},${dragging.ddy})`);
        return;
      }
      if (dragging.property) {
        const p = (self._propertyPts() || [])[dragging.propVtx];
        if (!p) return;
        p[0] = Math.round(dragging.origX + (pt.x - dragging.startX));
        p[1] = Math.round(dragging.origY + (pt.y - dragging.startY));
        const dot = svgEl.querySelector(`.fpn-prop-vtx[data-prop-vtx="${dragging.propVtx}"]`);
        if (dot) { dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); }
        const path = svgEl.querySelector(".fpn-prop-path");
        if (path) path.setAttribute("d", self._propPathD(self._propertyPts()));
        return;
      }
      if (dragging.camera) {
        const cam = (self._camsFor(floor) || [])[dragging.camIdx];
        if (!cam) return;
        cam.x = Math.round(dragging.origX + (pt.x - dragging.startX));
        cam.y = Math.round(dragging.origY + (pt.y - dragging.startY));
        const gc = svgEl.querySelector(`.fpn-cam[data-cam-idx="${dragging.camIdx}"]`);
        if (gc) {
          const dot = gc.querySelector(".fpn-cam-dot"); if (dot) { dot.setAttribute("cx", cam.x); dot.setAttribute("cy", cam.y); }
          const cone = gc.querySelector(".fpn-cam-cone"); if (cone) cone.setAttribute("d", self._clippedCone(cam, dragging.geo));
          const tx = gc.querySelector("text"); if (tx) { tx.setAttribute("x", cam.x); tx.setAttribute("y", cam.y - 5); }
        }
        return;
      }
      if (dragging.entity) {
        const ent = (self._entsFor(floor) || [])[dragging.entIdx];
        if (!ent) return;
        const nx = Math.round(dragging.origX + (pt.x - dragging.startX));
        const ny = Math.round(dragging.origY + (pt.y - dragging.startY));
        if (Math.abs(nx - dragging.origX) > 1 || Math.abs(ny - dragging.origY) > 1) dragging.moved = true;
        ent.x = nx; ent.y = ny;
        const ge = svgEl.querySelector(`.fpn-ent[data-ent-idx="${dragging.entIdx}"]`);
        if (ge) {
          const dot = ge.querySelector(".fpn-ent-dot"); if (dot) { dot.setAttribute("cx", nx); dot.setAttribute("cy", ny); }
          const nm = ge.querySelector(".fpn-ent-nm"); if (nm) { nm.setAttribute("x", nx); nm.setAttribute("y", ny - 4); }
          const vl = ge.querySelector(".fpn-ent-val"); if (vl) { vl.setAttribute("x", nx); vl.setAttribute("y", ny + 6.5); }
        }
        return;
      }
      const rm = rooms[dragging.idx];
      if (!rm) return;
      if (dragging.resize) {
        rm.w = Math.max(15, Math.round(dragging.origW + (pt.x - dragging.startX)));
        rm.h = Math.max(10, Math.round(dragging.origH + (pt.y - dragging.startY)));
      } else {
        rm.x = Math.round(dragging.origX + (pt.x - dragging.startX));
        rm.y = Math.round(dragging.origY + (pt.y - dragging.startY));
      }
      const g = svgEl.querySelector(`.fpn-drag-room[data-idx="${dragging.idx}"]`);
      if (g) {
        const r = g.querySelector(".fpn-drag-rect");
        if (r) { r.setAttribute("x", rm.x); r.setAttribute("y", rm.y); r.setAttribute("width", rm.w); r.setAttribute("height", rm.h); }
        const t = g.querySelector("text");
        if (t) { t.setAttribute("x", rm.x + rm.w / 2); t.setAttribute("y", rm.y + rm.h / 2); }
        const rh = g.querySelector(".fpn-resize-handle");
        if (rh) { rh.setAttribute("x", rm.x + rm.w - 8); rh.setAttribute("y", rm.y + rm.h - 8); }
      }
    });

    const endDrag = () => {
      if (panning) { panning = null; return; }
      if (!dragging) return;
      if (dragging.zoneBody) {
        const rm = rooms[dragging.zi];
        if (rm && rm.points && (dragging.ddx || dragging.ddy)) rm.points.forEach(p => { p[0] += dragging.ddx; p[1] += dragging.ddy; });
      }
      if ((dragging.zoneVtx || dragging.zoneBody) && rooms[dragging.zi]) self._syncRoomBBox(rooms[dragging.zi]);
      // A device pin clicked without dragging → open its HA more-info controls.
      if (dragging.entity && !dragging.moved && dragging.entId) {
        self.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: dragging.entId }, bubbles: true, composed: true }));
      }
      const heavy = dragging.camera || dragging.property || dragging.zoneVtx || dragging.zoneBody;
      dragging = null;
      if (heavy) self._rerenderFloorPlanCard(); else redraw();
    };
    svgEl.addEventListener("mouseup", endDrag);
    svgEl.addEventListener("mouseleave", endDrag);

    svgEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const v = self._editVB || vbFromAttr();
      const p = svgPoint(e);
      const f = e.deltaY < 0 ? 0.85 : 1.18;
      const nw = Math.max(60, Math.min(8000, v.w * f)), nh = Math.max(42, Math.min(8000, v.h * f));
      const fx = nw / v.w, fy = nh / v.h;
      self._editVB = { x: p.x - (p.x - v.x) * fx, y: p.y - (p.y - v.y) * fy, w: nw, h: nh };
      applyVB();
    }, { passive: false });

    svgEl.addEventListener("mousedown", (e) => {
      if (dragging) return;
      const mid = e.button === 1;
      const bg = e.button === 0 && !e.target.closest(".fpn-drag-room, .fpn-resize-handle, .fpn-zone, .fpn-zone-vtx, .fpn-zone-mid, .fpn-zone-path, .fpn-prop-vtx, .fpn-prop-mid, .fpn-cam, .fpn-ent");
      if (!mid && !bg) return;
      e.preventDefault();
      const v = self._editVB || vbFromAttr();
      self._editVB = { x: v.x, y: v.y, w: v.w, h: v.h };
      panning = { sx: e.clientX, sy: e.clientY, vbX: v.x, vbY: v.y, w: v.w, h: v.h };
    });
  }

