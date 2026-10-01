# Nova vs JARVIS: full comparison

Compared on 1 October 2026. Nova is `abz2much/NOVA` at v7.128.0. JARVIS is `sam3gp8/jarvis-aio` at v8.4.2.

Method: I read both repos locally, then used three parallel readers for architecture, JARVIS only features, and the non code layers (frontend, tests, CI, docs). I also checked both GitHub pages. Nothing was executed, so test and CI claims are from reading config and code, not from running them. Items marked **(unverified)** are the readers' inferences.

---

## 1. The key finding: Nova is a fork of JARVIS

- Nova's GitHub description says "Copy of Sam3gp8/Jarvis-aio", and Nova's licence reads "Copyright (c) 2026 sam3gp8, abz2much".
- The changelogs are identical until about v7.85.5 (242 of Nova's 385 entries match JARVIS's 325 word for word). From v7.86 they differ, so that is the fork point.
- Nova renamed everything (JARVIS to Nova, Iron Man HUD to an ember and gold "stellar core"), then re-architected it.
- JARVIS carried on separately from 7.86 to 8.4.2 under its original author.
- **Version numbers are not comparable.** JARVIS 8.4.2 does not mean "newer features". Each side has things the other lacks.

| | Nova | JARVIS |
|---|---|---|
| Maintainer | Abi Adeyoola (146 commits, solo) | sam3gp8 (352), Pascal Jerney (46), Claude (44) |
| Version | 7.128.0 | 8.4.2 |
| Commits | 146 visible (history starts at v7.112/7.113) | 442 (from June 2026) |
| GitHub | 3 stars, 0 forks | 20 stars, 3 forks, 1 open issue |
| Licence | MIT | MIT |
| Python files / lines | 195 / about 66k | 111 / about 48k |
| Test files / lines | about 316 / about 62k | about 181 / about 30k |
| Test functions | about 3,581 | about 1,935 |
| Changelog | 4,429 lines, 385 entries | 3,956 lines, 325 entries |

Nova's repo notes say it is solo maintained and under heavy change, with breaking changes possible between versions.

---

## 2. What they have in common

Same product surface, inherited from the shared ancestor:

- HA custom integration via HACS, domain `nova` or `jarvis`, runs in process, no cloud account needed.
- Conversation agent through the HA Assist pipeline, Piper voice, ESP32 satellites.
- Cognitive Core (urgency classification, reasoning, learning), offline Local Mind, intent router with local templates.
- Doorbell and package vision, Frigate and Nest, visitor learning, intrusion, lockdown, hazards, appliances, energy, briefings, reminders, goals, follow ups.
- Document RAG, read only email, web research, calendar, memory, per person patterns, suggested automations.
- Same seven panel tabs (dashboard, intrusion, logs, memory, residence, settings, suggestions), live 3D house, floor plan editor.
- 32 services, two identical blueprints (doorbell analyse, face recognised), MIT licence, `hacs.json` with HA 2024.10.
- Guiding principle "suggest, don't act".

---

## 3. Where Nova is ahead (mostly engineering and safety)

### 3.1 Architecture
- `agent.py` is a 424 line façade over `agent_runtime/` (23 files, 5,867 lines, 49 tools in `capabilities/`). JARVIS's `agent.py` is still 3,562 lines.
- Tools are classified in one registry (capability, mutates, persists, network, trust). An import time check fails the load if schemas and registry disagree, or if a mutating tool is missing from the sub agent deny list.
- Grants are enforced at dispatch, not by prompt wording. In JARVIS, `_execute_tool` has no grant check, so a model that names a tool outside its sub agent's list would still run it if the tool exists **(reader finding from code)**.
- Delegation has a depth cap of 1, a 6 turn cap, and one named profile (HOMER, read only). A mistyped profile is rejected rather than widened.
- `cognitive/` (2,058 lines): pure evaluators over an immutable event snapshot, an ordered local rule list, first match arbitration, and one coordinator allowed to cause effects. A reply that is not valid JSON counts as a provider failure and is never cached as a decision. In JARVIS, an unparseable reply becomes a silent "no" and is cached as learned silence **(reader finding)**.
- `persistence/` owns every table, with transactional upgrades and a ledger. There are no `CREATE TABLE` statements outside it (JARVIS has 19 scattered ones). Atomic JSON writes. `NovaRuntime` lives on `entry.runtime_data`, and services register once and resolve the loaded runtime at call time.

### 3.2 Providers and credentials
- `providers/` (3,346 lines): typed requests, responses and errors, one descriptor per provider, per provider credential isolation, pooled clients closed exactly once on reload, and a single `execute_chat()` boundary that records activity.
- Destination policy refuses link local and cloud metadata addresses and re checks every redirect. Model discovery is paginated and bounded. Ollama uses its native `/api/chat`.
- JARVIS's `ws_list_models` accepts a browser supplied base URL, sends the stored key to it for custom providers, and returns raw exception text. Its `websocket.py` has no admin checks at all **(reader finding from grep)**. Nova closed this in 7.106.5 and 7.107.0 and gates debug and snapshot commands behind admin (7.86 to 7.90).

