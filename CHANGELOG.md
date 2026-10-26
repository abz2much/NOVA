## [8.72.1] Better on a phone

### Changed
- **The tabs are one row on a phone** that you can swipe sideways, instead of three rows. The open tab is moved into view.
- **Lockdown sits in the top bar**, beside the Nova name, instead of on a row of its own.
- **The head comes first.** The welcome card now sits under the head, so a phone shows live status straight away. On a phone the head starts about a third of the way down the first screen; before, it was below the first screen.
- **Appliance rows stack on a phone**: the name on its own row, then type, entity and watts.
- **Fields have labels you can see.** Goals, Memory's Teach form, Faces, appliances, camera names and the exit door name show their label above the field instead of only as a hint that disappears when you type. Fields under a section heading, like the exclusion lists, are named by that heading. Search boxes and the chat box keep their hint.

## [8.72.0] The panel tells you when something fails

### Added
- **A failure is shown on screen.** Saving a setting, a Quick Action, a lockdown change, a camera, document or memory action and the others now show "That didn't work:" and the reason at the bottom of the panel. Before, most of them only wrote to the browser console. Screen readers read the message out.
- **Quick Actions and service tests say "Done."** when they ran, and their button waits while they run.
- **An entity id that is not in the list says so**, instead of nothing happening. So does a document over 25 MB, and Brief me now with no announcement speakers chosen.
- **Call Off, Forget and removing a link ask first.**

### Changed
- **Destructive buttons are red**: Call Off, Forget, Reject, Remove, Forget everything, Clear credential, Disable all cameras and Reset Default.
- **"Blanket shush is on" is the first thing on the Command Center**, above the head, instead of below Quick Actions. It is read out once, not on every refresh.

## [8.71.1] Easier to read and tap

### Changed
- **Faint text is easier to read.** Timestamps, hints and OFF buttons now meet the 4.5:1 contrast standard on every background. Selected chips use dark text on orange, like the selected tab.
- **No text smaller than 11px**, and descriptions and help text are 12px.
- **Bigger buttons.** Every button is at least 24px. On a touch screen every button, menu and field is at least 44px, including the small ✕ buttons.

## [8.71.0] Panel works with a screen reader

### Added
- **Screen readers can find their way round the panel.** Panel titles are headings. Every field is named by its label. Every ON/OFF button says which setting it is and whether it is on. The current tab is marked. Chat replies are read out as they arrive.
- **A visible focus ring** shows where you are when you use the keyboard. Disabled buttons now look disabled.
- **Room feature icons are drawn icons with names** (Speaker, Lock, Water leak…) instead of emoji. A room's light button names the room, and each camera and goal button names its camera or goal.
- **Decisions in Logs open from the keyboard.** The detail scrolls into view and takes focus; Close takes you back to the row.

## [8.70.3] Keyboard focus kept

### Fixed
- **Keyboard focus stays where you were.** Saving a setting, switching tabs, or the dashboard's 20 second refresh used to drop focus to the top of the page. Focus now goes back to the same control, and the search box keeps your caret where it was.

## [8.70.2] Panel fixes

### Fixed
- **The Intrusion Log loads when you open the tab.** Before, it showed "Loading…" until you pressed Refresh.
- **Opening Settings no longer redraws the page four extra times.** The card statuses it reads on the first visit now redraw it once, together, so text you start typing is not wiped.
- **The panel no longer redraws itself once for no reason on first load in English.** On the Command Center this also stops the head restarting.
- **Long help text sits under its setting.** "Nova speaks", Garage and Basement no longer squeeze their help into a narrow column beside the menu.
- **Setup checks on the welcome card read on one line**: the name of the check, then what is wrong.

## [8.70.1] Camera summaries kept through a restart

### Fixed
- **Camera summaries survive a restart.** Nova saves the newest 20 and loads them back at start, so the Camera summary entity and the briefing are no longer blank after Home Assistant restarts.

## [8.70.0] Nova on your dashboards

