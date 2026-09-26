  // ─── Logs ─────────────────────────────────────────────────────────────
  // Ported from Classic's own _fetchDebugLog (nova-panel.js) — same single
  // nova/get_debug_log call, same client-side category+search filtering,
  // and the same escaping discipline: e.ts/e.cat/e.msg are log CONTENT
  // (entity names, states, model output can end up in them), so they are
  // attacker/LLM-influenced and go through this._esc() before innerHTML —
  // this is a real fixed-XSS surface in Classic, not decorative caution.
  // Kept in sync with every literal category string passed to nova_log()
  // across the backend (grep `nova_log("` to re-verify). ROUTE/REASON/TTS
  // removed 13 Sept 2026: nothing in the backend logs under those
  // categories any more, so their chips could never match a real entry —
  // caught live when a real "LEARN" entry (used, but missing from both
  // this list and the color map) fell through to a bare "•" bullet with
  // no way to filter for it.
  static LOG_FILTERS = ["all", "CONV", "REPLY", "LOCAL", "LEARN", "AGENT", "AUTO", "MODE", "CONFIG",
    "CLASSIFY", "CAMERA", "ENERGY", "BIO", "OFFER", "SAFETY", "ERROR", "WARNING", "GATE", "DEDUP", "OFFLINE"];
  static LOG_CATEGORIES = {
    CONV: { color: "#5fd0e0", icon: "💬" },
    REPLY: { color: "#4fb8ff", icon: "💭" },
    LOCAL: { color: "#5fbf7a", icon: "⚡" },
    LEARN: { color: "#8fd15c", icon: "🧠" },
    AGENT: { color: "var(--gold)", icon: "🤖" },
    AUTO: { color: "#ffb454", icon: "🔁" },
    MODE: { color: "#c9a0ff", icon: "🎚️" },
    CONFIG: { color: "#9d8cff", icon: "⚙️" },
    CLASSIFY: { color: "#9d8cff", icon: "🏷️" },
    CAMERA: { color: "#5fbf7a", icon: "📷" },
    ENERGY: { color: "#ffcf6a", icon: "🔌" },
    BIO: { color: "#ff8fc7", icon: "💓" },
    OFFER: { color: "#ffd27a", icon: "🙋" },
    SAFETY: { color: "#ff8a8a", icon: "🛡️" },
    ERROR: { color: "#ff6b81", icon: "❌" },
    WARNING: { color: "var(--warn)", icon: "⚠️" },
    GATE: { color: "var(--ink-faint)", icon: "🚧" },
    DEDUP: { color: "var(--ink-faint)", icon: "🔇" },
    OFFLINE: { color: "var(--ink-faint)", icon: "📴" },
  };

  _htmlLogs() {
    const filterChips = NovaPanel.LOG_FILTERS.map(f =>
      `<button class="mode-chip new-log-filter${(this._logFilter || "all") === f ? " mode-chip-on" : ""}" data-filter="${f}">${f.toUpperCase()}</button>`).join("");
    const view = this._logView || "system";
    const title = view === "decisions" ? "Decisions" : view === "spoken_history" ? "Spoken History"
      : view === "actions" ? "Actions" : "System Log";
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">${title}</div>
            <div class="panel-meta">Nova internal</div>
          </div>
          <div class="mode-grid">
            <button class="mode-chip new-logview${view === "system" ? " mode-chip-on" : ""}" data-view="system">SYSTEM LOG</button>
            <button class="mode-chip new-logview${view === "decisions" ? " mode-chip-on" : ""}" data-view="decisions">DECISIONS</button>
            <button class="mode-chip new-logview${view === "spoken_history" ? " mode-chip-on" : ""}" data-view="spoken_history">SPOKEN HISTORY</button>
            <button class="mode-chip new-logview${view === "actions" ? " mode-chip-on" : ""}" data-view="actions">ACTIONS</button>
          </div>
          ${view === "decisions" ? this._htmlDecisionsView() : view === "spoken_history" ? this._htmlSpokenHistoryView()
            : view === "actions" ? this._htmlActionsView() : `
          <div class="cfg-row">
            <input id="newLogSearch" class="cfg-field" style="flex:1" type="text" placeholder="search…" autocomplete="off" value="${this._esc(this._logSearch || "")}">
          </div>
          <div class="mode-grid">${filterChips}</div>
          <div class="toggle-desc" id="newLogCount" style="margin:8px 0"></div>
          <div id="newLogEntries" class="new-log-entries">
            <div class="stub-body">Loading…</div>
          </div>`}
        </div>
    `;
  }

  // ─── Spoken History (v7.104.0) ──────────────────────────────────────────
  // The last things Nova actually sent to a speaker — welcome-home,
  // reminders, alerts, briefings, manual tests, confirmed Assist replies,
  // and repeats. Text only, bounded to the last 100, newest first. Always
  // renders its heading; a distinct empty vs. error state, same pattern as
  // Provider Activity/Installed Automations.

  _spokenSourceLabel(source) {
    return {
      welcome: "Welcome", reminder: "Reminder", alert: "Alert",
      briefing: "Briefing", camera: "Camera", manual: "Manual",
      reply: "Reply", repeat: "Repeat", routine: "Routine", scene: "Scene",
      confirm: "Confirmation", followup: "Follow-up",
    }[source] || (source ? source[0].toUpperCase() + source.slice(1) : "Other");
  }

  _speakerLabel(eid) {
    const st = this._hass?.states?.[eid];
    return (st && st.attributes && st.attributes.friendly_name) || eid;
  }

  _htmlSpokenHistoryView() {
    // Static shell only — _fetchSpokenHistory()/_renderSpokenHistoryRows()
    // update #spokenHistoryEntries directly, the same way _fetchDecisions()/
    // _renderDecisionRows() do, so a fetch never has to go through a full
    // _render() (which would re-trigger _wire() and re-fetch, looping).
    return `<div id="spokenHistoryEntries"><div class="stub-body">Loading…</div></div>`;
  }

  async _fetchSpokenHistory() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_spoken_history" });
      this._spokenHistory = result.entries || [];
    } catch (_) { this._spokenHistory = null; }
    this._renderSpokenHistoryRows();
  }

  _renderSpokenHistoryRows() {
    const container = this.shadowRoot?.getElementById("spokenHistoryEntries");
    if (!container) return;
    const entries = this._spokenHistory;
    if (entries === null) {
      container.innerHTML = `<div class="stub-body">Couldn't load spoken history.</div>`;
      return;
    }
    if (!entries || !entries.length) {
      container.innerHTML = `<div class="stub-body">No spoken messages recorded yet.</div>`;
      return;
    }
    container.innerHTML = entries.map(e => {
      const when = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString() : "";
      const speakerNames = (e.speakers || []).map(s => this._speakerLabel(s)).join(", ") || "—";
      return `
        <div class="cfg-row">
          <label>${this._esc(this._spokenSourceLabel(e.source))} · ${this._esc(when)}</label>
          <span class="toggle-desc">${this._esc((e.delivery_state || "sent").toUpperCase())}</span>
        </div>
        <div class="stub-body" style="margin:-6px 0 4px">${this._esc(e.text)}</div>
        <div class="cfg-row">
          <span class="toggle-desc">${this._esc(speakerNames)}</span>
          <button class="mode-chip new-spoken-repeat" data-spoken-id="${e.id}">REPEAT</button>
        </div>`;
    }).join("");
    container.querySelectorAll(".new-spoken-repeat").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = parseInt(btn.getAttribute("data-spoken-id"), 10);
        if (!isNaN(id)) this._repeatSpoken(id);
      });
    });
  }

  async _repeatSpoken(spokenId) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/repeat_spoken", spoken_id: spokenId });
      this._fetchSpokenHistory();
    } catch (err) {
      console.error("Nova: repeat spoken failed", err);
    }
  }

  // ─── Actions (Action Audit Log) ─────────────────────────────────────────
  // Actions Nova genuinely attempted or performed — device controls, bulk
  // controls, scene/script/automation execution, safety routines, suggested-
  // automation installation, notifications. Request-level, keyset-paginated:
  // one page is a set of COMPLETE request groups (a bulk action's targets
  // are never split across pages). Strictly read-only — no retry/replay/
  // approve/reject control here, matching Spoken History and Decisions.

  _actionStatusClass(status) {
    return {
      success: "diag-ok", verified: "diag-ok",
      partial: "diag-warn", awaiting: "diag-warn",
      failed: "diag-down", blocked: "diag-down",
    }[status] || "diag-idle";
  }

  _htmlActionsView() {
    // Static shell only — _fetchActions()/_renderActionRows() update
    // #actionEntries directly, same pattern as Spoken History/Decisions, so
    // a fetch never re-triggers a full _render().
    return `
      <div id="actionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="actionLoadMoreRow" hidden>
        <button class="mode-chip" id="newActionLoadMore">LOAD MORE</button>
      </div>
    `;
  }

  async _fetchActions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._actions = []; this._actionsCursor = null; }
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (container && reset) container.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const args = { type: "nova/list_actions", limit: 20 };
      if (!reset && this._actionsCursor) {
        args.cursor_ts = this._actionsCursor.ts;
        args.cursor_request_id = this._actionsCursor.request_id;
      }
      const result = await this._hass.callWS(args);
      const page = result.requests || [];
      this._actions = reset ? page : (this._actions || []).concat(page);
      this._actionsCursor = result.next_cursor || null;
      this._renderActionRows();
    } catch (err) {
      this._actions = null;
      this._renderActionRows();
    }
  }

  _actionLabel(a) {
    return (a.action || "").replace(/_/g, " ");
  }

  _renderActionRows() {
    const container = this.shadowRoot?.getElementById("actionEntries");
    if (!container) return;
    const requests = this._actions;
    if (requests === null) {
      container.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Couldn't load actions.</div>`;
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    if (!requests || !requests.length) {
      container.innerHTML = `<div class="stub-body">No actions recorded yet.</div>`;
      const row = this.shadowRoot?.getElementById("actionLoadMoreRow");
      if (row) row.hidden = true;
      return;
    }
    container.innerHTML = requests.map(r => {
      const when = r.ts_created ? new Date(r.ts_created * 1000).toLocaleString() : "";
      const statusCls = this._actionStatusClass(r.status);
      const requester = r.requested_by_name || r.requested_by_user_id || r.request_device_id || "";
      const targets = r.targets || [];
      const spokenNote = (r.spoken_history_id !== null && r.spoken_history_id !== undefined)
        ? `<span class="toggle-desc" title="Linked Spoken History entry">🔊 spoken</span>` : "";
      const targetRows = targets.map(t => {
        const targetName = t.entity_id || [t.domain, t.service].filter(Boolean).join(".") || "—";
        return `
          <div class="cfg-row">
            <label>${this._esc(targetName)}</label>
            <span class="toggle-desc">approval: ${this._esc(t.approval_result)} · execution: ${this._esc(t.execution_result)}</span>
          </div>
          ${t.reason_text ? `<div class="stub-body" style="margin:-4px 0 6px;font-size:11px">${this._esc(t.reason_text)}</div>` : ""}`;
      }).join("");
      return `
        <details class="new-log-entry" style="display:block">
          <summary style="cursor:pointer;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="new-log-ts">${this._esc(when)}</span>
            <span class="new-log-cat">${this._esc((r.source || "").toUpperCase())}</span>
            <span class="new-log-msg">${this._esc(this._actionLabel(r))}${requester ? " · " + this._esc(requester) : ""}</span>
            <span class="${statusCls}">${this._esc((r.status || "").toUpperCase())}</span>
            ${spokenNote}
          </summary>
          <div style="margin-top:8px">${targetRows || '<div class="stub-body">No target detail.</div>'}</div>
        </details>`;
    }).join("");
    const loadMoreRow = this.shadowRoot?.getElementById("actionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._actionsCursor;
  }

  // ─── Decisions (Phase 1: decision explanations + feedback) ─────────────
  // A bounded, cursor-paginated browser over Nova's Decision Record store —
  // separate from the System Log above (nova/get_debug_log): these are the
  // structured observation/interpretation/evidence/outcome rows behind
  // nova/get_calibration's aggregate stats, not free-text log lines.

  _htmlDecisionsView() {
    return `
      <div class="cfg-row">
        <button class="mode-chip${this._decisionsUnjudgedOnly ? " mode-chip-on" : ""}" id="newDecUnjudged">UNJUDGED ONLY</button>
      </div>
      <div class="toggle-desc" id="newDecCount" style="margin:8px 0"></div>
      <div id="decisionEntries" class="new-log-entries">
        <div class="stub-body">Loading…</div>
      </div>
      <div class="cfg-row" id="decisionLoadMoreRow" hidden>
        <button class="mode-chip" id="newDecLoadMore">LOAD MORE</button>
      </div>
      <div id="decisionDrawer" class="new-decision-drawer" hidden></div>
    `;
  }

  async _fetchDecisions(reset = true) {
    if (!this._hass) return;
    if (reset) { this._decisions = []; this._decisionsCursor = null; }
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (container && reset) container.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const args = { type: "nova/list_decisions", limit: 50, only_unjudged: !!this._decisionsUnjudgedOnly };
      if (!reset && this._decisionsCursor) {
        args.cursor_ts = this._decisionsCursor.ts;
        args.cursor_id = this._decisionsCursor.id;
      }
      const result = await this._hass.callWS(args);
      this._decisions = reset ? (result.decisions || []) : (this._decisions || []).concat(result.decisions || []);
      this._decisionsCursor = result.next_cursor || null;
      this._renderDecisionRows();
    } catch (err) {
      if (container) container.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading decisions: ${this._esc(err)}</div>`;
    }
  }

  _renderDecisionRows() {
    const container = this.shadowRoot?.getElementById("decisionEntries");
    if (!container) return;
    const entries = this._decisions || [];
    const countEl = this.shadowRoot?.getElementById("newDecCount");
    if (countEl) countEl.textContent = `${entries.length} decision(s) loaded`;
    container.innerHTML = entries.length ? entries.map(d => {
      const outcomeCls = d.outcome === "good" ? "diag-ok" : d.outcome === "wrong" ? "diag-down"
        : d.outcome === "unnecessary" ? "diag-warn" : "diag-idle";
      const outcomeLabel = d.outcome ? d.outcome.toUpperCase() : "UNJUDGED";
      const when = d.ts ? new Date(d.ts * 1000).toLocaleString() : "";
      return `<div class="new-log-entry new-decision-row" data-id="${this._esc(d.id)}">
          <span class="new-log-ts">${this._esc(when)}</span>
          <span class="new-log-cat">${this._esc((d.kind || "").toUpperCase())}</span>
          <span class="new-log-msg">${this._esc(d.decision || "")}</span>
          <span class="${outcomeCls}">${this._esc(outcomeLabel)}</span>
        </div>`;
    }).join("") : `<div class="stub-body">No decisions recorded yet.</div>`;
    const loadMoreRow = this.shadowRoot?.getElementById("decisionLoadMoreRow");
    if (loadMoreRow) loadMoreRow.hidden = !this._decisionsCursor;
    container.querySelectorAll(".new-decision-row").forEach(row => {
      row.addEventListener("click", () => this._openDecisionDetail(parseInt(row.getAttribute("data-id"), 10)));
    });
  }

  _decisionBlockHtml(label, value) {
    const text = value === null || value === undefined || value === ""
      ? "—" : (typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));
    return `<div class="mode-bind-head">${this._esc(label)}</div>
      <pre class="new-sug-yaml" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin:0 0 8px">${this._esc(text)}</pre>`;
  }

  async _openDecisionDetail(id) {
    const drawer = this.shadowRoot?.getElementById("decisionDrawer");
    if (!drawer) return;
    drawer.hidden = false;
    drawer.innerHTML = `<div class="stub-body">Loading…</div>`;
    try {
      const result = await this._hass.callWS({ type: "nova/get_decision", decision_id: id });
      const d = result.decision || {};
      const judged = !!d.outcome;
      const row = (label, value) => `<div class="cfg-row"><label>${this._esc(label)}</label><span>${this._esc(
        value === null || value === undefined || value === "" ? "—" : String(value))}</span></div>`;
      drawer.innerHTML = `
        <div class="panel-head"><div class="panel-title">Decision #${this._esc(d.id)}</div>
          <button class="mode-chip" id="newDecCloseDrawer">CLOSE</button></div>
        ${row("Route", d.kind)}
        ${row("Decision", d.decision)}
        ${row("Reason", d.reason)}
        ${row("Confidence", d.confidence)}
        ${row("Model", d.model)}
        ${row("Tokens", d.tokens)}
        ${row("Latency (ms)", d.latency_ms)}
        ${row("Outcome", d.outcome)}
        ${this._decisionBlockHtml("Observation", d.observation)}
        ${this._decisionBlockHtml("Interpretation", d.interpretation)}
        ${this._decisionBlockHtml("Evidence", d.evidence)}
        <div class="mode-bind-head">Feedback</div>
        <div class="mode-grid">
          <button class="mode-chip new-dec-fb" data-verdict="good" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>HELPFUL</button>
          <button class="mode-chip new-dec-fb" data-verdict="unnecessary" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>UNNECESSARY</button>
          <button class="mode-chip new-dec-fb" data-verdict="wrong" data-id="${this._esc(d.id)}" ${judged ? "disabled" : ""}>WRONG</button>
        </div>
        <div class="toggle-desc" id="newDecFbStatus">${judged ? `Already judged: ${this._esc(d.outcome)}` : ""}</div>
        <div class="mode-bind-head">Decision Lab</div>
        <div class="cfg-row"><button class="mode-chip" id="newDecReplay" data-id="${this._esc(d.id)}">REPLAY</button></div>
        <div id="newDecReplayResult"></div>
      `;
      drawer.querySelector("#newDecCloseDrawer")?.addEventListener("click", () => {
        drawer.hidden = true; drawer.innerHTML = "";
      });
      drawer.querySelectorAll(".new-dec-fb").forEach(btn => {
        btn.addEventListener("click", () => this._submitDecisionOutcome(
          parseInt(btn.getAttribute("data-id"), 10), btn.getAttribute("data-verdict")));
      });
      drawer.querySelector("#newDecReplay")?.addEventListener("click", () => this._replayDecision(d.id));
    } catch (err) {
      drawer.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading decision: ${this._esc(err)}</div>`;
    }
  }

  async _replayDecision(id) {
    const resultEl = this.shadowRoot?.getElementById("newDecReplayResult");
    if (resultEl) resultEl.innerHTML = `<div class="stub-body">Replaying…</div>`;
    try {
      const r = await this._hass.callWS({ type: "nova/replay_decision", decision_id: id });
      if (!resultEl) return;
      const row = (label, value) => `<div class="cfg-row"><label>${this._esc(label)}</label><span>${this._esc(String(value))}</span></div>`;
      if (!r.supported) {
        resultEl.innerHTML = `
          <div class="toggle-desc" style="margin-top:8px"><b>${this._esc(r.label)}</b></div>
          <div class="stub-body">${this._esc(r.reason || "Not supported for this decision kind.")}</div>`;
        return;
      }
      resultEl.innerHTML = `
        <div class="toggle-desc" style="margin-top:8px"><b>${this._esc(r.label)}</b></div>
        ${row("Current suggestion threshold", r.current_threshold)}
        ${row("Would pass current threshold", r.would_pass_current_threshold ? "Yes" : "No")}
        ${row("Within 0.05 of threshold", r.within_0_05_of_threshold ? "Yes" : "No")}
      `;
    } catch (err) {
      if (resultEl) resultEl.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error running replay: ${this._esc(err)}</div>`;
    }
  }

  async _submitDecisionOutcome(id, verdict) {
    const statusEl = this.shadowRoot?.getElementById("newDecFbStatus");
    try {
      const result = await this._hass.callWS({ type: "nova/set_decision_outcome", decision_id: id, verdict });
      const disableButtons = () => this.shadowRoot?.querySelectorAll(".new-dec-fb")
        .forEach(b => b.setAttribute("disabled", "disabled"));
      if (result.status === "ok") {
        if (statusEl) statusEl.textContent = `Recorded: ${verdict}`;
        disableButtons();
        this._fetchDecisions(true);
      } else if (result.status === "already_judged") {
        if (statusEl) statusEl.textContent = "This decision was already judged.";
        disableButtons();
      } else {
        if (statusEl) statusEl.textContent = "Decision not found.";
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = `Error: ${this._esc(err)}`;
    }
  }

  // Pure DOM-render step, given an already-fetched entries array — no
  // network I/O. Called both synchronously from cache (_wire(), on
  // re-entering the System Log view) and from _fetchDebugLog()'s network
  // result, so a cached view renders immediately and a background refresh
  // reuses the exact same render path. Keeps the existing signature-based
  // skip (avoids flicker/scroll-jump on the shared 20s poll) — safe now
  // that the container is never left holding a stale "Loading…" shell by
  // the time this runs, cache-rendered or freshly fetched alike.
  _renderDebugLogEntries(entries) {
    const container = this.shadowRoot?.getElementById("newLogEntries");
    if (!container) return;
    if (!entries || !entries.length) {
      container.innerHTML = `<div class="stub-body">No entries yet. Talk to Nova to generate log entries.</div>`;
      return;
    }
    const cc = NovaPanel.LOG_CATEGORIES;
    const activeFilter = this._logFilter || "all";
    const categoryFiltered = activeFilter === "all" ? entries : entries.filter(e => e.cat === activeFilter);
    const search = (this._logSearch || "").trim().toLowerCase();
    const filtered = search
      ? categoryFiltered.filter(e => (e.msg || "").toLowerCase().includes(search) || (e.cat || "").toLowerCase().includes(search))
      : categoryFiltered;

    const countEl = this.shadowRoot?.getElementById("newLogCount");
    if (countEl) {
      countEl.textContent = search || activeFilter !== "all"
        ? `${filtered.length} of ${entries.length}`
        : `${entries.length} entries`;
    }

    const ordered = filtered.slice().reverse();

    // Skip the rebuild when nothing changed (same signature trick as
    // Classic) — avoids flicker/scroll-jump on the shared 20s poll. The
    // signature is stamped on the CONTAINER ELEMENT itself (dataset), not
    // kept as component-instance state: _render() tears down and rebuilds
    // #newLogEntries from scratch on every tab/view switch, so a fresh
    // container's dataset is naturally unstamped and this always proceeds
    // to render — instance-level state would instead persist a stale
    // "already rendered" signature across the rebuild and skip the render
    // that was needed to replace the shell's "Loading…" placeholder (the
    // exact bug this replaced).
    const first = ordered[0];
    const last = ordered[ordered.length - 1];
    const sig = ordered.length + "|" + (first ? first.ts + first.msg : "") + "|" + (last ? last.ts + last.msg : "");
    const renderSig = sig + "\0" + activeFilter + "\0" + search;
    if (renderSig === container.dataset.renderSig) {
      return;
    }
    const filterChanged = activeFilter !== container.dataset.renderFilter || search !== container.dataset.renderSearch;
    const nearTop = container.scrollTop < 40;
    const prevTop = container.scrollTop;

    container.innerHTML = ordered.length ? ordered.map(e => {
      const cat = cc[e.cat] || { color: "var(--ink-dim)", icon: "•" };
      const isError = e.cat === "ERROR" || (e.msg || "").toLowerCase().includes("error") || (e.msg || "").toLowerCase().includes("failed");
      const safeCat = this._esc(e.cat);
      return `<div class="new-log-entry${isError ? " new-log-entry-error" : ""}">
        <span class="new-log-ts">${this._esc(e.ts)}</span>
        <span class="new-log-cat" style="color:${cat.color}">${cat.icon} ${safeCat}</span>
        <span class="new-log-msg">${this._esc(e.msg)}</span>
      </div>`;
    }).join("") : `<div class="stub-body">No entries match${search ? ` "${this._esc(search)}"` : ""}${activeFilter !== "all" ? ` in ${activeFilter}` : ""}.</div>`;

    container.dataset.renderSig = renderSig;
    container.dataset.renderFilter = activeFilter;
    container.dataset.renderSearch = search;

    container.scrollTop = (filterChanged || nearTop) ? 0 : prevTop;
  }

  async _fetchDebugLog() {
    if (!this._hass) return;
    // Repeated navigation back into System Log must not pile up concurrent
    // duplicate requests — the in-flight one will render whatever it finds
    // in #newLogEntries when it resolves, same as any other stale-view
    // guard here (container lookup by id, below).
    if (this._debugLogFetchInFlight) return;
    this._debugLogFetchInFlight = true;
    try {
      const result = await this._hass.callWS({ type: "nova/get_debug_log" });
      const entries = result?.entries || [];
      this._debugLogEntries = entries;
      this._renderDebugLogEntries(entries);
    } catch (err) {
      // Fail-open: a background refresh failure must never discard rows
      // already on screen. Only show the error state when there's nothing
      // usable cached to fall back on (a genuine first-load failure).
      if (!this._debugLogEntries || !this._debugLogEntries.length) {
        const c = this.shadowRoot?.getElementById("newLogEntries");
        if (c) c.innerHTML = `<div class="new-log-entry-error" style="padding:12px">Error loading logs: ${this._esc(err)}</div>`;
      } else {
        console.warn("Nova: System Log refresh failed, keeping cached entries", err);
      }
    } finally {
      this._debugLogFetchInFlight = false;
    }
  }

  _wireLogs() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-logview").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logView = btn.getAttribute("data-view");
        this._render();
      });
    });
    if ((this._logView || "system") === "decisions") {
      const unjudgedBtn = root.getElementById("newDecUnjudged");
      if (unjudgedBtn) {
        unjudgedBtn.addEventListener("click", () => {
          this._decisionsUnjudgedOnly = !this._decisionsUnjudgedOnly;
          this._render();
        });
      }
      const loadMoreBtn = root.getElementById("newDecLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchDecisions(false));
      return;
    }
    if ((this._logView || "system") === "spoken_history") return;
    if ((this._logView || "system") === "actions") {
      const loadMoreBtn = root.getElementById("newActionLoadMore");
      if (loadMoreBtn) loadMoreBtn.addEventListener("click", () => this._fetchActions(false));
      return;
    }
    root.querySelectorAll(".new-log-filter").forEach(btn => {
      btn.addEventListener("click", () => {
        this._logFilter = btn.getAttribute("data-filter");
        root.querySelectorAll(".new-log-filter").forEach(b => b.classList.toggle("mode-chip-on", b === btn));
        this._fetchDebugLog();
      });
    });
    const logSearch = root.getElementById("newLogSearch");
    if (logSearch) {
      logSearch.addEventListener("input", (e) => {
        clearTimeout(this._logSearchDebounce);
        const val = e.currentTarget.value;
        this._logSearchDebounce = setTimeout(() => {
          this._logSearch = val;
          this._fetchDebugLog();
        }, 200);
      });
    }
  }

