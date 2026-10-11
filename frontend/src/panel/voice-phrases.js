  // ─── Voice Phrases (Settings → Voice & Speakers, 8.32.0) ───────────────
  // Things you say, linked to a script, automation, scene or shell command.
  // Its own websocket commands (nova/voice_phrases_*), never the generic
  // data-cfg-key autosave: the backend checks every entry. The list and the
  // editor redraw only #vpBody, so the rest of Settings keeps its state.

  static VP_KINDS = [
    ["script", "Script"], ["automation", "Automation"], ["scene", "Scene"],
    ["shell", "Shell command"], ["advanced", "Advanced"],
  ];
  static VP_SERVICE = { script: "script.turn_on", automation: "automation.trigger", scene: "scene.turn_on" };

  _voicePhrasesCardBody() {
    const vp = this._vp;
    const on = !vp || vp.enabled !== false;
    return `
      <div class="stub-body">Say something, and Nova runs a script, an automation, a scene or a shell command.</div>
      <div class="cfg-row">
        <label>Voice phrases</label>
        <button class="toggle-btn ${on ? "on" : "off"}" id="vpEnabled">${on ? "ON" : "OFF"}</button>
      </div>
      <div id="vpBody">${this._vpBodyHtml()}</div>`;
  }

  async _fetchVoicePhrases() {
    if (!this._hass) return;
    try {
      this._vp = await this._hass.callWS({ type: "nova/voice_phrases_list" });
    } catch (_) { this._vp = { error: true }; }
    this._vpRedraw();
  }

  _vpRedraw() {
    const root = this.shadowRoot;
    const host = root?.getElementById("vpBody");
    if (host) this._setHtml(host, this._vpBodyHtml());
    const sw = root?.getElementById("vpEnabled");
    if (sw && this._vp && !this._vp.error) {
      const on = this._vp.enabled !== false;
      sw.classList.toggle("on", on);
      sw.classList.toggle("off", !on);
      this._setText(sw, on ? "ON" : "OFF");
    }
  }

  _vpBodyHtml() {
    const vp = this._vp;
    if (!vp) return `<div class="stub-body">Loading voice phrases…</div>`;
    if (vp.error) return `<div class="stub-body">Couldn't load voice phrases. Restart Home Assistant after updating, then reopen Settings.</div>`;
    if (this._vpEdit) return this._vpEditorHtml();
    const entries = Array.isArray(vp.entries) ? vp.entries : [];
    const asks = new Set(vp.asks_first || []);
    const rows = entries.map(e => {
      const pid = this._esc(e.phrase_id);
      const first = (e.phrases || [])[0] || "";
      const on = e.enabled !== false;
      return `
      <div class="cfg-row vp-row">
        <label>${this._esc(e.name)}</label>
        <span class="toggle-desc">“${this._esc(first)}” → ${this._esc(this._vpTargetText(e))}</span>
        <button class="toggle-btn ${on ? "on" : "off"} vp-toggle" data-pid="${pid}">${on ? "ON" : "OFF"}</button>
        <button class="mode-chip vp-edit" data-pid="${pid}">Edit</button>
        <button class="mode-chip vp-del" data-pid="${pid}">Delete</button>
      </div>
      ${asks.has(e.phrase_id) ? `<div class="toggle-desc vp-note"><span>Asks first, because of Voice Confirmation.</span> <button class="vp-link" data-goto="voice_confirmation">Voice Confirmation settings</button></div>` : ""}`;
    }).join("");
    return `
      ${rows || `<div class="stub-body">No voice phrases yet.</div>`}
      <div class="cfg-row"><button class="mode-chip" id="vpAdd">+ Add a voice phrase</button></div>
      <div class="mode-bind-head">Try a sentence</div>
      <div class="cfg-row">
        <input id="vpTry" class="cfg-field" style="flex:1" type="text" maxlength="500" placeholder="e.g. Nova, the house is cold" autocomplete="off">
        <button class="mode-chip" id="vpTryBtn">Try</button>
      </div>
      <div class="stub-body" id="vpTryResult"></div>
      <div class="mode-bind-head">Good to know</div>
      <div class="toggle-desc">Your phrases come before Nova's own built in commands.</div>
      <div class="toggle-desc">Home Assistant's own sentence triggers with the same words win. So does anything Home Assistant answers itself when "Prefer handling commands locally" is on.</div>
      <div class="toggle-desc">Unlock, open and disarm still need a tap on your phone.</div>
      <div class="toggle-desc">A shell command runs without asking. It can be made to ask first: see Voice phrases in Nova's docs.</div>`;
  }

  _vpTargetText(e) {
    const action = e.action || {};
    const ids = this._vpIds(action);
    return ids.length ? ids.join(", ") : String(action.service || "");
  }

  _vpIds(action) {
    const t = ((action || {}).target || {}).entity_id;
    return Array.isArray(t) ? t : (t ? [t] : []);
  }

  // The editor's state from a saved entry: the panel shorthand when the
  // entry fits one, Advanced otherwise.
  _vpEditFrom(e) {
    const action = (e && e.action) || {};
    const service = String(action.service || "");
    const ids = this._vpIds(action);
    const data = action.data && Object.keys(action.data).length ? JSON.stringify(action.data, null, 2) : "";
    // A new entry starts as a Script.
    let kind = e ? "advanced" : "script", entity = "", shell = "";
    const simple = Object.entries(NovaPanel.VP_SERVICE).find(([, svc]) => svc === service);
    if (!e) { /* a new entry */ }
    else if (simple && ids.length === 1 && !data) { kind = simple[0]; entity = ids[0]; }
    else if (service.startsWith("shell_command.") && !ids.length && !data) { kind = "shell"; shell = service.slice(14); }
    return {
      phrase_id: e ? e.phrase_id : null,
      name: e ? e.name || "" : "",
      phrases: e && (e.phrases || []).length ? [...e.phrases] : [""],
      match: e && e.match === "exact" ? "exact" : "flexible",
      allow_ai: !!(e && e.allow_ai),
      enabled: !e || e.enabled !== false,
      reply: e ? e.reply || "" : "",
      kind, entity, shell,
      service: kind === "advanced" ? service : "",
      entities: kind === "advanced" ? ids.join(", ") : "",
      data,
      errors: "", warnings: [],
    };
  }

  _vpEntityOptions(domain, current) {
    const states = this._hass?.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith(domain + ".")).sort();
    if (current && !ids.includes(current)) ids.unshift(current);
    const opts = ids.map(id => [id, `${states[id]?.attributes?.friendly_name || id} (${id})`]);
    return `<option value="">— pick one —</option>` + this._optSelect(opts, current);
  }

  _vpShellOptions(current) {
    const names = Object.keys(this._hass?.services?.shell_command || {}).sort();
    if (current && !names.includes(current)) names.unshift(current);
    return `<option value="">— pick one —</option>` + this._optSelect(names.map(n => [n, n]), current);
  }

  _vpEditorHtml() {
    const ed = this._vpEdit;
    const kinds = NovaPanel.VP_KINDS;
    const phrases = ed.phrases.map((p, i) => `
      <div class="cfg-row">
        <input class="cfg-field vp-phrase" style="flex:1" type="text" maxlength="80" value="${this._esc(p)}" placeholder="e.g. the house is cold" autocomplete="off">
        ${ed.phrases.length > 1 ? `<button class="mode-chip vp-phrase-del" data-i="${i}" title="Remove this phrase">×</button>` : ""}
      </div>`).join("");
    let what = "";
    if (ed.kind === "shell") {
      what = `
      <div class="cfg-row"><label>Shell command</label>
        <select class="cfg-field" id="vpShell">${this._vpShellOptions(ed.shell)}</select></div>`;
    } else if (ed.kind === "advanced") {
      what = `
      <div class="cfg-row"><label>Service</label>
        <input class="cfg-field" style="flex:1" id="vpService" type="text" value="${this._esc(ed.service)}" placeholder="e.g. climate.set_temperature" autocomplete="off"></div>
      <div class="cfg-row"><label>Entity ids</label>
        <input class="cfg-field" style="flex:1" id="vpEntities" type="text" value="${this._esc(ed.entities)}" placeholder="e.g. climate.hall, climate.landing" autocomplete="off"></div>
      <div class="cfg-row"><label>Data (JSON)</label>
        <textarea class="cfg-field vp-json" id="vpData" rows="4" placeholder='{"temperature": 21}'>${this._esc(ed.data)}</textarea></div>`;
    } else {
      what = `
      <div class="cfg-row"><label>Run</label>
        <select class="cfg-field" id="vpEntity">${this._vpEntityOptions(ed.kind, ed.entity)}</select></div>`;
    }
    const shell = ed.kind === "shell";
    return `
      <div class="vp-editor">
        <div class="mode-bind-head">${ed.phrase_id ? "Edit voice phrase" : "New voice phrase"}</div>
        <div class="cfg-row"><label>Name</label>
          <input class="cfg-field" style="flex:1" id="vpName" type="text" maxlength="60" value="${this._esc(ed.name)}" placeholder="e.g. House is cold" autocomplete="off"></div>
        <div class="mode-bind-head">What you say</div>
        <div class="toggle-desc">Up to 10 ways of saying it.</div>
        ${phrases}
        ${ed.phrases.length < 10 ? `<div class="cfg-row"><button class="mode-chip" id="vpPhraseAdd">+ Add another way to say it</button></div>` : ""}
        <div class="cfg-row"><label>Match</label>
          <select class="cfg-field" id="vpMatch">${this._optSelect([["flexible", "Flexible: small differences are fine"], ["exact", "Exact: the same words only"]], ed.match)}</select></div>
        <div class="mode-bind-head">What to run</div>
        <div class="cfg-row"><label>Type</label>
          <select class="cfg-field" id="vpKind">${this._optSelect(kinds, ed.kind)}</select></div>
        ${what}
        ${shell ? "" : `<div class="cfg-row"><label>Let the AI pick this by meaning</label>
          <button class="toggle-btn ${ed.allow_ai ? "on" : "off"}" id="vpAllowAi">${ed.allow_ai ? "ON" : "OFF"}</button></div>
        <div class="toggle-desc">When the words don't match, the AI may still run this if what you said clearly means it.</div>`}
        <div class="cfg-row"><label>Reply</label>
          <input class="cfg-field" style="flex:1" id="vpReply" type="text" maxlength="200" value="${this._esc(ed.reply)}" placeholder="e.g. Warming the house up{addr}." autocomplete="off"></div>
        <div class="toggle-desc">${this._tHtml("Leave it empty for \"Done\". {addr} adds \", sir\" or however Nova addresses you. {honorific} adds just that word.")}</div>
        ${ed.errors ? `<div class="stub-body diag-warn" id="vpErrors">${this._esc(ed.errors)}</div>` : ""}
        <div class="cfg-row">
          <button class="mode-chip" id="vpSave">Save</button>
          <button class="mode-chip" id="vpCancel">Cancel</button>
        </div>
      </div>`;
  }

  // Read what is on screen into the editor state, before a redraw.
  _vpReadEditor() {
    const root = this.shadowRoot, ed = this._vpEdit;
    if (!root || !ed) return;
    const val = id => root.getElementById(id)?.value;
    ed.name = val("vpName") ?? ed.name;
    ed.phrases = [...root.querySelectorAll(".vp-phrase")].map(el => el.value);
    ed.match = val("vpMatch") ?? ed.match;
    ed.reply = val("vpReply") ?? ed.reply;
    if (ed.kind === "shell") ed.shell = val("vpShell") ?? ed.shell;
    else if (ed.kind === "advanced") {
      ed.service = val("vpService") ?? ed.service;
      ed.entities = val("vpEntities") ?? ed.entities;
      ed.data = val("vpData") ?? ed.data;
    } else ed.entity = val("vpEntity") ?? ed.entity;
  }

  // The entry the backend checks. Problems the panel can see first are
  // returned as text; everything else the backend checks again.
  _vpBuildEntry() {
    const ed = this._vpEdit;
    const entry = {
      name: ed.name.trim(),
      phrases: ed.phrases.map(p => p.trim()).filter(Boolean),
      match: ed.match,
      allow_ai: ed.kind === "shell" ? false : !!ed.allow_ai,
      enabled: ed.enabled,
      reply: ed.reply.trim(),
    };
    if (ed.phrase_id) entry.phrase_id = ed.phrase_id;
    if (ed.kind === "shell") {
      if (!ed.shell) return { error: this._t("Pick a shell command.") };
      entry.action = { service: "shell_command." + ed.shell, data: {} };
    } else if (ed.kind === "advanced") {
      let data = {};
      if (ed.data.trim()) {
        try { data = JSON.parse(ed.data); } catch (_) { return { error: this._t("The data is not valid JSON.") }; }
      }
      const ids = ed.entities.split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
      entry.action = { service: ed.service.trim(), data };
      if (ids.length) entry.action.target = { entity_id: ids.length === 1 ? ids[0] : ids };
    } else {
      if (!ed.entity) return { error: this._t("Pick what to run.") };
      entry.action = { service: NovaPanel.VP_SERVICE[ed.kind], target: { entity_id: ed.entity }, data: {} };
    }
    return { entry };
  }

  async _vpSave(entry) {
    try {
      const res = await this._hass.callWS({ type: "nova/voice_phrases_save", entry });
      return { ok: true, warnings: res?.warnings || [] };
    } catch (err) {
      return { ok: false, error: String(err?.message || err || this._t("Couldn't save.")) };
    }
  }

  _vpFind(pid) {
    return ((this._vp && this._vp.entries) || []).find(e => e.phrase_id === pid);
  }

  _vpShowWarnings(warnings) {
    if (!warnings || !warnings.length) return;
    const host = this.shadowRoot?.getElementById("vpTryResult");
    if (host) this._setText(host, warnings.join(" "));
  }

  // Wired from _wireSettings on every Settings render. One listener on the
  // card, so redraws of #vpBody need no rewiring.
  _wireVoicePhrases() {
    const root = this.shadowRoot;
    const card = root?.getElementById("settings-card-voice_phrases");
    if (!card) return;
    if (!this._vpFetchedOnce) {
      this._vpFetchedOnce = true;
      this._fetchVoicePhrases();
    }
    card.addEventListener("click", ev => this._vpClick(ev));
    card.addEventListener("change", ev => {
      if (ev.target?.id === "vpKind" && this._vpEdit) {
        this._vpReadEditor();
        this._vpEdit.kind = ev.target.value;
        if (this._vpEdit.kind === "shell") this._vpEdit.allow_ai = false;
        this._vpRedraw();
      }
    });
    card.addEventListener("keydown", ev => {
      if (ev.target?.id === "vpTry" && ev.key === "Enter") { ev.preventDefault(); this._vpTry(); }
    });
  }

  async _vpTry() {
    const root = this.shadowRoot;
    const text = (root?.getElementById("vpTry")?.value || "").trim();
    const out = root?.getElementById("vpTryResult");
    if (!out || !text || !this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/voice_phrases_test", text });
      const m = res && res.match;
      if (!m) {
        this._setText(out, this._vp && this._vp.enabled === false
          ? "Voice phrases are off, so nothing matches."
          : "No phrase matches. Nothing was run.");
      } else {
        this._setText(out, m.tier === "exact"
          ? this._t("Matches \"{name}\" exactly. Nothing was run.", { name: m.name })
          : this._t("Matches \"{name}\" (flexible). Nothing was run.", { name: m.name }));
      }
    } catch (_) {
      this._setText(out, "Couldn't test that sentence.");
    }
  }

  async _vpClick(ev) {
    const btn = ev.target?.closest?.("button");
    if (!btn || !this._hass) return;
    const pid = btn.getAttribute("data-pid");
    if (btn.id === "vpEnabled") {
      const on = !(this._vp && this._vp.enabled !== false);
      try {
        await this._hass.callWS({ type: "nova/voice_phrases_set_enabled", enabled: on });
      } catch (err) { console.error("Nova: voice phrases switch failed", err); }
      await this._fetchVoicePhrases();
    } else if (btn.id === "vpAdd") {
      this._vpEdit = this._vpEditFrom(null);
      this._vpRedraw();
    } else if (btn.classList.contains("vp-edit")) {
      const e = this._vpFind(pid);
      if (e) { this._vpEdit = this._vpEditFrom(e); this._vpRedraw(); }
    } else if (btn.classList.contains("vp-del")) {
      const e = this._vpFind(pid);
      if (!e || !window.confirm(this._t("Delete the voice phrase \"{name}\"?", { name: e.name }))) return;
      try {
        await this._hass.callWS({ type: "nova/voice_phrases_delete", phrase_id: pid });
      } catch (err) { console.error("Nova: voice phrase delete failed", err); }
      await this._fetchVoicePhrases();
    } else if (btn.classList.contains("vp-toggle")) {
      const e = this._vpFind(pid);
      if (!e) return;
      const res = await this._vpSave({ ...e, enabled: e.enabled === false });
      if (!res.ok) console.error("Nova: voice phrase toggle failed", res.error);
      await this._fetchVoicePhrases();
    } else if (btn.classList.contains("vp-link")) {
      const target = this.shadowRoot?.getElementById("settings-card-" + btn.getAttribute("data-goto"));
      if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (btn.id === "vpTryBtn") {
      await this._vpTry();
    } else if (btn.id === "vpPhraseAdd") {
      this._vpReadEditor();
      if (this._vpEdit.phrases.length < 10) this._vpEdit.phrases.push("");
      this._vpRedraw();
    } else if (btn.classList.contains("vp-phrase-del")) {
      this._vpReadEditor();
      this._vpEdit.phrases.splice(Number(btn.getAttribute("data-i")), 1);
      if (!this._vpEdit.phrases.length) this._vpEdit.phrases.push("");
      this._vpRedraw();
    } else if (btn.id === "vpAllowAi") {
      this._vpReadEditor();
      this._vpEdit.allow_ai = !this._vpEdit.allow_ai;
      this._vpRedraw();
    } else if (btn.id === "vpCancel") {
      this._vpEdit = null;
      this._vpRedraw();
    } else if (btn.id === "vpSave") {
      this._vpReadEditor();
      const built = this._vpBuildEntry();
      if (built.error) { this._vpEdit.errors = built.error; this._vpRedraw(); return; }
      btn.disabled = true;
      const res = await this._vpSave(built.entry);
      if (!res.ok) {
        this._vpEdit.errors = res.error;
        this._vpRedraw();
        return;
      }
      this._vpEdit = null;
      await this._fetchVoicePhrases();
      this._vpShowWarnings(res.warnings);
    }
  }
