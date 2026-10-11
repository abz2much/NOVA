"""Recording a person's voice for Nova Voice ID (8.36.0).

From the panel: pick a person and a satellite. Nova arms one session and the
satellite announces the first sentence. The person says the wake word, then
the sentence. That voice turn, from that satellite only, belongs to the
session: conversation.py hands it here before dedup and returns the reply,
so it never reaches voice phrases, the local engine or the AI.

A turn's sample is committed to the app (nvid-enrol-commit) when it has a
sample_id, at least 1.5 s of speech and, from the third sample on, the
result doesn't show someone else as the best match. The app only names the
best match when the status is "known", so "someone else" means: known as
another person, or this person in second place. Otherwise the same sentence
is asked again, at most twice, then Nova moves to the next sentence.

Each next prompt is the turn's own reply, spoken by the pipeline on that
satellite. Only the first prompt is an assist_satellite.announce: announcing
while the satellite's own pipeline run is still going would cut that run off.
The mic is never reopened, so the person says the wake word each time.

Eight commits end it ("Thanks, {name}, that's done."). Cancel ends it. After
5 minutes it ends quietly. Samples already committed stay in the app. Nova
keeps no audio or voice data: the session holds ids and counts only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

SAMPLES_NEEDED = 8
MAX_RETRIES = 2
SESSION_S = 300.0
MIN_SPEECH_S = 1.5
CHECK_MATCH_FROM = 2          # committed samples before the match check starts
ANNOUNCE_TIMEOUT_S = 20.0

# Short, plain sentences of 2 to 4 seconds. None is a command or a question:
# Home Assistant's own local handling and sentence triggers run before Nova
# and would act on one.
SENTENCES: dict[str, list[str]] = {
    "en": [
        "The quick brown fox jumps over the lazy dog.",
        "A cup of tea always tastes better in the morning.",
        "The garden looks lovely after the rain.",
        "My favourite colour is a deep ocean blue.",
        "We walked along the beach as the sun went down.",
        "Seven green apples sat in a wooden bowl.",
        "The train to the city leaves just after nine.",
        "Bright stars filled the sky above the hills.",
    ],
    "fr": [
        "Le vif renard brun saute par-dessus le chien paresseux.",
        "Une tasse de thé est toujours meilleure le matin.",
        "Le jardin est magnifique après la pluie.",
        "Ma couleur préférée est un bleu océan profond.",
        "Nous avons marché sur la plage au coucher du soleil.",
        "Sept pommes vertes étaient dans un bol en bois.",
        "Le train pour la ville part juste après neuf heures.",
        "Des étoiles brillantes remplissaient le ciel au-dessus des collines.",
    ],
    "de": [
        "Der schnelle braune Fuchs springt über den faulen Hund.",
        "Eine Tasse Tee schmeckt morgens immer besser.",
        "Der Garten sieht nach dem Regen wunderschön aus.",
        "Meine Lieblingsfarbe ist ein tiefes Meeresblau.",
        "Wir gingen am Strand entlang, als die Sonne unterging.",
        "Sieben grüne Äpfel lagen in einer Holzschale.",
        "Der Zug in die Stadt fährt kurz nach neun.",
        "Helle Sterne füllten den Himmel über den Hügeln.",
    ],
    "es": [
        "El rápido zorro marrón salta sobre el perro perezoso.",
        "Una taza de té siempre sabe mejor por la mañana.",
        "El jardín se ve precioso después de la lluvia.",
        "Mi color favorito es un azul océano profundo.",
        "Caminamos por la playa mientras se ponía el sol.",
        "Siete manzanas verdes estaban en un cuenco de madera.",
        "El tren a la ciudad sale justo después de las nueve.",
        "Estrellas brillantes llenaban el cielo sobre las colinas.",
    ],
    "it": [
        "La veloce volpe marrone salta sopra il cane pigro.",
        "Una tazza di tè è sempre più buona al mattino.",
        "Il giardino è bellissimo dopo la pioggia.",
        "Il mio colore preferito è un blu oceano profondo.",
        "Abbiamo camminato sulla spiaggia al tramonto.",
        "Sette mele verdi erano in una ciotola di legno.",
        "Il treno per la città parte subito dopo le nove.",
        "Stelle luminose riempivano il cielo sopra le colline.",
    ],
    "nl": [
        "De snelle bruine vos springt over de luie hond.",
        "Een kopje thee smaakt 's ochtends altijd beter.",
        "De tuin ziet er prachtig uit na de regen.",
        "Mijn lievelingskleur is een diep oceaanblauw.",
        "We liepen langs het strand terwijl de zon onderging.",
        "Zeven groene appels lagen in een houten schaal.",
        "De trein naar de stad vertrekt net na negen uur.",
        "Heldere sterren vulden de lucht boven de heuvels.",
    ],
    "pt": [
        "A rápida raposa castanha salta sobre o cão preguiçoso.",
        "Uma chávena de chá sabe sempre melhor de manhã.",
        "O jardim fica lindo depois da chuva.",
        "A minha cor favorita é um azul oceano profundo.",
        "Caminhámos pela praia enquanto o sol se punha.",
        "Sete maçãs verdes estavam numa taça de madeira.",
        "O comboio para a cidade parte logo depois das nove.",
        "Estrelas brilhantes enchiam o céu sobre as colinas.",
    ],
}

PHRASES: dict[str, dict[str, str]] = {
    "prompt": {
        "en": "{name}, after the wake word, please say: {sentence}",
        "fr": "{name}, après le mot d'activation, dites : {sentence}",
        "de": "{name}, sag nach dem Aktivierungswort bitte: {sentence}",
        "es": "{name}, después de la palabra de activación, di: {sentence}",
        "it": "{name}, dopo la parola di attivazione, di': {sentence}",
        "nl": "{name}, zeg na het activeringswoord: {sentence}",
        "pt": "{name}, depois da palavra de ativação, diga: {sentence}",
    },
    "next": {
        "en": "Thanks. Next, please say: {sentence}",
        "fr": "Merci. Ensuite, dites : {sentence}",
        "de": "Danke. Als Nächstes sag bitte: {sentence}",
        "es": "Gracias. Ahora di: {sentence}",
        "it": "Grazie. Ora di': {sentence}",
        "nl": "Bedankt. Zeg nu: {sentence}",
        "pt": "Obrigado. A seguir, diga: {sentence}",
    },
    "retry": {
        "en": "Sorry, once more please: {sentence}",
        "fr": "Désolé, encore une fois s'il vous plaît : {sentence}",
        "de": "Entschuldigung, bitte noch einmal: {sentence}",
        "es": "Perdona, otra vez por favor: {sentence}",
        "it": "Scusa, ancora una volta per favore: {sentence}",
        "nl": "Sorry, nog een keer graag: {sentence}",
        "pt": "Desculpe, mais uma vez, por favor: {sentence}",
    },
    "another": {
        "en": "Let's try a different one. Please say: {sentence}",
        "fr": "Essayons une autre phrase. Dites : {sentence}",
        "de": "Versuchen wir einen anderen Satz. Sag bitte: {sentence}",
        "es": "Probemos otra. Di: {sentence}",
        "it": "Proviamo un'altra frase. Di': {sentence}",
        "nl": "Laten we een andere proberen. Zeg: {sentence}",
        "pt": "Vamos tentar outra. Diga: {sentence}",
    },
    "done": {
        "en": "Thanks, {name}, that's done.",
        "fr": "Merci, {name}, c'est terminé.",
        "de": "Danke, {name}, das war's.",
        "es": "Gracias, {name}, ya está.",
        "it": "Grazie, {name}, abbiamo finito.",
        "nl": "Bedankt, {name}, klaar.",
        "pt": "Obrigado, {name}, está feito.",
    },
    "stopped": {
        "en": "Sorry, I couldn't save your voice. Check the Voice ID card in Nova.",
        "fr": "Désolé, je n'ai pas pu enregistrer votre voix. Vérifiez la carte Voice ID dans Nova.",
        "de": "Entschuldigung, ich konnte deine Stimme nicht speichern. Sieh dir die Voice ID Karte in Nova an.",
        "es": "Perdona, no he podido guardar tu voz. Revisa la tarjeta Voice ID en Nova.",
        "it": "Scusa, non sono riuscita a salvare la tua voce. Controlla la scheda Voice ID in Nova.",
        "nl": "Sorry, ik kon je stem niet opslaan. Kijk bij de Voice ID kaart in Nova.",
        "pt": "Desculpe, não consegui guardar a sua voz. Veja o cartão Voice ID na Nova.",
    },
}


def _lang(code: str | None) -> str:
    base = (code or "en").replace("_", "-").split("-")[0].lower()
    return base if base in SENTENCES else "en"


def sentence(lang: str, index: int) -> str:
    items = SENTENCES[_lang(lang)]
    return items[index % len(items)]


def phrase(key: str, lang: str, **params: str) -> str:
    table = PHRASES[key]
    return table.get(_lang(lang), table["en"]).format(**params)


class EnrolError(Exception):
    """Starting a session failed. ``code`` is shown to the panel."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message                       # fixed text, for the panel
        super().__init__(message)


