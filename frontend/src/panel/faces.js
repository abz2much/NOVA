  // ─── Faces ────────────────────────────────────────────────────────────
  // Recent faces Frigate or Double Take named, and the household resident
  // roster. Nova has no face engine of its own, and no image is shown or
  // stored here. The commands are admin only (nova/list_faces,
  // nova/add_resident, nova/remove_resident).
  _htmlFaces() {
    const on = this._liveData?.config?.face_stand_down === true;
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Faces</div>
            <div class="panel-meta" id="facesMeta">—</div>
          </div>
          <div class="stub-body">Nova does not run its own face engine. It reads names from Frigate or Double Take. No images are shown or stored here. The residents below are the people Nova treats as members of the household.</div>
          <div class="cfg-row"><button class="mode-chip" id="facesRefresh">⟳ REFRESH</button></div>
          <div id="facesBody"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Residents</div>
            <div class="panel-meta" id="facesResidentsMeta">—</div>
          </div>
          <div class="stub-body">Use the name exactly as Frigate or Double Take reports it. Case and spacing do not matter. Intrusion stand down is <b>${on ? "ON" : "OFF"}</b>${on ? "; change it under Settings → Security alarm." : " (the default); change it under Settings → Security alarm."}</div>
          <div class="cfg-row">
            <input class="cfg-field" id="facesAddName" maxlength="60" placeholder="Resident name">
            <button class="mode-chip" id="facesAdd">ADD RESIDENT</button>
          </div>
          <div class="toggle-desc" id="facesMsg"></div>
          <div id="facesResidents"></div>
        </div>
    `;
  }

  _facesAgeText(sec) {
    const s = Number(sec) || 0;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  async _fetchFaces() {
    if (!this._hass) return;
    try {
      this._faces = await this._hass.callWS({ type: "nova/list_faces", limit: 40 });
    } catch (err) {
      this._faces = { error: true, code: err && err.code };
    }
    this._renderFacesBody();
  }

  _renderFacesBody() {
    const root = this.shadowRoot;
    const body = root?.getElementById("facesBody");
    const resBox = root?.getElementById("facesResidents");
    if (!body || !resBox) return;
    const f = this._faces || {};
    const meta = root.getElementById("facesMeta");
    const resMeta = root.getElementById("facesResidentsMeta");
    if (f.error) {
      const denied = f.code === "unauthorized";
      this._setHtml(body, `<div class="stub-body">${denied ? "Faces needs a Home Assistant administrator." : "Could not load faces."}</div>`);
      this._setHtml(resBox, "");
      if (meta) this._setText(meta, "—");
      return;
    }
    const faces = f.faces || [];
    const residents = f.residents || [];
    const src = f.sources || {};
    if (meta) this._setText(meta, this._t("{count} RECENT", { count: faces.length }));
    if (resMeta) this._setText(resMeta, this._t(residents.length === 1 ? "{count} RESIDENT" : "{count} RESIDENTS", { count: residents.length }));
    if (!faces.length) {
      this._setHtml(body, src.configured === false
        ? `<div class="stub-body">No face recognition source found. Set up Frigate face recognition or Double Take, and make sure Home Assistant has MQTT. Recent faces appear here once one of them names someone.</div>`
        : `<div class="stub-body">No faces seen recently. Names appear here when Frigate or Double Take recognises someone.</div>`);
    } else {
      this._setHtml(body, faces.map(r => `
        <div class="feed-row face-row">
          <span class="feed-text"><b>${this._esc(r.name)}</b>
            <span class="${r.known ? "diag-ok" : "diag-warn"}">${r.known ? "KNOWN" : "UNKNOWN"}</span>
            ${r.resident ? `<span class="diag-ok">RESIDENT</span>` : ""}
            <span class="dim">· ${this._esc(r.camera)} · ${Math.round(r.confidence)}% · ${this._esc(this._facesAgeText(r.age_seconds))}</span>
          </span>
          ${r.known ? (r.resident
            ? `<button class="mode-chip" data-face-remove="${this._esc(r.name)}">REMOVE RESIDENT</button>`
            : `<button class="mode-chip" data-face-add="${this._esc(r.name)}">ADD RESIDENT</button>`) : ""}
        </div>`).join(""));
    }
    this._setHtml(resBox, residents.length ? residents.map(n => `
        <div class="feed-row resident-row">
          <span class="feed-text"><b>${this._esc(n)}</b></span>
          <button class="mode-chip" data-face-remove="${this._esc(n)}">REMOVE</button>
        </div>`).join("")
      : `<div class="stub-body">No residents yet. Add the people who live here.</div>`);
    root.querySelectorAll("[data-face-add]").forEach(b => {
      b.addEventListener("click", () => this._faceAdd(b.getAttribute("data-face-add")));
    });
    root.querySelectorAll("[data-face-remove]").forEach(b => {
      b.addEventListener("click", () => this._faceRemove(b.getAttribute("data-face-remove")));
    });
  }

  async _faceAdd(name) {
    const root = this.shadowRoot;
    const msg = root?.getElementById("facesMsg");
    if (!this._hass || !String(name || "").trim()) return;
    try {
      const res = await this._hass.callWS({ type: "nova/add_resident", name });
      if (msg) this._setText(msg, this._t(res.added ? "{name} added." : "{name} is already a resident.", { name }));
      const input = root?.getElementById("facesAddName");
      if (input) input.value = "";
    } catch (err) {
      if (msg) this._setText(msg, (err && err.message) || "Could not add that name.");
      return;
    }
    await this._fetchFaces();
  }

  async _faceRemove(name) {
    const msg = this.shadowRoot?.getElementById("facesMsg");
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/remove_resident", name });
      if (msg) this._setText(msg, this._t("{name} removed.", { name }));
    } catch (err) {
      if (msg) this._setText(msg, (err && err.message) || "Could not remove that name.");
      return;
    }
    await this._fetchFaces();
  }

  _wireFaces() {
    const root = this.shadowRoot;
    root.getElementById("facesRefresh")?.addEventListener("click", () => this._fetchFaces());
    root.getElementById("facesAdd")?.addEventListener("click", () => {
      this._faceAdd(root.getElementById("facesAddName")?.value || "");
    });
    root.getElementById("facesAddName")?.addEventListener("keydown", e => {
      if (e.key === "Enter") this._faceAdd(e.target.value || "");
    });
    if (this._faces) this._renderFacesBody();
    this._fetchFaces();
  }

