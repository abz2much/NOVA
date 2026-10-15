"""Translations for the safety notifications Nova writes without the model.

Freeze, intrusion and lockdown alerts are built from fixed templates so they
never depend on the model, which also means the model's language setting
cannot translate them. The household's language is looked up here instead,
with English as the fallback for any language or key that is missing.
Device names, area names and the honorific are filled in as they are, never
translated.

The longer lockdown summaries that stitch device lists into one sentence
("I locked X and closed Y, but Z is open") are still English only; a half
translated sentence reads worse than a clean English one.

Notes for the lockdown templates:
* {names} is a comma joined device list and always follows the verb, so
  past participles never have to agree with a device (de and nl put the
  participle after the object, as their grammar needs).
* Clauses about a device are impersonal or use the infinitive, so one
  wording is right whatever the device's grammatical gender.
* The auxiliary ("I", "j'ai", "ich habe") sits in the wrapper, so {did}
  reads naturally after it; pt uses the simple past and needs none.
* The *_pending forms only say a command was sent. A background check
  confirms each lock or cover later and alerts if one did not secure, so
  these never claim the home is secure. lockdown_locked and lockdown_closed
  remain for the night sweep's own wording.
"""
from __future__ import annotations

_FALLBACK = "en"

# Full sentences start with this; see message().
_HONORIFIC_LEAD_IN = "{honorific}, "

# The word for "and" in device lists, and the languages that put a comma
# before it.
_AND: dict[str, str] = {"en": "and", "fr": "et", "de": "und", "es": "y",
                        "it": "e", "nl": "en", "pt": "e"}
_OXFORD = {"en"}


def _norm(lang: str | None) -> str:
    return (lang or "en").split("-")[0].lower()


_TITLE_TEXT: dict[str, dict[str, str]] = {
    "en": {  # English
        "freeze_critical": "Nova — Freeze Warning",
        "freeze_warning": "Nova — Temperature Alert",
        "intrusion_investigating": "Nova — Security Alert",
        "intrusion_confirmed": "Nova — INTRUSION",
        "critical_hazard": "Nova — Safety Alarm",
        "lockdown": "Nova — House Secured",
    },
    "fr": {  # French
        "freeze_critical": "Nova — Alerte de gel",
        "freeze_warning": "Nova — Alerte température",
        "intrusion_investigating": "Nova — Alerte de sécurité",
        "intrusion_confirmed": "Nova — INTRUSION",
        "critical_hazard": "Nova — Alarme de sécurité",
        "lockdown": "Nova — Maison sécurisée",
    },
    "de": {  # German
        "freeze_critical": "Nova — Frostwarnung",
        "freeze_warning": "Nova — Temperaturwarnung",
        "intrusion_investigating": "Nova — Sicherheitswarnung",
        "intrusion_confirmed": "Nova — EINBRUCH",
        "critical_hazard": "Nova — Sicherheitsalarm",
        "lockdown": "Nova — Haus gesichert",
    },
    "es": {  # Spanish
        "freeze_critical": "Nova — Alerta de heladas",
        "freeze_warning": "Nova — Alerta de temperatura",
        "intrusion_investigating": "Nova — Alerta de seguridad",
        "intrusion_confirmed": "Nova — INTRUSIÓN",
        "critical_hazard": "Nova — Alarma de seguridad",
        "lockdown": "Nova — Casa asegurada",
    },
    "it": {  # Italian
        "freeze_critical": "Nova — Allerta gelo",
        "freeze_warning": "Nova — Allerta temperatura",
        "intrusion_investigating": "Nova — Allerta sicurezza",
        "intrusion_confirmed": "Nova — INTRUSIONE",
        "critical_hazard": "Nova — Allarme di sicurezza",
        "lockdown": "Nova — Casa protetta",
    },
    "nl": {  # Dutch
        "freeze_critical": "Nova — Vorstwaarschuwing",
        "freeze_warning": "Nova — Temperatuuralarm",
        "intrusion_investigating": "Nova — Beveiligingsalarm",
        "intrusion_confirmed": "Nova — INBRAAK",
        "critical_hazard": "Nova — Veiligheidsalarm",
        "lockdown": "Nova — Huis beveiligd",
    },
    "pt": {  # Portuguese
        "freeze_critical": "Nova — Alerta de gelo",
        "freeze_warning": "Nova — Alerta de temperatura",
        "intrusion_investigating": "Nova — Alerta de segurança",
        "intrusion_confirmed": "Nova — INTRUSÃO",
        "critical_hazard": "Nova — Alarme de segurança",
        "lockdown": "Nova — Casa protegida",
    },
}