@dataclass
class Session:
    device_id: str
    satellite: str
    person: str
    name: str
    lang: str
    expires_at: float
    prompt_index: int = 0
    committed: int = 0
    retries: int = 0
    state: str = "active"           # active | done | cancelled | expired | error
    message: str = ""
    started_at: float = field(default_factory=time.monotonic)


_SESSION: Session | None = None
_LOCK = asyncio.Lock()


def _now() -> float:
    return time.monotonic()


def current() -> Session | None:
    """The active session, or None. An expired one ends here, quietly."""
    session = _SESSION
    if session is None or session.state != "active":
        return None
    if _now() >= session.expires_at:
        session.state = "expired"
        _LOGGER.info("Nova Voice ID: recording session expired (%d of %d saved)",
                     session.committed, SAMPLES_NEEDED)
        return None
    return session


def status() -> dict[str, Any]:
    """For the panel: the session, or the one that just ended."""
    current()                        # ends an expired session
    session = _SESSION
    if session is None:
        return {"state": "idle"}
    return {
        "state": session.state,
        "person": session.person,
        "name": session.name,
        "satellite": session.satellite,
        "committed": session.committed,
        "needed": SAMPLES_NEEDED,
        "expires_in": max(0, int(session.expires_at - _now())) if session.state == "active" else 0,
        "message": session.message,
    }


