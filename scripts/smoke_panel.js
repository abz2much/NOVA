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
const dom = new JSDOM("<!DOCTYPE html><body></body>", { url: "http://localhost/", pretendToBeVisual: true });
const { window } = dom;
global.window = window; global.document = window.document;
["HTMLElement", "customElements", "Node", "Event", "CustomEvent", "requestAnimationFrame", "cancelAnimationFrame", "FileReader"].forEach(k => { if (window[k]) global[k] = window[k]; });
// jsdom doesn't implement window.confirm (always undefined/falsy) — both
// panels' document-delete flows gate on it, so stub it to auto-confirm.
window.confirm = () => true;
// jsdom's own window.prompt exists but a runtime reassignment (e.g. a test
// setting window.prompt = fn after the component scripts are already
// eval'd) isn't visible to bare `prompt(...)` calls inside that eval'd
// code — a vm/jsdom quirk, not something a test should have to rediscover.
// Define the real stub once, here, before any component script loads, and
// let individual tests drive its answers through this mutable queue instead
// of reassigning window.prompt themselves (Floor Plan Editor's Add Room
// asks for a name, then a type).
global.__promptQueue = [];
window.prompt = () => (global.__promptQueue.length ? global.__promptQueue.shift() : null);

window.eval(fs.readFileSync(COMPONENT, "utf8"));

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
    { id: "master_bedroom", name: "Master Bedroom", caps: ["mmwave", "switch", "lock"], active: false, bedroom: true, lights_on: 0, lights_total: 6,
      temp: "72°F", humidity: "48%", temp_entity: "sensor.mb_temp", humidity_entity: "sensor.mb_humidity", last_motion: "1h" },
    { id: "attic", name: "Attic", caps: ["light"], active: false, bedroom: false, lights_on: 0, lights_total: 2,
      temp: null, humidity: null, temp_entity: null, humidity_entity: null, last_motion: null },
    { id: "entry", name: "Entry", caps: ["light", "door"], active: false, bedroom: false, lights_on: 0, lights_total: 3,
      temp: null, humidity: null, temp_entity: null, humidity_entity: null, last_motion: null },
    { id: "network_room", name: "Network Room", caps: ["light", "switch", "alarm", "leak", "climate", "cam"], active: false, bedroom: false, lights_on: 0, lights_total: 1,
      temp: "84°F", humidity: "33%", temp_entity: "sensor.nr_temp", humidity_entity: "sensor.nr_humidity", last_motion: null },
    { id: "roam", name: "Roam", caps: ["spkr", "mmwave", "cam"], active: false, bedroom: false, lights_on: 0, lights_total: 0,
      temp: null, humidity: null, temp_entity: null, humidity_entity: null, last_motion: null },
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
    all_people: [{ entity_id: "person.abi", name: "Abi" }, { entity_id: "person.rachel", name: "Rachel" }],
    person_honorifics: { "person.rachel": "boss", "person.abi": "captain" },
    general_speaker: "media_player.kitchen_speaker",
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
const _listModelCalls = [];
const _modeSetCalls = [];
const _serviceCalls = [];
const _docDeleteCalls = [];
const _intrLabelCalls = [];
let _energyAgency = "advisory";
let _bioEnabled = false;
let _pendingFacts = [{ id: 42, key: "bedtime", value: "10pm", subject: "primary" }];
let _knownFacts = [
  { id: 1, key: "trash day", value: "Tuesday", subject: "household", source: "stated", confidence: 1 },
  { id: 2, key: "favorite tea", value: "Earl Grey", subject: "primary", source: "inferred", confidence: 0.7 },
];
let _intrCalledOff = false;
let _intrAck = false;
const _intrSnap = { image_b64: "ZmFrZQ==", camera: "camera.dining_room", ts: 1730000000, path: "/config/nova/intrusion/x.jpg" };
const _updateConfigCalls = [];
const _coverageCalls = [];
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
    if (m.type === "nova/compute_camera_coverage") { _coverageCalls.push(m.camera); return { reason: "faces the front walk", covered: ["Front Yard"] }; }
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
          snapshot_path: "/config/nova/intrusion/a.jpg", image_b64: "ZmFrZQ==", label: null },
        { id: "evt_2", ts: 1785999000, kind: "unresolved", reason: "no response",
          breach: "kitchen window", breach_area: "kitchen", snapshot_path: "", label: "false" },
      ], learning: { events: 2, labeled: 1, patterns: {}, damped_patterns: ["kitchen|4"], min_false_to_damp: 3 } };
      if (m.action === "label") { _intrLabelCalls.push({ event_id: m.event_id, label: m.label }); return { ok: true, id: m.event_id, label: m.label,
        learning: { events: 2, labeled: 2, patterns: {}, damped_patterns: [], min_false_to_damp: 3 } }; }
      if (m.action === "learning") return { events: 2, labeled: 1, patterns: {}, damped_patterns: [], min_false_to_damp: 3 };
      if (m.action === "dismiss") { _intrCalledOff = true; return { ok: true, last_snapshot: _intrSnap, called_off: true, suppressed_for: 600, false_alarms_24h: 1 }; }
      if (m.action === "acknowledge") { _intrAck = true; return { ok: true, last_snapshot: _intrSnap, called_off: _intrCalledOff, acknowledged: true, suppressed_for: 0, false_alarms_24h: 0 }; }
      return { last_snapshot: _intrSnap, called_off: _intrCalledOff, acknowledged: _intrAck, suppressed_for: _intrCalledOff ? 600 : 0, false_alarms_24h: _intrCalledOff ? 1 : 0 };
    }
    if (m.type === "nova/voice_confirm_test") return { ok: true, satellite: "assist_satellite.basement_nova", note: "Announce fired." };
    if (m.type === "nova/list_models") {
      _listModelCalls.push({ ...m });
      if (m.provider === "custom") return {
        models: [], error: "model_discovery_unavailable",
      };
      return {
        models: m.provider === "groq"
          ? ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct", "meta-llama/llama-4-scout-17b"]
          : ["gpt-4o", "gpt-4o-mini"],
      };
    }
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
  callService: async (domain, service, data, target) => { _serviceCalls.push({ domain, service, data, target }); },
};
// Command Center is Nova's one dashboard now (Classic was deleted in
// v7.101.30 once this reached feature parity). Tests below drive the
// single "nova-panel" element directly.
setTimeout(async () => {
  const checks = [];
  // ── Nova Command Center ──
  const elNew = window.document.createElement("nova-panel");
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

  // Real gap Abi caught live: the Areas grid hard-capped at 6 tiles, so 8 of
  // his 14 real areas never rendered at all. Also covers the redesign that
  // shipped alongside the fix: capability icons (canonical order, capped at
  // 5 per Classic's own convention), sparkline trends, and a light toggle.
  checks.push(
    ["new look: Areas grid has no hard cap — all 8 fixture areas render",
      newRoot.querySelectorAll(".area-tile").length === 8],
    ["new look: area capability icons follow canonical order and cap at 5",
      (() => {
        const nrTile = Array.from(newRoot.querySelectorAll(".area-tile")).find(t => /Network Room/.test(t.textContent));
        const icons = nrTile ? Array.from(nrTile.querySelectorAll(".area-cap")).map(c => c.getAttribute("title")) : [];
        return icons.length === 5 && icons.join(",") === "cam,light,switch,climate,leak";
      })()],
    ["new look: a room with no temp/humidity sensor renders no climate row or accent bar",
      (() => {
        const atticTile = Array.from(newRoot.querySelectorAll(".area-tile")).find(t => /Attic/.test(t.textContent));
        return !!atticTile && atticTile.classList.contains("no-temp") && !atticTile.querySelector(".area-climate");
      })()],
    ["new look: a room with a temp sensor renders its sparkline trend",
      (() => {
        const garageTile = Array.from(newRoot.querySelectorAll(".area-tile")).find(t => /Garage/.test(t.textContent));
        return !!garageTile && !!garageTile.querySelector(".area-climate svg polyline");
      })()],
  );
  const garageLightToggle = Array.from(newRoot.querySelectorAll(".area-light-toggle")).find(b => b.getAttribute("data-area-name") === "Garage");
  garageLightToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["new look: area light toggle calls light.turn_off targeted at the area",
    _serviceCalls.some(c => c.domain === "light" && c.service === "turn_off" && c.target?.area_id === "garage")]);

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
    ["settings tab has every setting card, General real",
      sRoot.querySelectorAll(".settings-card").length === 28
      && /Sleep state/.test(sRoot.innerHTML) && /Announcements/.test(sRoot.innerHTML)],
    ["settings tab: Room Speakers card is real, not a stub",
      (() => {
        const rs = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Room Speakers/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!rs && !rs.querySelector(".stub-tag") && rs.querySelectorAll(".new-room-speaker-select").length === 2;
      })()],
    ["settings tab: Person Honorifics card lists every person, preset vs custom rendered correctly",
      (() => {
        const ph = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Person Honorifics/.test(c.querySelector(".panel-title")?.textContent || ""));
        if (!ph || ph.querySelector(".stub-tag")) return false;
        const rows = ph.querySelectorAll(".person-honorific-row");
        if (rows.length !== 2) return false;
        // person.rachel -> "boss", a preset option, selected directly, custom input hidden
        const rachelSel = ph.querySelector('select[data-person-id="person.rachel"]');
        const rachelCustom = ph.querySelector('input[data-person-id="person.rachel"]');
        if (!rachelSel || rachelSel.value !== "boss" || !rachelCustom.hidden) return false;
        // person.abi -> "captain", not a preset -> select shows "__custom__", input visible & prefilled
        const abiSel = ph.querySelector('select[data-person-id="person.abi"]');
        const abiCustom = ph.querySelector('input[data-person-id="person.abi"]');
        return abiSel && abiSel.value === "__custom__" && !abiCustom.hidden && abiCustom.value === "captain";
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
    ["settings tab: no card is silently missing (all 28 are real)",
      Array.from(sRoot.querySelectorAll(".settings-card")).every(c => !c.querySelector(".stub-tag"))],
    ["settings tab: Security Alarm card exposes the source and opt in lockdown controls",
      (() => {
        const card = sRoot.getElementById("settings-card-security_alarm");
        return !!card
          && !!card.querySelector('select[data-cfg-key="security_alarm_entity"]')
          && !!card.querySelector('button[data-cfg-key="lockdown_auto_on_arm"]');
      })()],
    ["settings tab: Floor Plan Editor is real, with rooms and the drag canvas",
      (() => {
        const fpeCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Floor Plan Editor/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!fpeCard && !fpeCard.querySelector(".stub-tag") && !!fpeCard.querySelector("#fpnSvg")
          && !!fpeCard.querySelector(".fpn-drag-room");
      })()],
    ["settings tab: Floor Plan Editor has no Classic bridge left — openings/cameras/property/zones are all real here now",
      (() => {
        const fpeCard = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Floor Plan Editor/.test(c.querySelector(".panel-title")?.textContent || ""));
        return !!fpeCard && !fpeCard.querySelector("#fpnGoClassic")
          && !!fpeCard.querySelector("#opAddWindow") && !!fpeCard.querySelector("#fpnCamAdd")
          && !!fpeCard.querySelector("#fpnAddZone") && !!fpeCard.querySelector("#fpnAddProperty");
      })()],
    ["settings tab shows only the active group by default",
      Array.from(sRoot.querySelectorAll('.settings-card[data-settings-group="general"]')).every(c => !c.hidden)
      && Array.from(sRoot.querySelectorAll('.settings-card:not([data-settings-group="general"])')).every(c => c.hidden)],
  );

  // Floor Plan Editor: floor-tab switch, Add Room, Save, and Units toggle.
  // Drag/resize itself isn't exercised here — jsdom implements neither
  // createSVGPoint nor getScreenCTM, so that math needs a real browser to
  // verify (same reason Classic's identical drag code isn't jsdom-tested).
  sRoot.querySelector('.fpn-floor-tab[data-fpn-floor="2f"]').click();
  let fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: floor tab switch re-renders just that card with the other floor's rooms",
    fpeCardNow.querySelector('.fpn-floor-tab[data-fpn-floor="2f"]')?.classList.contains("active")
    && /MASTER BEDROOM/.test(fpeCardNow.textContent)
    && !/DOWNSTAIRS HALLWAY/.test(fpeCardNow.textContent)]);

  global.__promptQueue = ["Sun Room", "room"];
  sRoot.getElementById("fpnAddRoom").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add Room appends a room to the working copy and redraws it",
    /SUN ROOM/.test(fpeCardNow.textContent)
    && fpeCardNow.querySelectorAll(".fpn-drag-room").length === 6]);   // 2f default has 5 rooms + this one

  sRoot.getElementById("fpnSave").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Save writes floor_plan_rooms with the added room, not a full-page re-render",
    _updateConfigCalls.some(c => c.key === "floor_plan_rooms" && /Sun Room/.test(c.value))]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnUnits").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Units toggle saves floor_plan_units",
    _updateConfigCalls.some(c => c.key === "floor_plan_units" && c.value === "metric")]);
  sRoot = elNew.shadowRoot;

  // Export/Import (ported from Classic — a manual layout backup/restore).
  // jsdom has no URL.createObjectURL, so Export can only be confirmed not
  // to throw (the real download itself needs a live browser); Import's
  // FileReader path jsdom does support fully, so that gets a real test.
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  let _exportThrew = false;
  try { fpeCardNow.querySelector("#fpnExport").click(); } catch (_e) { _exportThrew = true; }
  checks.push(["floor plan editor: Export button exists and doesn't throw",
    !_exportThrew]);

  const importFile = fpeCardNow.querySelector("#fpnImportFile");
  const fakeLayout = { "1f": { label: "1st Floor", viewBox: "0 0 320 150", rooms: [{ name: "Imported Room", x: 5, y: 5, w: 40, h: 30, type: "room" }] } };
  const fakeFile = new window.File([JSON.stringify(fakeLayout)], "nova-floor-plan.json", { type: "application/json" });
  Object.defineProperty(importFile, "files", { value: [fakeFile], configurable: true });
  importFile.dispatchEvent(new window.Event("change"));
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Import loads a layout file into the working copy",
    /IMPORTED ROOM/.test(fpeCardNow.textContent)]);

  // Devices on plan (jarvis-aio port, Phase 2): add a device, see its pin,
  // save it, remove it, and confirm the opacity slider persists.
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnEntInput").value = "camera.front";
  fpeCardNow.querySelector("#fpnEntAdd").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add device places a live-state pin on the plan",
    fpeCardNow.querySelectorAll(".fpn-ent").length === 1
    && /front/i.test(fpeCardNow.textContent)]);

  fpeCardNow.querySelector("#fpnSave").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Save writes floor_plan_entities alongside the rooms",
    _updateConfigCalls.some(c => c.key === "floor_plan_entities" && /camera\.front/.test(c.value))]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector(".fpn-ent-del").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: removing a device chip clears its pin from the canvas",
    fpeCardNow.querySelectorAll(".fpn-ent").length === 0]);

  const fpnBgOp = fpeCardNow.querySelector("#fpnBgOp");
  fpnBgOp.value = "0.5";
  fpnBgOp.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: background opacity slider saves floor_plan_bg_opacity",
    _updateConfigCalls.some(c => c.key === "floor_plan_bg_opacity" && c.value === "0.5")]);
  sRoot = elNew.shadowRoot;

  // Outdoor zone + property line (Phase 3a): added as a polygon room / a
  // separate property config, rendered as draggable-vertex canvas elements.
  global.__promptQueue = ["Side Yard"];
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnAddZone").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add Outdoor Zone places a draggable-vertex polygon",
    fpeCardNow.querySelectorAll(".fpn-zone").length === 1
    && fpeCardNow.querySelectorAll(".fpn-zone-vtx").length === 4
    && /SIDE YARD/.test(fpeCardNow.textContent)]);

  fpeCardNow.querySelector("#fpnAddProperty").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add Property Line draws a 4-corner boundary and shows lot size",
    fpeCardNow.querySelectorAll(".fpn-prop-vtx").length === 4
    && /Lot:/.test(fpeCardNow.textContent)]);

  fpeCardNow.querySelector("#fpnSave").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Save writes floor_plan_property alongside rooms/entities",
    _updateConfigCalls.some(c => c.key === "floor_plan_property" && /points/.test(c.value))]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnAddProperty").click();   // toggles to "Clear Property" once >=3 points exist
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: clicking Property again clears the boundary (window.confirm stubbed true)",
    fpeCardNow.querySelectorAll(".fpn-prop-vtx").length === 0]);
  sRoot = elNew.shadowRoot;

  // Cameras + AI coverage (Phase 3b): add a camera, aim it, compute coverage,
  // save, then remove it.
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnCamAdd").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add Camera places a pin with aim/FOV/range controls",
    fpeCardNow.querySelectorAll(".fpn-cam").length === 1
    && !!fpeCardNow.querySelector('.cam-field-new[data-cam="angle"]')
    && !!fpeCardNow.querySelector(".cam-io-new")
    && /INDOOR/.test(fpeCardNow.textContent)]);

  fpeCardNow.querySelector("#fpnCamCompute").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Compute coverage calls nova/compute_camera_coverage",
    _coverageCalls.length === 1]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#fpnSave").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Save writes floor_plan_cameras",
    _updateConfigCalls.some(c => c.key === "floor_plan_cameras" && /"indoor":true/.test(c.value))]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector(".cam-del-new").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: removing a camera clears its pin from the canvas",
    fpeCardNow.querySelectorAll(".fpn-cam").length === 0]);
  sRoot = elNew.shadowRoot;

  // Windows/doors/dormers (Phase 3c): add a window, see its wall marker,
  // save it, remove it.
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector("#opAddWindow").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: Add Window places a wall marker with wall/position/size controls",
    fpeCardNow.querySelectorAll(".fpn-op-marker").length === 1
    && !!fpeCardNow.querySelector('.op-field-new[data-op="wall"]')
    && !!fpeCardNow.querySelector('.op-field-new[data-op="pos"]')
    && /WINDOW/.test(fpeCardNow.textContent)]);

  fpeCardNow.querySelector("#fpnSave").click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["floor plan editor: Save writes floor_plan_elements",
    _updateConfigCalls.some(c => c.key === "floor_plan_elements" && /"type":"window"/.test(c.value))]);

  sRoot = elNew.shadowRoot;
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  fpeCardNow.querySelector(".op-del-new").click();
  fpeCardNow = sRoot.getElementById("settings-card-floor_plan_editor");
  checks.push(["floor plan editor: removing an opening clears its wall marker",
    fpeCardNow.querySelectorAll(".fpn-op-marker").length === 0]);
  sRoot = elNew.shadowRoot;

  // ── New look: Residence tab (v7.101.24) — reuses Classic's NOVA3D engine
  // via window.NOVA3D rather than re-deriving the 3D geometry ──
  hass.states["cover.test_front_door"] = { state: "closed", attributes: { friendly_name: "Front Door Cover" } };
  const residenceTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "residence");
  residenceTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  let resRoot = elNew.shadowRoot;
  checks.push(
    ["residence tab: nav tab switch marks it active and updates the brand tag",
      resRoot.querySelector('.nav-tab[data-tab="residence"]')?.classList.contains("active")
      && /Residence/.test(resRoot.querySelector(".brand-tag")?.textContent || "")],
    ["residence tab: renders the home-style selector, floor tabs, and view-preset buttons",
      !!resRoot.querySelector('select[data-cfg-key="residence_style"]')
      && resRoot.querySelectorAll(".res-floor-tab").length >= 2
      && resRoot.querySelectorAll(".res-view-btn").length === 5],
    ["residence tab: 3D scene mount renders a real SVG from window.NOVA3D",
      (() => { const svg = resRoot.querySelector("#resIso svg"); return !!svg && svg.outerHTML.length > 100; })()],
    ["residence tab: stats panel shows style/occupied from live data",
      /Cape Cod/.test(resRoot.getElementById("resStyleTag")?.textContent || "")
      && /\//.test(resRoot.getElementById("resOcc")?.textContent || "")],
    ["residence tab: door mapping renders one select per door slot",
      resRoot.querySelectorAll(".door-map-sel-new").length === elNew._doorSlots().length],
  );

  await elNew._fetchMmwaveNew();
  resRoot = elNew.shadowRoot;
  checks.push(["residence tab: mmWave list renders live per-room presence after fetch",
    /Kitchen/.test(resRoot.getElementById("resMmwaveList")?.textContent || "")
    && /OCCUPIED/.test(resRoot.getElementById("resMmwaveSummary")?.textContent || "")]);

  const priorSceneHtml = resRoot.getElementById("resIso")?.innerHTML || "";
  const view90Btn = Array.from(resRoot.querySelectorAll(".res-view-btn")).find(b => b.getAttribute("data-res-theta") === "90");
  view90Btn.click();
  checks.push(["residence tab: clicking a view-preset button rotates the model without a full re-render",
    elNew._house3dTheta === 90 && resRoot.getElementById("resIso")?.innerHTML !== priorSceneHtml]);

  const floor1fBtn = resRoot.querySelector('.res-floor-tab[data-res-floor="1f"]');
  floor1fBtn.click();
  checks.push(["residence tab: switching floor tabs updates the active floor",
    elNew._currentFloor === "1f" && floor1fBtn.classList.contains("active")]);

  // Scroll-to-zoom on the 3D scene (v7.101.27) — Abi caught trying to zoom in on
  // the model and finding only rotate was wired, so any drag just spun the house.
  const priorZoomSceneHtml = resRoot.getElementById("resIso")?.innerHTML || "";
  const zoomInEvt = new resRoot.ownerDocument.defaultView.WheelEvent("wheel", { deltaY: -100, bubbles: true, cancelable: true });
  resRoot.getElementById("resScene").dispatchEvent(zoomInEvt);
  await new Promise(r => setTimeout(r, 30));
  checks.push(["residence tab: scrolling up on the 3D scene zooms in",
    elNew._house3dZoom > 1 && resRoot.getElementById("resIso")?.innerHTML !== priorZoomSceneHtml]);
  const priorZoom = elNew._house3dZoom;
  const zoomOutEvt = new resRoot.ownerDocument.defaultView.WheelEvent("wheel", { deltaY: 100, bubbles: true, cancelable: true });
  resRoot.getElementById("resScene").dispatchEvent(zoomOutEvt);
  await new Promise(r => setTimeout(r, 30));
  checks.push(["residence tab: scrolling down on the 3D scene zooms back out",
    elNew._house3dZoom < priorZoom]);

  const doorMapSel = resRoot.querySelector('.door-map-sel-new[data-slot="front"]');
  const priorDoorMappingSaves = _updateConfigCalls.filter(c => c.key === "door_mapping").length;
  doorMapSel.value = "cover.test_front_door";
  doorMapSel.dispatchEvent(new resRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["residence tab: mapping a door slot to an entity saves door_mapping",
    _updateConfigCalls.some(c => c.key === "door_mapping" && c.value === JSON.stringify({ front: "cover.test_front_door" }))]);

  const styleSel = elNew.shadowRoot.querySelector('select[data-cfg-key="residence_style"]');
  styleSel.value = "ranch";
  styleSel.dispatchEvent(new elNew.shadowRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["residence tab: changing home style autosaves residence_style via nova/update_config",
    _updateConfigCalls.some(c => c.key === "residence_style" && c.value === "ranch")]);
  checks.push(["residence tab: post-save re-render lands back on the residence tab with a live scene",
    !!elNew.shadowRoot.getElementById("resIso")?.querySelector("svg")]);
  delete hass.states["cover.test_front_door"];

  // Back to settings for the tests that follow.
  const backToSettingsBtn = Array.from(elNew.shadowRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "settings");
  backToSettingsBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;

  // Person Honorifics: picking "Custom…" reveals the text input without saving
  // yet (nothing to save), then typing+blurring the custom input saves it.
  const rachelSel = sRoot.querySelector('select[data-person-id="person.rachel"]');
  const priorPersonHonorificSaves = _updateConfigCalls.filter(c => c.key === "person_honorifics").length;
  rachelSel.value = "__custom__";
  rachelSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  // No `await` here on purpose: revealing the custom input is synchronous
  // (no save, so nothing to wait on) — waiting risks a still-in-flight
  // re-render from an earlier test's save landing here and clobbering this
  // synchronous DOM mutation before it's asserted.
  const rachelCustomAfterPick = sRoot.querySelector('input[data-person-id="person.rachel"]');
  checks.push(["settings tab: Person Honorifics — picking Custom… reveals the input without an unwanted save",
    !rachelCustomAfterPick.hidden
    && _updateConfigCalls.filter(c => c.key === "person_honorifics").length === priorPersonHonorificSaves]);
  rachelCustomAfterPick.value = "boss lady";
  rachelCustomAfterPick.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Person Honorifics — custom text input autosaves into the per-person JSON dict",
    _updateConfigCalls.some(c => c.key === "person_honorifics"
      && JSON.parse(c.value)["person.rachel"] === "boss lady"
      && JSON.parse(c.value)["person.abi"] === "captain")]);  // Abi's existing override untouched
  sRoot = elNew.shadowRoot;
  const abiSelForDefault = sRoot.querySelector('select[data-person-id="person.abi"]');
  abiSelForDefault.value = "";
  abiSelForDefault.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Person Honorifics — picking “— use default —” removes that person's override entirely",
    _updateConfigCalls.some(c => c.key === "person_honorifics" && !("person.abi" in JSON.parse(c.value)))]);
  sRoot = elNew.shadowRoot;

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

  // Post-v7.103.1 layout polish: Setup Doctor, Provider Activity and Service
  // tests all use Nova's normal prominent .panel-head > .panel-title pattern
  // (same element as the Diagnostics card's own title), replacing the small
  // .mode-bind-head diagnostic-label style everywhere it was used as a real
  // heading. The divider that used to be baked into .mode-bind-head's own
  // border-top (lost when Setup Doctor moved off that class) is restored as
  // a standalone empty .mode-bind-head spacer between the main Diagnostics
  // section and Setup Doctor — same class, same CSS, no new line style.
  // Individual check rows (Core services and Setup Doctor) now render their
  // name and OK/OFF/IDLE/WARN/DOWN status as separate sibling elements
  // inside the existing two-column .cfg-row pattern (the same one the
  // Core-services aggregate summary already used), instead of appending the
  // status into the label's own text.
  const diagCardOf = (root) => Array.from(root.querySelectorAll(".settings-card"))
    .find(c => /^Diagnostics$/.test(c.querySelector(".panel-title")?.textContent?.trim() || ""));
  const rowFor = (diagCard, labelText) => Array.from(diagCard.querySelectorAll(".cfg-row"))
    .find(r => r.querySelector("label")?.textContent.trim() === labelText);
  checks.push(
    ["settings tab: Provider Activity section renders its heading with an empty result",
      (() => {
        const diagCard = diagCardOf(sRoot);
        return !!diagCard && /Provider Activity/.test(diagCard.textContent)
          && /No provider activity recorded yet\. Activity appears after Nova uses a supported conversation or classifier path\./.test(diagCard.textContent);
      })()],
    ["settings tab: Setup Doctor, Provider Activity and Service tests all use the same .panel-head > .panel-title heading pattern",
      (() => {
        const diagCard = diagCardOf(sRoot);
        if (!diagCard) return false;
        const referenceTitle = diagCard.querySelector(".panel-title"); // the card's own "Diagnostics" title — the reference prominent heading
        const titleFor = (text) => Array.from(diagCard.querySelectorAll(".panel-title")).find(h => h.textContent.trim() === text);
        const matchesReference = (el) => !!el && !!referenceTitle
          && el.tagName === referenceTitle.tagName
          && el.className === referenceTitle.className
          && el.parentElement?.className === "panel-head";
        const noneStillSmall = ["Setup Doctor", "Provider Activity", "Service tests"].every(text =>
          !Array.from(diagCard.querySelectorAll(".mode-bind-head")).some(h => h.textContent.trim() === text));
        return matchesReference(titleFor("Setup Doctor"))
          && matchesReference(titleFor("Provider Activity"))
          && matchesReference(titleFor("Service tests"))
          && noneStillSmall;
      })()],
    ["settings tab: a divider (the existing empty .mode-bind-head spacer) sits between the main Diagnostics section and Setup Doctor",
      (() => {
        const diagCard = diagCardOf(sRoot);
        if (!diagCard) return false;
        const setupTitle = Array.from(diagCard.querySelectorAll(".panel-title")).find(h => h.textContent.trim() === "Setup Doctor");
        const dividers = Array.from(diagCard.querySelectorAll(".mode-bind-head")).filter(h => h.textContent.trim() === "");
        return !!setupTitle && dividers.some(d =>
          d.compareDocumentPosition(setupTitle) & Node.DOCUMENT_POSITION_FOLLOWING);
      })()],
    ["settings tab: Core services aggregate summary row is unchanged — label left, rolled-up status right",
      (() => {
        const diagCard = diagCardOf(sRoot);
        const row = rowFor(diagCard, "Core services");
        const status = row?.querySelector("span");
        return !!row && !!status && /3\/4 CORE SERVICES HEALTHY/.test(status.textContent)
          && row.children.length === 2 && row.children[0].tagName === "LABEL" && row.children[1] === status;
      })()],
    ["settings tab: Core-services check labels and statuses are separate elements, status right-aligned via .cfg-row",
      (() => {
        const diagCard = diagCardOf(sRoot);
        const row = rowFor(diagCard, "LLM");
        const status = row?.querySelector("span.diag-ok");
        return !!row && !!status && row.classList.contains("cfg-row")
          && row.querySelector("label").textContent.trim() === "LLM"
          && status.textContent.trim() === "OK"
          && !row.querySelector("label").textContent.includes("OK");
      })()],
  );
  // Setup Doctor's own check rows only render once nova/get_setup_health
  // returns a non-core-service check; the default mock leaves it in its
  // "Loading…" state (exercised by the heading-pattern check above), so
  // fetch a realistic payload here to prove the row layout itself.
  const setupHealthCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/get_setup_health") return { checks: [
      { key: "room_speakers", name: "Room speakers", status: "warn", detail: "2 rooms missing a speaker", suggested_fix: "assign one in Settings" },
    ] };
    return setupHealthCallWS(m);
  };
  await elNew._fetchDiagnosticsData();
  sRoot = elNew.shadowRoot;
  checks.push(["settings tab: Setup Doctor labels and statuses are separate elements, status right-aligned via .cfg-row",
    (() => {
      const diagCard = diagCardOf(sRoot);
      const row = rowFor(diagCard, "Room speakers");
      const status = row?.querySelector("span.diag-warn");
      return !!row && !!status && row.classList.contains("cfg-row")
        && row.querySelector("label").textContent.trim() === "Room speakers"
        && status.textContent.trim() === "WARN"
        && !row.querySelector("label").textContent.includes("WARN");
    })()]);
  hass.callWS = setupHealthCallWS;
  await elNew._fetchDiagnosticsData();
  sRoot = elNew.shadowRoot;
  const providerActivityCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/get_provider_activity") throw new Error("boom");
    return providerActivityCallWS(m);
  };
  await elNew._fetchDiagnosticsData();
  sRoot = elNew.shadowRoot;
  checks.push(["settings tab: Provider Activity shows a distinct error state when the request fails",
    (() => {
      const diagCard = diagCardOf(sRoot);
      return !!diagCard && /Provider Activity/.test(diagCard.textContent) && /Couldn't load provider activity\./.test(diagCard.textContent);
    })()]);
  hass.callWS = providerActivityCallWS;
  await elNew._fetchDiagnosticsData();
  sRoot = elNew.shadowRoot;

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
  checks.push(["settings tab: model discovery sends no browser-controlled URL",
    _listModelCalls.length > 0
    && _listModelCalls.every(c => Object.keys(c).sort().join(",") === "provider,type")]);
  const llmProvSel = sRoot.querySelector('.new-model-row[data-role="llm"] .new-prov-select');
  llmProvSel.value = "openai";
  llmProvSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: AI Models provider change saves provider, clears base_url, and reloads its own model list",
    _updateConfigCalls.some(c => c.key === "llm_provider" && c.value === "openai")
    && _updateConfigCalls.some(c => c.key === "llm_base_url" && c.value === "")
    && /gpt-4o/.test(sRoot.querySelector('.new-model-row[data-role="llm"] .new-model-select')?.innerHTML || "")]);

  const llmModelSel = sRoot.querySelector('.new-model-row[data-role="llm"] .new-model-select');
  const llmCustomInput = sRoot.querySelector('.new-model-row[data-role="llm"] .new-model-custom');
  llmProvSel.value = "custom";
  llmProvSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  const unavailableShown = /no models found/.test(llmModelSel.textContent || "");
  llmModelSel.value = "__custom__";
  llmModelSel.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  llmCustomInput.value = "manually-entered-model";
  llmCustomInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: manual model entry remains available when discovery is unavailable",
    unavailableShown
    && _updateConfigCalls.some(c => c.key === "model" && c.value === "manually-entered-model")]);

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

  // Arrival front-door picker (v7.101.9) -- gates the welcome briefing on the
  // door actually opening, not just GPS/zone presence. Same convention as
  // Classic's own "openings entity list includes window sensors" check:
  // inject a fixture entity, verify the pure options-builder picks it up.
  checks.push(["settings tab: Briefings card has the arrival front-door picker",
    !!sRoot.querySelector('select[data-cfg-key="arrival_front_door_entity"]')]);
  checks.push(["settings tab: arrival front-door picker lists door-like binary_sensors",
    (() => {
      const st = elNew._hass.states;
      st["binary_sensor.test_entry_front_door"] = { state: "off", attributes: { device_class: "door", friendly_name: "Front Door" } };
      const html = elNew._frontDoorOptions("");
      delete st["binary_sensor.test_entry_front_door"];
      return html.some(([eid]) => eid === "binary_sensor.test_entry_front_door");
    })()]);

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

  // Notifications: legacy single selection is preserved, then a second
  // device can be selected and both are saved as a JSON list.
  const safetyNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Awareness & Safety");
  safetyNavBtn.click();
  await new Promise(r => setTimeout(r, 10));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["settings tab: Notifications card preserves the legacy selected device",
      (() => {
        const nc = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /^Notifications$/.test(c.querySelector(".panel-title")?.textContent?.trim() || ""));
        const abi = nc?.querySelector('.new-notify-service-toggle[data-notify-service="notify.mobile_app_abi_phone"]');
        return !!nc && !nc.querySelector(".stub-tag") && !!abi
          && abi.classList.contains("on") && abi.textContent.trim() === "ON"
          && nc.querySelectorAll(".new-notify-service-toggle").length === 2;
      })()],
  );
  const notifyToggle = sRoot.querySelector('.new-notify-service-toggle[data-notify-service="notify.mobile_app_spouse_phone"]');
  notifyToggle.click();
  const abiNotifyToggle = sRoot.querySelector('.new-notify-service-toggle[data-notify-service="notify.mobile_app_abi_phone"]');
  abiNotifyToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: rapid Notifications toggles preserve both changes in order",
    _updateConfigCalls.some(c => c.key === "notify_services" && c.value === JSON.stringify([
      "notify.mobile_app_abi_phone", "notify.mobile_app_spouse_phone",
    ])) && _updateConfigCalls.some(c => c.key === "notify_services" && c.value === JSON.stringify([
      "notify.mobile_app_spouse_phone",
    ]))]);
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
  const costEntityInput = sRoot.querySelector('input[data-cfg-key="energy_cost_today_entity"]');
  costEntityInput.value = "sensor.electricity_cost_today";
  costEntityInput.dispatchEvent(new sRoot.ownerDocument.defaultView.Event("change", { bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Energy Management cost-entity field autosaves via the generic cfg-field handler",
    _updateConfigCalls.some(c => c.key === "energy_cost_today_entity" && c.value === "sensor.electricity_cost_today")]);
  sRoot = elNew.shadowRoot;

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
  // Regression guard: the dead "Announce unidentified loads" toggle (backed
  // by appliance_announce_unknown, which the announce chokepoint no longer
  // reads at all) was removed rather than left as a control with no effect.
  checks.push(["settings tab: Appliances card no longer has the dead 'Announce unidentified loads' toggle",
    (() => {
      const ac = Array.from(sRoot.querySelectorAll(".settings-card")).find(c => /Appliances/.test(c.querySelector(".panel-title")?.textContent || ""));
      return !!ac && !ac.querySelector("#newApplianceUnknown")
        && !/loads matching no declared appliance/.test(ac.textContent || "");
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
  // the add-entity control. Force a clean, empty starting chip list rather
  // than asserting against the fixture's original seed value, so this
  // section's outcome doesn't depend on what ran before it.
  delete PANEL.config.pattern_include_entities;
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
  // Camera Watch / Visitor Learning (v7.101.5) — previously Classic-only,
  // parity gap Abi caught: he's on the new look and had no way to flip
  // camera_auto_analyze from his actual settings screen.
  checks.push(["settings tab: Camera Watch and Visitor Learning toggles are real, not stubs",
    !!sRoot.querySelector('.toggle-btn[data-cfg-key="camera_auto_analyze"]')
    && !!sRoot.querySelector('.toggle-btn[data-cfg-key="visitor_learning"]')]);
  const camWatchBtn = sRoot.querySelector('.toggle-btn[data-cfg-key="camera_auto_analyze"]');
  camWatchBtn.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["settings tab: Camera Watch toggle autosaves via nova/update_config",
    _updateConfigCalls.some(c => c.key === "camera_auto_analyze" && c.value === false)]);
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

  // Wellbeing Context: switch to Home & Extras group. The status fetch
  // itself is guarded by a once-per-lifetime flag fired the first time
  // settings wired at all (already consumed earlier in this run), so
  // force a fresh fetch directly with the ON/2-sensors starting state
  // explicit rather than depending on what ran before; the toggle click
  // below turns it back OFF.
  _bioEnabled = true;
  const homeNavBtn = Array.from(sRoot.querySelectorAll(".settings-nav-btn")).find(b => b.textContent === "Home & Extras");
  homeNavBtn.click();
  await elNew._fetchBio();
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

  // ── System Log re-entry loading bug (fixed post-v7.106.0) ──
  // Root cause: the dedup "skip redundant re-render" signature lived on
  // the component INSTANCE, which survives a full shadow-DOM teardown and
  // rebuild (every tab/Logs-sub-view switch tears down and recreates
  // #newLogEntries from its hardcoded "Loading…" shell). Returning to
  // System Log after the identical data had already been rendered once
  // made the guard wrongly conclude the brand-new, still-blank container
  // already showed it, and skipped the very render that would have
  // replaced "Loading…" with the real rows — permanently, since nothing
  // else ever revisits an already-"handled" fetch. Fixed by caching the
  // fetched entries (_debugLogEntries) so a re-entry renders them
  // synchronously before the background refresh even starts, and by
  // moving the dedup signature onto the container element's own dataset
  // (naturally unstamped on a fresh element) instead of the component
  // instance.
  {
    const findNavTab = (root, tab) =>
      Array.from(root.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === tab);
    const findLogView = (root, view) =>
      Array.from(root.querySelectorAll(".new-logview")).find(b => b.getAttribute("data-view") === view);
    const FIRST_ENTRIES = [
      { ts: "10:00:00", cat: "CONV", msg: "first-visit entry one" },
      { ts: "10:00:01", cat: "AGENT", msg: "first-visit entry two" },
    ];

    // 1) First-ever visit: no cache yet — must show Loading, then entries.
    delete elNew._debugLogEntries;
    hass.callWS = async (m) => (m.type === "nova/get_debug_log" ? { entries: FIRST_ENTRIES } : originalCallWS(m));
    elNew._logView = "system";
    elNew._currentTab = "dashboard";
    elNew._render();
    let r = elNew.shadowRoot;
    findNavTab(r, "logs").click();
    r = elNew.shadowRoot;
    checks.push(["system log: first visit (no cache) shows Loading before the fetch resolves",
      /Loading/.test(r.getElementById("newLogEntries")?.textContent || "")]);
    await new Promise(res => setTimeout(res, 20));
    r = elNew.shadowRoot;
    checks.push(["system log: first visit shows the real entries once the fetch resolves",
      r.querySelectorAll("#newLogEntries .new-log-entry").length === 2
      && /first-visit entry one/.test(r.getElementById("newLogEntries")?.textContent || "")]);

    // 2) Leave for another top-level tab, then return: cached entries must
    // render immediately (synchronously, before any network round trip),
    // never a blocking "Loading…" — this is the exact reported bug.
    findNavTab(r, "dashboard").click();
    await new Promise(res => setTimeout(res, 5));
    r = elNew.shadowRoot;
    findNavTab(r, "logs").click();
    r = elNew.shadowRoot; // freshly rebuilt shell, checked BEFORE the awaited refresh below
    checks.push(["system log: returning to the tab renders cached entries immediately, not Loading",
      r.querySelectorAll("#newLogEntries .new-log-entry").length === 2
      && !/Loading/.test(r.getElementById("newLogEntries")?.textContent || "")]);

    // 3) The background refresh that fires on that same re-entry must
    // complete without ever blanking the cached rows back to Loading.
    await new Promise(res => setTimeout(res, 20));
    r = elNew.shadowRoot;
    checks.push(["system log: the background refresh on return completes without hiding the cached rows",
      r.querySelectorAll("#newLogEntries .new-log-entry").length === 2
      && !/Loading/.test(r.getElementById("newLogEntries")?.textContent || "")]);

    // 4) Rapid repeated tab switching must never have more than one
    // System Log request in flight at once (the in-flight guard).
    let concurrent = 0, maxConcurrent = 0, totalCalls = 0;
    hass.callWS = async (m) => {
      if (m.type !== "nova/get_debug_log") return originalCallWS(m);
      totalCalls++; concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
      await new Promise(res => setTimeout(res, 15)); // simulate real network latency
      concurrent--;
      return { entries: FIRST_ENTRIES };
    };
    for (let i = 0; i < 4; i++) {
      findNavTab(elNew.shadowRoot, "dashboard").click();
      findNavTab(elNew.shadowRoot, "logs").click();
    }
    await new Promise(res => setTimeout(res, 80));
    checks.push(["system log: rapid repeated tab switching never has more than one request in flight at once",
      maxConcurrent <= 1 && totalCalls >= 1]);

    // 5) A failed background refresh must keep the cached rows visible
    // (fail-open) and surface the failure only as a non-blocking signal
    // (console.warn), never an error banner replacing valid rows.
    hass.callWS = async (m) => (m.type === "nova/get_debug_log" ? { entries: FIRST_ENTRIES } : originalCallWS(m));
    await elNew._fetchDebugLog();
    r = elNew.shadowRoot;
    const rowsBeforeFailure = r.querySelectorAll("#newLogEntries .new-log-entry").length;
    const warnCalls = [];
    const origWarn = console.warn;
    console.warn = (...a) => { warnCalls.push(a); };
    hass.callWS = async (m) => { if (m.type === "nova/get_debug_log") throw new Error("network down"); return originalCallWS(m); };
    await elNew._fetchDebugLog();
    console.warn = origWarn;
    r = elNew.shadowRoot;
    checks.push(["system log: a failed refresh keeps the cached rows visible instead of an error banner",
      rowsBeforeFailure === 2
      && r.querySelectorAll("#newLogEntries .new-log-entry").length === 2
      && !r.getElementById("newLogEntries")?.querySelector(".new-log-entry-error")]);
    checks.push(["system log: a failed refresh exposes a non-blocking warning, not a silent failure",
      warnCalls.some(a => /System Log refresh failed/.test(String(a[0])))]);

    // 6) A slow System Log response that resolves AFTER the user has
    // switched to a different Logs sub-view must not touch that other
    // view's DOM (its container id no longer exists to be found/written).
    let resolveSlow;
    hass.callWS = async (m) => {
      if (m.type === "nova/get_debug_log") {
        return new Promise(res => { resolveSlow = () => res({ entries: [{ ts: "11:00:00", cat: "CONV", msg: "stale, arrived late" }] }); });
      }
      return originalCallWS(m);
    };
    elNew._logView = "system";
    elNew._render(); // kicks off a _fetchDebugLog() that is now pending on resolveSlow
    r = elNew.shadowRoot;
    findLogView(r, "decisions").click();
    await new Promise(res => setTimeout(res, 5));
    r = elNew.shadowRoot;
    const decisionsHtmlBeforeStale = r.getElementById("decisionEntries")?.innerHTML || "";
    resolveSlow();
    await new Promise(res => setTimeout(res, 10));
    r = elNew.shadowRoot;
    checks.push(["system log: a stale response after switching sub-views cannot overwrite the new view",
      !r.getElementById("newLogEntries") // System Log's own shell no longer exists
      && !/stale, arrived late/.test(r.getElementById("decisionEntries")?.innerHTML || "")
      && r.getElementById("decisionEntries")?.innerHTML === decisionsHtmlBeforeStale]);

    // 7) Switching among all four Logs sub-views never mixes their data or
    // loading flags — at any moment only the active sub-view's container
    // exists in the DOM at all.
    hass.callWS = async (m) => (m.type === "nova/get_debug_log" ? { entries: FIRST_ENTRIES } : originalCallWS(m));
    const sequence = ["system", "decisions", "spoken_history", "actions", "system"];
    const containerIdFor = { system: "newLogEntries", decisions: "decisionEntries", spoken_history: "spokenHistoryEntries", actions: "actionEntries" };
    let sequenceOk = true;
    for (const view of sequence) {
      const btn = findLogView(elNew.shadowRoot, view) || findNavTab(elNew.shadowRoot, "logs");
      btn.click();
      await new Promise(res => setTimeout(res, 15));
      const vr = elNew.shadowRoot;
      for (const [otherView, otherId] of Object.entries(containerIdFor)) {
        const shouldExist = otherView === view;
        if (!!vr.getElementById(otherId) !== shouldExist) sequenceOk = false;
      }
    }
    checks.push(["system log: switching among all four Logs sub-views never leaves more than one view's container in the DOM",
      sequenceOk]);

    // Restore the default mock and leave the shared element on System Log,
    // matching what the rest of the suite expects below.
    hass.callWS = originalCallWS;
    delete elNew._debugLogEntries;
    elNew._logView = "system";
    elNew._render();
    await new Promise(res => setTimeout(res, 20));
    sRoot = elNew.shadowRoot;
  }

  // ── Spoken History (v7.104.0) — beside System Log and Decisions ──
  const spokenViewBtn = Array.from(sRoot.querySelectorAll(".new-logview")).find(b => b.getAttribute("data-view") === "spoken_history");
  spokenViewBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["spoken history: heading always renders",
      /Spoken History/.test(sRoot.querySelector(".panel-title")?.textContent || "")],
    ["spoken history: empty result shows a clear empty state",
      /No spoken messages recorded yet\./.test(sRoot.getElementById("spokenHistoryEntries")?.textContent || "")],
  );

  const spokenCallWS = hass.callWS;
  let repeatCalls = [];
  hass.callWS = async (m) => {
    if (m.type === "nova/get_spoken_history") return { entries: [
      { id: 5, timestamp: 1700000000, text: "Welcome home, sir.", source: "welcome",
        speakers: ["binary_sensor.mailbox"], delivery_state: "sent", repeat_of_id: null },
      { id: 4, timestamp: 1699990000, text: "Reminder: take out the bins.", source: "reminder",
        speakers: ["media_player.gone"], delivery_state: "sent", repeat_of_id: null },
    ] };
    if (m.type === "nova/repeat_spoken") { repeatCalls.push(m.spoken_id); return { ok: true, spoken: "Welcome home, sir." }; }
    return spokenCallWS(m);
  };
  await elNew._fetchSpokenHistory();
  sRoot = elNew.shadowRoot;
  const spokenRows = sRoot.querySelectorAll("#spokenHistoryEntries .cfg-row");
  checks.push(
    ["spoken history: renders newest-first with source, text, delivery label and resolved/fallback speaker names",
      (() => {
        const body = sRoot.getElementById("spokenHistoryEntries");
        const text = body?.textContent || "";
        return /Welcome/.test(text) && text.indexOf("Welcome") < text.indexOf("Reminder")
          && /Welcome home, sir\./.test(text) && /Reminder: take out the bins\./.test(text)
          && /SENT/.test(text) && /Mailbox/.test(text)          // resolved friendly_name
          && /media_player\.gone/.test(text);                    // entity-id fallback when unresolved
      })()],
    ["spoken history: each entry has a Repeat button", sRoot.querySelectorAll(".new-spoken-repeat").length === 2],
  );
  sRoot.querySelector('.new-spoken-repeat[data-spoken-id="5"]').click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["spoken history: Repeat button calls nova/repeat_spoken with spoken_id (never id)",
    repeatCalls.length === 1 && repeatCalls[0] === 5]);

  hass.callWS = async (m) => {
    if (m.type === "nova/get_spoken_history") throw new Error("boom");
    return spokenCallWS(m);
  };
  await elNew._fetchSpokenHistory();
  sRoot = elNew.shadowRoot;
  checks.push(["spoken history: distinct error state when the request fails",
    /Couldn't load spoken history\./.test(sRoot.getElementById("spokenHistoryEntries")?.textContent || "")]);
  hass.callWS = spokenCallWS;

  // ── Actions (Action Audit Log) — beside Spoken History, read-only ──
  const actionsViewBtn = Array.from(sRoot.querySelectorAll(".new-logview")).find(b => b.getAttribute("data-view") === "actions");
  actionsViewBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["actions tab: heading always renders",
      /Actions/.test(sRoot.querySelector(".panel-title")?.textContent || "")],
    ["actions tab: empty result shows a clear empty state",
      /No actions recorded yet\./.test(sRoot.getElementById("actionEntries")?.textContent || "")],
  );

  const actionsCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/list_actions") return { requests: [
      {
        request_id: "req-bulk-1", ts_created: 1700000000, action: "bulk_control",
        source: "voice", requested_by_user_id: null, requested_by_name: null,
        request_device_id: "dev-sat-1", status: "partial", spoken_history_id: 5,
        targets: [
          { entity_id: "light.kitchen", domain: "light", service: "turn_off",
            approval_result: "not_required", execution_result: "verified", reason_text: null },
          { entity_id: "light.hall", domain: "light", service: "turn_off",
            approval_result: "not_requested", execution_result: "blocked",
            reason_text: "requires per-device confirmation; skipped in bulk control" },
        ],
      },
      {
        request_id: "req-single-1", ts_created: 1699990000, action: "control_device",
        source: "chat", requested_by_user_id: null, requested_by_name: null,
        request_device_id: null, status: "success", spoken_history_id: null,
        targets: [
          { entity_id: "lock.front_door", domain: "lock", service: "lock",
            approval_result: "not_required", execution_result: "verified", reason_text: null },
        ],
      },
    ], next_cursor: { ts: 1699990000, request_id: "req-single-1" } };
    return actionsCallWS(m);
  };
  await elNew._fetchActions();
  sRoot = elNew.shadowRoot;
  const actionGroups = sRoot.querySelectorAll("#actionEntries details");
  checks.push(
    ["actions tab: renders one row per REQUEST, not one per target (2 requests, 3 targets total)",
      actionGroups.length === 2],
    ["actions tab: mixed target outcomes render as PARTIAL, not success or failed",
      /PARTIAL/.test(actionGroups[0]?.textContent || "")],
    ["actions tab: a fully-verified single-target request renders as SUCCESS",
      /SUCCESS/.test(actionGroups[1]?.textContent || "")],
    ["actions tab: per-target approval and execution results are both shown, separately",
      /approval: not_required/.test(actionGroups[0].textContent) && /execution: verified/.test(actionGroups[0].textContent)
      && /approval: not_requested/.test(actionGroups[0].textContent) && /execution: blocked/.test(actionGroups[0].textContent)],
    ["actions tab: the policy-skip reason is shown for the blocked target",
      /requires per-device confirmation; skipped in bulk control/.test(actionGroups[0].textContent)],
    ["actions tab: a linked Spoken History entry is indicated",
      /spoken/i.test(actionGroups[0].textContent)],
    ["actions tab: LOAD MORE appears when a next_cursor is present",
      sRoot.getElementById("actionLoadMoreRow")?.hidden === false],
    ["actions tab: no retry/replay/approve/reject/run button anywhere in the view",
      !sRoot.getElementById("actionEntries").querySelector(
        "button, [data-retry], [data-approve], [data-reject], [data-run], [data-replay]")],
  );

  hass.callWS = async (m) => {
    if (m.type === "nova/list_actions") throw new Error("boom");
    return actionsCallWS(m);
  };
  await elNew._fetchActions();
  sRoot = elNew.shadowRoot;
  checks.push(["actions tab: distinct error state when the request fails",
    /Couldn't load actions\./.test(sRoot.getElementById("actionEntries")?.textContent || "")]);
  hass.callWS = actionsCallWS;

  // ── New look: Memory tab (ported from Classic's own Memory tab) ──
  // Force an empty pending-facts queue explicitly, rather than asserting
  // against the fixture's original seed fact, so this section's outcome
  // doesn't depend on what ran before it.
  _pendingFacts = [];
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
    // _pendingFacts was forced empty above, so the panel should be hidden.
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

  // Pending Confirmation: inject a fresh pending fact directly (independent
  // of the _pendingFacts fixture, which was forced empty above) to
  // exercise confirm/reject/edit for real.
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

  // ── New look: Intrusion tab (ported from Classic's own Intrusion tab) ──
  // Safety-relevant (call-off/acknowledge affect real escalation), so this
  // is a straight port of Classic's original exact websocket calls, not
  // new behavior. A fresh callWS override is used here for a clean,
  // deterministic ARMED starting state rather than depending on any
  // earlier section's mock state.
  const intrusionTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "intrusion");
  intrusionTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  const intrusionCallWS = hass.callWS;
  let _fakeCalledOff = false;
  hass.callWS = async (m) => {
    if (m.type === "nova/intrusion") {
      if (m.action === "dismiss") { _fakeCalledOff = true; return { ok: true, last_snapshot: _intrSnap, called_off: true, suppressed_for: 600, false_alarms_24h: 1 }; }
      if (m.action === "acknowledge") return { ok: true, last_snapshot: _intrSnap, called_off: _fakeCalledOff, acknowledged: true, suppressed_for: 0, false_alarms_24h: 0 };
      return { last_snapshot: _intrSnap, called_off: _fakeCalledOff, acknowledged: false, suppressed_for: 0, false_alarms_24h: 0 };
    }
    return intrusionCallWS(m);
  };
  await elNew._fetchIntrusion();
  sRoot = elNew.shadowRoot;
  checks.push(
    ["intrusion tab: real card shows ARMED with the last snapshot and controls",
      /ARMED/.test(sRoot.getElementById("newIntrStatus")?.textContent || "")
      && !!sRoot.querySelector(".intr-img")
      && !!sRoot.querySelector('select[data-cfg-key="intrusion_response_timeout"]')
      && !!sRoot.querySelector('button[data-cfg-key="intrusion_vision_confirm"]')
      && !!sRoot.querySelector(".new-intr-ack") && !!sRoot.querySelector(".new-intr-dismiss")],
  );
  // Reused generic autosave, but on a non-Settings tab for the first time —
  // confirms the _saveSetting render-guard broadening actually works here.
  const visionToggle = sRoot.querySelector('button[data-cfg-key="intrusion_vision_confirm"]');
  visionToggle.click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["intrusion tab: vision-confirm toggle autosaves via the shared generic handler",
    _updateConfigCalls.some(c => c.key === "intrusion_vision_confirm" && c.value === false)]);
  sRoot.querySelector(".new-intr-dismiss").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["intrusion tab: CALL OFF calls nova/intrusion dismiss and updates the status",
    /CALLED OFF/.test(sRoot.getElementById("newIntrStatus")?.textContent || "")]);
  hass.callWS = intrusionCallWS;

  await elNew._fetchIntrusionLog();
  sRoot = elNew.shadowRoot;
  checks.push(
    ["intrusion log: real card lists events with kind, snapshot, and label controls",
      sRoot.querySelectorAll(".new-ilog-item").length === 2
      && /CONFIRMED/.test(sRoot.getElementById("newIlogBody")?.textContent || "")
      && !!sRoot.querySelector(".intr-img")
      && /1\/2 labelled/.test(sRoot.getElementById("newIlogLearn")?.textContent || "")],
    ["intrusion log: an already-labelled event shows its CLEAR option, marks the right button active",
      (() => {
        const item2 = sRoot.querySelector('.new-ilog-item[data-ev="evt_2"]');
        return !!item2 && !!item2.querySelector('.new-ilog-btn[data-label=""]')
          && item2.querySelector('.new-ilog-btn[data-label="false"]')?.classList.contains("mode-chip-on");
      })()],
  );
  sRoot.querySelector('.new-ilog-item[data-ev="evt_1"] .new-ilog-btn[data-label="real"]').click();
  await new Promise(r => setTimeout(r, 20));
  checks.push(["intrusion log: labelling an event calls nova/intrusion label with the right event/label",
    _intrLabelCalls.some(c => c.event_id === "evt_1" && c.label === "real")]);

  // ── New look: Suggestions tab (ported from Classic's own Suggestions tab) ──
  // Data rides on the same nova/get_panel_data payload already polled for
  // the dashboard (d.suggestions) — no separate fetch to wire up.
  const suggestionsTabBtn = Array.from(newRoot.querySelectorAll(".nav-tab")).find(b => b.getAttribute("data-tab") === "suggestions");
  suggestionsTabBtn.click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(
    ["suggestions tab: real card shows the pattern type, confidence, and evidence",
      (() => {
        const card = sRoot.querySelector(".new-sug");
        return !!card && /Daily routine/.test(card.textContent) && /82% confident/.test(card.textContent)
          && /Turn porch light on/.test(card.textContent) && /light\.porch/.test(card.textContent)
          && /seen 6× in 30 days/.test(card.textContent);
      })()],
    ["suggestions tab: YAML is hidden until toggled", sRoot.querySelector(".new-sug-yaml")?.hidden === true],
  );
  sRoot.querySelector(".new-sug-yaml-btn").click();
  checks.push(["suggestions tab: 'See the automation' reveals the YAML", sRoot.querySelector(".new-sug-yaml")?.hidden === false]);
  sRoot.querySelector(".new-sug-approve").click();
  await new Promise(r => setTimeout(r, 20));
  sRoot = elNew.shadowRoot;
  checks.push(["suggestions tab: approve calls nova/suggestion_action and dims the card in place",
    _sugCalls.some(c => c.id === 11 && c.action === "approve")
    && sRoot.querySelector(".new-sug")?.style.opacity === "0.35"
    && Array.from(sRoot.querySelectorAll(".new-sug button")).every(b => b.disabled)]);

  // Post-v7.103.0 fix: Installed Automations used to vanish entirely —
  // _htmlAutomationTrials() returned "" for an empty result, and the
  // no-suggestions early-return in _htmlSuggestions() never called it at
  // all. It must always render its heading, with a distinct empty vs.
  // error state. The default mock returns {} for nova/list_automation_trials,
  // exercising the real empty-result path already reached above.
  checks.push(["suggestions tab: Installed Automations section renders with an empty result",
    /Installed Automations/.test(sRoot.textContent)
    && /No tracked Nova automations yet\. Automations installed from new suggestions will appear here\./.test(sRoot.textContent)]);
  const automationTrialsCallWS = hass.callWS;
  hass.callWS = async (m) => {
    if (m.type === "nova/list_automation_trials") throw new Error("boom");
    return automationTrialsCallWS(m);
  };
  await elNew._fetchAutomationTrials();
  sRoot = elNew.shadowRoot;
  checks.push(["suggestions tab: Installed Automations shows a distinct error state when the request fails",
    /Installed Automations/.test(sRoot.textContent) && /Couldn't load installed automations\./.test(sRoot.textContent)]);
  hass.callWS = automationTrialsCallWS;
  await elNew._fetchAutomationTrials();
  sRoot = elNew.shadowRoot;

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

  if (elNew._fetchInterval) clearInterval(elNew._fetchInterval);
  if (elNew._sparklineInterval) clearInterval(elNew._sparklineInterval);
  if (elNew._animHandle) cancelAnimationFrame(elNew._animHandle);

  // v7.93.2 regression guard: a real live bug — the new look's custom
  // properties were declared under `:root{}`, which matches NOTHING inside
  // a shadow tree (unlike `:host{}`, the correct selector for a shadow
  // root's own scoped tokens). Every var(--x) silently failed, stripping
  // every background/border/font with no visible error — jsdom doesn't do
  // real CSS layout, so no structural check here would ever have caught
  // it; only reading the source text can.
  const panelSrc = fs.readFileSync(COMPONENT, "utf8");
  checks.push(["new look declares its CSS tokens on :host, not :root (shadow DOM)",
    /:host\s*\{/.test(panelSrc) && !/(^|[^-\w]):root\s*\{/.test(panelSrc)]);

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
    /\.settings-card\[hidden\]\s*\{\s*display\s*:\s*none\s*\}/.test(panelSrc)]);

  // Regression guard: appliance_announce_unknown/appliance_power_guessing no
  // longer have any effect on whether Nova announces a cycle (the provenance
  // gate is unconditional) -- neither the removed setting keys, the removed
  // toggle's element id, nor the old "falls back to generic power guesses"
  // empty-state wording (which implied unidentified loads could announce)
  // may reappear anywhere in the panel source.
  checks.push(["panel source: dead appliance settings/wording were fully removed, not left dangling",
    !/appliance_announce_unknown/.test(panelSrc)
      && !/appliance_power_guessing/.test(panelSrc)
      && !/newApplianceUnknown/.test(panelSrc)
      && !/generic power guesses/.test(panelSrc)]);

  let ok = true;
  for (const [n, p] of checks) { console.log((p ? "  PASS  " : "  FAIL  ") + n); if (!p) ok = false; }
  console.log(ok ? "\nSMOKE TEST CLEAN" : "\nSMOKE TEST FAILED");
  process.exit(ok ? 0 : 1);
}, 350);
