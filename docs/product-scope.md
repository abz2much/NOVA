# What Nova includes

Nova is a voice and text assistant for Home Assistant. It answers questions
about the home, carries out commands, watches for safety problems and tells
people what they need to know at the right time.

This page lists what every install gets and what is optional. It describes
Nova for any home. Your own setup decides which optional features you use.

## Core: on for every install

Core features work as soon as Nova is installed with an AI provider. They
need nothing else.

| Feature | Notes |
| --- | --- |
| Conversation, voice and text | Needs an AI provider. |
| Local commands and status answers | These work offline through the Local Mind. |
| Confirmations and "repeat that" | |
| Voice phrases | These do nothing until you save a phrase. |
| Safety alarms | Smoke, carbon monoxide, gas and water leak sensors are announced everywhere, quiet hours included, and pushed to phones. Freeze warnings, intrusion checks, lockdown and alarm sync run too. None of this needs the Observer. |
| Sentinel | Warns about doors, windows and locks left open. |
| Presence, quiet hours, sleep and modes | |
| Reminders and briefings | Briefings stay off until you pick a time. |
| Calendar reader | Reads Home Assistant calendars. |
| Official weather warnings | From your country's source. Met Éireann is the only one built in today. |
| Root cause ("why did that happen") | |
| Suggestions and learning | Nova learns only from what people confirm. |
| Goals and follow-ups | |
| Decision record, action log, setup doctor and service health | |
| Memory and knowledge facts | |

## Optional: off until set up

An optional feature does nothing until you set it up, and the AI is not
offered its tools before then.

| Feature | How to set it up |
| --- | --- |
| Email | Turn on email and give an IMAP server and login (Configure → Email). |
| Wellbeing | Turn on wellbeing and add wearable sensors. It needs a local Ollama provider. |
| Faces | Choose a face recognition source (Settings → Cameras). It needs MQTT with Frigate or Double Take. |
| Cameras | Add camera entities to Home Assistant. Package Watch is a separate switch (Settings → Cameras). |
| Energy | Add power or energy sensors to Home Assistant. |
| Host health | Turn it on in Settings → Host Health. It needs the System Monitor integration. |
| Web research | Choose DuckDuckGo or your own SearXNG as the web research backend. |
| Voice add-on install | Turn on "Install voice add-ons" in Settings → Anticipation & Memory (Home Assistant OS only). Nova installs Piper, Whisper and openWakeWord at the next restart. |
| Departure reminders | Turn on departure alerts. Travel times come from Google Maps Travel Time when it is installed, otherwise from public OpenStreetMap routing. |
| Documents and semantic search | Put files in the library folder. Semantic search also needs Ollama embeddings. |
| Extra hazard feeds | Turn on the Hazard Monitor and pick feeds: US weather, earthquakes, EONET or a custom CAP feed. |
| n8n specialists | Set a webhook URL and key. |
| Voice fingerprint and the Voice ID app | Set a speaker recognition entity or the Voice ID app address. |

## Upgrading from an older version

An install from before 8.41.0 keeps everything it was already running. Package
Watch, face recognition, departure reminders, web research and the voice
add-on install are saved as they were, so nothing turns off on upgrade.
