# Host health

This guide is for you if you want Nova to keep an eye on the machine Home
Assistant runs on: processor, memory and disk use, memory and input/output
pressure, and the temperature where the hardware reports it. It is off by
default.

## How it works

Nova reads only Home Assistant's own **System Monitor** integration. It reads
System Monitor's sensors the same way it reads any other sensor. Nova never
reads `/proc`, `/sys` or any other system path directly, never runs shell
commands, and never calls the Supervisor. It has no way to control,
restart or repair the machine.

## Set it up

1. Go to **Settings → Devices & services**.
2. Add or open the **System Monitor** integration.
3. Open its entities.
4. Turn on the recommended entities listed below. System Monitor turns
   several of them off by default.
5. Turn on any optional entities your hardware actually reports.
6. Come back to Nova's **Settings → Host Health**.
7. Check the readings Nova found, and pick a source by hand for anything
   that has more than one candidate. This is most often disk use, if you
   monitor more than one disk.
8. Turn on **Host health awareness**.
9. If you want Nova to say something when a problem lasts, turn on
   **Alerts** as well. It is a separate switch, and turning it off stops
   alerts at once.

## Which entities to turn on

Recommended: Processor use, Memory usage, Memory Pressure Some 60s Average,
Memory Pressure Full 60s Average, IO Pressure Some 60s Average, IO Pressure
Full 60s Average, and Disk usage percentage for your Home Assistant data
disk.

Optional: Processor temperature, Swap usage, CPU Pressure Some 60s Average,
Load 5 min and Uptime.

Nova finds these by the integration that owns them and their own internal
type, never by their name or entity ID. Renaming an entity, or running Home
Assistant in another language, makes no difference. If there is exactly one
candidate for a reading, Nova maps it for you. If there are several, you
pick one. If a System Monitor entity is turned off, Nova says so and tells
you how to turn it on. It never turns one on for you.

## Worth knowing

- Nova samples every 2 minutes. Just after you turn awareness on, the panel
  can show dashes for a short while. That is the normal wait for the first
  sample, not a fault. Wait up to 3 minutes and refresh the Nova panel.
- Some hardware doesn't report processor temperature at all. Whether the
  pressure (PSI) sensors exist depends on your platform and kernel. A
  missing optional entity (temperature, PSI, swap, load or uptime) is not a
  setup failure. Nova shows it as not available and works with the readings
  it has.
- Disk use measures how full the disk is, not whether it is healthy. A full
  disk and a failing disk are different problems, and Nova only reports the
  first.
- Input/output pressure measures how much work is waiting on the disk, not
  whether the drive is failing. Nova never claims to measure NVMe or SSD
  health.
- Nova can't warn you once the machine has frozen or lost power, because
  Nova runs on it. This shows you when things are getting worse. It is not a
  replacement for proper infrastructure monitoring.

## How alerts work

An alert needs a problem to last across several samples in a row, never a
single spike. You set how long it must last (2 to 120 minutes) and the
minimum gap between repeat alerts (5 to 720 minutes). Recovery is only
announced after the readings have been steady again for a while. A restart
or reload starts this tracking from zero.

Alerts go through the same presence aware announcements and phone
notifications as every other Nova alert. The load on the machine is never
treated as a household safety emergency.

When you ask Nova "why is Nova slow?", the health summary is part of the
evidence it and HOMER look at.

## Infrastructure audit

Separately, every 15 minutes Nova checks the sensors you list under
**Settings → Host Health → Infrastructure audit**. A sensor measured in
percent is flagged above 90 and critical above 96. A binary sensor is
flagged when it goes off, or when it goes on for a problem sensor. A listed
sensor that can't be read gets a warning. The list starts empty, and with
nothing listed the audit does nothing. Pick a room for its alerts, or none
to only record them in the log.

## See also

- [Settings reference](settings-reference.md): the host health settings
- [Getting started](getting-started.md#when-something-is-not-working): Diagnostics and HOMER
- [Presence and alerts](presence-and-alerts.md): how alerts reach you