_MESSAGE_TEXT: dict[str, dict[str, str]] = {
    "en": {  # English
        "freeze_critical": (
            "{honorific}, outdoor temperature has dropped to {reading}. Pipe "
            "freeze risk is severe. I recommend opening cabinet doors near "
            "exterior walls and confirming heat is set to at least {set_to}."
        ),
        "freeze_warning": (
            "{honorific}, outdoor temperature is {reading}. I'm monitoring for "
            "pipe freeze risk."
        ),
        "intrusion_alert": (
            "{honorific}, motion at {where} while no one is home{ctx}. "
            "Investigating from the point of entry — I'll alert the house and "
            "every device only if it's a real intrusion."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, motion at {where} while the household is asleep{ctx}. "
            "Investigating from the point of entry — I'll alert the house and "
            "every device only if it's a real intrusion."
        ),
        "intrusion_alert_confined": (
            "{honorific}, motion at {where} while the house is secured{ctx}. "
            "Investigating from the point of entry — I'll alert the house and "
            "every device only if it's a real intrusion."
        ),
        "intrusion_ctx_open": " ({name} open)",
        "intrusion_ctx_armed": " (alarm armed)",
        "lockdown_lifted": "{honorific}, lockdown lifted. The house is back to normal.",
        "lockdown_already_secured": (
            "{honorific}, lockdown engaged — the home was already fully secured."
        ),
        "lockdown_locked": "locked {names}",
        "lockdown_closed": "closed {names}",
        "lockdown_lock_pending": "lock {names}",
        "lockdown_close_pending": "close {names}",
        "lockdown_gap_one": (
            "{names} is open and I can't secure it remotely — you'll want to close "
            "it"
        ),
        "lockdown_gap_few": (
            "{names} are open and I can't secure them remotely — you'll want to "
            "close them"
        ),
        "lockdown_gap_many": (
            "{count} openings are open and I can't secure them remotely — you'll "
            "want to close them"
        ),
        "lockdown_did_pending": (
            "{honorific}, lockdown engaged. I sent commands to {did}. I will alert "
            "you if anything does not secure."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, lockdown engaged. I sent commands to {did}, but {gap}. I "
            "will alert you if anything does not secure."
        ),
        "lockdown_gap_only": (
            "{honorific}, lockdown engaged. Everything was already secured, but "
            "{gap}."
        ),
        "lockdown_nighttime": "{honorific}, nighttime lockdown: {body}. The house is secured.",
        "lockdown_left_alone": (
            "{names} was opened again after I secured it, so I'll leave it until "
            "morning"
        ),
        "lockdown_unreadable": "I can't tell if {names} is closed",
        "lockdown_secure_failed": "I couldn't secure {names} — please check by hand",
        "lockdown_failed_only": "{honorific}, lockdown engaged, but {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, nighttime lockdown: {body}. The house is not fully "
            "secured — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, nighttime lockdown: the house is not fully secured — "
            "{failed}."
        ),
    },
    "fr": {  # French
        "freeze_critical": (
            "{honorific}, la température extérieure est descendue à {reading}. Le "
            "risque de gel des canalisations est élevé. Je recommande d'ouvrir les "
            "portes des placards le long des murs extérieurs et de régler le "
            "chauffage à au moins {set_to}."
        ),
        "freeze_warning": (
            "{honorific}, la température extérieure est de {reading}. Je surveille "
            "le risque de gel des canalisations."
        ),
        "intrusion_alert": (
            "{honorific}, mouvement détecté à {where} alors que personne n'est à "
            "la maison{ctx}. J'enquête depuis le point d'entrée — je n'alerterai "
            "la maison et tous les appareils que s'il s'agit d'une véritable "
            "intrusion."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, mouvement détecté à {where} pendant que le foyer "
            "dort{ctx}. J'enquête depuis le point d'entrée — je n'alerterai la "
            "maison et tous les appareils que s'il s'agit d'une véritable "
            "intrusion."
        ),
        "intrusion_alert_confined": (
            "{honorific}, mouvement détecté à {where} alors que la maison est "
            "sécurisée{ctx}. J'enquête depuis le point d'entrée — je n'alerterai "
            "la maison et tous les appareils que s'il s'agit d'une véritable "
            "intrusion."
        ),
        "intrusion_ctx_open": " ({name} ouvert)",
        "intrusion_ctx_armed": " (alarme armée)",
        "lockdown_lifted": "{honorific}, confinement levé. La maison est revenue à la normale.",
        "lockdown_already_secured": (
            "{honorific}, confinement activé — la maison était déjà entièrement "
            "sécurisée."
        ),
        "lockdown_locked": "verrouillé {names}",
        "lockdown_closed": "fermé {names}",
        "lockdown_lock_pending": "verrouiller {names}",
        "lockdown_close_pending": "fermer {names}",
        "lockdown_gap_one": "{names} : impossible à verrouiller à distance, à fermer manuellement",
        "lockdown_gap_few": "{names} : impossible à verrouiller à distance, à fermer manuellement",
        "lockdown_gap_many": (
            "{count} ouvertures ne peuvent pas être verrouillées à distance — à "
            "fermer manuellement"
        ),
        "lockdown_did_pending": (
            "{honorific}, confinement activé. J'ai envoyé les commandes pour "
            "{did}. Je vous alerterai si quelque chose ne se sécurise pas."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, confinement activé. J'ai envoyé les commandes pour "
            "{did}, mais {gap}. Je vous alerterai si quelque chose ne se sécurise "
            "pas."
        ),
        "lockdown_gap_only": (
            "{honorific}, confinement activé. Tout était déjà sécurisé, mais {gap}."
        ),
        "lockdown_nighttime": (
            "{honorific}, confinement nocturne : {body}. La maison est sécurisée."
        ),
        "lockdown_left_alone": (
            "{names} a été rouvert après que je l'ai sécurisé, je n'y touche plus "
            "jusqu'au matin"
        ),
        "lockdown_unreadable": "je ne sais pas si {names} est fermé",
        "lockdown_secure_failed": "je n'ai pas pu sécuriser {names} — à vérifier manuellement",
        "lockdown_failed_only": "{honorific}, confinement activé, mais {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, confinement nocturne : {body}. La maison n'est pas "
            "entièrement sécurisée — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, confinement nocturne : la maison n'est pas entièrement "
            "sécurisée — {failed}."
        ),
    },
    "de": {  # German
        "freeze_critical": (
            "{honorific}, die Außentemperatur ist auf {reading} gefallen. Es "
            "besteht ein hohes Risiko eingefrorener Rohre. Ich empfehle, "
            "Schranktüren an Außenwänden zu öffnen und die Heizung auf mindestens "
            "{set_to} einzustellen."
        ),
        "freeze_warning": (
            "{honorific}, die Außentemperatur beträgt {reading}. Ich überwache das "
            "Risiko eingefrorener Rohre."
        ),
        "intrusion_alert": (
            "{honorific}, Bewegung bei {where}, während niemand zu Hause ist{ctx}. "
            "Ich untersuche vom Eintrittspunkt aus — ich alarmiere das Haus und "
            "alle Geräte nur bei einem echten Einbruch."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, Bewegung bei {where}, während der Haushalt schläft{ctx}. "
            "Ich untersuche vom Eintrittspunkt aus — ich alarmiere das Haus und "
            "alle Geräte nur bei einem echten Einbruch."
        ),
        "intrusion_alert_confined": (
            "{honorific}, Bewegung bei {where}, während das Haus gesichert "
            "ist{ctx}. Ich untersuche vom Eintrittspunkt aus — ich alarmiere das "
            "Haus und alle Geräte nur bei einem echten Einbruch."
        ),
        "intrusion_ctx_open": " ({name} offen)",
        "intrusion_ctx_armed": " (Alarm scharf)",
        "lockdown_lifted": (
            "{honorific}, Sicherung aufgehoben. Das Haus ist wieder im "
            "Normalzustand."
        ),
        "lockdown_already_secured": (
            "{honorific}, Sicherung aktiviert — das Haus war bereits vollständig "
            "gesichert."
        ),
        "lockdown_locked": "{names} verriegelt",
        "lockdown_closed": "{names} geschlossen",
        "lockdown_lock_pending": "{names} zu verriegeln",
        "lockdown_close_pending": "{names} zu schließen",
        "lockdown_gap_one": (
            "{names}: lässt sich nicht aus der Ferne sichern — bitte manuell "
            "schließen"
        ),
        "lockdown_gap_few": (
            "{names}: lassen sich nicht aus der Ferne sichern — bitte manuell "
            "schließen"
        ),
        "lockdown_gap_many": (
            "{count} Öffnungen lassen sich nicht aus der Ferne sichern — bitte "
            "manuell schließen"
        ),
        "lockdown_did_pending": (
            "{honorific}, Sicherung aktiviert. Ich habe Befehle gesendet, um "
            "{did}. Ich werde Sie benachrichtigen, falls etwas nicht sichert."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, Sicherung aktiviert. Ich habe Befehle gesendet, um "
            "{did}, aber {gap}. Ich werde Sie benachrichtigen, falls etwas nicht "
            "sichert."
        ),
        "lockdown_gap_only": (
            "{honorific}, Sicherung aktiviert. Alles war bereits gesichert, aber "
            "{gap}."
        ),
        "lockdown_nighttime": "{honorific}, nächtliche Sicherung: {body}. Das Haus ist gesichert.",
        "lockdown_left_alone": (
            "{names} wurde wieder geöffnet, nachdem ich es gesichert hatte, ich "
            "lasse es bis zum Morgen"
        ),
        "lockdown_unreadable": "ich kann nicht erkennen, ob {names} geschlossen ist",
        "lockdown_secure_failed": "{names} konnte nicht gesichert werden — bitte manuell prüfen",
        "lockdown_failed_only": "{honorific}, Sicherung aktiviert, aber {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, nächtliche Sicherung: {body}. Das Haus ist nicht "
            "vollständig gesichert — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, nächtliche Sicherung: das Haus ist nicht vollständig "
            "gesichert — {failed}."
        ),
    },
    "es": {  # Spanish
        "freeze_critical": (
            "{honorific}, la temperatura exterior ha bajado a {reading}. El riesgo "
            "de congelación de tuberías es alto. Recomiendo abrir las puertas de "
            "los armarios junto a las paredes exteriores y ajustar la calefacción "
            "al menos a {set_to}."
        ),
        "freeze_warning": (
            "{honorific}, la temperatura exterior es de {reading}. Estoy vigilando "
            "el riesgo de congelación de tuberías."
        ),
        "intrusion_alert": (
            "{honorific}, movimiento en {where} cuando no hay nadie en casa{ctx}. "
            "Estoy investigando desde el punto de entrada; solo alertaré a la casa "
            "y a todos los dispositivos si es una intrusión real."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, movimiento en {where} mientras la casa duerme{ctx}. "
            "Estoy investigando desde el punto de entrada; solo alertaré a la casa "
            "y a todos los dispositivos si es una intrusión real."
        ),
        "intrusion_alert_confined": (
            "{honorific}, movimiento en {where} mientras la casa está "
            "protegida{ctx}. Estoy investigando desde el punto de entrada; solo "
            "alertaré a la casa y a todos los dispositivos si es una intrusión "
            "real."
        ),
        "intrusion_ctx_open": " ({name} abierto)",
        "intrusion_ctx_armed": " (alarma armada)",
        "lockdown_lifted": (
            "{honorific}, confinamiento desactivado. La casa ha vuelto a la "
            "normalidad."
        ),
        "lockdown_already_secured": (
            "{honorific}, confinamiento activado — la casa ya estaba totalmente "
            "asegurada."
        ),
        "lockdown_locked": "bloqueado {names}",
        "lockdown_closed": "cerrado {names}",
        "lockdown_lock_pending": "bloquear {names}",
        "lockdown_close_pending": "cerrar {names}",
        "lockdown_gap_one": (
            "{names}: no se puede asegurar a distancia — hay que cerrarlo "
            "manualmente"
        ),
        "lockdown_gap_few": (
            "{names}: no se pueden asegurar a distancia — hay que cerrarlos "
            "manualmente"
        ),
        "lockdown_gap_many": (
            "{count} aberturas no se pueden asegurar a distancia — hay que "
            "cerrarlas manualmente"
        ),
        "lockdown_did_pending": (
            "{honorific}, confinamiento activado. He enviado órdenes para {did}. "
            "Le avisaré si algo no se asegura."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, confinamiento activado. He enviado órdenes para {did}, "
            "pero {gap}. Le avisaré si algo no se asegura."
        ),
        "lockdown_gap_only": (
            "{honorific}, confinamiento activado. Todo ya estaba asegurado, pero "
            "{gap}."
        ),
        "lockdown_nighttime": (
            "{honorific}, confinamiento nocturno: {body}. La casa está asegurada."
        ),
        "lockdown_left_alone": (
            "{names} se volvió a abrir después de asegurarlo, así que lo dejo "
            "hasta mañana"
        ),
        "lockdown_unreadable": "no sé si {names} está cerrado",
        "lockdown_secure_failed": "no pude asegurar {names} — hay que revisarlo manualmente",
        "lockdown_failed_only": "{honorific}, confinamiento activado, pero {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, confinamiento nocturno: {body}. La casa no está "
            "totalmente asegurada — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, confinamiento nocturno: la casa no está totalmente "
            "asegurada — {failed}."
        ),
    },
    "it": {  # Italian
        "freeze_critical": (
            "{honorific}, la temperatura esterna è scesa a {reading}. Il rischio "
            "di congelamento delle tubature è elevato. Consiglio di aprire gli "
            "sportelli dei mobiletti lungo le pareti esterne e di impostare il "
            "riscaldamento ad almeno {set_to}."
        ),
        "freeze_warning": (
            "{honorific}, la temperatura esterna è di {reading}. Sto monitorando "
            "il rischio di congelamento delle tubature."
        ),
        "intrusion_alert": (
            "{honorific}, movimento a {where} mentre non c'è nessuno in casa{ctx}. "
            "Sto indagando dal punto di ingresso — avviserò la casa e tutti i "
            "dispositivi solo se si tratta di una vera intrusione."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, movimento a {where} mentre la casa dorme{ctx}. Sto "
            "indagando dal punto di ingresso — avviserò la casa e tutti i "
            "dispositivi solo se si tratta di una vera intrusione."
        ),
        "intrusion_alert_confined": (
            "{honorific}, movimento a {where} mentre la casa è messa in "
            "sicurezza{ctx}. Sto indagando dal punto di ingresso — avviserò la "
            "casa e tutti i dispositivi solo se si tratta di una vera intrusione."
        ),
        "intrusion_ctx_open": " ({name} aperto)",
        "intrusion_ctx_armed": " (allarme inserito)",
        "lockdown_lifted": "{honorific}, blocco revocato. La casa è tornata alla normalità.",
        "lockdown_already_secured": (
            "{honorific}, blocco attivato — la casa era già completamente protetta."
        ),
        "lockdown_locked": "bloccato {names}",
        "lockdown_closed": "chiuso {names}",
        "lockdown_lock_pending": "bloccare {names}",
        "lockdown_close_pending": "chiudere {names}",
        "lockdown_gap_one": "{names}: impossibile bloccare a distanza — da chiudere manualmente",
        "lockdown_gap_few": "{names}: impossibile bloccare a distanza — da chiudere manualmente",
        "lockdown_gap_many": (
            "{count} aperture non si possono bloccare a distanza — da chiudere "
            "manualmente"
        ),
        "lockdown_did_pending": (
            "{honorific}, blocco attivato. Ho inviato i comandi per {did}. Ti "
            "avviserò se qualcosa non si mette in sicurezza."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, blocco attivato. Ho inviato i comandi per {did}, ma "
            "{gap}. Ti avviserò se qualcosa non si mette in sicurezza."
        ),
        "lockdown_gap_only": "{honorific}, blocco attivato. Era già tutto protetto, ma {gap}.",
        "lockdown_nighttime": "{honorific}, blocco notturno: {body}. La casa è protetta.",
        "lockdown_left_alone": (
            "{names} è stato riaperto dopo che l'avevo messo in sicurezza, lo "
            "lascio stare fino al mattino"
        ),
        "lockdown_unreadable": "non riesco a capire se {names} è chiuso",
        "lockdown_secure_failed": (
            "non sono riuscito a mettere in sicurezza {names} — da controllare "
            "manualmente"
        ),
        "lockdown_failed_only": "{honorific}, blocco attivato, ma {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, blocco notturno: {body}. La casa non è completamente "
            "protetta — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, blocco notturno: la casa non è completamente protetta — "
            "{failed}."
        ),
    },
    "nl": {  # Dutch
        "freeze_critical": (
            "{honorific}, de buitentemperatuur is gedaald tot {reading}. Er is een "
            "hoog risico op bevriezing van leidingen. Ik raad aan om kastdeuren "
            "bij buitenmuren te openen en de verwarming op minstens {set_to} te "
            "zetten."
        ),
        "freeze_warning": (
            "{honorific}, de buitentemperatuur is {reading}. Ik houd het risico op "
            "bevriezing van leidingen in de gaten."
        ),
        "intrusion_alert": (
            "{honorific}, beweging bij {where} terwijl er niemand thuis is{ctx}. "
            "Ik onderzoek het vanaf het toegangspunt — ik waarschuw het huis en "
            "alle apparaten alleen als het een echte inbraak is."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, beweging bij {where} terwijl het huishouden slaapt{ctx}. "
            "Ik onderzoek het vanaf het toegangspunt — ik waarschuw het huis en "
            "alle apparaten alleen als het een echte inbraak is."
        ),
        "intrusion_alert_confined": (
            "{honorific}, beweging bij {where} terwijl het huis beveiligd is{ctx}. "
            "Ik onderzoek het vanaf het toegangspunt — ik waarschuw het huis en "
            "alle apparaten alleen als het een echte inbraak is."
        ),
        "intrusion_ctx_open": " ({name} open)",
        "intrusion_ctx_armed": " (alarm ingeschakeld)",
        "lockdown_lifted": "{honorific}, vergrendeling opgeheven. Het huis is weer normaal.",
        "lockdown_already_secured": (
            "{honorific}, vergrendeling ingeschakeld — het huis was al volledig "
            "beveiligd."
        ),
        "lockdown_locked": "{names} vergrendeld",
        "lockdown_closed": "{names} gesloten",
        "lockdown_lock_pending": "{names} te vergrendelen",
        "lockdown_close_pending": "{names} te sluiten",
        "lockdown_gap_one": "{names}: niet op afstand te vergrendelen — graag handmatig sluiten",
        "lockdown_gap_few": "{names}: niet op afstand te vergrendelen — graag handmatig sluiten",
        "lockdown_gap_many": (
            "{count} openingen zijn niet op afstand te vergrendelen — graag "
            "handmatig sluiten"
        ),
        "lockdown_did_pending": (
            "{honorific}, vergrendeling ingeschakeld. Ik heb opdrachten gestuurd "
            "om {did}. Ik waarschuw u als iets niet beveiligt."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, vergrendeling ingeschakeld. Ik heb opdrachten gestuurd "
            "om {did}, maar {gap}. Ik waarschuw u als iets niet beveiligt."
        ),
        "lockdown_gap_only": (
            "{honorific}, vergrendeling ingeschakeld. Alles was al beveiligd, maar "
            "{gap}."
        ),
        "lockdown_nighttime": (
            "{honorific}, nachtelijke vergrendeling: {body}. Het huis is beveiligd."
        ),
        "lockdown_left_alone": (
            "{names} is weer geopend nadat ik het had beveiligd, dus ik laat het "
            "tot de ochtend"
        ),
        "lockdown_unreadable": "ik kan niet zien of {names} dicht is",
        "lockdown_secure_failed": "ik kon {names} niet beveiligen — graag handmatig controleren",
        "lockdown_failed_only": "{honorific}, vergrendeling ingeschakeld, maar {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, nachtelijke vergrendeling: {body}. Het huis is niet "
            "volledig beveiligd — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, nachtelijke vergrendeling: het huis is niet volledig "
            "beveiligd — {failed}."
        ),
    },
    "pt": {  # Portuguese
        "freeze_critical": (
            "{honorific}, a temperatura exterior desceu para {reading}. O risco de "
            "congelamento das tubagens é elevado. Recomendo abrir as portas dos "
            "armários junto às paredes exteriores e definir o aquecimento para "
            "pelo menos {set_to}."
        ),
        "freeze_warning": (
            "{honorific}, a temperatura exterior é de {reading}. Estou a "
            "monitorizar o risco de congelamento das tubagens."
        ),
        "intrusion_alert": (
            "{honorific}, movimento em {where} enquanto não está ninguém em "
            "casa{ctx}. Estou a investigar a partir do ponto de entrada — só "
            "alertarei a casa e todos os dispositivos se for uma intrusão real."
        ),
        "intrusion_alert_sleep": (
            "{honorific}, movimento em {where} enquanto a casa dorme{ctx}. Estou a "
            "investigar a partir do ponto de entrada — só alertarei a casa e todos "
            "os dispositivos se for uma intrusão real."
        ),
        "intrusion_alert_confined": (
            "{honorific}, movimento em {where} enquanto a casa está "
            "protegida{ctx}. Estou a investigar a partir do ponto de entrada — só "
            "alertarei a casa e todos os dispositivos se for uma intrusão real."
        ),
        "intrusion_ctx_open": " ({name} aberto)",
        "intrusion_ctx_armed": " (alarme armado)",
        "lockdown_lifted": "{honorific}, confinamento levantado. A casa voltou ao normal.",
        "lockdown_already_secured": (
            "{honorific}, confinamento ativado — a casa já estava totalmente "
            "protegida."
        ),
        "lockdown_locked": "tranquei {names}",
        "lockdown_closed": "fechei {names}",
        "lockdown_lock_pending": "trancar {names}",
        "lockdown_close_pending": "fechar {names}",
        "lockdown_gap_one": "{names}: não dá para trancar à distância — feche manualmente",
        "lockdown_gap_few": "{names}: não dá para trancar à distância — feche manualmente",
        "lockdown_gap_many": (
            "{count} aberturas não podem ser trancadas à distância — feche "
            "manualmente"
        ),
        "lockdown_did_pending": (
            "{honorific}, confinamento ativado. Enviei comandos para {did}. Vou "
            "avisá-lo se algo não ficar seguro."
        ),
        "lockdown_did_gap_pending": (
            "{honorific}, confinamento ativado. Enviei comandos para {did}, mas "
            "{gap}. Vou avisá-lo se algo não ficar seguro."
        ),
        "lockdown_gap_only": (
            "{honorific}, confinamento ativado. Tudo já estava seguro, mas {gap}."
        ),
        "lockdown_nighttime": "{honorific}, confinamento noturno: {body}. A casa está segura.",
        "lockdown_left_alone": (
            "{names} foi aberto de novo depois de eu o proteger, por isso deixo-o "
            "até de manhã"
        ),
        "lockdown_unreadable": "não consigo saber se {names} está fechado",
        "lockdown_secure_failed": "não consegui proteger {names} — verifique manualmente",
        "lockdown_failed_only": "{honorific}, confinamento ativado, mas {gap}.",
        "lockdown_nighttime_partial": (
            "{honorific}, confinamento noturno: {body}. A casa não está totalmente "
            "segura — {failed}."
        ),
        "lockdown_nighttime_failed_only": (
            "{honorific}, confinamento noturno: a casa não está totalmente segura "
            "— {failed}."
        ),
    },
}


