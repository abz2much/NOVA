# How Nova works

This page is for developers and anyone curious about what happens inside
Nova: how it is built, how it decides, the rules that keep it safe, and how
it reports what it did. The [CHANGELOG](../CHANGELOG.md) has the history of
each change.

## Where it runs and what it stores

Nova is a Home Assistant custom integration (domain `nova`), installed
through HACS into `custom_components/nova/`. It runs inside Home Assistant.
It registers a conversation agent for the voice pipeline and serves its own
dashboard panel directly. There is no separate container.

State and learned behaviour live under Home Assistant's own config
directory, usually `/config/nova/`, mostly as local SQLite:

- `patterns.db`: state change and command history, used to suggest
  automations and to ground decisions in what normally happens.
- `conversations.db`: conversation history and Spoken History.
- `decisions.db`: the evidence, confidence and outcome behind each entry in
  the Decisions view.
- `knowledge.db`: facts from "remember that" and the relations map.

Other files there include the doorbell training log, lockdown state and the
reasoning cache. The list is not complete. One exception: the semantic
memory database, `nova.db`, sits at the config root itself (usually
`/config/nova.db`), not in the `nova/` folder.

Every path comes from the config directory Home Assistant reports, never an
assumed `/config`. See [Privacy and your data](privacy.md) for what each
store holds.

## How Nova decides

The reasoning is layered, cheapest and most local first:

1. A classifier takes a quick first look at each event and flags whether it
   is worth thinking about.
2. Local templates handle most events.
3. A learned cache remembers past announce or stay silent decisions by a
   general event signature, so repeated patterns are settled locally over
   time.
4. A cloud model, or a local one, is used for the genuinely unclear cases.

A connectivity breaker guards cloud calls, and every local decision writes
its reasoning to the log view.

### The Cognitive Core

The Cognitive Core is a loop that runs every 30 seconds. It sorts household
events by urgency and decides whether each is worth your attention. It
grounds its decisions in your home's history ("the kitchen light at 7am is
routine; this window has never opened before"), escalates security events
when you are away, and proposes automations from patterns it sees. Safety
checks, lockdown, freeze warnings, the ignore list and pattern logging all
run from this loop.

At the start of each tick it takes one read only picture of the house: who
is home, what the alarm means, whether anyone is asleep, quiet hours, open
doors and windows, unlocked locks, locks and doors it can't read, open
covers, person motion, whether lockdown is on, and open situations. Every
part of that tick sees the same picture.

### The Local Mind

When the cloud can't be reached, Nova keeps working. The Local Mind judges
household events on the machine itself. It checks whether a sensor is
flapping, compares the event with history in `patterns.db`, looks for
similar cached decisions, makes a judgement about the situation, and words
the result in Nova's voice. Routine events keep getting sensible, well
worded calls with no internet at all.

Fixed voice and text commands, such as turning on a light, locking a door
or asking what is open, also run locally with no AI call. What the Local
Mind does not do is open ended reasoning about something genuinely new, web
research or vision. Those still need a cloud or local GPU provider.

A reply from a provider that Nova can't read counts as a provider failure.
The Local Mind decides instead, and the unreadable reply is never cached or
taken to mean "stay silent".

## The conversation agent

Nova's conversation agent uses a loop of tool calls: it reads your request
with your home's context, calls a tool, looks at the result, and answers.
The tools are Nova's own, not the generic Home Assistant conversation
interface. Most tools also have a control in the panel.

### Chat tab

