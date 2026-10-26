  // ─── Command Center dock (8.77.0) ──────────────────────────────────────
  // Five buttons at the bottom of the Command Center. Each one but Home
  // opens a panel that slides up over the lower part of the hero, so the
  // head stays in view; Home, Esc or Close shut it. The boxes that used to
  // be stacked under the hero live in these panels with their ids
  // unchanged, so _renderData fills them as before. Nova's panel is the
  // activity feed in plain sentences, written here from each row's kind
  // (nova/get_activity_log), so it costs no tokens.
  static DOCK = [["home", "Home"], ["nova", "Nova"], ["areas", "Areas"], ["goals", "Goals"], ["more", "More"]];
  static DOCK_ICONS = {
    home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    nova: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
    areas: '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/>',
    goals: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
  };
  // More's sections: [key, title, id of the panel that says whether it shows].
  static MORE_SECTIONS = [["mode", "Operational Mode", ""], ["actions", "Quick Actions", ""], ["core", "Cognitive Core", ""],
    ["cameras", "Camera Watch", "cameraPanel"], ["muted", "Muted", "mutesPanel"]];
  // What counts towards Nova's badge: things Nova did or said, not changes
  // it only noted.
  static NOVA_NEWS = ["spoken", "pushed", "verify", "held", "goal", "followup"];

  _dockHtml() {
    const head = (title, metaId) => `
          <div class="dock-sheet-head"><div class="panel-title">${title}</div>${metaId ? `<div class="panel-meta" id="${metaId}">—</div>` : ""}
            <button class="mode-chip dock-close">Close</button></div>`;
    const sheet = (key, title, body, metaId) => `
        <section class="dock-sheet" data-sheet="${key}" id="dockSheet-${key}" role="region" aria-label="${title}" tabindex="-1" hidden>${head(title, metaId)}${body}
        </section>`;
    const moreSec = {
      mode: `
            <div class="panel" id="operationalModePanel">
              <div class="panel-head"><div class="panel-title">Operational Mode</div></div>
              <div id="operationalModeBody"></div>
            </div>`,
      actions: `
            <div class="panel">
              <div class="panel-head"><div class="panel-title">Quick Actions</div></div>
              <div class="mode-grid">
                <button class="mode-chip" data-svc="nova.briefing">Briefing</button>
                <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":30}'>Nap 30m</button>
                <button class="mode-chip" data-svc="nova.nap" data-svc-data='{"duration_minutes":60}'>Nap 60m</button>
                <button class="mode-chip" data-svc="nova.unshush">Unshush All</button>
                <button class="mode-chip" data-svc="nova.observer_status">Status Dump</button>
                <button class="mode-chip" id="qaRunAnalysis">Analyze Now</button>
              </div>
              <div class="toggle-desc" id="qaAnalysisResult" style="margin-top:8px"></div>
            </div>`,
      core: `
            <div class="panel">
              <div class="panel-head">
                <div class="panel-title">Cognitive Core</div>
                <div class="panel-meta" id="cognitiveState">—</div>
              </div>
              <div class="metric-grid" id="cognitiveMetrics"></div>
              <div class="toggle-desc" id="cognitiveAnalysis"></div>
            </div>`,
      cameras: `
            <div class="panel camera-panel" id="cameraPanel" hidden>
              <div class="camera-head-row">
                <div>
                  <div class="panel-title" style="margin-bottom:5px">Camera Watch</div>
                  <div class="camera-note">Authenticated snapshots from cameras available to Home Assistant.</div>
                </div>
                <button class="camera-toggle" id="camToggle">Show cameras ▾</button>
              </div>
              <div class="camera-strip" id="camStrip"></div>
            </div>`,
      muted: `
            <div class="panel" id="mutesPanel" hidden>
              <div class="panel-head">
                <div class="panel-title">Muted</div>
                <div class="panel-meta" id="mutesMeta">SAVED</div>
              </div>
              <div id="mutesBody"></div>
            </div>`,
    };
    const more = `
          <div class="more-list">${NovaPanel.MORE_SECTIONS.map(([k, t]) => `
            <button class="more-item" data-more="${k}">${t}</button>`).join("")}
          </div>${NovaPanel.MORE_SECTIONS.map(([k]) => `
          <div class="more-sec" data-more-sec="${k}" hidden>
            <button class="mode-chip more-back">Back to the list</button>${moreSec[k]}
          </div>`).join("")}`;
    const nova = `
          <div class="nova-feed" id="novaFeed" role="log"></div>
          <div class="ask-nova">
            <input class="cfg-field" id="askNovaInput" maxlength="1000" placeholder="Ask Nova something" aria-label="Ask Nova">
            <button class="mode-chip" id="askNovaSend">Send</button>
          </div>
          <div class="ask-reply" id="askNovaReply" aria-live="polite"></div>
          <div class="nova-foot"><button class="mode-chip" id="novaSeeAll">See everything</button></div>`;
    const areas = `
          <div class="areas-grid" id="areasGrid"></div>`;
    const goals = `
          <div class="goal-list" id="goalList"></div>
          <div class="goal-create">
            <label class="field" for="goalOutcome"><span>Outcome Nova should work toward</span>
              <input class="cfg-field" id="goalOutcome" maxlength="500"></label>
            <button class="mode-chip" id="goalCreate">Add goal</button>
          </div>
          <div class="toggle-desc" id="goalResult"></div>`;
    const icon = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${NovaPanel.DOCK_ICONS[k]}</svg>`;
    return `
        <div class="dock-wrap">${sheet("nova", "Nova", nova)}${sheet("areas", "Areas", areas, "areasMeta")}${sheet("goals", "Goals", goals, "goalsMeta")}${sheet("more", "More", more)}
          <nav class="dock" aria-label="Command Center">${NovaPanel.DOCK.map(([k, label]) => `
            <button class="dock-btn" data-dock="${k}"${k === "home" ? "" : ` aria-expanded="false" aria-controls="dockSheet-${k}"`}>${icon(k)}<span class="dock-label">${label}</span>${k === "nova" ? '<span class="dock-badge" hidden></span>' : ""}</button>`).join("")}
          </nav>
        </div>`;
  }

  // After each render: wire the dock and put back the panel that was open.
  _dockWire() {
    const root = this.shadowRoot;
    if (!root?.querySelector(".dock")) return;
    root.querySelectorAll(".dock-btn").forEach(b => b.addEventListener("click", () => {
      const k = b.getAttribute("data-dock");
      this._dockOpenSheet(k === "home" || k === this._dockOpen ? null : k, true);
    }));
    root.querySelectorAll(".dock-sheet").forEach(s => s.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.stopPropagation(); this._dockOpenSheet(null, true); }
    }));
    root.querySelectorAll(".dock-close").forEach(b => b.addEventListener("click", () => this._dockOpenSheet(null, true)));
    root.querySelectorAll(".more-item").forEach(b => b.addEventListener("click", () => this._dockMore(b.getAttribute("data-more"))));
    root.querySelectorAll(".more-back").forEach(b => b.addEventListener("click", () => this._dockMore(null)));
    root.getElementById("askNovaSend")?.addEventListener("click", () => this._askNova());
    root.getElementById("askNovaInput")?.addEventListener("keydown", e => { if (e.key === "Enter") this._askNova(); });
    root.getElementById("novaSeeAll")?.addEventListener("click", () => root.querySelector('.nav-tab[data-tab="logs"]')?.click());
    this._askNovaDraw();
    if (this._dockOpen) this._dockOpenSheet(this._dockOpen, false);
    this._dockMore(this._dockMoreSec || null);
  }

  // Open one panel (or none). `user` is true for a tap or a key: focus
  // moves into the panel, or back to its button when it closes.
  _dockOpenSheet(key, user) {
    const root = this.shadowRoot;
    const was = this._dockOpen;
    this._dockOpen = key || null;
    if (key === "nova") this._novaMarkSeen();
    root.querySelectorAll(".dock-btn[aria-expanded]").forEach(b =>
      b.setAttribute("aria-expanded", String(b.getAttribute("data-dock") === key)));
    root.querySelectorAll(".dock-sheet").forEach(s => {
      const open = s.getAttribute("data-sheet") === key;
      clearTimeout(s._dockTimer);
      if (open) {
        s.hidden = false;
        void s.offsetWidth;   // start the slide from below
        s.classList.add("dock-open");
      } else if (!s.hidden) {
        s.classList.remove("dock-open");
        // Slide out only when no other panel takes its place.
        if (this._reduceMotion || !user || key) s.hidden = true;
        else s._dockTimer = setTimeout(() => { s.hidden = true; }, 300);
      }
    });
    if (!user) return;
    if (key) root.querySelector(`.dock-sheet[data-sheet="${key}"]`)?.focus();
    else if (was) root.querySelector(`.dock-btn[data-dock="${was}"]`)?.focus();
  }

  // Show one of More's sections, or the list when key is null. Cameras and
  // Muted are listed only while their panels have something to show.
  _dockMore(key) {
    const root = this.shadowRoot;
    const sheet = root?.querySelector('.dock-sheet[data-sheet="more"]');
    if (!sheet) return;
    this._dockMoreSec = key || null;
    sheet.querySelector(".more-list").hidden = !!key;
    sheet.querySelectorAll(".more-sec").forEach(s => { s.hidden = s.getAttribute("data-more-sec") !== key; });
    this._dockSync();
  }

  _dockSync() {
    const root = this.shadowRoot;
    NovaPanel.MORE_SECTIONS.forEach(([k, , panelId]) => {
      const item = root?.querySelector(`.more-item[data-more="${k}"]`);
      if (item && panelId) item.hidden = !!root.getElementById(panelId)?.hidden;
    });
  }

  // ── Nova's feed in plain sentences ──
  // One line per row, newest first; a run of changes Nova let pass becomes
  // one quiet line. Rows from before 8.77.0 have no kind and show as written.
  _novaLines() {
    const lines = [];
    for (const e of this._activityData || []) {
      const msg = String(e.msg || "");
      const kind = e.kind || "note";
      const last = lines[lines.length - 1];
      if (kind === "passed") {
        if (last && last.kind === "passed") { last.count += 1; continue; }
        lines.push({ ts: e.ts, kind, count: 1 });
        continue;
      }
      let text = msg;
      if (kind === "spoken") text = this._t("I told you: {msg}", { msg });
      else if (kind === "pushed") text = this._t("I sent to your phone: {msg}", { msg });
      else if (kind === "held") text = this._t("I kept quiet about: {msg}", { msg });
      else if (kind === "checked") text = this._t("I looked into {change}.", { change: msg.replace(/\s*— flagged for reasoning$/, "") });
      else if (kind === "goal") {
        const m = msg.match(/^Goal #\d+ '(.+?)' engaged \(run \d+\): ([\s\S]*)$/);
        if (m) text = this._t("I worked on your goal “{title}”: {result}", { title: m[1], result: m[2] });
      } else if (kind === "followup") {
        const m = msg.match(/^Follow-up #\d+ ran: ([\s\S]+?) → ([\s\S]*)$/);
        if (m) text = this._t("I followed up on “{what}”: {answer}", { what: m[1], answer: m[2] });
      }
      lines.push({ ts: e.ts, kind, text, key: `${e.ts}|${kind}|${msg}` });
    }
    for (const l of lines) {
      if (l.kind !== "passed") continue;
      l.text = l.count === 1 ? this._t("I noticed 1 small change and let it pass.")
        : this._t("I noticed {count} small changes and let them pass.", { count: l.count });
    }
    return lines;
  }

  _novaRender() {
    const root = this.shadowRoot;
    const feed = root?.getElementById("novaFeed");
    const lines = this._novaLines();
    if (feed) {
      this._setHtml(feed, lines.length ? lines.map(l => `
            <div class="nova-row${l.kind === "passed" ? " nova-quiet" : ""}">
              <span class="nova-time">${this._esc(l.ts || "")}</span><span class="nova-dot nova-dot-${this._esc(l.kind)}" aria-hidden="true"></span>
              <span class="nova-text">${this._esc(l.text)}</span>
            </div>`).join("") : `<div class="stub-body">${this._esc(this._t("No activity yet."))}</div>`);
    }
    this._dockSync();
    // The badge: things Nova did since the panel was last opened. It waits
    // for the first activity fetch, so what is already there is not "new".
    if (!Array.isArray(this._activityData)) return;
    const news = lines.filter(l => NovaPanel.NOVA_NEWS.includes(l.kind));
    if (this._novaSeen === undefined || (this._novaSeen && !news.some(l => l.key === this._novaSeen))) {
      this._novaSeen = news[0]?.key || null;
    }
    if (this._dockOpen === "nova") this._novaSeen = news[0]?.key || null;
    let count = 0;
    for (const l of news) { if (l.key === this._novaSeen) break; count++; }
    const badge = root?.querySelector('.dock-btn[data-dock="nova"] .dock-badge');
    if (badge) {
      badge.hidden = count === 0;
      this._setText(badge, count === 0 ? "" : count > 9 ? "9+" : String(count));
    }
  }

  _novaMarkSeen() {
    const news = this._novaLines().filter(l => NovaPanel.NOVA_NEWS.includes(l.kind));
    this._novaSeen = news[0]?.key || null;
    const badge = this.shadowRoot?.querySelector('.dock-btn[data-dock="nova"] .dock-badge');
    if (badge) badge.hidden = true;
  }

  // ── Ask Nova: the same thread as the Chat tab ──
  async _askNova() {
    const input = this.shadowRoot?.getElementById("askNovaInput");
    const text = String(input?.value || "").trim();
    if (!this._hass || !text || this._chatState().busy) return;
    input.value = "";
    this._askLast = { q: text, a: null };
    this._askNovaDraw();
    const turn = await this._chatAsk(text, () => {});
    this._askLast = { q: text, a: turn };
    this._askNovaDraw();
  }

  _askNovaDraw() {
    const el = this.shadowRoot?.getElementById("askNovaReply");
    if (!el) return;
    const last = this._askLast;
    if (!last) { this._setHtml(el, ""); return; }
    this._setHtml(el, `
            <div class="ask-q">${this._esc(last.q)}</div>
            <div class="ask-a${last.a?.error ? " chat-error" : ""}">${last.a ? this._esc(last.a.text) : this._esc(this._t("Nova is thinking…"))}</div>
            ${last.a ? `<button class="mode-chip" id="askNovaChat">Open chat</button>` : ""}`);
    el.querySelector("#askNovaChat")?.addEventListener("click", () =>
      this.shadowRoot.querySelector('.nav-tab[data-tab="chat"]')?.click());
  }
