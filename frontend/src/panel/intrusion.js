  // ─── Intrusion ────────────────────────────────────────────────────────
  // Safety relevant: call off and acknowledge change real escalation, so
  // this tab only shows Nova's state and sends its exact websocket actions.
  // The timeout select and vision toggle save through _saveSetting like
  // the controls on the Settings tab.
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

  // Sends one nova/intrusion action and returns the reply.
  _intrSend(action, extra) {
    return this._hass.callWS(Object.assign({ type: "nova/intrusion", action }, extra));
  }

  async _fetchIntrusion() {
    if (!this._hass) return;
    const timeout = this._liveData?.config?.intrusion_response_timeout;
    if (timeout != null) this._intrTimeout = timeout;
    let status;
    try { status = await this._intrSend("status"); } catch (_) { status = { error: true }; }
    this._intr = status;
    this._renderIntrusionStatus();
  }

  _renderIntrusionStatus() {
    const root = this.shadowRoot, body = root?.getElementById("newIntrBody");
    if (!body) return;
    const badge = root.getElementById("newIntrStatus"), s = this._intr || {};
    if (s.error) {
      this._setHtml(body, `<div class="stub-body">Couldn't load — restart Home Assistant after updating.</div>`);
      if (badge) this._setText(badge, "—");
      return;
    }
    if (badge) this._setHtml(badge, s.called_off
      ? `<span class="diag-warn">${this._tHtml("CALLED OFF · {seconds}s", { seconds: s.suppressed_for })}</span>`
      : `<span class="diag-ok">ARMED</span>`);

    const parts = [];
    const shot = s.last_snapshot;
    if (shot && shot.image_b64) {
      const camName = (shot.camera || "").replace("camera.", "").replace(/_/g, " ");
      const taken = shot.ts ? new Date(shot.ts * 1000).toLocaleString() : "";
      parts.push(`<div class="intr-snap">
        <img src="data:image/jpeg;base64,${shot.image_b64}" alt="intrusion snapshot" class="intr-img">
        <div class="toggle-desc">${this._esc(camName)} · ${this._esc(taken)}</div>
      </div>`);
    } else {
      parts.push(`<div class="stub-body">No intrusion snapshots captured. This stays empty unless Nova confirms an intruder on camera.</div>`);
    }
    const falseAlarms = s.false_alarms_24h;
    if (falseAlarms) {
      const line = falseAlarms === 1 ? "{count} false alarm called off in the last 24h" : "{count} false alarms called off in the last 24h";
      parts.push(`<div class="stub-body">${this._tHtml(line, { count: falseAlarms })}</div>`);
    }
    if (s.acknowledged) parts.push(`<div class="stub-body">✓ Acknowledged — automatic escalation held (you're handling it)</div>`);
    const waits = [["60", "1 min"], ["120", "2 min"], ["180", "3 min"], ["300", "5 min"], ["600", "10 min"]];
    const vision = this._liveData?.config?.intrusion_vision_confirm !== false;
    parts.push(`
      <div class="cfg-row">
        <label>Auto-escalate if no response after</label>
        <select class="cfg-field" data-cfg-key="intrusion_response_timeout">${this._optSelect(waits, String(this._intrTimeout || 120))}</select>
      </div>
      <div class="cfg-row">
        <label>Confirm Frigate person with Nova vision before alarming</label>
        <button class="toggle-btn ${vision ? "on" : "off"}" data-cfg-key="intrusion_vision_confirm" data-cfg-val="${vision ? "false" : "true"}">${vision ? "ON" : "OFF"}</button>
      </div>
      <div class="mode-grid">
        <button class="mode-chip new-intr-ack">✓ I'M LOOKING (HOLD)</button>
        <button class="mode-chip danger new-intr-dismiss">✕ CALL OFF (FALSE ALARM)</button>
      </div>`);
    this._setHtml(body, parts.join(""));

    body.querySelectorAll(".toggle-btn[data-cfg-key], select.cfg-field[data-cfg-key]").forEach(el => {
      const save = value => this._saveSetting(el.getAttribute("data-cfg-key"), value);
      if (el.tagName === "BUTTON") el.addEventListener("click", () => save(el.getAttribute("data-cfg-val") === "true"));
      else el.addEventListener("change", () => save(el.value));
    });
    // Call off and acknowledge: the button stays disabled until the fresh
    // status redraws it, and comes back only when the action failed.
    const action = (selector, name, askFirst) => {
      const btn = body.querySelector(selector);
      if (!btn) return;
      btn.addEventListener("click", async () => {
        if (!this._hass) return;
        if (askFirst && !askFirst()) return;
        btn.disabled = true;
        try {
          await this._intrSend(name, { reason: "panel" });
          await this._fetchIntrusion();
        } catch (err) {
          this._fail(err, `intrusion ${name}`);
          btn.disabled = false;
        }
      });
    };
    action(".new-intr-dismiss", "dismiss", () => window.confirm(this._tx("Call off this alert as a false alarm?")));
    action(".new-intr-ack", "acknowledge");
  }

  async _fetchIntrusionLog() {
    const root = this.shadowRoot;
    const body = root?.getElementById("newIlogBody"), learned = root?.getElementById("newIlogLearn");
    if (!this._hass || !body) return;
    try {
      const log = await this._intrSend("log", { limit: 40 });
      this._ilog = log;
      const tally = log?.learning || {};
      if (learned) this._setText(learned, this._t("{labelled}/{events} labelled", { labelled: tally.labeled || 0, events: tally.events || 0 }));
      this._setHtml(body, this._renderIntrusionLogHtml(log));
      this._wireIntrusionLabels();
    } catch (_) {
      this._setHtml(body, `<div class="stub-body">Could not load the log.</div>`);
    }
  }

  _renderIntrusionLogHtml(res) {
    const events = (res && res.events) || [];
    if (!events.length) return `<div class="stub-body">No intrusion events recorded yet.</div>`;
    const quiet = ((res.learning || {}).damped_patterns || []).length;
    const head = quiet
      ? `<div class="stub-body">${this._tHtml(quiet === 1 ? "Nova has learned {count} benign pattern — low-confidence alerts for these stay quiet." : "Nova has learned {count} benign patterns — low-confidence alerts for these stay quiet.", { count: quiet })}</div>`
      : "";
    const tone = { confirmed: "diag-down", unresolved: "diag-warn", investigating: "diag-off" };
    const row = e => {
      const label = e.label || "", on = v => (label === v ? " mode-chip-on" : "");
      return `<div class="new-ilog-item" data-ev="${this._esc(e.id)}">
        <div class="cfg-row">
          <span class="${tone[e.kind] || "diag-off"}">${this._esc((e.kind || "").toUpperCase())}</span>
          <span class="toggle-desc">${this._esc(new Date((e.ts || 0) * 1000).toLocaleString())}</span>
        </div>
        <div class="toggle-desc">${this._esc(e.breach || e.camera || "activity")}${e.reason ? " — " + this._esc(e.reason) : ""}</div>
        ${e.image_b64 ? `<img class="intr-img" src="data:image/jpeg;base64,${e.image_b64}" alt="snapshot">` : ""}
        <div class="mode-grid">
          <button class="mode-chip new-ilog-btn${on("real")}" data-label="real">REAL</button>
          <button class="mode-chip new-ilog-btn${on("false")}" data-label="false">FALSE ALARM</button>
          ${label ? `<button class="mode-chip new-ilog-btn" data-label="">CLEAR</button>` : ""}
        </div>
      </div>`;
    };
    return head + events.map(row).join("");
  }

  _wireIntrusionLabels() {
    const body = this.shadowRoot?.getElementById("newIlogBody");
    if (!body) return;
    body.querySelectorAll(".new-ilog-btn").forEach(btn => btn.addEventListener("click", async () => {
      const eventId = btn.closest(".new-ilog-item")?.getAttribute("data-ev");
      if (!eventId || !this._hass) return;
      try {
        await this._intrSend("label", { event_id: eventId, label: btn.getAttribute("data-label") });
        await this._fetchIntrusionLog();
      } catch (err) { this._fail(err, "intrusion label"); }
    }));
  }

  _wireIntrusion() {
    this._fetchIntrusion();
    this._fetchIntrusionLog();
    const refresh = this.shadowRoot.getElementById("newIlogRefresh");
    if (!refresh) return;
    refresh.addEventListener("click", async () => {
      const label = refresh.textContent;
      refresh.disabled = true;
      this._setText(refresh, "⟳ LOADING…");
      try { await this._fetchIntrusionLog(); } finally {
        refresh.disabled = false;
        this._setText(refresh, label);
      }
    });
  }
