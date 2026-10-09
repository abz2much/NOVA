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
  the covers it controls. It is off until you turn it on.

## What it needs

- [ ] **People in Home Assistant**: a person entity for each resident, with a
  phone or other tracker. Nova uses them to know the house is empty.
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

## How intrusion decides

- Intrusion only runs when everyone tracked is away, or the alarm is armed
  away. It never runs on the mere absence of motion.
- Bare motion while away is not enough. Nova also needs an open door or
  window, or an armed alarm, before it opens an investigation. Pets and robot
  vacuums are the reason.
- At night it needs a real breach: an exterior door or window open. If you
  set ground floor areas, only those count.
- Outdoor motion never starts an indoor intrusion.
- A camera's car, animal or package sensor never counts. Real motion and
  person sensors do.
- With **Require confinement for intrusion monitoring** on, Nova only watches
  while a lockdown is on or the alarm is armed.
- Appliance and cabinet doors never count as a way in: a fridge, freezer,
  oven, dishwasher, washing machine, dryer, microwave or cabinet. Nor does
  anything on your exclude list.
- When someone is home and the alarm is armed home or night, or the household
  is asleep, walking from room to room never confirms an intrusion. Only the
  alarm itself going off, or a person on camera that Nova's vision check
  confirms, does. Armed away, vacation and everyone away work as before.
- In that same case (someone home, armed home or night, or asleep), the first
  "Motion at … while the house is secured" alert goes to your phone only, not
  the speakers. Armed away, vacation, everyone away and a confirmed intrusion
  still use the speakers.

On the **Intrusion** tab you can press **I'M LOOKING (HOLD)** to stop the
automatic escalation, or **CALL OFF (FALSE ALARM)**. Calling off an intrusion
is always confirmed, and when you ask by voice it needs a tap on your phone.

## What it learns by itself

- The single Alarmo panel, if there is one.
- Which sensors are outdoors, so they never start an indoor intrusion.
- Which locks guard a door. A thermostat's keypad lock is not one.

## What it will never do on its own

- Unlock a door or open a garage door because of a voice command alone. That
  always needs a tap on your phone.
- Engage lockdown unless you turned on **Automatic lockdown**.
- Silence a critical alert. Mutes, the blanket shush and quiet hours never
  apply to one.
- Keep camera pictures. The one exception is up to 40 pictures from confirmed
  intrusions, stored privately for the Intrusion tab.

## Limits

- Nova only knows the house is empty from person and tracker entities, or an
  armed alarm. Without them, intrusion stays quiet.
- A scene or script that unlocks a door is not checked by the voice rule,
  because Nova cannot see inside it.
- Freeze alerts need an outdoor temperature.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| No intrusion alert while away | Nova did not see the house as empty, or had no open door or armed alarm to back up the motion | Check everyone's person entity reads away. |
| Intrusion alerts while someone is home | That person has no person entity or tracker | Add one for each resident. |
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
