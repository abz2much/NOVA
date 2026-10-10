# Privacy and your data

This page is for anyone who wants to know what Nova stores, where it keeps
it, and what ever leaves your home network.

Nova is local first. Everything it learns and stores stays inside your Home
Assistant, mostly in the `nova/` folder of Home Assistant's config directory
(`/config/nova/` on Home Assistant OS and Container). Nova follows whatever
config directory Home Assistant reports, so it never writes outside it.
There is no Nova cloud and no telemetry. Nova never sends anything anywhere
on its own initiative.

## What is stored, and where

### Learned routines

- Learned behaviour lives in `patterns.db` (the state changes and commands
  Nova uses to suggest automations), the per person routines table, and the
  reasoning cache. All of it is local SQLite.
- State change records keep a limited note of what caused each change. That
  lets Nova leave out changes made by an automation you already have,
  instead of learning them as a household routine.
- Camera detections that matter (from Eufy, Frigate, Nest or Nova's own
  vision) go into the same table as a short, structured record: the kind of
  thing seen, the camera or area, the source, a confidence number only when
  the source gave one, and a resident's name only when Nova's recognition
  has a fresh, confident match for that camera. Nova never stores an image,
  a face print, the raw data from the camera system or the raw answer from a
  vision model.

### Knowledge and memory

- Curated facts live in `knowledge.db`. Conversation history and
  conversation memory are local SQLite too. Each is kept for the right
  conversation or person, not searched across everyone. You can edit and
  erase them from the panel.
- With semantic search turned on, each confirmed fact also gets a vector
  (`fact_vectors` in `knowledge.db`) so Nova can find it by meaning. The
  fact text is sent only to the Ollama server you set up, and forgetting a
  fact deletes its vector too.

### Relations

Nova keeps a small map of how things relate, for example "house member owns
bike" or "child's room adjacent_to hallway", in `knowledge.db`.

- Every relation starts as pending, whoever or whatever proposed it. Only a
  person confirming it in the Memory tab makes it live.
- Nova can't confirm a relation, and there is no tool for it to do so. It
  can only propose one, drop a pending one, and look up confirmed ones.
  Facts are different: Nova can still confirm a pending fact with you in
  conversation.
- Only confirmed relations reach the AI, as a short fenced block of up to 12
  links, or the lookup tool.
- Nothing works out relations by itself.
- The table holds at most 500 live links. At that limit new proposals are
  refused, and nothing is removed to make room.
- A relation you remove stays removed, unless you state it again yourself.

### Decisions and activity

- `decisions.db` holds the Decisions view's evidence, confidence and the
  feedback you gave.
- Provider Activity keeps daily totals: number of calls, model, token counts
  and timings. It never keeps the questions, answers, tool details, images
  or keys behind a call.

### Documents

Anything you put in `/config/nova/documents` for Nova to search, plus its
index and vectors. Nova only ever reads inside that folder.

### Scene memory

Off by default, and you choose to turn it on. When on, Nova keeps the text
of what its cameras described, never images or faces, in `scene_memory.db`,
so it can answer "where did I last see my keys?". Entries expire after 14
days by default (you can choose 1 to 90 in Settings), each camera keeps a
limited number, and Settings has a button to forget everything. Questions
you ask about a camera are never stored.

### Camera pictures

Pictures Nova looks at on request are not kept. The one exception is
confirmed intrusion snapshots: Nova keeps the last 40 for the panel's review
and labelling history. They are stored privately under `/config/nova/`,
never in a folder a web browser can reach, and the panel only gets them over
its own signed in connection. A photo in a push notification is a short
lived, cryptographically signed copy that expires and deletes itself.
Recording is Frigate's job, and under your control.

### Faces

The Faces tab's list of residents is saved in `nova/face_roster.json`, names
only, and readable only by Home Assistant's own user. Nova has no face
engine of its own and keeps no face images.

### Wearables

Off by default. When you turn it on, Nova reads the wearable sensors Home
Assistant already has, for comfort context, such as being quieter when a
sleep sensor says you are resting. This context only reaches the AI when you
use a local Ollama provider. With a cloud provider (Groq, OpenAI, Anthropic
or Gemini) it is held back completely, so heart rate and sleep readings
never leave your network. It is not medical: Nova never diagnoses, raises
alarms about, or interprets a reading clinically. Anything worrying is for
your own device or a medical professional.

### Keys and files

- AI provider keys live in Home Assistant's `secrets.yaml`, one per
  provider, never in plain panel settings. Any older plain text key is moved
  there safely when you upgrade, and only removed from the old place once
  the move is confirmed.
- Nova's config file, the copy of `secrets.yaml` it backs up, and its state
  backups are readable only by Home Assistant's own user.
- Errors shown to the AI and the panel carry only the type of error, not
  file paths or library details. Diagnostics downloads remove passwords,
  tokens and webhook addresses from every value, including error lines, the
  log tails and service health.

## What leaves your network

Only what the features you turn on need:

- The AI and vision providers you chose, for the messages Nova sends them.
- The text to speech service you chose, if it isn't the local Piper voice.
- Only if you turn them on:
  - web research, through DuckDuckGo or your own SearXNG;
  - read only email, over IMAP;
  - hazard feeds: Met Éireann, a CAP feed you set, or the USGS, NWS and
    NASA EONET feeds;
  - Google Maps travel times for leave alerts, through Home Assistant's own
    integration;
  - the optional n8n specialist bridges.

Each one is used only when the feature that needs it runs. Home Assistant,
not Nova, holds the keys for the integrations it uses.

## What never leaves

- If you run everything through Ollama and a local text to speech voice,
  nothing leaves your own network.
- Wearable and sleep data never go to a cloud AI.
- Camera images are never stored, apart from confirmed intrusion snapshots,
  which stay on your machine.
- Nova never sends anything to a Nova service, because there isn't one.

## See also

- [Memory and knowledge](memory-and-knowledge.md): facts, relations and documents
- [Cameras and faces](cameras-and-faces.md): scene memory and faces
- [How Nova works](how-nova-works.md): storage and security in more detail
