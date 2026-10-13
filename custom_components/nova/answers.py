"""The one list of spoken yes and no answers (8.49.0).

A held request on a speaker, a proactive offer, an announcement's feedback
window and a voice confirmation all ask a yes/no question. They used to keep
four lists that disagreed. They all read this one now.

The whole answer must be on the list, after voice_phrases.normalise (case,
punctuation, contractions, "Nova" and a trailing "please" do not matter), so
"Yes." counts and "yes I went through about five thousand" does not. A word
like "unlock it" is never on the list: a channel that wants its own action
words as a yes adds them itself (see voice_confirm and the intent router).

Stdlib only, so the intent router can load it without Home Assistant.
"""
from __future__ import annotations

from typing import Iterable, Optional

YES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "yes please", "please do",
    "do it", "go ahead", "yes go ahead", "go for it", "confirm", "confirmed",
    "affirmative", "yes do it", "yeah do it", "yes it is", "proceed",
    "do",   # "please do", once normalise drops the leading "please"
})
NO = frozenset({
    "no", "nope", "no thanks", "no thank you", "cancel", "do not", "stop",
    "leave it", "never mind", "nevermind", "negative", "not now", "no do not",
    "abort",
})


def yes_or_no(text, *, extra_yes: Iterable[str] = ()) -> Optional[str]:
    """"yes" or "no" when the whole utterance is a plain answer, else None.
    Never raises."""
    from .voice_phrases import normalise
    said = normalise(text)
    if not said:
        return None
    if said in NO:
        return "no"
    if said in YES or said in frozenset(extra_yes):
        return "yes"
    return None
