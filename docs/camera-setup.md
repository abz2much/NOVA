# Camera setup

This guide is for you if you want Nova's camera features, such as doorbell
analysis, package detection and the live camera tiles, and need to connect
your cameras first. Everything here is optional. If you are starting with
voice and reasoning, skip it and come back later.

## How Nova uses cameras

Nova does not need any particular camera brand. It works with any camera
Home Assistant shows as a `camera.*` entity, and it takes still pictures
through Home Assistant's normal image interface.

[Frigate](https://frigate.video) is the recommended backbone. Frigate's own
object detection for people and packages, sent over MQTT, is far more
reliable than a vision model guessing from a raw feed. Its event snapshots
are high resolution and cropped to what it detected. Nova listens to
`frigate/events` and uses those snapshots directly. Face recognition reads
Frigate's `tracked_object_update` and `last_recognized_face` channels. If you
run cameras at all, routing them through Frigate is what makes the vision
features sharp.

Nest is supported, but you don't need it. Nova can use Nest cameras and
doorbells, but Google's cameras need extra setup because of how their stream
interface behaves. If you have Nest cameras, route them into Frigate through
go2rtc. You then get Nest's doorbell events as well as Frigate's detection
and reliable frames.

Eufy Security is supported and needs no extra setup at all. With the
[eufy_security HACS integration](https://github.com/fuatakgun/eufy_security)
in place, Nova finds each Eufy camera and doorbell by itself. It uses the
device's own sensors for doorbell presses, stranger or known face, and
packages. It needs no Frigate, no go2rtc and no vision model call for the
routine cases, such as a known face or a normal delivery. Nova finds the
device by its unique ID, so renaming it in Home Assistant never breaks it.

Any other camera, such as a generic RTSP or local ONVIF camera, works through
the normal snapshot path with no special setup. Add it to Frigate for
detection, or let Nova take stills from it directly.

The Nest sections below are only for Nest. If you don't use Nest, skip them.
Eufy needs nothing more, and other cameras only need Frigate pointed at them.

## Eufy Security cameras

1. Install the [eufy_security integration](https://github.com/fuatakgun/eufy_security)
   through HACS.
2. Add it in **Settings → Devices & Services** and sign in with your Eufy
   account.

That is all. Nova needs no settings of its own. The next time Nova reloads,
it finds every Eufy camera and starts watching these sensors directly:
`ringing`, `stranger_person_detected`, `person_detected`,
`motion_detection_type_vehicle`, `pet_detected`, `package_delivered`,
`package_stranded` and `package_taken`. All of this uses the camera's own
detection, with no vision model cost for routine events.

If you add a camera to Eufy after Nova has started, reload Nova to pick it
up. You don't need to restart Home Assistant.

## Nest cameras

Nova uses Nest cameras and doorbells through the official
[Google Nest integration](https://www.home-assistant.io/integrations/nest/).
Nova does not, and legally cannot, talk to Google's Smart Device Management
(SDM) interface with its own credentials, because Google ties SDM access to
your Google account and your Device Access project. You set this up once:

1. **Google SDM.** Create a project in the
   [Device Access Console](https://console.nest.google.com/device-access).
   Google charges a one time fee of US $5. Also create a Google Cloud project
   with the SDM API turned on and OAuth credentials.
2. **Credentials in Home Assistant.** Add your OAuth client ID and secret
   under **Settings → Devices & Services → Application Credentials**. Then
   add the Google Nest integration and allow it. Your cameras and doorbell
   appear as `camera.*` entities.
3. **Nova.** There is nothing more to do. Nova spots Nest cameras by itself
   and uses the right picture source for each event: event media, waking the
   stream, or its own snapshot path. Battery and WebRTC only Nest cameras
   can't give an ordinary still picture while idle. The Nova panel handles
   this by moving to its own snapshot method, so the tile shows pictures
   instead of going blank.

## Continuous streaming for Nest cameras

This is recommended if you use Nest. Google's SDM interface hands out
WebRTC and RTSP stream addresses that expire about every five minutes, and
it won't reliably give a still picture while a camera is idle. That is fine
for the odd look, but live tiles can stall, and 24 hour recording in Frigate
struggles.

The lasting fix, and the one Nova is built around, is to restream each Nest
camera through [go2rtc](https://github.com/AlexxIT/go2rtc). go2rtc speaks
Google's SDM protocol, renews the expiring stream for you, and republishes a
steady RTSP or WebRTC feed. Home Assistant, Frigate and Nova then use it like
any local camera. If you already run Frigate, it comes with its own go2rtc.
That is the other reason Frigate is the recommended backbone: it is both
your detector and your Nest restreamer.

### 1. Point go2rtc at your Nest account

In your go2rtc (or Frigate) settings, add a `nest:` source for each camera.
You need five values, all from the Device Access setup above:

```yaml
go2rtc:
  streams:
    bedroom2_restream:
      - "nest:?client_id=CLIENT_ID&client_secret=CLIENT_SECRET&refresh_token=REFRESH_TOKEN&project_id=DEVICE_ACCESS_PROJECT_ID&device_id=DEVICE_ID"
    front_doorbell_restream:
      - "nest:?client_id=CLIENT_ID&client_secret=CLIENT_SECRET&refresh_token=REFRESH_TOKEN&project_id=DEVICE_ACCESS_PROJECT_ID&device_id=DOORBELL_DEVICE_ID"
```

- `client_id` and `client_secret`: the same OAuth pair you added under
  Application Credentials.
- `project_id`: the Device Access Console project ID, not the Google Cloud
  project.
- `refresh_token`: from the Nest integration's saved settings. In
  **Settings → Apps → File editor** (or over SSH), open
  `.storage/core.config_entries`, find the `nest` entry and copy its
  `refresh_token`.
- `device_id`: easiest through the go2rtc web page, which Frigate shows on
  port `1984`. Choose Add, then nest, enter the other four values, and it
  lists your devices with their IDs. Copy the one you want.

### 2. Add the restreams to Frigate (optional)

If you want continuous recording and object detection, add each
`*_restream` as a Frigate camera and turn on `detect` and `record`. Frigate's
own detection of people and packages is more reliable than a vision model
guessing, and Nova uses Frigate's snapshots directly.

### 3. Tell Nova to take pictures from the restreams

Restart Home Assistant so the new `camera.*_restream` entities exist. Then
map each Nest camera to its restream in Nova's settings with
`camera_overrides`. The Nest entity keeps its name, chips and doorbell
events, while every picture comes from the steady restream:

```json
{
  "camera_overrides": {
    "camera.bedroom2_camera": "camera.bedroom2_restream",
    "camera.front_doorbell": "camera.front_doorbell_restream"
  }
}
```

This goes in `/config/nova/config.json`. Merge it into the existing settings
rather than replacing the file. Nova checks it when it loads, so a typo is
set aside with a notification instead of breaking the panel. Then open
**Camera Watch → DIAG** on that camera. It should show
`override → camera.bedroom2_restream` and a healthy full size picture, not a
blank or black tile.

go2rtc's Nest source is made by a third party, and Google sometimes changes
how its sign in works. If a restream drops, Nova goes back to the original
Nest entity by itself. At worst you get the behaviour you had before the
restream, never worse.

## See also

- [Cameras and faces](cameras-and-faces.md): what Nova does with your cameras
- [Safety and security](safety-and-security.md): intrusion and camera confirmation
- [Settings reference](settings-reference.md): every setting, including the camera ones
