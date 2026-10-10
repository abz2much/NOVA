  // ─── Chat ─────────────────────────────────────────────────────────────
  // Type to Nova from the panel. Each message goes through nova/chat, open to
  // any Home Assistant user, to Nova's own conversation agent. The server keeps
  // one thread per user, so the panel sends no conversation id. Unlocking,
  // opening and disarming need a tap on the user's phone. NEW CHAT calls
  // nova/chat_new, which clears the thread on the server. The messages shown
  // here live in the panel only and are gone when it is reloaded.
  _htmlChat() {
    return `
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">Chat</div>
            <div class="panel-meta"><button class="mode-chip" id="chatNew">NEW CHAT</button></div>
          </div>
          <div class="chat-log" id="chatLog"></div>
          <div class="chat-compose">
            <input class="cfg-field" id="chatInput" maxlength="1000" placeholder="Ask Nova something">
            <button class="mode-chip" id="chatSend">SEND</button>
          </div>
          <div class="toggle-desc" id="chatMsg"></div>
        </div>
    `;
  }

  _chatState() {
    if (!this._chat) this._chat = { turns: [], busy: false };
    return this._chat;
  }

  _renderChat() {
    const log = this.shadowRoot?.getElementById("chatLog");
    if (!log) return;
    const c = this._chatState();
    if (!c.turns.length) {
      this._setHtml(log, `<div class="stub-body">Ask about your home or tell Nova what to do. Unlocking, opening and disarming need a tap on your phone.</div>`);
      return;
    }
    this._setHtml(log, c.turns.map(t => `
        <div class="chat-turn ${t.role}${t.error ? " chat-error" : ""}">
          <span class="chat-who">${t.role === "user" ? this._t("You") : this._t("Nova")}</span>
          <span class="chat-text">${this._esc(t.text)}</span>
        </div>`).join("") + (c.busy ? `<div class="stub-body">${this._t("Nova is thinking…")}</div>` : ""));
    log.scrollTop = log.scrollHeight;
  }

  async _chatSend() {
    const root = this.shadowRoot;
    const input = root?.getElementById("chatInput");
    const msg = root?.getElementById("chatMsg");
    const c = this._chatState();
    const text = String(input?.value || "").trim();
    if (!this._hass || !text || c.busy) return;
    input.value = "";
    if (msg) this._setText(msg, "");
    c.turns.push({ role: "user", text });
    c.busy = true;
    this._renderChat();
    try {
      const res = await this._hass.callWS({ type: "nova/chat", text });
      if (res && res.ok) {
        c.turns.push({ role: "nova", text: res.reply });
      } else if (res && res.code === "rate_limited") {
        c.turns.push({ role: "nova", text: this._t("You're sending too fast, wait a moment"), error: true });
      } else {
        c.turns.push({ role: "nova", text: (res && res.error) || this._t("Could not reach Nova."), error: true });
      }
    } catch (err) {
      c.turns.push({ role: "nova", error: true, text: this._t("Could not reach Nova.") });
    }
    c.busy = false;
    this._renderChat();
  }

  _wireChat() {
    const root = this.shadowRoot;
    root.getElementById("chatSend")?.addEventListener("click", () => this._chatSend());
    root.getElementById("chatInput")?.addEventListener("keydown", e => {
      if (e.key === "Enter") this._chatSend();
    });
    root.getElementById("chatNew")?.addEventListener("click", async () => {
      this._chat = { turns: [], busy: false };
      this._renderChat();
      try {
        await this._hass.callWS({ type: "nova/chat_new" });
      } catch (err) {
        console.error("Nova: chat reset failed", err);
      }
    });
    this._renderChat();
    root.getElementById("chatInput")?.focus();
  }

