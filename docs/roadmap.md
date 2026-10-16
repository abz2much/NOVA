# Roadmap

This page lists ideas that might be added to Nova one day, for anyone curious
about where it could go. These are ideas, not promises. Unless it says
otherwise, none of them is built, and none is scheduled.

## Voice ID: done, with two things left

Nova Voice ID is built (8.35.0 to 8.39.0): Nova's own speech to text, voice
results in the logs, recording and deleting voices, telling who is speaking,
and learning from everyday use. See [Voice ID](voice-and-speakers.md#voice-id).
What's left:

- **Tuning the thresholds from real scores.** The app's `t_known`,
  `t_maybe`, `m_gap` and speech length settings still have their starting
  values. They should be set from the VOICE lines of real households,
  including scores from other people's voices, before more homes rely on
  them.
- **The empty reply after a mic reopen.** When Nova reopens a satellite's
  microphone for a follow up, the answer from a real satellite sometimes
  arrives empty. That is why voice recording asks for the wake word each
  time instead of keeping the mic open.

## Driving mode

Sending travel reminders, briefings and alerts a different way while you are
away from home, for example through an Android Auto connection.

Why it is on hold: there is no current need for it.

## A specialist sub-agent for automations

A second sub-agent that could act on devices.

Why it is on hold: Nova's main agent already controls devices directly. A
second path that can act would widen the area that has to be kept safe,
without a clear benefit today.

## Speech adjusted to distance

Changing a speaker's volume depending on how close a person is to it.

Why it is on hold: it needs a suitable mmWave sensor placed relative to each
speaker, and it would behave unevenly in any room without one.

## See also

- [How Nova works](how-nova-works.md): what Nova does today
- [Phase 8: work left for later](phase8-deferred.md): the independent rewrite of Nova's code, and what is left of it. This is housekeeping, not a feature idea
- [CHANGELOG](../CHANGELOG.md): what has changed
