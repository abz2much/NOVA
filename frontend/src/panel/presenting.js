  // ─── Presenting ───────────────────────────────────────────────────────
  // While the Command Center is open and the house is awake, the cards also
  // come round by themselves, one kind after another (the "Cards on the
  // Command Center" setting, on by default), so the hero is not only ever
  // "Hello". Energy is the first kind; its figures are read with the Energy
  // tab's own commands.
  // When Nova answers an energy question it publishes a small card of the
  // real figures (nova/subscribe_presentation). The head turns towards the
  // card and the hero copy gives way to it; after the card expires, or when
  // it is tapped, the hero goes back to whatever _coreState() says. Asleep
  // wins: the head stays asleep, though the card can still be read.
  _presentStart() {
    const conn = this._hass?.connection;
    if (!conn || typeof conn.subscribeMessage !== "function") return;
    const token = this._presentToken = this._presentToken || 0;
    if (this._presentLive === token) return;   // already started for this round
    this._presentLive = token;
    Promise.resolve(conn.subscribeMessage((card) => this._onPresentCard(card), { type: "nova/subscribe_presentation" }))
      .then((unsub) => {
        if (this._presentToken === token) this._presentUnsub = unsub;
        else if (typeof unsub === "function") unsub();   // stopped, or drawn again, while connecting
      })
      .catch(() => { if (this._presentLive === token) this._presentLive = null; });   // the card is optional
    this._presentCycleStart();
  }

  _presentCycleStart() {
    if (this._presentCycleTimer) return;
    const every = this._presentCycleMs || 90000;
    this._presentCycleTimer = setInterval(() => this._presentAmbient(), every);
    // The first card comes soon after the page opens, not a full round later.
    this._presentFirstTimer = setTimeout(() => this._presentAmbient(), this._presentFirstMs || 15000);
  }

  // Every kind of card Nova can show is listed here: how to draw it (html)
  // and how to read the figures for the rotation (ambient, which gives the
  // card's data or null when there is nothing to show). New kinds join the
  // rotation by being added here.
  _presentKinds() {
    return {
      energy: { html: (d) => this._presentEnergyHtml(d), ambient: () => this._presentEnergyData() },
      security: { html: (d) => this._presentSecurityHtml(d), ambient: async () => this._presentSecurityData() },
    };
  }

  async _presentEnergyData() {
    const [status, today] = await Promise.all([
      this._hass.callWS({ type: "nova/energy_flow", action: "status" }),
      this._hass.callWS({ type: "nova/energy_flow", action: "today" }),
    ]);
    return {
      solar_today_kwh: today?.solar_kwh ?? null, battery_pct: status?.battery?.pct ?? null,
      export_kwh: today?.grid_export_kwh ?? null, home_kwh: today?.home_kwh ?? null,
      solar_w: status?.solar?.w ?? null, curve: [],
    };
  }

  // The security card is a calm summary from Home Assistant's live states,
  // counts only (no names): locks, doors, the alarm and lockdown. Null when
  // there is nothing to summarise, so the rotation skips it.
  _presentSecurityData() {
    const states = this._hass?.states || {};
    const cfg = this._liveData?.config || {};
    let locks = 0, locked = 0, doors = 0, open = 0;
    const alarms = [];
    for (const [id, st] of Object.entries(states)) {
      const s = st && st.state;
      if (id.startsWith("lock.")) {
        if (s === "locked") { locks++; locked++; }
        else if (["unlocked", "unlocking", "locking", "jammed", "open", "opening"].includes(s)) locks++;
      } else if (id.startsWith("binary_sensor.")) {
        const cls = st?.attributes?.device_class;
        if ((cls === "door" || cls === "garage_door") && (s === "on" || s === "off")) { doors++; if (s === "on") open++; }
      } else if (id.startsWith("alarm_control_panel.")) {
        alarms.push(id);
      }
    }
    // The alarm Nova watches: the one chosen in Settings, else the only one.
    const alarmId = cfg.security_alarm_entity && states[cfg.security_alarm_entity] ? cfg.security_alarm_entity
      : (alarms.length === 1 ? alarms[0] : null);
    const alarm = alarmId ? String(states[alarmId].state || "") : "";
    const data = {
      locks_total: locks || null, locks_locked: locks ? locked : null,
      doors_total: doors || null, doors_open: doors ? open : null,
      alarm: ["disarmed", "arming", "pending", "triggered"].includes(alarm) || alarm.startsWith("armed_") ? alarm : null,
      lockdown: !!this._liveData?.lockdown?.active,
    };
    return data.locks_total || data.doors_total || data.alarm || data.lockdown ? data : null;
  }

  _presentSecurityHtml(d) {
    const n = (v) => this._esc(this._presentNum(v, 0));
    const big = d.locks_total > 0;
    const alarmWord = !d.alarm ? null
      : d.alarm.startsWith("armed_") ? this._tx("ARMED")
      : d.alarm === "disarmed" ? this._tx("Disarmed")
      : d.alarm === "triggered" ? this._tx("Triggered") : this._tx("Arming");
    const rows = [
      alarmWord ? ["Security alarm", this._esc(alarmWord)] : null,
      d.doors_total > 0 ? ["Doors", d.doors_open > 0 ? `${n(d.doors_open)} ${this._esc(this._tx("Open"))}` : this._esc(this._tx("Closed"))] : null,
      d.lockdown ? ["Lockdown", this._esc(this._tx("ON"))] : null,
    ].filter(Boolean);
    if (!big && !rows.length) return "";
    return `${big ? `<div class="pc-cap">Locked</div><div class="pc-big"><span class="pc-fig" data-unit="">${n(d.locks_locked)}/${n(d.locks_total)}</span></div>` : ""}`
      + (rows.length ? `<div class="pc-rows">${rows.map(([label, value]) => `<div class="pc-row"><span>${label}</span><b>${value}</b></div>`).join("")}</div>` : "");
  }

  async _presentAmbient() {
    const cfg = this._liveData?.config || {};
    if (cfg.hero_card_cycle === false || this._currentTab !== "dashboard") return;
    if (this._presenting || this._coreState() !== "idle" || document.hidden) return;
    const token = this._presentToken;
    const kinds = Object.entries(this._presentKinds());
    // Each round starts at the next kind; one with nothing to show is skipped.
    for (let i = 0; i < kinds.length; i++) {
      const at = ((this._presentTurn || 0) + i) % kinds.length;
      const [kind, def] = kinds[at];
      let data = null;
      try { data = await def.ambient(); } catch (_) { /* no card of this kind this time */ }
      // Gone, or busy with a real card, while the figures were being read.
      if (this._presentToken !== token || this._presenting || this._coreState() !== "idle") return;
      if (!data || !def.html(data)) continue;
      this._presentTurn = at + 1;
      const now = Date.now() / 1000;
      this._onPresentCard({ v: 1, kind, id: `ambient-${kind}-${Date.now()}`, created: now, expires: now + 30, data });
      return;
    }
  }

  _presentStop() {
    this._presentToken = (this._presentToken || 0) + 1;
    if (this._presentCycleTimer) { clearInterval(this._presentCycleTimer); this._presentCycleTimer = null; }
    if (this._presentFirstTimer) { clearTimeout(this._presentFirstTimer); this._presentFirstTimer = null; }
    const unsub = this._presentUnsub;
    this._presentUnsub = null;
    if (typeof unsub === "function") { try { unsub(); } catch (_) { /* already closed */ } }
    if (this._presenting) this._presentEnd();
  }

  _onPresentCard(card) {
    const def = card && card.v === 1 && card.data ? this._presentKinds()[card.kind] : null;
    if (!def) return;
    if (card.id && card.id === this._presentId) return;   // the same card twice
    const ttl = Math.max(0, (Number(card.expires) - Number(card.created)) * 1000) || 30000;
    let left = Number(card.expires) * 1000 - Date.now();
    if (!(left > 0)) return;                 // already expired
    if (left > ttl) left = ttl;              // this clock runs behind the server's
    const html = def.html(card.data);
    if (!html) return;
    const root = this.shadowRoot, el = root.getElementById("presentCard");
    if (!el) return;
    this._presentId = card.id || null;
    this._setHtml(el, html);
    root.getElementById("heroStage")?.classList.add("presenting");
    this._presenting = true;
    if (this._coreState() !== "asleep") this._targetCoreState("presenting");
    if (this._presentTimer) clearTimeout(this._presentTimer);
    this._presentTimer = setTimeout(() => this._presentEnd(), left);
  }

  _presentEnd() {
    if (this._presentTimer) { clearTimeout(this._presentTimer); this._presentTimer = null; }
    if (!this._presenting) return;
    this._presenting = false;
    this.shadowRoot?.getElementById("heroStage")?.classList.remove("presenting");
    this._targetCoreState(this._coreState());
  }

  _presentNum(v, digits) {
    try {
      return Number(v).toLocaleString(this._resolveUiLang(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
    } catch (_) {
      return Number(v).toFixed(digits);
    }
  }

  // The card's markup, or "" when none of its figures can be shown. A
  // figure Nova could not read is null and its row is left out.
  _presentEnergyHtml(d) {
    const has = (v) => typeof v === "number" && isFinite(v);
    const n = (v, dp) => this._esc(this._presentNum(v, dp));
    const rows = [
      has(d.battery_pct) ? ["Battery", `${n(d.battery_pct, 0)}%`] : null,
      has(d.export_kwh) ? ["Sold to grid", `${n(d.export_kwh, 1)} kWh`] : null,
      has(d.home_kwh) ? ["Home used", `${n(d.home_kwh, 1)} kWh`] : null,
    ].filter(Boolean);
    const big = has(d.solar_today_kwh);
    if (!big && !rows.length && !has(d.solar_w)) return "";
    const curve = Array.isArray(d.curve) ? d.curve.filter(has) : [];
    let svg = "";
    if (curve.length >= 2) {
      const max = Math.max(...curve, 0.001);
      const pts = curve.map((v, i) => `${(i / (curve.length - 1) * 120).toFixed(1)},${(26 - v / max * 24).toFixed(1)}`).join(" ");
      svg = `<svg class="pc-curve" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--gold)" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
    }
    const now = has(d.solar_w) ? `<div class="pc-now"><span>Now</span> <span>· ${n(d.solar_w, 0)} W</span></div>` : "";
    return `${big ? `<div class="pc-cap">Solar made</div><div class="pc-big"><span class="pc-fig" data-unit="kWh">${n(d.solar_today_kwh, 1)}</span></div>` : ""}${now}${svg}`
      + (rows.length ? `<div class="pc-rows">${rows.map(([label, value]) => `<div class="pc-row"><span>${label}</span><b>${value}</b></div>`).join("")}</div>` : "");
  }
