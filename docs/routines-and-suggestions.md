# Nova Routines and Suggestions

Nova learns the household's habits and uses them in a few ways: leave alerts
for calendar events, reminders about usual routines, small offers such as
turning a light on, suggested automations, daily briefings, and modes that
change how chatty it is.

## What it does

- **Leave alerts.** For an upcoming calendar event with a real place, one
  heads up says when to leave on foot, by public transport and by car.
- **Routine and presence reminders.** "You usually turn the Kitchen Light on
  around now." "You usually leave around 12:00." Each is said once, and only
  while that person is home.
- **Offers.** It is dark in a room with someone in it: "Shall I turn the
  lights on?" A light has been on for 90 minutes in an empty room: "Shall I
  turn it off?" The heating or cooling is running while nobody is home:
  "Would you like me to set it back to save energy?"
- **Suggested automations.** Patterns Nova sees become suggestions on the
  **Suggestions** tab, for you to approve or dismiss.
- **Briefings.** A morning and an evening summary, and a welcome briefing
  when someone gets home.
- **Modes.** Party, movie, guest, away and focus change how much Nova speaks
  and offers. There is also a lab mode for chosen rooms, and you can add
  modes of your own.

## What it needs

- [ ] **Calendars in Home Assistant**, for leave alerts. Each event needs a
  location that is a real place.
- [ ] **Optional: the Google Maps Travel Time integration**, for walking and
  public transport times. Driving falls back to a travel sensor, then an open
  source router, then the departure lead.
- [ ] **Light sensors (lux) in rooms**, for the dark room offer.
- [ ] **A thermostat with an eco preset**, for the heating offer to ask.
- [ ] **Time.** Routines and suggestions need several days of history.

## Leave alerts

- Nova looks at events starting in the next three hours, and checks every 30
  seconds.
- Only events with a real place count. An event with no location, a meeting
  link, "Online", "Zoom" or "Teams", a phone number, "TBC", or your own home
  gets no alert. All day events never do.
- Journeys start from home while anyone is home. Only when nobody is home does
  Nova use the first person with a position.
- After the first alert, Nova reminds you again at a later leave time only if
  everyone who was home is still home.
- Nova makes at most 40 Google lookups a day and reuses each answer for a
  while.

## Offers and earned trust

- Nova asks one question at a time and waits for a yes or no. The same offer
  is not repeated for 30 minutes.
- An offer is only made when Nova can really do it. A thermostat with no eco
  preset gets a plain "the heating is running but no one's home" with no
  question. A dark room with no working light gets nothing.
- **Earned trust.** After you say yes to the same offer 3 times, with Nova at
  least 80% confident, it may do that one thing by itself next time and tell
  you it did.
- A mode such as party, movie or away can switch offers off.

## Suggested automations

- Nova learns from lights, locks, thermostats and similar devices. Doors and
  windows, presence and arrivals, button presses, and motion are opt in.
- A suggestion needs real evidence: several separate days, timing that
  clusters, recent activity, and for "after A, do B", A followed by B most of
  the time. Suggestions only link things in the same area, arrivals and
  departures excepted.
- Nova compares each suggestion with the automations Home Assistant already
  has. Exact duplicates are hidden, and likely overlaps are flagged.
- When you approve one, Nova checks it with Home Assistant's own validator,
  writes it safely, and confirms it loaded. The Suggestions tab then shows
  whether it actually runs.

## What it will never do on its own

- Install an automation without your approval.
- Act on an offer before it has earned your trust, or offer what it cannot
  do.
- Nag you back toward a less secure habit. A window that is usually open but
  is now closed stays closed and silent.
- Give a leave alert for an online event.

## Limits

- Leave alerts need the event's location. Nova does not look one up from the
  title.
- Routines need days of history, so a new install is quiet at first.
- Google lookups are capped at 40 a day. After that, driving uses the
  fallbacks and walking and public transport are left out.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| No leave alert for an event | It has no real place, is all day, starts more than three hours ahead, or its calendar is excluded | Add an address as the event's location. |
| A leave alert for a calendar you do not care about | That calendar is included | Add it under "Calendars that never trigger a leave alert". |
| "I couldn't work out the travel time" | A route lookup failed | Nova used the departure lead instead. Check the Google integration. |
| No suggestions yet | Not enough history | Wait a few days, or opt in more signals in **Settings → Routine Learning**. |

## Settings

Panel, **Settings → Anticipation & Memory**:

| Setting | What it does |
|---|---|
| Departure alerts | Leave alerts for calendar events. On by default. |
| Leave alerts: walking, public transport, driving | Which ways of travelling to include. Walking and public transport need Google Maps Travel Time. |
| Time journeys with Google Maps Travel Time | Use that integration when it is set up. |
| Departure lead (min) | The lead used when no travel time is known. 30 by default. |
| Origin tracker | Where journeys start. Blank: home while anyone is home. |
| OSRM URL, Travel sensor | Your own routing server, or a sensor giving the travel time in minutes. |
| Calendars that never trigger a leave alert | For example birthdays or holidays. |
| Routine alerts | The routine reminders above. On by default. |
| Leave reminder (min) | How long before a person's usual departure to remind them. 15 by default. |
| Review suggestions with AI | Checks each new suggestion with the Suggestion Review model first. It can only reject. Off by default. |
| Adaptive suggestions | Adjust the suggestion bar from past feedback. Off by default. |

Panel, **Settings → Routine Learning**: "Learn doors & windows", "Learn
presence & arrivals" and "Learn button & remote presses" opt those signals
in. "Learn motion/presence triggers" is in Anticipation & Memory.

Panel, **Settings → Briefings**: **Morning** and **Evening** times (07:30 and
19:30 by default), each on or off, **Only when someone's home**, and the
**Front door** whose opening starts a welcome briefing.

Panel, **Command Center → Operational Mode**: the active mode, and **Auto
(follow occupancy)**, which switches between normal and away as people come
and go. Party, movie and focus only allow critical announcements. Away keeps
security watching but switches convenience offers off.
