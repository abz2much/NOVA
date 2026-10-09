  // ─── Memory ───────────────────────────────────────────────────────────
  // Ported from Classic's own Memory tab (nova-panel.js): curated facts
  // ("What Nova Knows" + TEACH form), a Pending Confirmation queue for
  // facts staged via "remember that…" but not yet approved, and Person
  // Routines (habits confidently attributed to one person). Each section
  // patches its own container after a fetch/action rather than doing a
  // full _render() — the TEACH inputs are free text the user may be
  // mid-typing, and a full re-render would wipe them the same way it
  // would for AI Models/Cameras/Appliances.
  _htmlMemory() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">What Nova Knows</div>
            <div class="panel-meta" id="newMemCount">—</div>
          </div>
          <div class="stub-body">Durable facts &amp; preferences Nova recalls in conversation. Teach it something, or forget anything with ✕.</div>
          <div class="cfg-row cfg-row-wrap">
            <input id="newMemKey" class="cfg-field" style="flex:1" placeholder="what (e.g. trash day)" autocomplete="off">
            <input id="newMemVal" class="cfg-field" style="flex:1" placeholder="is (e.g. Tuesday)" autocomplete="off">
            <select id="newMemSubject" class="cfg-field">
              <option value="household">Household</option>
              <option value="primary">About me</option>
            </select>
            <button class="mode-chip" id="newMemAdd">TEACH</button>
          </div>
          <div id="newMemList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel" id="newPendingPanel" hidden>
          <div class="panel-head">
            <div class="panel-title">Pending Confirmation</div>
            <div class="panel-meta" id="newPendingCount">—</div>
          </div>
          <div class="stub-body">Nova proposed these while talking with you — from "remember that…" — but nobody confirmed them yet, so they aren't trusted or used in conversation until you approve, edit, or reject them here.</div>
          <div id="newPendingList" class="mem-body"></div>
        </div>

        <div class="panel" id="newRelationsPanel">
          <div class="panel-head">
            <div class="panel-title">Relations</div>
            <div class="panel-meta" id="newRelationsCount">—</div>
          </div>
          <div class="stub-body">Links between things, such as "sam owns car.jeep" or "kitchen adjacent_to garage". Nova proposes them when you tell it how things relate. A new link waits here and is not used until you confirm it. Only confirmed links are shown to Nova. Removing one is permanent: Nova will not add it back on its own.</div>
          <div class="toggle-desc" id="newRelationsMsg"></div>
          <div id="newRelationsPending" class="mem-body"></div>
          <div class="mode-bind-head">Confirmed</div>
          <div id="newRelationsList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Person Routines</div>
            <div class="panel-meta">Learned</div>
          </div>
          <div class="stub-body">Habits Nova has confidently attributed to one person, from 30 days of sole-occupant activity — separate from household-wide facts above.</div>
          <div id="newProutineList" class="mem-body"><div class="stub-body">Loading…</div></div>
        </div>
    `;
  }

  async _fetchKnowledge() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_knowledge" });
      this._knowledge = { facts: res?.facts || [], pending: res?.pending || [], stats: res?.stats || {} };
    } catch (err) {
      this._knowledge = { facts: [], pending: [], stats: {}, error: String(err) };
    }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _fetchPersonRoutines() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_person_routines" });
      this._personRoutines = { groups: res?.routines || {} };
    } catch (err) {
      this._personRoutines = { groups: {}, error: String(err) };
    }
    this._personRoutinesLoaded = true;
    this._renderPersonRoutines();
  }

  _renderPersonRoutines() {
    const list = this.shadowRoot?.getElementById("newProutineList");
    if (!list) return;
    const groups = this._personRoutines?.groups || {};
    const people = Object.keys(groups).sort();
    if (this._personRoutines?.error) {
      this._setHtml(list, `<div class="stub-body">${this._tHtml("Couldn't load routines — {error}", { error: this._esc(this._personRoutines.error) })}</div>`);
      return;
    }
    if (!people.length) {
      this._setHtml(list, this._personRoutinesLoaded
        ? `<div class="stub-body">Nothing person-specific learned yet — Nova needs a few weeks of sole-occupant data before routines are confidently individual.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    this._setHtml(list, people.map(person => {
      const items = groups[person]
        .slice().sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
        .map(r => {
          const pct = Math.round((r.confidence || 0) * 100);
          return `
            <div class="cfg-row">
              <label>${this._esc(r.description)}</label>
              <span class="toggle-desc">${pct}% · ×${r.occurrences || "?"}</span>
            </div>`;
        }).join("");
      const label = this._esc(person.replace(/_/g, " ")).replace(/\b\w/g, c => c.toUpperCase());
      return `<div class="mode-bind-head">${label}</div>${items}`;
    }).join(""));
  }

  _renderKnowledgeList() {
    const list = this.shadowRoot?.getElementById("newMemList");
    if (!list) return;
    const facts = this._knowledge?.facts || [];
    const count = this.shadowRoot?.getElementById("newMemCount");
    if (count) this._setText(count, this._t(facts.length === 1 ? "{count} fact" : "{count} facts", { count: facts.length }));
    if (this._knowledge?.error) {
      this._setHtml(list, `<div class="stub-body">${this._tHtml("Couldn't load memory — {error}", { error: this._esc(this._knowledge.error) })}</div>`);
      return;
    }
    if (!facts.length) {
      this._setHtml(list, this._knowledgeLoaded
        ? `<div class="stub-body">Nothing yet. Say "remember that…" to Nova, or teach it above.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    const groups = {};
    facts.forEach(f => { (groups[f.subject] = groups[f.subject] || []).push(f); });
    const labels = { household: "Household", primary: "About me" };
    const order = Object.keys(groups).sort(
      (a, b) => (a === "household" ? -1 : b === "household" ? 1 : a.localeCompare(b)));
    this._setHtml(list, order.map(subj => {
      const items = groups[subj].map(f => {
        const soft = (f.source !== "stated" || (f.confidence ?? 1) < 0.9);
        const hedge = soft
          ? `<span title="${this._tHtml("{source} · {percent}% sure", { source: this._esc(this._tx(f.source)), percent: Math.round((f.confidence ?? 1) * 100) })}">~</span>`
          : "";
        const exp = f.expires_at ? `<span title="expires">⌛</span>` : "";
        return `
          <div class="cfg-row" data-id="${f.id}">
            <label>${this._esc(f.key)}</label>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="toggle-desc">${this._esc(f.value)}${hedge}${exp}</span>
              <button class="new-mem-forget" data-id="${f.id}" title="Forget this" aria-label="Forget">✕</button>
            </div>
          </div>`;
      }).join("");
      const label = labels[subj] || this._esc(subj.replace(/_/g, " "));
      return `<div class="mode-bind-head">${label}</div>${items}`;
    }).join(""));
    list.querySelectorAll(".new-mem-forget").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._forgetKnowledge(id);
      });
    });
  }

  async _teachKnowledge() {
    const root = this.shadowRoot;
    if (!root || !this._hass) return;
    const keyEl = root.getElementById("newMemKey");
    const valEl = root.getElementById("newMemVal");
    const subjEl = root.getElementById("newMemSubject");
    const key = (keyEl?.value || "").trim();
    const value = (valEl?.value || "").trim();
    const subject = subjEl?.value || "household";
    if (!key || !value) return;
    try {
      const res = await this._hass.callWS({
        type: "nova/add_knowledge", key, value, subject,
        kind: subject === "primary" ? "preference" : "fact",
      });
      this._knowledge = { facts: res?.facts || [], pending: this._knowledge.pending, stats: this._knowledge.stats };
      if (keyEl) keyEl.value = "";
      if (valEl) valEl.value = "";
      if (keyEl) keyEl.focus();
    } catch (err) { console.error("Nova: teach failed", err); }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
  }

  async _forgetKnowledge(id) {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/forget_knowledge", fact_id: id });
      this._knowledge = { facts: res?.facts || [], pending: this._knowledge.pending, stats: this._knowledge.stats };
    } catch (err) { console.error("Nova: forget failed", err); }
    this._renderKnowledgeList();
  }

  async _pendingFactAction(id, action) {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/pending_fact_action", fact_id: id, action });
      this._knowledge = { facts: res?.facts || this._knowledge.facts, pending: res?.pending || [], stats: this._knowledge.stats };
    } catch (err) { console.error(`Nova: ${action} failed`, err); }
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _editPendingFact(id, value) {
    if (!this._hass || !value) return;
    try {
      const res = await this._hass.callWS({ type: "nova/edit_pending_fact", fact_id: id, value });
      this._knowledge = { facts: this._knowledge.facts, pending: res?.pending || [], stats: this._knowledge.stats };
    } catch (err) { console.error("Nova: edit pending fact failed", err); }
    this._renderPendingFacts();
  }

  _renderPendingFacts() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("newPendingPanel");
    const list = root?.getElementById("newPendingList");
    const countEl = root?.getElementById("newPendingCount");
    if (!panel || !list) return;
    const pending = this._knowledge?.pending || [];
    panel.hidden = pending.length === 0;
    if (!pending.length) { this._setHtml(list, ""); return; }
    if (countEl) this._setText(countEl, this._t("{count} waiting", { count: pending.length }));
    this._setHtml(list, pending.map(f => `
      <div class="cfg-row" data-id="${f.id}">
        <label>${this._esc(f.key)}</label>
        <input class="cfg-field new-pending-edit-val" style="flex:1" data-id="${f.id}" value="${this._esc(f.value)}">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip new-pending-confirm" data-id="${f.id}">✓ Confirm</button>
        <button class="mode-chip new-pending-reject" data-id="${f.id}">✕ Reject</button>
        <button class="mode-chip new-pending-save-edit" data-id="${f.id}">💾 Save edit</button>
      </div>`).join(""));
    list.querySelectorAll(".new-pending-confirm").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._pendingFactAction(id, "confirm");
      });
    });
    list.querySelectorAll(".new-pending-reject").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        if (!isNaN(id)) this._pendingFactAction(id, "reject");
      });
    });
    list.querySelectorAll(".new-pending-save-edit").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = parseInt(e.currentTarget.getAttribute("data-id"), 10);
        const input = list.querySelector(`.new-pending-edit-val[data-id="${id}"]`);
        const value = (input?.value || "").trim();
        if (!isNaN(id) && value) this._editPendingFact(id, value);
      });
    });
  }

  async _fetchRelations() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/list_relations" });
      this._relations = { pending: res?.pending || [], confirmed: res?.confirmed || [], cap: res?.cap || 500 };
    } catch (err) {
      this._relations = { pending: [], confirmed: [], cap: 500, error: String(err) };
    }
    this._relationsLoaded = true;
    this._renderRelations();
  }

  _relationErrorText(code) {
    return ({
      invalid_subject: "The first name must be 1 to 80 characters and cannot be 'unknown'.",
      invalid_object: "The second name must be 1 to 80 characters and cannot be 'unknown'.",
      invalid_predicate: "The link word must be lowercase with underscores, 2 to 40 characters, such as owns or adjacent_to.",
      self_relation: "A thing cannot be linked to itself.",
      duplicate: "That link already exists.",
      not_found: "That link is no longer waiting for confirmation.",
    })[code] || "Could not save that change.";
  }

  _renderRelations() {
    const root = this.shadowRoot;
    const pendingBox = root?.getElementById("newRelationsPending");
    const list = root?.getElementById("newRelationsList");
    if (!pendingBox || !list) return;
    const rel = this._relations || { pending: [], confirmed: [], cap: 500 };
    const countEl = root.getElementById("newRelationsCount");
    if (countEl) this._setText(countEl, this._t("{confirmed} confirmed · {waiting} waiting", { confirmed: rel.confirmed.length, waiting: rel.pending.length }));
    if (rel.error) {
      this._setHtml(pendingBox, "");
      this._setHtml(list, `<div class="stub-body">${this._tHtml("Couldn't load relations — {error}", { error: this._esc(rel.error) })}</div>`);
      return;
    }
    this._setHtml(pendingBox, rel.pending.length ? `
      <div class="mode-bind-head">Waiting for confirmation</div>` + rel.pending.map(r => `
      <div class="cfg-row cfg-row-wrap rel-row" data-id="${r.id}">
        <input class="cfg-field rel-subject" style="flex:1" maxlength="80" value="${this._esc(r.subject)}" aria-label="First thing">
        <input class="cfg-field rel-predicate" style="flex:1" maxlength="40" value="${this._esc(r.predicate)}" aria-label="Link">
        <input class="cfg-field rel-object" style="flex:1" maxlength="80" value="${this._esc(r.object)}" aria-label="Second thing">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip rel-confirm" data-id="${r.id}">✓ Confirm</button>
        <button class="mode-chip rel-save" data-id="${r.id}">💾 Save edit</button>
        <button class="mode-chip rel-reject" data-id="${r.id}">✕ Reject</button>
      </div>`).join("") : "");
    this._setHtml(list, rel.confirmed.length ? rel.confirmed.map(r => `
      <div class="cfg-row" data-id="${r.id}">
        <label>${this._esc(r.subject)} <b>${this._esc(r.predicate)}</b> ${this._esc(r.object)}</label>
        <button class="new-rel-remove" data-id="${r.id}" title="Remove this relation" aria-label="Remove">✕ Remove</button>
      </div>`).join("")
      : (this._relationsLoaded
        ? `<div class="stub-body">None yet. Tell Nova how things relate, for example "Sam owns the Jeep", then confirm it here. Only you can confirm a link, Nova cannot.</div>`
        : `<div class="stub-body">Loading…</div>`));
    const idOf = (el) => parseInt(el.getAttribute("data-id"), 10);
    pendingBox.querySelectorAll(".rel-confirm").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "confirm")));
    pendingBox.querySelectorAll(".rel-reject").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "reject")));
    pendingBox.querySelectorAll(".rel-save").forEach(b => b.addEventListener("click", e => {
      const id = idOf(e.currentTarget);
      const row = pendingBox.querySelector(`.rel-row[data-id="${id}"]`);
      if (!isNaN(id) && row) this._editRelation(id, {
        subject: row.querySelector(".rel-subject").value,
        predicate: row.querySelector(".rel-predicate").value,
        object: row.querySelector(".rel-object").value,
      });
    }));
    list.querySelectorAll(".new-rel-remove").forEach(b => b.addEventListener("click", e => this._relationAction(idOf(e.currentTarget), "remove")));
  }

  async _relationAction(id, action) {
    if (!this._hass || isNaN(id)) return;
    const msg = this.shadowRoot?.getElementById("newRelationsMsg");
    try {
      const res = await this._hass.callWS({ type: "nova/relation_action", relation_id: id, action });
      this._relations = { pending: res?.pending || [], confirmed: res?.confirmed || [], cap: res?.cap || 500 };
      if (msg) this._setText(msg, res?.ok ? "" : "That link has already changed.");
    } catch (err) {
      if (msg) this._setText(msg, "Could not change that link (administrator only).");
      return;
    }
    this._renderRelations();
  }

  async _editRelation(id, fields) {
    if (!this._hass || isNaN(id)) return;
    const msg = this.shadowRoot?.getElementById("newRelationsMsg");
    try {
      const res = await this._hass.callWS({ type: "nova/edit_relation", relation_id: id, ...fields });
      if (!res?.ok) {
        // Keep what the person typed so they can fix it.
        if (msg) this._setText(msg, this._relationErrorText(res?.error));
        return;
      }
      this._relations = { pending: res?.pending || [], confirmed: res?.confirmed || [], cap: res?.cap || 500 };
      if (msg) this._setText(msg, "Saved.");
    } catch (err) {
      if (msg) this._setText(msg, "Could not save that change (administrator only).");
      return;
    }
    this._renderRelations();
  }

  _wireMemory() {
    const root = this.shadowRoot;
    const memAdd = root.getElementById("newMemAdd");
    if (memAdd) {
      memAdd.addEventListener("click", () => this._teachKnowledge());
      ["newMemKey", "newMemVal"].forEach(id => {
        const el = root.getElementById(id);
        if (el) el.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); this._teachKnowledge(); }
        });
      });
    }
    this._renderKnowledgeList();
    this._renderPendingFacts();
    this._renderPersonRoutines();
    this._fetchRelations();
  }

