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

_YES_EN = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "yes please", "please do",
    "do it", "go ahead", "yes go ahead", "go for it", "confirm", "confirmed",
    "affirmative", "yes do it", "yeah do it", "yes it is", "proceed",
    "do",   # "please do", once normalise drops the leading "please"
}
_NO_EN = {
    "no", "nope", "no thanks", "no thank you", "cancel", "do not", "stop",
    "leave it", "never mind", "nevermind", "negative", "not now", "no do not",
    "abort",
}

# The same answers in the languages Nova writes its alerts in. The whole
# answer still has to be on the list, so "oui mais pas maintenant" is not a
# yes. Accented and plain spellings are both listed because speech to text
# gives either. "no" is already English and Spanish and Italian, and the
# Polish "no" (a casual yes) is left as no, the safe reading.
_YES_OTHER = {
    # French
    "oui", "ouais", "ouaip", "bien sûr", "bien sur", "daccord", "vas y",
    "allez y", "fais le", "faites le", "confirme", "confirmé", "confirmer",
    "affirmatif",
    # German
    "ja", "jawohl", "klar", "gern", "gerne", "mach es", "mach das", "los",
    "einverstanden", "bestätige", "bestätigt", "bestatigt",
    # Spanish
    "sí", "si", "claro", "vale", "de acuerdo", "adelante", "hazlo",
    "por supuesto", "confirma", "confirmado", "afirmativo",
    # Italian
    "sì", "certo", "certamente", "va bene", "fallo", "vai", "avanti",
    "conferma", "confermo", "confermato", "affermativo", "daccordo",
    # Dutch
    "jawel", "jazeker", "oké", "doe maar", "ga je gang", "doe het",
    "bevestig", "bevestigd", "zeker",
    # Portuguese
    "sim", "pode", "pode fazer", "faça", "faca", "vá em frente",
    "va em frente", "com certeza", "confirmo",
    # Polish
    "tak", "oczywiście", "oczywiscie", "dobrze", "zgoda", "rób to",
    "rob to", "zrób to", "zrob to", "potwierdzam", "potwierdź", "potwierdz",
    # Russian
    "да", "ага", "угу", "ладно", "хорошо", "давай", "делай", "сделай",
    "подтверждаю", "конечно", "согласен",
    # Ukrainian
    "так", "добре", "гаразд", "зроби", "підтверджую", "звичайно", "згоден",
}
_NO_OTHER = {
    # French
    "non", "non merci", "annule", "annuler", "arrête", "arrete", "arrêter",
    "laisse tomber", "pas maintenant", "ne le fais pas", "négatif", "negatif",
    # German
    "nein", "nein danke", "abbrechen", "abbruch", "stopp", "lass es",
    "lass es sein", "nicht jetzt", "nicht", "bloß nicht", "negativ",
    # Spanish
    "no gracias", "cancela", "cancelar", "para", "parar", "déjalo", "dejalo",
    "ahora no", "no lo hagas", "negativo",
    # Italian
    "no grazie", "annulla", "annullare", "ferma", "fermati", "lascia stare",
    "non ora", "non farlo",
    # Dutch
    "nee", "nee bedankt", "nee dank je", "annuleer", "annuleren", "laat maar",
    "niet nu", "niet doen", "negatief",
    # Portuguese
    "não", "nao", "não obrigado", "nao obrigado", "deixa", "deixa estar",
    "agora não", "agora nao", "não faça", "nao faca",
    # Polish
    "nie", "nie dziękuję", "nie dziekuje", "anuluj", "anulować", "stój",
    "zostaw to", "zostaw", "nie teraz", "nie rób tego", "nie rob tego",
    # Russian
    "нет", "нет спасибо", "отмена", "отмени", "отменить", "стоп", "оставь",
    "оставь как есть", "не сейчас", "не надо", "не нужно", "отбой",
    # Ukrainian
    "ні", "ні дякую", "скасуй", "скасувати", "скасування", "зупинись",
    "залиш", "залиш як є", "не зараз", "не треба", "не роби цього", "відбій",
}
YES = frozenset(_YES_EN | _YES_OTHER)
NO = frozenset(_NO_EN | _NO_OTHER)


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
