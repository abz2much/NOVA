## [8.85.10] Smoke, CO, gas and leak alarms no longer go quiet

### Fixed
- **An alarm that is already on when Nova starts is now announced.** Nova announced a smoke, carbon monoxide, gas or leak alarm only at the moment the sensor turned on, so one already on when Nova started or restarted was never mentioned. It is now announced once, as a critical alert, saying it is still active.
- **An alarm that stays on is repeated.** Every 5 minutes while the sensor stays on, up to 6 times (about 30 minutes), as critical alerts, then Nova stops. Before, it spoke once and never again.
- **Nova says when an alarm has cleared.** When the sensor goes back to a normal reading after being on for at least a minute, you get one message, pushed to your phones and spoken unless the house is asleep. A sensor that goes unavailable is dropped quietly, because a dead battery is not an all clear. Your own entity exclusions still apply.

**Good to know:** a faulty detector stuck on will speak once at start, up to 6 more times in the first half hour, and once more after each restart or reload of Nova.

## [8.85.9] Safer backups, and fewer phantom people at night

### Fixed
- **A backup no longer copies Nova's databases while they may be changing.** Nova's databases use write-ahead logging, and a backup that copied the files as they stood could catch one half written, and copied the `-wal` and `-shm` files at different moments from the database itself. Each database now goes into the backup as a consistent snapshot taken with SQLite's own backup, without those side files. Anything that is not a real SQLite database is copied as it is.
- **Restoring a backup clears the leftover `-wal` and `-shm` files next to a database it replaces.** A leftover one belongs to the old database and must not be applied to the restored one.
- **Camera descriptions are told to be careful at night.** At night, and in infrared (black and white) pictures, shadows, insects close to the lens, spider webs and glare often look like people, and a person described by mistake was spoken aloud as one. Every camera description now says a person is present only when a person's shape is clearly visible, and says something is unclear when it is not sure. Whether Nova is right is still up to the model, so a wrong call can still happen.

## [8.85.8] No more doubled or lost announcements

### Fixed
- **Two alerts at the same moment are no longer both spoken.** Nova checks that it has not just said something, then takes seconds to reach the speakers, and only afterwards notes what it said. A second alert checking in that gap passed the same check, so the same thing was said twice. An alert now holds its place while it is being delivered. A place nobody gives up is dropped after a minute.
- **A reply to a speaker that fails is no longer lost.** When a voice satellite's reply was sent to a paired speaker, Nova told the satellite to stay quiet without checking that the speaker played it. If every speaker failed, nobody heard the reply. The satellite now speaks it instead. Replies sent to a speaker may take a moment longer to finish, because Nova waits to hear whether it played.
- **A doorbell check and an Eufy "delivered" event close together no longer both announce the same package.** Checks of one camera now run one at a time, so the second sees what the first recorded.

## [8.85.7] Who can search chats and test cameras

### Fixed
- **Only an admin can search Nova's conversation memory over the panel connection.** `nova/search_memory` let any signed-in user search every resident's past chats, with no limit on the length of the search or the number of results. It is now admin only, with a 200 character search and at most 20 results. The Nova panel itself never used it, so nothing in the panel changes.
- **Only an admin can run a camera's diagnostic, and only on a real camera, once a minute per camera.** Any signed-in user could run it, it accepted any entity as if it were a camera, and it can wake a camera's stream for up to 30 seconds. The Diagnose button works as before for admins, and shows a message if you press it again within a minute.

### Not changed
- **The panel's main data feed (`nova/get_panel_data`) is still open to every signed-in user.** It carries lockdown, intrusion, camera and alarm panel state. Hiding it would break the panel for household members who are not admins, so it stays an accepted risk, as decided on 13 September.

## [8.85.6] Fewer lost alerts