### 3.3 Security and trust
- `prompt_fence.py`: one random token fence used for tool results, memory, knowledge, camera awareness and suggestion review. Pending facts need confirmation before they are trusted. JARVIS has no shared fence.
- `assist_policy.py` (662 lines) puts HA Assist tools behind Nova's own confirm gate. Mutating Assist calls get one attempt only. JARVIS sends model named HA tools straight to HA with up to three attempts and no confirmation.
- Voice cannot unlock or open without a phone tap. JARVIS's confirm gate has no voice origin parameter.
- `safety_config.py`: strict validators, automatic lockdown off by default. JARVIS defaults `lockdown_auto_on_arm` to on.
- `alarm_source.py`: one authoritative alarm entity (configured, or exactly one Alarmo panel). JARVIS iterates all alarm panels.
- Fixed stored XSS, biometrics withheld from cloud models, `execute_plan` domain allowlist (7.87).

### 3.4 Automation safety
- `automation/installation.py` is the only writer of `automations.yaml`. It validates with HA's own validator, refuses duplicates, writes atomically, reloads, confirms it loaded, and restores the original bytes on failure.
- JARVIS's `automation_creator.py` (121 lines) treats a read or parse error as an empty list and then rewrites the file with only the new automation, with no lock, atomic replace or rollback. An unreadable file could be wiped **(reader finding, lines 84 to 104)**.
- Nova also has inventory and matching of loaded automations, attribution (so it does not learn from its own or other automations), probation trials, a scoring engine (distinct days, coverage, concentration, recency), area presence gating, and an optional AI veto for suggestions (off by default, can only reject).

### 3.5 Features JARVIS does not have
- Action Audit Log (`action_log.py`): what Nova actually did, approval separate from execution result.
- Spoken History (`spoken_history.py`): last 100 confirmed announcements, repeatable.
- Habituation (`habituation.py`): three days of the same notification and it goes quiet, emergencies exempt.
- Per person honorifics (`honorific.py`). JARVIS has one global honorific.
- Host health via HA System Monitor entities, no `/proc` reads, HAOS safe.
- Solar from the HA Energy dashboard config, setup health checks, entity verification shared by agent and local engine, guided self hosted AI setup, provider activity metrics, Eufy discovery, "Nova gets a face" (7.127.0).

### 3.6 Frontend, tests and CI
- Panel: 19 source fragments plus `nova3d.js` built into one shipped file by `scripts/build_panel.py` (deterministic, `--check` fails if stale). JARVIS hand edits a 10,434 line file.
- Tests: about 2x the volume, 29 real HA integration tests (PHACC), contract tests that guard websocket, service and config flow shapes, and an evaluation harness (15 fixtures, baseline scoring). JARVIS has 1 integration test.
- CI: mypy (strict on persistence), per package coverage floors (persistence 92, audio 91, cognitive 90, vision 90, providers 86, automation 81, diagnostics 77, agent_runtime 71, intent 33), PHACC job, panel build check, architecture checks in `audit.py` (390 lines vs 221), `permissions: contents: read`.
- A smoke test of 2,604 lines against 972.

---

## 4. Where JARVIS is ahead (mostly reach and newer product features)

| Item | Detail | Nova status |
|---|---|---|
| Native Gemini | `google-genai` Interactions API: server side tool chaining, thinking toggle, retry, grounding, SDK model discovery (8.1.0) | Gemini works only through the OpenAI compatible endpoint, so no native features |
| Knowledge graph | `relations` table plus semantic recall using embeddings (8.3.0) | Facts only, no relations or embedding recall |
| Scene memory | `vision/scene_memory.py`: where last seen, changed since, inventory | Absent |
| Intrusion confinement | `intrusion_requires_confinement` master switch (8.4.0) | Absent |
| Self tuning | `feedback.py` adapts thresholds from outcomes across proactive surfaces (8.3.0) | Suggestion path only |
| Driving mode | Android Auto routes alerts to the car screen (7.89) | Absent |
| Delivery and mail | Instant porch trigger plus second look, vision carrier check, wide view exclusion, 30 minute cooldown (8.0 to 8.2.4) | Basic package handling, the newer logic was **not found (unverified)** |
| `ClosingConnection` | Fixes SQLite handle leaks (8.2.1) | Absent. Nova's `knowledge.py` uses a plain `_connect()` **(unverified leak)** |
| Paths | `paths.py` resolves the config dir | Nova hard codes `/config/nova/...` (for example `cognitive_core.py:57`), so non standard installs may break |
| Languages | 22 panel languages including four Chinese variants, 21 integration translations | 18 panel languages (no Chinese), 7 integration translations |
| Setup flow | 34 step config flow with provider menu and per model steps | 9 steps, simpler |
| Floor plan import | `sweethome3d_to_floorplan.py` and docs | Absent |
| Release tooling | Auto `release.yml` with changelog notes, `i18n_coverage.py`, traffic snapshots, PR and translation templates | Manual releases only |
| Docs | `GROWTH.md`, `KERNEL_PLAN.md` (event bus, world model, planner roadmap) | None |
| Community | 20 stars, 3 forks, contributors, in HACS default store per README | Solo, 3 stars |

