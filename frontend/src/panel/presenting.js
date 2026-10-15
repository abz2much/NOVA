  // ─── Presenting ───────────────────────────────────────────────────────
  // When Nova answers an energy question it publishes a small card of the
  // real figures (nova/subscribe_presentation). The head turns towards the
  // card and the hero copy gives way to it; after the card expires, or when
  // it is tapped, the hero goes back to whatever _coreState() says. Asleep
  // wins: the head stays asleep, though the card can still be read.
  _presentStart() {
    const conn = this._hass?.connection;
    if (this._presentUnsub || this._presentPending || !conn || typeof conn.subscribeMessage !== "function") return;
    const token = this._presentToken = (this._presentToken || 0) + 1;
    this._presentPending = true;
    Promise.resolve(conn.subscribeMessage((card) => this._onPresentCard(card), { type: "nova/subscribe_presentation" }))
      .then((unsub) => {
        this._presentPending = false;
        if (this._presentToken === token) this._presentUnsub = unsub;
        else if (typeof unsub === "function") unsub();   // torn down while connecting
      })
      .catch(() => { this._presentPending = false; });   // the card is optional
  }

  _presentStop() {
    this._presentToken = (this._presentToken || 0) + 1;
    const unsub = this._presentUnsub;
    this._presentUnsub = null;
    if (typeof unsub === "function") { try { unsub(); } catch (_) { /* already closed */ } }
    if (this._presenting) this._presentEnd();
  }

  _onPresentCard(card) {
    if (!card || card.v !== 1 || card.kind !== "energy" || !card.data) return;
    if (card.id && card.id === this._presentId) return;   // the same card twice
    const ttl = Math.max(0, (Number(card.expires) - Number(card.created)) * 1000) || 30000;
    let left = Number(card.expires) * 1000 - Date.now();
    if (!(left > 0)) return;                 // already expired
    if (left > ttl) left = ttl;              // this clock runs behind the server's
    const html = this._presentHtml(card.data);
    if (!html) return;
    const root = this.shadowRoot, el = root.getElementById("presentCard");
    if (!el) return;
    this._presentId = card.id || null;
    this._setHtml(el, html);
    root.getElementById("heroStage")?.classList.add("presenting");
    this._presenting = true;
    if (this._coreState() !== "asleep") this._targetCoreState("presenting");
    if (this._presentTimer) clearTimeout(this._presentTimer);
    this._presentTimer = setTimeout(() => this._presentEnd(), left);
  }

  _presentEnd() {
    if (this._presentTimer) { clearTimeout(this._presentTimer); this._presentTimer = null; }
    if (!this._presenting) return;
    this._presenting = false;
    this.shadowRoot?.getElementById("heroStage")?.classList.remove("presenting");
    this._targetCoreState(this._coreState());
  }

  _presentNum(v, digits) {
    try {
      return Number(v).toLocaleString(this._resolveUiLang(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
    } catch (_) {
      return Number(v).toFixed(digits);
    }
  }

  // The card's markup, or "" when none of its figures can be shown. A
  // figure Nova could not read is null and its row is left out.
  _presentHtml(d) {
    const has = (v) => typeof v === "number" && isFinite(v);
    const n = (v, dp) => this._esc(this._presentNum(v, dp));
    const rows = [
      has(d.battery_pct) ? ["Battery", `${n(d.battery_pct, 0)}%`] : null,
      has(d.export_kwh) ? ["Sold to grid", `${n(d.export_kwh, 1)} kWh`] : null,
      has(d.home_kwh) ? ["Home used", `${n(d.home_kwh, 1)} kWh`] : null,
    ].filter(Boolean);
    const big = has(d.solar_today_kwh);
    if (!big && !rows.length && !has(d.solar_w)) return "";
    const curve = Array.isArray(d.curve) ? d.curve.filter(has) : [];
    let svg = "";
    if (curve.length >= 2) {
      const max = Math.max(...curve, 0.001);
      const pts = curve.map((v, i) => `${(i / (curve.length - 1) * 120).toFixed(1)},${(26 - v / max * 24).toFixed(1)}`).join(" ");
      svg = `<svg class="pc-curve" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--gold)" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
    }
    const now = has(d.solar_w) ? `<div class="pc-now"><span>Now</span> <span>· ${n(d.solar_w, 0)} W</span></div>` : "";
    return `${big ? `<div class="pc-cap">Solar made</div><div class="pc-big"><span class="pc-fig" data-unit="kWh">${n(d.solar_today_kwh, 1)}</span></div>` : ""}${now}${svg}`
      + (rows.length ? `<div class="pc-rows">${rows.map(([label, value]) => `<div class="pc-row"><span>${label}</span><b>${value}</b></div>`).join("")}</div>` : "");
  }
