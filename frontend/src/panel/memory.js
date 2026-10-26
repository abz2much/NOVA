  // ─── Memory ───────────────────────────────────────────────────────────
  // Four sections: what Nova knows (with the TEACH form), facts waiting for
  // a yes, relations between things, and habits tied to one person. Each
  // section redraws only its own box after a fetch or an action, so text
  // half typed into TEACH is never wiped by a full render.
  _htmlMemory() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">What Nova Knows</div>
            <div class="panel-meta" id="newMemCount">—</div>
          </div>
          <div class="stub-body">Durable facts &amp; preferences Nova recalls in conversation. Teach it something, or forget anything with ✕.</div>
          <div class="cfg-row cfg-row-wrap">
            <label class="field" for="newMemKey"><span>what (e.g. trash day)</span>
              <input id="newMemKey" class="cfg-field" autocomplete="off"></label>
            <label class="field" for="newMemVal"><span>is (e.g. Tuesday)</span>
              <input id="newMemVal" class="cfg-field" autocomplete="off"></label>
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
          <div class="stub-body">Links between things, such as "house member owns bike" or "child's room adjacent_to hallway". Nova proposes them when you tell it how things relate. A new link waits here and is not used until you confirm it. Only confirmed links are shown to Nova. Removing one is permanent: Nova will not add it back on its own.</div>
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

  // One call to Nova: { reply } on success, { failed } with the error text.
  async _memAsk(msg) {
    try {
      return { reply: await this._hass.callWS(msg) };
    } catch (err) {
      return { failed: String(err), err };
    }
  }

  // Stores a new fact list, keeping the waiting list and stats unless given.
  // The tab may act before its first load, when there is nothing to keep.
  _keepKnowledge(facts, pending) {
    const was = this._knowledge || {};
    this._knowledge = { facts, pending: pending ?? was.pending ?? [], stats: was.stats ?? {} };
  }

  // Calls fn(id, button) when a button carrying a numeric data-id is clicked.
  _memBind(box, selector, fn) {
    box.querySelectorAll(selector).forEach(btn => btn.addEventListener("click", () => {
      const id = parseInt(btn.getAttribute("data-id"), 10);
      if (!isNaN(id)) fn(id, btn);
    }));
  }

  async _fetchKnowledge() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/get_knowledge" });
    this._knowledge = failed
      ? { facts: [], pending: [], stats: {}, error: failed }
      : { facts: reply?.facts || [], pending: reply?.pending || [], stats: reply?.stats || {} };
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _fetchPersonRoutines() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/get_person_routines" });
    this._personRoutines = failed ? { groups: {}, error: failed } : { groups: reply?.routines || {} };
    this._personRoutinesLoaded = true;
    this._renderPersonRoutines();
  }

  _renderPersonRoutines() {
    const box = this.shadowRoot?.getElementById("newProutineList");
    if (!box) return;
    const state = this._personRoutines || {};
    if (state.error) {
      this._setHtml(box, `<div class="stub-body">${this._tHtml("Couldn't load routines — {error}", { error: this._esc(state.error) })}</div>`);
      return;
    }
    const groups = state.groups || {}, people = Object.keys(groups).sort();
    if (!people.length) {
      this._setHtml(box, this._personRoutinesLoaded
        ? `<div class="stub-body">Nothing person-specific learned yet — Nova needs a few weeks of sole-occupant data before routines are confidently individual.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    // "person_alex" reads "Person Alex". Capitalise first, then escape, so a
    // name holding & or quotes is not turned into a broken entity.
    const title = key => this._esc(key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()));
    const surest = (a, b) => (b.confidence || 0) - (a.confidence || 0);
    const habit = r => `
            <div class="cfg-row">
              <label>${this._esc(r.description)}</label>
              <span class="toggle-desc">${Math.round((r.confidence || 0) * 100)}% · ×${r.occurrences || "?"}</span>
            </div>`;
    this._setHtml(box, people
      .map(person => `<div class="mode-bind-head">${title(person)}</div>${groups[person].slice().sort(surest).map(habit).join("")}`)
      .join(""));
  }

  _renderKnowledgeList() {
    const root = this.shadowRoot;
    const box = root?.getElementById("newMemList");
    if (!box) return;
    const known = this._knowledge || {}, facts = known.facts || [];
    const count = root.getElementById("newMemCount");
    if (count) this._setText(count, this._t(facts.length === 1 ? "{count} fact" : "{count} facts", { count: facts.length }));
    if (known.error) {
      this._setHtml(box, `<div class="stub-body">${this._tHtml("Couldn't load memory — {error}", { error: this._esc(known.error) })}</div>`);
      return;
    }
    if (!facts.length) {
      this._setHtml(box, this._knowledgeLoaded
        ? `<div class="stub-body">Nothing yet. Say "remember that…" to Nova, or teach it above.</div>`
        : `<div class="stub-body">Loading…</div>`);
      return;
    }
    // Household first, then the other subjects by name.
    const bySubject = {};
    for (const f of facts) (bySubject[f.subject] = bySubject[f.subject] || []).push(f);
    const subjectNames = { household: "Household", primary: "About me" };
    const rank = s => (s === "household" ? 0 : 1);
    const subjects = Object.keys(bySubject).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    // A fact not stated outright, or held at under 90%, carries a "~" saying how sure Nova is.
    const factRow = f => {
      const sure = f.confidence ?? 1;
      const hedge = f.source !== "stated" || sure < 0.9
        ? `<span title="${this._tHtml("{source} · {percent}% sure", { source: this._esc(this._tx(f.source)), percent: Math.round(sure * 100) })}">~</span>`
        : "";
      const expiry = f.expires_at ? `<span title="expires">⌛</span>` : "";
      return `
          <div class="cfg-row" data-id="${f.id}">
            <label>${this._esc(f.key)}</label>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="toggle-desc">${this._esc(f.value)}${hedge}${expiry}</span>
              <button class="new-mem-forget danger" data-id="${f.id}" title="Forget this" aria-label="Forget">✕</button>
            </div>
          </div>`;
    };
    this._setHtml(box, subjects
      .map(subj => `<div class="mode-bind-head">${subjectNames[subj] || this._esc(subj.replace(/_/g, " "))}</div>${bySubject[subj].map(factRow).join("")}`)
      .join(""));
    this._memBind(box, ".new-mem-forget", id => this._forgetKnowledge(id));
  }

  async _teachKnowledge() {
    const root = this.shadowRoot;
    if (!root || !this._hass) return;
    const [keyEl, valEl, subjEl] = ["newMemKey", "newMemVal", "newMemSubject"].map(id => root.getElementById(id));
    const key = (keyEl?.value || "").trim(), value = (valEl?.value || "").trim();
    const subject = subjEl?.value || "household";
    if (!key || !value) return;
    const kind = subject === "primary" ? "preference" : "fact";
    const { reply, failed, err } = await this._memAsk({ type: "nova/add_knowledge", key, value, subject, kind });
    if (failed) {
      this._fail(err, "teach");
    } else {
      this._keepKnowledge(reply?.facts || []);
      [keyEl, valEl].forEach(el => { if (el) el.value = ""; });
      if (keyEl) keyEl.focus();
    }
    this._knowledgeLoaded = true;
    this._renderKnowledgeList();
  }

  async _forgetKnowledge(id) {
    if (!this._hass) return;
    const fact = (this._knowledge?.facts || []).find(f => f.id === id);
    if (!window.confirm(this._t("Forget \"{name}\"?", { name: fact ? fact.key : "" }))) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/forget_knowledge", fact_id: id });
    if (failed) this._fail(err, "forget");
    else this._keepKnowledge(reply?.facts || []);
    this._renderKnowledgeList();
  }

  async _pendingFactAction(id, action) {
    if (!this._hass) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/pending_fact_action", fact_id: id, action });
    if (failed) this._fail(err, `${action}`);
    else this._keepKnowledge(reply?.facts || this._knowledge?.facts || [], reply?.pending || []);
    this._renderKnowledgeList();
    this._renderPendingFacts();
  }

  async _editPendingFact(id, value) {
    if (!this._hass || !value) return;
    const { reply, failed, err } = await this._memAsk({ type: "nova/edit_pending_fact", fact_id: id, value });
    if (failed) this._fail(err, "edit pending fact");
    else this._keepKnowledge(this._knowledge?.facts || [], reply?.pending || []);
    this._renderPendingFacts();
  }

  _renderPendingFacts() {
    const root = this.shadowRoot;
    const panel = root?.getElementById("newPendingPanel"), box = root?.getElementById("newPendingList");
    if (!panel || !box) return;
    const waiting = this._knowledge?.pending || [];
    panel.hidden = !waiting.length;
    if (!waiting.length) { this._setHtml(box, ""); return; }
    const countEl = root.getElementById("newPendingCount");
    if (countEl) this._setText(countEl, this._t("{count} waiting", { count: waiting.length }));
    this._setHtml(box, waiting.map(f => `
      <div class="cfg-row" data-id="${f.id}">
        <label>${this._esc(f.key)}</label>
        <input class="cfg-field new-pending-edit-val" style="flex:1" data-id="${f.id}" value="${this._esc(f.value)}">
      </div>
      <div class="mode-grid" style="margin-bottom:10px">
        <button class="mode-chip new-pending-confirm" data-id="${f.id}">✓ Confirm</button>
        <button class="mode-chip danger new-pending-reject" data-id="${f.id}">✕ Reject</button>
        <button class="mode-chip new-pending-save-edit" data-id="${f.id}">💾 Save edit</button>
      </div>`).join(""));
    this._memBind(box, ".new-pending-confirm", id => this._pendingFactAction(id, "confirm"));
    this._memBind(box, ".new-pending-reject", id => this._pendingFactAction(id, "reject"));
    this._memBind(box, ".new-pending-save-edit", id => {
      const typed = (box.querySelector(`.new-pending-edit-val[data-id="${id}"]`)?.value || "").trim();
      if (typed) this._editPendingFact(id, typed);
    });
  }

  async _fetchRelations() {
    if (!this._hass) return;
    const { reply, failed } = await this._memAsk({ type: "nova/list_relations" });
    this._relations = failed
      ? { pending: [], confirmed: [], cap: 500, error: failed }
      : { pending: reply?.pending || [], confirmed: reply?.confirmed || [], cap: reply?.cap || 500 };
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
        <button class="new-rel-remove danger" data-id="${r.id}" title="Remove this relation" aria-label="Remove">✕ Remove</button>
      </div>`).join("")
      : (this._relationsLoaded
        ? `<div class="stub-body">None yet. Tell Nova how things relate, for example "House member owns the bike", then confirm it here. Only you can confirm a link, Nova cannot.</div>`
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
    list.querySelectorAll(".new-rel-remove").forEach(b => b.addEventListener("click", e => {
      const id = idOf(e.currentTarget);
      if (window.confirm(this._tx("Remove this link? Nova will not add it back on its own."))) this._relationAction(id, "remove");
    }));
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