def cancel() -> bool:
    session = current()
    if session is None:
        return False
    session.state = "cancelled"
    return True


def clear() -> None:
    """Forget any session (unload, tests)."""
    global _SESSION
    _SESSION = None


def _first_name(hass, person: str) -> str:
    state = hass.states.get(person)
    name = ""
    if state is not None:
        name = str((state.attributes or {}).get("friendly_name") or "").strip()
    if not name:
        name = person.split(".", 1)[-1].replace("_", " ")
    return name.split()[0] if name.split() else name


def _satellite_device(hass, satellite: str) -> str | None:
    from homeassistant.helpers import entity_registry as er
    entry = er.async_get(hass).async_get(satellite)
    return entry.device_id if entry is not None and entry.device_id else None


async def async_start(hass, entry, person: str, satellite: str) -> dict[str, Any]:
    """Arm a session and announce the first sentence. Raises EnrolError."""
    global _SESSION
    from . import voice_id, voice_id_store
    from .output_language import resolve

    if not isinstance(person, str) or not voice_id.PERSON_RE.fullmatch(person) \
            or hass.states.get(person) is None:
        raise EnrolError("bad_person", "Pick a person from the list.")
    if not isinstance(satellite, str) or not satellite.startswith("assist_satellite.") \
            or hass.states.get(satellite) is None:
        raise EnrolError("bad_satellite", "Pick a satellite.")
    device_id = _satellite_device(hass, satellite)
    if not device_id:
        raise EnrolError("bad_satellite", "That satellite has no device in Home Assistant.")
    if not voice_id_store.get_uri(hass, entry):
        raise EnrolError("unreachable", voice_id.ERROR_MESSAGES["unreachable"])
    if not await voice_id_store.async_get_token(hass):
        raise EnrolError("no_token", voice_id.ERROR_MESSAGES["no_token"])

    async with _LOCK:
        if current() is not None:
            raise EnrolError("busy", "A recording is already running. Cancel it first.")
        lang = _lang(resolve(hass))
        session = Session(device_id=device_id, satellite=satellite, person=person,
                          name=_first_name(hass, person), lang=lang,
                          expires_at=_now() + SESSION_S)
        _SESSION = session

    message = phrase("prompt", session.lang, name=session.name,
                     sentence=sentence(session.lang, 0))
    try:
        async with asyncio.timeout(ANNOUNCE_TIMEOUT_S):
            await hass.services.async_call(
                "assist_satellite", "announce",
                {"entity_id": satellite, "message": message}, blocking=True)
    except TimeoutError:
        pass                         # still playing; the session stays armed
    except Exception as exc:  # noqa: BLE001
        session.state = "error"
        session.message = "The satellite couldn't play the prompt."
        _LOGGER.warning("Nova Voice ID: announce on %s failed: %s", satellite, exc)
        raise EnrolError("announce_failed", session.message) from exc
    return status()