The Chat tab sends your message to the same agent over the `nova/chat`
command, open to any Home Assistant user. The server builds the conversation
id from your user (`nova_chat_` plus your user id), so each person has their
own thread and the panel never sends one. A Chat turn skips the relevance gate
and the duplicate check, works out who you are from your Home Assistant user,
and is never routed to a speaker. Unlock, open, disarm and standing down an
intrusion need a tap on your phone, as for voice. You can send 20 messages a
minute and one at a time. See [Safety and security](safety-and-security.md#chat).

### Device control

- Nova can control one device or many, run scenes and scripts, and carry
  out plans with several steps (`control_device`, `bulk_control`,
  `run_scene_or_script`, `execute_plan`).
- It can read live state, search entities, list a room's devices and sum up
  the whole home (`get_entity_state`, `search_entities`,
  `get_area_devices`, `get_home_summary`), and read Home Assistant's history
  and logbook to answer "what happened while I was out?"
  (`activity_history`).
- Names are matched in a fixed order: entity ID, then exact name or alias,
  then the kind of device you name (such as "the lights"), then close
  matches with a clear lead. If a name could mean several devices, Nova asks
  which one, naming each with its area, instead of picking the nearest.
- A request that says not to change anything never changes anything. A
  question about reaching Home Assistant itself gets a status answer, not a
  list of who is home.
- `execute_plan` may only use an allowed list of home control domains, so a
  made up or injected step can't reach a system service such as
  `homeassistant.restart` or `shell_command`.

### Honest reporting

A result is reported as verified, accepted, unverified or failed. Home
Assistant accepting a command is never worded the same as Nova confirming it
took effect. Nova never says a lock is locked or a light is on before it has
checked. Bulk actions, plans, scenes, scripts and lockdown only claim what
they observed. Scenes, scripts and automations are marked as not checkable,
because their effect is not visible to Nova.

### Sub-agents and HOMER

For a complex, self contained part of a request, Nova can start a focused
sub-agent inside Home Assistant (`delegate_task`). It gets a short
objective, a small set of read only tools and a few turns, then hands its
result back. Sub-agents can't control devices or start sub-agents of their
own, and the depth is capped. On a single GPU machine they run one after
another, so the gain is a tight, focused context rather than speed.

HOMER is Nova's one read only diagnostic specialist: a named `delegate_task`
profile (`profile: "homer"`, with `capability: "diagnostics"` kept as
another name for the same profile). For a fault such as "why is this device
unavailable?", "why did this automation fail?", "is the machine running out
of memory?" or "check Nova's connectivity", Nova can hand the question to
HOMER instead of guessing. HOMER uses only diagnostic and state reading
tools: system health, Cognitive Core status, connectivity, energy status,
activity history, entity state and search, and root cause analysis. It
keeps what it saw apart from what it concluded, and reports a likely cause,
its confidence and a suggested next step. It never acts on a device and
never claims to have fixed anything. It has no tool for device control,
settings, writing memory or delegating, and it is limited to 4 tool turns.
It has its own system prompt, not a layer on top of the normal one, so it
never carries the normal prompt's statement that the agent can control
devices. Nova checks every tool call against the sub-agent's actual tools
when it runs, so a tool shown to a model by mistake still can't be used.
HOMER is always available.

When host health is on, the `system_diagnostics` tool also reports the
machine's resource use, so "why is Nova slow?" includes that evidence. See
[Host health](host-health.md).

### Specialist bridges

Five tools (`ask_executive_assistant`, `ask_marketing_agent`,
`ask_security_privacy_agent`, `ask_homelab_infra_agent`,
`ask_house_manager_agent`) pass a question to separate agents run by n8n,
over a private webhook. They need your own n8n setup, and a new install
doesn't have them. Each stays off until you set both the webhook base
address (`n8n_webhook_base_url`) and the shared secret
(`nova_specialist_webhook_key` in `secrets.yaml`). There is no built in
address, redirects are never followed, and sub-agents can't call them.

### Goals and follow-ups

Goals (`create_goal`, `update_goal`, `manage_goals`) and follow-ups
(`schedule_followup`, `manage_followups`) run later on their own, with no
one present. Those runs can check, look and report back, including anything
they think needs doing, but they never control devices or change anything
themselves.

## Safety rules

- Every lock, cover, alarm, scene and script action goes through one
  authority check. Disarming by voice needs a phone tap, like unlocking and
  opening. When Nova acts on its own it may only lock, close or adjust
  comfort.
- Nova checks after acting before it calls anything secured.
- Nova uses one chosen household alarm for security decisions, not every
  alarm shaped entity a camera hub or bridge exposes. A single Alarmo panel
  is found automatically, other setups can choose theirs, and automatic
  lockdown is something you turn on, never on by default.
- Voice can lock a door at once but can never unlock one or open a garage
  door. That always needs a tap on your phone, so a spoofed or faked voice
  can't open the house.
- Critical safety announcements pass every gate: the rate limit, entity and
  category mutes, and a blanket `nova.shush`.
- Smoke, gas, water and carbon monoxide are recognised from Home Assistant's
  device information before any presence or duplicate check, so a plain
  sensor name can't hide an emergency. A hazard that trips again quickly,
  or while a sensor is muted, debounced or the classifier budget is used
  up, still gets through. Only an entity you excluded yourself is skipped.
- You can confirm or dismiss an intrusion, and acknowledge an alert, by voice
  (`dismiss_intrusion`, `acknowledge_alert`).
- Situations in progress, such as an intrusion investigation, a freeze
  warning or a parcel on the doorstep, survive a restart.
- Nova only learns from outcomes you confirmed.

## Security

- Each AI provider has its own key in `secrets.yaml`: Groq, OpenAI,
  Anthropic, Gemini, a custom endpoint, and an optional key for a protected
  Ollama server. A key set for one provider can never be sent to another.
  Key values are never shown in any screen, only whether a provider is set
  up. Moving an older shared key never guesses which provider it belongs
  to, and leaves it alone if that can't be worked out safely.
- Model lists and endpoint tests for self hosted servers check every
  redirect before following it, refuse link local and cloud metadata
  addresses, and never pass a key to a different address. Cloud providers
  are only ever reached at their fixed addresses.
- Every chat request goes through one bounded Provider Activity boundary.
  Hidden reasoning, provider response objects, keys, request bodies, images
  and headers are kept out of the activity records and out of what older
  integrations receive.
- Every memory store (conversation recall, long term semantic search and
  the facts and preferences from "remember that") is scoped to the right
  conversation or person. Anything brought back into a conversation is
  wrapped against prompt injection rather than trusted as it is.
- A new preference or routine from "remember that" isn't trusted straight
  away. Nova asks you to confirm it in the same conversation. If you don't,
  it waits in the Memory tab for you to approve, edit or reject, so
  something Nova merely read, such as an email or a calendar invite, never
  quietly becomes a fact.
- Confirmed intrusion snapshots are stored privately and fetched only
  through an admin only panel command. Copies for notifications use signed
  addresses and are deleted when they expire.
- Wearable data is held back from cloud AI calls completely.

## How Nova stays quiet

Nova tries hard not to nag you. A few of the rules:

- Intrusion never confirms on a resident walking about while the alarm is
  armed home or night, or the household is asleep. That needs the alarm
  going off or a person confirmed on camera. Appliance doors, such as a
  fridge, never count as a way in. At night, an alert needs a real breach,
  such as a ground floor door or window open, not ordinary movement.
- There is one answer to "who is home" for intrusion, lockdown, packages,
  briefings and offers: people, plus device trackers linked to a person. A
  TV or hub tracker can't switch off away detection, and a person reading
  unknown is never taken as away.
- A medium alert while everyone is away, such as a door left unlocked,
  always reaches the phones. Every safety decision is written to the
  decision log in one format.
- The night sweep doesn't fight people. Anything it couldn't secure, or that
  someone opened again, gets one try and one alert a night. Lockdown never
  says "fully secured" when it can't read a door or window, and each
  decision links to the commands it sent.
- Sleep is set explicitly (Auto, Awake or Asleep), not guessed from bedroom
  presence alone, so one person going to bed doesn't mark the whole house
  asleep.
- Nova uses only the one speaker you assigned to each room, so a duplicate
  speaker for a TV can't be spoken through.
- With exactly one person home, Nova uses their own form of address. With
  nobody or several people home, it drops the address rather than guessing.
- An appliance finishing is only announced from real evidence: the
  appliance's own job state sensor, or one you set up yourself on the Energy
  tab. Guesses from power use help Nova learn but never speak. A status
  sensor already sitting at "done" when Nova starts is ignored; only a
  change Nova saw happen is announced.
- "Around this time, X is usually Y" never nags you back towards a less
  secure habit. A window that is usually open but is now closed stays
  silent. Something open, unlocked or disarmed when that is unusual only
  gets a mention with a separate, concrete reason: everyone confirmed away,
  or the alarm actually armed.
- A door or window left open escalates on repeat reminders (10, then 20,
  then 30 minutes) and stays quiet when it is above 10°C outside.
- A camera's car, animal or package sensor never counts as a person. Nova
  only offers what it can actually do, and the last person leaving the house
  hears nothing.
- Arrival briefings greet the person who just came in, and don't report the
  door they just opened as open.
- Thermostat keypad locks can be left out of lockdown and the "unlocked"
  lists. The left unlocked reminder recognises them by their device.
- Garage, basement and other room items only appear when the home has them,
  and that only changes what you see, never what Nova secures.

## Learning and suggestions

- Nova reads Home Assistant's loaded automations (from the UI, YAML,
  packages and blueprints) at startup and when automations reload. It
  matches automation triggers to the changes they cause, so an existing
  automation can't teach Nova its own output as a routine. New suggestions
  are compared with what you already have: exact duplicates are hidden, and
  blueprints, templates and rules on the same device are shown as warnings.
  This needs no AI call.
