  // ─── Screen reader names and states ───────────────────────────────────
  // Runs after every redraw (_render, _setHtml) over what was just drawn.
  // Most cards share a few row shapes, so names and states are read from
  // what is already on screen instead of being written into every
  // template: a row's label names its fields, an "on" class becomes
  // aria-pressed, a panel title becomes a heading. The names are the
  // visible text, so a translation reaches them too.
  static A11Y_PRESSED = [
    [".toggle-btn", "on"],
    [".mode-chip[data-cfg-key]", "mode-chip-on"],
    [".mode-chip[data-lab-area]", "mode-chip-on"],
    [".mode-chip[data-mode]", "mode-chip-on"],
    [".mode-chip[data-agency]", "mode-chip-on"],
    [".new-cam-loc-chip", "mode-chip-on"],
    [".new-log-filter", "mode-chip-on"],
    [".new-logview", "mode-chip-on"],
    [".new-ilog-btn", "mode-chip-on"],
    [".settings-nav-btn", "active"],
    [".area-light-toggle", "on"],
  ];

  _a11yId(el) {
    if (!el.id) {
      this._a11ySeq = (this._a11ySeq || 0) + 1;
      el.id = `a11y-${this._a11ySeq}`;
    }
    return el.id;
  }

  _a11yNamed(el) {
    return el.hasAttribute("aria-label") || el.hasAttribute("aria-labelledby") || (el.labels && el.labels.length > 0);
  }

  _a11y(scope) {
    if (!scope || typeof scope.querySelectorAll !== "function") return;
    try {
      scope.querySelectorAll(".panel-title").forEach(t => {
        t.setAttribute("role", "heading");
        t.setAttribute("aria-level", "2");
      });

      // A row's own label names the fields and toggles in it.
      const ROWS = ".cfg-row, .pairing-row, .toggle-row, .new-model-row, .cred-row";
      scope.querySelectorAll(ROWS).forEach(row => {
        const label = Array.from(row.children).find(c => c.tagName === "LABEL"
          || ["pairing-label", "toggle-label", "model-label"].some(k => c.classList.contains(k)));
        if (!label || !label.textContent.trim()) return;
        const desc = row.classList.contains("toggle-row")
          ? Array.from(row.children).find(c => c.classList.contains("toggle-desc")) : null;
        row.querySelectorAll("select, input, textarea, .toggle-btn").forEach(field => {
          if (field.closest(ROWS) !== row || this._a11yNamed(field)) return;
          field.setAttribute("aria-labelledby", this._a11yId(label));
          if (desc && desc.textContent.trim()) field.setAttribute("aria-describedby", this._a11yId(desc));
        });
      });

      // A field in a row with no label of its own sits under a section
      // heading ("Exclude whole domains", "Try a sentence"): the heading,
      // which anyone can see, names it.
      scope.querySelectorAll(".cfg-row").forEach(row => {
        const fields = Array.from(row.querySelectorAll("input, textarea, select"))
          .filter(f => f.closest(ROWS) === row && !this._a11yNamed(f) && f.type !== "hidden" && f.type !== "file");
        if (!fields.length) return;
        let prev = row.previousElementSibling;
        while (prev && (prev.classList.contains("stub-body") || prev.classList.contains("toggle-desc"))) prev = prev.previousElementSibling;
        if (!prev || !prev.classList.contains("mode-bind-head") || !prev.textContent.trim()) return;
        fields.forEach(f => f.setAttribute("aria-labelledby", this._a11yId(prev)));
      });

      // A field with no label at all is named by its hint text.
      scope.querySelectorAll("input[placeholder], textarea[placeholder]").forEach(field => {
        if (!this._a11yNamed(field)) field.setAttribute("aria-label", field.getAttribute("placeholder"));   // i18n-ok: the placeholder, translated before this runs
      });

      for (const [sel, onClass] of NovaPanel.A11Y_PRESSED) {
        scope.querySelectorAll(sel).forEach(b => b.setAttribute("aria-pressed", String(b.classList.contains(onClass))));
      }

      scope.querySelectorAll(".nav-tab").forEach(t => {
        if (t.classList.contains("active")) t.setAttribute("aria-current", "page");
        else t.removeAttribute("aria-current");
      });

      // A chip's × names the item it takes off the list.
      scope.querySelectorAll(".new-pl-chip > button").forEach(b => {
        const item = Array.from(b.parentElement.childNodes)
          .filter(n => n !== b).map(n => n.textContent).join("").trim();
        if (item) b.setAttribute("aria-label", `${this._tx("Remove")} ${item}`);   // i18n-ok: built with _tx
      });
    } catch (_) { /* names are an extra: the panel works without them */ }
  }
