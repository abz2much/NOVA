/*
 * Nova Command Center Panel.
 * v7.123.0
 *
 * Started life as "Command Center" — a genuinely separate implementation
 * from the original Classic UI, built with full creative freedom over
 * ongoing maintenance cost. Classic reached feature parity and was
 * deleted in v7.101.30; this is now Nova's one and only dashboard,
 * registered directly as "nova-panel" via panel_custom (no more style
 * switcher, no more dynamic import — this file loads on its own).
 *
 * Layout: Command Center + Settings, reorganized around what you're
 * trying to do rather than which subsystem it touches (General, Voice &
 * Speakers, Awareness & Safety, Learning & Memory, Cameras, Home &
 * Extras), plus a search box across every setting. All 27 Settings cards
 * are real. Residence, Intrusion, Suggestions, Logs, and Memory are all
 * full nav tabs here.
 *
 * Design: an animated "stellar core" (Nova = a star's sudden brightening)
 * replaces a camera feed as the dashboard's visual anchor — it works
 * identically whether someone has zero cameras or twelve, and doesn't
 * repeat the cyan sci-fi-HUD look this project's name already evokes.
 * Camera Watch becomes an optional, collapsed card instead, since not
 * every camera integration (e.g. Eufy) streams live into Home Assistant.
 * Approved from a static mockup (nova-command-center-mockup.html) before
 * this real, live-data build.
 */
class NovaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._liveData = null;
    this._activityData = null;
    this._renderedOnce = false;
    this._fetchInterval = null;
    this._lastActivitySig = null;
    this._flareUntil = 0;
    this._animHandle = null;
    this._particles = [];
    this._current = { speed: 0.20, count: 70, radiusMul: 1, glow: 0.55, hot: 0.35, flare: 0.05 };
    this._camOpen = false;
    this._cameraImages = {};
    this._cameraLoading = {};
    this._cameraDiagnostics = {};
    this._cameraInterval = null;
    this._cognitive = null;
    this._currentTab = "dashboard"; // "dashboard" | "settings" | "logs" | "memory" | "intrusion" | "suggestions"
    this._logFilter = "all";
    this._logSearch = "";
    this._settingsSection = "general";
    this._settingsSearch = "";
    this._uiStrings = null;
    this._uiLangLoaded = null;
    this._uiLangRequest = 0;
  }

  // ─── HA property contract — same shape as Classic's, see nova-panel.js ──
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    if (first) {
      this._render();
      this._startIntervals();
      this._loadUiStrings();
    }
  }
  get hass() { return this._hass; }
  set panel(panel) { this._config = panel?.config || {}; }
  set narrow(narrow) { this._narrow = narrow; }
  set route(route) { this._route = route; }

  connectedCallback() {
    if (!window.__novaBannerLogged) {
      window.__novaBannerLogged = true;
      console.log("%c Nova Panel %c v7.123.0 ",
        "color: #f4b860; background: #1e0d06; padding: 2px 6px;",
        "color: #e2542f; background: #050403; padding: 2px 6px;");
    }
    if (!document.getElementById("nova-new-fonts")) {
      const l = document.createElement("link");
      l.id = "nova-new-fonts";
      l.rel = "stylesheet";
      l.href = "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Manrope:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap";
      document.head.appendChild(l);
    }
    if (this._hass && !this._renderedOnce) {
      this._render();
      this._startIntervals();
    }
  }

  disconnectedCallback() {
    if (this._fetchInterval) clearInterval(this._fetchInterval);
    if (this._sparklineInterval) clearInterval(this._sparklineInterval);
    if (this._cameraInterval) clearInterval(this._cameraInterval);
    if (this._animHandle) cancelAnimationFrame(this._animHandle);
    if (this._resizeListener) window.removeEventListener("resize", this._resizeListener);
  }

  _startIntervals() {
    this._fetchLiveData();
    if (!this._fetchInterval) {
      this._fetchInterval = setInterval(() => this._fetchLiveData(), 20000);
    }
    this._fetchAreaSparklines();
    if (!this._sparklineInterval) {
      // Trend history changes slowly — matches Classic's own 5-minute cadence
      // (nova-panel.js's _fetchAreaSparklines), no need to poll as often as
      // the main dashboard data.
      this._sparklineInterval = setInterval(() => this._fetchAreaSparklines(), 300000);
    }
  }

  async _fetchAreaSparklines() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "nova/get_area_sparklines" });
      this._sparklines = res?.sparklines || {};
    } catch (err) {
      console.warn("Nova: sparkline fetch failed", err);
      return;
    }
    this._renderData();
  }

  // ─── Data ────────────────────────────────────────────────────────────────

  async _fetchLiveData() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: "nova/get_panel_data" });
      this._liveData = result;
      try {
        const log = await this._hass.callWS({ type: "nova/get_activity_log", hours: 24, limit: 60 });
        this._activityData = log?.entries || [];
      } catch (_) { this._activityData = []; }
    } catch (err) {
      console.warn("Nova: panel data fetch failed", err);
    }
    try {
      this._solar = await this._hass.callWS({ type: "nova/solar", action: "status" });
    } catch (_) { this._solar = null; }
    try {
      this._mode = await this._hass.callWS({ type: "nova/mode", action: "status" });
    } catch (_) { this._mode = null; }
    if (this._currentTab === "dashboard") {
      try {
        this._cognitive = await this._hass.callWS({ type: "nova/get_cognitive_status" });
      } catch (_) { this._cognitive = null; }
    }
    if (this._currentTab === "logs") this._fetchDebugLog();
    this._loadUiStrings();
    this._detectFlare();
    this._renderData();
  }

  _detectFlare() {
    const top = (this._activityData || [])[0];
    const sig = top ? `${top.ts}|${top.tag}|${top.msg}` : null;
    if (sig && this._lastActivitySig && sig !== this._lastActivitySig) {
      this._flareUntil = Date.now() + 4000; // a real event just landed — brief flare
    }
    this._lastActivitySig = sig;
  }

  _coreState() {
    const sleepState = String(this._liveData?.status?.sleep?.state || "").toUpperCase();
    if (sleepState === "ASLEEP") return "asleep";
    if (Date.now() < this._flareUntil) return "reasoning";
    return "idle";
  }

  _data() {
    const live = this._liveData;
    if (!live) return null;
    return {
      status: live.status || {},
      areas: live.areas || [],
      doors: live.doors || {},
      cameras: live.config?.cameras || [],
      areasMonitored: live.meta?.areas_monitored ?? "—",
      occupied: (live.areas || []).filter(a => a.active).length,
      config: live.config || {},
      doorbellTraining: live.doorbell_training || {},
      suggestions: live.suggestions || [],
      goals: live.goals || [],
      lockdown: live.lockdown || live.config?.lockdown || {},
      onboarding: live.onboarding || live.config?.onboarding || null,
      available_labels: live.config?.available_labels || [],
    };
  }

