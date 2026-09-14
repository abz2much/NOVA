<div align="center">

# Nova AI Assistant

An autonomous AI butler for Home Assistant: voice, vision, and a reasoning core that learns your home and watches over it.

<img src="docs/media/hero-stellar-core.svg" alt="Nova Command Center — animated stellar-core dashboard" width="100%">

[![HACS Integration](https://img.shields.io/badge/HACS-Integration-41BDF5?logo=home-assistant&logoColor=white)](https://github.com/abz2much/NOVA)
[![Release](https://img.shields.io/github/v/release/abz2much/NOVA?color=00d9ff)](https://github.com/abz2much/NOVA/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

Nova installs as a Home Assistant custom integration through HACS and runs entirely inside HA, with no separate container and no cloud account required to start. Its design principle is simple: suggest, don't act. Nova starts conservative, tells you what it notices, and only takes on more autonomy as you let it.

> **Active development.** Nova is a solo-maintained project under heavy, ongoing change — expect frequent releases, including breaking changes between versions, while things settle. It's genuinely usable today, but not a "set it up once and forget it" integration yet. Check `CHANGELOG.md` before updating if you want to know what changed.

## Quick start (5 minutes, no cameras required)

Nova looks elaborate, but the floor is low. You can be talking to it in five minutes with nothing but Home Assistant and one free API key. Cameras, voice hardware, and local GPU inference are all optional upgrades you add later.

1. **Install via HACS.** Add this repo (badge below), install "Nova AI Assistant," restart Home Assistant.
2. **Add the integration.** Go to *Settings → Devices & Services → Add Integration → Nova*. Paste a cloud API key from [Groq](https://console.groq.com), Anthropic, OpenAI, or Gemini; Nova detects which provider it belongs to from the key itself, no picker needed. Or leave it blank and point it at a local Ollama URL to run with no cloud account at all. Groq has a free tier if you want to try it without paying anything.
3. **That's it.** Nova registers its conversation agent and appears in your sidebar. Ask it about your home, your calendar, or the outside world.

Everything past this point (vision, doorbell analysis, Classic's live 3D floor plan, proactive safety) layers on top as you connect cameras and voice. None of it is required to start. Jump to [Installation](#installation) for the full walkthrough.

## What it does

### Voice and conversation

A pluggable LLM brain (Groq, Gemini, OpenAI, Anthropic, or a local Ollama server) drives natural conversation through the Home Assistant voice pipeline, answered in a custom Piper TTS voice. Works with ESP32-S3 satellites, Wyoming, and Google speakers.

### Web research and schedule awareness

Ask about the outside world: current events, facts, "what's the latest on…" and Nova looks it up. DuckDuckGo works out of the box with no key, or point it at a self-hosted SearXNG. It also reads your `calendar.*` entities, surfacing upcoming events and flagging conflicts such as overlaps and tight back-to-back transitions.

### Answers from your own paperwork

Drop appliance manuals and receipts (PDF, `.txt`, `.md`) into `/config/nova/documents`, then ask "what's the filter size for the furnace?" or "when did we buy the dishwasher?" Nova answers from your documents and cites the source. Retrieval is keyword-based out of the box; with Ollama running, flip on semantic search for meaning-based matching, nothing extra to install.

### The Nova voice

Modelled on Stark's Nova: dry, precise, unflappable, and quietly witty, but strictly situational about it. The wit stays out of the way the moment something is wrong; Nova does not joke during a smoke alarm. A banter level setting (plain / dry / full) tunes how much character surfaces, and urgent or grave events always speak plainly no matter what that setting is.

### Vision and cameras

Automatic doorbell-press analysis using a two-pass live-clip and recorded-event approach, package and mail detection on porch cameras, and silent visitor learning that quietly builds a picture of who comes and goes. All of it runs on vision models reasoning over Nest and Frigate feeds.

### The Cognitive Core

A reasoning loop that classifies every household event by urgency and decides whether it's worth your attention. It grounds decisions in your home's actual history ("the kitchen light at 7am is routine; the basement window has never opened before"), escalates security-relevant events when you're away, and proposes automations from patterns it observes.

### The Local Mind

When the cloud is unreachable, Nova doesn't go dumb. An offline reasoning brain replicates the full decision procedure (self-awareness, historical grounding, case-based memory, situational judgment, persona phrasing), so it keeps making sound, well-spoken calls with no internet at all.

### Safety and security

Proactive monitoring for freezing pipes, smoke, CO, water, unauthorized entry, and nighttime lockdown. Enforcement is occupancy-gated, so it only kicks in when it should.

### Two dashboard looks, your choice

**Command Center** trades the dark-cyan glassmorphism look for a warm ember/gold "stellar core" centerpiece — an animated particle core whose state (idle, reasoning, asleep) reflects what Nova is actually doing, so the dashboard looks and feels the same whether you have zero cameras or twelve. Areas show live capability icons, temperature/humidity sparklines, and a light toggle per room; Settings is reorganized around what you're doing rather than which subsystem it touches, right down to a per-person "who does Nova call whom" card. Turn it on from *Settings → Devices & Services → Nova → Configure → "Panel look"* — only the 3D Residence view stays on Classic for now.

**Classic** is still the default: the original dashboard with a live isometric 3D house, per-room occupancy glow, radial telemetry gauges, an event feed, a doorbell-training view. Switch between the two anytime; nothing is deleted.

<div align="center">
<table border="0">
<tr>
<td width="50%"><img src="docs/media/areas-card.svg" alt="Command Center Areas card with capability icons, sparklines, and a light toggle" width="100%"></td>
<td width="50%"><img src="docs/media/settings-card.svg" alt="Command Center Settings tab, showing the per-person Honorifics card" width="100%"></td>
</tr>
<tr>
<td align="center"><em>Every monitored room, its live capability icons, a temperature/humidity trend, and a one-tap light toggle.</em></td>
<td align="center"><em>Settings reorganized by task, not subsystem — down to what Nova calls each person when they're home alone.</em></td>
</tr>
</table>

<sub>Visuals reflect the panel's actual design system. The live dashboard renders in your browser inside Home Assistant.</sub>
</div>

## Full capability reference

Everything Nova can do today, grouped by domain. In conversation these surface as tools the agent invokes on its own; most also have a panel control.

**Conversation and delegation**
- Natural voice and text conversation through the Home Assistant pipeline, in a dry, MCU-JARVIS-inspired persona with a tunable banter level.
- Ephemeral sub-agents (`delegate_task`): for a complex, self-contained slice of a request, Nova spins up a focused in-process sub-agent with a minimal objective, a curated read-only tool subset, and a small turn budget, then folds the result back. Sub-agents can't actuate or recurse, and depth is capped. On a single-GPU host they run serially, so the benefit is a tight, focused context rather than parallelism.
- Five specialist bridge tools (`ask_executive_assistant`, `ask_marketing_agent`, `ask_security_privacy_agent`, `ask_homelab_infra_agent`, `ask_house_manager_agent`) hand off to separate n8n-orchestrated agents over a private webhook. These need their own n8n setup to work and aren't something a fresh install has access to out of the box.

**Devices, scenes and home state**
- Control one device or many at once, run scenes and scripts, and execute multi-step plans (`control_device`, `bulk_control`, `run_scene_or_script`, `execute_plan`).
- Query live state, search entities, list a room's devices, and summarize the whole home (`get_entity_state`, `search_entities`, `get_area_devices`, `get_home_summary`).
- Read Home Assistant's recorded history and logbook to answer "what happened while I was out" (`activity_history`).

**Schedule and communications**
- Read household `calendar.*` entities, surface upcoming events, and flag overlaps and tight back-to-back transitions (`calendar_agenda`).
- Read-only email (`read_email`): checks the inbox over IMAP, read-only by construction (opens with EXAMINE, fetches with BODY.PEEK, never marks, moves, or deletes). Fetched mail is sanitized and treated as untrusted. The password lives in `secrets.yaml`.
- Scheduled morning and evening briefings covering weather, calendar, overnight events, energy, and active hazards, occupancy-gated.

**Web and your documents**
- Look up the outside world (`web_research`) using DuckDuckGo out of the box, or a self-hosted SearXNG.
- Answer from your own manuals and receipts (`search_documents`, `ingest_documents`), keyword-based out of the box or Ollama-embedded semantic search.

**Cameras and vision**
- Look at a camera on demand and describe it (`look_at_camera`); report who's recognized at the door (`who_do_you_see`).
- Automatic doorbell-press analysis, package and mail detection, and silent visitor learning over Nest and Frigate feeds.

**Awareness, diagnostics and energy**
- Report the cognitive core's state, connectivity, and a full self-diagnostic (`cognitive_status`, `connectivity_status`, `system_diagnostics`); reason about the root cause of a fault (`root_cause`).
- Whole-home energy: current draw, peak awareness, and load advice with tunable agency (`energy_status`).

**Weather and hazards**
- Local forecast (`weather_forecast`) and a live multi-hazard report covering nearby earthquakes (USGS), severe weather (NWS), and disasters (NASA EONET) (`hazard_report`).

**Safety and security**
- Proactive monitoring for freezing pipes, smoke, CO, water, unauthorized entry, and nighttime lockdown, occupancy-gated so enforcement only happens when it should.
- Confirm or dismiss intrusion events and acknowledge alerts by voice (`dismiss_intrusion`, `acknowledge_alert`); optional voice confirmation before sensitive actions like unlocking.

**Modes, memory, goals and suggestions**
- Set operational modes, including custom ones (`set_mode`), and tune how much Nova acts on its own (`manage_autonomy`).
- Remember facts you tell it (`remember`); open and track standing goals (`create_goal`, `update_goal`, `manage_goals`) and schedule follow-ups (`schedule_followup`, `manage_followups`).
- Review, approve, or dismiss the automations it proposes from observed patterns (`review_suggestions`, `approve_suggestion`, `dismiss_suggestion`).
- Mute a noisy entity from awareness, or bring it back (`ignore_entity`, `unignore_entity`); read opt-in wearable and wellbeing context (`wellbeing_context`).

**Resilience and privacy**
- The Local Mind replicates the full decision procedure offline, so Nova stays useful with no internet at all.
- All LLM credentials live in Home Assistant's `secrets.yaml`, never in plaintext panel config. Any existing plaintext keys are relocated automatically and safely (verify-before-strip) on upgrade.
- A single config resolver acts as the one source of truth for which model each part of Nova runs on.

## Requirements

To start, you need exactly two things:

- **Home Assistant** with [HACS](https://hacs.xyz) installed.
- **One LLM API key.** [Groq](https://console.groq.com) has a generous free tier and is the recommended starting point, or run fully local with Ollama and no key at all.

Optional add-ons unlock more, but none are required to begin:

- *Voice*: HA OS / Supervised is recommended; Nova auto-installs the Piper, Whisper, and openWakeWord voice stack through the Supervisor. On Container/Core you'd add those yourself.
- *Vision*: a Gemini API key for camera reasoning, plus cameras. Any HA camera works, but Frigate is the recommended backbone for detection and snapshots, and Nest cameras and doorbells are supported through it.
- *Voice hardware*: ESP32-S3 satellites and a Piper TTS voice.
- *Fully local inference*: a GPU box running Ollama. Point `llm_base_url` at it and Nova runs entirely on your own hardware, with no cloud account.

## Installation

**1. Add this repository to HACS.**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=abz2much&repository=NOVA&category=Integration)

Click the badge above, or do it manually: in **HACS → ⋮ (top right) → Custom repositories**, add the URL below with category **Integration**.

```
https://github.com/abz2much/NOVA
```

**2. Install "Nova AI Assistant"** from HACS, then restart Home Assistant.

**3. Add the integration.** Go to **Settings → Devices & Services → Add Integration → Nova**. Enter a cloud API key from Groq, Anthropic, OpenAI, or Gemini; Nova detects the provider from the key's own shape, so there's no separate picker. Or leave it blank and enter a local LLM URL (for example `http://homeassistant.local:11434/v1`) to run Ollama with no cloud account. Nova registers its conversation agent and appears in the sidebar.

**4. Set up voice (optional).** On Home Assistant OS / Supervised, Nova bootstraps the voice stack itself on first run: it installs and starts the Piper, Whisper, and openWakeWord add-ons, downloads the Nova voice, and creates an Assist pipeline with Nova as the conversation agent. On Container/Core installs, with no Supervisor, install those pieces yourself and create the pipeline through Settings → Voice Assistants.

**5. Fine-tune (optional).** Advanced routing, observer mode, camera watching, and the AI-model-per-role assignments are all configured from the Nova panel under **Settings**.

> **Hard-refresh after updates.** The dashboard JavaScript is cached aggressively; after upgrading, refresh with `Ctrl+Shift+R` so the new panel loads.

## Advanced setup: cameras and continuous streaming

Everything in this section is optional. You only need it for the vision features (doorbell analysis, package detection, the HUD's live camera tiles). Skip it if you're starting with voice and reasoning and come back when you want cameras.

### How Nova uses cameras

Nova doesn't require any specific camera brand. It works with any camera Home Assistant exposes as a `camera.*` entity, pulling stills through the standard image API. But it's built to lean on [Frigate](https://frigate.video) as the backbone, and that's the recommended setup.

Frigate is the funnel: its on-camera object detection for people and packages, fired over MQTT, is far more reliable than a vision model guessing at a raw feed, and its event snapshots are high-resolution and cropped to the detection. Nova listens to `frigate/events`, consumes those snapshots directly, and its face recognition reads Frigate's `tracked_object_update` and `last_recognized_face` channels. If you run cameras at all, routing them through Frigate is what makes the vision features sharp.

Nest is a supported source, not a requirement. Nova can consume Nest cameras and doorbells too, but Google's cameras need extra plumbing (below) because of how their stream API behaves. If you have Nest gear, route it into Frigate through go2rtc and you get the best of both: Nest's doorbell events plus Frigate's detection and durable frames.

Any other camera, generic RTSP, local ONVIF, and so on, works through the standard snapshot path with no special setup. Add it to Frigate for detection, or let Nova pull stills directly.

The rest of this section is Nest-specific. If you don't use Nest, skip it entirely: point Frigate at your cameras and you're done.

### Nest cameras (only if you use Nest)

Nova consumes Nest cameras and doorbells through the official [Google Nest integration](https://www.home-assistant.io/integrations/nest/). It does not, and legally cannot, talk to Google's Smart Device Management API with its own credentials, because Google binds SDM access to your Google account and Device Access project. One-time setup:

1. **Google SDM API.** Create a project in the [Device Access Console](https://console.nest.google.com/device-access) (US $5 one-time fee) and a Google Cloud project with the SDM API enabled and OAuth credentials.
2. **Credentials in HA.** Add your OAuth Client ID and Secret under *Settings → Devices & Services → Application Credentials*, then add the Google Nest integration and authorize it. Your cameras and doorbell appear as `camera.*` entities.
3. **That's it for Nova.** It auto-detects Nest-platform cameras and uses the right frame source for each event (event media, stream-wake, or its own snapshot path). Battery/WebRTC-only Nest cameras can't produce ordinary still images while idle; the Nova panel handles this by escalating to its own snapshot tier automatically, so the tile shows frames instead of going blank.

### Continuous streaming for Nest cameras (recommended if you use Nest)

Google's SDM API hands out WebRTC/RTSP stream URLs that expire roughly every five minutes and won't reliably produce a still image while a camera is idle. That's fine for the occasional glance, but it means live tiles can stall and 24/7 NVR recording (Frigate) chokes.

The durable fix, and the one Nova is built to lean on, is restreaming each Nest camera through [go2rtc](https://github.com/AlexxIT/go2rtc), which speaks Google's SDM protocol natively, renews the expiring stream transparently, and republishes a solid RTSP/WebRTC feed that Home Assistant, Frigate, and Nova all consume like any local camera. If you already run Frigate, you already have a go2rtc instance bundled with it, which is the other reason Frigate is the recommended backbone: it does double duty as both your detector and your Nest restreamer.

**1. Point go2rtc at your Nest account.** In your go2rtc (or Frigate) config, add a `nest:` source per camera. You need five values, all from the same Device Access setup above:

```yaml
go2rtc:
  streams:
    bedroom2_restream:
      - "nest:?client_id=CLIENT_ID&client_secret=CLIENT_SECRET&refresh_token=REFRESH_TOKEN&project_id=DEVICE_ACCESS_PROJECT_ID&device_id=DEVICE_ID"
    front_doorbell_restream:
      - "nest:?client_id=CLIENT_ID&client_secret=CLIENT_SECRET&refresh_token=REFRESH_TOKEN&project_id=DEVICE_ACCESS_PROJECT_ID&device_id=DOORBELL_DEVICE_ID"
```

- `client_id` / `client_secret`: the same OAuth pair you added under Application Credentials.
- `project_id`: the Device Access Console project UUID (not the Google Cloud project).
- `refresh_token`: from the Nest integration's stored config. In *Settings → Add-ons → File editor* (or SSH), open `.storage/core.config_entries`, find the `nest` entry, and copy its `refresh_token`.
- `device_id`: easiest through the go2rtc web UI (Frigate exposes it on port `1984`). Choose Add → nest, supply the other four values, and it lists your devices with their IDs. Copy the one you want.

**2. (Optional) add the restreams as Frigate cameras.** If you want continuous recording and object detection, add each `*_restream` as a Frigate camera and enable `detect`/`record`. Frigate's on-camera object detection for people and packages is more reliable than vision-LLM guessing, and Nova will happily consume Frigate's snapshots.

**3. Tell Nova to source frames from the twins.** Restart Home Assistant so the new `camera.*_restream` entities exist, then map each Nest camera to its restream in Nova's config (`camera_overrides`). The Nest entity keeps its identity (chips, names, doorbell events), while every frame comes from the durable restream:

```json
{
  "camera_overrides": {
    "camera.bedroom2_camera": "camera.bedroom2_restream",
    "camera.front_doorbell": "camera.front_doorbell_restream"
  }
}
```

This lives in `/config/nova/config.json`; merge it into the existing object rather than replacing the file. Nova validates this on load, so a typo is sidelined with a notification rather than breaking the panel. Then open **Camera Watch → DIAG** on the camera. It should report `override → camera.bedroom2_restream` and a healthy full-size frame instead of a blank or black tile.

> **Note:** go2rtc's Nest source is a third-party bridge, and Google occasionally changes its auth behavior. If a restream ever drops, Nova falls back to the original Nest entity automatically; worst case is the pre-restream behavior, never worse.

## Configuration highlights

| Setting | What it does |
| --- | --- |
| `llm_provider` / per-role models | Choose Groq, Gemini, OpenAI, Anthropic, Ollama, or custom, independently for the main agent, classifier, reasoning, review, vision, and camera-reasoning roles. |
| `llm_base_url` | Point the Ollama/custom providers at your local GPU server (for example `http://gpu-server:11434/v1`). |
| `observer_enabled` | Let Nova watch the event stream and decide what's worth surfacing. |
| `rich_reasoning` | Cloud-first judgment for medium/high-urgency events: costs a bit more, reasons better. |
| `visitor_learning` | Silently learn from person events at the door. Never spoken. |
| `package_detection` | Watch porch cameras for packages and mail. |
| `cognition_threshold` | How salient an event must be before Nova escalates it. |

## Languages

Nova follows your Home Assistant language automatically, and you can override it under **Settings → General → Language** (or leave it on Auto). The setup and configuration dialogs are localized through Home Assistant's own translation system; the in-panel HUD is localized by Nova. Anything not yet translated falls back cleanly to English, so nothing breaks.

**Panel UI:** complete for all 18 supported languages: Czech, Danish, Dutch, Finnish, French, German, Italian, Norwegian Bokmål, Polish, Portuguese, Brazilian Portuguese, Romanian, Russian, Slovak, Spanish, Swedish, Turkish, and Ukrainian (English is the source language, built in).

**Setup dialog:** complete for French, German, Spanish, Italian, Portuguese, and Dutch. Other languages fall back to English here; this is Home Assistant's own translation layer, separate from the panel.

### Help translate

Translations are plain JSON files; no code required.

- **Panel UI:** `custom_components/nova/frontend/i18n/<lang>.json`, keyed by the exact English string.
- **Setup dialog:** `custom_components/nova/translations/<lang>.json` (Home Assistant's format).

To add a language or refine an existing one, copy an existing file, translate the values, and keep the keys and technical tokens (entity IDs, model names, URLs) unchanged. Corrections are welcome, especially native-speaker fixes to machine-assisted translations. Right-to-left languages (Arabic, Hebrew, and so on) also need panel layout support, so that's a good area to help with if you're interested.

## Architecture

Nova is a Home Assistant custom integration (domain `nova`, around 86 Python modules) installed through HACS into `custom_components/nova/`. It runs in-process: it registers the conversation agent and voice pipeline and serves the custom dashboard panel directly. State and learned behavior persist under `/config/nova/` (a SQLite `patterns.db`, the curated `knowledge.db`, the reasoning cache, the doorbell-training dataset, and lockdown state), so Nova keeps getting smarter across restarts.

The reasoning pipeline is layered for resilience and cost: local templates, then a learned cache, then cloud (or eventually a local model), with the Local Mind offline brain as the floor beneath everything. A connectivity breaker guards cloud calls, and every local decision logs its reasoning chain to the dashboard's log view.

## Privacy and your data

Nova is local-first. Everything it learns and stores lives inside your Home Assistant instance under `/config/nova/`. There is no Nova cloud, no telemetry, and nothing is sent anywhere except the LLM/vision calls you configure yourself.

What's stored, and where:

- **Learned behavior and patterns:** `patterns.db` (state changes and commands used to propose automations), `person_patterns` (per-person routines), and the reasoning cache. All local SQLite.
- **Knowledge and memory:** the curated `knowledge.db` and conversation memory (vectors or FTS), local SQLite. Editable and erasable from the dashboard.
- **Documents:** anything you drop in `/config/nova/documents` for the RAG agent, plus its index and vectors. Ingestion is path-guarded so it only ever reads inside that folder.
- **Camera and vision:** snapshots are analyzed on demand and not retained by Nova; recording is Frigate's job, under your control.
- **Biometrics:** off by default and opt-in. When enabled, Nova reads wearable entities Home Assistant already exposes for comfort context (being quieter when a sleep sensor says you're resting, for example). This context only ever reaches the model when you're running a local Ollama provider — with a cloud provider configured (Groq, OpenAI, Anthropic, Gemini) it's withheld entirely, so heart-rate/sleep readings never leave your network. It is explicitly not medical: it never diagnoses, alarms on, or clinically interprets a reading, and anything concerning is left to your own device or a medical professional.

What leaves your network is only what you choose: requests to whichever LLM provider (Groq, OpenAI, Anthropic) and vision model you configure, or nothing at all if you run everything locally through Ollama. Swap any provider for a local model to keep the whole pipeline on premises. Sensitive integration credentials are held by Home Assistant, not Nova.

## What's different from upstream

Nova started as a fork of [jarvis-aio](https://github.com/sam3gp8/jarvis-aio) and shares most of its architecture — jarvis-aio is itself actively developed, not a frozen base. This section tracks where Nova has genuinely diverged, updated as real changes ship rather than left to go stale.

**Security hardening**
- Voice commands can lock a door instantly, but can never unlock one or open a garage — that always requires a tap on your phone, so a spoofed or deepfaked voice can't grant physical access on its own.
- The `execute_plan` tool (multi-step device automation from a single request) is restricted to an explicit allowlist of home-control domains, so a hallucinated or injected plan step can't reach a system-level service like `homeassistant.restart` or `shell_command`.
- Biometric/wellbeing data (heart rate, sleep stage) is withheld entirely from cloud LLM calls — it only ever reaches the model when you're running a local Ollama provider.
- Every memory store Nova has — cross-session conversation recall, long-term semantic search, and curated facts/preferences from "remember that…" — is scoped to the right conversation or person rather than searched globally, and anything pulled back into a live conversation is wrapped against prompt injection rather than trusted verbatim.
- A new preference or routine from "remember that…" isn't trusted immediately — Nova asks you to confirm it in the same conversation, and if you don't, it waits in the panel's Memory tab for you to approve, edit, or reject, rather than something Nova merely read (an email, a calendar invite) quietly becoming an accepted fact.
- Voice model downloads are checksum-verified before being installed.

**Smarter, less noisy home awareness**
- Sleep state is explicit (Auto / Awake / Asleep), not inferred purely from bedroom occupancy — one person going to bed no longer marks the whole house "asleep" while someone else is still up.
- Night-time intrusion alerts require an actual breach (a ground-floor door or window genuinely open), not just ordinary movement — a trip to the bathroom no longer triggers a security alert, while a real breach still escalates exactly as before.
- Speakers are assigned per room explicitly (Settings → Room Speakers) instead of auto-discovered — Nova only ever uses the one speaker you've assigned to a room, so a stray Music Assistant/AirPlay/Cast duplicate for a TV can no longer get spoken through.
- Nova addresses whoever's actually home instead of one fixed honorific for everyone — exactly one person home gets their own configured address term (or the existing global default); with nobody home, or more than one person home, it drops the address entirely rather than guessing whose preference to use. Configurable per person from Settings → Person Honorifics (new Command Center look).

**New capabilities**
- Native Eufy Security doorbell/camera support: doorbell press, stranger-vs-known-face detection, and package delivered/stranded/taken all use Eufy's own on-device sensors directly (discovered by unique_id, so a rename can't break it) instead of the Nest/Frigate-only pipeline jarvis-aio assumes. A known/regular face and routine package events cost zero vision-LLM calls — only an actual stranger triggers one.
- Solar/battery/grid visibility: a dashboard card showing live solar generation, battery level, grid import/export direction, and self-sufficiency, plus a voice/chat tool ("how's our solar doing"). Reads straight from Home Assistant's own Energy dashboard configuration, so any install that's already set that up needs no separate setup for this.
- A second, optional Command Center look (Settings → Devices & Services → Nova → Configure → "Panel look") — an animated "stellar core" centerpiece instead of a camera feed, so it looks and feels the same whether you have zero cameras or twelve, with its own navigation and Logs, Memory, Intrusion, and Suggestions tabs, plus a Settings tab reorganized around what you're doing rather than which subsystem it touches. Its Areas cards show every monitored room (no hard cap), each with capability icons, live temperature/humidity sparklines, and a light toggle. 25 of its 26 settings cards are now fully functional, matching Classic feature-for-feature; only the Floor Plan Editor (a full drag-and-drop SVG room layout tool) stays on Classic for now. Only the Residence 3D view remains Classic-only at this point. Classic stays the default; nothing is deleted.

This list grows as real fixes ship — see `CHANGELOG.md` for the full history.

## Credit

Nova is a fork of [jarvis-aio](https://github.com/sam3gp8/jarvis-aio) by sam3gp8, renamed and extended with its own set of features. Full credit to the original project for the base it's built on. This repo is public: issues and pull requests are welcome.

## License

[MIT](LICENSE)

<sub>Persona inspired by the JARVIS of the Marvel Cinematic Universe. This is an independent project, not affiliated with or endorsed by Marvel or Disney.</sub>