def _by_key(by_language: dict) -> dict[str, dict[str, str]]:
    """{language: {key: text}} turned into {key: {language: text}}, the
    shape callers and tests read."""
    out: dict[str, dict[str, str]] = {}
    for lang, texts in by_language.items():
        for key, text in texts.items():
            out.setdefault(key, {})[lang] = text
    return out


TITLES = _by_key(_TITLE_TEXT)
MESSAGES = _by_key(_MESSAGE_TEXT)


def title(key: str, lang: str | None) -> str:
    """The notification title for ``key`` in ``lang``, else English, else
    "Nova"."""
    texts = TITLES.get(key, {})
    return texts.get(_norm(lang)) or texts.get(_FALLBACK) or "Nova"


def _strip_honorific_lead_in(tmpl: str) -> tuple[str, bool]:
    """Every full sentence template opens with "{honorific}, " and nowhere
    else holds it. It is taken off here and put back by persona.lead_in(),
    so an empty honorific (nobody in particular to address) gives a properly
    capitalised sentence rather than a stray ", ". Fragments such as
    intrusion_ctx_open never had it and pass through as they are."""
    if tmpl.startswith(_HONORIFIC_LEAD_IN):
        return tmpl[len(_HONORIFIC_LEAD_IN):], True
    return tmpl, False


