# Phase 8: work left for later

Phase 8 is making Nova independently written, with no copied runs left from
the code it was forked from. On 15 October 2026 it was reprioritised: the
files with the most copied code, and the ones that matter most for how Nova
behaves, were rewritten (8.49.31 to 8.49.43). Everything below was left for
a later phase on purpose. Nothing here is broken; it still carries copied
runs of 6 or more lines.

A "run" is 6 or more lines in a row that also appear together in the fork
source. The counts come from the copied run scan after 8.49.43.

## How to pick this up

Use the same method as Phase 8: rewrite each file with its copied text
removed, keep every public name, message, key, command schema and stored
value, keep behaviour identical unless a fix is agreed, then rescan. Start
from the largest files in each table.

## Integration code: 48 files, 923 lines in runs

Small files, 6 to 40 copied lines each.

| File | Lines in runs | Longest run |
|---|---|---|
| `mode_scene.py` | 40 | 18 |
| `agent_runtime/ha_tools.py` | 40 | 14 |
| `automation/installation.py` | 40 | 9 |
| `core_common.py` | 39 | 20 |
| `memory.py` | 36 | 9 |
| `agent_runtime/registry.py` | 36 | 18 |
| `agent_runtime/capabilities/memory.py` | 36 | 11 |
| `agent_runtime/capabilities/diagnostics.py` | 33 | 17 |
| `directive_helper.py` | 31 | 16 |
| `conversation.py` | 31 | 18 |
| `providers/anthropic.py` | 31 | 25 |
| `agent_runtime/grants.py` | 31 | 15 |
| `automation/trials.py` | 30 | 8 |
| `core_delivery.py` | 28 | 20 |
| `core_autonomy.py` | 27 | 11 |
| `memory_thread.py` | 26 | 16 |
| `person_patterns.py` | 26 | 14 |
| `agent_runtime/capabilities/communications.py` | 26 | 12 |
| `core_state.py` | 25 | 15 |
| `agent_runtime/capabilities/automation.py` | 21 | 8 |
| `agent_runtime/capabilities/safety_modes.py` | 19 | 12 |
| `websocket.py` | 18 | 6 |
| `providers/routing.py` | 18 | 12 |
| `agent_runtime/dispatcher.py` | 17 | 11 |
| `ws_voice.py` | 16 | 16 |
| `cognitive/coordinator.py` | 15 | 9 |
| `provider_activity.py` | 14 | 8 |
| `agent_runtime/delegation.py` | 14 | 8 |
| `panel_register.py` | 13 | 7 |
| `proactive_audio.py` | 12 | 6 |
| `cognitive_core.py` | 11 | 11 |
| `providers/errors.py` | 9 | 9 |
| `automation/patterns.py` | 9 | 9 |
| `automation/models.py` | 9 | 9 |
| `output_language.py` | 8 | 8 |
| `providers/base.py` | 8 | 8 |
| `providers/ollama.py` | 8 | 8 |
| `cognitive/provider.py` | 8 | 8 |
| `camera_backends.py` | 7 | 7 |
| `voice_recognition.py` | 7 | 7 |
| `local_engine.py` | 7 | 7 |
| `local_mind.py` | 7 | 7 |
| `observer.py` | 6 | 6 |
| `appliance_monitor.py` | 6 | 6 |
| `bootstrap.py` | 6 | 6 |
| `state_ledger.py` | 6 | 6 |
| `host_health.py` | 6 | 6 |
| `intent/__init__.py` | 6 | 6 |

## Tests: 115 files, 6989 lines in runs

The old tests were left whole, apart from those rewritten alongside their
module in each batch. The five largest are test_intrusion_investigation.py,
test_service_health.py, test_decision_record.py, test_pattern_analyzer.py
and test_camera_probe.py. Rewrite each test file together with the module
it tests, and never weaken what it checks.

## Panel source: 9 files, 274 lines in runs

Mostly the floor plan editor. The built panel
(`custom_components/nova/frontend/nova-panel.js`) is generated from these
files and clears when they do.

| File | Lines in runs | Longest run |
|---|---|---|
| `frontend/src/panel/floor-plan-editor.js` | 118 | 13 |
| `frontend/src/panel/floor-plan-geometry.js` | 69 | 28 |
| `frontend/src/panel/memory.js` | 28 | 9 |
| `frontend/src/panel/floor-plan.js` | 27 | 9 |
| `frontend/src/panel/settings-cards-more.js` | 7 | 7 |
| `frontend/src/panel/logs.js` | 7 | 7 |
| `frontend/src/panel/core.js` | 6 | 6 |
| `frontend/src/panel/live-data.js` | 6 | 6 |
| `frontend/src/panel/intrusion.js` | 6 | 6 |

## Panel translations: 10 files, 400 lines in runs

The same 40 line block in each language file under
`custom_components/nova/frontend/i18n/`.

## Config text: 10 files, 258 lines in runs

services.yaml field schemas, setup translations and two blueprints. Most of
this is the public contract that users' automations rely on, so it must
stay as it is. Only the wording that is not part of the contract can change.

## Repo tooling

| File | Lines in runs |
|---|---|
| `scripts/audit.py` | 179 |
| `scripts/smoke_panel.js` | 124 |
| `.github/workflows/validate.yml` | 69 |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | 47 |
| `CONTRIBUTING.md` | 23 |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | 20 |
| `custom_components/nova/manifest.json` | 6 |

## Not code work

- **CHANGELOG.md**: the old release notes carry 2970 lines in
  runs. Whether to keep, trim or rewrite them is the maintainer's call.
- **README.md and LICENSE** stay as they are (MIT attribution); no change
  without legal advice.

## Still to do once the above is done

- A final whole repo copied run scan.
- The shadow review (report only).
