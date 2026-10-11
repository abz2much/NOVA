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
- **Say it exactly and it runs.** Saying one of a phrase's sayings word for
  word counts as your OK, so it runs straight away, by voice or in Chat, even
  with Voice Confirmation on. "Nova" at the start, punctuation and "I'm"
  against "I am" don't matter.
- **A looser match asks first** when Voice Confirmation is on and the phrase
  runs a script, an automation or a scene. On a voice satellite Nova asks
  itself, with just the phrase's name ("House is cold?"); say yes on the same
  satellite (see [Confirming by voice](#confirming-by-voice)). In Chat it is
  handed to the AI to ask, so the Logs tab shows two rows for it: the
  phrase's own row, blocked as "handed to agent", and the row of the tool
  that asked. The card's "Asks first" note is about these looser matches.
- **Unlock, open and disarm** still need a tap on your phone when asked by
  voice or in the Chat tab, even when you say the phrase exactly. A phrase
  can't ask for that tap, so such a phrase runs nothing there: Nova says it
  needs confirming and can't ask for that right now. Say it the usual way
  instead ("unlock the front door"). Saying a phrase exactly never skips any
  question for unlock, open, disarm or calling off an intrusion.
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

## Confirming by voice

With **Voice Confirmation** on, Nova asks before it runs a scene, a script,
an automation or anything else that needs your OK. When you asked on a voice
satellite:

1. Nova asks in its own reply, on the same speaker its replies to that
   satellite use (your **Satellite → Speaker** pairing, else the room's
   speaker). For a voice phrase it asks with just the phrase's name, such as
   "House is cold?"; otherwise its reply ends with "Shall I go ahead?". It
   never plays a separate question in another room.
2. The satellite keeps listening for your answer, even with **Continued
   conversation** off. When the reply plays on a separate speaker, the mic
   opens once the speaker has finished.
3. Say **yes** ("Yes", "go ahead", "do it") on the **same satellite** within
   60 seconds, and Nova runs it. Say **no** ("No", "cancel") and it won't.
   Anything else drops the question, and Nova answers what you said as
   usual.

What to know:

- Only the satellite you were asked on can say yes. A yes on another
  satellite, in the Chat tab or in the panel does nothing, and leaves the
  question waiting.
- If two satellites hear your yes at the same moment, the one that hears it
  first can take it and nothing runs. Ask again.
- The question waits 60 seconds and is forgotten on a restart.
- Unlock, open and disarm, and calling off an intrusion, still need a tap on
  your phone by voice. A spoken yes never replaces that tap.
- A yes runs what a voice phrase, or a scene, script or automation, held. Other
  actions that need confirming (a device, a plan, a mode, or one of Home
  Assistant's own tools) still ask in the reply. For those, say the full
  request again after Nova asks.
- The Logs tab shows the run as "approved".

### Answering after Nova reopens the mic

When Nova keeps a satellite listening after its own question (for example
"Shall I raise both to 22 degrees?"), your answer continues that same
conversation, so a plain "Yes" is understood. This lasts 60 seconds and only
on that satellite.

Replies on a voice satellite are kept to one or two short sentences, and
never talk about voice phrases, tools or how Nova works. Chat replies are
unchanged.

## Voice ID

Nova Voice ID is Nova's own speech to text. It needs the Nova Voice ID app,
installed from [github.com/abz2much/nova-voice-id](https://github.com/abz2much/nova-voice-id).
The app passes your speech to the Whisper app and gives back Whisper's words
unchanged. From app 0.2.0 it also says whose voice it heard. For now Nova only
logs that: who Nova thinks is speaking, its replies and its memory don't
change.

1. Install and start the Nova Voice ID app.
2. In Nova, open **Settings → Voice & Speakers → Voice ID**. The app fills in
   its address and token when Nova finds it. If it doesn't, paste the address
   from the app's log (it looks like `tcp://<name>:10350`) and the token from
   the app's Configuration tab. The status says **Connected**, with the app's
   version and Whisper's models.
3. Pick a **Fallback speech to text**, usually your Whisper entity.
4. In Home Assistant, open **Settings → Voice assistants**, pick your
   assistant, and set its speech to text to **Nova Voice ID**.

If the app is stopped or can't be reached, Nova hands the audio to the
fallback, so voice keeps working. If the app stops halfway through a
sentence, that one request fails; say it again. The Logs tab shows one STT
line per request (app, fallback or error, and how long it took) and, for each
voice turn, a **VOICE** line with the app's result: status, best match,
scores, seconds of speech and the satellite. Never your words. Settings →
Diagnostics shows a **Voice ID** row.

A value you type in the address or token field always wins. The app only
fills a field that is empty or still holds what it filled in last time.

### Recording a voice

The People list shows everyone in Home Assistant (`person.*`), with how many
samples the app has for them. **Hide** tucks away accounts that aren't real
people.

1. Pick the satellite under **Record on**, then press **Record voice** next to
   the person.
2. The satellite says "{name}, after the wake word, please say: …". Say the
   wake word, then the sentence. Nova never reopens the mic, so say the wake
   word each time.
3. Nova answers on that satellite with the next sentence, or "Sorry, once
   more please" when the sample was too short or sounded like someone else.
   After two tries it moves on to another sentence.
4. After 8 samples it says "Thanks, {name}, that's done." The card shows the
   progress as it goes, and **Cancel** stops it.

Record each person on every satellite, because rooms sound different. While a
recording runs, only the turns from that satellite go to it; the others work
as usual. A recording turn never runs a voice phrase, a command or the AI.
After 5 minutes it stops by itself. Samples already saved are kept.

The sentences are plain ones, never commands or questions, because Home
Assistant answers some sentences itself before Nova hears them.

### Deleting a voice

**Delete voice** removes that person's voice and recordings from the app,
after you confirm. **Delete all voice data** removes everyone's, after two
confirmations.

### Where voice data lives

Only in the Nova Voice ID app, never in Nova. Nova keeps the app's address in
its settings and the token in Home Assistant's `secrets.yaml`. The voice
result for a request lives only for that request, and the log lines hold
numbers and ids, not audio or words.

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
| You said yes and nothing ran | The yes was on another satellite, came after 60 seconds, or another satellite heard it first | Ask again, and answer on the same satellite. |
| "The app's token doesn't match" | The token in Nova isn't the app's | Copy the token from the app's Configuration tab into the Voice ID card. |
| A recording turn ran a command instead | Home Assistant handled the sentence itself | Use the sentences Nova asks for, word for word. |
| The Voice ID status says Unreachable | The app is stopped, or the address is wrong | Start the app, and paste the address from its log again. Voice still works through the fallback. |
| Voice stopped working after picking Nova Voice ID | The app is down and no fallback is set | Pick a fallback in the Voice ID card. |

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