def _fill(template: str, params: dict) -> tuple[str, bool]:
    body, addressed = _strip_honorific_lead_in(template)
    return body.format(**params), addressed


def message(key: str, lang: str | None, **params) -> str:
    """The message for ``key`` in ``lang`` (English when missing), filled
    with ``params``. ``honorific`` (default "sir") is applied through the
    persona's lead in rather than formatted in.

    Never raises: when the translation cannot be filled the English one is
    tried, and when that fails too the raw English template (or "") comes
    back.
    """
    honorific = params.pop("honorific", "sir")
    texts = MESSAGES.get(key, {})
    english = texts.get(_FALLBACK, "") or ""
    try:
        text, addressed = _fill(texts.get(_norm(lang)) or english, params)
    except Exception:
        try:
            text, addressed = _fill(english, params)
        except Exception:
            return english
    if not addressed:
        return text
    from . import persona
    return persona.lead_in(honorific, text)


def join_names(names, lang: str | None = None) -> str:
    """Join names as a sentence would, with the language's "and": one name
    as is, two as "a et b" (fr), three or more as "a, b, and c" (en) or
    "a, b e c" (it). Names are never translated."""
    names = [name for name in (names or []) if name]
    if len(names) < 2:
        return names[0] if names else ""
    code = _norm(lang)
    word = _AND.get(code, "and")
    if len(names) == 2:
        return f"{names[0]} {word} {names[1]}"
    last = f", {word} " if code in _OXFORD else f" {word} "
    return ", ".join(names[:-1]) + last + names[-1]