### Fixed
- **Weather, earthquake and natural disaster alerts are no longer repeated after every restart, and a failed send is tried again.** Nova marked each one as told when it fetched it, before sending, and only kept that in memory. A send that failed was lost for good, and a restart told you everything still active a second time. An alert is now marked told only once it was spoken or pushed, and the list is saved to `hazard_seen.json`. Earthquakes still look back one hour, so a gap longer than an hour can still miss one.
- **Tamper, safety and problem sensors are no longer lost when the AI provider is down.** They had no rule, so each one was sent to the AI to judge, and if it could not answer the event vanished. A tamper or safety sensor turning on is now flagged high urgency, and a problem sensor medium, without asking the AI.
- **The hourly limit on AI calls no longer drops entry and unlock events.** It counted every event, including the many a rule settles without any AI call, so after 30 busy events in an hour a door opening or an unlock could be dropped. It now counts, and holds back, only the events that need an AI call.

## [8.85.5] Safer doors, alarms and reminders

### Fixed
- **Releasing a door latch by voice now needs a phone tap**, the same as unlocking. The "release the latch" command (`lock.open`) was missing from the lists, so a copied voice could have opened a latched door.
- **Nova now tells you when it cannot tell which alarm is yours.** With more than one Alarmo panel and none chosen, Nova announced no alarm and did no automatic lockdown, and said nothing about it. Settings, Repairs now shows "Nova needs you to choose your home alarm" until you pick one under Settings, Security alarm in the Nova panel.
- **Only an admin can use `nova.create_automation`.** It can write lock and cover actions, and any logged in user could call it. Automations and scripts, which have no user, still work.
- **Nova's AI can no longer confirm its own memories.** It could save a fact and confirm it in the same turn, so text from a poisoned email or web page could plant a fact. Only you can confirm one, in the Memory tab. The AI can still reject one.
- **Searching Nova's memory works with apostrophes, commas and hyphens.** A search such as "Sam's dog" found nothing, with no sign that anything was wrong.
- **An alert held back while you slept no longer counts as heard.** After three days of the same alert Nova goes quiet about it. It counted a day when it decided to send, so an alert held back three nights running could go quiet without you ever hearing it. A day now counts only once the alert was delivered.
- **An alert that went quiet speaks again after 30 days.** It stayed quiet for ever, even for a real fault weeks later. It then needs three days running to go quiet again.
- **A reminder is marked done only once it was delivered.** If it reached no speaker and no phone, it was marked done anyway and lost. Nova now tries again every 5 minutes, and gives up, with a warning in the log, a day after it was due.

### Changed
- **A reminder set for when you are home now goes to your phone if nobody is home.** Before, it waited, with no message, until someone came in. A reminder no phone could take stays due and is tried again.

## [8.85.4] Nova tells you when its safety monitoring is down

### Fixed
- **A failed start of Nova's safety monitoring is no longer silent.** If the part that watches for smoke, carbon monoxide, leaks and intruders failed to start, or the link between your alarm and lockdown failed, Nova only wrote a line in the log and carried on as if all was well. Now Settings, Repairs shows "Nova's safety monitoring is not running", names what failed and why, and removes the notice by itself once it starts cleanly. After fixing the cause, reload Nova.
- **A half-started safety monitor no longer reports that it is running.** If its start failed partway, Nova still counted it as running with nothing behind it. It is now stopped cleanly.

## [8.85.3] Nova's learning history no longer grows forever

### Fixed
- **Nova's record of what happens in your home is now trimmed.** It kept every state change and every command for ever, so `patterns.db` only ever grew. The daily clean-up now removes anything older than 90 days, which is as far back as anything Nova learns from looks. **After updating:** the first clean-up deletes everything older than 90 days in one go. Conversations are still kept for 30 days.

## [8.85.2] No more endless waiting on an AI provider

### Fixed
- **A stalled AI provider no longer freezes Nova.** Calls to Groq, OpenAI, Claude and a custom endpoint had no time limit, so a provider that stopped answering could hold up chat or the reasoning loop for minutes, with two silent retries on top. Each call now gives up after 60 seconds and retries once at most. Gemini and Ollama already had limits.

## [8.85.1] Nothing left running

