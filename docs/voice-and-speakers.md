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
- [ ] **Optional: voice satellites**, such as ESP32-S3 boards running Assist
  or Wyoming satellites. Google speakers work for replies and announcements.
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

## Voice phrases

Link something you say to something Nova runs. Say "Nova, the house is
cold" and Nova runs your heating boost script. Set them up in **Settings →
Voice & Speakers → Voice Phrases**.

### Add a phrase

1. Press **+ Add a voice phrase**.
2. Give it a name, such as "House is cold".
3. Add up to 10 ways of saying it: "the house is cold", "I'm cold", "it's
   freezing in here".
4. Pick what to run: a script, an automation, a scene, a shell command, or
   **Advanced** for any other service Nova may use, with entity ids and
   JSON data.
5. Optional: the reply. Empty means "Done". `{addr}` adds ", sir" or however
   Nova addresses whoever is home; `{honorific}` adds just that word.
6. Save. Then try a few sentences in **Try a sentence**. It shows which
   phrase would match and never runs anything.

### Flexible or exact

- **Exact** needs the same words. Case, punctuation, "Nova" at the start and
  "please" are ignored, and "I'm" is the same as "im" or "I am".
- **Flexible** (the default) also allows small differences: "the house is a
  bit cold" matches "the house is cold", and a one letter slip in a longer
  word is fine. It refuses near misses on purpose:
  - **Opposites.** "Turn off the heating" never matches "turn on the
    heating". This also means "the house is cold, warm it up" matches
    nothing, because "warm" is the opposite of "cold".
  - **Not.** "The house is not cold" or "isn't cold" never matches.
  - **Questions.** "Is the house cold?" never matches.
  - **Long sentences.** A sentence that only contains your phrase among many
    other words does not match.
  - **Ties.** If two phrases match equally well, neither runs.

### Let the AI pick it by meaning

Turn on **Let the AI pick this by meaning** and the AI may run the phrase
when what you said clearly means it, even if the words differ ("it's a bit
nippy in here"). Only phrases with this on are shown to the AI, at most 50,
and never a shell command. The AI can only run a phrase you set up; it can
never add or change one.

### Safety

- Every target goes through Nova's usual permission check, and every run is
  in the Logs tab.
- **Voice Confirmation.** With it on, a phrase that runs a script, an
  automation or a scene asks first, as Nova's own "activate" does. The phrase
  is handed to the AI to ask, so the Logs tab shows two rows for it: the
  phrase's own row, blocked as "handed to agent", and the row of the tool
  that asked. The card marks the phrases that will ask first.
- **Unlock, open and disarm** still need a tap on your phone when asked by
  voice or in the Chat tab. A phrase can't ask for that tap, so such a phrase
  runs nothing there: Nova says it needs confirming and can't ask for that
  right now. Say it the usual way instead ("unlock the front door").
- A phrase with several targets runs only when every target is allowed. If
  one needs confirming, none of them runs.
- Nova only says what it sent. Home Assistant can't say when a script has
  finished, so the reply never claims it worked.
- If a phrase needs confirming and Nova is offline, it says it can't ask
  right now and runs nothing.

### Shell commands

Nova never runs files itself. A shell command phrase calls one of Home
Assistant's own `shell_command` services, set up in `configuration.yaml`.
It runs without asking, and the AI can never pick it.

To make one ask first, turn on Voice Confirmation, then add the command to
`voice_confirm_entities` in `/config/nova/config.json`, and reload Nova
(**Settings → Devices & Services → Nova → ⋮ → Reload**):

```json
"voice_confirm_entities": ["shell_command.heat_boost"]
```

This only works while Voice Confirmation is on. The same list exempts an
entity from asking when you write it with a `!`, such as
`"!script.heating_boost"`.

### Offline

Matching and running happen on your Home Assistant, so phrases work in the
Chat tab and while the AI is offline. Only picking by meaning needs the AI.

### What comes first

1. Home Assistant answers first when it can. A sentence trigger in one of
   your own automations with the same words wins. So does anything Home
   Assistant handles itself when **Prefer handling commands locally** is on
   in your Assist pipeline.
2. Then your voice phrases.
3. Then Nova's own built in commands, such as "turn on the kitchen light".
4. Then the AI.

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
| A voice phrase does nothing | Another phrase ties with it, the words are a near miss, or Home Assistant answered first | Try the sentence in **Try a sentence**, and check your automations for a sentence trigger with the same words. |
| A phrase says it needs confirming | Voice Confirmation is on, or it unlocks, opens or disarms | Answer the question. For unlock, open or disarm, ask the usual way and tap your phone. |

## Settings

Panel, **Settings → General**:

| Setting | What it does |
|---|---|
| Announcements | Master switch for all proactive speech. |
| Nova speaks | The language Nova talks and writes in. Auto follows Home Assistant, including its region, such as Brazilian Portuguese, Canadian French, Latin American Spanish, Swiss German or Traditional Chinese. |
| Language | The panel's language. Auto follows Home Assistant. Fully translated in Brazilian Portuguese, Czech, Dutch, French, German, Polish, Russian, Simplified Chinese, Spanish, Swedish and Traditional Chinese; other languages are partly translated. |

Panel, **Settings → Room Speakers**, **Satellite → Speaker** and
**Announcement Speakers**: see above.

Panel, **Settings → Voice Phrases**: the on/off switch for all phrases, and
your phrases. See [Voice phrases](#voice-phrases).

Panel, **Settings → Person Honorifics**: what Nova calls each person when they
are home alone, or a custom address.

Panel, **Settings → Nova Character & Research**: **Banter level**, from
Plain (no wit) through Dry (occasional wit, the default) to Full (expressive
wit). Urgent or grave events are always spoken plainly.

Panel, **Settings → Anticipation & Memory**:

| Setting | What it does |
|---|---|
| Use Home Assistant default voice | Use your preferred Assist pipeline's speech engine. Off by default. |
| Continued conversation | Keep listening for a follow up after Nova answers. Off by default. |
| Follow me between rooms | Reopen the microphone where you moved to. Needs two or more satellites. |

In **Configure → Core**, "Address me as" is the default address when no
person specific one applies. **Configure → Routing** holds the "Broadcast
speaker group".

## See also

- [Translating Nova](translating.md): languages and how to help