### Added
- **Nova entities.** The Nova device now has Status, Quiet, Nap ends, Lockdown, Last announcement, Next reminder, Briefing, Camera summary, Energy note and Suggestions, so any dashboard can show what Nova is doing. They update when something changes; only Energy note refreshes on a timer, every 30 minutes.
- **Timed shush.** `nova.shush` takes `minutes` (1 to 1440) and lifts itself when the time is up, even across a restart.
- **Suggestion services.** `nova.suggestion_accept` and `nova.suggestion_dismiss` act on a suggestion by its id, the same as the panel. Admin users only.

### Changed
- **The latest briefing is kept**, so the Briefing entity can show its text.

## [8.69.1] Open windows while armed home or night

### Fixed
- **Open windows no longer count as a break in while armed home or night.** A door or window only backs up an intrusion if it opened after the alarm armed and, with ground floor areas set, is on the ground floor. A window left open before arming stays ignored even if its sensor drops out and comes back. Armed away and vacation work exactly as before.
- **No "open while the alarm is armed" message when armed home or night.** It now only comes when armed away or on vacation.

## [8.69.0] Faster, quieter start up

### Fixed
- **No more blocking warning at start up.** Nova reads its saved lockdown state without holding up Home Assistant. Lockdown works exactly as before.
- **Memory database connections are always closed**, even after an error.

### Changed
- **Nothing you will notice.** The test suite and contributor guide were tidied.

## [8.68.0] Internal tidy

### Fixed
- **Camera analysis help text.** The Frames field of nova.analyze_camera said 2 to 4 frames; it takes up to 6, and now says so.

### Changed
- **Nothing else you will notice.** Service definitions, blueprints and project tooling were tidied. Your services, blueprints and automations work exactly as before. The audit now says when its names check is skipped, and the issue forms ask for what helps fix a problem.

## [8.67.0] Floor plan editor fixes

### Fixed
- **Opening width and camera range.** Changing an opening's width or a camera's range in the floor plan editor did nothing. Both now save, in feet or metres.
- **Device pins kept.** Removing a camera or an opening also removed the first device pin on that floor. It no longer does.
- **Empty or broken floors.** A layout file with a broken floor is now refused instead of breaking the editor. A floor with no rooms keeps its camera and pin controls, and a property line drawn on it now appears next to the plan.
- **Memory tab.** A fact taught before the list had loaded was saved but not shown. A person's routines heading with & or quotes in the name now shows correctly.

### Changed
- **Nothing else you will notice.** Parts of the panel were tidied, and some Italian and Portuguese wording reads more naturally. The CLEAR button now reads as an action in both. The floor plan, memory and intrusion views work as before.

## [8.66.2] Internal tidy

### Changed
- **Nothing you will notice.** Sixty test files were tidied and now check more cases. Nova works exactly as before.

## [8.66.1] Internal tidy

### Changed
- **Nothing you will notice.** Twenty test files were tidied and now check more cases. Nova works exactly as before.

## [8.66.0] No false error from the appliance monitor when Nova stops

### Fixed
- **No false error in the Home Assistant log.** If you saved your appliances in the Energy tab and the monitor then had nothing to watch, the next time Nova was reloaded with the Observer on, Home Assistant logged "Unable to remove unknown job listener" with a traceback. Appliance monitoring itself was not affected. The monitor now forgets a listener once it has removed it, so the error no longer appears.

### Changed
- **Nothing you will notice.** Internal code tidy in five files. Voice recognition, cameras, alerts, appliance monitoring and offline decisions work exactly as before.

## [8.65.2] Internal tidy

### Changed
- **Nothing you will notice.** Internal code tidy in eighteen files. Alerts, announcements, voice, the panel and the AI tools work exactly as before.

## [8.65.1] Internal tidy, and a fix for the outdoor temperature

### Changed
- **Nothing you will notice.** Internal code tidy in twelve files. Chat, voice, memory, automations and the AI tools work exactly as before.

### Fixed
- **Outdoor temperature.** If the first outdoor temperature sensor had no reading (unavailable or unknown), Nova stopped looking and acted as if there was no outdoor temperature, even when another outdoor sensor was fine. It now tries the next one. This feeds the freeze risk alert and the door left open temperature check.

## [8.65.0] Change Nova's banter by voice

