# Nova Memory and Knowledge

Nova remembers what you tell it, how things in the house relate, and the
recent conversation. It can also answer from your own documents, read your
inbox, check your calendar and look things up on the web.

## What it does

- **Facts.** Say "remember that the bins go out on Tuesday" and Nova keeps
  it, once you confirm it.
- **Relations.** How people, places and things link up, for example "House
  member owns the bike" or "the child's room is next to the hallway".
- **Conversation memory.** What was said recently, for the person speaking.
- **Your documents.** Answers from manuals and receipts, with the source.
- **Email.** Reads your inbox, read only.
- **Calendar.** Upcoming events, overlaps and tight back to back events.
- **Web research.** Current events and facts, through DuckDuckGo or your own
  SearXNG.

## What it needs

- [ ] Nothing for facts, relations and conversation memory.
- [ ] **For documents:** files in the `nova/documents` folder inside your
  Home Assistant config folder, or uploaded on the **Settings → Document
  Library** card. PDF, `.txt` and `.md` files.
- [ ] **For email:** **Configure → Email**, and the password saved in
  `secrets.yaml` under the key you name there.
- [ ] **For the calendar:** `calendar.*` entities in Home Assistant.
- [ ] **Optional: an Ollama server** with an embedding model (for example
  `ollama pull nomic-embed-text`) for search by meaning.

## Facts and relations

- A new fact starts **pending**. Nova asks you to confirm it in the same
  conversation. If you do not, it waits on the Memory tab under **Pending
  Confirmation**, where you can approve, edit or reject it.
- Only confirmed facts reach the model. They are kept for the right person,
  so one resident's private facts are not shared with another.
- A relation also starts pending. Only a person can confirm it, on the Memory
  tab under **Relations**. Nova can propose a relation, drop a pending one,
  and look up confirmed ones, but never confirm one itself.
- Nova keeps at most 500 relations. At the cap, new proposals are refused and
  nothing is removed. A relation you remove stays removed unless you state it
  again.

## Documents

- Nova reads new files in the documents folder, and any watch folders you
  add, every 10 minutes. **INGEST FOLDER** reads them now.
- Search works by keyword with no set up. With semantic search on, it also
  matches by meaning through your Ollama server.
- Nova only ever reads inside those folders.

## What it learns by itself

- Facts and relations to propose, from what you say. They stay pending until
  you confirm them.
- Each person's routines, shown on the Memory tab under **Person Routines**.

## What it will never do on its own

- Trust a fact, or use a relation, before a person confirms it.
- Confirm a relation itself.
- Change your mail. It opens the mailbox read only and never marks, moves or
  deletes anything. Mail it reads is treated as untrusted text.
- Write anything Nova merely read, such as an email or a calendar invite, into
  its memory as a fact without your confirmation.
- Send fact or document text anywhere to build the search by meaning except
  the Ollama server you set. (When you ask a question, the text it finds goes
  to your conversation model, like any other answer.)

## Limits

- Background chatter that is not addressed to Nova is never saved to
  conversation memory.
- Semantic search needs Ollama. Without it, search is by keyword.
- Web research by DuckDuckGo gives short answers. SearXNG gives fuller
  results.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| A fact is not used | It is still pending | Approve it on the Memory tab. |
| A new document is not found | It has not been read yet | Press **INGEST FOLDER**, or wait up to 10 minutes. |
| "Couldn't reach the library" | The Document Library could not load | Restart Home Assistant after updating, then reopen. |
| Email questions fail | Email reading is off, or the password key is wrong | Check **Configure → Email** and `secrets.yaml`. |

## Settings

The **Memory** tab: **What Nova Knows**, **Pending Confirmation**,
**Relations** and **Person Routines**, to review, edit and forget.

Panel, **Settings → Memory**: the memory backend and the number of stored
memories. Full review lives on the Memory tab.

Panel, **Settings → Document Library**: the search backend, **UPLOAD FILE**,
**INGEST FOLDER**, a test search, and **Watch folders** with **SCAN WATCH**.

Panel, **Settings → Nova Character & Research**:

| Setting | What it does |
|---|---|
| Web research backend | DuckDuckGo, or your own SearXNG. |
| SearXNG URL | Your SearXNG server. |
| Calendar tight gap | Minutes between events treated as back to back. |

Panel, **Settings → Anticipation & Memory**:

| Setting | What it does |
|---|---|
| Memory threading | Picks a conversation up where you left off, across breaks and restarts. On by default. |
| Memory window (hrs) | After this long idle, Nova catches the conversation up again. 48 hours by default. |
| Memory max turns | How many past turns it reads back. 12 by default. |

**Configure → Email**: "Enable email reading", "IMAP host", "IMAP port",
"IMAP username / email address", "Mailbox folder" (INBOX by default), "Use
SSL", and "secrets.yaml key holding the password".
