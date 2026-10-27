  // ─── Themes ───────────────────────────────────────────────────────────
  // Every colour the panel draws comes from here (8.74.0); the smoke test
  // fails on a colour written anywhere else.
  // 8.78.0: the themes are Oriel's (github.com/abz2much/oriel), built the
  // same way: each is two seed colours, a base and an accent, and
  // _themeBuild works out every token from them. Cards are glass over a lit
  // page, text is neutral, and the accent is kept for what it means: the
  // open tab, an "on" state, links and focus, the head. Solar and home keep
  // their own colours in every theme; the battery takes the accent. Every
  // text and status colour reaches 4.5:1 on the cards it sits on
  // (scripts/smoke_panel.js checks again).
  //   tokens: page is the page background; glass is the card fill (used
  //   faded) and shadow its shadow; surface and surface-2 are what a card
  //   and a cell inside it come to over the page. ember is the accent fill
  //   (open tab, buttons), on-accent the text on it, gold the accent as text
  //   (links, focus, "on"). dark-a and black are the dark panel (Energy),
  //   with on-dark text. c-* are the energy flow's four series.
  //   head: ramp is the canvas head's four steps from dim to bright; main,
  //   deep, pale and warm are the WebGL head's colours (0 to 1); eye is the
  //   bright centre of the canvas head's eyes, and fade the colour its glow
  //   fades out towards.
  static THEME_DEFAULT = "deep_teal";
  static THEMES = this._themeTable({
    deep_teal: { label: "Deep Teal", style: "dark", base: "#03313A", accent: "#8FFFE0" },
    midnight_signal: { label: "Midnight Signal", style: "dark", base: "#0B132B", accent: "#39FF88" },
    plum_dusk: { label: "Plum Dusk", style: "dark", base: "#1A0B2E", accent: "#FFB7A5" },
    cocoa_bark: { label: "Cocoa Bark", style: "dark", base: "#382A28", accent: "#C1A094" },
    forest_mint: { label: "Forest Mint", style: "dark", base: "#18251D", accent: "#B7FF72" },
    slate_harbour: { label: "Slate Harbour", style: "dark", base: "#11212D", accent: "#9BA8AB" },
  });

  // Each theme with its tokens and head colours worked out from its seeds.
  static _themeTable(defs) {
    const out = {};
    for (const [key, def] of Object.entries(defs)) out[key] = { ...def, ...this._themeBuild(def) };
    return out;
  }

  // Oriel's colour maths: colours are [r, g, b] (0 to 255).
  static _themeBuild(def) {
    const hex = h => { h = String(h).replace("#", ""); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); };
    const toHex = c => "#" + c.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
    const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
    const lin = v => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const lum = c => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
    const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
    // Step c towards `to` until it reaches `ratio` on every surface given.
    const reach = (c, surfaces, ratio, to) => {
      for (let t = 0; t <= 1; t += 0.02) { const x = mix(c, to, t); if (surfaces.every(s => contrast(x, s) >= ratio)) return x; }
      return to;
    };
    const W = [255, 255, 255], K = [0, 0, 0];
    const B = hex(def.base), A = hex(def.accent);
    // The fixed data and status hues, made for dark glass.
    const GOOD = hex("#8fdc9c"), WARN = hex("#f4c860"), BAD = hex("#ff9a86"), BAD_STRONG = hex("#ff6a58");
    const SOLAR = hex("#facc15"), HOME = hex("#60a5fa"), GRID = hex("#94a3b8"), INFO = hex("#7cb8ff");
    // The page: a gradient of the base, lit near the top.
    const page = [mix(B, W, 0.07), B, mix(B, K, 0.35)];
    const pageRep = mix(page[1], page[2], 0.3);
    // The lightest glass that keeps near-white text readable on cards and cells.
    let g = 0.4, glass, surface, cell, ink;
    for (; g >= 0.06; g -= 0.02) {
      glass = mix(B, W, g); surface = mix(pageRep, glass, 0.2); cell = mix(surface, glass, 0.34);
      ink = mix(W, B, 0.06);
      if ([surface, cell].every(s => contrast(mix(s, ink, 0.62), s) >= 4.6)) break;
    }
    const inkDim = reach(mix(cell, ink, 0.84), [cell, surface], 4.6, ink);
    const inkFaint = reach(mix(cell, ink, 0.58), [cell, surface], 4.6, ink);
    const darkA = mix(B, K, 0.45), black = mix(B, K, 0.8), onDark = mix(W, B, 0.03);
    const chip = mix(B, K, 0.45);
    const on = [W, mix(A, K, 0.85)].sort((x, y) => contrast(y, A) - contrast(x, A))[0];
    const good = reach(GOOD, [surface, cell], 4.6, W), warn = reach(WARN, [surface, cell], 4.6, W), bad = reach(BAD, [surface, cell], 4.6, W);
    const alarmBg = mix(chip, BAD_STRONG, 0.42);
    const tokens = {
      page: `radial-gradient(1100px 700px at 46% 0%,${toHex(mix(B, W, 0.18))}99,transparent 62%),radial-gradient(800px 600px at 92% 100%,${toHex(A)}12,transparent 70%),linear-gradient(160deg,${toHex(page[0])} 0%,${toHex(page[1])} 55%,${toHex(page[2])} 100%)`,
      bg: toHex(page[1]), glass: toHex(glass), shadow: toHex(mix(B, K, 0.75)),
      surface: toHex(surface), "surface-2": toHex(cell), "line-soft": toHex(mix(surface, glass, 0.9)),
      ink: toHex(ink), "ink-dim": toHex(inkDim), "ink-faint": toHex(inkFaint),
      ember: toHex(A), "on-accent": toHex(on), gold: toHex(reach(A, [surface, cell], 4.6, W)), "gold-pale": toHex(mix(A, W, 0.45)),
      warn: toHex(warn), good: toHex(good), "good-ink": toHex(reach(mix(GOOD, W, 0.25), [surface, cell], 4.6, W)),
      bad: toHex(bad), "bad-strong": toHex(BAD_STRONG), "bad-ink": toHex(reach(mix(BAD, W, 0.3), [surface, cell], 4.6, W)),
      "alarm-bg": toHex(alarmBg), "alarm-line": toHex(BAD_STRONG), "alarm-ink": toHex(reach(hex("#ffe4e8"), [alarmBg], 4.6, W)),
      info: toHex(reach(INFO, [surface, cell], 4.6, W)),
      "c-solar": toHex(SOLAR), "c-grid": toHex(GRID), "c-house": toHex(HOME), "c-battery": toHex(A),
      "dark-a": toHex(darkA), black: toHex(black), "on-dark": toHex(onDark),
      "orb-deep": toHex(mix(A, B, 0.6)), "media-bg": toHex(black), "glow-top": toHex(mix(B, W, 0.16)), "hero-end": toHex(mix(B, K, 0.2)),
    };
    const unit = c => c.map(v => +(v / 255).toFixed(3));
    const head = {
      ramp: [mix(A, B, 0.8), mix(A, B, 0.6), A, mix(A, W, 0.65)].map(c => c.map(Math.round)),
      main: unit(A), deep: unit(mix(A, B, 0.6)), pale: unit(mix(A, W, 0.65)), warm: unit(mix(A, W, 0.35)),
      eye: mix(A, W, 0.82).map(Math.round), fade: mix(B, K, 0.3).map(Math.round),
    };
    return { tokens, head };
  }

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
