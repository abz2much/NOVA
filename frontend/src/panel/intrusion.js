  // ─── Intrusion ────────────────────────────────────────────────────────
  // Ported from Classic's own Intrusion tab (nova-panel.js). Safety-relevant
  // (call-off/acknowledge affect real escalation), so this is a straight
  // port of Classic's exact websocket calls and semantics — no new
  // behavior invented here. The timeout select and vision-confirm toggle
  // reuse the generic .cfg-field/.toggle-btn autosave already wired for
  // Settings (see _saveSetting's dashboard-only render guard above).
  _htmlIntrusion() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion</div>
            <div class="panel-meta" id="newIntrStatus">—</div>
          </div>
          <div class="stub-body">Last intrusion snapshot and false-alarm call-off. When Nova confirms an intruder on camera it grabs a still; if it's not real, call it off here or say "it's a false alarm".</div>
          <div id="newIntrBody"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Intrusion Log</div>
            <div class="panel-meta" id="newIlogLearn">—</div>
          </div>
          <div class="stub-body">Every intrusion event with its snapshot. Mark each one <b>real</b> or <b>false alarm</b> — Nova learns from your labels and stops firing the low-confidence alerts for patterns you keep calling false. A confirmed intrusion always alerts, no matter what it has learned.</div>
          <div class="cfg-row"><button class="mode-chip" id="newIlogRefresh">⟳ REFRESH</button></div>
          <div id="newIlogBody"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  async _fetchIntrusion() {
    if (!this._hass) return;
    try {
      const cfg = this._liveData?.config || {};
      if (cfg.intrusion_response_timeout != null) this._intrTimeout = cfg.intrusion_response_timeout;
    } catch (_) {}
    try {
      this._intr = await this._hass.callWS({ type: "nova/intrusion", action: "status" });
    } catch (_) {
      this._intr = { error: true };
    }
    this._renderIntrusionStatus();
  }

  _renderIntrusionStatus() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIntrBody");
    const statusEl = root?.getElementById("newIntrStatus");
    if (!body) return;
    const s = this._intr || {};
    if (s.error) {
      body.innerHTML = `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`;
      if (statusEl) statusEl.textContent = "—";
      return;
    }
    if (statusEl) {
      statusEl.innerHTML = s.called_off
        ? `<span class="diag-warn">CALLED OFF · ${s.suppressed_for}s</span>`
        : `<span class="diag-ok">ARMED</span>`;
    }
    const snap = s.last_snapshot;
    let html = "";
    if (snap && snap.image_b64) {
      const when = snap.ts ? new Date(snap.ts * 1000).toLocaleString() : "";
      html += `<div class="intr-snap">
        <img src="data:image/jpeg;base64,${snap.image_b64}" alt="intrusion snapshot" class="intr-img">
        <div class="toggle-desc">${this._esc((snap.camera || "").replace("camera.", "").replace(/_/g, " "))} · ${this._esc(when)}</div>
      </div>`;
    } else {
      html += `<div class="stub-body">No intrusion snapshots captured. This stays empty unless Nova confirms an intruder on camera.</div>`;
    }
    if (s.false_alarms_24h) {
      html += `<div class="stub-body">${s.false_alarms_24h} false alarm${s.false_alarms_24h === 1 ? "" : "s"} called off in the last 24h</div>`;
    }
    if (s.acknowledged) {
      html += `<div class="stub-body">✓ Acknowledged — automatic escalation held (you're handling it)</div>`;
    }
    html += `
      <div class="cfg-row">
        <label>Auto-escalate if no response after</label>
        <select class="cfg-field" data-cfg-key="intrusion_response_timeout">${this._optSelect([["60", "1 min"], ["120", "2 min"], ["180", "3 min"], ["300", "5 min"], ["600", "10 min"]], String(this._intrTimeout || 120))}</select>
      </div>
      <div class="cfg-row">
        <label>Confirm Frigate person with Nova vision before alarming</label>
        <button class="toggle-btn ${this._liveData?.config?.intrusion_vision_confirm !== false ? "on" : "off"}" data-cfg-key="intrusion_vision_confirm" data-cfg-val="${this._liveData?.config?.intrusion_vision_confirm !== false ? "false" : "true"}">${this._liveData?.config?.intrusion_vision_confirm !== false ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        <button class="mode-chip new-intr-ack">✓ I'M LOOKING (HOLD)</button>
        <button class="mode-chip new-intr-dismiss">✕ CALL OFF (FALSE ALARM)</button>
      </div>`;
    body.innerHTML = html;
    body.querySelectorAll(".toggle-btn[data-cfg-key], select.cfg-field[data-cfg-key]").forEach(el => {
      if (el.tagName === "BUTTON") {
        el.addEventListener("click", () => this._saveSetting(el.getAttribute("data-cfg-key"), el.getAttribute("data-cfg-val") === "true"));
      } else {
        el.addEventListener("change", () => this._saveSetting(el.getAttribute("data-cfg-key"), el.value));
      }
    });
    const dismissBtn = body.querySelector(".new-intr-dismiss");
    dismissBtn?.addEventListener("click", async () => {
      if (!this._hass) return;
      dismissBtn.disabled = true;
      try {
        await this._hass.callWS({ type: "nova/intrusion", action: "dismiss", reason: "panel" });
        await this._fetchIntrusion();
      } catch (err) {
        console.error("Nova: intrusion dismiss failed", err);
        dismissBtn.disabled = false;
      }
    });
    const ackBtn = body.querySelector(".new-intr-ack");
    ackBtn?.addEventListener("click", async () => {
      if (!this._hass) return;
      ackBtn.disabled = true;
      try {
        await this._hass.callWS({ type: "nova/intrusion", action: "acknowledge", reason: "panel" });
        await this._fetchIntrusion();
      } catch (err) {
        console.error("Nova: intrusion acknowledge failed", err);
        ackBtn.disabled = false;
      }
    });
  }

  async _fetchIntrusionLog() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIlogBody");
    const side = root?.getElementById("newIlogLearn");
    if (!this._hass || !body) return;
    try {
      const res = await this._hass.callWS({ type: "nova/intrusion", action: "log", limit: 40 });
      this._ilog = res;
      const L = res?.learning || {};
      if (side) side.textContent = `${L.labeled || 0}/${L.events || 0} labelled`;
      body.innerHTML = this._renderIntrusionLogHtml(res);
      this._wireIntrusionLabels();
    } catch (err) {
      body.innerHTML = `<div class="stub-body">Could not load the log.</div>`;
    }
  }

  _renderIntrusionLogHtml(res) {
    const evs = (res && res.events) || [];
    if (!evs.length) {
      return `<div class="stub-body">No intrusion events recorded yet.</div>`;
    }
    const damped = ((res.learning || {}).damped_patterns || []).length;
    let html = "";
    if (damped) {
      html += `<div class="stub-body">Nova has learned ${damped} benign pattern${damped === 1 ? "" : "s"} — low-confidence alerts for these stay quiet.</div>`;
    }
    for (const e of evs) {
      const when = new Date((e.ts || 0) * 1000).toLocaleString();
      const kindCls = { confirmed: "diag-down", unresolved: "diag-warn", investigating: "diag-off" }[e.kind] || "diag-off";
      const label = e.label || "";
      html += `<div class="new-ilog-item" data-ev="${this._esc(e.id)}">
        <div class="cfg-row">
          <span class="${kindCls}">${this._esc((e.kind || "").toUpperCase())}</span>
          <span class="toggle-desc">${this._esc(when)}</span>
        </div>
        <div class="toggle-desc">${this._esc(e.breach || e.camera || "activity")}${e.reason ? " — " + this._esc(e.reason) : ""}</div>
        ${e.image_b64 ? `<img class="intr-img" src="data:image/jpeg;base64,${e.image_b64}" alt="snapshot">` : ""}
        <div class="mode-grid">
          <button class="mode-chip new-ilog-btn${label === "real" ? " mode-chip-on" : ""}" data-label="real">REAL</button>
          <button class="mode-chip new-ilog-btn${label === "false" ? " mode-chip-on" : ""}" data-label="false">FALSE ALARM</button>
          ${label ? `<button class="mode-chip new-ilog-btn" data-label="">CLEAR</button>` : ""}
        </div>
      </div>`;
    }
    return html;
  }

  _wireIntrusionLabels() {
    const body = this.shadowRoot?.getElementById("newIlogBody");
    body?.querySelectorAll(".new-ilog-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const item = btn.closest(".new-ilog-item");
        const id = item?.getAttribute("data-ev");
        if (!id || !this._hass) return;
        try {
          await this._hass.callWS({ type: "nova/intrusion", action: "label", event_id: id, label: btn.getAttribute("data-label") });
          await this._fetchIntrusionLog();
        } catch (err) { console.error("Nova: intrusion label failed", err); }
      });
    });
  }

  _wireIntrusion() {
    const root = this.shadowRoot;
    this._fetchIntrusion();
    const refreshBtn = root.getElementById("newIlogRefresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        refreshBtn.disabled = true;
        const orig = refreshBtn.textContent;
        refreshBtn.textContent = "⟳ LOADING…";
        try { await this._fetchIntrusionLog(); }
        finally { refreshBtn.disabled = false; refreshBtn.textContent = orig; }
      });
    } else {
      this._fetchIntrusionLog();
    }
  }

