/**
 * Behavioral smoke test for the combined Nova panel (nova-panel).
 *
 * node --check only validates syntax — it can't catch an orphaned stylesheet, a
 * data-contract mismatch, or a camera module that never renders. This renders the
 * real component under jsdom with a realistic nova/get_panel_data payload and
 * asserts the dashboard actually draws: styles, the 3D residence, AND the folded-in
 * Camera Watch (live feed + chips from config.cameras + auto-selected stream).
 *
 * Run:  npm install jsdom --no-save && NODE_PATH=node_modules node scripts/smoke_panel.js
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");

const COMPONENT = path.resolve(__dirname, "..", "custom_components", "nova", "frontend", "nova-panel.js");
const NEW_LOOK_COMPONENT = path.resolve(__dirname, "..", "custom_components", "nova", "frontend", "nova-panel-new.js");
const dom = new JSDOM("<!DOCTYPE html><body></body>", { url: "http://localhost/", pretendToBeVisual: true });
const { window } = dom;
global.window = window; global.document = window.document;
["HTMLElement", "customElements", "Node", "Event", "CustomEvent", "requestAnimationFrame", "cancelAnimationFrame"].forEach(k => { if (window[k]) global[k] = window[k]; });
// jsdom doesn't implement window.confirm (always undefined/falsy) — both
// panels' document-delete flows gate on it, so stub it to auto-confirm.
window.confirm = () => true;

window.eval(fs.readFileSync(COMPONENT, "utf8"));
window.eval(fs.readFileSync(NEW_LOOK_COMPONENT, "utf8"));

// Raw get_panel_data contract: status.*, meta.*, dominant, areas[], config.cameras
const PANEL = {
  status: {
    observer: { state: "RUNNING", level: "live" }, sleep: { state: "ASLEEP", level: "warn" },
    gemini: { state: "READY", level: "live" }, broadcast: { state: "ONLINE", level: "live" },
    notify: { state: "READY", level: "live" }, satellites: { state: "8 / 8", level: "live" },
  },
  meta: { bedrooms: 3, areas_monitored: 14, announcements_today: 0, est_cost: "—", uptime: "6m" },
  dominant: { area_id: "garage", name: "Garage", subtitle: "Occupied · 26s", coord: "#09", temp: "66°", humidity: "52%", lights: "ON", satellite: "—", last_motion: "00:26" },
  areas: [
    { id: "garage", name: "Garage", caps: ["cam", "light"], active: true, bedroom: false, lights_on: 1, lights_total: 1,
      temp: "68°F", humidity: "51%", temp_entity: "sensor.garage_temp", humidity_entity: "sensor.garage_humidity", last_motion: "26s" },
    { id: "backyard", name: "Backyard", caps: ["cam", "mmwave"], active: true, bedroom: false, lights_on: 0, lights_total: 0,
      temp: null, humidity: null, temp_entity: null, humidity_entity: null, last_motion: null },
    { id: "kitchen", name: "Kitchen", caps: ["sat", "spkr"], active: false, bedroom: false, lights_on: 0, lights_total: 0,
      temp: "71°F", humidity: null, temp_entity: "sensor.kitchen_temp", humidity_entity: null, last_motion: "12m" },
  ],
  onboarding: { dismissed: false, show: true, done_count: 1, total: 5, steps: [
    { id: "notify", label: "Set an alert destination", hint: "phone", done: true, jump: "Notifications" },
    { id: "cameras", label: "Connect cameras (optional)", hint: "nest", done: false, jump: "Cameras" },
    { id: "voice", label: "Set up voice (optional)", hint: "voice", done: false },
    { id: "banter", label: "Pick a personality level", hint: "wit", done: false },
    { id: "briefings", label: "Turn on daily briefings", hint: "briefings", done: false, jump: "Briefings" },
  ] },
  config: { floor_plan_address: "123 Example St, Springfield IL", banter_level: 2, search_backend: "searxng", searxng_url: "http://sx.local:8080", calendar_tight_gap_min: 20, recognition_source: "frigate", voice_confirm_enabled: true, voice_confirm_mode: "gated", intrusion_response_timeout: 120, cameras: [{ entity_id: "camera.front", name: "Front Door", raw_name: "Front Door", outdoor: false, location_mode: "auto" }, { entity_id: "camera.back", name: "Backyard", raw_name: "Backyard", outdoor: true, location_mode: "auto" }], camera_names: {}, lockdown: { active: false },
    cast_devices: [{ entity_id: "media_player.living_room_speaker", name: "Living Room Speaker" }, { entity_id: "media_player.kitchen_speaker", name: "Kitchen Speaker" }],
    speaker_areas: [{ area_id: "living_room", name: "Living Room" }, { area_id: "kitchen", name: "Kitchen" }],
    room_speakers: { living_room: "media_player.living_room_speaker" },
    general_speaker: "media_player.kitchen_speaker", ui_style: "classic",
    satellites: [{ entity_id: "assist_satellite.basement_nova", name: "Basement Nova", area: "Basement" }],
    satellite_pairings: { "assist_satellite.basement_nova": "media_player.living_room_speaker" },
    notify_services_available: ["notify.mobile_app_abi_phone", "notify.mobile_app_spouse_phone"],
    notify_service: "notify.mobile_app_abi_phone",
    sentinel_rules: [
      { id: "door_left_open", desc: "A door has been open for a while" },
      { id: "garage_left_open", desc: "The garage has been open overnight" },
    ],
    disabled_sentinel_rules: ["garage_left_open"],
    appliance_profile: [{ name: "Dryer", type: "dryer", entity: "", watts: 4200 }],
    appliance_announce_unknown: false,
    memory_stats: { backend: "sqlite-vec", total_memories: 214 },
    observer_stats: {
      running: true, calls_last_hour: 4, rate_limit: 30, events_24h: 112, flagged_24h: 9,
      spoken_24h: 3, cognition_enabled: true, cog_entities: 88, cog_predictable: 61,
      cog_routines: 14, cog_presence: 2, presence: [{ name: "Abi", zone: "home", gps: true, distance_km: 0 }],
      cog_escalated: 1, local_rate: 92, local_decisions: 103, cloud_calls: 9,
      learned_patterns: 14, llm_breaker: "closed",
    },
    pattern_include_entities: ["binary_sensor.garage_bay_occupied"],
    excluded_entities: ["light.spare_bedroom"], excluded_domains: [], excluded_labels: [] },
  available_labels: [{ name: "guest_visible" }, { name: "noisy" }],
  suggestions: [
    { id: 11, description: "Turn porch light on at 18:00 (6 days running)", confidence: 0.82, count: 6, yaml: "{}",
      pattern_type: "time_routine", entities: ["light.porch"],
      why_headline: "A daily routine around 18:00",
      evidence: ["Observed turning on near 18:00", "Happened 6 times in the last 30 days", "Consistent on about 82% of days"] },
  ],
  doorbell_training: {
    stats: { total: 18, notable: 3, by_source: { nest: 14, frigate: 4 } },
    recent: [
      { ts: "2026-07-13T18:22:00Z", image_source: "nest", category: "package", summary: "Amazon box left at the door", notable: true },
      { ts: "2026-07-13T09:05:00Z", image_source: "frigate", category: "", summary: "Mail carrier, routine delivery", notable: false },
    ],
  },
  goals: [
    { id: 1, title: "Guest prep", outcome: "House ready for guests by Saturday", status: "active",
      steps_done: 2, steps_total: 4, steps: [], next_check_ts: "2026-07-13T20:00:00", deadline_ts: null,
      last_result: "", updated_ts: "2026-07-13T19:00:00" },
    { id: 2, title: "Warm living room", outcome: "Living room at 72°", status: "done",
      steps_done: 1, steps_total: 1, steps: [], next_check_ts: "", deadline_ts: null,
      last_result: "Reached 72°, sir.", updated_ts: "2026-07-13T18:00:00" },
  ],
};
const _subscribedEvents = [];
const _renameCalls = [];
const _locationCalls = [];
const _sugCalls = [];
let _semanticEnabled = false;
let _activeMode = "normal";
const _modeSetCalls = [];
const _serviceCalls = [];
const _docDeleteCalls = [];
let _energyAgency = "advisory";
let _bioEnabled = false;
let _pendingFacts = [{ id: 42, key: "bedtime", value: "10pm", subject: "primary" }];
let _knownFacts = [
  { id: 1, key: "trash day", value: "Tuesday", subject: "household", source: "stated", confidence: 1 },
  { id: 2, key: "favorite tea", value: "Earl Grey", subject: "primary", source: "inferred", confidence: 0.7 },
];
let _intrCalledOff = false;
let _intrAck = false;
const _intrSnap = { url: "/local/nova/intrusion/intrusion_dining_room_1730000000.jpg", camera: "camera.dining_room", ts: 1730000000, path: "/config/www/nova/intrusion/x.jpg" };
const _updateConfigCalls = [];
const hass = {
  config: { location_name: "Springfield IL", latitude: 39.78, longitude: -89.65 },
  states: { "assist_satellite.a": { state: "idle", attributes: {} }, "camera.front": { attributes: { access_token: "tok123" } }, "camera.back": { attributes: { access_token: "tok456" } },
    "binary_sensor.mailbox": { state: "off", attributes: { friendly_name: "Mailbox" } } },
  callWS: async (m) => {
    if (m.type === "nova/update_config") { _updateConfigCalls.push({ key: m.key, value: m.value }); return {}; }
    if (m.type === "nova/get_panel_data") return PANEL;
    if (m.type === "nova/get_activity_log") return { entries: [
      { ts: "08:59", urgency: "low", tag: "OBS", msg: "motion in kitchen" },
      { ts: "09:02", urgency: "medium", tag: "GOAL", msg: "goal #1 engaged quietly" },
      { ts: "09:05", urgency: "high", tag: "SAFETY", msg: "garage door left open" },
    ] };
    if (m.type === "nova/get_cognitive_status") return { learning: { days_of_data: 48, state_changes: 217802, commands: 93, suggestions: 0 }, ignore_rules: 0 };
    if (m.type === "nova/get_person_routines") return { routines: { username: [
      { id: 1, pattern_type: "time_routine", description: "office light turns on around 07:00 most days when Username is home", confidence: 0.82, occurrences: 9, last_seen: "2026-07-13" },
    ] } };
    if (m.type === "nova/get_knowledge") return { facts: _knownFacts, pending: _pendingFacts, stats: {} };
    if (m.type === "nova/add_knowledge") {
      const id = Math.max(0, ..._knownFacts.map(f => f.id)) + 1;
      _knownFacts = [..._knownFacts, { id, key: m.key, value: m.value, subject: m.subject, source: "stated", confidence: 1 }];
      return { ok: true, facts: _knownFacts };
    }
    if (m.type === "nova/forget_knowledge") {
      _knownFacts = _knownFacts.filter(f => f.id !== m.fact_id);
      return { ok: true, facts: _knownFacts };
    }
    if (m.type === "nova/pending_fact_action") {
      _pendingFacts = _pendingFacts.filter(f => f.id !== m.fact_id);
      return { ok: true, facts: [], pending: _pendingFacts };
    }
    if (m.type === "nova/edit_pending_fact") {
      const f = _pendingFacts.find(f => f.id === m.fact_id);
      if (f) f.value = m.value;
      return { ok: !!f, pending: _pendingFacts };
    }
    if (m.type === "nova/camera_snapshot") return { image: "/9j/dGVzdGpwZWc=" };
    if (m.type === "nova/biometrics") {
      if (m.action === "enable") _bioEnabled = true;
      if (m.action === "disable") _bioEnabled = false;
      return { enabled: _bioEnabled, found: _bioEnabled ? 2 : 0, entities: _bioEnabled ? [
        { kind: "heart_rate", entity: "sensor.watch_hr", value: "62", unit: "bpm", name: "Heart Rate" },
        { kind: "sleep_stage", entity: "sensor.sleep_stage", value: "light_sleep", unit: "", name: "Sleep Stage" },
      ] : [] };
    }
    if (m.type === "nova/energy") {
      if (m.action === "status") return { watts: 9200, kw: 9.2, meter: "sensor.home_power", peak_watts: 8000, over_peak: true, agency: _energyAgency, configured_agency: _energyAgency, running: [
        { name: "Dryer", entity: "switch.dryer", watts: 4200, shed_ok: true },
        { name: "Refrigerator", entity: "sensor.fridge", watts: 200, shed_ok: false },
      ], advice: ["Heads up — Dryer and Oven are running at 9.2 kW, over your peak."] };
      if (m.action === "set_agency") { _energyAgency = m.agency; return { watts: 9200, kw: 9.2, over_peak: true, agency: m.agency, configured_agency: m.agency, running: [], advice: [] }; }
    }
    if (m.type === "nova/solar") {
      if (m.action === "status") return {
        configured: true, solar_w: 3200, battery_w: -450, battery_pct: 82,
        grid_w: -650, grid_direction: "export", self_sufficiency_pct: 100.0,
        advice: ["Generating 3.2 kW of solar right now. Exporting 0.7 kW to the grid. Battery at 82%."],
      };
    }
    if (m.type === "nova/hazard") {
      if (m.action === "status") return { enabled: true, center: [40.77, -75.61], using_override: false,
        quake_radius_km: 300, quake_min_mag: 2.5, disaster_radius_km: 300,
        feeds: { earthquakes: true, weather: true, disasters: true } };
      if (m.action === "scan") return { ok: true, center: [40.77, -75.61],
        earthquakes: [{ id: "q1", mag: 3.4, place: "12km N of town", dist_km: 12 }],
        weather: [{ id: "w1", event: "Tornado Warning", severity: "Extreme", area: "Lehigh, PA" }],
        disasters: [{ id: "d1", title: "Wildfire", category: "Wildfires", dist_km: 40 }],
        counts: { earthquakes: 1, weather: 1, disasters: 1 } };
    }
    if (m.type === "nova/mode") {
      if (m.action === "status") return { active: _activeMode, since: 0, reason: "", description: "Default operation.", overrides: {}, available: [
        { name: "normal", description: "Default operation." },
        { name: "party", description: "Guests over." },
        { name: "movie", description: "Near-silent." },
        { name: "away", description: "Household away." },
      ]};
      if (m.action === "set") { _activeMode = m.mode; _modeSetCalls.push({ mode: m.mode }); return { ok: true, mode: m.mode, active: m.mode, description: "switched", available: [
        { name: "normal", description: "Default operation." },
        { name: "party", description: "Guests over." },
      ]}; }
    }
    if (m.type === "nova/intrusion") {
      if (m.action === "log") return { events: [
        { id: "evt_1", ts: 1786000000, kind: "confirmed", reason: "person on camera",
          breach: "kitchen window", breach_area: "kitchen", camera: "camera.kitchen",
          snapshot_url: "/local/nova/intrusion/a.jpg", label: null },
        { id: "evt_2", ts: 1785999000, kind: "unresolved", reason: "no response",
          breach: "kitchen window", breach_area: "kitchen", snapshot_url: "", label: "false" },
      ], learning: { events: 2, labeled: 1, patterns: {}, damped_patterns: ["kitchen|4"], min_false_to_damp: 3 } };
      if (m.action === "label") return { ok: true, id: m.event_id, label: m.label,
        learning: { events: 2, labeled: 2, patterns: {}, damped_patterns: [], min_false_to_damp: 3 } };
      if (m.action === "learning") return { events: 2, labeled: 1, patterns: {}, damped_patterns: [], min_false_to_damp: 3 };
      if (m.action === "dismiss") { _intrCalledOff = true; return { ok: true, last_snapshot: _intrSnap, called_off: true, suppressed_for: 600, false_alarms_24h: 1 }; }
      if (m.action === "acknowledge") { _intrAck = true; return { ok: true, last_snapshot: _intrSnap, called_off: _intrCalledOff, acknowledged: true, suppressed_for: 0, false_alarms_24h: 0 }; }
      return { last_snapshot: _intrSnap, called_off: _intrCalledOff, acknowledged: _intrAck, suppressed_for: _intrCalledOff ? 600 : 0, false_alarms_24h: _intrCalledOff ? 1 : 0 };
    }
    if (m.type === "nova/voice_confirm_test") return { ok: true, satellite: "assist_satellite.basement_nova", note: "Announce fired." };
    if (m.type === "nova/list_models") return {
      models: m.provider === "groq"
        ? ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct", "meta-llama/llama-4-scout-17b"]
        : ["gpt-4o", "gpt-4o-mini"],
    };
    if (m.type === "nova/diagnostics") return {
      overall: "warn", summary: "3/4 core services healthy",
      services: [
        { name: "LLM", key: "llm", status: "ok", detail: "reachable — 5 model(s) available" },
        { name: "Embeddings", key: "embeddings", status: "off", detail: "semantic search disabled" },
        { name: "TTS", key: "tts", status: "ok", detail: "tts.piper available" },
        { name: "STT", key: "stt", status: "warn", detail: "configured not found; 1 other present" },
      ],
    };
    if (m.type === "nova/semantic_search") {
      if (m.action === "status") return { enabled: _semanticEnabled, ollama_configured: true, base: "http://gpu.local:11434", model: "nomic-embed-text", vector_count: _semanticEnabled ? 42 : 0 };
      if (m.action === "enable") { _semanticEnabled = true; return { ok: true, enabled: true, model: "nomic-embed-text", base: "http://gpu.local:11434", dim: 768 }; }
      if (m.action === "disable") { _semanticEnabled = false; return { ok: true, enabled: false }; }
      if (m.action === "test") return { ok: true, model: "nomic-embed-text", dim: 768 };
    }
    if (m.type === "nova/documents") {
      if (m.action === "status") return { chroma: true, fts: false, chunk_count: 42, directory: "/config/nova/documents", sources: [{ source: "furnace_manual.pdf", chunks: 30 }, { source: "dishwasher_receipt.txt", chunks: 12 }] };
      if (m.action === "ingest") return { ok: true, files_ingested: 2, files_seen: 2, total_chunks: 42 };
      if (m.action === "search") return { results: [{ text: "The furnace filter size is 16x25x1 MERV 11.", source: "furnace_manual.pdf", chunk: 4, score: 0.88 }] };
      if (m.action === "upload") return { ok: true, filename: m.filename || "uploaded.pdf", chunks: 12, embedded: 12 };
      if (m.action === "scan_watch") return { ok: true, watched: 1, new_files: 2 };
      if (m.action === "delete") { _docDeleteCalls.push(m.filename); return { ok: true, filename: m.filename }; }
    }
    if (m.type === "nova/mmwave_overview") return {
      rooms: [
        { area_id: "kitchen", name: "Kitchen", outdoor: false, sensor_count: 2, detecting_count: 1, state: "detecting", freshest: "now", sensors: [] },
        { area_id: "office", name: "Office", outdoor: false, sensor_count: 1, detecting_count: 0, state: "clear", freshest: "12m", sensors: [] },
        { area_id: "patio", name: "Patio", outdoor: true, sensor_count: 1, detecting_count: 0, state: "clear", freshest: "3h", sensors: [] },
      ],
      summary: { rooms_with_mmwave: 3, rooms_detecting: 1, total_sensors: 4 },
    };
    if (m.type === "nova/suggestion_action") {
      _sugCalls.push({ id: m.suggestion_id, action: m.action });
      return m.action === "approve"
        ? { ok: true, installed: true, alias: "Nova · porch on at 18:00" }
        : { ok: true };
    }
    if (m.type === "nova/camera_location") {
      _locationCalls.push({ entity_id: m.entity_id, mode: m.mode });
      return { ok: true, cameras: [
        { entity_id: "camera.front", name: "Bedroom 2", raw_name: "Front Door", outdoor: m.mode === "outdoor", location_mode: m.mode },
        { entity_id: "camera.back", name: "Backyard", raw_name: "Backyard", outdoor: true, location_mode: "auto" },
      ] };
    }
    if (m.type === "nova/rename_camera") {
      _renameCalls.push({ entity_id: m.entity_id, name: m.name });
      return { ok: true,
        camera_names: m.name ? { [m.entity_id]: m.name } : {},
        cameras: [
          { entity_id: "camera.front", name: m.name || "Front Door", raw_name: "Front Door", outdoor: false, location_mode: "auto" },
          { entity_id: "camera.back", name: "Backyard", raw_name: "Backyard", outdoor: true, location_mode: "auto" },
        ] };
    }
    if (m.type === "nova/camera_diagnostics") return {
      summary: [{ entity_id: "camera.front", state: "idle", platform: "nest" }],
      platforms: { nest: 1, frigate: 1 },
      probe: {
        entity_id: "camera.front", state: "idle", platform: "nest",
        attrs: { frontend_stream_type: "web_rtc" },
        tiers: [
          ["backend:nest", "no image — no recent event media cached"],
          ["snapshot", "error: HomeAssistantError: stream unavailable"],
          ["wake-retry", "still unusable (0B)"],
        ],
        verdict: "NO FRAME from any tier. Nest cameras only yield event media after a motion/doorbell event — check Pub/Sub.",
        elapsed_ms: 4210,
      },
    };
    if (m.type === "nova/get_area_sparklines") return { sparklines: {
      garage: { temp: [64, 65, 66, 67, 68, 68, 67, 68], humidity: [50, 50, 51, 52, 51, 51, 50, 51] },
    } };
    if (m.type === "nova/get_debug_log") return { entries: [
      { ts: "09:00:01", cat: "CONV", msg: "heard: turn on the porch light" },
      { ts: "09:00:02", cat: "AGENT", msg: "executed light.turn_on for porch" },
      { ts: "09:01:15", cat: "ERROR", msg: "camera.front unavailable" },
    ] };
    return {};
  },
  connection: {
    subscribeEvents: async (handler, eventType) => {
      _subscribedEvents.push(eventType);
      return () => {};
    },
  },
  callService: async (domain, service, data) => { _serviceCalls.push({ domain, service, data }); },
};

// v7.93.0: "nova-panel" is now a thin shell that picks between Classic and
// the new look at runtime (see NovaPanelShell in nova-panel.js). This suite
// tests Classic's own internals directly, so it creates "nova-panel-classic"
// — the tag Classic is registered under — bypassing the shell entirely. The
// shell itself gets its own small check further down.
const el = window.document.createElement("nova-panel-classic");
window.document.body.appendChild(el);
el.hass = hass;

setTimeout(async () => {
  const sr = el.shadowRoot, html = sr.innerHTML;
  const checks = [
    // ── Command Center tab (default) ──
    ["stylesheet injected", html.includes("<style>") && html.includes("--cyan:") && html.includes("#00f2fe")],
    ["dashboard grid present", !!sr.querySelector(".grid")],
    ["onboarding welcome card shows for new users", !!sr.querySelector(".onboarding-card")],
    ["onboarding shows step progress + checklist",
      /1\/5 done/.test(sr.querySelector(".ob-progress")?.textContent || "") && sr.querySelectorAll(".ob-step").length === 5],
    ["onboarding surfaces the proactive briefings step",
      /Turn on daily briefings/.test(sr.querySelector(".ob-steps")?.textContent || "")],
    ["onboarding marks done steps", !!sr.querySelector(".ob-step.ob-done")],
    ["onboarding has dismiss + settings-jump", !!sr.querySelector("#ob-dismiss") && !!sr.querySelector(".ob-go[data-tab-jump]")],
    ["onboarding steps have per-step jump buttons", sr.querySelectorAll(".ob-step-go[data-ob-jump]").length >= 3],
    ["Residence tab button present", !!sr.querySelector('[data-tab="residence"]')],
    ["Camera Watch module present", !!sr.querySelector(".c-camera") && !!sr.querySelector("#cam-feed")],
    ["camera owns center (residence moved out of dashboard)", !sr.querySelector("#house3d-scene")],
    ["camera chips from config.cameras (2)", sr.querySelectorAll(".camchip[data-cam]").length === 2],
    ['camera auto-selected (no "NO CAMERA")', !/NO CAMERA SELECTED/.test(sr.querySelector("#cam-feed")?.innerHTML || "NO CAMERA SELECTED")],
    ["live MJPEG src wired with token", !!(sr.querySelector("#cam-feed img") && /camera_proxy_stream\/camera\.front\?token=tok123/.test(sr.querySelector("#cam-feed img").src))],
    ["camera native aspect (height:auto, no object-fit)", /\.cam-feed img\s*\{[^}]*height:\s*auto/.test(html) && !/\.cam-feed img\s*\{[^}]*object-fit/.test(html)],
    ["system status rows live (RUNNING)", /RUNNING/.test(html)],
    ["Goals panel present", !!sr.querySelector(".goal-list")],
    ["both goals rendered", sr.querySelectorAll(".goal").length === 2],
    ["active goal has cancel button, done goal doesn't",
      !!sr.querySelector('.goal-active .goal-cancel') && !sr.querySelector('.goal-done .goal-cancel')],
    ["done goal has a delete button (tidy the list)",
      !!sr.querySelector('.goal-done .goal-delete')],
    ["new-goal input present (write a goal in)",
      !!sr.querySelector('.goal-new-input') && !!sr.querySelector('.goal-new-btn')],
    ["done goal shows status badge", /DONE/.test(sr.querySelector(".goal-done .goal-status-badge")?.textContent || "")],
    ["active goal shows step progress (2/4)", /2\/4/.test(sr.querySelector(".goal-active .goal-steps-pct")?.textContent || "")],
    ["real-time state_changed subscription wired", _subscribedEvents.includes("state_changed")],
    ["camera event subscriptions still wired", _subscribedEvents.includes("nova_camera_event")],
    ["area tile shows temp reading", /68°F/.test(sr.querySelector('.area[data-area-id="garage"] .area-reading')?.textContent || "")],
    ["area tile sparkline rendered for garage", !!sr.querySelector('.area[data-area-id="garage"] .spark')],
    ["area tile without sensor has no readings row", !sr.querySelector('.area[data-area-id="backyard"] .area-readings')],
    ["area tiles are keyboard-focusable (drill-down affordance)", sr.querySelector('.area[data-area-id="garage"]')?.getAttribute('tabindex') === '0'],
    ["activity search box present", !!sr.getElementById("activity-search")],
    ["activity feed renders all mock entries", sr.querySelectorAll("#activity-feed .evt").length === 3],
    ["activity feed rows carry category icons", sr.querySelectorAll("#activity-feed .evt .evt-icon").length === 3],
  ];

  // ── activity feed search: narrow, count, empty state, live-patch respect ──
  el._activitySearch = "garage";
  el._updateActivityFeed();
  checks.push(
    ["activity search narrows feed", el.shadowRoot.querySelectorAll("#activity-feed .evt").length === 1],
    ["activity count shows filtered/total", /1 OF 3/.test(el.shadowRoot.getElementById("activity-count")?.textContent || "")],
  );
  el._patchLiveDom(PANEL);  // a poll/real-time refresh must keep the filter applied
  checks.push(
    ["live patch keeps activity filter applied", el.shadowRoot.querySelectorAll("#activity-feed .evt").length === 1],
  );
  el._activitySearch = "zzz-no-match";
  el._updateActivityFeed();
  checks.push(
    ["activity search empty state shown", /No events match/.test(el.shadowRoot.getElementById("activity-feed")?.textContent || "")],
  );
  el._activitySearch = "";
  el._updateActivityFeed();
  checks.push(
    ["clearing activity search restores all entries", el.shadowRoot.querySelectorAll("#activity-feed .evt").length === 3
      && /LAST 3/.test(el.shadowRoot.getElementById("activity-count")?.textContent || "")],
  );

  // ── click the Garage tile: entity-card drill-down should open ──
  sr.querySelector('.area[data-area-id="garage"]')?.click();
  const detail = el.shadowRoot;
  checks.push(
    ["area detail overlay opens on tile click", !!detail.getElementById("area-detail-overlay")],
    ["area detail shows the right area name", /Garage/.test(detail.querySelector(".area-detail-title")?.textContent || "")],
    ["area detail shows temp value + sparkline", /68°F/.test(detail.querySelector(".ads-value")?.textContent || "") && !!detail.querySelector(".ads-spark .spark")],
  );
  detail.querySelector(".area-detail-close")?.click();
  checks.push(
    ["area detail overlay closes on ✕", !el.shadowRoot.getElementById("area-detail-overlay")],
  );

  // ── switch to Residence tab and re-check ──
  el._currentTab = "residence";
  el._render();
  const r = el.shadowRoot, rhtml = r.innerHTML;
  checks.push(
    ["residence tab renders iso scene", !!r.querySelector("#house3d-scene")],
    ["2D isometric SVG rendered", !!r.querySelector("#res-iso svg")],
    ["solid house drawn (svg polygons)", r.querySelectorAll("#res-iso svg polygon").length >= 15],
    ["3D house is data-driven from editor rooms (feet)", (() => { const p = el._house3dPlan(); return !!(p && p["1f"] && p["1f"].length && p["1f"].some(rm => rm.w > 0 && rm.d > 0 && rm.name)); })()],
    ["home-type roof is applied (gable/hip/flat/gambrel by style)", (() => { const sp = el._houseSpec(); return ["gable","hip","flat","gambrel"].includes(sp.roof) && sp.stories != null; })()],
    ["Dutch Colonial maps to a gambrel roof", el._styleDefaults('dutch_colonial').roof === 'gambrel' && el._resStyles().dutch_colonial.roof === 'gambrel' && el._resStyles().dutch_colonial.label === 'Dutch Colonial'],
    ["gambrel exterior renders without error", (() => { try { const s = el._liveData && el._liveData.config; const prev = s ? el._liveData.config.residence_style : null; if (s) el._liveData.config.residence_style = 'dutch_colonial'; const svg = el._renderEditorPreview(); if (s) el._liveData.config.residence_style = prev; return typeof svg === 'string' && svg.length > 100; } catch (e) { return false; } })()],
    ["operational mode has AUTO occupancy toggle", (() => { try { const prev = el._currentTab; el._currentTab = 'settings'; const h = el._html(); el._currentTab = prev; return /data-cfg-key="operational_mode_auto"/.test(h) && /mode-auto-row/.test(h); } catch (e) { return false; } })()],
    ["openings entity list includes window sensors", (() => { const st = el._hass.states; st['binary_sensor.test_kitchen_window'] = { state: 'off', attributes: { device_class: 'window', friendly_name: 'Kitchen Window' } }; const html = el._doorEntityOptions(''); delete st['binary_sensor.test_kitchen_window']; return /test_kitchen_window/.test(html); })()],
    ["mode bindings: lab rooms + movie room/player/dim", (() => { try { const prev = el._currentTab; el._currentTab = 'settings'; const h = el._html(); el._currentTab = prev; return /class="mode-bindings"/.test(h) && /data-lab-area/.test(h) && /data-cfg-key="movie_area"/.test(h) && /data-cfg-key="movie_media_player"/.test(h) && /data-cfg-key="movie_dim_pct"/.test(h); } catch (e) { return false; } })()],
    ["exterior All view draws lit windows + garage doors (clean shell)", (() => { const s = el._build3DHouse ? "" : ""; const svg = r.querySelector("#res-iso")?.innerHTML || ""; return /class="gdoor"/.test(svg) || /gdoor/.test(svg); })()],
    ["occupied stat wired (n / total)", /\d+\s*\/\s*\d+/.test((r.getElementById("res-occ") || {}).textContent || "")],
    ["home-style selector with options", !!r.querySelector("#res-style-sel") && r.querySelectorAll("#res-style-sel option").length >= 6],
    ["property banner reflects HA home location (not a hardcoded personal default)", !!r.querySelector(".res-banner") && /Springfield IL/.test(r.querySelector("#res-addr")?.textContent || "")],
    ["floor editor exposes export/import + units controls", (() => { const h = el._renderFloorPlanEditor(el._data()); return /id="fp-export"/.test(h) && /class="fp-import-layout"/.test(h) && /id="fp-units"/.test(h); })()],
    ["floor editor: place windows/exterior/cellar/interior openings", (() => { const h = el._renderOpenings("1f"); return /id="op-add-window"/.test(h) && /id="op-add-extdoor"/.test(h) && /id="op-add-cellar"/.test(h) && /id="op-add-intdoor"/.test(h); })()],
  );

  // ── stored XSS regression #3 (fixed 13 Sept 2026): the floor-plan EDITOR's
  // SVG builds room-name <text> labels by string concatenation — a room name
  // is user-entered, so a payload there must render as inert text in the
  // returned markup, and (once inserted into the DOM the way the real editor
  // canvas does) must never actually execute. ──
  window.__xssFired3 = false;
  const xssRoomName = '</text><image href=x onerror="window.__xssFired3=true"/><text>';
  const fpXssSvg = el._renderEditableSVG(
    { ground: { rooms: [{ name: xssRoomName, type: "room", x: 10, y: 10, w: 40, h: 30 }] } },
    "ground",
  );
  const fpXssHost = document.createElement("div");
  fpXssHost.innerHTML = fpXssSvg;   // same sink the real editor canvas uses
  checks.push(
    ["floor-plan room-name XSS payload is escaped in the returned SVG string",
      // room names render UPPERCASED, so the escaped payload reads &lt;IMAGE...
      /&lt;image/i.test(fpXssSvg) && !fpXssSvg.includes('<image href=x onerror=')],
    ["floor-plan room-name XSS payload does not create a live <image> element",
      !fpXssHost.querySelector("image")],
    ["floor-plan room-name XSS payload's onerror handler never actually ran",
      window.__xssFired3 === false],
    ["cased openings: add button + room-scoped no-sensor row", (() => { try { const h = el._renderOpenings("1f"); if (!/id="op-add-cased"/.test(h)) return false; const arr = el._elemsFor("1f"); const n0 = arr.length; arr.push({ id: 'ec', type: 'door', kind: 'cased', wall: 'front', room: '', pos: 0.5, w: 20 }); const h2 = el._renderOpenings("1f"); const entBefore = (h.match(/data-op="entity"/g) || []).length; const entAfter = (h2.match(/data-op="entity"/g) || []).length; arr.length = n0; return /CASED OPENING/.test(h2) && /open passage/.test(h2) && /data-op="room"/.test(h2) && entAfter === entBefore; } catch (e) { return false; } })()],
    ["camera FOV: add button + placement + cone", (() => { try { if (!/id="cam-add"/.test(el._renderCameras("1f"))) return false; const arr = el._camsFor("1f"); const n0 = arr.length; arr.push({ id: 'ct', x: 100, y: 80, angle: 270, fov: 90, range: 55, entity: '', indoor: true }); const h2 = el._renderCameras("1f"); const coneOk = /^M 100 80 L .* A 55 55 .* Z$/.test(el._coneD(arr[arr.length - 1])); const svg = el._renderEditableSVG({ '1f': { rooms: [{ name: 'Dining', x: 50, y: 50, w: 80, h: 60 }] } }, "1f"); arr.length = n0; return /CAM 1/.test(h2) && /INDOOR/.test(h2) && coneOk && /class="fp-cam"/.test(svg) && /fp-cam-cone/.test(svg) && /fp-cam-dot/.test(svg); } catch (e) { return false; } })()],
    ["camera coverage: LOS through openings, walls block", (() => { try { const sp = el._editingPlan, se = el._editingElements; el._editingPlan = { '1f': { rooms: [ { name: 'Dining', x: 0, y: 0, w: 40, h: 40, type: 'room' }, { name: 'Kitchen', x: 40, y: 0, w: 40, h: 40, type: 'room' } ] } }; el._editingElements = { '1f': [ { id: 'o1', type: 'door', kind: 'cased', room: 'Dining', wall: 'right', pos: 0.5, w: 20 } ] }; const cam = { x: 20, y: 20, angle: 0, fov: 170, range: 120, indoor: true }; const withOpen = el._computeCoverage('1f', cam); el._editingElements = { '1f': [] }; const noOpen = el._computeCoverage('1f', cam); el._editingPlan = sp; el._editingElements = se; return withOpen.Kitchen > 0 && !noOpen.Kitchen && withOpen.Dining > 0; } catch (e) { return false; } })()],
    ["editor zoom/pan: viewBox override + grid follows + live apply", (() => { try { const sp = el._editingPlan, sv = el._editVB; el._editingPlan = { '1f': { rooms: [ { name: 'House', x: 0, y: 0, w: 200, h: 150, type: 'room' } ] } }; el._editVB = { x: 50, y: 50, w: 100, h: 80 }; const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const mock = { _a: {}, setAttribute: (k, v) => mock._a[k] = v, querySelectorAll: () => [] }; el._applyEditVB(mock); el._editingPlan = sp; el._editVB = sv; return /viewBox="50 50 100 80"/.test(svg) && /class="fp-grid-rect" x="50" y="50"/.test(svg) && mock._a.viewBox === '50 50 100 80'; } catch (e) { return false; } })()],
    ["3D wall snap: nearly-touching rooms align in feet plan (seamless walls)", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'A', type: 'room', x: 0, y: 0, w: 50, h: 40 }, { name: 'B', type: 'room', x: 52.5, y: 0, w: 50, h: 40 } ] } }; const feet = el._planToFeet(el._getEditingPlan()); const a = feet['1f'][0], b = feet['1f'][1]; const closed = Math.abs((a.x + a.w) - b.x) < 0.001; el._editingPlan = { '1f': { rooms: [ { name: 'A', type: 'room', x: 0, y: 0, w: 50, h: 40 }, { name: 'C', type: 'room', x: 110, y: 0, w: 50, h: 40 } ] } }; const f2 = el._planToFeet(el._getEditingPlan()); const apart = Math.abs((f2['1f'][0].x + f2['1f'][0].w) - f2['1f'][1].x) > 5; el._editingPlan = sp; return closed && apart; } catch (e) { return false; } })()],
    ["i18n swap: translates standalone labels, leaves mixed/dynamic strings", (() => { try { const save = el._uiStrings; el._uiStrings = { "General": "Général", "Settings": "Paramètres", "Satellite → Speaker": "Satellite → Enceinte" }; const div = document.createElement("div"); div.innerHTML = '<span>General</span><button>Settings</button><span>Satellite → Speaker</span><span>Occupied 5/14</span>'; el._localizeDOM(div); const sp = div.querySelectorAll("span"); const ok = sp[0].textContent === "Général" && div.querySelector("button").textContent === "Paramètres" && sp[1].textContent === "Satellite → Enceinte" && sp[2].textContent === "Occupied 5/14"; el._uiStrings = save; return ok; } catch (e) { return false; } })()],
    ["floor-below ghost: 2f editor shows 1f footprint outline (red), zones excluded", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'Living', type: 'room', x: 0, y: 0, w: 120, h: 80 }, { name: 'Garage', type: 'room', x: 120, y: 0, w: 60, h: 80 }, { name: 'Yard', type: 'outdoor', x: 0, y: 100, w: 180, h: 100 } ] }, '2f': { rooms: [ { name: 'Master', type: 'room', x: 10, y: 10, w: 80, h: 60 } ] } }; const svg = el._renderEditableSVG({ '2f': el._editingPlan['2f'] }, '2f'); const ghosts = (svg.match(/stroke="rgba\(255,90,90,0\.45\)"/g) || []).length; const fb = el._floorBelow('2f') === '1f' && el._floorBelow('bsmt') === null; el._editingPlan = sp; return ghosts === 2 && /1F BELOW/.test(svg) && fb; } catch (e) { return false; } })()],
    ["3b-1 polygon room in 3D: points carried to feet + preview renders", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'LRoom', type: 'room', x: 0, y: 0, w: 60, h: 60, points: [[0, 0], [60, 0], [60, 30], [30, 30], [30, 60], [0, 60]] } ] } }; const feet = el._planToFeet(el._getEditingPlan()); const r = feet['1f'][0]; const carried = Array.isArray(r.points) && r.points.length === 6 && r.points[1][0] === 12; const svg = el._renderEditorPreview(); el._editingPlan = sp; return carried && svg.length > 100; } catch (e) { return false; } })()],
    ["polygon rooms (3a): reshape materializes points + renders polygon + bbox syncs", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'Den', type: 'room', x: 10, y: 20, w: 40, h: 30 } ] } }; const rm = el._editingPlan['1f'].rooms[0]; el._ensureZonePoints(rm); const mat = rm.points.length === 4; rm.points[2] = [80, 80]; el._syncRoomBBox(rm); const bbox = rm.x === 10 && rm.y === 20 && rm.w === 70 && rm.h === 60; const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const poly = /class="fp-zone-path"/.test(svg) && (svg.match(/fp-zone-vtx/g) || []).length === 4; el._editingPlan = sp; return mat && bbox && poly; } catch (e) { return false; } })()],
    ["polygonal outdoor zones: render, point-in-poly, coverage", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'House', x: 0, y: 0, w: 100, h: 80, type: 'room' }, { name: 'Yard', type: 'outdoor', points: [[0, 100], [200, 100], [200, 200], [100, 200], [100, 300], [0, 300]] } ] } }; const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const polyOk = /class="fp-zone-path"/.test(svg) && (svg.match(/fp-zone-vtx/g) || []).length === 6; const pipOk = el._pointInPoly(50, 150, el._editingPlan['1f'].rooms[1].points) && !el._pointInPoly(150, 250, el._editingPlan['1f'].rooms[1].points); const cov = el._computeCoverage('1f', { x: 50, y: 150, angle: 90, fov: 170, range: 300, indoor: false }); const ez = el._ensureZonePoints({ x: 10, y: 10, w: 20, h: 20, type: 'outdoor' }); el._editingPlan = sp; return polyOk && pipOk && (cov.Yard || 0) > 0 && ez.length === 4; } catch (e) { return false; } })()],
    ["objects place anywhere: field + grid include left/above home (neg coords)", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'House', x: 0, y: 0, w: 150, h: 120, type: 'room' }, { name: 'Front Yard', x: 20, y: -160, w: 200, h: 130, type: 'outdoor' }, { name: 'Side Yard', x: -140, y: 0, w: 120, h: 120, type: 'outdoor' } ] } }; const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const vb = svg.match(/viewBox="([^"]+)"/)[1].split(' ').map(Number); const g = svg.match(/<rect class="fp-grid-rect" x="(-?\d+)" y="(-?\d+)"[^>]*fill="url\(#fp-grid-sm\)"/); el._editingPlan = sp; return vb[1] <= -160 && vb[0] <= -140 && g && parseInt(g[1]) <= -140 && parseInt(g[2]) <= -160; } catch (e) { return false; } })()],
    ["editor grid covers negative coords (front/side yard space)", (() => { try { const sp = el._editingPlan, spr = el._editingProperty; el._editingPlan = { '1f': { rooms: [ { name: 'House', x: 100, y: 100, w: 200, h: 120, type: 'room' } ] } }; el._setProperty([[-150, -150], [550, -150], [550, 450], [-150, 450]]); const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); el._editingPlan = sp; el._editingProperty = spr; const g = svg.match(/<rect class="fp-grid-rect" x="(-?\d+)" y="(-?\d+)"[^>]*fill="url\(#fp-grid-sm\)"/); return g && parseInt(g[1]) < 0 && parseInt(g[2]) < 0; } catch (e) { return false; } })()],
    ["property boundary: renders, computes area, viewBox frames lot", (() => { try { const sp = el._editingPlan, spr = el._editingProperty; el._editingPlan = { '1f': { rooms: [ { name: 'Living', x: 100, y: 100, w: 100, h: 80, type: 'room' } ] } }; el._setProperty([[0, 0], [500, 0], [500, 500], [0, 500]]); const area = el._propertyArea(el._propertyPts()); const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const m = svg.match(/viewBox="([^"]+)"/); const vb = m ? m[1].split(' ').map(Number) : [0, 0, 0, 0]; el._editingPlan = sp; el._editingProperty = spr; return /class="fp-prop-path"/.test(svg) && (svg.match(/fp-prop-vtx/g) || []).length === 4 && /sq ft|acres/.test(area) && vb[0] <= 0 && (vb[0] + vb[2]) >= 500; } catch (e) { return false; } })()],
    ["exterior openings snap to house, not outdoor zones", (() => { try { const sp = el._editingPlan, se = el._editingElements; el._editingPlan = { '1f': { rooms: [ { name: 'Living', x: 0, y: 0, w: 100, h: 60, type: 'room' }, { name: 'Backyard', x: 0, y: 80, w: 200, h: 150, type: 'outdoor' } ] } }; el._editingElements = { '1f': [ { id: 'd', type: 'door', kind: 'exterior', wall: 'back', pos: 0.5, w: 4 } ] }; const svg = el._renderEditableSVG({ '1f': { rooms: el._editingPlan['1f'].rooms } }, '1f'); const m = svg.match(/class="op-marker"[^>]*y="([\d.]+)"[^>]*fill="#ffaa28"/); el._editingPlan = sp; el._editingElements = se; return m && parseFloat(m[1]) < 100; } catch (e) { return false; } })()],
    ["outdoor zone: covered by camera, excluded from 3D shell", (() => { try { const sp = el._editingPlan; el._editingPlan = { '1f': { rooms: [ { name: 'Living Room', x: 0, y: 0, w: 40, h: 40, type: 'room' }, { name: 'Front Yard', x: -10, y: -55, w: 60, h: 50, type: 'outdoor' } ] } }; const cov = el._computeCoverage('1f', { x: 20, y: -30, angle: 270, fov: 150, range: 80, indoor: false }); const p3d = el._planToFeet(el._getEditingPlan()); const names = (p3d['1f'] || []).map(r => r.name); el._editingPlan = sp; return cov['Front Yard'] > 0 && names.includes('living room') && !names.includes('front yard'); } catch (e) { return false; } })()],
    ["camera cone clips to walls, bleeds through openings", (() => { try { const sp = el._editingPlan, se = el._editingElements; el._editingPlan = { '1f': { rooms: [ { name: 'A', x: 0, y: 0, w: 40, h: 40, type: 'room' }, { name: 'B', x: 40, y: 0, w: 40, h: 40, type: 'room' } ] } }; el._editingElements = { '1f': [] }; let geo = el._planGeometry('1f'); const noOpen = el._rayCast(20, 20, 0, 200, geo); el._editingElements = { '1f': [ { id: 'o', type: 'door', kind: 'cased', room: 'A', wall: 'right', pos: 0.5, w: 24 } ] }; geo = el._planGeometry('1f'); const withOpen = el._rayCast(20, 20, 0, 200, geo); const d = el._clippedCone({ x: 20, y: 20, angle: 0, fov: 120, range: 200, indoor: true }, geo); el._editingPlan = sp; el._editingElements = se; return Math.abs(noOpen - 20) < 2 && withOpen > 50 && !/ A /.test(d) && (d.match(/ L /g) || []).length > 10; } catch (e) { return false; } })()],
    ["cased opening not drawn as an exterior door in the shell", (() => { try { const sp = el._editingPlan, se = el._editingElements; el._editingPlan = { '1f': { rooms: [ { name: 'Kitchen', x: 0, y: 0, w: 40, h: 40, type: 'room' }, { name: 'Living', x: 0, y: 40, w: 40, h: 40, type: 'room' } ] } }; el._editingElements = { '1f': [ { id: 'o1', type: 'door', kind: 'cased', room: 'Kitchen', wall: 'back', pos: 0.5, w: 24 } ] }; const svgCased = el._renderEditorPreview(); el._editingElements = { '1f': [ { id: 'd1', type: 'door', kind: 'exterior', wall: 'back', pos: 0.5, w: 4 } ] }; const svgExt = el._renderEditorPreview(); el._editingPlan = sp; el._editingElements = se; const dCased = (svgCased.match(/class="door/g) || []).length; const dExt = (svgExt.match(/class="door/g) || []).length; return dCased === 0 && dExt >= 1; } catch (e) { return false; } })()],
    ["camera coverage LLM: compute button + confirms/reason + roomAt", (() => { try { const sp = el._editingPlan, se = el._editingElements, sc = el._editingCameras; el._editingPlan = { '1f': { rooms: [ { name: 'Dining', x: 0, y: 0, w: 40, h: 40, type: 'room' }, { name: 'Living', x: 0, y: 40, w: 80, h: 50, type: 'room' } ] } }; el._editingElements = { '1f': [ { id: 'o1', type: 'door', kind: 'cased', room: 'Dining', wall: 'back', pos: 0.5, w: 24 } ] }; el._editingCameras = { '1f': [ { id: 'c1', x: 20, y: 20, angle: 90, fov: 150, range: 120, entity: '', indoor: true, coverage: { covered: ['Dining', 'Living'], reason: 'via the open staircase', source: 'llm' } } ] }; const h = el._renderCameras('1f'); const ra = el._roomAt('1f', 20, 20); const od = el._openingDescriptions('1f'); el._editingPlan = sp; el._editingElements = se; el._editingCameras = sc; return /id="cam-compute"/.test(h) && /confirms Dining, Living/.test(h) && /open staircase/.test(h) && ra === 'Dining' && od.length > 0; } catch (e) { return false; } })()],
    ["3D view presets render + set the angle", (() => { const bar = el._viewPresetBar('editor'); const before = el._editorTheta; el._setView('editor', 90); const ok = /data-vtheta="90"/.test(bar) && /FRONT/.test(bar) && /ISO/.test(bar) && el._editorTheta === 90; el._editorTheta = before; return ok; })()],
    ["editor viewBox auto-fits to rooms with padding", (() => { const plan = { '1f': { rooms: [{ x: 100, y: 100, w: 80, h: 60, name: 'Test' }] } }; const svg = el._renderEditableSVG(plan, '1f'); const m = svg.match(/viewBox=\"([^\"]+)\"/); if (!m) return false; const p = m[1].split(' ').map(Number); return p[0] < 100 && p[1] < 100 && p[2] >= 80 && p[3] >= 140; })()],
    ["settings: routine-learning card (datalist picker + doors/presence + chip renders)", (() => { const cfg = el._liveData.config; cfg.pattern_include_entities = ["binary_sensor.zzz_test"]; const h = el._renderRoutineLearning(el._data()); delete cfg.pattern_include_entities; return /data-cfg-key="pattern_learn_doors"/.test(h) && /data-cfg-key="pattern_learn_presence"/.test(h) && /list="pl-entity-list"/.test(h) && /binary_sensor\.zzz_test/.test(h); })()],
    ["model self-heal picks vision-capable model for the vision role", (() => { const v = el._pickHealModel(["openai/gpt-oss-120b","qwen/qwen3.6-27b"], "vision_model"); const t = el._pickHealModel(["openai/gpt-oss-120b","qwen/qwen3.6-27b"], "model"); return v === "qwen/qwen3.6-27b" && t === "openai/gpt-oss-120b"; })()],
    ["floor editor has a live 3D preview", (() => { const h = el._renderFloorPlanEditor(el._data()); return /id="fp-3d-preview"/.test(h) && typeof el._renderEditorPreview === "function"; })()],
    ["floor editor: dormers placeable on the 2nd floor", (() => { const h = el._renderOpenings("2f"); const h1 = el._renderOpenings("1f"); return /id="op-add-fdormer"/.test(h) && /id="op-add-rdormer"/.test(h) && !/op-add-fdormer/.test(h1); })()],
    ["garage bay open/closed resolves per-bay from its sensor", (() => { const cfg = el._liveData.config; el._hass.states["binary_sensor._g1"] = { state: "open", attributes: {} }; cfg.garage_bays = 2; cfg.door_mapping = { garage_1: "binary_sensor._g1" }; const g = el._house3dGarage(); const ok = !!(g.length === 2 && g[0].open === true && g[1].open === false); delete cfg.garage_bays; delete cfg.door_mapping; delete el._hass.states["binary_sensor._g1"]; return ok; })()],
    ["placed door resolves open/closed from its sensor", (() => { const cfg = el._liveData.config; el._hass.states["binary_sensor._t"] = { state: "on", attributes: {} }; cfg.floor_plan_elements = { "1f": [{ type: "door", kind: "exterior", wall: "front", pos: 0.5, w: 12, entity: "binary_sensor._t" }] }; const e = el._house3dElements()["1f"][0]; const ok = !!(e && e.open === true && e.type === "door" && e.w > 0); delete cfg.floor_plan_elements; delete el._hass.states["binary_sensor._t"]; return ok; })()],
    ["banner stats populated (sqft + bed/bath)", /\d/.test(r.querySelector("#res-sqft")?.textContent || "") && /\d/.test(r.querySelector("#res-bb")?.textContent || "")],
    ["sqft estimate sane (<= 5000)", (() => { const m = (r.querySelector("#res-sqft")?.textContent || "").replace(/[^\d]/g, ""); return m && Number(m) <= 5000; })()],
    ["style tag reflects template", /CAPE COD/.test(r.querySelector("#res-style-tag")?.textContent || "")],
    ["3D residence is rotatable (drag wired)", r.querySelector("#house3d-scene")?._house3dWired === true]
  );

  // ── switch to 1st-floor isolation: model should draw labeled rooms ──
  el._currentFloor = "1f";
  el._render();
  checks.push(
    ["floor isolation draws labeled rooms (1F)", el.shadowRoot.querySelectorAll("#res-iso svg text").length >= 6],
    ["floor isolation keeps garage room", /GARAGE/.test(el.shadowRoot.querySelector("#res-iso svg")?.textContent || "")]
  );

  // ── camera fallback chain: stream → still → Nova WS snapshot ──
  el._currentTab = "dashboard";   // the floor-plan section above leaves us on residence
  el._render();
  const camImg = el.shadowRoot.querySelector("#cam-feed img");
  camImg.dispatchEvent(new window.Event("error"));       // MJPEG failed
  checks.push(
    ["cam error #1 falls back to proxy stills", el._camMode === "still"
      && /camera_proxy\/camera\.front/.test(camImg.src)],
  );
  camImg.dispatchEvent(new window.Event("error"));       // stills failed too
  await new Promise(r => setTimeout(r, 20));             // let the WS shot resolve
  checks.push(
    ["cam error #2 escalates to Nova snapshot tier", el._camMode === "nova"],
    ["Nova tier renders the WS frame as a data URL", /^data:image\/jpeg;base64,/.test(camImg.src)],
    ["resolved tier remembered per entity", el._camModeByEntity["camera.front"] === "nova"],
  );

  // ── watchdog: proxies that HANG (no error event) still escalate ──
  el._camMode = "stream";
  delete el._camModeByEntity["camera.front"];
  el._armCamWatchdog("camera.front", camImg, "stream", 5);
  await new Promise(r => setTimeout(r, 25));
  checks.push(
    ["hung stream (no pixels, no error) watchdogs into stills", el._camMode === "still"],
  );
  el._armCamWatchdog("camera.front", camImg, "still", 5);
  await new Promise(r => setTimeout(r, 25));
  checks.push(
    ["hung stills watchdog into Nova tier", el._camMode === "nova"],
  );

  // ── WS failure (e.g. HA not restarted) surfaces a hint, not silence ──
  const realCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/camera_snapshot") throw new Error("unknown command nova/camera_snapshot");
    return realCallWS(m);
  };
  el._camWsTimer && clearInterval(el._camWsTimer); el._camWsTimer = null;
  el._camNovaFallback("camera.front");
  await new Promise(r => setTimeout(r, 20));
  hass.callWS = realCallWS;
  checks.push(
    ["WS-unavailable shows restart hint instead of blank", /restart Home Assistant/i.test(
      el.shadowRoot.querySelector("#cam-feed .cam-none")?.textContent || "")],
  );

  // ── camera diagnostics: DIAG button probes and renders verdicts ──
  el.shadowRoot.getElementById("cam-diag-btn")?.click();
  await new Promise(r => setTimeout(r, 20));
  const diag = el.shadowRoot.querySelector("#cam-feed .cam-diag");
  const diagText = diag?.textContent || "";
  checks.push(
    ["DIAG button present in Camera Watch head", !!el.shadowRoot.getElementById("cam-diag-btn")],
    ["DIAG overlay renders platform histogram", /nest×1/.test(diagText) && /frigate×1/.test(diagText)],
    ["DIAG shows per-tier verdicts", /backend:nest/.test(diagText) && /wake-retry/.test(diagText)],
    ["DIAG surfaces the actionable Nest verdict", /Pub\/Sub/.test(diagText)],
    ["DIAG TILE line reports client-side render state", /TILE/.test(diagText) && /no decoded pixels/.test(diagText)],
  );
  el.shadowRoot.getElementById("cam-diag-btn")?.click();   // toggle off
  checks.push(
    ["DIAG toggles closed on second tap", !el.shadowRoot.querySelector("#cam-feed .cam-diag")],
  );

  // ── camera_overrides: frames reroute to the restream twin ──
  el._liveData.config.camera_overrides = { "camera.front": "camera.back" };
  el._camMode = "stream"; delete el._camModeByEntity["camera.front"];
  if (el._camWsTimer) { clearInterval(el._camWsTimer); el._camWsTimer = null; }
  el._lastCamKey = "";
  el._renderCameraFeed();
  const ovImg = el.shadowRoot.querySelector("#cam-feed img");
  checks.push(
    ["override reroutes stream URL to the twin", /camera_proxy_stream\/camera\.back/.test(ovImg?.src || "")],
    ["override uses the twin's token", /tok456/.test(ovImg?.src || "")],
        ["camera settings expose enable/disable toggles (choose all/some/none)", (() => { const h = el._renderCameraSettings(el._data()); return /cam-enable-toggle/.test(h) && /id="cam-disable-all"/.test(h) && /cameras in use/.test(h); })()],
    ["strip shows the override mapping", /Front Door → back/.test(el.shadowRoot.getElementById("cam-strip")?.textContent || "")],
  );
  delete el._liveData.config.camera_overrides;
  el._lastCamKey = ""; el._renderCameraFeed();   // restore for anything downstream

  // ── Settings tab: camera names + location designation (v6.50.0 home) ──
  el._currentTab = "settings";
  el._render();
  const camsetRows = el.shadowRoot.querySelectorAll(".camset-row");
  checks.push(
    ["✎ button removed from Command Center (decluttered)",
      !el.shadowRoot.getElementById("cam-rename-btn")],
    ["Settings renders a row per camera", camsetRows.length === 2],
    ["Nova Character panel renders banter + web-research controls",
      !!el.shadowRoot.querySelector('[data-cfg-key="banter_level"]')
      && !!el.shadowRoot.querySelector('[data-cfg-key="search_backend"]')
      && !!el.shadowRoot.querySelector('[data-cfg-key="calendar_tight_gap_min"]')],
    ["banter select reflects the SAVED value (not reset to default)",
      el.shadowRoot.querySelector('[data-cfg-key="banter_level"]')?.value === "2"],
    ["web-research backend reflects saved value",
      el.shadowRoot.querySelector('[data-cfg-key="search_backend"]')?.value === "searxng"],
    ["recognition source selector present and reflects saved value",
      el.shadowRoot.querySelector('[data-cfg-key="recognition_source"]')?.value === "frigate"],
    ["voice-confirm toggle + mode reflect saved values",
      el.shadowRoot.querySelector('[data-cfg-key="voice_confirm_enabled"]')?.classList.contains("on")
      && el.shadowRoot.querySelector('[data-cfg-key="voice_confirm_mode"]')?.value === "gated"],
    ["voice-confirm test button present", !!el.shadowRoot.getElementById("vc-test")],
    ["name input placeholder is the HA name",
      el.shadowRoot.querySelector('.camset-name[data-cam="camera.front"]')?.getAttribute("placeholder") === "Front Door"],
    ["location chips render with resolved AUTO label",
      /AUTO \(indoor\)/.test(el.shadowRoot.querySelector('.camset-row[data-cam="camera.front"]')?.textContent || "")],
  );

  const nameInput = el.shadowRoot.querySelector('.camset-name[data-cam="camera.front"]');
  nameInput.value = "Bedroom 2";
  nameInput.dispatchEvent(new window.Event("blur"));
  await new Promise(r => setTimeout(r, 20));
  checks.push(
    ["rename WS called with entity + new name",
      _renameCalls.length === 1 && _renameCalls[0].entity_id === "camera.front"
      && _renameCalls[0].name === "Bedroom 2"],
    ["display name resolver picks up the rename", el._camName("camera.front") === "Bedroom 2"],
  );
  nameInput.dispatchEvent(new window.Event("blur"));       // unchanged — must not re-call
  await new Promise(r => setTimeout(r, 10));
  checks.push(
    ["unchanged blur does not re-save", _renameCalls.length === 1],
  );

  el.shadowRoot.querySelector('.camset-row[data-cam="camera.front"] .cam-loc-chip[data-loc="outdoor"]')?.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(
    ["location WS called with entity + mode", _locationCalls.length === 1
      && _locationCalls[0].entity_id === "camera.front" && _locationCalls[0].mode === "outdoor"],
    ["OUTDOOR chip becomes active in the row",
      el.shadowRoot.querySelector('.camset-row[data-cam="camera.front"] .cam-loc-chip[data-loc="outdoor"]')?.classList.contains("active") === true],
    ["camera metadata refreshed from response",
      (el._cams.find(c => c.entity_id === "camera.front") || {}).location_mode === "outdoor"],
  );
  el._currentTab = "dashboard";
  el._render();
  checks.push(
    ["strip on Command Center shows the Nova-only name",
      /Bedroom 2/.test(el.shadowRoot.getElementById("cam-strip")?.textContent || "")],
  );

  // ── switch to Memory tab: person routines fetch + render ──
  el._currentTab = "memory";
  el._render();
  await el._fetchPersonRoutines();
  const mem = el.shadowRoot;
  checks.push(
    ["Person Routines panel present", !!mem.getElementById("proutine-list")],
    ["person group rendered (Username)", /Username/.test(mem.getElementById("proutine-list")?.textContent || "")],
    ["routine description rendered", /office light turns on/.test(mem.getElementById("proutine-list")?.textContent || "")],
    ["confidence bar rendered (82%)", /82%/.test(mem.getElementById("proutine-list")?.textContent || "")],
  );

  // ── Pending facts (v7.88.0): a fact remember() staged, unconfirmed ──
  await el._fetchKnowledge();
  const pend1 = el.shadowRoot;
  checks.push(
    ["pending panel visible when a fact is waiting", pend1.getElementById("pending-facts-panel")?.hidden === false],
    ["pending fact key/value rendered", /bedtime/.test(pend1.getElementById("pending-facts-list")?.textContent || "")],
    ["pending fact has confirm/reject/edit controls",
      !!pend1.querySelector(".pending-confirm") && !!pend1.querySelector(".pending-reject")
      && !!pend1.querySelector(".pending-save-edit")],
  );
  pend1.querySelector(".pending-confirm")?.click();
  await new Promise(r => setTimeout(r, 0));   // let the click handler's await settle
  const pend2 = el.shadowRoot;
  checks.push(
    ["confirming a pending fact removes it from the list",
      !/bedtime/.test(pend2.getElementById("pending-facts-list")?.textContent || "")],
    ["pending panel hides itself once nothing is left waiting",
      pend2.getElementById("pending-facts-panel")?.hidden === true],
  );

  // ── switch to Logs tab: category filter + text search ──
  el._currentTab = "logs";
  el._render();
  await el._fetchDebugLog();
  const logs1 = el.shadowRoot;
  checks.push(
    ["log search box present", !!logs1.getElementById("log-search")],
    ["all 3 log entries render initially", logs1.querySelectorAll(".log-entry").length === 3],
    ["log count shows total", /3 entries/.test(logs1.getElementById("log-count")?.textContent || "")],
    ["log filter chips cover every real backend category, not the stale ROUTE/REASON/TTS set",
      logs1.querySelectorAll(".log-filter").length === 20
      && !!logs1.querySelector('.log-filter[data-filter="LEARN"]')
      && !logs1.querySelector('.log-filter[data-filter="ROUTE"]')],
  );

  // Real gap Abi caught live (13 Sept 2026): a genuine "LEARN" category
  // (anticipation entries) rendered with a bare "•" bullet and had no
  // filter chip — the cc map + filter list had drifted from nova_log()'s
  // real category set.
  const learnCallWS = hass.callWS;
  hass.callWS = async (m) => (m.type === "nova/get_debug_log"
    ? { entries: [{ ts: "15:44:58", cat: "LEARN", msg: "anticipation: Rachel is heading home — about 0.8 km out." }] }
    : learnCallWS(m));
  await el._fetchDebugLog();
  checks.push(["LEARN entries get their own icon/color, not a bare bullet",
    (() => {
      const catEl = el.shadowRoot.querySelector(".log-entry .log-cat");
      return !!catEl && /🧠/.test(catEl.textContent) && !/^•/.test(catEl.textContent.trim());
    })()]);
  hass.callWS = learnCallWS;
  el._logSearch = "";
  await el._fetchDebugLog();

  el._logSearch = "porch";
  await el._fetchDebugLog();
  const logs2 = el.shadowRoot;
  checks.push(
    ["search narrows to matching entries", logs2.querySelectorAll(".log-entry").length === 2],
    ["search excludes non-matching entry", !/camera\.front unavailable/.test(logs2.getElementById("debug-log-entries")?.textContent || "")],
    ["log count reflects filtered/total", /2 of 3/.test(logs2.getElementById("log-count")?.textContent || "")],
  );

  el._logSearch = "nonexistent-term-xyz";
  await el._fetchDebugLog();
  checks.push(
    ["search with no matches shows empty state, not a blank pane",
      /No entries match/.test(el.shadowRoot.getElementById("debug-log-entries")?.textContent || "")],
  );

  // ── stored XSS regression (fixed 11 Sept 2026): log entries are built with
  // innerHTML from e.msg/e.cat/e.ts, which can carry entity names, states, or
  // model output — content Nova doesn't fully control. A malicious payload in
  // any of those fields must render as inert text, never as a real element. ──
  el._logSearch = "";
  const _realCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/get_debug_log") return { entries: [
      { ts: "09:02:00", cat: "AGENT", msg: '<img src=x onerror="window.__xssFired=true">' },
    ] };
    return _realCallWS(m);
  };
  window.__xssFired = false;
  await el._fetchDebugLog();
  hass.callWS = _realCallWS;
  const logsXss = el.shadowRoot;
  checks.push(
    ["XSS payload in log msg does not create a live <img> element",
      !logsXss.querySelector("#debug-log-entries img")],
    ["XSS payload renders as literal escaped text instead",
      /<img src=x onerror=/.test(logsXss.getElementById("debug-log-entries")?.textContent || "")],
    ["XSS payload's onerror handler never actually ran", window.__xssFired === false],
  );

  // ── stored XSS regression #2 (fixed 13 Sept 2026): the log FETCH ERROR path
  // (a callWS rejection, not a returned entry) also built innerHTML by string-
  // concatenating the raw error object — a different code path than the one
  // above, so needs its own coverage. ──
  hass.callWS = async (m) => {
    if (m.type === "nova/get_debug_log") {
      throw new Error('<img src=x onerror="window.__xssFired2=true">');
    }
    return _realCallWS(m);
  };
  window.__xssFired2 = false;
  await el._fetchDebugLog();
  hass.callWS = _realCallWS;
  const logsErrXss = el.shadowRoot;
  checks.push(
    ["fetch-error XSS payload does not create a live <img> element",
      !logsErrXss.querySelector("#debug-log-entries img")],
    ["fetch-error XSS payload renders as literal escaped text instead",
      /<img src=x onerror=/.test(logsErrXss.getElementById("debug-log-entries")?.textContent || "")],
    ["fetch-error XSS payload's onerror handler never actually ran", window.__xssFired2 === false],
  );

  // ── pattern-engine suggestion: approve installs the automation (v6.52.0) ──
  // v7.81.0: suggestions live on their own tab now, not the dashboard.
  el._currentTab = "suggestions";
  el._render();
  const sugCard = el.shadowRoot.querySelector('.sug[data-sug-id="11"]');
  checks.push(
    ["suggestion card renders with approve button",
      !!sugCard && !!sugCard.querySelector(".sug-approve")],
    ["suggestion shows the why headline",
      !!sugCard && /A daily routine around 18:00/.test(sugCard.textContent)],
    ["suggestion shows observed evidence",
      !!sugCard && /What Nova observed/.test(sugCard.textContent)
        && /Happened 6 times/.test(sugCard.textContent)],
    ["suggestion shows a typed pattern chip",
      !!sugCard && !!sugCard.querySelector(".sug-type")],
    ["suggestion shows the entity involved",
      !!sugCard && /light\.porch/.test(sugCard.textContent)],
  );
  sugCard?.querySelector(".sug-approve")?.click();
  await new Promise(r => setTimeout(r, 20));
  const toastEl = el.shadowRoot.querySelector(".toast, #toast, .nova-toast");
  checks.push(
    ["approve sends suggestion_action", _sugCalls.length === 1
      && _sugCalls[0].id === 11 && _sugCalls[0].action === "approve"],
    ["approved card is visually retired", sugCard?.style.opacity === "0.35"],
  );

  // v7.81.0: Suggestions is its own tab, with an empty state.
  checks.push(
    ["Suggestions tab button present", /data-tab="suggestions"/.test(el._html())],
    ["suggestions empty state renders when none",
      /sug-empty-title/.test(el._renderSuggestions({ suggestions: [] }))],
    ["numeric_trigger has a typed label",
      /SENSOR THRESHOLD/.test(el._renderSuggestions({ suggestions: [
        { id: 99, pattern_type: "numeric_trigger", description: "x",
          confidence: 0.8, count: 9, entities: [], evidence: [] }] }))],
  );

  // ── mmWave presence overview on the residence tab (v6.53.0) ──
  el._currentTab = "residence";
  el._render();
  await el._fetchMmwave();
  const mmList = el.shadowRoot.getElementById("mmwave-list");
  const mmText = mmList?.textContent || "";
  const mmSummary = el.shadowRoot.getElementById("mmwave-summary")?.textContent || "";
  checks.push(
    ["mmWave panel renders a row per sensor-equipped room",
      mmList?.querySelectorAll(".mmwave-room").length === 3],
    ["occupied room is marked live", !!mmList?.querySelector(".mmwave-room.live .mmwave-dot.on")],
    ["occupied room shows OCCUPIED + now", /Kitchen/.test(mmText) && /OCCUPIED/.test(mmText)],
    ["clear room shows freshness age", /Office/.test(mmText) && /12m/.test(mmText)],
    ["outdoor room tagged", /Patio/.test(mmText) && /OUT/.test(mmText)],
    ["summary reflects detecting/total", /1\/3 OCCUPIED/.test(mmSummary)],
  );

  // ── mmWave glow feeds the floor plan itself (v6.54.0) ──
  // Kitchen is detecting in the mock; _house3dLit must mark it 'mmwave', a
  // distinct state from plain area-occupancy, so the plan glows accordingly.
  const litMap = el._house3dLit();
  checks.push(
    ["detecting room enters floor-plan lit map as 'mmwave'",
      litMap["kitchen"] === "mmwave"],
    ["non-detecting sensor room is not force-lit by mmwave",
      litMap["office"] !== "mmwave"],
  );

  // ── Document Library (RAG) panel on Settings (v6.55.0) ──
  el._currentTab = "settings";
  el._render();
  await el._fetchDocLibrary();
  const docBody = el.shadowRoot.getElementById("doclib-body");
  const docStatus = el.shadowRoot.getElementById("doclib-status");
  checks.push(
    ["doc library shows backend + chunk count", /VECTOR/.test(docStatus?.textContent || "") && /42/.test(docStatus?.textContent || "")],
    ["doc library lists ingested sources",
      /furnace_manual\.pdf/.test(docBody?.textContent || "") && /dishwasher_receipt\.txt/.test(docBody?.textContent || "")],
  );
  // ingest button
  el.shadowRoot.getElementById("doclib-ingest")?.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(
    ["doc ingest button present + wired", !!el.shadowRoot.getElementById("doclib-ingest")],
  );
  // test-search
  const dq = el.shadowRoot.getElementById("doclib-q");
  if (dq) {
    dq.value = "furnace filter size";
    dq.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise(r => setTimeout(r, 20));
  }
  checks.push(
    ["doc search renders excerpt with source + score",
      /16x25x1/.test(el.shadowRoot.getElementById("doclib-body")?.textContent || "")
      && /furnace_manual\.pdf/.test(el.shadowRoot.getElementById("doclib-body")?.textContent || "")],
  );

  // ── semantic search banner (Ollama embeddings, v6.57.0) ──
  await el._fetchVectorBackend();
  const vbState = el.shadowRoot.getElementById("vecbk-state")?.textContent || "";
  const vbBtn = el.shadowRoot.getElementById("vecbk-toggle");
  checks.push(
    ["semantic banner shows KEYWORD when not enabled", /KEYWORD/.test(vbState)],
    ["enable button offered when Ollama configured",
      vbBtn && vbBtn.style.display !== "none" && /ENABLE/.test(vbBtn.textContent)],
  );
  // enabling flips to SEMANTIC and offers a disable toggle
  vbBtn?.click();
  await new Promise(r => setTimeout(r, 20));
  const vbState2 = el.shadowRoot.getElementById("vecbk-state")?.textContent || "";
  const vbBtn2 = el.shadowRoot.getElementById("vecbk-toggle");
  checks.push(
    ["after enable the banner reflects SEMANTIC (Ollama)", /SEMANTIC/.test(vbState2)],
    ["disable toggle offered once semantic active",
      vbBtn2 && /DISABLE/.test(vbBtn2.textContent)],
  );

  // ── Intrusion / Security panel (own tab, v7.73.0) ──
  el._currentTab = "intrusion";
  el._render();
  await el._fetchIntrusion();
  const intrBody = el.shadowRoot.getElementById("intr-body")?.innerHTML || "";
  const intrStatus = el.shadowRoot.getElementById("intr-status")?.textContent || "";
  checks.push(
    ["intrusion panel shows ARMED status", /ARMED/.test(intrStatus)],
    ["intrusion panel renders the last snapshot image", /intrusion_dining_room/.test(intrBody) && /intr-img/.test(intrBody)],
    ["intrusion call-off button present", !!el.shadowRoot.querySelector(".intr-dismiss")],
    ["intrusion acknowledge button present", !!el.shadowRoot.querySelector(".intr-ack-btn")],
    ["intrusion response-timeout selector present", !!el.shadowRoot.querySelector('[data-cfg-key="intrusion_response_timeout"]')],
  );
  el.shadowRoot.querySelector(".intr-dismiss")?.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(
    ["calling off intrusion flips status to CALLED OFF",
      /CALLED OFF/.test(el.shadowRoot.getElementById("intr-status")?.textContent || "")],
  );

  // ── Wellbeing Context panel (v6.63.0) ──
  el._currentTab = "settings";
  el._render();
  await el._fetchBio();
  const bioStatus = el.shadowRoot.getElementById("bio-status")?.textContent || "";
  checks.push(
    ["wellbeing panel shows OFF by default", /OFF/.test(bioStatus)],
    ["wellbeing enable button present", !!el.shadowRoot.getElementById("bio-toggle")],
  );
  // enabling reveals discovered wearable entities
  el.shadowRoot.getElementById("bio-toggle")?.click();
  await new Promise(r => setTimeout(r, 20));
  const bioBody2 = el.shadowRoot.getElementById("bio-body")?.textContent || "";
  const bioStatus2 = el.shadowRoot.getElementById("bio-status")?.textContent || "";
  checks.push(
    ["enabling wellbeing turns status ON", /ON/.test(bioStatus2)],
    ["wellbeing lists discovered biometric readings", /heart rate/i.test(bioBody2) && /62/.test(bioBody2)],
  );

  // ── Energy Management panel (v6.62.0) ──
  await el._fetchEnergy();
  const energyDraw = el.shadowRoot.getElementById("energy-draw")?.textContent || "";
  const energyBody = el.shadowRoot.getElementById("energy-body")?.textContent || "";
  checks.push(
    ["energy panel shows draw + over-peak", /9\.2 kW/.test(energyDraw) && /OVER PEAK/.test(energyDraw)],
    ["energy panel lists running loads", /Dryer/.test(energyBody) && /Refrigerator/.test(energyBody)],
    ["energy panel marks protected loads", /protected/.test(energyBody)],
    ["energy panel shows advice", /over your peak/.test(energyBody)],
    ["energy agency chips present", el.shadowRoot.querySelectorAll("#energy-agency .mode-chip").length === 3],
  );

  // ── Solar panel (v7.91.0) — lives on the Command Center (dashboard) tab ──
  el._currentTab = "dashboard";
  el._render();
  await el._fetchSolar();
  const solarSufficiency = el.shadowRoot.getElementById("solar-sufficiency")?.textContent || "";
  const solarBody = el.shadowRoot.getElementById("solar-body")?.textContent || "";
  checks.push(
    ["solar panel shows self-sufficiency", /100(\.0)?% self-sufficient/.test(solarSufficiency)],
    ["solar panel shows generation", /3\.20 kW/.test(solarBody)],
    ["solar panel shows grid export direction", /Exporting/.test(solarBody)],
    ["solar panel shows battery level", /82%/.test(solarBody)],
  );
  el._currentTab = "settings";
  el._render();

  // ── Room Speakers panel (v7.92.0) ──
  const roomSpeakerSelects = el.shadowRoot.querySelectorAll(".room-speaker-select");
  const livingRoomSel = Array.from(roomSpeakerSelects).find(s => s.getAttribute("data-area-id") === "living_room");
  const generalSel = el.shadowRoot.querySelector(".general-speaker-select");
  checks.push(
    ["room speakers card renders one dropdown per area", roomSpeakerSelects.length === 2],
    ["room speaker dropdown reflects the assigned entity", livingRoomSel?.value === "media_player.living_room_speaker"],
    ["general speaker dropdown reflects the configured fallback", generalSel?.value === "media_player.kitchen_speaker"],
  );

  // ── Operational Mode panel (Directive Layer, v6.61.0) ──
  await el._fetchMode();
  const modeActive = el.shadowRoot.getElementById("mode-active")?.textContent || "";
  const modeGrid = el.shadowRoot.getElementById("mode-grid")?.textContent || "";
  checks.push(
    ["mode panel shows active mode", /NORMAL/.test(modeActive)],
    ["mode panel lists selectable modes", /party/.test(modeGrid) && /movie/.test(modeGrid) && /away/.test(modeGrid)],
  );
  // switching mode updates the active tag
  const partyChip = [...el.shadowRoot.querySelectorAll(".mode-chip")].find(b => b.dataset.mode === "party");
  if (partyChip) { partyChip.click(); await new Promise(r => setTimeout(r, 20)); }
  checks.push(
    ["selecting a mode updates active",
      /PARTY/.test(el.shadowRoot.getElementById("mode-active")?.textContent || "")],
  );

  // ── System Diagnostics panel (v6.60.0) ──
  await el._fetchDiagnostics();
  const diagBody = el.shadowRoot.getElementById("diag-body")?.textContent || "";
  const diagOverall = el.shadowRoot.getElementById("diag-overall")?.textContent || "";
  checks.push(
    ["diagnostics lists all four core services",
      /LLM/.test(diagBody) && /Embeddings/.test(diagBody) && /TTS/.test(diagBody) && /STT/.test(diagBody)],
    ["diagnostics shows per-service detail", /reachable/.test(diagBody)],
    ["diagnostics overall summary rendered", /HEALTHY|WARN|DOWN/i.test(diagOverall)],
    ["diagnostics run-check button present", !!el.shadowRoot.getElementById("diag-refresh")],
  );

  // ── every config toggle must be WIRED, not just rendered (v6.76.1) ──
  // A button with data-cfg-key but no data-cfg-val is inert: the click handler
  // scopes to [data-cfg-key][data-cfg-val], so it renders fine and does nothing.
  const inertToggles = [...el.shadowRoot.querySelectorAll("button[data-cfg-key]")]
    .filter(b => !b.hasAttribute("data-cfg-val"))
    .map(b => b.getAttribute("data-cfg-key"));
  checks.push(
    ["no inert config toggles (every button has data-cfg-val)",
      inertToggles.length === 0 || `inert: ${inertToggles.join(", ")}`],
    ["hazard master toggle is wired",
      !!el.shadowRoot.querySelector('button[data-cfg-key="hazard_monitor_enabled"][data-cfg-val]')],
    ["hazard feed toggles are wired",
      !!el.shadowRoot.querySelector('button[data-cfg-key="hazard_quakes_on"][data-cfg-val]') &&
      !!el.shadowRoot.querySelector('button[data-cfg-key="hazard_weather_on"][data-cfg-val]') &&
      !!el.shadowRoot.querySelector('button[data-cfg-key="hazard_disasters_on"][data-cfg-val]')],
  );

  // ── Intrusion Log + training (v6.76.0) ──
  el._currentTab = "intrusion";
  el._render();
  await el._wireIntrusionLog();
  const ilogBody = el.shadowRoot.getElementById("ilog-body")?.textContent || "";
  const ilogSide = el.shadowRoot.getElementById("ilog-learn")?.textContent || "";
  checks.push(
    ["intrusion log card present", !!el.shadowRoot.getElementById("ilog-body")],
    ["intrusion log renders events", /kitchen window/.test(ilogBody)],
    ["intrusion log shows event kinds", !!el.shadowRoot.querySelector(".ilog-confirmed") && !!el.shadowRoot.querySelector(".ilog-unresolved")],
    ["intrusion log shows snapshot image", !!el.shadowRoot.querySelector(".ilog-snap")],
    ["intrusion log has real/false label buttons",
      !!el.shadowRoot.querySelector(".ilog-real") && !!el.shadowRoot.querySelector(".ilog-false")],
    ["existing label is reflected", !!el.shadowRoot.querySelector(".ilog-false.ilog-on")],
    ["learned-benign summary surfaces", /learned 1 benign pattern/i.test(ilogBody)],
    ["label counter in header", /1\/2 LABELLED/.test(ilogSide)],
  );

  // ── Multi-Hazard Monitor panel (v6.71.0) ──
  el._currentTab = "settings";
  el._render();
  await el._wireHazard();
  const hazLoc = el.shadowRoot.getElementById("haz-loc")?.textContent || "";
  const hazOverall = el.shadowRoot.getElementById("hazard-overall")?.textContent || "";
  checks.push(
    ["hazard card present with enable toggle", !!el.shadowRoot.querySelector('[data-cfg-key="hazard_monitor_enabled"]')],
    ["vision model row carries the image-capable hint",
      (() => { const row = el.shadowRoot.querySelector('.model-row[data-role="vision"]');
        return !!row && /image-capable/.test(row.textContent) && !!row.querySelector('.model-hint'); })()],
    ["non-vision model rows have no hint",
      !el.shadowRoot.querySelector('.model-row[data-role="reasoning"] .model-hint')],
    ["anticipation & memory card present", !!el.shadowRoot.querySelector('[data-cfg-key="departure_alerts_enabled"]')],
    ["anticipation exposes memory + continued-conv toggles",
      !!el.shadowRoot.querySelector('[data-cfg-key="memory_threading_enabled"]') &&
      !!el.shadowRoot.querySelector('[data-cfg-key="continued_conversation_enabled"]')],
    ["multi-satellite follow toggle present",
      !!el.shadowRoot.querySelector('[data-cfg-key="continued_conversation_multi_satellite"][data-cfg-val]')],
    ["button-learning toggle present",
      !!el.shadowRoot.querySelector('[data-cfg-key="pattern_learn_buttons"][data-cfg-val]')],
    ["anticipation numeric config wired", !!el.shadowRoot.querySelector('[data-cfg-key="departure_lead_minutes"]')],
    ["anticipation toggles carry data-cfg-val", !!el.shadowRoot.querySelector('[data-cfg-key="routine_alerts_enabled"][data-cfg-val]')],
    ["hazard card has three feed toggles",
      !!el.shadowRoot.querySelector('[data-cfg-key="hazard_quakes_on"]') &&
      !!el.shadowRoot.querySelector('[data-cfg-key="hazard_weather_on"]') &&
      !!el.shadowRoot.querySelector('[data-cfg-key="hazard_disasters_on"]')],
    ["hazard card has location override inputs",
      !!el.shadowRoot.querySelector('[data-cfg-key="hazard_lat"]') &&
      !!el.shadowRoot.querySelector('[data-cfg-key="hazard_lon"]')],
    ["hazard status resolves the monitoring center", /40\.77/.test(hazLoc)],
    ["hazard overall reflects enabled state", /ON|OFF/.test(hazOverall)],
    ["hazard scan-now button present", !!el.shadowRoot.getElementById("haz-scan")],
  );
  // exercise a live scan render
  const hazScanBtn = el.shadowRoot.getElementById("haz-scan");
  if (hazScanBtn) {
    hazScanBtn.click();
    await new Promise(r => setTimeout(r, 30));
    const hazBody = el.shadowRoot.getElementById("haz-body")?.textContent || "";
    checks.push(["hazard scan renders quake + weather + disaster",
      /M3\.4/.test(hazBody) && /Tornado/.test(hazBody) && /Wildfire/.test(hazBody)]);
  }

  // v7.84.0: Settings sub-navigation groups cards into sections.
  const _card = (title) => [...el.shadowRoot.querySelectorAll(".settings-grid > .panel")]
    .find(p => (p.querySelector(".head span")?.textContent || "").trim() === title);
  const _subnav = el.shadowRoot.querySelectorAll(".settings-subnav-btn");
  el._settingsSection = "safety";
  el._applySettingsSections();
  const _haz = _card("Hazard Monitor");
  const _gen = _card("General");
  checks.push(
    ["settings sub-nav renders section buttons", _subnav.length >= 5],
    ["sub-nav shows the active section's cards", !!_haz && _haz.style.display !== "none"],
    ["sub-nav hides other sections' cards", !!_gen && _gen.style.display === "none"],
  );
  // v7.84.2 regression guard: sub-nav must survive UI localization. _localizeDOM
  // rewrites heading text AFTER render, so sections are stamped (data-section)
  // from the English heading at render time. Simulate a translated heading and
  // confirm the card still resolves to its section instead of stranding in
  // "general" (which left every non-General sub-tab empty for non-English users).
  const _hazSpan = _haz && _haz.querySelector(".head span");
  if (_hazSpan) _hazSpan.textContent = "Surveillance des dangers"; // FR; not in MAP
  el._settingsSection = "safety";
  el._applySettingsSections();
  checks.push(
    ["sub-nav section survives a translated heading (localized UI)",
      !!_haz && _haz.dataset.section === "safety" && _haz.style.display !== "none"],
  );
  el._settingsSection = "general";
  el._applySettingsSections();

  // v7.92.1 regression guard: every Settings card must be in the heading→
  // section MAP, or it silently strands in "general" instead of its logical
  // section (exactly what happened to "Room Speakers" when it first shipped —
  // caught only by a live user report, not by this suite, since an unmapped
  // card still renders fine, just in the wrong tab).
  const _roomSpk = _card("Room Speakers");
  el._settingsSection = "voice";
  el._applySettingsSections();
  checks.push(
    ["Room Speakers card is mapped to its section, not stranded in General",
      !!_roomSpk && _roomSpk.dataset.section === "voice" && _roomSpk.style.display !== "none"],
  );
  el._settingsSection = "general";
  el._applySettingsSections();

  // Panel look (v7.93.0) is deliberately NOT in Classic's own Settings —
  // it lives in the HA integration's Configure dialog (config_flow.py's
  // Core step) instead, per Abi's feedback that Classic's General section
  // was already busy. Classic's Settings should have no such control.
  checks.push(["panel look control is not in Classic's Settings (moved to Configure)",
    !el.shadowRoot.getElementById("ui-style-select")]);

  // v7.85.1: option builders must tolerate a stale/missing selected entity (a
  // removed entity still referenced in config). This threw and blanked the whole
  // panel — _travelSensorOptions('sensor.gone') reading undefined.attributes.
  let _builderSafe = true;
  try {
    el._travelSensorOptions("sensor.does_not_exist_xyz");
    el._trackerOptions("person.does_not_exist_xyz");
    el._doorEntityOptions("binary_sensor.does_not_exist_xyz");
  } catch (_e) { _builderSafe = false; }
  checks.push(["entity option builders tolerate a stale selected entity", _builderSafe]);

  // v7.85.0: Excluded entities card — three chip pickers + maps to Learning.
  const _exclCard = _card("Excluded Entities");
  checks.push(
    ["excluded-entities card renders three pickers",
      !!el.shadowRoot.querySelector("#excl-ent-add")
      && !!el.shadowRoot.querySelector("#excl-dom-add")
      && !!el.shadowRoot.querySelector("#excl-lab-add")],
    ["excluded-entities card maps to the Learning section",
      !!_exclCard && _exclCard.dataset.section === "learning"],
  );

  // ── New Command Center look (v7.93.0) — genuinely separate component ──
  const elNew = window.document.createElement("nova-panel-new");
  window.document.body.appendChild(elNew);
  elNew.hass = hass;
  await new Promise(r => setTimeout(r, 60));
  const newRoot = elNew.shadowRoot;
  const newText = newRoot.innerHTML;
  checks.push(
    ["new look renders the brand + hero", /Nova/.test(newText) && !!newRoot.getElementById("core")],
    ["new look shows status chips from real panel data",
      /Observer/.test(newText) && /RUNNING/.test(newText)],
    ["new look renders the activity feed", /motion in kitchen/.test(newText)],
    ["new look renders areas", /Garage/.test(newText) || /Kitchen/.test(newText)],
    ["new look shows a live dot only on occupied areas",
      newRoot.querySelectorAll(".area-tile.active .live-dot").length === 2
      && newRoot.querySelectorAll(".area-tile:not(.active) .live-dot").length === 0],
    ["new look's camera card is hidden with no cameras configured or shown with some",
      !!newRoot.getElementById("cameraPanel")],
  );

  // ── New look: Settings tab (v7.94.0) ──
  // Patching `global`, not `window`: the component code runs via
  // window.eval() but this harness only copies specific globals once at
  // startup (see the forEach a few lines up) rather than giving eval'd
  // code true window scope, so a bare `cancelAnimationFrame(...)` call
  // inside the component resolves through Node's `global`, not `window`
  // (the same reason nova-panel.js's own reload code had to say
  // window.location.reload() explicitly instead of the bare form).
  let _cafCalls = 0;
  const _realCaf = global.cancelAnimationFrame.bind(global);
  global.cancelAnimationFrame = (h) => { _cafCalls++; return _realCaf(h); };

  const settingsTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "settings");
  settingsTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  let sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab renders the search box and group nav",
      !!sRoot.getElementById("settingsSearch") && sRoot.querySelectorAll(".settings-nav-btn").length === 6],
    ["settings tab has one card per Classic setting, General real",
      sRoot.querySelectorAll(".settings-card").length === 26
      && /Sleep state/.test(sRoot.innerHTML) && /Announcements/.test(sRoot.innerHTML)],
    ["settings tab: Room Speakers card is real, not a stub",
      (() => {
        const rs = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Room Speakers/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!rs && !rs.querySelector(".stub-tag") && rs.querySelectorAll(".new-room-speaker-select").length === 2;
      })()],
    ["settings tab: Residence / Home card is real, not a stub",
      (() => {
        const rh = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Residence \/ Home/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!rh && !rh.querySelector(".stub-tag")
          && rh.querySelector('select[data-cfg-key="residence_style"]')
          && rh.querySelector('input[data-cfg-key="home_bedrooms"]')
          && rh.querySelector('button[data-cfg-key="has_basement"]');
      })()],
    ["settings tab: numeric Residence field autosaves as a Number, matching Classic",
      (() => {
        const sqftInput = sRoot.querySelector('input[data-cfg-key="floor_plan_sqft"]');
        sqftInput.value = "2200";
        sqftInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
        const call = _updateConfigCalls.find(c => c.key === "floor_plan_sqft");
        return !!call && call.value === 2200;
      })()],
    ["settings tab: Operational Mode card is real and shows the active mode",
      (() => {
        const om = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Operational Mode/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!om && !om.querySelector(".stub-tag")
          && om.querySelectorAll(".mode-chip[data-mode]").length >= 2
          && om.querySelectorAll(".mode-chip-on").length === 1;
      })()],
    ["settings tab: unbuilt cards are honestly labeled, not silently missing",
      (() => {
        // Floor Plan Editor stays a stub permanently (a full SVG drag-and-
        // drop editor tied to Classic's Residence 3D view, out of scope for
        // the same reason that tab is Classic-only) — a durable pointer,
        // unlike the temporary ones this check used while other cards were
        // still being built out.
        const fpeCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Floor Plan Editor/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!fpeCard && !!fpeCard.querySelector(".stub-tag") && /Configure/.test(fpeCard.textContent);
      })()],
    ["settings tab shows only the active group by default",
      Array.from(sRoot.querySelectorAll('.settings-card[data-settings-group="general"]')).every(c => !c.hidden)
      && Array.from(sRoot.querySelectorAll('.settings-card:not([data-settings-group="general"])')).every(c => c.hidden)],
  );

  // Diagnostics card: fetched once on entering Settings (async), so give it
  // a beat to land and re-render before asserting on its content.
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Diagnostics card is real and merges Classic's two diagnostics cards",
      (() => {
        const diagCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /^Diagnostics$/.test(c.querySelector(".panel-title")?.textContent?.trim() || ""));
        return !!diagCard && !diagCard.querySelector(".stub-tag")
          && /LLM/.test(diagCard.textContent) && /TTS — Nova voice test/.test(diagCard.textContent)
          && !!diagCard.querySelector('[data-svc="nova.test_tts"]');
      })()],
    ["settings tab: Diagnostics service-test button calls the HA service",
      (() => {
        const ttsBtn = sRoot.querySelector('[data-svc="nova.test_tts"]');
        ttsBtn.click();
        return true; // assert the resulting call below, after the microtask settles
      })()],
  );
  await new Promise(r => setTimeout(r, 10));
  checks.push(["settings tab: Diagnostics service call reached hass.callService",
    _serviceCalls.some(c => c.domain === "nova" && c.service === "test_tts")]);

  // Toggling a real General setting saves through the same nova/update_config
  // contract Classic uses.
  const announceToggle = sRoot.querySelector('.toggle-btn[data-cfg-key="announcements_enabled"]');
  announceToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab toggle saves via nova/update_config",
    _updateConfigCalls.some(c => c.key === "announcements_enabled")]);
  sRoot = elNew.shadowRoot;

  // Clicking a mode chip calls nova/mode (not nova/update_config — a
  // separate, pre-existing websocket contract Classic's own mode-grid uses).
  const newPartyChip = Array.from(sRoot.querySelectorAll(".mode-chip[data-mode]")).find(b => b.getAttribute("data-mode") === "party");
  newPartyChip.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: mode chip click calls nova/mode set",
    _modeSetCalls.some(c => c.mode === "party")]);
  sRoot = elNew.shadowRoot;

  // Search crosses group boundaries
  const searchBox = sRoot.getElementById("settingsSearch");
  searchBox.value = "camera";
  searchBox.dispatchEvent(new Event("input"));
  await new Promise(r => setTimeout(r, 5));
  sRoot = elNew.shadowRoot;
  checks.push(["settings search surfaces matches from other groups",
    Array.from(sRoot.querySelectorAll(".settings-card")).some(c =>
      !c.hidden && /Cameras/.test(c.querySelector(".panel-title")?.textContent || ""))]);

  // AI Models: switch to its group, confirm it's real (not a stub) with all
  // six roles rendered, live models loaded from nova/list_models, and the
  // vision-role hint present — then exercise the provider-change flow
  // (self-heal + llm_base_url clear), which deliberately does NOT go
  // through _saveSetting/_render (see _wireAiModels's own comment).
  const voiceNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Voice & Speakers");
  voiceNavBtn.click();
  await new Promise(r => setTimeout(r, 30));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: AI Models card is real with all six roles and live models loaded",
      (() => {
        const aiCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /AI Models/.test(c.querySelector(".panel-title")?.textContent || ""));
        if (!aiCard || aiCard.querySelector(".stub-tag")) return false;
        const rows = aiCard.querySelectorAll(".new-model-row");
        const llmRow = aiCard.querySelector('.new-model-row[data-role="llm"] .new-model-select');
        return rows.length === 6
          && !!llmRow && /llama-3\.3-70b-versatile/.test(llmRow.innerHTML)
          && /image-capable model/.test(aiCard.querySelector('.new-model-row[data-role="vision"]')?.textContent || "");
      })()],
  );
  const llmProvSel = sRoot.querySelector('.new-model-row[data-role="llm"] .new-prov-select');
  llmProvSel.value = "openai";
  llmProvSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: AI Models provider change saves provider, clears base_url, and reloads its own model list",
    _updateConfigCalls.some(c => c.key === "llm_provider" && c.value === "openai")
    && _updateConfigCalls.some(c => c.key === "llm_base_url" && c.value === "")
    && /gpt-4o/.test(sRoot.querySelector('.new-model-row[data-role="llm"] .new-model-select')?.innerHTML || "")]);

  // Briefings: real card, schedule fields + include-feed chips autosave
  // through the same generic .cfg-field/.mode-chip[data-cfg-key] contract
  // everything else in this tab uses.
  checks.push(
    ["settings tab: Briefings card is real with schedule fields and include chips",
      (() => {
        const bc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Briefings/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!bc && !bc.querySelector(".stub-tag")
          && !!bc.querySelector('input[data-cfg-key="briefing_morning_time"]')
          && !!bc.querySelector('.mode-chip[data-cfg-key="briefing_include_weather"]')
          && !!bc.querySelector("#newBriefNow");
      })()],
  );
  const weatherChip = sRoot.querySelector('.mode-chip[data-cfg-key="briefing_include_weather"]');
  weatherChip.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Briefings include-chip autosaves via nova/update_config",
    _updateConfigCalls.some(c => c.key === "briefing_include_weather" && c.value === false)]);
  sRoot = elNew.shadowRoot;

  // Voice Confirmation: real card, mode select + test button call the same
  // nova/voice_confirm_test contract Classic's own test button uses.
  checks.push(
    ["settings tab: Voice Confirmation card is real with mode select and test button",
      (() => {
        const vc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Voice Confirmation/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!vc && !vc.querySelector(".stub-tag")
          && !!vc.querySelector('select[data-cfg-key="voice_confirm_mode"]')
          && !!vc.querySelector("#newVcTest");
      })()],
  );
  const vcTestBtn = sRoot.getElementById("newVcTest");
  vcTestBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Voice Confirmation test button reports the result",
    /assist_satellite\.basement_nova/.test(sRoot.getElementById("newVcTestResult")?.innerHTML || "")]);

  // Satellite → Speaker: real card, one row per satellite, same
  // satellite_pairings JSON-string contract as Classic and Room Speakers.
  checks.push(
    ["settings tab: Satellite → Speaker card is real with one row per satellite",
      (() => {
        const sc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Satellite/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!sc && !sc.querySelector(".stub-tag") && sc.querySelectorAll(".new-sat-pair-select").length === 1;
      })()],
  );
  const satSel = sRoot.querySelector(".new-sat-pair-select");
  satSel.value = "media_player.kitchen_speaker";
  satSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Satellite → Speaker pairing autosaves as a JSON string",
    _updateConfigCalls.some(c => c.key === "satellite_pairings"
      && c.value === JSON.stringify({ "assist_satellite.basement_nova": "media_player.kitchen_speaker" }))]);
  sRoot = elNew.shadowRoot;

  // Announcement Speakers: real card, one toggle per Cast device.
  checks.push(
    ["settings tab: Announcement Speakers card is real with one toggle per Cast device",
      (() => {
        const ac = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Announcement Speakers/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!ac && !ac.querySelector(".stub-tag") && ac.querySelectorAll(".new-ann-speaker-toggle").length === 2;
      })()],
  );
  const annToggle = sRoot.querySelector('.new-ann-speaker-toggle[data-speaker-id="media_player.living_room_speaker"]');
  annToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Announcement Speakers toggle autosaves as a JSON array",
    _updateConfigCalls.some(c => c.key === "announcement_speakers" && c.value === JSON.stringify(["media_player.living_room_speaker"]))]);
  sRoot = elNew.shadowRoot;

  // Notifications: switch to the Awareness & Safety group, confirm it's
  // real with the notify-service select populated, and that it saves
  // through the same generic .cfg-field contract as everything else.
  const safetyNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Awareness & Safety");
  safetyNavBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Notifications card is real with the notify-service select populated",
      (() => {
        const nc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /^Notifications$/.test(c.querySelector(".panel-title")?.textContent?.trim() || ""));
        const sel = nc?.querySelector('select[data-cfg-key="notify_service"]');
        return !!nc && !nc.querySelector(".stub-tag") && !!sel && /mobile_app_abi_phone/.test(sel.innerHTML);
      })()],
  );
  const notifySel = sRoot.querySelector('select[data-cfg-key="notify_service"]');
  notifySel.value = "notify.mobile_app_spouse_phone";
  notifySel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Notifications select autosaves via nova/update_config",
    _updateConfigCalls.some(c => c.key === "notify_service" && c.value === "notify.mobile_app_spouse_phone")]);
  sRoot = elNew.shadowRoot;

  // Sentinel Rules: real card, one toggle per rule, already-disabled rule
  // reflected as OFF, toggling re-enables it via disabled_sentinel_rules.
  checks.push(
    ["settings tab: Sentinel Rules card is real with per-rule toggles",
      (() => {
        const sc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Sentinel Rules/.test(c.querySelector(".panel-title")?.textContent || ""));
        const offBtn = sc?.querySelector('.new-rule-toggle[data-rule-id="garage_left_open"]');
        return !!sc && !sc.querySelector(".stub-tag")
          && sc.querySelectorAll(".new-rule-toggle").length === 2
          && !!offBtn && offBtn.classList.contains("off") && offBtn.textContent.trim() === "OFF";
      })()],
  );
  const garageRuleBtn = sRoot.querySelector('.new-rule-toggle[data-rule-id="garage_left_open"]');
  garageRuleBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Sentinel Rules toggle re-enables a disabled rule",
    _updateConfigCalls.some(c => c.key === "disabled_sentinel_rules" && c.value === JSON.stringify([]))]);
  sRoot = elNew.shadowRoot;

  // Hazard Monitor: status fetched once on entering Settings (async, like
  // Diagnostics), SCAN NOW re-checks USGS/NWS/EONET via nova/hazard.
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Hazard Monitor card is real and shows the resolved location",
      (() => {
        const hc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Hazard Monitor/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!hc && !hc.querySelector(".stub-tag")
          && /40\.77, -75\.61/.test(hc.textContent)
          && !!hc.querySelector("#newHazScan");
      })()],
  );
  const newHazScanBtn = sRoot.getElementById("newHazScan");
  newHazScanBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Hazard Monitor scan renders quake + weather + disaster results",
    (() => {
      const body = sRoot.getElementById("newHazBody");
      const t = body?.textContent || "";
      return /12km N of town/.test(t) && /Tornado Warning/.test(t) && /Wildfire/.test(t);
    })()]);

  // Energy Management: status fetched once (like Diagnostics/Hazard), shows
  // current draw + running loads, and set_agency round-trips through
  // nova/energy (not update_config — a separate, pre-existing contract).
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Energy Management card is real and shows current draw + running loads",
      (() => {
        const ec = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Energy Management/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!ec && !ec.querySelector(".stub-tag")
          && /9\.2 kW/.test(ec.textContent) && /OVER PEAK/.test(ec.textContent)
          && /Dryer/.test(ec.textContent) && /Refrigerator/.test(ec.textContent)
          && ec.querySelectorAll('#newEnergyAgency .mode-chip[data-agency]').length === 3
          && ec.querySelector('.mode-chip[data-agency="advisory"]').classList.contains("mode-chip-on");
      })()],
  );
  const autonomousChip = sRoot.querySelector('.mode-chip[data-agency="autonomous"]');
  autonomousChip.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["settings tab: Energy Management agency change reflects live via nova/energy",
    sRoot.querySelector('.mode-chip[data-agency="autonomous"]')?.classList.contains("mode-chip-on")]);

  // Appliances: batch-edit-then-save, like Classic and AI Models — add a
  // row, fill it in, Save persists the WHOLE list as one JSON array plus a
  // nova/reload_appliances call, without wiping the row mid-edit (the
  // reason this card deliberately avoids _saveSetting's auto-render).
  checks.push(
    ["settings tab: Appliances card is real with the declared row present",
      (() => {
        const ac = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Appliances/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!ac && !ac.querySelector(".stub-tag")
          && ac.querySelectorAll(".new-appliance-row").length === 1
          && ac.querySelector(".new-appliance-name")?.value === "Dryer";
      })()],
  );
  const applianceAddBtn = sRoot.getElementById("newApplianceAdd");
  applianceAddBtn.click();
  const newRows = sRoot.querySelectorAll(".new-appliance-row");
  checks.push(["settings tab: Appliances + Add appliance inserts a new row without wiping the existing one",
    newRows.length === 2 && newRows[0].querySelector(".new-appliance-name").value === "Dryer"]);
  newRows[1].querySelector(".new-appliance-name").value = "Oven";
  newRows[1].querySelector(".new-appliance-watts").value = "3000";
  const applianceSaveBtn = sRoot.getElementById("newApplianceSave");
  applianceSaveBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Appliances Save persists the whole list as one JSON array",
    _updateConfigCalls.some(c => c.key === "appliance_profile"
      && c.value === JSON.stringify([{ name: "Dryer", type: "dryer", entity: "", watts: 4200 }, { name: "Oven", type: "appliance", entity: "", watts: 3000 }]))]);
  sRoot = elNew.shadowRoot;

  // Anticipation & Memory: switch to Learning & Memory group, confirm it's
  // real and fully generic (toggles + number fields autosave the same way
  // as every other generically-wired card).
  const learningNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Learning & Memory");
  learningNavBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Anticipation & Memory card is real with its toggles and number fields",
      (() => {
        const amCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Anticipation & Memory/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!amCard && !amCard.querySelector(".stub-tag")
          && !!amCard.querySelector('button[data-cfg-key="continued_conversation_enabled"]')
          && !!amCard.querySelector('input[data-cfg-key="memory_threading_hours"]');
      })()],
  );
  const memHoursInput = sRoot.querySelector('input[data-cfg-key="memory_threading_hours"]');
  memHoursInput.value = "72";
  memHoursInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Anticipation & Memory number field autosaves as a Number",
    _updateConfigCalls.some(c => c.key === "memory_threading_hours" && c.value === 72)]);
  sRoot = elNew.shadowRoot;

  // Memory: the small stats card Classic's own Settings tab actually has —
  // full review/edit is a separate Classic-only tab, out of scope here.
  checks.push(
    ["settings tab: Memory card is real and shows backend + stored count",
      (() => {
        const mc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => (c.querySelector(".panel-title")?.childNodes[0]?.textContent || "").trim() === "Memory");
        return !!mc && !mc.querySelector(".stub-tag") && /sqlite-vec/.test(mc.textContent) && /214/.test(mc.textContent);
      })()],
  );

  // Observer Tuning: real stats readout + the one editable field (hourly
  // cap), which saves to a DIFFERENT key (classifier_rate_limit) than it
  // reads (rate_limit) — a deliberate Classic asymmetry preserved here.
  checks.push(
    ["settings tab: Observer Tuning card is real with stats and the rate-limit input",
      (() => {
        const oc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Observer Tuning/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!oc && !oc.querySelector(".stub-tag")
          && /RUNNING/.test(oc.textContent) && /ONLINE/.test(oc.textContent)
          && !!oc.querySelector("#newObserverRateLimit");
      })()],
  );
  const rateLimitInput = sRoot.getElementById("newObserverRateLimit");
  rateLimitInput.value = "60";
  rateLimitInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Observer Tuning hourly cap saves to classifier_rate_limit as a Number",
    _updateConfigCalls.some(c => c.key === "classifier_rate_limit" && c.value === 60)]);
  sRoot = elNew.shadowRoot;

  // Routine Learning: real card with the doors/presence/buttons toggles and
  // the add-entity control. NOTE: Classic's own "routine-learning card" test
  // (above) sets `cfg.pattern_include_entities` and then `delete`s it again
  // as cleanup on the SAME shared PANEL.config object this section reuses —
  // so by here the key is gone and the chip list starts genuinely empty;
  // assert against that real state rather than the fixture's original value.
  checks.push(
    ["settings tab: Routine Learning card is real with the add-entity control",
      (() => {
        const rc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Routine Learning/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!rc && !rc.querySelector(".stub-tag")
          && !!rc.querySelector('button[data-cfg-key="pattern_learn_doors"]')
          && !!rc.querySelector("#newPlAddEntity") && !!rc.querySelector("#newPlEntityInput");
      })()],
  );
  const plInput = sRoot.getElementById("newPlEntityInput");
  plInput.value = "binary_sensor.mailbox";
  sRoot.getElementById("newPlAddEntity").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Routine Learning + Add appends the entity to pattern_include_entities",
    _updateConfigCalls.some(c => c.key === "pattern_include_entities" && c.value === JSON.stringify(["binary_sensor.mailbox"]))]);
  sRoot = elNew.shadowRoot;
  const plDelBtn = sRoot.querySelector(".new-pl-del");
  checks.push(["settings tab: Routine Learning shows the newly-added chip with a remove button",
    !!plDelBtn]);
  if (plDelBtn) {
    plDelBtn.click();
    await new Promise(r => setTimeout(r, 20));
    checks.push(["settings tab: Routine Learning chip removal updates pattern_include_entities",
      _updateConfigCalls.some(c => c.key === "pattern_include_entities" && c.value === JSON.stringify([]))]);
  }
  sRoot = elNew.shadowRoot;

  // Excluded Entities: three chip pickers (entities/domains/labels), each
  // using the same _exclSave pattern as Routine Learning's fix above
  // (write the array onto _liveData.config before the round-trip).
  checks.push(
    ["settings tab: Excluded Entities card is real with three pickers and the existing entity chip",
      (() => {
        const ec = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Excluded Entities/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!ec && !ec.querySelector(".stub-tag")
          && !!ec.querySelector("#newExclEntAdd") && !!ec.querySelector("#newExclDomAdd") && !!ec.querySelector("#newExclLabAdd")
          && /light\.spare_bedroom/.test(ec.textContent);
      })()],
  );
  sRoot.getElementById("newExclDomInput").value = "switch";
  sRoot.getElementById("newExclDomAdd").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Excluded Entities + Add domain saves excluded_domains",
    _updateConfigCalls.some(c => c.key === "excluded_domains" && c.value === JSON.stringify(["switch"]))]);
  sRoot = elNew.shadowRoot;
  const exclEntDelBtn = sRoot.querySelector(".new-excl-ent-del");
  checks.push(["settings tab: Excluded Entities re-renders the domain chip after adding",
    /switch/.test(sRoot.getElementById("newExclDomChips")?.textContent || "") && !!exclEntDelBtn]);
  exclEntDelBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Excluded Entities chip removal saves excluded_entities",
    _updateConfigCalls.some(c => c.key === "excluded_entities" && c.value === JSON.stringify([]))]);
  sRoot = elNew.shadowRoot;

  // Cameras: switch to the Cameras group, confirm the per-camera enable/
  // rename/location controls are real, and that they deliberately do NOT
  // route through the generic _saveSetting/_render round-trip (only the
  // #newCamsetBody sub-tree patches, matching Classic's #camset-body).
  const camerasNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Cameras");
  camerasNavBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Cameras card is real with two camera rows",
      (() => {
        const cc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /^Cameras$/.test(c.querySelector(".panel-title")?.textContent?.trim() || ""));
        return !!cc && !cc.querySelector(".stub-tag") && cc.querySelectorAll(".new-camset-row").length === 2
          && !!cc.querySelector("#newCamEnableAll");
      })()],
  );
  const camToggle = sRoot.querySelector('.new-cam-enable-toggle[data-cam="camera.front"]');
  camToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Cameras disable toggle saves disabled_cameras and re-renders just the camera list",
    _updateConfigCalls.some(c => c.key === "disabled_cameras" && c.value === JSON.stringify(["camera.front"]))
    && sRoot.querySelector('.new-cam-enable-toggle[data-cam="camera.front"]')?.classList.contains("off")]);
  sRoot = elNew.shadowRoot;
  const outdoorChip = sRoot.querySelector('.new-cam-loc-chip[data-cam="camera.front"][data-loc="outdoor"]');
  outdoorChip.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Cameras location chip calls nova/camera_location and reflects the result",
    _locationCalls.some(c => c.entity_id === "camera.front" && c.mode === "outdoor")
    && sRoot.querySelector('.new-cam-loc-chip[data-cam="camera.front"][data-loc="outdoor"]')?.classList.contains("mode-chip-on")]);
  const newCamNameInput = sRoot.querySelector('.new-camset-name[data-cam="camera.back"]');
  newCamNameInput.value = "Driveway";
  newCamNameInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("blur", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Cameras rename input calls nova/rename_camera on blur",
    _renameCalls.some(c => c.entity_id === "camera.back" && c.name === "Driveway")]);
  sRoot = elNew.shadowRoot;

  // Doorbell Training: real card, analysed-event stats + rows, and the
  // backlog-scan button calls the nova.train_doorbell_backlog service.
  checks.push(
    ["settings tab: Doorbell Training card is real with stats and event rows",
      (() => {
        const dc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Doorbell Training/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!dc && !dc.querySelector(".stub-tag")
          && /18 analysed/.test(dc.textContent) && /3 notable/.test(dc.textContent)
          && /Amazon box left at the door/.test(dc.textContent)
          && !!dc.querySelector("#newDbtScan");
      })()],
  );
  sRoot.getElementById("newDbtScan").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Doorbell Training scan button calls nova.train_doorbell_backlog",
    _serviceCalls.some(c => c.domain === "nova" && c.service === "train_doorbell_backlog" && c.data?.limit === 40)]);

  // Wellbeing Context: switch to Home & Extras group, fetched once (like
  // Diagnostics/Hazard/Energy). NOTE: Classic's own "wellbeing enable
  // button" test earlier already clicked bio-toggle and left the shared
  // _bioEnabled mock flag ON (no cleanup) — so this section starts from
  // ON/2-sensors, and the toggle click below turns it back OFF.
  const homeNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Home & Extras");
  homeNavBtn.click();
  await new Promise(r => setTimeout(r, 30));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Wellbeing Context card is real and lists wearable entities",
      (() => {
        const wc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Wellbeing Context/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!wc && !wc.querySelector(".stub-tag") && !!wc.querySelector("#newBioToggle")
          && /ON · 2 sensors/.test(wc.textContent) && /heart rate/.test(wc.textContent);
      })()],
  );
  sRoot.getElementById("newBioToggle").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["settings tab: Wellbeing Context toggle disables via nova/biometrics",
    (() => {
      const wc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Wellbeing Context/.test(c.querySelector(".panel-title")?.textContent || ""));
      return !!wc && /OFF/.test(wc.textContent);
    })()]);

  // Nova Character & Research: fully generic .cfg-field card, same as
  // Anticipation & Memory — banter level, search backend, SearXNG URL.
  checks.push(
    ["settings tab: Nova Character & Research card is real with its three fields",
      (() => {
        const crc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Nova Character & Research/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!crc && !crc.querySelector(".stub-tag")
          && !!crc.querySelector('select[data-cfg-key="banter_level"]')
          && !!crc.querySelector('select[data-cfg-key="search_backend"]')
          && !!crc.querySelector('input[data-cfg-key="searxng_url"]')
          && crc.querySelector('input[data-cfg-key="searxng_url"]').value === "http://sx.local:8080";
      })()],
  );
  const banterSel = sRoot.querySelector('select[data-cfg-key="banter_level"]');
  banterSel.value = "2";
  banterSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Nova Character & Research select autosaves via nova/update_config",
    _updateConfigCalls.some(c => c.key === "banter_level" && c.value === "2")]);
  sRoot = elNew.shadowRoot;

  // Document Library: fetched once (status + vector backend), ingest and
  // search both hit the same nova/documents contract Classic uses, delete
  // is gated on window.confirm (stubbed to auto-confirm above).
  await new Promise(r => setTimeout(r, 30));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Document Library card is real with sources + semantic-search hint",
      (() => {
        const dl = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Document Library/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!dl && !dl.querySelector(".stub-tag")
          && /furnace_manual\.pdf/.test(dl.textContent) && /VECTOR/.test(dl.textContent)
          && !!dl.querySelector("#newDoclibIngest") && !!dl.querySelector("#newVecbkToggle");
      })()],
  );
  const doclibSearch = sRoot.getElementById("newDoclibSearch");
  doclibSearch.value = "furnace filter size";
  doclibSearch.dispatchEvent(new sRoot.ownerDocument.defaultView.KeyboardEvent("keydown", { key: "Enter" }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Document Library search renders hit excerpts",
    /16x25x1 MERV 11/.test(sRoot.getElementById("newDoclibBody")?.textContent || "")]);
  sRoot.getElementById("newDoclibIngest").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["settings tab: Document Library ingest re-fetches status and restores the source list",
    /furnace_manual\.pdf/.test(sRoot.getElementById("newDoclibBody")?.textContent || "")]);
  sRoot.querySelector(".new-doclib-del").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Document Library delete calls nova/documents delete (window.confirm stubbed to auto-confirm)",
    _docDeleteCalls.includes("furnace_manual.pdf")]);

  // ── New look: Logs tab (ported from Classic's own System Log) ──
  const logsTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "logs");
  logsTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["logs tab renders all mock entries with a count",
      sRoot.querySelectorAll(".new-log-entry").length === 3
      && /3 entries/.test(sRoot.getElementById("newLogCount")?.textContent || "")],
    ["logs tab renders every real backend log category as a filter chip",
      sRoot.querySelectorAll(".new-log-filter").length === 20
      && !!sRoot.querySelector('.new-log-filter[data-filter="LEARN"]')
      && !sRoot.querySelector('.new-log-filter[data-filter="ROUTE"]')], // dead category, dropped
  );

  // Real gap Abi caught live from a screenshot: a genuine "LEARN" category
  // (anticipation entries) appeared in the log with a bare "•" bullet and
  // no way to filter for it — neither the filter-chip list nor the color
  // map had ever been updated to match nova_log()'s real category set.
  const originalCallWSForLearn = hass.callWS;
  hass.callWS = async (m) => (m.type === "nova/get_debug_log"
    ? { entries: [{ ts: "15:44:58", cat: "LEARN", msg: "anticipation: Rachel is heading home — about 0.8 km out." }] }
    : originalCallWSForLearn(m));
  await elNew._fetchDebugLog();
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: a LEARN entry gets its own icon/color, not a bare bullet",
    (() => {
      const catEl = sRoot.querySelector(".new-log-entry .new-log-cat");
      return !!catEl && /🧠/.test(catEl.textContent) && !/^•/.test(catEl.textContent.trim());
    })()]);
  const learnFilterBtn = sRoot.querySelector('.new-log-filter[data-filter="LEARN"]');
  learnFilterBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: LEARN filter chip actually isolates LEARN entries",
    sRoot.querySelectorAll(".new-log-entry").length === 1
    && sRoot.querySelector('.new-log-filter[data-filter="LEARN"]')?.classList.contains("mode-chip-on")]);
  hass.callWS = originalCallWSForLearn;
  elNew._logFilter = "all";
  await elNew._fetchDebugLog();
  const errFilterBtn = sRoot.querySelector('.new-log-filter[data-filter="ERROR"]');
  errFilterBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: category filter narrows to matching entries and marks itself active",
    sRoot.querySelectorAll(".new-log-entry").length === 1
    && /camera\.front unavailable/.test(sRoot.getElementById("newLogEntries")?.textContent || "")
    && sRoot.querySelector('.new-log-filter[data-filter="ERROR"]')?.classList.contains("mode-chip-on")
    && !sRoot.querySelector('.new-log-filter[data-filter="all"]')?.classList.contains("mode-chip-on")]);
  sRoot.querySelector('.new-log-filter[data-filter="all"]').click();
  await new Promise(r => setTimeout(r, 10));

  const logSearchInput = sRoot.getElementById("newLogSearch");
  logSearchInput.value = "porch";
  logSearchInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("input", { bubbles: true }));
  await new Promise(r => setTimeout(r, 250)); // debounced 200ms, same as Classic
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: search narrows entries and updates the count",
    sRoot.querySelectorAll(".new-log-entry").length === 2
    && !/camera\.front unavailable/.test(sRoot.getElementById("newLogEntries")?.textContent || "")
    && /2 of 3/.test(sRoot.getElementById("newLogCount")?.textContent || "")]);
  logSearchInput.value = "xyz-nonsense-term";
  logSearchInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("input", { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  checks.push(["logs tab: no-match search shows an honest empty state",
    /No entries match/.test(sRoot.getElementById("newLogEntries")?.textContent || "")]);
  logSearchInput.value = "";
  logSearchInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("input", { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));

  // Stored-XSS regressions ported verbatim from Classic (fixed 11+13 Sept
  // 2026 there) — log content (ts/cat/msg) is attacker/LLM-influenced, so
  // it MUST go through this._esc() before innerHTML. Two paths: a
  // malicious entry in a normal response, and a malicious error message
  // on a failed fetch.
  window.__xssFiredNew = false;
  const originalCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/get_debug_log") {
      return { entries: [{ ts: "09:00:00", cat: "CONV", msg: '<img src=x onerror="window.__xssFiredNew=true">' }] };
    }
    return originalCallWS(m);
  };
  await elNew._fetchDebugLog();
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: stored-XSS in a log entry is escaped, not executed",
    !sRoot.getElementById("newLogEntries")?.querySelector("img")
    && /onerror/.test(sRoot.getElementById("newLogEntries")?.textContent || "")
    && window.__xssFiredNew === false]);

  window.__xssFiredNew2 = false;
  hass.callWS = async (m) => {
    if (m.type === "nova/get_debug_log") throw new Error('<img src=x onerror="window.__xssFiredNew2=true">');
    return originalCallWS(m);
  };
  await elNew._fetchDebugLog();
  sRoot = elNew.shadowRoot;
  checks.push(["logs tab: stored-XSS in a fetch-error message is escaped, not executed",
    !sRoot.getElementById("newLogEntries")?.querySelector("img")
    && window.__xssFiredNew2 === false]);
  hass.callWS = originalCallWS;

  // ── New look: Memory tab (ported from Classic's own Memory tab) ──
  const memoryTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "memory");
  memoryTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["memory tab renders known facts grouped by subject, with the fact count",
      /trash day/.test(sRoot.getElementById("newMemList")?.textContent || "")
      && /favorite tea/.test(sRoot.getElementById("newMemList")?.textContent || "")
      && /Household/.test(sRoot.getElementById("newMemList")?.textContent || "")
      && /About me/.test(sRoot.getElementById("newMemList")?.textContent || "")
      && /2 facts/.test(sRoot.getElementById("newMemCount")?.textContent || "")],
    ["memory tab renders person routines with a confidence percentage",
      /Username/.test(sRoot.getElementById("newProutineList")?.textContent || "")
      && /office light turns on/.test(sRoot.getElementById("newProutineList")?.textContent || "")
      && /82%/.test(sRoot.getElementById("newProutineList")?.textContent || "")],
    // Classic's own Memory-tab test (earlier in this file) already confirmed
    // the one seed pending fact ("bedtime"), consuming the shared
    // _pendingFacts fixture — same class of shared-fixture gotcha as
    // pattern_include_entities/_bioEnabled above. Assert the real
    // now-empty state rather than the fixture's original value.
    ["memory tab hides the pending-confirmation panel once nothing is waiting",
      sRoot.getElementById("newPendingPanel")?.hidden === true],
  );

  // Teach a new fact — exercises nova/add_knowledge and confirms the
  // inputs clear on success (so the form is obviously ready for the next one).
  sRoot.getElementById("newMemKey").value = "wifi password hint";
  sRoot.getElementById("newMemVal").value = "ask the router";
  sRoot.getElementById("newMemAdd").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["memory tab: TEACH adds a fact via nova/add_knowledge and clears the form",
    /wifi password hint/.test(sRoot.getElementById("newMemList")?.textContent || "")
    && sRoot.getElementById("newMemKey")?.value === ""
    && sRoot.getElementById("newMemVal")?.value === ""]);

  const forgetBtn = sRoot.querySelector('.new-mem-forget[data-id="1"]');
  forgetBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["memory tab: forgetting a fact removes it via nova/forget_knowledge",
    !/trash day/.test(sRoot.getElementById("newMemList")?.textContent || "")]);

  // Pending Confirmation: inject a fresh pending fact (the shared fixture's
  // seed one is already consumed) to exercise confirm/reject/edit for real.
  const memoryCallWS = hass.callWS;
  hass.callWS = async (m) => (m.type === "nova/get_knowledge"
    ? { facts: _knownFacts, pending: [{ id: 99, key: "quiet hours", value: "10pm-7am", subject: "household" }], stats: {} }
    : memoryCallWS(m));
  await elNew._fetchKnowledge();
  sRoot = elNew.shadowRoot;
  checks.push(["memory tab: a pending fact shows the panel with confirm/reject/edit controls",
    sRoot.getElementById("newPendingPanel")?.hidden === false
    && /quiet hours/.test(sRoot.getElementById("newPendingList")?.textContent || "")
    && !!sRoot.querySelector(".new-pending-confirm") && !!sRoot.querySelector(".new-pending-reject")
    && !!sRoot.querySelector(".new-pending-save-edit")]);

  hass.callWS = async (m) => (m.type === "nova/pending_fact_action"
    ? { ok: true, facts: _knownFacts, pending: [] }
    : memoryCallWS(m));
  sRoot.querySelector(".new-pending-confirm").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["memory tab: confirming a pending fact hides the panel again",
    sRoot.getElementById("newPendingPanel")?.hidden === true]);
  hass.callWS = memoryCallWS;

  // Switching tabs back and forth must not leak the core's animation loop
  // (a real bug caught before shipping — _render() tearing down the canvas
  // without cancelling its requestAnimationFrame loop first). jsdom has no
  // real canvas 2D context (getContext("2d") returns null), so _initCore()
  // never actually reaches requestAnimationFrame here — inject a fake
  // handle to exercise _render()'s cleanup guard directly instead of
  // relying on canvas support this environment doesn't have.
  elNew._animHandle = 999999;
  const dashTabBtn = Array.from(sRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "dashboard");
  dashTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["switching tabs cancels the previous core animation loop instead of leaking it",
    _cafCalls >= 1]);
  global.cancelAnimationFrame = _realCaf;

  const newLookSel = newRoot.getElementById("lookSelect");
  if (newLookSel) {
    newLookSel.value = "classic";
    newLookSel.dispatchEvent(new Event("change"));
    await new Promise(r => setTimeout(r, 20));
    checks.push(["new look's own switcher saves ui_style back to classic",
      _updateConfigCalls.some(c => c.key === "ui_style" && c.value === "classic")]);
  }
  if (elNew._fetchInterval) clearInterval(elNew._fetchInterval);
  if (elNew._animHandle) cancelAnimationFrame(elNew._animHandle);

  // ── Look shell (v7.93.0) — fail-closed default path ──
  // Only the "classic" (default/fail-closed) path is exercised here — the
  // "new" path's dynamic import() of a real URL isn't something jsdom can
  // resolve without a live server, so that direction is a live-instance
  // check (see the plan's verification section), not a unit-level one.
  let _shellStyle = "classic";
  const shellHass = Object.assign({}, hass, {
    callWS: async (m) => (m.type === "nova/get_panel_data" ? { config: { ui_style: _shellStyle } } : {}),
  });
  const shellEl = window.document.createElement("nova-panel");
  window.document.body.appendChild(shellEl);
  shellEl.hass = shellHass;
  await new Promise(r => setTimeout(r, 20));
  checks.push(["look shell mounts Classic by default",
    shellEl.querySelector("nova-panel-classic") !== null]);

  // v7.93.3 regression guard: a real live bug — the panel-look preference
  // can change from three places (Classic's Configure dialog, either
  // look's own switcher), and Configure isn't the Nova panel's own page,
  // so saving it there had no way to reload an already-open Nova tab. A
  // real user hit this exactly ("switched back and it wouldn't change").
  // The shell now notices for itself on its own poll — simulate the
  // preference changing elsewhere, then invoke that poll's check directly
  // (real interval is 20s; not waiting for it here).
  // Not intercepting window.location.reload() itself — jsdom's Location
  // doesn't reliably allow stubbing it — _checkForStyleChange reports its
  // own decision (and still calls reload as a real side effect, safely a
  // no-op stub under jsdom, same as elsewhere in this suite).
  _shellStyle = "new";
  const _shouldReload = await shellEl._checkForStyleChange();
  checks.push(["look shell detects ui_style changed elsewhere while open",
    _shouldReload === true]);

  // v7.93.2 regression guard: a real live bug — the new look's custom
  // properties were declared under `:root{}`, which matches NOTHING inside
  // a shadow tree (unlike `:host{}`, the correct selector for a shadow
  // root's own scoped tokens). Every var(--x) silently failed, stripping
  // every background/border/font with no visible error — jsdom doesn't do
  // real CSS layout, so no structural check here would ever have caught
  // it; only reading the source text can.
  const newLookSrc = fs.readFileSync(NEW_LOOK_COMPONENT, "utf8");
  checks.push(["new look declares its CSS tokens on :host, not :root (shadow DOM)",
    /:host\s*\{/.test(newLookSrc) && !/(^|[^-\w]):root\s*\{/.test(newLookSrc)]);

  // Real bug caught live (13 Sept 2026): giving .settings-card its own
  // `display` (needed for the CSS multi-column masonry fix) made author
  // CSS of equal specificity beat the browser's UA-stylesheet
  // `[hidden]{display:none}` rule — origin beats specificity in the
  // cascade, so EVERY card became visible regardless of group/search
  // filtering, dumping every setting into whichever group was active.
  // jsdom's cascade doesn't enforce origin precedence the way real
  // browsers do, so the existing `.hidden` IDL-property assertions passed
  // while the real rendering was broken — this checks the actual CSS
  // text for the explicit override instead.
  checks.push(["new look's .settings-card[hidden] explicitly forces display:none (overrides its own display rule)",
    /\.settings-card\[hidden\]\s*\{\s*display\s*:\s*none\s*\}/.test(newLookSrc)]);

  let ok = true;
  for (const [n, p] of checks) { console.log((p ? "  PASS  " : "  FAIL  ") + n); if (!p) ok = false; }
  if (typeof el._stopIntervals === "function") el._stopIntervals();
  console.log(ok ? "\nSMOKE TEST CLEAN" : "\nSMOKE TEST FAILED");
  process.exit(ok ? 0 : 1);
}, 350);
