  // ─── Themes ───────────────────────────────────────────────────────────
  // Every colour the panel draws comes from here (8.74.0); the smoke test
  // fails on a colour written anywhere else. A theme is a set of CSS colour
  // tokens plus the hologram head's colours. Each was worked out from one of
  // the palettes in Abi's notes, and every text and status colour reaches
  // 4.5:1 on the cards it sits on (scripts/smoke_panel.js checks again).
  // Classic is the look from before themes, value for value.
  //   tokens: ember is the main accent (active tabs, buttons), gold the
  //   highlight (numbers, links); good, warn and bad are status colours, and
  //   their -ink forms are for text. c-* are the energy flow's four series.
  //   head: ramp is the canvas head's four steps from dim to bright; main,
  //   deep, pale and warm are the WebGL head's colours (0 to 1); eye is the
  //   bright centre of the canvas head's eyes, and fade the colour its glow
  //   fades out towards.
  static THEME_DEFAULT = "deep_teal";
  static THEMES = {
    deep_teal: {
      label: "Deep Teal",
      tokens: { "bg": "#011b21", "surface": "#082c33", "surface-2": "#0e3b43", "line-soft": "#1b5059", "ink": "#eefffa", "ink-dim": "#a2bcbb", "ink-faint": "#87a5a5", "ember": "#8fffe0", "gold": "#8fffe0", "gold-pale": "#d2fff3", "warn": "#fbbf24", "good": "#a3e635", "good-ink": "#bfee72", "bad": "#fb7185", "bad-strong": "#f43f5e", "bad-ink": "#fda4af", "alarm-bg": "#7f1d2d", "alarm-line": "#f87185", "alarm-ink": "#ffe4e8", "on-accent": "#000000", "info": "#60a5fa", "c-solar": "#facc15", "c-grid": "#60a5fa", "c-house": "#8fffe0", "c-battery": "#c084fc", "orb-deep": "#2c5f5a", "media-bg": "#000e10", "glow-top": "#1b4443", "hero-end": "#04242a" },
      head: { ramp: [[29,73,71],[51,107,100],[143,255,224],[216,255,244]], main: [0.561,1.0,0.878], deep: [0.2,0.42,0.392], pale: [0.824,1.0,0.953], warm: [0.757,1.0,0.933], eye: [233,255,249], fade: [1,27,33] },
    },
    twilight_denim: {
      label: "Twilight Denim",
      tokens: { "bg": "#111522", "surface": "#1e263b", "surface-2": "#273150", "line-soft": "#303d59", "ink": "#dce3ef", "ink-dim": "#aab2c4", "ink-faint": "#949cb0", "ember": "#98a9c4", "gold": "#98a9c4", "gold-pale": "#d6dde7", "warn": "#fbbf24", "good": "#8bef92", "good-ink": "#aef4b3", "bad": "#fb7185", "bad-strong": "#f43f5e", "bad-ink": "#fda4af", "alarm-bg": "#7f1d2d", "alarm-line": "#f87185", "alarm-ink": "#ffe4e8", "on-accent": "#000000", "info": "#60a5fa", "c-solar": "#facc15", "c-grid": "#60a5fa", "c-house": "#98a9c4", "c-battery": "#c084fc", "orb-deep": "#3a4153", "media-bg": "#080a11", "glow-top": "#29303f", "hero-end": "#181e2f" },
      head: { ramp: [[44,51,66],[64,73,91],[152,169,196],[219,225,234]], main: [0.596,0.663,0.769], deep: [0.251,0.286,0.357], pale: [0.839,0.867,0.906], warm: [0.776,0.816,0.875], eye: [234,238,243], fade: [17,21,34] },
    },
    pine_mist: {
      label: "Pine Mist",
      tokens: { "bg": "#051f20", "surface": "#0b2b26", "surface-2": "#123630", "line-soft": "#235347", "ink": "#daf1de", "ink-dim": "#9cb7a8", "ink-faint": "#84a093", "ember": "#8eb69b", "gold": "#8eb69b", "gold-pale": "#d2e2d7", "warn": "#fbbf24", "good": "#a3e635", "good-ink": "#bfee72", "bad": "#fb7185", "bad-strong": "#f43f5e", "bad-ink": "#fda4af", "alarm-bg": "#7f1d2d", "alarm-line": "#f87185", "alarm-ink": "#ffe4e8", "on-accent": "#000000", "info": "#60a5fa", "c-solar": "#facc15", "c-grid": "#60a5fa", "c-house": "#8eb69b", "c-battery": "#c084fc", "orb-deep": "#2e4c45", "media-bg": "#021010", "glow-top": "#1e3a36", "hero-end": "#082523" },
      head: { ramp: [[32,61,57],[53,84,75],[142,182,155],[215,229,220]], main: [0.557,0.714,0.608], deep: [0.208,0.329,0.294], pale: [0.824,0.886,0.843], warm: [0.757,0.843,0.784], eye: [232,240,235], fade: [5,31,32] },
    },
    smoked_lilac: {
      label: "Smoked Lilac",
      tokens: { "bg": "#17151c", "surface": "#292531", "surface-2": "#37323f", "line-soft": "#45404f", "ink": "#e2dde7", "ink-dim": "#b8b3be", "ink-faint": "#a19ca7", "ember": "#b4a6d6", "gold": "#b4a6d6", "gold-pale": "#e1dbef", "warn": "#fbbf24", "good": "#8bef92", "good-ink": "#aef4b3", "bad": "#fb7185", "bad-strong": "#f43f5e", "bad-ink": "#fda4af", "alarm-bg": "#7f1d2d", "alarm-line": "#f87185", "alarm-ink": "#ffe4e8", "on-accent": "#000000", "info": "#60a5fa", "c-solar": "#facc15", "c-grid": "#60a5fa", "c-house": "#b4a6d6", "c-battery": "#2dd4bf", "orb-deep": "#464154", "media-bg": "#0c0a0e", "glow-top": "#332f3d", "hero-end": "#201d27" },
      head: { ramp: [[54,50,65],[78,72,93],[180,166,214],[229,224,241]], main: [0.706,0.651,0.839], deep: [0.306,0.282,0.365], pale: [0.882,0.859,0.937], warm: [0.839,0.808,0.91], eye: [240,237,247], fade: [23,21,28] },
    },
    classic: {
      label: "Classic",
      tokens: { "bg": "#15110d", "surface": "#1e1712", "surface-2": "#2a2119", "line-soft": "#33291f", "ink": "#f3ece1", "ink-dim": "#b6a897", "ink-faint": "#988a78", "ember": "#e2542f", "gold": "#f4b860", "gold-pale": "#ffe3ad", "warn": "#e8b23d", "good": "#5fbf7a", "good-ink": "#8fdba8", "bad": "#ff6b81", "bad-strong": "#ff5a5a", "bad-ink": "#ff9a9a", "alarm-bg": "#7d2028", "alarm-line": "#d95b65", "alarm-ink": "#ffd7d7", "on-accent": "#1e0d06", "info": "#6ea8ff", "c-solar": "#f4b860", "c-grid": "#6ea8ff", "c-house": "#e2542f", "c-battery": "#2aa198", "orb-deep": "#7a2513", "media-bg": "#080706", "glow-top": "#2a1c11", "hero-end": "#19140f" },
      head: { ramp: [[126,36,18],[226,84,47],[244,184,96],[255,231,189]], main: [0.957,0.722,0.376], deep: [0.62,0.27,0.12], pale: [1.0,0.89,0.68], warm: [0.886,0.33,0.18], eye: [255,244,222], fade: [20,12,8] },
    },
  };
  // The same in every theme: floor plan door kinds, weather warning levels
  // (yellow, orange, red as everywhere else) and the log kinds.
  static THEME_SHARED = { "fp-dormer": "#b06aff", "fp-interior": "#5a7a8a", "fp-cellar": "#c98a2a", "fp-cased": "#78b9d7", "hazard-yellow": "#e6b800", "hazard-orange": "#f08c00", "hazard-red": "#e03131", "hazard-none": "#888888", "log-conv": "#5fd0e0", "log-reply": "#4fb8ff", "log-local": "#5fbf7a", "log-learn": "#8fd15c", "log-auto": "#ffb454", "log-mode": "#c9a0ff", "log-config": "#9d8cff", "log-classify": "#9d8cff", "log-camera": "#5fbf7a", "log-energy": "#ffcf6a", "log-bio": "#ff8fc7", "log-offer": "#ffd27a", "log-safety": "#ff8a8a", "log-error": "#ff6b81", "log-voice": "#7fe0c4" };

  _themeKey() {
    const key = this._liveData?.config?.ui_theme;
    return Object.prototype.hasOwnProperty.call(NovaPanel.THEMES, key) ? key : NovaPanel.THEME_DEFAULT;
  }
  _theme() { return NovaPanel.THEMES[this._themeKey()]; }
  // The chosen theme's tokens as CSS declarations, for the :host rule.
  _themeTokens() {
    this._themeShown = this._themeKey();
    const all = { ...NovaPanel.THEME_SHARED, ...this._theme().tokens };
    return Object.entries(all).map(([k, v]) => `--${k}:${v};`).join("");
  }
  // Puts a newly chosen theme on the open panel by rewriting its stylesheet,
  // not the page, so the head and the Command Center keep running.
  _applyTheme() {
    const style = this.shadowRoot?.querySelector("style");
    if (style && this._themeShown !== this._themeKey()) style.textContent = this._css();   // i18n-ok: a stylesheet, not words
  }