def _someone_else(result, person: str) -> bool:
    """Whether the result shows another person as the best match."""
    if result.status == "known" and result.person and result.person != person:
        return True
    return result.second == person


async def async_handle_turn(hass, entry, device_id: str | None, result) -> str | None:
    """The reply for a session turn, or None when the turn isn't the session's.

    ``result`` is the turn's VoiceResult (or None). Never raises."""
    session = current()
    if session is None or not device_id or device_id != session.device_id:
        return None
    from . import voice_id, voice_id_store
    try:
        usable = (result is not None and result.sample_id
                  and result.speech_s is not None and result.speech_s >= MIN_SPEECH_S)
        if usable and session.committed >= CHECK_MATCH_FROM and _someone_else(result, session.person):
            usable = False
        if usable:
            try:
                await voice_id.enrol_commit(
                    voice_id_store.get_uri(hass, entry),
                    await voice_id_store.async_get_token(hass),
                    result.sample_id, session.person)
            except voice_id.VoiceIdManagementError as exc:
                if exc.code != "not_found":          # expired sample: just retry
                    session.state = "error"
                    session.message = exc.message
                    _LOGGER.warning("Nova Voice ID: saving a sample failed (%s)", exc.code)
                    return phrase("stopped", session.lang)
                usable = False
        if usable:
            session.committed += 1
            session.retries = 0
            session.prompt_index += 1
            if session.committed >= SAMPLES_NEEDED:
                session.state = "done"
                return phrase("done", session.lang, name=session.name)
            return phrase("next", session.lang,
                          sentence=sentence(session.lang, session.prompt_index))
        if session.retries < MAX_RETRIES:
            session.retries += 1
            return phrase("retry", session.lang,
                          sentence=sentence(session.lang, session.prompt_index))
        session.retries = 0
        session.prompt_index += 1
        return phrase("another", session.lang,
                      sentence=sentence(session.lang, session.prompt_index))
    except Exception as exc:  # noqa: BLE001 - a session turn must never crash the turn
        session.state = "error"
        session.message = "Recording stopped after an error."
        _LOGGER.warning("Nova Voice ID: recording turn failed: %s", type(exc).__name__)
        return phrase("stopped", session.lang)
