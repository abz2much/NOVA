# Nova Getting Started

Nova is a Home Assistant integration. It runs inside Home Assistant, talks
through your voice pipeline, and keeps an eye on the house. This guide covers
installing it, choosing AI models, where each setting lives, and a short tour
of the panel. Each main feature has its own guide, listed at the end.

## What it needs

- [ ] **Home Assistant 2024.10.0 or newer**, with [HACS](https://hacs.xyz).
- [ ] **At least one AI provider**: a key for Groq, Anthropic, OpenAI or
  Gemini, or the address of an Ollama server on your network. You can use
  more than one.
- [ ] **For voice:** Home Assistant OS or Supervised installs the voice add
  ons for you. On Container or Core you add Piper, Whisper and openWakeWord
  yourself. See [Voice and speakers](voice-and-speakers.md).

Cameras, faces, energy and the rest are optional. Add them later.

## Installing

1. In HACS, add `https://github.com/abz2much/NOVA` as a custom repository with
   the category **Integration**, install "Nova AI Assistant", and restart
   Home Assistant.
2. Go to **Settings → Devices & Services → Add Integration → Nova**.
3. **Set up Nova.** Enter a key for each cloud provider you use (Groq API key,
   Anthropic API key, OpenAI API key, Gemini API key), your Ollama address,
   or both.
4. **Choose a provider for each role.** Nova has five roles: Conversation,
   Classifier, Reasoning, Camera reasoning and Vision. Vision also offers
   "Not now (set up later)".
5. **Choose a model for each role.** Nova fills in a sensible choice and tests
   each one, including a small test picture for Vision.

Keys are saved in Home Assistant's `secrets.yaml`, one per provider. If Nova
finds an old Nova `config.json`, it imports it and skips these screens.

When setup finishes, a "Nova is ready" notification lists anything that needs
attention. Nova also appears in the sidebar.

After an update, refresh the browser with `Ctrl+Shift+R` so the new panel
loads.

## The AI roles

- **Conversation:** talks to you and uses Nova's tools.
- **Classifier:** a cheap first look at each home event.
- **Reasoning:** the deeper look when an event might matter.
- **Camera reasoning:** text only reasoning about what cameras saw.
- **Vision:** looks at camera pictures. It needs a model whose provider
  reports image support. Any provider works, including a local Ollama vision
  model.

To change them later, use the panel's **Settings → AI Models** card. It has
profiles (Hybrid and Local Text move background work to Ollama), a separate
OpenAI-compatible endpoint, and an Ollama context length. Changes are staged
until you press **Apply**. Nova checks them, saves them and reloads itself.

## Where settings live

There are two places.

- **The Nova panel, Settings tab.** Most day to day settings, in cards grouped
  by task, with a search box.
- **Configure.** In **Settings → Devices & Services → Nova → Configure**:
  - **Core:** the persona, directive, "Address me as", and "Enable home
    control".
  - **Routing:** bedroom areas, ground floor areas, the broadcast speaker
    group and the phone notify service.
  - **Observer:** observer mode, its model tiers and quiet hours.
  - **Credentials:** add or clear each provider's key.
  - **Identity:** who is speaking. See [Voice recognition](voice-recognition.md).
  - **Email:** the read only inbox. See [Memory and knowledge](memory-and-knowledge.md).

A few advanced settings have no control in either place and are only set in
`/config/nova/config.json`. The guides say when that is the case.

## A tour of the panel

- **Command Center.** The animated core, your areas with live sensors and a
  light toggle, Cognitive Core, Goals, Activity, Quick Actions, Operational
  Mode, a Muted card, and camera snapshots on request.
- **Residence.** A 3D view of the house built from your floor plan, with door
  mapping and live room presence.
- **Intrusion.** The intrusion status and the Intrusion Log.
- **Faces.** The faces recently named and your list of residents. See
  [Cameras and faces](cameras-and-faces.md).
- **Suggestions.** Learned Opportunities, automations Created by Nova, and
  your Existing Home Assistant Automations. See
  [Routines and suggestions](routines-and-suggestions.md).
- **Settings.** The setting cards.
- **Logs.** System Log, Decisions, Spoken History and Actions.
- **Diagnostics.** Core services, Setup Doctor, Provider Activity, and HOMER,
  a read only helper that looks into "why is this broken" questions.
- **Memory.** What Nova Knows, facts Pending Confirmation, Relations and
  Person Routines.
- **Energy.** See [Energy](energy.md).

## What it will never do on its own

- Install an automation without your approval.
- Unlock a door or open a garage door because of a voice command alone. That
  always needs a tap on your phone.
- Replace a model you saved because a provider's model list did not show it.

## Troubleshooting

| You see | What to do |
|---|---|
| A provider fails its test during setup | Check the key, or that the Ollama address answers from Home Assistant. You can pick another model or provider on the same screen. |
| "Nova is ready" lists problems | Each line has a suggested fix. Run the checks again in Diagnostics, Setup Doctor. |
| The panel looks old after an update | Refresh with `Ctrl+Shift+R`. |
| Vision is "Not now" | Add a vision capable model later in Settings → AI Models. |

## The feature guides

- [Safety and security](safety-and-security.md)
- [Cameras and faces](cameras-and-faces.md)
- [Weather warnings](weather-warnings.md)
- [Voice and speakers](voice-and-speakers.md)
- [Presence and alerts](presence-and-alerts.md)
- [Routines and suggestions](routines-and-suggestions.md)
- [Memory and knowledge](memory-and-knowledge.md)
- [Voice recognition](voice-recognition.md)
- [Energy](energy.md)
