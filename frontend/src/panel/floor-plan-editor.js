  // An imported layout file is text from outside, and the editor writes its
  // names, floor keys and values into HTML and SVG attributes. Drop the
  // characters that can close an attribute or open a tag, in keys and values
  // alike, and skip the keys that change an object's prototype.
  _fpCleanImport(value, depth = 0) {
    if (depth > 12) return null;
    if (typeof value === "string") return value.replace(/[<>"`]/g, "");
    if (Array.isArray(value)) return value.map(v => this._fpCleanImport(v, depth + 1));
    if (value && typeof value === "object") {
      const out = {};
      for (const key of Object.keys(value)) {
        if (key === "__proto__" || key === "constructor" || key === "prototype") continue;
        out[this._fpCleanImport(key, depth + 1)] = this._fpCleanImport(value[key], depth + 1);
      }
      return out;
    }
    return value;
  }

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
          <button class="mode-chip" id="fpnUnits">${this._fpUnits() === "metric" ? "Units: Metric" : "Units: Imperial"}</button>
          <span class="fpn-zoom">
            <button class="mode-chip" id="fpnZoomIn" aria-label="${this._esc(this._tx("Zoom in"))}">+</button>
            <button class="mode-chip" id="fpnZoomOut" aria-label="${this._esc(this._tx("Zoom out"))}">−</button>
            <button class="mode-chip" id="fpnZoomFit">⤢ Fit</button>
          </span>
        </div>
      </div>
      <div class="fpn-hint fpn-hint-mouse">Drag to move · bottom-right handle to resize · right-click to delete · click the small circle on an edge to add a corner · scroll or use + and − to zoom · drag empty space to pan</div>
      <div class="fpn-hint fpn-hint-touch">Drag an item with one finger to move it · tap the small circle on an edge to add a corner · pinch with two fingers to zoom · move two fingers to pan</div>
      ${this._fpSelBarHtml()}
      <div class="fpn-canvas" id="fpnCanvas">${this._renderFloorPlanSVG(plan, floor)}</div>
      ${this._renderRoomsListNew(floor)}
      ${this._renderOpeningsNew(floor)}
      ${this._renderCamerasNew(floor)}
      ${this._renderPlanEntitiesNew(floor)}
      <div class="fpn-actions">
        <button class="mode-chip" id="fpnSave">Save Layout</button>
        <span class="toggle-desc fpn-unsaved" id="fpnUnsaved"${this._fpDirty ? "" : " hidden"}>Unsaved changes.</span>
        <button class="mode-chip danger" id="fpnReset">Reset Default</button>
        <button class="mode-chip" id="fpnExport">⬇ Export</button>
        <button class="mode-chip" id="fpnImport">⬆ Import</button>
        <input type="file" id="fpnImportFile" accept=".json,application/json" style="display:none">
      </div>`;
  }

  _fpnAddPropertyButton() {
    const pp = this._propertyPts();
    const has = pp.length >= 3;
    return `<button class="mode-chip" id="fpnAddProperty">${has ? "Clear Property" : "+ Property Line"}</button>`
      + (has ? `<span class="toggle-desc">${this._tHtml("Lot: {area}", { area: this._propertyArea(pp) })}</span>` : "");
  }

  // The item picked by a tap, a click or the keyboard: { kind, i, v }. A
  // kind is room, cam, pin, corner (corner v of shaped room i) or lot (lot
  // corner v). Gives the item itself, or null (and drops the pick) when it
  // is gone.
  _fpSelItem() {
    const s = this._fpSel, floor = this._editorFloor;
    if (!s) return null;
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    let item = null;
    if (s.kind === "room") item = rooms[s.i];
    else if (s.kind === "cam") item = this._camsFor(floor)[s.i];
    else if (s.kind === "pin") item = this._entsFor(floor)[s.i];
    else if (s.kind === "corner") item = this._fpHasOutline(rooms[s.i]) ? rooms[s.i].points[s.v] : null;
    else if (s.kind === "lot") item = (this._propertyPts() || [])[s.v];
    if (!item) this._fpSel = null;
    return item || null;
  }

  // The bar above the plan: what is picked, and buttons that move, resize
  // and delete it, so nothing needs a drag or a right-click.
  _fpSelBarHtml() {
    const item = this._fpSelItem(), s = this._fpSel;
    if (!item) return `<div class="fpn-selbar" id="fpnSelBar"><span class="toggle-desc">Tap or click an item to move, resize or delete it without dragging.</span></div>`;
    const rooms = ((this._getEditingPlan()[this._editorFloor] || {}).rooms) || [];
    let name = "", corners = 0;
    if (s.kind === "room") name = item.name || "";
    else if (s.kind === "cam") name = this._t("CAM {number}", { number: s.i + 1 });
    else if (s.kind === "pin") name = this._entMarkerStyle(item.e).name;
    else if (s.kind === "corner") { name = `${rooms[s.i].name || ""} · ${this._t("Corner {number}", { number: s.v + 1 })}`; corners = rooms[s.i].points.length; }
    else { name = this._t("Lot corner {number}", { number: s.v + 1 }); corners = this._propertyPts().length; }
    const box = s.kind === "room" && !this._fpHasOutline(item);
    return `
      <div class="fpn-selbar" id="fpnSelBar">
        <span class="fpn-sel-name">${this._esc(name)}</span>
        <span class="fpn-sel-tools">
          <button class="mode-chip" data-fpn-move="left" aria-label="${this._esc(this._tx("Move left"))}">←</button>
          <button class="mode-chip" data-fpn-move="up" aria-label="${this._esc(this._tx("Move up"))}">↑</button>
          <button class="mode-chip" data-fpn-move="down" aria-label="${this._esc(this._tx("Move down"))}">↓</button>
          <button class="mode-chip" data-fpn-move="right" aria-label="${this._esc(this._tx("Move right"))}">→</button>
        </span>
        ${box ? `<span class="fpn-sel-tools">
          <button class="mode-chip" data-fpn-size="wider">Wider</button>
          <button class="mode-chip" data-fpn-size="narrower">Narrower</button>
          <button class="mode-chip" data-fpn-size="taller">Taller</button>
          <button class="mode-chip" data-fpn-size="shorter">Shorter</button>
        </span>` : ""}
        <span class="fpn-sel-tools">
          ${s.kind === "pin" ? `<button class="mode-chip" id="fpnSelOpen">Open controls</button>` : ""}
          <button class="mode-chip danger" id="fpnSelDelete"${corners && corners <= 3 ? " disabled" : ""}>Delete</button>
          <button class="mode-chip" id="fpnSelDone">Done</button>
        </span>
        <span class="toggle-desc fpn-keys">Arrow keys move it · Shift moves further · Alt and arrows resize a room · Delete removes it</span>
      </div>`;
  }

  _renderRoomsListNew(floor) {
    const rooms = ((this._getEditingPlan()[floor] || {}).rooms) || [];
    const types = [["room", "Room"], ["bath", "Bath"], ["stairs", "Stairs"], ["door", "Door"]];
    const rows = rooms.map((r, i) => {
      // A type from an older or imported plan is kept as it is.
      const own = types.some(t => t[0] === r.type) ? "" : `<option value="${this._esc(r.type || "")}" selected>${this._esc(r.type || "")}</option>`;
      const type = r.type === "outdoor"
        ? `<span class="new-pl-chip">Outdoor zone</span>`
        : `<label class="field"><span>Type</span><select class="cfg-field fpn-room-type" data-ri="${i}">${own}${types.map(([v, label]) => `<option value="${v}"${r.type === v ? " selected" : ""}>${label}</option>`).join("")}</select></label>`;
      return `
        <div class="cfg-row cfg-row-wrap fpn-room-row" data-ri="${i}">
          <label class="field"><span>Name</span><input class="cfg-field fpn-room-name" data-ri="${i}" value="${this._esc(r.name || "")}"></label>
          ${type}
          <button class="mode-chip danger fpn-room-del" data-ri="${i}" aria-label="${this._esc(this._t("Delete {name}", { name: r.name || "" }))}">Delete</button>
        </div>`;
    }).join("");
    return `
      <div class="mode-bind-head">Rooms <span class="toggle-desc">rename a room, change its type or remove it</span></div>
      ${rows || `<div class="toggle-desc">No rooms on this floor yet — add one above.</div>`}`;
  }

  _renderPlanEntitiesNew(floor) {
    const ents = this._entsFor(floor);
    const op = this._fpBgOpacity();
    const hasBg = !!(this._fpStored("floor_plan_bg") || {})[floor];
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
        <button class="mode-chip" id="fpnBgUpload">${hasBg ? "⬆ Replace Image" : "⬆ Upload Image"}</button>
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
          <span class="new-pl-chip">${this._tHtml("CAM {number}", { number: i + 1 })}</span>
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
    // tabindex -1: Chrome would make the plan a tab stop of its own; Tab
    // should reach the rooms in it instead.
    let svg = `<svg viewBox="${vb}" class="fpn-svg" id="fpnSvg" tabindex="-1" style="width:100%;height:100%;min-height:520px;background:var(--bg);border:1px solid var(--line-soft);border-radius:var(--r-tile);cursor:crosshair;">`;
    svg += '<defs>'
      + '<pattern id="fpn-grid-sm" width="10" height="10" patternUnits="userSpaceOnUse"><path d="M 10 0 L 0 0 0 10" fill="none" stroke="var(--gold)" stroke-opacity="0.05" stroke-width="0.2"/></pattern>'
      + '<pattern id="fpn-grid-lg" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M 50 0 L 0 0 0 50" fill="none" stroke="var(--gold)" stroke-opacity="0.12" stroke-width="0.3"/></pattern>'
      + '</defs>';
    const sel = floor === this._editorFloor && this._fpSelItem() ? this._fpSel : null;
    const picked = (kind, i) => sel && sel.kind === kind && sel.i === i ? " fpn-sel" : "";
    const stop = name => ` tabindex="0" role="button" aria-label="${this._esc(name)}"`;
    const vbp = vb.split(" ").map(Number);
    const gx = vbp[0], gy = vbp[1], gw = vbp[2], gh = vbp[3];
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-sm)"/>`;
    svg += `<rect class="fpn-grid-rect" x="${gx}" y="${gy}" width="${gw}" height="${gh}" fill="url(#fpn-grid-lg)"/>`;

    // A traced-over image of the real plan, escaped like any stored text.
    const traced = (this._fpStored("floor_plan_bg") || {})[floor];
    if (traced) {
      svg += `<image href="${this._esc(traced)}" x="0" y="0" width="100%" height="100%" opacity="${this._fpBgOpacity()}" preserveAspectRatio="xMidYMid meet"/>`;
    }

    // Property boundary (the lot) — draw behind rooms; vertices are draggable.
    let prop = [];
    try { prop = this._propertyPts() || []; } catch (_) { prop = []; }
    if (prop.length >= 2) {
      svg += `<path class="fpn-prop-path" d="${this._propPathD(prop)}" fill="var(--gold)" fill-opacity="0.03" stroke="var(--gold)" stroke-width="1" stroke-dasharray="6 4" pointer-events="none"/>`;
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
      const colors = { room: "var(--gold)", bath: "var(--ink-faint)", stairs: "var(--ember)", door: "var(--warn)", outdoor: "var(--good-ink)" };
      const c = colors[rm.type] || "var(--gold)";
      const out = rm.type === "outdoor";
      const fs = rm.w > 80 ? 7 : (rm.w > 50 ? 5.5 : (rm.w > 25 ? 4 : 3));
      if (out || (rm.points && rm.points.length >= 3)) {
        const zpts = this._zonePoints(rm);
        let zcx = 0, zcy = 0; zpts.forEach(p => { zcx += p[0]; zcy += p[1]; }); zcx /= zpts.length; zcy /= zpts.length;
        svg += `<g class="fpn-zone${picked("room", i)}" data-zone-idx="${i}"${stop(rm.name || "")}>`;
        svg += `<path class="fpn-zone-path" data-zone-idx="${i}" d="${this._propPathD(zpts)}" fill="${c}" fill-opacity="0.06" stroke="${c}" stroke-width="1"${out ? ' stroke-dasharray="4 3"' : ""} style="cursor:move"/>`;
        svg += `<text x="${zcx.toFixed(1)}" y="${zcy.toFixed(1)}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        for (let vi = 0; vi < zpts.length; vi++) { const a = zpts[vi], b = zpts[(vi + 1) % zpts.length]; svg += `<circle class="fpn-zone-mid" data-zone-idx="${i}" data-edge="${vi}" cx="${(a[0] + b[0]) / 2}" cy="${(a[1] + b[1]) / 2}" r="2" fill="none" stroke="${c}" stroke-width="0.6" opacity="0.5" style="cursor:copy"/>`; }
        for (let vi = 0; vi < zpts.length; vi++) { svg += `<circle class="fpn-zone-vtx" data-zone-idx="${i}" data-vtx="${vi}" cx="${zpts[vi][0]}" cy="${zpts[vi][1]}" r="2.8" fill="${c}" stroke="var(--bg)" stroke-width="0.6" style="cursor:grab"/>`; }
        svg += "</g>";
      } else {
        svg += `<g class="fpn-drag-room${picked("room", i)}" data-idx="${i}"${stop(rm.name || "")} style="cursor:move">`;
        svg += `<rect x="${rm.x}" y="${rm.y}" width="${rm.w}" height="${rm.h}" rx="2" fill="${out ? "var(--good-ink)" : "var(--gold)"}" fill-opacity="${out ? 0.06 : 0.08}" stroke="${c}" stroke-width="1" class="fpn-drag-rect"/>`;
        svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2}" text-anchor="middle" fill="${c}" font-size="${fs}" font-family="var(--font-display)" letter-spacing="0.3" pointer-events="none">${this._esc((rm.name || "").toUpperCase())}</text>`;
        if (rm.w > 30 && rm.h > 24) svg += `<text x="${rm.x + rm.w / 2}" y="${rm.y + rm.h / 2 + fs + 2.5}" text-anchor="middle" fill="${c}" opacity="0.6" font-size="${(fs * 0.72).toFixed(1)}" font-family="var(--font-body)" pointer-events="none">${this._fpDim(rm.w)} × ${this._fpDim(rm.h)}</text>`;
        svg += `<rect x="${rm.x + rm.w - 8}" y="${rm.y + rm.h - 8}" width="8" height="8" fill="${c}" opacity="0.35" rx="1" class="fpn-resize-handle" data-idx="${i}" style="cursor:nwse-resize"/>`;
        svg += "</g>";
      }
    }
    for (const lbl of (floorData.labels || [])) {
      svg += `<text x="${lbl.x}" y="${lbl.y}" text-anchor="middle" fill="var(--ink-faint)" font-size="4" font-family="var(--font-body)">${this._esc(lbl.text)}</text>`;
    }

    // Placed openings as wall markers.
    const els = this._elemsFor(floor) || [];
    if (els.length && floorData.rooms && floorData.rooms.length) {
      const house = this._fpBounds(floorData.rooms.filter(r => r.type !== "outdoor"));
      const mnx = house.x0, mny = house.y0, mxx = house.x1, mxy = house.y1;
      els.forEach((e, i) => {
        if (e.type === "dormer") {
          const dp = e.pos != null ? e.pos : 0.5, dcx = mnx + dp * (mxx - mnx), dcy = e.slope === "rear" ? mxy : mny;
          svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${dcx - 4}" y="${dcy - 3}" width="8" height="6" fill="var(--fp-dormer)" opacity="0.9" rx="1.5" pointer-events="none"/>`;
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
        const col = e.type === "window" ? "var(--gold)" : (e.kind === "interior" ? "var(--fp-interior)" : (e.kind === "cellar" ? "var(--fp-cellar)" : (e.kind === "cased" ? "var(--fp-cased)" : "var(--warn)")));
        const ex = horiz ? cx - w / 2 : cx - 2, ey = horiz ? cy - 2 : cy - w / 2, ew = horiz ? w : 4, eh = horiz ? 4 : w;
        svg += `<rect class="fpn-op-marker" data-op-marker="${i}" x="${ex}" y="${ey}" width="${ew}" height="${eh}" fill="${col}" opacity="0.9" rx="1" pointer-events="none"/>`;
      });
    }

    // Cameras — icon + FOV cone, clipped to walls.
    const cams = this._camsFor(floor);
    const camGeo = cams.length ? this._planGeometry(floor) : null;
    for (let ci = 0; ci < cams.length; ci++) {
      const cam = cams[ci], out = cam.indoor === false;
      svg += `<g class="fpn-cam${picked("cam", ci)}" data-cam-idx="${ci}"${stop(this._t("CAM {number}", { number: ci + 1 }))}>`
        + `<path class="fpn-cam-cone" d="${this._clippedCone(cam, camGeo)}" fill="${out ? "var(--warn)" : "var(--ember)"}" fill-opacity="0.1" stroke="${out ? "var(--warn)" : "var(--ember)"}" stroke-opacity="0.6" stroke-width="0.7" pointer-events="none"/>`
        + `<circle class="fpn-cam-dot" cx="${cam.x}" cy="${cam.y}" r="3.2" fill="${out ? "var(--warn)" : "var(--ember)"}" stroke="var(--ink)" stroke-width="0.7" style="cursor:grab"/>`
        + `<text x="${cam.x}" y="${cam.y - 5}" text-anchor="middle" fill="${out ? "var(--warn)" : "var(--ember)"}" font-size="5" font-family="var(--font-body)" pointer-events="none">${ci + 1}</text>`
        + "</g>";
    }

    const ents = this._entsFor(floor);
    for (let ei = 0; ei < ents.length; ei++) {
      const ent = ents[ei];
      if (!ent || !ent.e) continue;
      const ms = this._entMarkerStyle(ent.e);
      const nm = ms.name.length > 16 ? (ms.name.slice(0, 15) + "…") : ms.name;
      svg += `<g class="fpn-ent${picked("pin", ei)}" data-ent-idx="${ei}" data-ent-id="${this._esc(ent.e)}"${stop(ms.name)} style="cursor:pointer">`
        + `<circle class="fpn-ent-dot" cx="${ent.x}" cy="${ent.y}" r="3" fill="${ms.color}" stroke="var(--bg)" stroke-width="0.7"/>`
        + `<text class="fpn-ent-nm" x="${ent.x}" y="${ent.y - 4}" text-anchor="middle" fill="${ms.color}" font-size="3.4" font-family="var(--font-body)" pointer-events="none">${this._esc(nm)}</text>`
        + `<text class="fpn-ent-val" x="${ent.x}" y="${ent.y + 6.5}" text-anchor="middle" fill="var(--ink-dim)" font-size="3" font-family="var(--font-body)" pointer-events="none">${this._esc(ms.val)}</text>`
        + "</g>";
    }

    // A dashed ring round the picked item; it takes no pointer events.
    const item = sel && this._fpSelItem();
    if (item) {
      const ring = 'class="fpn-sel-ring" fill="none" stroke="var(--ink)" stroke-width="0.8" stroke-dasharray="3 2" pointer-events="none"';
      if (sel.kind === "room" && this._fpHasOutline(item)) svg += `<path ${ring} d="${this._propPathD(item.points)}"/>`;
      else if (sel.kind === "room") svg += `<rect ${ring} x="${item.x - 2}" y="${item.y - 2}" width="${item.w + 4}" height="${item.h + 4}" rx="3"/>`;
      else if (sel.kind === "cam" || sel.kind === "pin") svg += `<circle ${ring} cx="${item.x}" cy="${item.y}" r="6"/>`;
      else svg += `<circle ${ring} cx="${item[0]}" cy="${item[1]}" r="5"/>`;
    }
    svg += "</svg>";
    return svg;
  }

  // Redraws only this card, so an edit in progress elsewhere on the
  // settings page is left alone and the scroll position holds.
  _rerenderFloorPlanCard() {
    const card = this.shadowRoot?.getElementById("settings-card-floor_plan_editor");
    if (!card) return;
    const meta = NovaPanel.SETTINGS_CARDS.find(x => x.id === "floor_plan_editor");
    this._setHtml(card, `
        <div class="panel-head"><div class="panel-title">${this._esc(meta.title)}</div></div>
        ${this._floorPlanEditorCardBody()}`);
    this._wireFloorPlanEditor();
  }

  // A layout file is usable when it has at least one floor and every floor
  // is an object whose rooms, if any, are a list of objects.
  _fpLayoutUsable(layout) {
    const isObj = v => !!v && typeof v === "object" && !Array.isArray(v);
    if (!isObj(layout) || !Object.keys(layout).length) return false;
    return Object.values(layout).every(f => isObj(f)
      && (f.rooms === undefined || (Array.isArray(f.rooms) && f.rooms.every(isObj))));
  }

  // Saves one config key and mirrors it into the live data.
  async _fpSaveKey(key, value, live = value) {
    await this._hass.callWS({ type: "nova/update_config", key, value });
    if (this._liveData?.config) this._liveData.config[key] = live;
  }

  // An edit not saved yet: the note beside Save says so until Save.
  _fpMarkDirty() {
    this._fpDirty = true;
    const note = this.shadowRoot?.getElementById("fpnUnsaved");
    if (note) note.hidden = false;
  }

  _fpViewOf(svgEl) {
    const [x, y, w, h] = (svgEl.getAttribute("viewBox") || "0 0 320 150").split(" ").map(Number);
    return { x, y, w, h };
  }
  _fpShowView(svgEl) {
    const v = this._editVB;
    if (!v) return;
    svgEl.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
    svgEl.querySelectorAll(".fpn-grid-rect").forEach(r => {
      r.setAttribute("x", v.x); r.setAttribute("y", v.y); r.setAttribute("width", v.w); r.setAttribute("height", v.h);
    });
    this._fpHitSize(svgEl);
  }
  // A finger's 44 screen pixels in plan units, for the see-through grab area
  // round each handle on a touch screen (styles.js). Redone after each zoom.
  _fpHitSize(svgEl) {
    const ctm = svgEl.getScreenCTM && svgEl.getScreenCTM();
    if (ctm && ctm.a > 0) svgEl.style.setProperty("--fpn-hit", `${Math.round(4400 / ctm.a) / 100}px`);
  }
  // Zooms by f about a plan point (the middle when none is given), within
  // fixed limits, without redrawing the plan. The limit caps f itself, so
  // width and height always change together and the plan keeps its shape.
  _fpZoom(f, at) {
    const svgEl = this.shadowRoot?.getElementById("fpnSvg");
    if (!svgEl) return;
    const v = this._editVB || this._fpViewOf(svgEl), c = at || { x: v.x + v.w / 2, y: v.y + v.h / 2 };
    f = Math.min(Math.max(f, 60 / v.w, 42 / v.h), 8000 / v.w, 8000 / v.h);
    const w = v.w * f, h = v.h * f;
    this._editVB = { x: c.x - (c.x - v.x) * (w / v.w), y: c.y - (c.y - v.y) * (h / v.h), w, h };
    this._fpShowView(svgEl);
  }

  // Picks an item (or none) and redraws the card when that changes.
  _fpSelect(sel) {
    const same = JSON.stringify(sel) === JSON.stringify(this._fpSel);
    this._fpSel = sel;
    if (!same) this._rerenderFloorPlanCard();
  }
  _fpOpenPin(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }
  // Moves the picked item by dx, dy plan units.
  _fpNudge(dx, dy) {
    const item = this._fpSelItem(), s = this._fpSel;
    if (!item) return;
    const shift = p => { p[0] += dx; p[1] += dy; };
    if (s.kind === "room" && this._fpHasOutline(item)) { item.points.forEach(shift); this._syncRoomBBox(item); }
    else if (s.kind === "corner") { shift(item); this._syncRoomBBox(this._getEditingPlan()[this._editorFloor].rooms[s.i]); }
    else if (s.kind === "lot") shift(item);
    else { item.x += dx; item.y += dy; }
    this._fpDirty = true;
    this._rerenderFloorPlanCard();
  }
  // Resizes the picked room, when it is a plain box, keeping a least size.
  _fpResize(dw, dh) {
    const item = this._fpSelItem();
    if (!item || this._fpSel.kind !== "room" || this._fpHasOutline(item)) return;
    item.w = Math.max(15, item.w + dw); item.h = Math.max(10, item.h + dh);
    this._fpDirty = true;
    this._rerenderFloorPlanCard();
  }
  // Deletes the picked item after asking. A shape keeps three corners.
  _fpDeleteSel() {
    const item = this._fpSelItem(), s = this._fpSel, floor = this._editorFloor;
    if (!item) return;
    const rooms = this._getEditingPlan()[floor].rooms;
    let list, at;
    if (s.kind === "room") {
      if (!window.confirm(this._t("Delete '{name}' from floor plan?", { name: item.name }))) return;
      list = rooms; at = s.i;
    } else if (s.kind === "cam") {
      if (!window.confirm(this._tx("Delete this camera?"))) return;
      list = this._camsFor(floor); at = s.i;
    } else if (s.kind === "pin") {
      if (!window.confirm(this._tx("Remove this device from the plan?"))) return;
      list = this._entsFor(floor); at = s.i;
    } else {
      list = s.kind === "lot" ? this._propertyPts() : rooms[s.i].points; at = s.v;
      if (list.length <= 3 || !window.confirm(this._tx("Delete this corner?"))) return;
    }
    list.splice(at, 1);
    if (s.kind === "corner") this._syncRoomBBox(rooms[s.i]);
    this._fpSel = null;
    this._fpDirty = true;
    this._rerenderFloorPlanCard();
  }

  _wireFloorPlanEditor() {
    const root = this.shadowRoot;
    if (!root || !root.getElementById("fpnCanvas")) return;   // the card is not on screen
    const on = (id, type, fn) => { const el = root.getElementById(id); if (el) el.addEventListener(type, fn); return el; };
    const each = (sel, type, fn) => root.querySelectorAll(sel).forEach(el => el.addEventListener(type, () => fn(el)));
    const redraw = () => this._rerenderFloorPlanCard();
    const changed = () => { this._fpDirty = true; redraw(); };
    const floorRooms = () => ((this._getEditingPlan()[this._editorFloor] || {}).rooms) || [];
    const idx = (el, attr) => parseInt(el.getAttribute(attr));
    const stamp = () => Date.now().toString(36);
    // The middle of everything on the floor, where new pins and cameras land.
    const middle = () => {
      const rooms = floorRooms();
      if (!rooms.length) return [100, 80];
      const b = this._fpBounds(rooms);
      return [Math.round((b.x0 + b.x1) / 2), Math.round((b.y0 + b.y1) / 2)];
    };

    // Floors, zoom and units.
    each(".fpn-floor-tab", "click", btn => {
      this._editorFloor = btn.getAttribute("data-fpn-floor");
      this._editVB = null;
      this._fpSel = null;
      redraw();
    });
    on("fpnZoomFit", "click", () => { this._editVB = null; redraw(); });
    on("fpnZoomIn", "click", () => this._fpZoom(0.75));
    on("fpnZoomOut", "click", () => this._fpZoom(1 / 0.75));
    on("fpnUnits", "click", async () => {
      try { await this._fpSaveKey("floor_plan_units", this._fpUnits() === "metric" ? "imperial" : "metric"); }
      catch (err) { this._fail(err, "floor plan units save"); }
      redraw();
    });

    // Rooms, outdoor zones and the lot.
    // A new room or zone gets a numbered name, ready to type over in the
    // Rooms list.
    const nextName = (named, outdoor) => {
      const rooms = floorRooms(), taken = new Set(rooms.map(r => r.name));
      let n = rooms.filter(r => (r.type === "outdoor") === outdoor).length + 1;
      while (taken.has(named(n))) n++;
      return named(n);
    };
    const editName = i => {
      const f = root.querySelector(`.fpn-room-name[data-ri="${i}"]`);
      if (f) { f.focus(); f.select(); }
    };
    on("fpnAddRoom", "click", () => {
      const level = this._getEditingPlan()[this._editorFloor];
      if (!level) return;
      const name = nextName(n => this._t("Room {number}", { number: n }), false);
      (level.rooms = level.rooms || []).push({ name, x: 50, y: 50, w: 60, h: 40, type: "room" });
      changed();
      editName(level.rooms.length - 1);
    });
    on("fpnAddZone", "click", () => {
      const level = this._getEditingPlan()[this._editorFloor];
      if (!level) return;
      const name = nextName(n => this._t("Zone {number}", { number: n }), true);
      level.rooms = level.rooms || [];
      // A new zone sits just below the house, at least 90 units wide.
      const house = level.rooms.filter(r => r.type !== "outdoor");
      let [x, y, w] = [40, 40, 90];
      if (house.length) {
        const b = this._fpBounds(house);
        x = Math.round(b.x0); y = Math.round(b.y1 + 25); w = Math.round(Math.max(b.x1 - b.x0, 90));
      }
      const h = 70;
      level.rooms.push({ name, x, y, w, h, type: "outdoor", points: [[x, y], [x + w, y], [x + w, y + h], [x, y + h]] });
      changed();
      editName(level.rooms.length - 1);
    });

    // The Rooms list: rename, retype or remove.
    const roomOf = el => floorRooms()[idx(el, "data-ri")];
    each(".fpn-room-name", "change", f => {
      const rm = roomOf(f), name = f.value.trim();
      if (!rm) return;
      if (!name || name === rm.name) { redraw(); return; }
      // Interior doors and cased openings name the room they belong to.
      this._elemsFor(this._editorFloor).forEach(e => { if (e.room === rm.name) e.room = name; });
      rm.name = name;
      changed();
    });
    each(".fpn-room-type", "change", f => { const rm = roomOf(f); if (!rm) return; rm.type = f.value; changed(); });
    each(".fpn-room-del", "click", b => {
      const rm = roomOf(b);
      if (!rm || !window.confirm(this._t("Delete '{name}' from floor plan?", { name: rm.name }))) return;
      floorRooms().splice(idx(b, "data-ri"), 1);
      this._fpSel = null;
      changed();
    });

    // The selection bar: move, resize and delete without dragging.
    const STEPS = { left: [-5, 0], right: [5, 0], up: [0, -5], down: [0, 5] };
    each("[data-fpn-move]", "click", b => { const [dx, dy] = STEPS[b.getAttribute("data-fpn-move")] || [0, 0]; this._fpNudge(dx, dy); });
    const SIZES = { wider: [5, 0], narrower: [-5, 0], taller: [0, 5], shorter: [0, -5] };
    each("[data-fpn-size]", "click", b => { const [dw, dh] = SIZES[b.getAttribute("data-fpn-size")] || [0, 0]; this._fpResize(dw, dh); });
    on("fpnSelDelete", "click", () => this._fpDeleteSel());
    on("fpnSelDone", "click", () => this._fpSelect(null));
    on("fpnSelOpen", "click", () => { const pin = this._fpSel?.kind === "pin" && this._fpSelItem(); if (pin) this._fpOpenPin(pin.e); });
    on("fpnAddProperty", "click", () => {
      if (this._propertyPts().length >= 3) {
        if (window.confirm(this._tx("Remove the property boundary?"))) { this._setProperty([]); changed(); }
        return;
      }
      // A rectangle around the floor with a generous margin; a floor with
      // no rooms gets one around a 200 by 150 box near the origin.
      const rooms = floorRooms();
      let b = this._fpBounds(rooms);
      if (!rooms.length || !isFinite(b.x0)) b = { x0: 20, y0: 20, x1: 220, y1: 170 };
      const m = Math.max(150, Math.max(b.x1 - b.x0, b.y1 - b.y0) * 0.7);
      const [l, t, r, btm] = [b.x0 - m, b.y0 - m, b.x1 + m, b.y1 + m].map(Math.round);
      this._setProperty([[l, t], [r, t], [r, btm], [l, btm]]);
      changed();
    });

    // Windows, doors and dormers.
    const addOpening = item => { this._elemsFor(this._editorFloor).push(item); changed(); };
    [["opAddWindow", "window", null], ["opAddExtdoor", "door", "exterior"], ["opAddCellar", "door", "cellar"],
      ["opAddIntdoor", "door", "interior"], ["opAddCased", "door", "cased"]].forEach(([id, type, kind]) => on(id, "click",
      () => addOpening({ id: "e" + stamp(), type, kind, wall: "front", pos: 0.5, w: 20, entity: "" })));
    [["opAddFdormer", "front"], ["opAddRdormer", "rear"]].forEach(([id, slope]) => on(id, "click",
      () => addOpening({ id: "e" + stamp(), type: "dormer", slope, pos: 0.5, entity: "" })));
    each(".op-field-new", "change", f => {
      const item = this._elemsFor(this._editorFloor)[idx(f, "data-i")];
      if (!item) return;
      const key = f.getAttribute("data-op");
      if (key === "w") item.w = this._fpFromReal(parseFloat(f.value) || 4);
      else item[key] = key === "pos" ? parseFloat(f.value) : f.value;
      changed();
    });
    each(".op-del-new", "click", b => { this._elemsFor(this._editorFloor).splice(idx(b, "data-i"), 1); changed(); });
    // Pointing at an opening's row lights up its marker on the plan.
    root.querySelectorAll(".op-row-new").forEach(row => {
      const marker = () => root.querySelector(`.fpn-op-marker[data-op-marker="${row.getAttribute("data-i")}"]`);
      const glow = lit => () => { const m = marker(); if (m) m.classList.toggle("op-glow", lit); };
      row.addEventListener("mouseenter", glow(true));
      row.addEventListener("mouseleave", glow(false));
      const pick = row.querySelector(".op-ent-new");
      if (pick) { pick.addEventListener("focus", glow(true)); pick.addEventListener("blur", glow(false)); }
    });

    // Cameras.
    on("fpnCamAdd", "click", () => {
      const [x, y] = middle();
      this._camsFor(this._editorFloor).push({ id: "c" + stamp(), x, y, angle: 270, fov: 90, range: 55, entity: "", indoor: true });
      changed();
    });
    const camOf = el => this._camsFor(this._editorFloor)[idx(el, "data-ci")];
    const applyCamField = f => {
      const cam = camOf(f);
      if (!cam) return null;
      const key = f.getAttribute("data-cam");
      if (key === "range") cam.range = this._fpFromReal(parseFloat(f.value) || 10);
      else cam[key] = key === "angle" || key === "fov" ? parseFloat(f.value) : f.value;
      return cam;
    };
    root.querySelectorAll(".cam-field-new").forEach(f => {
      // While a slider moves only the cone is redrawn; letting go redraws the card.
      f.addEventListener("input", () => {
        const cam = applyCamField(f);
        if (!cam || f.getAttribute("data-cam") === "entity") return;
        const cone = root.querySelector(`.fpn-cam[data-cam-idx="${f.getAttribute("data-ci")}"] .fpn-cam-cone`);
        if (cone) cone.setAttribute("d", this._clippedCone(cam, this._planGeometry(this._editorFloor)));
      });
      f.addEventListener("change", () => { applyCamField(f); changed(); });
    });
    each(".cam-io-new", "click", b => { const cam = camOf(b); if (!cam) return; cam.indoor = cam.indoor === false; changed(); });
    each(".cam-del-new", "click", b => { this._camsFor(this._editorFloor).splice(idx(b, "data-ci"), 1); changed(); });
    const compute = on("fpnCamCompute", "click", async () => {
      const floor = this._editorFloor, cams = this._camsFor(floor);
      if (!cams.length) return;
      compute.disabled = true;
      const geo = this._planGeometry(floor), openings = this._openingDescriptions(floor);
      for (const cam of cams) {
        const candidates = this._computeCoverage(floor, cam, geo);
        const camera = { entity: cam.entity || "", room: this._roomAt(floor, cam.x, cam.y), fov: cam.fov || 90,
          range_ft: this._fpToReal(cam.range || 55), indoor: cam.indoor !== false, candidates, openings };
        try { cam.coverage = await this._hass.callWS({ type: "nova/compute_camera_coverage", camera }); } catch (_) { /* the next camera still runs */ }
      }
      changed();
    });

    // Save, reset, export and import.
    on("fpnSave", "click", async () => {
      const plan = this._editingPlan, ents = this._editingEntities, cams = this._editingCameras,
        els = this._editingElements, prop = this._editingProperty, hasProp = prop != null;
      if (!plan && !ents && !cams && !els && !hasProp) return;
      const writes = [["floor_plan_rooms", plan], ["floor_plan_entities", ents], ["floor_plan_cameras", cams], ["floor_plan_elements", els]]
        .filter(([, v]) => v);
      if (hasProp) writes.push(["floor_plan_property", { points: prop }]);
      try {
        for (const [key, v] of writes) await this._hass.callWS({ type: "nova/update_config", key, value: JSON.stringify(v) });
        if (this._liveData?.config) writes.forEach(([key, v]) => { this._liveData.config[key] = v; });
        this._editingPlan = this._editingEntities = this._editingCameras = this._editingElements = this._editingProperty = null;
        this._fpDirty = false;
        const note = root.getElementById("fpnUnsaved");
        if (note) note.hidden = true;
        this._notice(this._tx("Saved."));
      } catch (err) { this._fail(err, "floor plan save"); }
    });
    on("fpnReset", "click", async () => {
      if (!window.confirm(this._tx("Go back to the default floor plan? Your own rooms will be deleted."))) return;
      try { await this._fpSaveKey("floor_plan_rooms", "", {}); }
      catch (err) { this._fail(err, "floor plan reset"); }
      this._editingPlan = null;
      this._editorFloor = null;
      this._fpSel = null;
      redraw();
    });
    // A layout file is a manual backup, or a way to move a plan to another install.
    on("fpnExport", "click", () => {
      try {
        const link = document.createElement("a");
        const url = URL.createObjectURL(new Blob([JSON.stringify(this._getEditingPlan(), null, 2)], { type: "application/json" }));
        link.href = url;
        link.download = "nova-floor-plan.json";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (err) { this._fail(err, "floor plan export"); }
    });
    const importFile = root.getElementById("fpnImportFile");
    if (importFile && root.getElementById("fpnImport")) {
      on("fpnImport", "click", () => importFile.click());
      importFile.addEventListener("change", () => {
        const file = importFile.files && importFile.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = ev => {
          try {
            const layout = this._fpCleanImport(JSON.parse(ev.target.result));
            if (!this._fpLayoutUsable(layout)) throw new Error("no usable floors");
            this._editingPlan = layout;
            this._fpSel = null;
            if (!layout[this._editorFloor]) this._editorFloor = Object.keys(layout)[0];
            changed();
          } catch (err) { this._fail(err, "floor plan import — invalid layout file"); }
        };
        reader.readAsText(file);
        importFile.value = "";
      });
    }

    // Device pins and the traced-over image.
    on("fpnEntAdd", "click", () => {
      const input = root.getElementById("fpnEntInput");
      const eid = input && input.value.trim();
      if (!eid || !(this._hass && this._hass.states && this._hass.states[eid])) return;
      const pins = this._entsFor(this._editorFloor);
      if (pins.some(p => p.e === eid)) return;
      const [x, y] = middle();
      pins.push({ e: eid, x, y });
      changed();
    });
    // Camera and opening rows share this button style; only pin chips carry data-ei.
    each(".fpn-ent-del[data-ei]", "click", b => { this._entsFor(this._editorFloor).splice(idx(b, "data-ei"), 1); changed(); });
    const bgFile = root.getElementById("fpnBgFile");
    if (bgFile && root.getElementById("fpnBgUpload")) {
      on("fpnBgUpload", "click", () => bgFile.click());
      bgFile.addEventListener("change", async () => {
        const file = bgFile.files && bgFile.files[0];
        if (!file) return;
        const floor = this._editorFloor;
        try {
          const image = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => reject(new Error("read failed"));
            reader.readAsDataURL(file);
          });
          const all = this._fpStored("floor_plan_bg") || {};
          all[floor] = image;
          await this._fpSaveKey("floor_plan_bg", JSON.stringify(all));
          redraw();
        } catch (err) {
          this._fail(err, "floor plan background upload");
        } finally {
          bgFile.value = "";
        }
      });
    }
    const opacity = root.getElementById("fpnBgOp");
    if (opacity) {
      const shown = root.getElementById("fpnBgOpVal");
      opacity.addEventListener("input", () => {
        if (shown) this._setText(shown, Math.round(parseFloat(opacity.value) * 100) + "%");
        const img = root.querySelector("#fpnSvg image");
        if (img) img.setAttribute("opacity", opacity.value);
      });
      opacity.addEventListener("change", async () => {
        try { await this._fpSaveKey("floor_plan_bg_opacity", String(parseFloat(opacity.value))); }
        catch (err) { this._fail(err, "floor plan bg opacity save"); }
      });
    }

    this._wireFloorPlanDrag();
  }

  // Pointer handling on the plan. One listener per event type on the SVG
  // looks at what was pressed; jsdom cannot run the screen maths, so
  // scripts/smoke_panel.js drives this with stand-ins for them.
  _wireFloorPlanDrag() {
    const svgEl = this.shadowRoot.getElementById("fpnSvg");
    if (!svgEl) return;
    const floor = this._editorFloor, level = this._getEditingPlan()[floor];
    if (!level) return;
    const rooms = level.rooms || [];   // an imported floor may have none yet
    this._fpHitSize(svgEl);
    // Fingers on the plan, by pointer id: two of them pinch and pan.
    const touches = new Map();
    let drag = null, pan = null, pinch = null, settleAfterPinch = false;

    const toPlan = e => {
      const pt = svgEl.createSVGPoint();
      pt.x = e.clientX; pt.y = e.clientY;
      return pt.matrixTransform(svgEl.getScreenCTM().inverse());
    };
    const viewOf = () => this._fpViewOf(svgEl);
    const showView = () => this._fpShowView(svgEl);
    // Quick redraw of the plan alone; the card is redrawn where lists change.
    const redrawPlan = () => {
      const canvas = this.shadowRoot.getElementById("fpnCanvas");
      if (!canvas) return;
      this._setHtml(canvas, this._renderFloorPlanSVG(this._getEditingPlan(), floor));
      setTimeout(() => this._wireFloorPlanDrag(), 10);
    };
    const num = (el, attr) => parseInt(el.getAttribute(attr));
    const place = (el, attrs) => { if (el) Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); };
    const moved = (from, start, now) => Math.round(from + (now - start));
    const midpoint = (pts, edge) => {
      const a = pts[edge], b = pts[(edge + 1) % pts.length];
      if (!a || !b) return false;
      pts.splice(edge + 1, 0, [Math.round((a[0] + b[0]) / 2), Math.round((a[1] + b[1]) / 2)]);
      return true;
    };
    // A drag only starts once the pointer has moved a few pixels (more for a
    // finger, which never holds quite still), so a tap is still a tap.
    const begin = (e, state) => {
      const pt = toPlan(e);
      drag = Object.assign({ startX: pt.x, startY: pt.y, sx: e.clientX, sy: e.clientY, slop: e.pointerType === "mouse" ? 4 : 10, live: false }, state);
    };

    // Pressing the left button on an item starts its drag. Return true when
    // the press was used and must go no further than the plan.
    const press = (e, t) => {
      if (t.classList.contains("fpn-drag-rect") && t.closest(".fpn-drag-room")) {
        const i = num(t.closest(".fpn-drag-room"), "data-idx"), rm = rooms[i];
        if (!rm) return false;
        begin(e, { idx: i, origX: rm.x, origY: rm.y, resize: false });
        t.setAttribute("stroke-width", "2.5");
        return false;   // a room drag still lets the press bubble on
      }
      if (t.classList.contains("fpn-resize-handle")) {
        const i = num(t, "data-idx"), rm = rooms[i];
        if (rm) begin(e, { idx: i, origW: rm.w, origH: rm.h, resize: true });
        return true;
      }
      if (t.classList.contains("fpn-zone-path")) {
        const zi = num(t, "data-zone-idx"), rm = rooms[zi];
        if (rm) { this._ensureZonePoints(rm); begin(e, { zoneBody: true, zi, ddx: 0, ddy: 0 }); }
        return true;
      }
      if (t.classList.contains("fpn-zone-vtx")) {
        const zi = num(t, "data-zone-idx"), vi = num(t, "data-vtx"), rm = rooms[zi];
        const p = rm && this._ensureZonePoints(rm)[vi];
        if (p) begin(e, { zoneVtx: true, zi, vi, origX: p[0], origY: p[1] });
        return true;
      }
      if (t.classList.contains("fpn-zone-mid")) {
        const rm = rooms[num(t, "data-zone-idx")];
        if (rm && midpoint(this._ensureZonePoints(rm), num(t, "data-edge"))) { this._fpDirty = true; this._rerenderFloorPlanCard(); }
        return true;
      }
      if (t.classList.contains("fpn-prop-vtx")) {
        const pi = num(t, "data-prop-vtx"), p = (this._propertyPts() || [])[pi];
        if (p) begin(e, { propVtx: pi, origX: p[0], origY: p[1], property: true });
        return true;
      }
      if (t.classList.contains("fpn-prop-mid")) {
        if (midpoint(this._propertyPts(), num(t, "data-prop-edge"))) { this._fpDirty = true; this._rerenderFloorPlanCard(); }
        return true;
      }
      if (t.classList.contains("fpn-cam-dot") && t.closest(".fpn-cam")) {
        const ci = num(t.closest(".fpn-cam"), "data-cam-idx"), cam = (this._camsFor(floor) || [])[ci];
        if (cam) begin(e, { camIdx: ci, origX: cam.x, origY: cam.y, camera: true, geo: this._planGeometry(floor) });
        return true;
      }
      const pin = t.closest(".fpn-ent");
      if (pin) {
        const ei = num(pin, "data-ent-idx"), ent = (this._entsFor(floor) || [])[ei];
        if (ent) begin(e, { entIdx: ei, origX: ent.x, origY: ent.y, entity: true, moved: false, entId: pin.getAttribute("data-ent-id") });
        return true;
      }
      return false;
    };
    const ITEMS = ".fpn-drag-room, .fpn-resize-handle, .fpn-zone, .fpn-zone-vtx, .fpn-zone-mid, .fpn-zone-path, .fpn-prop-vtx, .fpn-prop-mid, .fpn-cam, .fpn-ent";
    // Pointer events cover a mouse, a pen and a finger in one path. Holding
    // on to the pointer keeps a drag going past the edge of the plan.
    const hold = e => { try { svgEl.setPointerCapture(e.pointerId); } catch (_) { /* not every browser has it */ } };
    const twoFingers = () => {
      const [a, b] = Array.from(touches.values());
      return { mid: { clientX: (a.x + b.x) / 2, clientY: (a.y + b.y) / 2 }, dist: Math.hypot(a.x - b.x, a.y - b.y) || 1 };
    };
    svgEl.addEventListener("pointerdown", e => {
      const t = e.target;
      if (e.pointerType === "touch") {
        touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (touches.size > 1) {
          // A second finger turns whatever the first was doing into a pinch.
          if (drag && drag.live) { finish(false); settleAfterPinch = true; }
          drag = null; pan = null;
          if (touches.size === 2) pinch = twoFingers();
          e.preventDefault();
          return;
        }
      }
      if (e.button === 0 && t !== svgEl && t.closest && svgEl.contains(t)) {
        const before = drag;
        const stop = press(e, t);
        if (stop || drag !== before) {
          e.preventDefault();
          if (drag) hold(e);
          if (stop) e.stopPropagation();
          return;
        }
      }
      if (drag) return;
      // The middle button anywhere, or the left button on empty space, pans.
      // One finger on empty space is left to the page, so it still scrolls.
      const empty = e.button === 0 && !t.closest(ITEMS) && e.pointerType !== "touch";
      if (e.button !== 1 && !empty) return;
      e.preventDefault();
      const v = this._editVB || viewOf();
      this._editVB = { x: v.x, y: v.y, w: v.w, h: v.h };
      pan = { sx: e.clientX, sy: e.clientY, vbX: v.x, vbY: v.y, w: v.w, h: v.h };
      hold(e);
    });

    // Right-click removes rooms, corners, cameras and pins.
    svgEl.addEventListener("contextmenu", e => {
      const t = e.target;
      drag = null; pan = null;   // a long press on a touch screen lands here mid-press
      if (t.closest(".fpn-drag-room, .fpn-cam, .fpn-ent") || t.classList.contains("fpn-zone-vtx") || t.classList.contains("fpn-prop-vtx")) this._fpSel = null;
      const room = t.closest(".fpn-drag-room"), cam = t.closest(".fpn-cam"), pin = t.closest(".fpn-ent");
      if (t.classList.contains("fpn-zone-vtx")) {
        e.preventDefault();
        const rm = rooms[num(t, "data-zone-idx")];
        if (!rm) return;
        const pts = this._ensureZonePoints(rm);
        if (pts.length <= 3) return;
        pts.splice(num(t, "data-vtx"), 1); this._syncRoomBBox(rm); this._fpDirty = true; this._rerenderFloorPlanCard();
      } else if (t.classList.contains("fpn-prop-vtx")) {
        e.preventDefault();
        const pts = this._propertyPts();
        if (pts.length <= 3) return;
        pts.splice(num(t, "data-prop-vtx"), 1); this._fpDirty = true; this._rerenderFloorPlanCard();
      } else if (room) {
        e.preventDefault();
        const i = num(room, "data-idx"), rm = rooms[i];
        if (rm && window.confirm(this._t("Delete '{name}' from floor plan?", { name: rm.name }))) { rooms.splice(i, 1); this._fpMarkDirty(); redrawPlan(); }
      } else if (cam) {
        e.preventDefault();
        const list = this._camsFor(floor), ci = num(cam, "data-cam-idx");
        if (list[ci] && window.confirm(this._tx("Delete this camera?"))) { list.splice(ci, 1); this._fpMarkDirty(); redrawPlan(); }
      } else if (pin) {
        e.preventDefault();
        const list = this._entsFor(floor), ei = num(pin, "data-ent-idx");
        if (list[ei] && window.confirm(this._tx("Remove this device from the plan?"))) { list.splice(ei, 1); this._fpMarkDirty(); redrawPlan(); }
      }
    });

    // Moving updates the dragged item's own SVG nodes; the model moves with it.
    const follow = pt => {
      const d = drag, dx = pt.x - d.startX, dy = pt.y - d.startY;
      if (d.zoneVtx) {
        const rm = rooms[d.zi], p = rm && rm.points && rm.points[d.vi];
        if (!p) return;
        p[0] = moved(d.origX, d.startX, pt.x); p[1] = moved(d.origY, d.startY, pt.y);
        const g = svgEl.querySelector(`.fpn-zone[data-zone-idx="${d.zi}"]`);
        if (g) {
          place(g.querySelector(".fpn-zone-path"), { d: this._propPathD(rm.points) });
          place(g.querySelector(`.fpn-zone-vtx[data-vtx="${d.vi}"]`), { cx: p[0], cy: p[1] });
        }
      } else if (d.zoneBody) {
        d.ddx = Math.round(dx); d.ddy = Math.round(dy);
        place(svgEl.querySelector(`.fpn-zone[data-zone-idx="${d.zi}"]`), { transform: `translate(${d.ddx},${d.ddy})` });
      } else if (d.property) {
        const p = (this._propertyPts() || [])[d.propVtx];
        if (!p) return;
        p[0] = moved(d.origX, d.startX, pt.x); p[1] = moved(d.origY, d.startY, pt.y);
        place(svgEl.querySelector(`.fpn-prop-vtx[data-prop-vtx="${d.propVtx}"]`), { cx: p[0], cy: p[1] });
        place(svgEl.querySelector(".fpn-prop-path"), { d: this._propPathD(this._propertyPts()) });
      } else if (d.camera) {
        const cam = (this._camsFor(floor) || [])[d.camIdx];
        if (!cam) return;
        cam.x = moved(d.origX, d.startX, pt.x); cam.y = moved(d.origY, d.startY, pt.y);
        const g = svgEl.querySelector(`.fpn-cam[data-cam-idx="${d.camIdx}"]`);
        if (g) {
          place(g.querySelector(".fpn-cam-dot"), { cx: cam.x, cy: cam.y });
          place(g.querySelector(".fpn-cam-cone"), { d: this._clippedCone(cam, d.geo) });
          place(g.querySelector("text"), { x: cam.x, y: cam.y - 5 });
        }
      } else if (d.entity) {
        const ent = (this._entsFor(floor) || [])[d.entIdx];
        if (!ent) return;
        const x = moved(d.origX, d.startX, pt.x), y = moved(d.origY, d.startY, pt.y);
        if (Math.abs(x - d.origX) > 1 || Math.abs(y - d.origY) > 1) d.moved = true;
        ent.x = x; ent.y = y;
        const g = svgEl.querySelector(`.fpn-ent[data-ent-idx="${d.entIdx}"]`);
        if (g) {
          place(g.querySelector(".fpn-ent-dot"), { cx: x, cy: y });
          place(g.querySelector(".fpn-ent-nm"), { x, y: y - 4 });
          place(g.querySelector(".fpn-ent-val"), { x, y: y + 6.5 });
        }
      } else {
        const rm = rooms[d.idx];
        if (!rm) return;
        if (d.resize) {
          rm.w = Math.max(15, Math.round(d.origW + dx)); rm.h = Math.max(10, Math.round(d.origH + dy));
        } else {
          rm.x = moved(d.origX, d.startX, pt.x); rm.y = moved(d.origY, d.startY, pt.y);
        }
        const g = svgEl.querySelector(`.fpn-drag-room[data-idx="${d.idx}"]`);
        if (g) {
          place(g.querySelector(".fpn-drag-rect"), { x: rm.x, y: rm.y, width: rm.w, height: rm.h });
          place(g.querySelector("text"), { x: rm.x + rm.w / 2, y: rm.y + rm.h / 2 });
          place(g.querySelector(".fpn-resize-handle"), { x: rm.x + rm.w - 8, y: rm.y + rm.h - 8 });
        }
      }
    };
    svgEl.addEventListener("pointermove", e => {
      if (touches.has(e.pointerId)) touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && touches.size >= 2) {
        // Pan by how far the middle moved, then zoom about the new middle.
        const now = twoFingers(), v = this._editVB || viewOf(), from = toPlan(pinch.mid), to = toPlan(now.mid);
        this._editVB = { x: v.x + from.x - to.x, y: v.y + from.y - to.y, w: v.w, h: v.h };
        showView();
        this._fpZoom(pinch.dist / now.dist, toPlan(now.mid));
        pinch = now;
        return;
      }
      if (pan) {
        const box = svgEl.getBoundingClientRect();
        this._editVB = { x: pan.vbX - (e.clientX - pan.sx) * (pan.w / box.width), y: pan.vbY - (e.clientY - pan.sy) * (pan.h / box.height), w: pan.w, h: pan.h };
        showView();
      } else if (drag) {
        if (!drag.live && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < drag.slop) return;
        drag.live = true;
        follow(toPlan(e));
      }
    });

    // Ends a drag. `redraw` is false when a pinch takes over, as the plan
    // must stay the same element while both fingers are on it.
    const finish = (redraw = true) => {
      if (pan) { pan = null; return; }
      if (!drag) return;
      const d = drag, zone = rooms[d.zi];
      drag = null;
      if (!d.live) {
        // A tap picks the item; a pin also opens its Home Assistant controls.
        if (d.entity && d.entId) this._fpOpenPin(d.entId);
        const rect = svgEl.querySelector(`.fpn-drag-room[data-idx="${d.idx}"] .fpn-drag-rect`);
        if (rect && !d.resize) rect.setAttribute("stroke-width", "1");
        this._fpSelect(d.entity ? { kind: "pin", i: d.entIdx } : d.camera ? { kind: "cam", i: d.camIdx }
          : d.zoneVtx ? { kind: "corner", i: d.zi, v: d.vi } : d.property ? { kind: "lot", v: d.propVtx }
          : { kind: "room", i: d.zoneBody ? d.zi : d.idx });
        return;
      }
      if (d.zoneBody && zone && zone.points && (d.ddx || d.ddy)) zone.points.forEach(p => { p[0] += d.ddx; p[1] += d.ddy; });
      if ((d.zoneVtx || d.zoneBody) && zone) this._syncRoomBBox(zone);
      // A pin that moved less than a plan unit was still only tapped.
      if (d.entity && !d.moved && d.entId) this._fpOpenPin(d.entId);
      this._fpMarkDirty();
      if (!redraw) return;
      if (d.camera || d.property || d.zoneVtx || d.zoneBody) this._rerenderFloorPlanCard(); else redrawPlan();
    };
    const lift = e => {
      touches.delete(e.pointerId);
      if (touches.size < 2) pinch = null;
      if (!touches.size && settleAfterPinch) { settleAfterPinch = false; this._rerenderFloorPlanCard(); return; }
      finish();
    };
    svgEl.addEventListener("pointerup", lift);
    svgEl.addEventListener("pointercancel", lift);
    // While a finger drags or pinches, the page must not scroll or zoom
    // under it. One finger on empty space is left alone.
    svgEl.addEventListener("touchmove", e => {
      if (drag || pinch || (e.touches && e.touches.length > 1)) e.preventDefault();
    }, { passive: false });

    // Keyboard: Tab reaches each room, zone, camera and pin, and focusing
    // one picks it. Arrows move it (Shift: five times as far), Alt and
    // arrows resize a room, Delete removes it, Enter opens a pin, Escape
    // lets go.
    const STOPS = ".fpn-drag-room, .fpn-zone, .fpn-cam, .fpn-ent";
    const pickOf = g => g.classList.contains("fpn-cam") ? { kind: "cam", i: num(g, "data-cam-idx") }
      : g.classList.contains("fpn-ent") ? { kind: "pin", i: num(g, "data-ent-idx") }
      : { kind: "room", i: num(g, g.classList.contains("fpn-zone") ? "data-zone-idx" : "data-idx") };
    const stopOf = e => e.target && e.target.closest ? e.target.closest(STOPS) : null;
    svgEl.addEventListener("focusin", e => {
      const g = stopOf(e);
      if (g && !drag && !pinch) this._fpSelect(pickOf(g));
    });
    const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    svgEl.addEventListener("keydown", e => {
      const g = stopOf(e);
      if (!g) return;
      const pick = pickOf(g);
      if (ARROWS[e.key]) {
        e.preventDefault();
        this._fpSel = pick;
        const step = e.shiftKey ? 25 : 5, [dx, dy] = ARROWS[e.key];
        if (e.altKey) this._fpResize(dx * step, dy * step); else this._fpNudge(dx * step, dy * step);
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        this._fpSel = pick;
        this._fpDeleteSel();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (pick.kind === "pin") this._fpOpenPin(g.getAttribute("data-ent-id")); else this._fpSelect(pick);
      } else if (e.key === "Escape") {
        this._fpSelect(null);
      }
    });

    // The wheel zooms about the pointer, within fixed limits.
    svgEl.addEventListener("wheel", e => {
      e.preventDefault();
      this._fpZoom(e.deltaY < 0 ? 0.85 : 1.18, toPlan(e));
    }, { passive: false });
  }
