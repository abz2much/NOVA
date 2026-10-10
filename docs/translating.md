# Translating Nova

This page is for anyone who wants to know which languages Nova supports, or
who would like to add a language or fix a translation. You don't need to
write code: translations are plain JSON files.

## Which language Nova uses

Nova follows your Home Assistant language. You can choose a different
language for the panel under **Settings → General → Language**, or leave it
on Auto.

The language Nova speaks and writes in is a separate setting, **Settings →
General → Nova speaks**. It can differ from both Home Assistant's language
and the panel's. It keeps the region or script where that changes the
language, such as Brazilian Portuguese or Traditional Chinese. See
[Settings reference](settings-reference.md) for the details.

## Language status

There are three separate sets of text.

### The panel

- Fully translated: Brazilian Portuguese, Czech, Dutch, French, German,
  Polish, Russian, Simplified Chinese, Spanish, Swedish and Traditional
  Chinese. Checks in CI keep these complete.
- Partly translated: Danish, Finnish, Italian, Norwegian Bokmål, Portuguese,
  Romanian, Slovak, Turkish and Ukrainian. Their files exist, and anything
  not yet translated shows in English.
- Entity IDs, model names, counts, log content and other live values are
  never translated.
- Some text Nova's backend sends is still English in every language: Setup
  Doctor results, the energy outlook notes, and the reasons shown with
  learned suggestions. Mode descriptions, the first run checklist, Host
  Health reading names and memory sources are translated in the fully
  translated languages.

### The setup dialog

The setup and configuration dialogs use Home Assistant's own translation
system, separate from the panel. They are complete in 20 languages besides
English: Brazilian Portuguese, Czech, Danish, Dutch, Finnish, French,
German, Italian, Norwegian Bokmål, Polish, Portuguese, Romanian, Russian,
Simplified Chinese, Slovak, Spanish, Swedish, Traditional Chinese (with
Taiwan wording), Turkish and Ukrainian. Other languages fall back to
English.

### Safety notifications

Safety notifications are translated for English, French, German, Spanish,
Italian, Dutch and Portuguese. In any other language they stay in English.

## How regional languages are matched

The panel tries the full regional tag first, then the base language. For
example, `pt-BR` uses `pt-br.json`, and `fr-CA` uses `fr.json`. Simplified
Chinese (`zh-Hans`) uses `zh.json`, and Traditional Chinese (`zh-Hant`) uses
`zh-hant.json`. Anything not translated falls back to English, so nothing
breaks.

## Add or fix a language

There are two kinds of file:

- The panel: `custom_components/nova/frontend/i18n/<lang>.json`. Each key is
  the exact English text the panel shows.
- The setup dialog: `custom_components/nova/translations/<lang>.json`, in
  Home Assistant's own format, with `en.json` as the English source.

To add a language or improve one, copy an existing file, translate the
values, and keep the keys unchanged. Leave technical values as they are:
entity IDs, model names and addresses. Keep every `{placeholder}` and symbol
(such as →, · and %) exactly as in the English. Corrections are welcome,
especially from native speakers improving machine assisted translations.

If you change the panel's own text in code, the steps for keeping the
language files in step are in `CONTRIBUTING.md`.

## Right to left languages

Languages written right to left, such as Arabic and Hebrew, also need the
panel's layout to support them. That work hasn't been done yet, so it is a
good place to help if you are interested.

## See also

- [Getting started](getting-started.md): where settings live
- [Voice and speakers](voice-and-speakers.md): the voice and how Nova addresses people
- [Settings reference](settings-reference.md): the language settings