Host telemetry via `/proc` and `/sys` (`host_telemetry.py`) is not a gap. Nova chose entity based host health on purpose, which suits HAOS.

---

## 5. Weaknesses on each side

**Nova**
- Still three big files: `websocket.py` 4,221, `cognitive_core.py` 3,974, `nova-panel.js` 8,286 (generated). Only the decision path was extracted from the core.
- `agent_runtime` has the second lowest coverage floor (71%).
- Dispatcher returns `str(exc)` to the model, which could leak library detail.
- Retry classification matches substrings in error text ("500", "429").
- `installation.py` rewrites `automations.yaml` through PyYAML, which may drop comments **(unverified)**.
- Pattern timestamps use local time, so DST fall back hours can be an hour out (noted in its own changelog).
- Config entry diagnostics redact by key name only.
- `eufy.py`'s docstring appears to contain a real device serial, which would contradict the 7.120.3 hygiene claim **(unverified, worth a check)**.
- Minimal community, no auto release, and a short integration translation set.
- Breaking changes are expected, per its README.

**JARVIS**
- Giant flat modules: `agent.py` 3,562, `cognitive_core.py` 3,623, `websocket.py` 3,102, panel 10,434. Functions of 200 to 350 lines (`ws_get_panel_data` about 349).
- No type checking or coverage gate in CI, only one integration test. Its own `KERNEL_PLAN.md` says HA lifecycle tests were blocked.
- The security gaps in section 3 (no websocket admin checks, no shared prompt fence, no grant check in `_execute_tool`, unguarded automation writes). I have not exploited any of them. They are from code reading.
- Heavy patch churn (many 7.99.x and 8.x releases in days), though the changelog is thorough.
- `google-genai` pin has already broken hassfest once (HA core bundles a newer version).
- Daily traffic job needs a personal access token that someone must renew.
- Both repos use `except Exception` heavily (1,049 in JARVIS, 1,261 in Nova).

---

## 6. Side by side summary

| Area | Nova | JARVIS | Edge |
|---|---|---|---|
| Architecture | Packages, typed contracts, import time invariants | Flat monoliths | Nova |
| Security posture | Fenced prompts, grants, admin gated websocket, Assist policy | Mostly prompt and per module guards | Nova |
| Provider layer | Typed, isolated, SSRF aware | One file, native Gemini | Nova (JARVIS for Gemini) |
| Automation writes | Validated, atomic, rollback | Plain overwrite | Nova |
| Product features | Audit log, honorifics, habituation, host health, solar | Graph, scene memory, driving, confinement, delivery logic | Split |
| Panel | Modular and built | Monolith | Nova |
| Languages | 18 panel, 7 backend | 22 panel, 21 backend | JARVIS |
| Setup experience | Short flow | Rich per provider flow | JARVIS |
| Tests and CI | Deeper (mypy, coverage, PHACC, contracts, evals) | Lighter | Nova |
| Release and community tooling | Manual | Auto release, i18n check, templates | JARVIS |
| Community and visibility | Very small | Small but real | JARVIS |

---

## 7. Suggested next steps for Nova (judgement, not requested work)

1. Port `ClosingConnection` (tiny) and check `knowledge.py` for leaked connections.
2. Add `paths.py` style config dir resolution in place of hard coded `/config/nova`.
3. Scene memory with a `where_last_seen` tool.
4. Knowledge relations and semantic recall, reusing Nova's fencing.
5. Optional intrusion confinement switch.
6. Driving mode.
7. Native Gemini behind the `providers/` interface.
8. Chinese panel files and the 14 missing backend translations.
9. Auto release workflow and `extract_changelog.py`, PR and translation templates.
10. Check the `eufy.py` serial and the delivery and mail logic gaps.

For JARVIS, the highest value borrowings from Nova would be admin gating on websocket commands, safe automation installation, the prompt fence, grants at dispatch, and mypy plus coverage gates.

---

## 8. Not verified

- No tests, builds or importers were run in either repo.
- Nova's package monitor was not read end to end, so the claim that it lacks JARVIS's 8.x delivery logic is likely but unproven.
- JARVIS's 8.x changelog was not compared line by line against every Nova fix, so some of Nova's gaps may already be closed.
- Websocket admin coverage in Nova was not audited command by command.
- Translation completeness was not measured.
- GitHub star and fork counts are from page fetches on 1 October 2026 and will change.

Sources: [sam3gp8/jarvis-aio](https://github.com/sam3gp8/jarvis-aio), [abz2much/NOVA](https://github.com/abz2much/NOVA), plus the local code in both repos.
