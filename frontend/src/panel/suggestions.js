  // ─── Suggestions ──────────────────────────────────────────────────────
  // Ported from Classic's own Suggestions tab. Data rides on the same
  // nova/get_panel_data payload the dashboard already polls (d.suggestions)
  // — no separate fetch. Approve/dismiss dim the card in place rather than
  // removing it or re-fetching, matching Classic's own lightweight pattern.
  static SUGGESTION_TYPE_LABEL = {
    time_routine: "Daily routine", sequence: "Action sequence",
    repeated_command: "Repeated command", temp_pref: "Temperature",
    presence: "Presence", numeric_trigger: "Sensor threshold",
  };

  _htmlSuggestions() {
    const sugs = this._data()?.suggestions || [];
    if (!sugs.length) {
      return `
        ${this._htmlAutomationInventory()}
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Learned Opportunities</div></div>
          <div class="stub-body">No suggestions right now. Nova proposes automations as it notices routines repeat — a light you turn on each evening, a scene after a button press, the heat when it's cold. As patterns build up, they'll appear here for you to review and approve. Nothing is ever created without your say-so.</div>
          <div class="mode-grid"><button class="mode-chip" id="sugRunAnalysis">Analyze Now</button></div>
          <div class="toggle-desc" id="sugAnalysisResult" style="margin-top:8px">See why nothing has qualified yet, or force a fresh pass over your history.</div>
        </div>
        ${this._htmlAutomationTrials()}`;
    }
    const rows = sugs.map(s => {
      const pct = Math.round((s.confidence || 0) * 100);
      const confColor = pct >= 80 ? "#5fbf7a" : pct >= 55 ? "var(--warn)" : "var(--ink-faint)";
      const label = NovaPanel.SUGGESTION_TYPE_LABEL[s.pattern_type] || "Learned pattern";
      const evidence = (s.evidence || []).map(e => `<li>${this._esc(e)}</li>`).join("");
      const entities = (s.entities || []).length
        ? `<div class="mode-grid">${(s.entities || []).map(e => `<span class="area-cap" style="width:auto;padding:3px 8px;font-family:var(--font-mono);font-size:10px">${this._esc(e)}</span>`).join("")}</div>`
        : "";
      const match = s.automation_match || {};
      // The backend names the automations it matched in match.matches.
      const matched = (match.matches || [])[0] || {};
      const matchName = matched.name || matched.entity_id || "an existing automation";
      let overlap = "";
      if (match.status === "possible_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Possible overlap with <b>${this._esc(matchName)}</b>. Review both before creating this automation.</div>`;
      } else if (match.status === "unknown_overlap") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ ${(match.matches || []).length ? `<b>${this._esc(matchName)}</b> may control the same device, but Nova cannot fully compare its blueprint or template.` : "Nova cannot fully compare this automation's template or blueprint with existing automations."}</div>`;
      } else if (match.status === "inventory_unavailable") {
        overlap = `<div class="stub-body" style="color:var(--warn)">⚠ Nova could not check existing automations. Review Home Assistant before creating this one.</div>`;
      }
      return `
        <div class="panel new-sug" data-sug-id="${s.id}">
          <div class="panel-head">
            <div class="panel-title">${this._esc(label)}</div>
            <div class="panel-meta" style="color:${confColor}">${pct}% confident</div>
          </div>
          ${s.why_headline ? `<div class="stub-body"><b>${this._esc(s.why_headline)}</b></div>` : ""}
          <div class="stub-body">${this._esc(s.description)}</div>
          ${overlap}
          ${evidence ? `<div class="mode-bind-head">What Nova observed</div><ul style="margin:0 0 10px;padding-left:18px;font-size:12px;color:var(--ink-dim);line-height:1.6">${evidence}</ul>` : ""}
          ${entities}
          <div class="cfg-row"><span class="toggle-desc">seen ${s.count || "?"}× in 30 days</span></div>
          <div class="mode-grid">
            <button class="mode-chip new-sug-approve">✓ Create automation</button>
            <button class="mode-chip new-sug-dismiss">✕ Dismiss</button>
            <button class="mode-chip new-sug-yaml-btn">⌄ See the automation</button>
          </div>
          <pre class="new-sug-yaml" hidden style="white-space:pre-wrap;font-family:var(--font-mono);font-size:10.5px;color:var(--ink-dim);background:var(--surface-2);border:1px solid var(--line-soft);border-radius:8px;padding:10px;margin-top:8px">${this._esc(s.yaml || "")}</pre>
        </div>`;
    }).join("");
    return `
      ${this._htmlAutomationInventory()}
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Learned Opportunities</div>
          <div class="panel-meta">${sugs.length} suggestion${sugs.length === 1 ? "" : "s"} to review</div>
        </div>
        <div class="stub-body">Automations Nova has learned from watching your routines. Review each — approve to create it in Home Assistant, or dismiss it. Nothing runs until you approve, and you can see the exact automation before deciding.</div>
      </div>
      ${rows}
      ${this._htmlAutomationTrials()}`;
  }

  _htmlAutomationInventory() {
    const result = this._automationInventory;
    if (result === null) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Couldn't load Home Assistant automations. Nova will not assume the list is empty.</div>
        </div>`;
    }
    if (result === undefined) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Loading the automation inventory…</div>
        </div>`;
    }
    const automations = result.automations || [];
    if (!result.available) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div></div>
          <div class="stub-body">Nova's automation inventory is unavailable. Suggestions will be marked for manual review instead of assuming nothing exists.</div>
        </div>`;
    }
    if (!automations.length) {
      return `
        <div class="panel">
          <div class="panel-head"><div class="panel-title">Existing Home Assistant Automations</div><div class="panel-meta">0 found</div></div>
          <div class="stub-body">Home Assistant currently reports no loaded automations.</div>
        </div>`;
    }
    const rows = automations.map(a => {
      const status = a.enabled ? "enabled" : "disabled";
      const scope = a.understanding === "full" ? "fully understood"
        : a.understanding === "partial" ? "blueprint · partial comparison"
        : "metadata only";
      const triggered = a.last_triggered
        ? new Date(a.last_triggered).toLocaleString() : "never";
      const origin = a.origin === "nova" ? "created by Nova" : "existing";
      return `
        <div class="cfg-row">
          <label>${this._esc(a.name || a.entity_id)}</label>
          <span class="toggle-desc">${this._esc(status)} · ${this._esc(origin)} · ${this._esc(scope)} · last triggered ${this._esc(triggered)}</span>
        </div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Existing Home Assistant Automations</div>
          <div class="panel-meta">${automations.length} loaded</div>
        </div>
        <div class="stub-body">Nova uses this read-only inventory to avoid relearning routines Home Assistant already handles. It refreshes at startup and whenever automations are reloaded.</div>
        ${rows}
      </div>`;
  }

  // ─── Automation probation (Phase 3) ──────────────────────────────────────
  // Installing a suggestion only means it was ACCEPTED — this section shows
  // what's actually been observed running since, entirely separate from that
  // acceptance. Run counts come from Home Assistant's own automation_triggered
  // event; "Working"/"Needs adjustment" is manual feedback only, never inferred.

  _htmlAutomationTrials() {
    const trials = this._automationTrials;
    if (trials === null) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">Couldn't load installed automations.</div>
        </div>`;
    }
    if (!trials || !trials.length) {
      return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Created by Nova</div>
          </div>
          <div class="stub-body">No tracked Nova automations yet. Automations installed from new suggestions will appear here.</div>
        </div>`;
    }
    const rows = trials.map(t => {
      const when = t.last_run ? new Date(t.last_run * 1000).toLocaleString() : "never";
      const outcome = t.manual_outcome
        ? `<span class="toggle-desc">Feedback: ${this._esc(t.manual_outcome === "working" ? "Working" : "Needs adjustment")}</span>`
        : "";
      return `
        <div class="cfg-row">
          <label>${this._esc(t.automation_id)}</label>
          <span class="toggle-desc">ran ${t.run_count || 0}× · last ${this._esc(when)}</span>
        </div>
        <div class="mode-grid" data-trial-id="${t.id}">
          <button class="mode-chip new-trial-fb" data-verdict="working">WORKING</button>
          <button class="mode-chip new-trial-fb" data-verdict="needs_adjustment">NEEDS ADJUSTMENT</button>
        </div>
        <div class="cfg-row">${outcome}</div>`;
    }).join("");
    return `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">Created by Nova</div>
          <div class="panel-meta">${trials.length} tracked</div>
        </div>
        <div class="stub-body">Installing an automation means you accepted the suggestion — it isn't proof the automation works. This shows what's actually been observed running; "Working" and "Needs adjustment" are your own call, not Nova's.</div>
        ${rows}
      </div>`;
  }

  async _fetchAutomationTrials() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/list_automation_trials" });
      this._automationTrials = result.trials || [];
    } catch (_) { this._automationTrials = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _fetchAutomationInventory() {
    if (!this._hass) return;
    try {
      this._automationInventory = await this._hass.callWS({
        type: "nova/list_automation_inventory",
      });
    } catch (_) { this._automationInventory = null; }
    if (this._currentTab === "suggestions") this._render();
  }

  async _submitAutomationTrialFeedback(trialId, verdict) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({ type: "nova/automation_trial_feedback", trial_id: trialId, verdict });
      this._fetchAutomationTrials();
    } catch (err) {
      console.error("Nova: automation trial feedback failed", err);
    }
  }

  _wireSuggestions() {
    const root = this.shadowRoot;
    root.querySelectorAll(".new-sug").forEach(card => {
      const sid = parseInt(card.getAttribute("data-sug-id"), 10);
      const act = async (action) => {
        if (!this._hass || isNaN(sid)) return;
        const buttons = card.querySelectorAll("button");
        buttons.forEach(b => b.disabled = true);
        let res = null;
        try {
          res = await this._hass.callWS({ type: "nova/suggestion_action", suggestion_id: sid, action });
        } catch (err) {
          console.error(`Nova: suggestion ${action} failed`, err);
        }
        // Only a settled suggestion (installed, acknowledged, covered or
        // dismissed) greys out. A failed install stays pending on the
        // backend, so its buttons come back with the reason shown.
        if (res && res.ok) {
          card.style.opacity = "0.35";
          return;
        }
        buttons.forEach(b => b.disabled = false);
        let note = card.querySelector(".new-sug-status");
        if (!note) {
          note = document.createElement("div");
          note.className = "stub-body new-sug-status";
          note.style.color = "var(--warn)";
          card.querySelector(".new-sug-approve")?.parentElement?.before(note);
        }
        note.textContent = `⚠ ${(res && res.reason) || `Could not ${action} this suggestion. Try again.`}`;
      };
      card.querySelector(".new-sug-approve")?.addEventListener("click", () => act("approve"));
      card.querySelector(".new-sug-dismiss")?.addEventListener("click", () => act("dismiss"));
      card.querySelector(".new-sug-yaml-btn")?.addEventListener("click", () => {
        const pre = card.querySelector(".new-sug-yaml");
        if (pre) pre.hidden = !pre.hidden;
      });
    });
    root.querySelectorAll(".new-trial-fb").forEach(btn => {
      btn.addEventListener("click", () => {
        const group = btn.closest("[data-trial-id]");
        const trialId = parseInt(group?.getAttribute("data-trial-id"), 10);
        if (!isNaN(trialId)) this._submitAutomationTrialFeedback(trialId, btn.getAttribute("data-verdict"));
      });
    });
  }

