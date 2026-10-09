# Nova Weather Warnings

Nova's Hazard Monitor reads official weather warnings for your area and tells
you about them: on your phone, and out loud for the serious ones. It can also
watch for nearby earthquakes and natural disasters.

## What it does

- Reads one warning source: Met Éireann for Ireland, the US National Weather
  Service, or a custom CAP feed for anywhere else.
- Announces a warning once, again if its level changes, and sends a phone
  only note when it is cancelled.
- Yellow goes to your phone. Orange and Red are also spoken, outside quiet
  hours. During quiet hours only Red is spoken, unless you change it.
- The morning briefing lists the Orange and Red warnings in force.
- The **Settings → Hazard Monitor** card lists the warnings in force for your
  area, with their source.

## What it needs

- [ ] **Home Assistant's home location and country**, in Home Assistant's own
  settings. Nova uses them to pick the source and the area.
- [ ] **Turn it on**: **Settings → Hazard Monitor → Monitor**. It is off by
  default.
- [ ] **Outside Ireland and the US**: the https address of a CAP feed from
  your national warning service.

## What it works out by itself

- **The source.** Ireland gets Met Éireann and the US gets the National
  Weather Service. Every other country starts on Custom feed.
- **Your area.** For Met Éireann, Nova picks the nearest county from your home
  position. For a CAP feed, it alerts when a warning's area covers your home,
  or matches an area code or name you give.
- **What is new.** Warnings already announced are saved, so a restart does
  not repeat them.

Nova checks for warnings every 10 minutes.

## What it will never do on its own

- Treat a failed check as "no warnings". A warning is only treated as
  cancelled when a complete feed no longer lists it, or a CAP Cancel message
  arrives.
- Speak a warning below the levels you chose, or at night below Red unless you
  change that.
- Fetch a feed that is not https.

## Limits

- One source at a time.
- Met Éireann picks the county nearest your home. The county centres are
  approximate, so a home near a border may get the neighbouring county.
- The custom feed section is titled "Custom CAP feed (not tested by Nova)". It depends on how
  your national service publishes its feed.
- No Europe wide source yet. Use a Custom feed.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| "No warnings in force for your area." | Nothing current for your area | Nothing to do. |
| Nothing ever arrives | The monitor is off, or the source is Custom with no feed | Turn **Monitor** on, or add a **Feed address**. |
| A warning for the wrong county | Your home is near a county border | Set **Override lat / lon**. |
| A warning was only sent to the phone | It was Yellow, or it arrived during quiet hours below your night level | Change the levels under **Advanced**. |

## Settings

Panel, **Settings → Hazard Monitor**:

| Setting | What it does |
|---|---|
| Monitor | Turns the Hazard Monitor on. Off by default. |
| Source | Met Éireann (Ireland), US (National Weather Service), or Custom feed. |
| Feed address | Custom feed only: the https address of one CAP alert, or an Atom or RSS list of them. |
| Area codes, Area names | Custom feed only: one per line. An alert that matches is treated as yours. |
| Override lat / lon | Optional. Saved only when it differs from your home position. |

Under **Advanced**:

| Setting | What it does |
|---|---|
| Push to phone from | Yellow by default. Lower levels are ignored. |
| Also speak from | Orange by default. Below this, phone only. |
| Speak during quiet hours from | Red by default. Off makes every warning phone only at night. |
| Earthquakes, NASA disasters | Separate feeds from the US Geological Survey and NASA. |
| Quake radius (km) / min mag | 300 km and magnitude 2.5 by default. |

Quiet hours are set in **Configure → Observer**, "Quiet hours start" and
"Quiet hours end".
