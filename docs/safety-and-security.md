# Nova Safety and Security

Nova watches for intruders, things left open, and household hazards such as
smoke, carbon monoxide, leaks and freezing pipes. This guide covers what it
needs, what it does by itself, and what it never does without you.

## What it does

- **Hazards.** A smoke, gas, carbon monoxide or water leak sensor is treated
  as critical. It is announced whatever the sensor is called, and no mute or
  quiet hours can silence it.
- **Freezing pipes.** When the outdoor temperature reaches 35°F (about 2°C)
  Nova warns once. At 20°F (about −7°C) it raises a critical alert.
- **Left open (Sentinel).** A door open for 10 minutes, a window for 30, a
  garage door for 15, or a door lock unlocked for 20, gets a reminder. It
  repeats at the same interval, so a door says 10, then 20, then 30 minutes.
  Door and window reminders stay quiet when it is above 10°C outside.
- **Intrusion.** Motion inside while the household is away starts an
  investigation. Nova tells you once and keeps watching. It raises the full
  alarm, on every speaker and device, only when it confirms an intruder: a
  real way in through the house, or a person seen on an indoor camera. If
  nobody answers in time without that proof, it sends a softer notice asking
  you to check.
- **Lockdown.** When the alarm is armed, Nova can lock the doors and close
  the covers it controls. It is off until you turn it on. With it on, Nova
  also locks the doors and closes open covers at night while the household
  is asleep. The night sweep closes every open cover, blinds included; the
  armed lockdown closes only doors, garages, windows and gates. About 25
  seconds later the night sweep checks each one, and only says the house is
  secured when every lock reads locked and every cover reads closed. Anything
  that did not take is named and sent to your phones once, then left alone
  until morning. If you unlock or open something the sweep secured, it
  leaves it alone until morning too, and tells you once. It never sends more
  than 12 commands in an hour. Doors and windows
  it cannot close are named so you can close them by hand; a fridge or
  cabinet door, a sensor on your exclude list, or a shed door is not. A door
  or window whose sensor cannot be read is named too ("I can't tell if it's
  closed"), and lockdown never says the home is fully secured while one is.
- **Packages.** A package taken from the door while nobody is home is sent to
  your phones, as well as spoken.

## What it needs

- [ ] **People in Home Assistant**: a person entity for each resident, with a
  phone or other tracker linked to it. Nova uses them to know the house is
  empty.
- [ ] **Motion or presence sensors indoors**, and door and window contact
  sensors.
- [ ] **For hazards**: smoke, gas, carbon monoxide or moisture sensors. For
  freeze alerts, a weather entity, or a sensor named outdoor or outside.
- [ ] **A phone notify service** in Configure, Routing, "Phone notify service",
  and the devices to alert in the panel's **Settings → Notifications** card.
- [ ] **Optional: an alarm panel.** One Alarmo panel is found automatically.
  With anything else, pick it in **Settings → Security Alarm**.
- [ ] **Optional: ground floor areas** in Configure, Routing, "Ground-floor
  areas (for night-time intrusion alerts)".

## Who counts as home

Every part of Nova (intrusion, lockdown, packages, briefings and offers)
uses one answer to who is home:

- **Home** when a person reads home, or a device tracker linked to a person
  reads home.
- **Away** when every person reads away (not home, or another zone).
- **Unknown** otherwise, for example a person reading unknown. Unknown is
  never treated as away.
- A device tracker not linked to a person, such as a TV or a hub, does not
  count. If you have no person entities at all, Nova uses your device
  trackers instead.
- Motion never decides who is home.

And one answer to what the alarm means:

| Alarm state | Means |
|---|---|
| Armed away, armed vacation | Nobody should be moving about |
| Armed home, armed night, armed custom bypass | Residents are expected to move about |
| Disarmed, arming, pending, disarming, triggered, unavailable | Says nothing either way |

## One picture of the house

Every 30 seconds Nova takes one snapshot of the house: who is home, the
alarm, whether the house is asleep, quiet hours, what is open or unlocked,
whether lockdown is on, and any open situations. Everything Nova decides in
that moment uses the same snapshot. Anything it cannot read is treated as
unknown, never as away or secure.

## After a restart

Nova saves what it is in the middle of to `nova/situations.json`, so a
restart or a settings reload does not lose it:

- an intrusion investigation carries on (if it was saved in the last 10
  minutes) and can still confirm, without a second first alert;
- a parcel already on the step is not announced again, and its pickup is
  still noticed (saved in the last 24 hours);
- the freeze warning is not repeated.

If the file is missing or damaged, Nova starts fresh, as it always did.

## How alerts reach you

Every alert goes through one place that decides whether it goes out, and
whether it is spoken, sent to your phones, or both. Each kind keeps its own
rules:

| Alert | Spoken | Sent to phones |
|---|---|---|
| Intrusion, lockdown, freeze, offers | Below critical, not while asleep or in quiet hours | High and critical; anything not spoken while asleep or in quiet hours; anything not spoken while nobody is known to be home |
| Household events (doors, appliances' problems and so on) | When someone is home and awake, outside quiet hours; critical always | When nobody is home, or high and critical |
| An appliance finishing | When someone is home and awake | When nobody is home |
| Left open reminders | Unless asleep | Always |
| Earthquakes, weather, disasters | Outside quiet hours, by your warning levels | Always |
| Packages and mail | Outside quiet hours, with announcements on | A package taken while nobody is home, quiet hours or not |
| Doorbell and camera events | When notable | No |

A medium alert while nobody is known to be home, such as "Front Door is
unlocked while nobody appears to be home", goes to your phones. That covers
everyone away and presence unknown (for example a person reading unknown, or
no person entities set up).

"Someone is home" for household events comes from your people and the
trackers linked to them. Intrusion, lockdown, offers and appliances still
also count live motion when choosing speakers, so an intruder's movement
while armed away is still spoken.

## How intrusion decides

- Intrusion only runs when the residents are away, or their presence is
  unknown and the alarm is armed away. Someone reading home always wins. It
  never runs on the mere absence of motion.
- Bare motion while away is not enough. Nova also needs an open door or
  window, or an armed alarm, before it opens an investigation. Pets and robot
  vacuums are the reason.
- At night it needs a real breach: an exterior door or window open. If you
  set ground floor areas, only those count.
- Outdoor motion never starts an indoor intrusion.
- A camera's car, animal or package sensor never counts. Nor does a motion
  sensor on your exclude list. Real motion and person sensors do.
- With **Require confinement for intrusion monitoring** on, Nova only watches
  while a lockdown is on or the alarm is armed.
- Appliance and cabinet doors never count as a way in: a fridge, freezer,
  oven, dishwasher, washing machine, dryer, microwave or cabinet. Nor does
  anything on your exclude list.
- When someone is home and the alarm is armed home, night or custom bypass,
  or the household is asleep, walking from room to room never confirms an
  intrusion. Only the alarm itself going off, or a person on camera that
  Nova's vision check confirms, does. Armed away, vacation and everyone away
  work as before.
- In that same case, the first "Motion at … while the house is secured"
  alert, and the later "couldn't reach you" notice, go to your phone only,
  not the speakers. Armed away, vacation, everyone away and a confirmed
  intrusion still use the speakers.

On the **Intrusion** tab you can press **I'M LOOKING (HOLD)** to stop the
automatic escalation, or **CALL OFF (FALSE ALARM)**. Calling off an intrusion
is always confirmed, and when you ask by voice it needs a tap on your phone.

## Checking what Nova did

After Nova locks or closes something it checks the device afterwards: the
night sweep, lockdown, the voice "secure" reply and devices you ask it to
control. **Logs → Actions** shows verified, unverified or failed for each.
Scenes, scripts and automations are shown as "not checkable", because Nova
cannot see what they do.

## The decision log

Every safety decision is written to **Logs → Decisions** in the same form:
what Nova saw (who was home, the alarm, asleep, quiet hours), what it
concluded, what it did (spoken, sent to phones, or both) and why. Where a
decision sent commands (lockdown, the night sweep, the voice "secure" reply),
it lists their request ids, which match the entries in **Logs → Actions**. That covers
intrusion, lockdown, the night sweep, hazards, packages and doors left open.

## What it learns from you

Nova only learns from things you confirmed: a Helpful or Not helpful tap on
an alert, saying "it's a false alarm", a real or false label on the
**Intrusion** tab, and accepting or dismissing a suggestion. Nothing it
guesses for itself changes how it behaves.

Saying "it's a false alarm" also teaches it. After three false alarms at the
same place and time of day, with none marked real, the first alert for that
pattern stays quiet. Nova still investigates, and a confirmed intrusion
always alerts. One "real" label stops it learning to ignore that pattern.

## What it learns by itself

- The single Alarmo panel, if there is one.
- Which sensors are outdoors, so they never start an indoor intrusion.
- Which locks guard a door. A thermostat's keypad lock is not one.

## What it will never do on its own

- Unlock a door, open a garage door or disarm the alarm because of a voice
  command alone. That always needs a tap on your phone.
- Unlock, open, disarm, or run a scene or script by itself. When Nova acts on
  its own (lockdown, the night sweep, offers it trusts, a retry) it may only
  lock, close, and change lights or heating. Every action that can change a
  lock, cover, alarm, scene or script passes one authority check.
- Engage lockdown unless you turned on **Automatic lockdown**.
- Silence a critical alert. Mutes, the blanket shush and quiet hours never
  apply to one.
- Stop mentioning a lock, a cover (garage door, gate, door or blind) or the
  alarm because it came up three days running. Other repeats go quiet after
  three days; these never do.
- Keep camera pictures. The one exception is up to 40 pictures from confirmed
  intrusions, stored privately for the Intrusion tab.

## Limits

- Nova only knows the house is empty from person entities and the trackers
  linked to them, or an armed away alarm. Without them, intrusion stays quiet.
- A scene or script that unlocks a door is not checked by the voice rule,
  because Nova cannot see inside it.
- Freeze alerts need an outdoor temperature.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| No intrusion alert while away | Nova did not see the house as empty, or had no open door or armed alarm to back up the motion | Check everyone's person entity reads away, not unknown. |
| Intrusion alerts while someone is home | That person has no person entity, or their phone is not linked to it | Add one for each resident and link their phone. |
| A thermostat lock is ignored | It is not a door lock | Nothing to do. |
| Lockdown never engages | **Automatic lockdown** is off, or the alarm entity is not the one you arm | Check **Settings → Security Alarm**. |
| A door reminder does not come in summer | It is above 10°C outside | Nothing to do. |

## Settings

Panel, **Settings → Security Alarm**:

| Setting | What it does |
|---|---|
| Security alarm | The one alarm panel Nova uses. "Auto detect a single Alarmo panel" by default. Every other panel is ignored. |
| Automatic lockdown | Lets the alarm and sleep mode lock doors and close covers. Off by default. |
| Require confinement for intrusion monitoring | Only watch for intruders while lockdown is on or the alarm is armed. |
| Residents can stand down a new intrusion alert | See [Cameras and faces](cameras-and-faces.md). Off by default. |

Panel, **Intrusion** tab:

| Setting | What it does |
|---|---|
| Auto-escalate if no response after | How long an unanswered alert waits before the softer "please check" notice: 1, 2, 3, 5 or 10 minutes. 2 minutes by default. |
| Confirm Frigate person with Nova vision before alarming | Nova checks the picture itself before raising the alarm. On by default. |

Panel, **Settings → Sentinel Rules**: each left open rule can be turned on or
off. Panel, **Settings → General**: **Sentinel** turns all of them on or off.

Panel, **Settings → Voice Confirmation**: **Voice confirmation** asks out loud
before sensitive actions such as unlock, garage and disarm, and listens for a
spoken yes or no. Off by default. **Mode** chooses the satellite's own audio or
the room speaker. An unlock or garage opening asked for by voice still needs a
tap on your phone, whatever this setting says.

Only in `/config/nova/config.json`:

| Setting | What it does |
|---|---|
| `intrusion_require_corroboration` | `true` by default. `false` lets bare motion while away open an investigation. |
