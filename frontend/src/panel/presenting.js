  // ─── Presenting ───────────────────────────────────────────────────────
  // While the Command Center is open and the house is awake, the cards also
  // come round by themselves, one kind after another (the "Cards on the
  // Command Center" setting, on by default), so the hero is not only ever
  // one greeting. The greeting is a step in the same rotation: every few
  // cards the hero word changes to a fresh one (see _pickGreeting). Energy is
  // the first kind; its figures are read with the Energy tab's own commands.
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
    const every = this._presentCycleMs || 9000;
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
      // `refresh` (optional) redraws a card on show every `every` ms.
      datetime: { html: (d) => this._presentClockHtml(d.now), ambient: async () => ({ now: Date.now() }),
        refresh: () => this._presentClockHtml(Date.now()), every: 1000 },
      weather: { html: (d) => this._presentWeatherHtml(d), ambient: async () => this._presentWeatherData() },
      climate: { html: (d) => this._presentClimateHtml(d), ambient: async () => this._presentClimateData() },
      home: { html: (d) => this._presentHomeHtml(d), ambient: async () => this._presentHomeData() },
      calendar: { html: (d) => this._presentCalendarHtml(d), ambient: async () => this._presentCalendarData() },
      reminders: { html: (d) => this._presentRemindersHtml(d), ambient: () => this._presentRemindersData() },
      energy: { html: (d) => this._presentEnergyHtml(d), ambient: () => this._presentEnergyData() },
      security: { html: (d) => this._presentSecurityHtml(d), ambient: async () => this._presentSecurityData() },
    };
  }

  // The time and date, in the panel's language. Ticks while it is on show.
  _presentClockHtml(ms) {
    const lang = this._resolveUiLang(), t = new Date(ms);
    let time, date;
    try {
      time = t.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
      date = t.toLocaleDateString(lang, { weekday: "long", day: "numeric", month: "long" });
    } catch (_) {
      time = t.toTimeString().slice(0, 5);
      date = t.toDateString();
    }
    return `<div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(time)}</span></div><div class="pc-cap">${this._esc(date)}</div>`;
  }

  // Who is home, from Home Assistant's person entities. Null with nobody set up.
  _presentHomeData() {
    const people = [];
    for (const [id, st] of Object.entries(this._hass?.states || {})) {
      if (!id.startsWith("person.") || !st || st.state === "unavailable" || st.state === "unknown") continue;
      people.push({ name: String(st.attributes?.friendly_name || id.slice(7)), home: st.state === "home" });
    }
    if (!people.length) return null;
    people.sort((a, b) => (b.home - a.home) || a.name.localeCompare(b.name));
    return { people: people.slice(0, 6), total: people.length, home: people.filter(p => p.home).length };
  }

  _presentHomeHtml(d) {
    if (!d.total) return "";
    const n = (v) => this._esc(this._presentNum(v, 0));
    return `<div class="pc-cap">HOME</div><div class="pc-big"><span class="pc-fig" data-unit="">${n(d.home)}/${n(d.total)}</span></div>`
      + `<div class="pc-rows">${(d.people || []).map(p => `<div class="pc-row"><span>${this._esc(p.name)}</span><b>${this._esc(p.home ? this._tx("HOME") : this._tx("AWAY"))}</b></div>`).join("")}</div>`;
  }

  // "today", "tomorrow" or a weekday and date, in the panel's language, from
  // the browser's own date names (so no words of ours to translate).
  _presentDayWord(date) {
    const lang = this._resolveUiLang();
    const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((midnight(date) - midnight(new Date())) / 86400000);
    let word;
    try {
      word = Math.abs(days) <= 1 ? new Intl.RelativeTimeFormat(lang, { numeric: "auto" }).format(days, "day")
        : date.toLocaleDateString(lang, { weekday: "long", day: "numeric", month: "long" });
    } catch (_) {
      word = date.toDateString();
    }
    return word.charAt(0).toLocaleUpperCase(lang) + word.slice(1);
  }

  _presentTime(date) {
    try {
      return date.toLocaleTimeString(this._resolveUiLang(), { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return date.toTimeString().slice(0, 5);
    }
  }

  // The next event across the Home Assistant calendars: each calendar entity
  // shows its current or next event in its attributes. Null when none starts
  // within a week.
  _presentCalendarData() {
    const week = Date.now() + 7 * 86400000;
    let best = null;
    for (const [id, st] of Object.entries(this._hass?.states || {})) {
      if (!id.startsWith("calendar.") || !st || st.state === "unavailable") continue;
      const a = st.attributes || {};
      if (!a.message || !a.start_time) continue;
      const start = new Date(String(a.start_time).replace(" ", "T")).getTime();
      const end = a.end_time ? new Date(String(a.end_time).replace(" ", "T")).getTime() : start;
      if (!isFinite(start) || start > week || (end <= Date.now() && start < Date.now())) continue;
      if (!best || start < best.start) best = { title: String(a.message), start, all_day: !!a.all_day };
    }
    return best;
  }

  _presentCalendarHtml(d) {
    if (!d || !d.title || !isFinite(d.start)) return "";
    const when = new Date(d.start), started = d.start <= Date.now();
    const big = started ? this._tx("Now") : d.all_day ? this._presentDayWord(when) : this._presentTime(when);
    const sub = started || d.all_day ? "" : `${this._presentDayWord(when)} · `;
    return `<div class="pc-cap">Calendar</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(big)}</span></div>`
      + `<div class="pc-now">${this._esc(sub + d.title)}</div>`;
  }

  // The reminders still to come, from Nova's own store.
  async _presentRemindersData() {
    const res = await this._hass.callWS({ type: "nova/get_reminders" });
    const items = Array.isArray(res?.items) ? res.items.filter(r => r && r.label && r.at) : [];
    return items.length ? { items } : null;
  }

  _presentRemindersHtml(d) {
    const items = (d && Array.isArray(d.items) ? d.items : []).slice(0, 4);
    if (!items.length) return "";
    const first = items[0], at = new Date(first.at);
    const big = first.due || !isFinite(at.getTime()) ? this._tx("Now") : this._presentTime(at);
    return `<div class="pc-cap">Reminder</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(big)}</span></div>`
      + `<div class="pc-rows">${items.map(r => {
        const t = new Date(r.at);
        const when = r.due || !isFinite(t.getTime()) ? this._tx("Now") : this._presentTime(t);
        return `<div class="pc-row"><span>${this._esc(r.label)}</span><b>${this._esc(when)}</b></div>`;
      }).join("")}</div>`;
  }

  // Indoor temperature and humidity: the room cards' own readings, averaged,
  // with up to four rooms listed. Null when no room reports either.
  _presentClimateData() {
    const rooms = [];
    for (const a of this._liveData?.areas || []) {
      const t = parseFloat(a.temp), h = parseFloat(a.humidity);
      if (!isFinite(t) && !isFinite(h)) continue;   // "—" when a room has neither
      rooms.push({ name: String(a.name || ""), temp: isFinite(t) ? t : null, humidity: isFinite(h) ? h : null,
        unit: isFinite(t) ? String(a.temp).replace(/^[-\d.]+/, "") : "" });
    }
    if (!rooms.length) return null;
    const avg = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);
    const temps = rooms.filter(r => r.temp !== null), hums = rooms.filter(r => r.humidity !== null);
    return { temp: avg(temps.map(r => r.temp)), unit: temps.length ? temps[0].unit : "",
      humidity: avg(hums.map(r => r.humidity)), rooms: rooms.slice(0, 4) };
  }

  _presentClimateHtml(d) {
    if (!d || (d.temp == null && d.humidity == null)) return "";
    const n = (v, dp) => this._esc(this._presentNum(v, dp));
    const tenths = d.temp != null ? Math.round(d.temp * 10) / 10 : 0;   // 21 not 21.0, but 20.8 stays
    const big = d.temp != null ? `${n(tenths, Number.isInteger(tenths) ? 0 : 1)}${this._esc(d.unit || "°")}` : `${n(d.humidity, 0)}%`;
    const sub = d.temp != null && d.humidity != null ? `<div class="pc-now">${n(d.humidity, 0)}%</div>` : "";
    const rows = (d.rooms || []).map(r => {
      const parts = [r.temp != null ? `${n(r.temp, 0)}${this._esc(r.unit || "°")}` : "", r.humidity != null ? `${n(r.humidity, 0)}%` : ""].filter(Boolean);
      return `<div class="pc-row"><span>${this._esc(r.name)}</span><b>${parts.join(" · ")}</b></div>`;
    }).join("");
    return `<div class="pc-cap">INDOOR</div><div class="pc-big"><span class="pc-fig" data-unit="">${big}</span></div>${sub}`
      + (rows ? `<div class="pc-rows">${rows}</div>` : "");
  }

  // Outside: the first weather entity, else an outdoor temperature sensor
  // (as Nova's own weather answer does).
  _presentWeatherData() {
    const states = this._hass?.states || {};
    const live = (st) => st && st.state !== "unavailable" && st.state !== "unknown";
    const wid = Object.keys(states).sort().find(k => k.startsWith("weather.") && live(states[k]));
    if (wid) {
      const a = states[wid].attributes || {};
      const t = parseFloat(a.temperature);
      if (isFinite(t)) return { id: wid, temp: t };
    }
    const sid = Object.keys(states).sort().find(k => {
      const st = states[k], low = k.toLowerCase();
      return k.startsWith("sensor.") && live(st) && st.attributes?.device_class === "temperature"
        && (low.includes("outdoor") || low.includes("outside")) && isFinite(parseFloat(st.state));
    });
    return sid ? { id: sid, temp: parseFloat(states[sid].state) } : null;
  }

  _presentWeatherHtml(d) {
    const st = d && this._hass?.states?.[d.id];
    if (!st || !isFinite(d.temp)) return "";
    const h = this._hass, a = st.attributes || {};
    const plain = (s) => { const t = String(s || "").replace(/[_-]+/g, " ").trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
    const unit = a.temperature_unit || (st.attributes?.unit_of_measurement) || "°";
    // Home Assistant names its own states and attributes in the user's language.
    const cond = id => id.startsWith("weather.")
      ? (typeof h.formatEntityState === "function" ? h.formatEntityState(st) : plain(st.state)) : "";
    const attrName = k => (typeof h.formatEntityAttributeName === "function" ? h.formatEntityAttributeName(st, k) : plain(k));
    const attrVal = (k, fallbackUnit) => (typeof h.formatEntityAttributeValue === "function"
      ? h.formatEntityAttributeValue(st, k) : `${this._presentNum(a[k], 0)}${fallbackUnit ? (fallbackUnit === "%" ? "" : " ") + fallbackUnit : ""}`);
    const rows = [];
    if (isFinite(parseFloat(a.humidity))) rows.push([attrName("humidity"), attrVal("humidity", "%")]);
    if (isFinite(parseFloat(a.wind_speed))) rows.push([attrName("wind_speed"), attrVal("wind_speed", a.wind_speed_unit || "")]);
    const condition = cond(d.id);
    return `<div class="pc-cap">Weather</div><div class="pc-big"><span class="pc-fig" data-unit="">${this._esc(this._presentNum(d.temp, 0))}${this._esc(unit)}</span></div>`
      + (condition ? `<div class="pc-now">${this._esc(condition)}</div>` : "")
      + (rows.length ? `<div class="pc-rows">${rows.map(([l, v]) => `<div class="pc-row"><span>${this._esc(l)}</span><b>${this._esc(v)}</b></div>`).join("")}</div>` : "");
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

  // The hero word's greetings. The hour decides which time of day greeting can
  // come up (none late at night), and it is never the same one twice in a row.
  _pickGreeting(hour = new Date().getHours()) {
    // A map with a label each, so the panel's string list picks every greeting up.
    const g = { hello: "Hello.", intro: "Hi, I'm Nova.", here: "Nova here.", service: "At your service.",
      calm: "All good here.", morning: "Good morning.", afternoon: "Good afternoon.", evening: "Good evening." };
    const pool = [g.hello, g.intro, g.here, g.service, g.calm];
    if (hour >= 5 && hour < 12) pool.push(g.morning);
    else if (hour >= 12 && hour < 18) pool.push(g.afternoon);
    else if (hour >= 18 && hour < 22) pool.push(g.evening);
    const options = pool.filter((g) => g !== this._heroGreeting);
    return options[Math.floor(Math.random() * options.length)];
  }

  // One round of the rotation: every kind of card, with a fresh greeting in
  // front of every third one, so the hero's own word is part of the rotation
  // and is not just what the head says between cards.
  _presentSteps() {
    const every = this._presentGreetEvery === undefined ? 3 : this._presentGreetEvery;
    const steps = [];
    Object.entries(this._presentKinds()).forEach(([kind, def], i) => {
      if (every > 0 && i % every === 0) steps.push(["greeting", null]);
      steps.push([kind, def]);
    });
    return steps;
  }

  // The next step comes one cycle after this one started (the timer starts
  // over here). A card stays 8 seconds and the cycle is 9, so the gap
  // between two steps is only a second.
  _presentRestartCycle() {
    if (!this._presentCycleTimer) return;
    clearInterval(this._presentCycleTimer);
    this._presentCycleTimer = setInterval(() => this._presentAmbient(), this._presentCycleMs || 9000);
  }

  async _presentAmbient() {
    const cfg = this._liveData?.config || {};
    if (cfg.hero_card_cycle === false || this._currentTab !== "dashboard") return;
    if (this._presenting || this._coreState() !== "idle" || document.hidden) return;
    const token = this._presentToken;
    const steps = this._presentSteps();
    // Each round starts at the next step; a card with nothing to show is skipped.
    for (let i = 0; i < steps.length; i++) {
      const at = ((this._presentTurn || 0) + i) % steps.length;
      const [kind, def] = steps[at];
      if (kind === "greeting") {
        this._presentTurn = at + 1;
        this._heroGreeting = this._pickGreeting();
        this._typeHeroWord(this._heroGreeting);
        this._presentRestartCycle();
        return;
      }
      let data = null;
      try { data = await def.ambient(); } catch (_) { /* no card of this kind this time */ }
      // Gone, or busy with a real card, while the figures were being read.
      if (this._presentToken !== token || this._presenting || this._coreState() !== "idle") return;
      if (!data || !def.html(data)) continue;
      this._presentTurn = at + 1;
      const now = Date.now() / 1000;
      this._presentRestartCycle();
      this._onPresentCard({ v: 1, kind, id: `ambient-${kind}-${Date.now()}`, created: now, expires: now + 8, data });
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
    this._presentKind = card.kind;
    this._setHtml(el, html);
    root.getElementById("heroStage")?.classList.add("presenting");
    this._presenting = true;
    if (this._coreState() !== "asleep") this._targetCoreState("presenting");
    if (this._presentTimer) clearTimeout(this._presentTimer);
    this._presentTimer = setTimeout(() => this._presentEnd(), left);
    if (this._presentTick) { clearInterval(this._presentTick); this._presentTick = null; }
    if (def.refresh) {
      this._presentTick = setInterval(() => {
        const next = def.refresh(card.data);
        if (next) this._setHtml(el, next);
      }, def.every || 1000);
    }
  }

  _presentEnd() {
    if (this._presentTimer) { clearTimeout(this._presentTimer); this._presentTimer = null; }
    if (this._presentTick) { clearInterval(this._presentTick); this._presentTick = null; }
    if (!this._presenting) return;
    this._presenting = false;
    this._orbitSettle(this._presentKind);
    this._presentKind = null;
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
