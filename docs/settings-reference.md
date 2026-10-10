# Settings reference

This page lists Nova's main settings in one place, for when you want to know
exactly what a setting does or where to find it. Each feature guide also
lists its own settings.

Settings live in three places:

- **The Nova panel.** Most settings are on the Settings tab, in cards grouped
  as General, Voice & Speakers, Awareness & Safety, Learning & Memory,
  Cameras, and Home & Extras. The search box finds a card by name. A few
  live on other tabs, such as the Energy tab.
- **Configure.** **Settings → Devices & Services → Nova → Configure** holds
  routing, quiet hours, observer mode, credentials, identity and email.
- **`/config/nova/config.json`.** A few advanced settings have no control
  anywhere else. This file is also where the panel saves its settings, so
  merge changes into it rather than replacing it.

The setting names below are the keys Nova uses in `config.json`. You only
need them if you edit that file by hand.

## AI models and providers

| Setting | Where | What it does |
| --- | --- | --- |
| `llm_provider` and the model for each role | Settings → AI Models | Choose Groq, Gemini, OpenAI, Anthropic, Ollama or a custom endpoint, separately for the Main Agent, Classifier, Reasoning, Vision and Camera Reasoning roles. A saved model that is missing from a provider's live list is kept, never quietly replaced. |
| `ollama_base_url` | Settings → AI Models | The Ollama server used by Ollama roles, for example `http://ollama-host.local:11434`. Nova uses Ollama's own chat interface and can list the server's models and what they can do. |
| `custom_base_url` | Settings → AI Models | A separate OpenAI compatible endpoint, used only by roles set to the custom provider. It never takes the Ollama address or another provider's key. |
| Provider credentials | Settings → AI Models, or Configure → Credentials | One key per provider, stored only in `secrets.yaml`. Each role uses only its own provider's key, never another's. |
| `rich_reasoning` | Settings → General → Rich Reasoning | Uses the cloud reasoning model first for medium and high urgency events. It costs a bit more and reasons better. |
| `cognition_threshold` | `config.json` only | How noteworthy an event must be before Nova escalates it. |

## Speaking and alerts

| Setting | Where | What it does |
| --- | --- | --- |
| `observer_enabled` | Settings → General → Observer, or Configure → Observer | Lets Nova watch the stream of events in your home and decide what is worth telling you. |
| `output_language` | Settings → General → Nova speaks | Auto (the default) follows Home Assistant's language. Pick a language to make Nova speak and write in it, while Home Assistant and the panel stay as they are. Brazilian Portuguese and Traditional Chinese have their own options. Canadian French (`fr-CA`), Latin American Spanish (`es-419`) and Swiss German (`de-CH`) work on Auto, or when saved another way; the picker then shows the code and keeps it. Safety notifications are translated for English, French, German, Spanish, Italian, Dutch and Portuguese. In any other language they stay in English, while text Nova writes itself follows this setting. Fixed messages, such as package announcements, and the panel itself are not changed by this setting. |
| `announce_notify_only` | Settings → General → Notifications only | Off by default. When on, proactive alerts go to your phone instead of being spoken. Critical safety alerts still speak. Reminders, package and camera announcements, scheduled briefings, the infrastructure audit and hazard alerts are not covered by this switch; hazard speech follows its own quiet hours level. Replies to you are never affected. |
| Mutes (`nova.shush` and `nova.unshush`) | The Muted card on the Command Center, or the services | Entity, category and blanket mutes are saved in `nova/output_mutes.json` and survive a restart. The Muted card lists them with an Unmute button and shows a banner while the blanket mute is on. Critical safety alerts are never muted. |

## Safety

| Setting | Where | What it does |
| --- | --- | --- |
| `face_stand_down` | Settings → Security Alarm | Off by default. When on, a resident on the Faces list, recognised by Frigate or Double Take at or above the confidence threshold on a camera in the last 3 minutes, stops Nova opening a new intrusion investigation, as long as no unknown face or unexplained person was also seen. It never closes an investigation that is already open, never affects critical alerts, lockdown, freeze warnings or mutes, and every stand down is recorded in the Actions log. A face can be a photo or a lookalike, so treat this as a convenience, not a security control. |
| `hazard_source`, `hazard_counties`, `hazard_push_level`, `hazard_speak_level`, `hazard_night_speak_level` | Settings → Hazard Monitor | Choose Met Éireann, the US National Weather Service or a custom CAP feed. Ireland and the US get their own source by default, and other countries default to Custom. The nearest county is found for you, while an older saved `hazard_counties` list is still honoured, without panel controls. Yellow and above goes to your phone; Orange and above is also spoken outside quiet hours. At night a separate level applies, Red by default, and Off makes every warning phone only. Location overrides are saved only when they differ from Home Assistant's home position. |

The other safety settings, such as the security alarm, automatic lockdown
and the locks left out of lockdown, are described in
[Safety and security](safety-and-security.md#settings).

## Cameras and learning

| Setting | Where | What it does |
| --- | --- | --- |
| `visitor_learning` | Settings → Cameras | Quietly learns from people at the door. Never spoken. |
| `package_detection` | Settings → Cameras | Watches porch cameras for packages and mail. |
| `camera_event_learning` | Settings → Learning & Memory → Routine Learning | Feeds detections from Eufy, Frigate, Nest and Nova's own vision into routine learning. On by default, and off whenever Nova's learning itself is off. |
| `camera_event_confidence_floor`, `camera_event_dedup_window` | `config.json` only | The lowest confidence a source may report for a detection to count (0 to 100; 0 turns the filter off) and how many seconds apart two sources must be to count as different detections (300 by default). |
| `camera_historical_awareness`, `camera_awareness_min_observations` | Settings → Learning & Memory → Routine Learning | Adds repeated camera patterns from the past to conversations. It has its own off switch and needs at least 3 to 12 sightings (3 by default) across more than one day. It does nothing while learning or camera event learning is off. |

## Energy

| Setting | Where | What it does |
| --- | --- | --- |
| `energy_agency`, `energy_peak_watts`, `energy_cost_today_entity`, `energy_cost_net_entity` | Energy tab → Energy Management | How much say Nova has over high power devices (advice only by default), the whole home peak in watts, and your own sensors for today's cost and net cost. Without those sensors, the daily report estimates the cost from the Energy dashboard's price. |

## Host health

| Setting | Where | What it does |
| --- | --- | --- |
| `host_health_enabled`, `host_health_alerts_enabled` | Settings → Host Health | Reads Home Assistant's own System Monitor entities for the machine Nova runs on. Awareness and alerts are two separate switches, both off by default. |
| `host_health_mappings`, `host_health_thresholds`, `host_health_persistence_minutes`, `host_health_cooldown_minutes` | Settings → Host Health | Which System Monitor entity to use for a reading (only needed when there is more than one candidate), the alert level for each reading, how long a problem must last before the first alert (2 to 120 minutes), and the shortest gap between repeat alerts (5 to 720 minutes). |
| `infrastructure_audit_sensors`, `infrastructure_audit_area` | Settings → Host Health → Infrastructure audit | The sensors the 15 minute audit checks, and the room it speaks in. Empty by default, which turns the audit off. See [Host health](host-health.md#infrastructure-audit). |

## See also

- [Getting started](getting-started.md#where-settings-live): where settings live
- [Safety and security](safety-and-security.md): safety settings
- [Presence and alerts](presence-and-alerts.md): quiet hours, mutes and sleep
- [Host health](host-health.md): the host health settings explained