### Added
- **Say "full banter", "dry banter" or "plain banter".** Nova changes its banter level and answers in kind, for example "Full banter, sir. Do try to keep up." It works by voice and in the Chat tab, with or without "Nova" in front, and needs no AI, so it also works offline.
- **It saves the same setting as the panel.** The Banter level under Settings changes too, and the choice survives a restart.
- **A mode with its own tone still wins.** In movie mode, for example, Nova saves your choice and says "Saved, sir. Movie mode keeps me on plain banter until it ends." Your level applies once the mode ends.
- **Unchanged:** urgent and serious messages are always plain, whatever the level.

## [8.64.0] The Command Center rotation is faster and its cards are easier to read

### Changed
- **The small grey text on the cards is easier to read.** The caption (for example LOCKED), the row labels (Security alarm, Doors) and the small time line now use a lighter grey (contrast 5.8, it was 3.1) and are slightly bigger (11.5 and 13 pixels, were 10.5 and 12). The bright figures are unchanged, and so is the rest of the panel.
- **Each step of the rotation now lasts 8 seconds, not 15.** A card stays on for 8 seconds and the next step follows one second later (the cycle is 9 seconds, it was 16). The greeting steps move at the same pace. The first card still comes about 15 seconds after the page opens.

## [8.63.0] The Command Center greets you in different ways

### Changed
- **The hero word is no longer always "Hello."** It now picks from a short list: "Hello.", "Hi, I'm Nova.", "Nova here.", "At your service.", "All good here.", and "Good morning.", "Good afternoon." or "Good evening." at the right hours. It never picks the same one twice in a row. "Thinking…" and "Goodnight." are unchanged.
- **The greeting is now part of the rotation.** Before, the head sat alone for 5 seconds between cards and said "Hello." Now the rotation has a greeting step in front of every third card, and a card is followed by the next step after one second (the cycle is 16 seconds, a card stays 15).
- **Translations:** the new greetings are translated in the eleven complete panel languages. In the other nine they stay English for now, like "Hello." and "Goodnight."
- **Unchanged:** the "Cards on the Command Center" setting still turns the rotation off. With it off, the hero keeps one greeting, picked when the page opens.

## [8.62.0] Tool and service errors no longer pass raw error text on

### Changed
- **The AI's tool replies and a few service results now give a short safe error.** When a tool failed, the reply carried the raw error text, which can include file paths, host names or library detail. It now says the error type, for example "KeyError (details are in the Home Assistant log)", and the full error goes to the log. Home Assistant's own error messages are kept as they are, because Home Assistant gives the AI the same text for its own tools. Nova's own fixed messages (for example from the automation installer) are kept too.
- **Where this applies:** all the agent tools (control, home, cameras, memory, environment, safety modes, communications, automation, diagnostics) and the results of the camera analysis, goals, follow ups, agenda, conversation summary, voice confirmation test and automation install.
- **Not changed:** the error detail on the panel's health pages (setup health, service health, speech to text, embeddings). It is short, already hides your host address, and tells you why something is down.

## [8.61.0] The unused local voice command code is removed

### Removed
- **Code left over from `nova.speak` and `nova.process_intent` is gone.** Those services were removed in 8.60.0, and nothing else used the local intent router, the entity lock registry or the state ledger, so they are deleted. The state ledger's startup check goes too: it only ever tracked actions the router took, and the 8.60.0 start already cleared anything left over. Nothing about the audit, your spoken confirmations or continued conversations changes.
- **Your `state_ledger.jsonl` is left on disk.** Nothing reads it any more. It is still included in Nova backups.

## [8.60.0] nova.speak and nova.process_intent are removed

### Removed
- **`nova.speak` and `nova.process_intent` are gone.** They were retired in 8.40.0 and each call raised a Repairs notice. Use `tts.speak` to say something and `conversation.process` to run a phrase. The yes/no window that `nova.speak` could open went with them. Your spoken confirmations (locks, the Executive Assistant, Marketing) are separate and are unchanged, and so are continued conversations.
- **The infrastructure audit is unchanged.** It still speaks its alerts the same way. A few internal pieces that only these two services used (the local intent router, its entity lock registry and the startup hold for `nova.speak` calls) are now unused and will go in a later release.

## Earlier versions

Releases before 8.60.0 are in [docs/changelog-archive.md](docs/changelog-archive.md).