- Suggestions are scored on plain evidence: distinct days, coverage, how
  tightly the timing clusters, how recent it is, and for "after A, B" how
  often A is really followed by B. Repeats on one day, routines that have
  stopped, and busy sensors that only sometimes come first are not
  suggested.
- Suggestions only link things in the same area (arrivals and departures
  excepted), need the trigger to be followed by the action most of the time,
  keep one suggestion per device, and only appear when Nova can install
  them. With "Review suggestions with AI" on, the Suggestion Review model
  checks each one first. Rejected ones are never suggested again, and are
  listed with the reason so you can bring one back.
- Sequence and threshold suggestions can use same area presence as a
  condition when history supports it, and can learn to turn the device off
  when that presence clears.
- Nova tracks whether an automation it created actually runs.
- Detections from Eufy, Frigate, Nest and Nova's own vision feed the same
  pattern learner as every other entity. A detection is stored as a
  synthetic `camera_event.<location>` entity that can trigger an automation
  but can never be its target. Detections are merged across sources within
  a short window. See [Privacy and your data](privacy.md) for what is kept.
- Nova can add a short "what I've noticed lately" block to conversations
  from repeated camera patterns. It stays clearly about the past, needs
  sightings over several days, and never claims something is there now.

## Seeing what Nova did

- **Decisions** (Logs tab): every proactive decision, with its evidence,
  confidence and the model that decided, and Helpful, Unnecessary or Wrong
  feedback.
