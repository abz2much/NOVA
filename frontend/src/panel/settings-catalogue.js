  // ─── Settings ─────────────────────────────────────────────────────────
  // Reorganized around what you're trying to do rather than which Nova
  // subsystem it touches — the old Classic split (System Diagnostics
  // under General, a separate Diagnostics under Cameras) is merged here
  // into one place. Every card is "real:true" — nothing here is a stub.
  // Mirrors const.py's HONORIFIC_OPTIONS — kept in sync by hand, same as
  // AREA_CAP_ORDER/AREA_CAP_ICON below mirror their own backend source.
  static HONORIFIC_OPTIONS = ["sir", "ma'am", "boss", "friend"];

  static SETTINGS_GROUPS = [
    { id: "general", label: "General" },
    { id: "voice", label: "Voice & Speakers" },
    { id: "safety", label: "Awareness & Safety" },
    { id: "learning", label: "Learning & Memory" },
    { id: "cameras", label: "Cameras" },
    { id: "home", label: "Home & Extras" },
  ];

  static SETTINGS_CARDS = [
    { id: "general", group: "general", title: "General", real: true,
      desc: "Language, sleep state, and the core proactive-speech switches." },
    { id: "person_honorifics", group: "general", title: "Person Honorifics", real: true,
      desc: "What Nova calls each person when they're home alone. Drops the address entirely the moment more than one person — or nobody — is home." },
    { id: "residence_home", group: "general", title: "Residence / Home", real: true,
      desc: "Home style, stories, and layout counts that feed the Residence 3D view." },
    { id: "operational_mode", group: "general", title: "Operational Mode", real: true,
      desc: "Party/movie/away modes and what each one changes while active." },
    { id: "diagnostics", group: "general", title: "Diagnostics", real: true,
      desc: "Service health checks and system status (merged from Classic's two separate diagnostics cards)." },
    { id: "room_speakers", group: "voice", title: "Room Speakers", real: true,
      desc: "Assign the one speaker Nova may use per room, plus a general fallback." },
    { id: "ai_models", group: "voice", title: "AI Models", real: true,
      desc: "Provider and model per tier — main agent, classifier, reasoning, review, vision." },
    { id: "briefings", group: "voice", title: "Briefings", real: true,
      desc: "Daily briefing schedule, content, and delivery speakers." },
    { id: "voice_confirmation", group: "voice", title: "Voice Confirmation", real: true,
      desc: "Whether risky actions need a spoken or phone confirmation before Nova acts." },
    { id: "satellite_speaker", group: "voice", title: "Satellite → Speaker", real: true,
      desc: "Per-satellite override, for a specific satellite that shouldn't use its room's assigned speaker." },
    { id: "announcement_speakers", group: "voice", title: "Announcement Speakers", real: true,
      desc: "Which speakers whole-house broadcasts (briefings, sentinel alerts) use." },
    { id: "notifications", group: "safety", title: "Notifications", real: true,
      desc: "Your phone's notify service, for alerts when nobody's home to hear a speaker." },
    { id: "security_alarm", group: "safety", title: "Security Alarm", real: true,
      desc: "Choose the one alarm Nova uses for security decisions. Automatic lockdown is always opt in." },
    { id: "sentinel_rules", group: "safety", title: "Sentinel Rules", real: true,
      desc: "Enable or disable individual door/lock/garage anomaly rules." },
    { id: "hazard_monitor", group: "safety", title: "Hazard Monitor", real: true,
      desc: "Earthquake, severe weather, and disaster feeds near your home." },
    { id: "energy_management", group: "safety", title: "Energy Management", real: true,
      desc: "Peak-draw threshold and how much say Nova has over high-draw appliances." },
    { id: "host_health", group: "safety", title: "Host Health", real: true,
      desc: "Home Assistant's own System Monitor readings for the machine Nova runs on — off by default." },
    { id: "appliances", group: "safety", title: "Appliances", real: true,
      desc: "Declared appliance profiles Nova fingerprints by wattage." },
    { id: "anticipation_memory", group: "learning", title: "Anticipation & Memory", real: true,
      desc: "Cross-session memory window, continued conversation, and multi-satellite follow." },
    { id: "memory_curated", group: "learning", title: "Memory", real: true,
      desc: "Memory backend and how many memories are stored. Full review/edit lives on the Memory tab." },
    { id: "observer_tuning", group: "learning", title: "Observer Tuning", real: true,
      desc: "How cautious or talkative the proactive Observer is." },
    { id: "routine_learning", group: "learning", title: "Routine Learning", real: true,
      desc: "What Nova is allowed to learn from — doors, presence, button presses." },
    { id: "excluded_entities", group: "learning", title: "Excluded Entities", real: true,
      desc: "Entities, domains, or labels Nova should ignore entirely." },
    { id: "cameras", group: "cameras", title: "Cameras", real: true,
      desc: "Camera names, indoor/outdoor designation, and location overrides." },
    { id: "doorbell_training", group: "cameras", title: "Doorbell Training", real: true,
      desc: "Teach Nova to recognize regular visitors at the door." },
    { id: "floor_plan_editor", group: "home", title: "Floor Plan Editor", real: true,
      desc: "Rooms, outdoor zones, property line, windows/doors/dormers, camera placement, a background image, and AI camera-coverage estimation." },
    { id: "wellbeing_context", group: "home", title: "Wellbeing Context", real: true,
      desc: "Whether wearable heart-rate/sleep data reaches Nova, and which providers." },
    { id: "character_research", group: "home", title: "Nova Character & Research", real: true,
      desc: "Banter level and the web-research backend (DuckDuckGo or self-hosted SearXNG)." },
    { id: "document_library", group: "home", title: "Document Library", real: true,
      desc: "Manuals and receipts Nova can search and cite from." },
  ];

