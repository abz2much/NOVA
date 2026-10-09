# Nova Voice Recognition

Nova can know who it is talking to by voice, combine that with who is home and
who is on camera, and learn people's voices over time from ordinary
conversation. This guide covers how to set it up.

## How it fits together

Nova does not run a voice recognition model inside Home Assistant. That would
be heavy and would need the raw audio. Instead, a separate speaker
recognition service does the recognition and tells Home Assistant who is
speaking. Nova reads that and uses it as the strongest of its identity
signals.

```
  Voice satellite or Assist audio
            │
            ▼
  Speaker recognition service         ← does the recognition and enrolment
            │  tells Home Assistant who is speaking
            ▼
  binary_sensor.<person>_voice   or   sensor.current_speaker
            │
            ▼
  Nova's identity resolver: voice + presence + face  ──►  who you are
```

You supply the service. Nova supplies the combining and everything it already
does per person.

## What it needs

- [ ] **A speaker recognition service** that shows the current speaker as a
  Home Assistant entity. Two that work:
  - **VoiceBM** (`github.com/cybericebyte/VoiceBM`). Runs on the CPU and
    publishes over MQTT with Home Assistant discovery. It gives a
    `binary_sensor.<person>_voice` for each person, on while they speak, and a
    current speaker sensor, plus its own enrolment flow.
  - **speaker-recognition** (`github.com/EuleMitKeule/speaker-recognition`).
    A Home Assistant add on and integration with a current speaker sensor,
    and `/train` and `/recognize` endpoints.
- [ ] **A couple of voices enrolled** in that service, following its own
  instructions.
- [ ] **The entity working.** In **Developer Tools → States**, check that
  `binary_sensor.<person>_voice` turns on when that person speaks, or that
  `sensor.current_speaker` shows a name.

Neither service needs a GPU. Both default to the CPU.

## Point Nova at it

In **Settings → Devices & Services → Nova → Configure → Identity**:

- Turn on **Voice fingerprinting (requires a local GPU)**. This switches on the
  voice signal. The services above still run on the CPU.
- Set **Voice recognition source**: either a pattern for per person sensors,
  `binary_sensor.*_voice` (the VoiceBM style), or a single current speaker
  sensor, `sensor.current_speaker`.

Nova then weighs voice most heavily, and falls back to who is home and who is
on camera when the voice is uncertain.

**Confidence.** If the source gives a confidence, score, probability or
similarity attribute, Nova uses it. It accepts 0 to 1, or 0 to 100. Otherwise
it uses `voice_recognition_confidence`, 0.85 by default.

## What it learns by itself

The service does the enrolling, but it needs to know who an unknown voice
belongs to. Nova often knows already: you are the only one home, or a camera
just recognised your face. When Nova is sure who is speaking from those
signals, but the voice service does not know the voice yet, it fires an event:

```
event: nova_voice_enroll_candidate
data:  { person: "<person>", device_id: "…" }
```

Connect that event to your service's enrolment, so voices build up from
normal conversation. An example for VoiceBM, using its enrol command:

```yaml
automation:
  - alias: "Nova: auto enrol voice"
    trigger:
      - platform: event
        event_type: nova_voice_enroll_candidate
    action:
      # Enrol the pending sample under the person Nova identified.
      # Use your service's own enrol action or MQTT command.
      - service: mqtt.publish
        data:
          topic: "voicebm/enroll"
          payload: "{{ trigger.event.data.person }}"
```

For speaker-recognition, call its `/train` with the pending sample, named
with `trigger.event.data.person`.

The event fires at most once per person every 5 minutes, and can be turned
off with **Auto-flag voices to enroll as people speak**.

## What it will never do on its own

- Run voice recognition itself, or keep raw audio.
- Name a person below the minimum confidence. It says the speaker is unknown
  instead.
- Enrol a voice. It only fires the event, and your automation decides.

## Limits

- Recognition is only as good as the service and its enrolled samples.
- Unlocking a door or opening a garage by voice still needs a tap on your
  phone, whoever is speaking.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| Nova never knows who is speaking | The voice signal is off, or the source does not match an entity | Check **Voice fingerprinting** and **Voice recognition source**. |
| The source entity never changes | The service is not publishing | Check it in **Developer Tools → States**. |
| Nova says "unknown" too often | The confidence is below the minimum | Lower **Minimum confidence before committing to a person**, or enrol more samples. |
| No enrolment events | Auto flag is off, or it fired for that person in the last 5 minutes | Check **Auto-flag voices to enroll as people speak**. |

## Settings

**Configure → Identity**:

| Setting | Key | Default | What it does |
|---|---|---|---|
| Recognize who's speaking | `identity_enabled` | on | Works out who is speaking at all. |
| Voice fingerprinting (requires a local GPU) | `identity_voice_fingerprint` | off | Turns on the voice signal. |
| Voice recognition source | `voice_recognition_source` | blank | The current speaker sensor, or a pattern such as `binary_sensor.*_voice`. |
| Auto-flag voices to enroll as people speak | `voice_recognition_auto_enroll` | on | Fires `nova_voice_enroll_candidate`. |
| Minimum confidence before committing to a person | `identity_min_confidence` | 0.45 | Below this, Nova says "unknown" rather than guess. |

Only in `/config/nova/config.json`:

| Setting | Default | What it does |
|---|---|---|
| `voice_recognition_confidence` | 0.85 | The score used when the source gives none. |

## How Nova uses the result

Once Nova knows who is speaking, everything it does per person follows:
commands are put down to the right person, their own preferences and facts
come up (and stay private from other residents), and the routines it learns
are filed under the right person.
