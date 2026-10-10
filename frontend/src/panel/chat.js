  // ─── Chat ─────────────────────────────────────────────────────────────
  // Type to Nova from the panel. Each message goes through nova/chat (admin
  // only) to Nova's own conversation agent, so anything it does to the house
  // passes the same authorisation gate as Assist. The conversation lives in
  // memory only and is gone when the panel is reloaded.
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
    if (!this._chat) this._chat = { id: null, turns: [], busy: false };
    return this._chat;
  }

  _renderChat() {
    const log = this.shadowRoot?.getElementById("chatLog");
    if (!log) return;
    const c = this._chatState();
    if (!c.turns.length) {
      this._setHtml(log, `<div class="stub-body">Ask about your home or tell Nova what to do. Anything that unlocks, opens or disarms is still checked first, the same as in Assist.</div>`);
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
      const res = await this._hass.callWS({ type: "nova/chat", text, conversation_id: c.id });
      if (res && res.ok) {
        if (res.conversation_id) c.id = res.conversation_id;
        c.turns.push({ role: "nova", text: res.reply });
      } else {
        c.turns.push({ role: "nova", text: (res && res.error) || this._t("Could not reach Nova."), error: true });
      }
    } catch (err) {
      const denied = err && err.code === "unauthorized";
      c.turns.push({ role: "nova", error: true,
        text: denied ? this._t("Chat needs a Home Assistant administrator.") : this._t("Could not reach Nova.") });
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
    root.getElementById("chatNew")?.addEventListener("click", () => {
      this._chat = { id: null, turns: [], busy: false };
      this._renderChat();
    });
    this._renderChat();
    root.getElementById("chatInput")?.focus();
  }

