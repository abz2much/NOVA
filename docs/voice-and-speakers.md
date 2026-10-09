# Nova Voice and Speakers

Nova talks through Home Assistant's voice pipeline and speaks its alerts on
the speakers you choose. This guide covers the voice set up, which speaker
Nova uses where, the language it speaks, and how it addresses people.

## What it does

- Answers you through the Assist pipeline, on a voice satellite or in chat.
- Speaks alerts and briefings in the room they matter to, or on the whole
  house speakers for broadcasts.
- Speaks in the language you choose, and addresses whoever is home by the
  name they prefer.
- Removes markdown from anything it speaks, so lists and bold text read as
  plain sentences.

## What it needs

- [ ] **A voice pipeline.** On Home Assistant OS or Supervised, Nova installs
  and starts the Piper, Whisper and openWakeWord add ons on its first run,
  and creates an Assist pipeline with Nova as the conversation agent. On
  Container or Core, install them yourself and create the pipeline in
  **Settings → Voice Assistants**.
- [ ] **Optional: voice satellites**, such as ESP32 boards running Assist.
- [ ] **Speakers in Home Assistant**, as media players.
- [ ] **One speaker per room** in **Settings → Room Speakers**.

## Which speaker Nova uses

- **Room Speakers.** The one speaker Nova may use in each room, plus a
  general fallback. Nova never speaks through any other device in that room.
- **Satellite → Speaker.** An override for a satellite that should not use
  its room's speaker.
- **Announcement Speakers.** The speakers whole house broadcasts use, such as
  briefings and Sentinel alerts.
- **Urgency.** A critical alert goes to the broadcast speakers. Outside
  critical alerts, quiet hours, sleep and "Notifications only" can send an
  alert to your phone instead. See [Presence and alerts](presence-and-alerts.md).

## The voice

Nova speaks with Home Assistant's default voice and never forces one of its
own. By default it uses a free local Piper voice. Turn on **Use Home
Assistant default voice** and it uses the speech engine of your preferred
Assist pipeline instead, for example Home Assistant Cloud or ElevenLabs.

## What it works out by itself

- The satellites and Cast devices it can use, for the speaker cards.
- **Who is home, for the address.** With exactly one person home, Nova uses
  their own address. With nobody home, or more than one person, it uses no
  address at all rather than guess.

## What it will never do on its own

- Use a speaker you have not assigned to a room.
- Force a voice of its own.
- Act on a spoken unlock or garage opening without a tap on your phone. See
  [Safety and security](safety-and-security.md).

## Limits

- Continued conversation (keeping the microphone open after a reply) is off
  by default.
- "Nova speaks" changes the words Nova writes and speaks. It does not change
  the voice, Home Assistant's language, or the panel. Safety notifications are
  translated into English, French, German, Spanish, Italian, Dutch and
  Portuguese. In other languages they stay English.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| "No rooms found yet." | Home Assistant has no areas | Create areas and assign devices to them. |
| "No satellites found." | No Assist satellites in Home Assistant | Add one, or ignore this card. |
| "No Cast devices found." | No Cast speakers for broadcasts | Add one, or use Room Speakers only. |
| Nova speaks on a TV in that room | That TV is the room's assigned speaker | Pick another in **Settings → Room Speakers**. |
| The voice changed after an update | Nova follows Home Assistant's default voice | Set the voice in your Assist pipeline. |

## Settings

Panel, **Settings → General**:

| Setting | What it does |
|---|---|
| Announcements | Master switch for all proactive speech. |
| Nova speaks | The language Nova talks and writes in. Auto follows Home Assistant, including its region, such as Brazilian Portuguese, Canadian French, Latin American Spanish, Swiss German or Traditional Chinese. |
| Language | The panel's language. Auto follows Home Assistant. Fully translated in Brazilian Portuguese, Czech, Dutch, French, German, Polish, Russian, Simplified Chinese, Spanish, Swedish and Traditional Chinese; other languages are partly translated. |

Panel, **Settings → Room Speakers**, **Satellite → Speaker** and
**Announcement Speakers**: see above.

Panel, **Settings → Person Honorifics**: what Nova calls each person when they
are home alone, or a custom address.

Panel, **Settings → Nova Character & Research**: **Banter level**, from
"Plain — no wit" through "Dry — occasional wit (default)" to "Full —
expressive wit". Urgent or grave events are always spoken plainly.

Panel, **Settings → Anticipation & Memory**:

| Setting | What it does |
|---|---|
| Use Home Assistant default voice | Use your preferred Assist pipeline's speech engine. Off by default. |
| Continued conversation | Keep listening for a follow up after Nova answers. Off by default. |
| Follow me between rooms | Reopen the microphone where you moved to. Needs two or more satellites. |

In **Configure → Core**, "Address me as" is the default address when no
person specific one applies. **Configure → Routing** holds the "Broadcast
speaker group".
