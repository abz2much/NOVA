# Nova Cameras and Faces

Nova can look at your cameras when the doorbell rings or a person is seen,
watch the porch for packages and mail, remember what the cameras described,
and know which faces belong to the people who live here. Every part of this
is optional.

## What it does

- **Doorbell and person events.** When the doorbell rings, or a camera sees a
  person, Nova looks at the picture and tells you who or what is there.
- **Packages and mail.** Nova watches porch cameras for a package or mail,
  and says when one arrives, is left out, or is taken.
- **Scene memory.** If you turn it on, Nova keeps the words its cameras
  produced, so it can answer "where did I last see my keys?" or "what has
  changed in the hallway since yesterday?".
- **Faces.** Nova reads names from Frigate or Double Take, and keeps a list of
  the residents.
- **Learning.** Camera detections feed Nova's routine learning, for example
  "a person usually appears at the front door around this time".

## What it needs

- [ ] **Cameras in Home Assistant.** Any `camera.*` entity works.
- [ ] **A vision model**, set in **Settings → AI Models**. Any provider that
  reports image support works, including a local Ollama vision model.
- [ ] **Recommended: [Frigate](https://frigate.video).** Its person and object
  detection is more reliable than a model guessing, and its snapshots are
  sharper.
- [ ] **Nest:** the official Google Nest integration. Nest streams expire, so
  the README explains how to restream them through go2rtc.
- [ ] **Eufy:** the [eufy_security](https://github.com/fuatakgun/eufy_security)
  integration. Nova finds each Eufy camera by itself and uses its own
  doorbell, face and package sensors, with no vision call for routine events.
- [ ] **For faces:** Frigate face recognition or Double Take, and MQTT in
  Home Assistant.

## Packages and mail

- Nova watches cameras whose name includes `doorbell`, `porch`, `front` or
  `frontdoor`. Wide views are skipped, for example a name with `yard`,
  `driveway`, `garden`, `street` or `garage`.
- A porch motion sensor (named the same way) starts a check early. A sensor
  named `mailbox`, `letterbox` or `postbox` announces mail.
- Each camera announces each kind (delivered, mail, removed, left out) at
  most once every 30 minutes.
- A camera with Eufy's own package sensors uses those instead of a picture.

Sensors are found when Nova starts, so a new one needs a Nova reload.

## What it learns by itself

- Which cameras are indoors or outdoors. You can override this per camera in
  **Settings → Cameras**.
- Eufy cameras and their sensors, by their unique id, so renaming them is
  fine.
- Repeated camera patterns, once they show up over several days.

## What it will never do on its own

- Keep a picture it analysed. Confirmed intrusion pictures are the only
  exception.
- Store an image or a face. Scene memory keeps text only, and the Faces tab
  shows names only.
- Name a person from a guess. A name comes from Frigate or Double Take.
- Close an intrusion that is already open because a resident was seen.

## Faces and the intrusion stand down

On the **Faces** tab, add each resident with **ADD RESIDENT**, using the name
exactly as Frigate or Double Take reports it. Case and spacing do not matter.

**Residents can stand down a new intrusion alert** is in **Settings →
Security Alarm**, and is off by default. When on, a resident recognised at or
above the recognition confidence on a camera in the last 3 minutes stops Nova
opening a NEW intrusion investigation. It does not apply if an unknown face or
an unexplained person was also seen. It never closes one that is already
open. A face can be a photo or a look alike, so treat it as a convenience, not
a security control.

## Limits

- Battery Nest cameras cannot give a still picture while idle. Nova falls back
  to its own snapshot path, but the restream described in the README works
  best.
- Packages are found by camera name. A camera with the word glued into a
  longer name may not be watched.
- Nova has no face engine of its own.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| A camera tile is blank or black | The camera gives no still picture | Open **Camera Watch → DIAG** on the Command Center. For Nest, use a restream and `camera_overrides`. |
| The porch camera is not watched for packages | Its name has no porch word, or has a wide view word | Rename it, or check its location in **Settings → Cameras**. |
| A new mailbox sensor is ignored | Sensors are found at start | Reload Nova. |
| Faces tab says no recognition source | Nova sees no Frigate or Double Take names | Set one up, with MQTT in Home Assistant. |
| No faces seen recently | Nobody has been recognised lately | Nothing to do. |

## Settings

Panel, **Settings → Cameras**:

| Setting | What it does |
|---|---|
| Camera Watch — auto-analyze doorbell and person events | Looks at the picture on a doorbell press or person event. |
| Also analyze motion events | Also looks at Frigate detection events that are not doorbell presses. Noisier. Off by default. |
| Package Watch — detect packages and mail at the door | Watches porch cameras for packages and mail. |
| Visitor Learning — silently log strangers seen at the door | Keeps a quiet log of visitors. Never spoken. |
| Face recognition source | Both (Double Take + Frigate), Frigate only, or Double Take only. |
| Recognition confidence | How sure a face match must be before Nova uses it. |
| Each camera | A name for Nova, and its location. AUTO shows what Nova worked out. |

Panel, **Settings → Doorbell Training**: the analysed doorbell events and any
recurring visitor patterns. **Scan backlog** reads older recorded events.

Panel, **Settings → Routine Learning**:

| Setting | What it does |
|---|---|
| Learn from camera detections | Feeds detections into routine learning. On by default. No images or faces stored. |
| Scene memory | Keeps what the cameras describe, as text. Off by default. |
| Keep scene memory for | How many days scene memory keeps, from 1 to 90. 14 by default. |
| Forget everything | Deletes all of scene memory. |

Only in `/config/nova/config.json`:

| Setting | What it does |
|---|---|
| `camera_overrides` | Takes every frame for a camera from another one, for example a Nest camera from its go2rtc restream: `{"camera.front_doorbell": "camera.front_doorbell_restream"}`. Merge it into the existing file. |
