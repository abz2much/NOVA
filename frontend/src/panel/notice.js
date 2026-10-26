  // ─── Notices ──────────────────────────────────────────────────────────
  // One short message at the bottom of the panel: "Done." after an action,
  // or what went wrong after a failure. It is a polite live region, so a
  // screen reader says it without moving focus. The message is kept on the
  // element, not in the DOM, so a redraw puts it straight back.
  _notice(text, kind = "ok") {
    const ms = kind === "error" ? 8000 : 4000;
    this._noticeState = { text: String(text || ""), kind };
    if (this._noticeTimer) clearTimeout(this._noticeTimer);
    this._noticeTimer = setTimeout(() => { this._noticeState = null; this._drawNotice(); }, ms);
    this._drawNotice();
  }

  // A failed action: `where` is logged for the developer; the user sees the
  // server's own reason, or a plain sentence when it gave none.
  _fail(err, where) {
    console.error(`Nova: ${where || "action"} failed`, err);   // quiet: the notice below tells the user
    const reason = (err && (err.message || err.error || err.reason)) || this._tx("Something went wrong.");
    this._notice(this._t("That didn't work: {error}", { error: reason }), "error");
  }

  _drawNotice() {
    const el = this.shadowRoot?.getElementById("novaNotice");
    if (!el) return;
    const n = this._noticeState;
    el.classList.toggle("error", !!n && n.kind === "error");
    this._setText(el, n ? n.text : "");
    el.hidden = !n;
  }