### Fixed
- **Nova no longer leaves timers running after it unloads.** The reminder checker, the door and window watcher and Nova's other background work are now also stopped by Home Assistant itself when Nova unloads or reloads. Before, if Nova's internal state was lost, a reload could start a second reminder checker, so a reminder could be announced twice.
- **The recent faces log is saved straight away when Nova unloads**, instead of by a timer that could fire after Nova had gone.

### Behind the scenes
- Nova's tests now run against the newest Home Assistant and the newest Anthropic, OpenAI and Groq libraries on every change. There are new tests for a leak alarm being announced in a real Home Assistant, and for each AI library answering, calling a tool and taking an image. Both kinds would have caught the HA 2026.11 problems fixed in 8.85.0 before they reached a home. A twice-weekly preview also runs the tests against the next Home Assistant beta.

## [8.85.0] Working again on Home Assistant 2026.11

### Fixed
- **Nova talks to Claude again on Home Assistant 2026.11.** HA 2026.11 moved to version 2 of the Anthropic library, which refuses the `temperature` setting before anything is sent. Every Anthropic call failed (chat, cameras, package checks, classifier, briefings), and chat only said "I'm experiencing connectivity issues with my reasoning systems". Nova now retries once without `temperature` and stops sending it on that connection.
- **Leak, smoke, carbon monoxide and gas alarms, and your alarm going off, are announced again.** HA 2026.11 refuses to start a task from a background thread, and Nova started these announcements from one, so they were silently dropped. They now go through Home Assistant's event loop.
- **Logs now say what kind of local error a provider call hit** (for example "malformed_response, TypeError"), so a library problem is easy to spot. The error's own text is still never logged.

### Changed
- **Pick your home load sensor.** Energy tab, Energy Management, Home load sensor. Nova used the first sensor named like "Home Consumption"; on some systems (EcoFlow PowerOcean cloud) that one reads 0 W all night. When you pick one, the energy status, its advice, the over peak offer and the appliance monitor all use it, and a picked sensor that stops reporting shows as no reading instead of a guess. The status line says which sensor is in use and whether it was picked or guessed.
- **The cost fields list only sensors in a currency.** A price per kWh saved there was read as today's cost (0.40 €/kWh became "€0.40 today"). Now it is ignored with a warning, and Nova uses its own estimate. A cost sensor counts when its unit is a currency on its own (€, EUR, £...) or it is `monetary` without a "per" unit. **After updating:** a cost sensor with no unit, or with a rate unit such as `GBP/kWh`, is no longer used, so the daily report switches to Nova's estimate. Check the warning under the cost fields on the Energy tab.

## [8.84.1] Alerts that never go quiet

### Fixed
- **Nova no longer stops telling you about a package.** After the same alert came up three days running, Nova said "I'll stop mentioning it" and went quiet. That also silenced the alert for a package removed while you're away, since delivered and removed share one camera.
- **The same now holds for the doorbell, camera alerts, door, window and security sensor alerts, and doors, windows and garage doors left open.** Before, only locks, covers, alarms and emergencies were safe. Any of these that had already gone quiet come back.

## [8.83.1] The circles always show

### Fixed
- **Nova's core, rings and arcs on the Command Center sometimes never appeared,** however often the page was refreshed, until you opened another tab and came back (changing the theme did that). Home Assistant can hand the panel its data before putting it on the page; the first draw then ran on a canvas that wasn't on the page yet, and the animation stopped for good. The circles now start as soon as the panel is on the page.

## [8.83.0] When the alarm goes off

