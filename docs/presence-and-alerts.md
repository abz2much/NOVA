# Nova Presence and Alerts

Nova keeps track of who is home, which rooms are occupied and whether the
house is asleep. It uses that to decide whether something is worth saying,
where to say it, and when to send it to your phone instead. This guide also
covers how to quieten Nova.

## What it does

- **Who is home.** From your person entities, and their phones or other
  trackers.
- **Which rooms are occupied.** From motion, occupancy and presence sensors in
  each Home Assistant area.
- **Whether the house is asleep.** From the sleep state you set, or
  automatically from bedroom occupancy and quiet hours.
- **What is worth saying.** The observer looks at home events. A cheap first
  pass decides what deserves a closer look, and most decisions are made
  locally without a cloud call.
- **How to say it.** Out loud in the right room, on the broadcast speakers, or
  on your phone, depending on urgency, quiet hours, sleep and your settings.

## What it needs

- [ ] **A person entity for each resident**, with a tracker.
- [ ] **Areas in Home Assistant**, with motion, occupancy or presence sensors
  assigned to them.
- [ ] **Bedroom areas and quiet hours** in **Configure**: Routing, "Bedroom
  areas", and Observer, "Quiet hours start" and "Quiet hours end".
- [ ] **Your phone**: "Phone notify service" in Configure, Routing, and the
  devices in the panel's **Settings → Notifications** card.

## What counts as someone being here

- A person entity reading home, or an active motion, occupancy or presence
  sensor.
- A camera's car, animal or package sensor never counts as a person. For
  example `binary_sensor.garage_car_occupancy` says a car is in view, not that
  someone is there. Those detections still work as their own events.
- An entity you exclude in **Settings → Excluded Entities** is ignored here
  too.

## How alerts reach you

- **Critical** alerts (smoke, carbon monoxide, a leak, a confirmed intrusion)
  always speak, on the broadcast speakers, and pass every mute.
- During **quiet hours**, or while the house is **asleep**, anything less than
  critical goes to your phone instead of being spoken.
- With **Notifications only** on, proactive alerts go to your phone instead
  of the speakers. Reminders, package and camera announcements, scheduled
  briefings and the infrastructure audit still speak.
- With **Announcements** off, Nova makes no proactive speech at all.

## What it learns by itself

- **Habituation.** If the same notification comes up three days running,
  Nova says "This has come up three days running, so I'll stop mentioning
  it." and then stays quiet about it. Intrusion, lockdown, freeze, lock and
  alarm alerts are never quietened this way.
- **Adaptive awareness**, if you turn it on. Routine alerts get Helpful and
  Not helpful buttons on your phone. Nova changes its timing only from your
  taps, and only after 5 alerts are rated. Muting or silence never counts.
- Each person's routines, shown on the Memory tab under Person Routines.

## What it will never do on its own

- Mute a critical alert, even with a blanket shush on.
- Treat the absence of motion as proof that nobody is home.
- Count a car, an animal or a package as a person.
- Send wearable health data to a cloud model. See Wellbeing below.

## Muting

- **Shush** (`nova.shush`) mutes an entity or a category: appliances,
  doors_windows, presence, energy, security, climate, lights or schedule. With
  no arguments, it silences whatever Nova just said.
- **Unshush** (`nova.unshush`) undoes it. With no arguments it clears all
  mutes.
- Mutes survive a restart. The Command Center's **Muted** card lists them with
  an Unmute button, and shows a banner when the blanket shush is on.

## Limits

- Room presence depends on your sensors. A room with none is never occupied.
- Sleep in Auto needs bedroom areas, or Nova only knows quiet hours.
- Adaptive awareness needs rated alerts. With few ratings, nothing changes.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| Nova speaks in an empty house | Something reads as present: a motion sensor stuck on, or a tracker reading home | Check the sensor, or exclude it. |
| An alert came to the phone, not the speaker | Quiet hours, sleep, or Notifications only | Check the times in Configure, Observer, and **Settings → General**. |
| Nova went quiet about something | It came up three days running, or it is muted | Check the **Muted** card. Unshush it if needed. |
| "LOCAL-ONLY" on the Observer Tuning card | The cloud models are unreachable | Nova keeps working with its offline Local Mind until they return. |

## Settings

Panel, **Settings → General**:

| Setting | What it does |
|---|---|
| Announcements | Master switch for all proactive speech. |
| Notifications only | Send proactive alerts to your phone instead of speaking them. Critical safety alerts still speak. |
| Sentinel | Doors, windows and locks left open. Garage settings and wording show only when your home has a garage; see Settings → Home & Extras → Home layout → Garage (Auto, Yes or No). |
| Observer | AI event awareness. |
| Cognition | Local first look at events, deciding what deserves deeper reasoning. |
| Rich Reasoning | Use the reasoning model first for medium and high priority events. |
| Sleep state | Auto (occupancy + quiet hours), Awake or Asleep. |

Panel, **Settings → Notifications**: the phones or services that get Nova's
alerts. Normal alerts go to every selected device.

Panel, **Settings → Excluded Entities**: entities, domains or labels Nova
should ignore. Presence detection, room routing, the observer and routine
learning all skip them. Nova can still control one if you ask by name.

Panel, **Settings → Observer Tuning**: live counts, the cloud link status,
and **Hourly Cap**, the most observer calls per hour (30 by default, 0 for
unlimited).

Panel, **Settings → Anticipation & Memory**:

| Setting | What it does |
|---|---|
| Adaptive awareness | Helpful and Not helpful buttons on routine alerts. Off by default. |
| Adaptive interruptions | Speak less after alerts are repeatedly marked unhelpful. Off by default. |

Panel, **Settings → Wellbeing Context**: lets Nova read a connected wearable
(heart rate, sleep, steps) so it can be quieter when you are resting. Context
only, not medical. Off by default. It only reaches the model when your main
model runs on a local Ollama server.

Only in `/config/nova/config.json`:

| Setting | What it does |
|---|---|
| `cognition_threshold` | How important an event must be before Nova escalates it. |

## See also

- [Settings reference](settings-reference.md): every setting in one place
- [Host health](host-health.md): alerts about the machine Nova runs on
