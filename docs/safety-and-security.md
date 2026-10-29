# Nova Safety and Security

Nova watches for intruders, things left open, and household hazards such as
smoke, carbon monoxide, leaks and freezing pipes. This guide covers what it
needs, what it does by itself, and what it never does without you.

## What it does

- **Hazards.** A smoke, gas, carbon monoxide or water leak sensor is treated
  as critical. It is announced whatever the sensor is called, and no mute or
  quiet hours can silence it. This works on every install, with the
  Observer on or off (since 8.41.0), and goes to the phones as well.
- **Freezing pipes.** When the outdoor temperature reaches 35°F (about 2°C)
  Nova warns once. At 20°F (about −7°C) it raises a critical alert.
- **Left open (Sentinel).** A door open for 10 minutes, a window for 30, a
  garage door for 15, or a door lock unlocked for 20, gets a reminder. A
  garage door counts whether Home Assistant shows it as a sensor or as a
  cover. It repeats at the same interval, so a door says 10, then 20, then
  30 minutes.
  Door and window reminders stay quiet when it is above 10°C outside.
- **Garage or no garage.** Nova shows garage settings and wording only when
  your home has a garage: a garage door cover or sensor, or an area named
  Garage. Set **Settings → Home & Extras → Home layout → Garage** to Yes or
  No to choose yourself. This only changes what you see. Lockdown, the night
  sweep and intrusion check every door the same way whatever it says. The
  basement works the same way: its floor and door rows show only when there
  is an area or floor named Basement or Cellar, a floor below ground level,
  or a mapped basement door, unless you set **Basement** to Yes or No.
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
- [ ] **A phone notify service** in Configure, Rooms and speakers, "Phone notify service",
  and the devices to alert in the panel's **Settings → Notifications** card.
- [ ] **Optional: an alarm panel.** One Alarmo panel is found automatically.
  With anything else, pick it in **Settings → Security Alarm**.
- [ ] **Optional: ground floor areas** in Configure, Rooms and speakers,
  "Ground floor areas (for night time intrusion alerts)".

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

Every proactive message goes through one delivery path (since 8.44.0). It
applies the same rules in the same order, then decides whether the message is
spoken, sent to your phones, or both:

1. **Critical** always speaks and always goes to the phones. Nothing holds it.
2. A **mute**, **shush**, the hourly limit or a repeat holds the message.
   Messages Nova always pushes, such as weather warnings, still reach the
   phones.
3. With the **Announcements** switch off, nothing is spoken. High messages
   still go to the phones.
4. In **quiet hours**, or while the house is **asleep**, nothing is spoken. A
   high message goes to the phones instead. An everyday (medium or low) alert
   is dropped, not sent to the phones. Messages you set yourself, such as a
   reminder, also go to the phones.
5. With **Notifications only** on, the message goes to the phones instead of
   the speakers. If no phone can take it, Nova speaks it after all.

A reminder or briefing you set yourself ignores shush and the Announcements
switch. Quiet hours still apply unless you chose the time yourself. A repeat
within the repeat window is held even when the words differ, because each
message can carry a label.

Each kind keeps its own details:

| Alert | Spoken | Sent to phones |
|---|---|---|
| Intrusion, lockdown, freeze, offers | Below critical, not while asleep or in quiet hours | High and critical |
| Household events (doors, appliances' problems and so on) | When someone is home and awake, outside quiet hours; critical always | When nobody is home, or high and critical |
| An appliance finishing | When someone is home and awake | When nobody is home, or high |
| Left open reminders | Not in quiet hours or while asleep, unless urgent | Always |
| Earthquakes, weather, disasters | Outside quiet hours, by your warning levels | Always |
| Packages and mail | Outside quiet hours, with announcements on | A package taken while nobody is home, quiet hours or not |
| Doorbell and camera events | When notable | Only when they cannot be spoken and are high |

The outcome of every proactive message (who sent it, whether it was spoken
or pushed, and why not when it was held) is recorded in the activity log.
Safety alerts also go to the Decision Record.

A medium alert while nobody is known to be home, such as "Front Door is
unlocked while nobody appears to be home", goes to your phones. That covers
everyone away and presence unknown (for example a person reading unknown, or
no person entities set up).

"Someone is home" for household events comes from your people and the
trackers linked to them. Intrusion, lockdown, offers and appliances still
also count live motion when choosing speakers, so an intruder's movement
after the alarm goes off is still spoken.

## How intrusion decides

- While the alarm is **armed away or on vacation**, the alarm watches the
  house and Nova trusts it. Motion alone never starts Nova's own
  investigation, and Nova's general alerts don't announce it either. A
  sensor still on from you walking out is not mistaken for someone inside.
  This assumes your alarm (for example Alarmo) watches the sensors you care
  about in away mode.
- **When your alarm goes off**, Nova sends one critical alert, spoken in the
  house and pushed to every phone. It says which sensor tripped (Alarmo's
  `open_sensors`) and, if a camera covers that room on your floor plan,
  what Nova's vision check sees there: a person, no one, or that it couldn't
  tell. The snapshot comes with it. The camera check waits a few seconds at
  most, and a broken camera never stops the alert.
- The alarm's entry countdown (`pending`) as you come home is not an alarm,
  so Nova says nothing. Other alarm panels, such as camera hubs kept in sync
  with your chosen alarm, are not announced separately.
- Otherwise intrusion only runs when the residents are away, or their
  presence is unknown. Someone reading home always wins. It never runs on
  the mere absence of motion.
- Bare motion while away is not enough. Nova also needs an open door or
  window, or an alarm armed for home, before it opens an investigation. Pets
  and robot vacuums are the reason.
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
- Nova reads your own Home Assistant labels, on the entity or its device,
  and they beat any guess from a name. Labels are matched by name or id,
  ignoring case, spaces and hyphens:
  - **Appliance**: never a way in.
  - **Entry Point**: an outside door or window, always a way in.
  - **Group**: a sensor that repeats others, such as a floor's motion
    group. It never counts as a way in or as someone moving, so one person
    is not counted twice. A Home Assistant group is recognised by its member
    list even without the label.
- When someone is home and the alarm is armed home, night or custom bypass,
  or the household is asleep, walking from room to room never confirms an
  intrusion. Only the alarm itself going off, or a person on camera that
  Nova's vision check confirms, does. Everyone away works as before.
- In that same case, the first "Motion at … while the house is secured"
  alert, and the later "couldn't reach you" notice, go to your phone only,
  not the speakers. The alarm going off while you're out, everyone away and
  a confirmed intrusion still use the speakers.

On the **Intrusion** tab you can press **I'M LOOKING (HOLD)** to stop the
automatic escalation, or **CALL OFF (FALSE ALARM)**. Calling off an intrusion
is always confirmed, and when you ask by voice or from the Chat tab it needs a
tap on your phone.

## Chat

Any Home Assistant user can use the Chat tab, as with Assist. Everything Nova
does from there passes the same authority check as every other request, with
one extra rule. When a Chat message leads to unlocking a door, opening a
cover, garage door or door, disarming the alarm or calling off an intrusion,
Nova sends a request to the phone of the person who typed it and waits for
their tap. This holds even when **Voice confirmation** is off.

- Only the phone linked to the person entity for that account is asked, not
  the whole household.
- If that account has no person, or the person has no phone, Nova refuses the
  action and says why in the reply.
- A refused, unanswered or failed tap means the action is not done.
- Locking, closing, lights and heating need no tap.
- A message can be up to 1000 characters. You can send 20 a minute, one at a
  time. Over the limit, Chat says "You're sending too fast, wait a moment".

## Checking what Nova did

Before any lock, cover, garage door or alarm command, Nova reads the device
again at that moment, for its own actions and for your requests, and again
after you say yes:

- A device that reads unknown, unavailable or jammed, or a door sensor or
  zone it cannot read, is refused. Nothing is sent, and Nova says why. Use the
  Home Assistant app for a device Nova cannot read.
- A device already locked, closed or armed that way gets no command. The
  Actions log says it was already secure.
- A lock whose door sensor reads open is skipped by lockdown and the night
  sweep, and reported. Your own request is warned about and asked first.
- Arming the alarm with doors or windows open asks first and names them.
- Scenes and scripts are not checked this way, because Nova cannot see inside
  them.

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
Each entry also names the reading it was decided on, and keeps where each
fact came from and when (for example the person and the phone tracker that
put them at home).

With the Observer on, each decision it makes about an event also leaves one
short entry: the event, the reading, only the facts it used (who was home,
asleep, quiet hours), the rule and the reason, and the result. The result is
alerted, suppressed (Nova chose to speak but a rule held it) or ignored (Nova
chose to stay quiet), plus what delivery did. A critical hazard always gets
an entry. Routine events that never reach a decision, such as motion while
someone is home, get none and stay in the activity feed.

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
- Which locks guard a door. A thermostat's keypad lock is not one, for the
  left unlocked reminder. Lockdown, the night sweep and the "unlocked" lists
  in briefings skip only the locks you list under **Settings → Security
  Alarm → Locks left out of lockdown**. The list is empty unless you set it
  (an install from before 8.28.0 keeps the two locks it used to skip), and a
  change applies at once.

## What it will never do on its own

- Unlock a door, open a garage door or disarm the alarm because of a voice
  command alone, or because of a message in the Chat tab alone. That always
  needs a tap on your phone. Toggling a cover, or setting its position, counts
  as opening it (since 8.49.41). Toggling a switch that looks security related
  is treated like turning it off.
- Unlock, open, disarm, or run a scene or script by itself. When Nova acts on
  its own (lockdown, the night sweep, offers it trusts, a retry) it may only
  lock, close, and change lights or heating. Every action that can change a
  lock, cover, alarm, scene or script passes one authority check.
- Engage lockdown unless you turned on **Automatic lockdown**.
- Silence a critical alert. Mutes, the blanket shush and quiet hours never
  apply to one.
- Stop mentioning a lock, a cover (garage door, gate, door or blind), the
  alarm, a package, the doorbell or a camera, a door, window or security
  sensor, or a door, window or garage door left open, because it came up
  three days running. Other repeats go quiet after three days; these never do.
- Keep camera pictures. The one exception is up to 40 pictures from confirmed
  intrusions, stored privately for the Intrusion tab.

## Limits

- Nova only knows the house is empty from person entities and the trackers
  linked to them, or an armed away alarm. Without them, intrusion stays quiet.
- A scene or script that unlocks a door is not checked by the voice rule or
  the Chat rule, because Nova cannot see inside it.
- The Chat rule applies to the Chat tab only. Text typed into Assist, the
  Home Assistant companion app and automations follow the settings they
  always did.
- Freeze alerts need an outdoor temperature.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| No intrusion alert while away | Nova did not see the house as empty, or had no open door or armed alarm to back up the motion | Check everyone's person entity reads away, not unknown. |
| Intrusion alerts while someone is home | That person has no person entity, or their phone is not linked to it | Add one for each resident and link their phone. |
| A thermostat lock is ignored | It is not a door lock | Nothing to do. |
| Lockdown never engages | **Automatic lockdown** is off, or the alarm entity is not the one you arm | Check **Settings → Security Alarm**. |
| A door reminder does not come in summer | It is above 10°C outside | Nothing to do. |
| Chat says no phone is registered | Your Home Assistant account has no person entity, or that person has no phone with the companion app linked to it | Add a person for your account and link your phone to it. Chat refuses the action until you do. |

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
spoken yes or no. Off by default. When you asked on a voice satellite, Nova
asks in its own reply on that satellite's speaker and keeps listening there;
only a yes on that same satellite counts (see
[Confirming by voice](voice-and-speakers.md#confirming-by-voice)). **Mode**
chooses the satellite's own audio or the room speaker for requests that did
not come from a satellite. An unlock, a garage opening, or a toggle or
position change of a cover, asked for by voice, still needs a tap on your
phone, whatever this setting says.

Only in `/config/nova/config.json`:

| Setting | What it does |
|---|---|
| `intrusion_require_corroboration` | `true` by default. `false` lets bare motion while away open an investigation. |

## See also

- [Settings reference](settings-reference.md): every setting in one place
- [How Nova works](how-nova-works.md): the safety rules in more detail