### Changed
- **When your chosen alarm goes off, Nova says where and what the camera sees.** One critical alert, spoken and pushed to every phone: which sensor tripped (from Alarmo's `open_sensors`), and, if a camera covers that room on the floor plan, what Nova's vision check sees there (a person, no one, or that it couldn't tell), with the snapshot. The camera check waits at most 8 seconds, and a slow or broken camera never stops the alert. Before, Nova only said "a security alert has been triggered. Immediate attention required."
- With a security alarm chosen, other alarm panels (such as camera hubs kept in sync with it) are no longer announced separately, so one alarm is one alert.

### Fixed
- **Coming home no longer sounds like an emergency.** Nova treated the alarm's entry countdown (`pending`) as the alarm going off and announced it at critical level every time someone came home to an armed house.

## [8.82.0] Nova reads your labels

### Changed
- **Nova reads your Home Assistant labels for what a sensor is, and they beat any guess from its name.** A label counts whether it is on the entity or on its device (most homes label devices). Matched by name or id, ignoring case, spaces and hyphens.
  - **Appliance:** never a way into the house (a fridge or oven door).
  - **Entry Point:** an outside door or window, always a way in.
  - **Group:** a sensor that repeats others, such as a floor's motion group. It never counts as a way in or as someone moving. A Home Assistant group is recognised by its member list even without the label.
- **Excluded labels** (Settings, Excluded Entities) now also match a label on the entity's device.

### Fixed
- A floor or room group sensor could count as a second room, so one person in the kitchen looked like movement through two places, which is what an intrusion check looks for.

## [8.81.0] Nova trusts your alarm when you're out

### Changed
- **While the alarm is armed away or on vacation, Nova leaves motion to the alarm.** Before, Nova ran its own check next to the alarm. A motion or presence sensor still on from you walking out (a presence sensor can stay on for minutes) looked like someone in an empty, armed house. On 25 Oct the kitchen presence sensor stayed on 3 minutes after the alarm armed. Now motion alone never starts Nova's own investigation while armed away, and Nova's general alerts don't announce it either. When the alarm goes off, Nova alerts as before.
- Everything else is unchanged: armed home and night, the house asleep, everyone away with the alarm off, homes with no alarm, smoke, CO, leaks and the night sweep. An investigation already open before the alarm armed keeps running.
- This assumes your alarm (for example Alarmo) watches the sensors you care about in away mode.

## [8.80.2] Security card counts door locks only

### Fixed
- **The Security card on the Command Center counted every lock in Home Assistant**, including a thermostat's child lock, so it could show "0/2" locked with every door shut and the alarm armed. It now leaves out the locks that lockdown leaves out (Settings, Security Alarm, locks left out of lockdown), the same list the briefing and lockdown use. With no door locks left, the card shows the alarm and doors instead.

## [8.80.1] Departure reminder at midnight

### Fixed
- **A leave-time reminder could come twice around midnight.** Nova remembered "already reminded today", so for an event just after midnight the reminder given before midnight was given again once the date changed. It now remembers the event itself, so each event is reminded about once.
- Some intrusion tests failed at random. The test stand-in for Home Assistant could give two state changes the same time, so a door opened after the alarm was armed looked as if it was open when it was armed. Its clock now always moves forward. Nova's own intrusion logic is unchanged.

## [8.80.0] Light themes

### Added
- **Oriel's six light themes:** Classic (warm stone and sage, Oriel's own look), Violet Mist, Berry Sky, Quantum Ice, Pine Mist and Ember. Cards are white frosted glass over a softly shaded page, with near-black text; the theme colour marks the open tab, "on" states, links and Nova's core and rings.
- Every text and status colour stays readable in every theme (4.5:1 or better). The log colours, made for dark themes, are drawn darker in light ones.

### Changed
- Nova's dot in the top corner keeps a light highlight in every theme.
- The README's theme picture shows all twelve.

## [8.79.0] Nova's core and rings

### Changed
- **The head on the Command Center is replaced by Nova's core and rings:** a glowing core inside two turning rings, in the theme's colour, the same mark as the icon.
- **It shows Nova's mood.** At rest it turns slowly and breathes. While Nova is thinking, the rings spin fast and the core pulses. When it speaks, ripples spread from the core. When a card pops up, the rings brighten on that side. When the house is asleep, it goes dim and slow. The big arcs round it turn at the same pace.
- **It is drawn with plain canvas 2D**, so the page is lighter on a tablet and no longer needs WebGL. The 3D head and the older 2D head are removed.
- The Settings line for the pop-up cards says "beside Nova" instead of "beside the head", in all 20 languages.
- The README pictures show the new look.

### Fixed
- Three guides gave the wrong pop-up card timings; they now say a new card every 9 seconds, each shown for 8.
- A panel test for the pop-up cards could fail at random in CI when the page redrew during it. The stand-in for the 3D head it needed is gone with the head.

## [8.78.0] Oriel's themes

### Changed
- **The themes are Oriel's six dark ones:** Deep Teal (still the default), Midnight Signal, Plum Dusk, Cocoa Bark, Forest Mint and Slate Harbour. Each is built from a base and an accent colour by the same rules as Oriel, so both look like one family.
- **Cards are frosted glass** over a softly lit page, with shadows, instead of flat blocks of one colour.
- **Text is neutral.** It is no longer tinted with the theme colour, and the greeting is no longer painted in it. The accent now marks only what matters: the open tab, "on" states, links and focus, the arcs and the head.
- **Energy sits on a dark panel** on the Command Center. Solar and home keep their own colours in every theme; the battery takes the theme colour.
- The README's theme picture shows the new six.

### Removed
- **Twilight Denim, Pine Mist, Smoked Lilac and the old orange Classic.** If one of them was picked, the panel shows Deep Teal. Oriel's Classic and its light themes come in a later release.

## [8.77.1] New icon and README pictures

### Changed
- **A new icon** in the new look: a teal ball with two broken arcs, like the arcs round the head, on a dark round badge. Home Assistant and HACS show it in place of the orange ball.
- **The README shows the panel as it is now:** real screenshots of the Command Center, the Nova panel and the five colour themes, taken from a made-up test home. The three old drawings are gone.

### Fixed
- **The dock's panels scroll with a dark scrollbar** in the theme's colours, instead of the browser's bright default one.

## [8.77.0] The dock and Nova's feed

### Added
- **A bar of five buttons at the bottom of the Command Center:** Home, Nova, Areas, Goals and More. Each one but Home opens a panel that slides up over the lower part of the hero, so the head stays in view. Tap the button again, Home, Close or Esc to shut it. With "reduce motion" on, panels appear without sliding.
- **Nova's panel says what Nova did in plain sentences**, such as "I told you: the back door is open" or "I worked on your goal …". Changes it noticed and let pass become one quiet line. The panel writes these itself, so they cost no AI calls. A number on the button counts what is new since you last opened it.
- **Ask Nova** in the same panel sends a question through the Chat conversation and shows the answer there, with "Open chat" for a longer talk.
- The activity log now tells the panel what kind each entry is and whether Nova said it out loud.

### Changed
- **The long column of cards under the hero is gone.** Areas and Goals have their own panels; Operational Mode, Quick Actions, Cognitive Core, Camera Watch and Muted are under More.
- **The line under the greeting** uses the same plain sentences instead of the raw activity entry.
- The old Activity list is replaced by the Nova panel; **See everything** opens the Logs tab.

## [8.76.0] A new Command Center

### Added
- **The head is in the middle**, with three broken arcs turning slowly round it in opposite directions. They fade behind the greeting so the words stay clear, and stand still when your device asks for less motion.
- **Five live cards round the head:** Security, Who's home, Energy, Indoor and Weather. They update with the rest of the panel, and a number that changes counts up to its new value. Tap one to see more beside the head.
- **Nova's latest line** under the greeting: the newest entry from the Activity feed, which slides in when a new one arrives.

### Changed
- **Pop-up cards appear beside the head instead of covering the greeting.** When one ends it shrinks into its live card, which glows.
- **On a phone** the cards sit under the head in two columns, without the arcs.
- The guide said a pop-up card stays 15 seconds and comes every 20; it stays 8 and comes every 9, and the guide now says so.

## [8.75.1] Buttons in normal case

### Changed
- **About 60 buttons and view switches are no longer written in capitals:** Dismiss, Say hello, Open settings, Add goal, Cancel, Delete, Refresh, Send, Save, Brief me now, the Logs views and the rest. Short status badges (RUNNING, ARMED, KNOWN), ON and OFF, and the log codes in the filter row stay in capitals.
- **The welcome card's step buttons say "View"** instead of OPEN, since "Open" is also how a door's state is shown.
- Every new label is translated in all 20 languages. "Clear" now means clearing something, not a clear sky; several languages had the weather word.

### Fixed
- **A panel test for adding a resident was passing for the wrong reason.** It found the word RESIDENT in the button's own label, not in the updated row. It now checks that the row offers "Remove resident".

## [8.75.0] One font, bigger text

### Changed
- **One font, Plus Jakarta Sans, for headings, text and numbers.** The serif and the second sans font are no longer loaded, and the monospace font is kept only for automation code. Numbers line up in columns.
- **Bigger, easier text on one size scale** (12, 14, 16, 20, 24, 32, 48). Most text was 11 or 12px across 16 different sizes; now nothing is under 12px and buttons are 14px.
- **Three corner sizes** instead of fourteen, so cards, tiles and buttons match.
- **Chip buttons are no longer forced into capitals.** Mode and energy-agency chips start with a capital letter. Some button labels are still written in capitals; they change in a later release.

## [8.74.0] Colour themes

### Added
- **Pick the panel's colours in Settings → General → Theme.** Deep Teal is the new default; Twilight Denim, Pine Mist and Smoked Lilac are the other choices, and **Classic** keeps the warm ember and gold look from before. The whole panel changes at once, the hologram head included, and the head keeps running while it does.
- Every theme was worked out from one base and accent colour and checked: text, status colours and the log colours all reach 4.5:1 or better on the cards they sit on.

### Changed
- **Every colour the panel draws now comes from the theme.** Before, about 70 colours were written straight into the code; the energy flow now has its own four colours in every theme, so solar, grid, house and battery stay apart.

## [8.73.1] Floor plan editor without dragging

### Added
- **Pick an item to change it without dragging.** Tap or click a room, zone, corner, camera or device pin. A bar above the plan shows its name, arrows that move it a foot at a time, Wider, Narrower, Taller and Shorter for a room, Delete, and Open controls for a device. This is the way to delete on an iPhone, which has no right-click.
- **The plan works with the keyboard.** Tab reaches each room, zone, camera and pin, and a screen reader says its name. The arrow keys move it, Shift and an arrow move it five feet, Alt and an arrow resize a room, Delete removes it, Enter opens a device's controls and Escape lets go.
- **A Rooms list** under the plan renames a room, changes its type from a menu, or removes it. An interior door or doorway on a renamed room follows the new name.

### Changed
- **+ Add Room and + Outdoor Zone no longer ask questions in pop-ups.** They add "Room 2", "Zone 2" and so on, and put you in the new name in the Rooms list, ready to type over. The type is picked from a menu there instead of typed in.
- **Deleting a corner asks first**, and a shape always keeps three corners.
- **Five labels screen readers say are now translated**: the battery picture, Forget, and the three fields of a Memory link. Before, the string check missed labels written straight into the markup.


### Added
- **The floor plan editor works with a finger or a pen.** Drag a room, corner, camera or device pin with one finger. Pinch with two fingers to zoom, and move two fingers to pan. One finger on empty space still scrolls the page, so the plan never traps you. Before, touching the plan scrolled the page and nothing could be moved.
- **Bigger handles on a touch screen.** Corners, camera dots, pins and the resize handle catch a finger about 22px away, at any zoom.
- **+ and − zoom buttons** beside Fit.
- **"Unsaved changes."** shows beside Save Layout after an edit, and Save says "Saved." when it is done.

### Changed
- **Reset Default asks first.** Before, one click replaced your saved plan.
- **A click that barely moves is a click, not a drag.** Clicking a room no longer nudges it or redraws the plan, and tapping a pin opens its controls without moving it.
- **A drag keeps going past the edge of the plan.** Before, it stopped where the pointer left the plan.
- **Zooming in or out to the limit no longer squashes the plan.** Width and height now stop together.
- **The hint is right.** It said "double-click an edge to add a corner"; a single click on the small circle on the edge does it. A phone shows a hint for fingers instead.

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