- **Decision Lab**: replays a past decision against your current
  confidence threshold to show whether it would pass today. It is a policy
  check, not a reconstruction of what happened.
- **Setup Doctor** (Diagnostics tab): a read only health check with a plain
  fix for each problem, never applied for you.
- **Provider Activity** (Diagnostics tab): daily totals of AI calls by
  provider, model and role, with token counts and timings. Never the
  questions, answers, tool details, images or keys.
- **Spoken History** (Logs tab): the last 100 announcements Nova actually
  delivered. Replay one from the panel, or ask "what did you just say?".
- **Actions** (Logs tab): every command Nova sent and whether it was
  confirmed.
- Conversation memory belongs to the person speaking, with the shared
  conversation still there as context. Background chatter and TV dialogue
  not meant for Nova are rejected before they reach memory.

## AI providers and models

- One settings resolver decides which model each part of Nova uses, so no
  two parts disagree.
- Ollama and custom OpenAI compatible servers each have their own address,
  connection test, model list and profiles (Local Text and Hybrid), with a
  manual model fallback and one Apply action that checks every role before
  changing anything. Ollama uses its own `/api/chat` interface, including
  tools, images and thinking control.
  An older shared endpoint setting moves to the right address by itself,
  without tying Nova to any particular host, address or model.
- Model lists from Groq, OpenAI, Anthropic, Gemini, Ollama and custom
  endpoints are fetched live. Gemini and Anthropic lists are paged within
  strict limits, results are cached briefly per provider, and a saved model
  missing from a live list is never replaced.

## Reliability

- A setup that fails part way removes the services, listeners, entities,
  scheduled jobs and other things it registered before reporting the error.
- Failures to save settings or write to a database are reported, never
  treated as success.
- Nova's SQLite schema has a single owner. Databases are upgraded once at
  startup, one file at a time and all or nothing, and are checked against
  the real schema before the upgrade is recorded. A store that fails to
  upgrade is logged and tried again at the next start, without stopping
  Nova, and older releases can still open an upgraded file.
- Provider clients belong to the loaded Nova instance. Roles with the same
  settings share one client, and a replaced or unloaded client is closed
  once, after its work finishes.
- Normal phone alerts can go to several phones. One phone being unavailable
  doesn't block the others. Critical intrusion and confirmation messages go
  to the whole household.

## Building and testing

- The dashboard is built from ordered source files into the single
  `nova-panel.js` that HACS installs, by a build with no dependencies. CI
  checks the built file is current.
- CI checks the architecture: type checks for the capability packages
  (strict for persistence), no import cycles, a fixed direction for package
  dependencies, compatibility modules that can't grow, and coverage floors
  for each package. Unit tests run fully isolated from `/config`.
- An integration suite on a real Home Assistant test instance covers setup,
  reload, the config flow, websocket permissions, snapshot retrieval and
  cleanup after a failed setup.
- CI also runs Home Assistant's hassfest, HACS validation, the audit script,
  the unit tests on two Python versions, and the panel's syntax and smoke
  tests.

## See also

- [Getting started](getting-started.md): setup and a tour of the panel
- [Safety and security](safety-and-security.md): the safety features in detail
- [Privacy and your data](privacy.md): what is stored and what leaves
- [Settings reference](settings-reference.md): every setting
