  // ─── Voice ID (Settings → Voice & Speakers, 8.35.0, 8.36.0) ───────────
  // The app's address and token, the fallback, and the People list: record
  // or delete each person's voice in the Nova Voice ID app. Voice data lives
  // only in the app; Nova keeps none. Its own websocket commands
  // (nova/voice_id_*), except the address and fallback, which use the
  // generic data-cfg-key autosave. The list and the recording progress
  // redraw only #vidPeople and #vidRecord.

  _voiceIdCardBody() {
    const cfg = this._data()?.config || {};
    const st = cfg.voice_id_status || {};
    const fallback = cfg.voice_id_fallback_stt || "";
    const sttOpts = [["", "— none —"], ...(cfg.stt_entities || []).map(e => [e.entity_id, e.name])];
    if (fallback && !sttOpts.some(o => o[0] === fallback)) sttOpts.push([fallback, fallback]);
    const meta = [st.version ? `v${st.version}` : "", (st.models || []).join(", ")].filter(Boolean).join(" · ");
    const state = st.state === "connected" ? `<span class="vid-ok">Connected</span>`
      : st.state === "unreachable" ? `<span class="vid-bad">Unreachable</span>`
      : st.state === "checking" ? `<span>Checking…</span>`
      : st.state === "not_set" ? `<span>Not set</span>`
      : `<span>Not loaded</span>`;
    if (!this._vidSession) this._vidSession = cfg.voice_id_enrol || { state: "idle" };
    const sats = (cfg.satellites || []).map(s => [s.entity_id, s.area ? `${s.name} (${s.area})` : s.name]);
    if (!sats.some(o => o[0] === this._vidSatellite)) this._vidSatellite = sats.length ? sats[0][0] : "";
    return `
      <div class="stub-body">Set Nova Voice ID as the speech to text in your voice assistant (Settings → Voice assistants). If the app isn't running, Nova uses the fallback.</div>
      <div class="cfg-row">
        <label>Status</label>
        <div>${state}${meta ? ` <span class="toggle-desc">${this._esc(meta)}</span>` : ""}</div>
      </div>
      <div class="cfg-row">
        <label>App address</label>
        <input class="cfg-field" type="text" data-cfg-key="voice_id_uri" value="${this._esc(cfg.voice_id_uri || "")}">
      </div>
      <div class="toggle-desc">Shown in the Nova Voice ID app's log</div>
      <div class="cfg-row">
        <label>App token</label>
        <div class="vid-token-row">
          <input class="vid-token" id="vidToken" type="password" autocomplete="off" spellcheck="false" value="">
          <button class="mode-chip" id="vidTokenEye" title="Show or hide the token">👁</button>
        </div>
      </div>
      <div class="toggle-desc">${cfg.voice_id_token_set ? "Saved. Filled in by the app when Nova finds it, unless you typed your own." : "Copy it from the app's Configuration tab."}</div>
      <div class="cfg-row">
        <label>Fallback speech to text</label>
        <select class="cfg-field" data-cfg-key="voice_id_fallback_stt">${this._optSelect(sttOpts, fallback)}</select>
      </div>
      <div class="mode-bind-head">People</div>
      <div class="stub-body">Record each person on every satellite: rooms sound different. Voice data lives only in the Nova Voice ID app, never in Nova.</div>
      <div class="cfg-row">
        <label>Record on</label>
        ${sats.length
          ? `<select id="vidSatellite">${this._optSelect(sats, this._vidSatellite)}</select>`
          : `<span class="toggle-desc">No satellites found.</span>`}
      </div>
      <div id="vidRecord">${this._vidRecordHtml()}</div>
      <div id="vidPeople">${this._vidPeopleHtml()}</div>
      <div class="stub-body" id="vidMsg"></div>
      <div class="cfg-row"><button class="mode-chip vid-danger" id="vidDeleteAll">Delete all voice data</button></div>`;
  }

  _vidRecordHtml() {
    const s = this._vidSession || { state: "idle" };
    const v = { name: s.name || "", done: s.committed || 0, needed: s.needed || 8 };
    if (s.state === "active") {
      return `<div class="vid-progress"><b>${this._tHtml("Recording {name}: {done} of {needed}", v)}</b>
        <button class="mode-chip" id="vidCancel">Cancel</button></div>
        <div class="toggle-desc">Say the wake word, then the sentence the satellite asks for.</div>`;
    }
    if (s.state === "done") return `<div class="vid-progress vid-ok">${this._tHtml("{name}: all {needed} samples saved.", v)}</div>`;
    if (s.state === "cancelled") return `<div class="vid-progress">${this._tHtml("Recording cancelled. {done} of {needed} samples were saved.", v)}</div>`;
    if (s.state === "expired") return `<div class="vid-progress">${this._tHtml("Recording stopped after 5 minutes. {done} of {needed} samples were saved.", v)}</div>`;
    if (s.state === "error") return `<div class="vid-progress vid-bad">${this._esc(this._tx(s.message || "Recording stopped after an error."))}</div>`;
    return "";
  }

  _vidPeopleHtml() {
    const cfg = this._data()?.config || {};
    const res = this._vidPeople;
    if (!cfg.voice_id_uri) return `<div class="toggle-desc">Set the app address to see who has a voice saved.</div>`;
    if (!res) return `<div class="toggle-desc">Loading people…</div>`;
    if (!res.ok) return `<div class="toggle-desc vid-bad">${this._esc(this._tx(res.error || "Couldn't reach the app."))}</div>`;
    const hidden = new Set(Array.isArray(cfg.voice_id_hidden_people) ? cfg.voice_id_hidden_people : []);
    const recording = (this._vidSession || {}).state === "active";
    const row = (p, isHidden) => {
      const person = this._esc(p.person);
      const count = p.samples === 1 ? this._tHtml("1 sample") : this._tHtml("{count} samples", { count: p.samples || 0 });
      const when = p.updated ? ` · ${this._tHtml("updated {date}", { date: new Date(p.updated * 1000).toLocaleDateString() })}` : "";
      return `
      <div class="cfg-row vid-person">
        <label>${this._esc(p.name)}</label>
        <span class="toggle-desc">${count}${when}</span>
        <div class="vid-actions">
          ${isHidden ? "" : `<button class="mode-chip vid-rec" data-person="${person}" ${recording ? "disabled" : ""}>Record voice</button>`}
          <button class="mode-chip vid-del" data-person="${person}" data-name="${this._esc(p.name)}" ${p.samples ? "" : "disabled"}>Delete voice</button>
          <button class="mode-chip vid-hide" data-person="${person}">${isHidden ? "Show" : "Hide"}</button>
        </div>
      </div>`;
    };
    const people = res.people || [];
    const shown = people.filter(p => !hidden.has(p.person));
    const kept = people.filter(p => hidden.has(p.person));
    if (!people.length) return `<div class="toggle-desc">No people in Home Assistant yet.</div>`;
    return `${shown.map(p => row(p, false)).join("")}
      ${kept.length ? `<div class="cfg-row"><button class="mode-chip" id="vidShowHidden">${this._vidShowHidden
        ? this._tHtml("Hide the hidden people")
        : this._tHtml("Show hidden people ({count})", { count: kept.length })}</button></div>` : ""}
      ${this._vidShowHidden ? kept.map(p => row(p, true)).join("") : ""}`;
  }

  _vidRedraw() {
    const root = this.shadowRoot;
    const people = root?.getElementById("vidPeople");
    if (people) this._setHtml(people, this._vidPeopleHtml());
    const rec = root?.getElementById("vidRecord");
    if (rec) this._setHtml(rec, this._vidRecordHtml());
  }

  _vidSay(text) {
    const out = this.shadowRoot?.getElementById("vidMsg");
    if (out) this._setText(out, text || "");
  }

  async _fetchVoiceIdPeople() {
    if (!this._hass) return;
    try {
      this._vidPeople = await this._hass.callWS({ type: "nova/voice_id_profiles" });
    } catch (_) {
      this._vidPeople = { ok: false, error: "Couldn't load the People list. Restart Home Assistant after updating, then reopen Settings." };
    }
    this._vidRedraw();
  }

  async _loadVoiceIdToken() {
    const cfg = this._data()?.config || {};
    const field = this.shadowRoot?.getElementById("vidToken");
    if (!field || !cfg.voice_id_token_set || !this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/voice_id_token_get" });
      if (res && res.ok && field.isConnected) field.value = res.token || "";
    } catch (_) { /* the field just stays empty */ }
  }

  _vidPollSoon() {
    if (this._vidPollTimer) return;
    this._vidPollTimer = setTimeout(async () => {
      this._vidPollTimer = null;
      if (!this.isConnected || !this._hass) return;
      const before = (this._vidSession || {}).state;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record_status" });
        if (res && res.session) this._vidSession = res.session;
      } catch (_) { return; }
      this._vidRedraw();
      if (this._vidSession.state === "active") this._vidPollSoon();
      else if (before === "active") this._fetchVoiceIdPeople();
    }, 2000);
  }

  _wireVoiceId() {
    const root = this.shadowRoot;
    const card = root?.getElementById("settings-card-voice_id");
    if (!card) return;
    if (!this._vidFetchedOnce) {
      this._vidFetchedOnce = true;
      this._fetchVoiceIdPeople();
    }
    this._loadVoiceIdToken();
    if ((this._vidSession || {}).state === "active") this._vidPollSoon();
    card.addEventListener("click", ev => this._vidClick(ev));
    card.addEventListener("change", async ev => {
      if (ev.target?.id === "vidSatellite") this._vidSatellite = ev.target.value;
      if (ev.target?.id === "vidToken") {
        try {
          const res = await this._hass.callWS({ type: "nova/voice_id_token_set", token: ev.target.value.trim() });
          this._vidSay(res.ok ? (res.set ? this._t("Token saved.") : this._t("Token cleared.")) : res.error);
          if (res.ok) await this._fetchVoiceIdPeople();
        } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      }
    });
  }

  async _vidClick(ev) {
    const btn = ev.target?.closest?.("button");
    if (!btn || !this._hass) return;
    const person = btn.getAttribute("data-person");
    if (btn.id === "vidTokenEye") {
      const field = this.shadowRoot?.getElementById("vidToken");
      if (field) field.type = field.type === "password" ? "text" : "password";
    } else if (btn.classList.contains("vid-rec")) {
      if (!this._vidSatellite) { this._vidSay(this._t("Pick a satellite first.")); return; }
      this._vidSay("");
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record", person, satellite: this._vidSatellite });
        if (!res.ok) { this._vidSay(res.error); return; }
        this._vidSession = res.session;
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); return; }
      this._vidRedraw();
      this._vidPollSoon();
    } else if (btn.id === "vidCancel") {
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_record_cancel" });
        this._vidSession = res.session;
      } catch (_) { /* the poll picks it up */ }
      this._vidRedraw();
      this._fetchVoiceIdPeople();
    } else if (btn.classList.contains("vid-del")) {
      const name = btn.getAttribute("data-name") || person;
      if (!window.confirm(this._t("Delete {name}'s voice from the Nova Voice ID app? Their recordings are deleted too.", { name }))) return;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_delete", person });
        this._vidSay(!res.ok ? res.error
          : (res.deleted || []).length ? this._t("Deleted {name}'s voice.", { name })
          : this._t("The app had no voice saved for {name}.", { name }));
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      await this._fetchVoiceIdPeople();
    } else if (btn.id === "vidDeleteAll") {
      if (!window.confirm(this._t("Delete every voice saved in the Nova Voice ID app, for everyone?"))) return;
      if (!window.confirm(this._t("This can't be undone. Delete all voice data now?"))) return;
      try {
        const res = await this._hass.callWS({ type: "nova/voice_id_delete", all: true });
        this._vidSay(!res.ok ? res.error : this._t("Deleted all voice data ({count} people).", { count: (res.deleted || []).length }));
      } catch (_) { this._vidSay(this._t("Couldn't reach Nova. Reload the page and try again.")); }
      await this._fetchVoiceIdPeople();
    } else if (btn.classList.contains("vid-hide")) {
      const cfg = this._data()?.config || {};
      const hidden = new Set(Array.isArray(cfg.voice_id_hidden_people) ? cfg.voice_id_hidden_people : []);
      if (hidden.has(person)) hidden.delete(person); else hidden.add(person);
      await this._saveSetting("voice_id_hidden_people", JSON.stringify([...hidden].sort()));
    } else if (btn.id === "vidShowHidden") {
      this._vidShowHidden = !this._vidShowHidden;
      this._vidRedraw();
    }
  }

