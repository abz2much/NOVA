"""File reads Home Assistant flagged as blocking the event loop (v8.6.1).

HA logged 'Detected blocking call to open ... inside the event loop' for the
doorbell training log (read in full on every panel poll) and habituation.json
(first read from the cognitive status handler). Pins the fixes: the panel
offloads the doorbell read to the executor, and setup pre-warms habituation's
cache off the loop, so its later on-loop reads never touch the disk."""
from __future__ import annotations

import ast
import pathlib

from ws_sources import ws_function

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _func(path, name):
    tree = ast.parse((COMP / path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_panel_data_reads_doorbell_log_in_executor():
    src = ast.unparse(ws_function("ws_get_panel_data"))
    assert "async_add_executor_job(_get_doorbell_training, hass)" in src
    assert "_get_doorbell_training(hass)" not in src.replace(
        "async_add_executor_job(_get_doorbell_training, hass)", "")


def test_setup_prewarms_habituation_off_the_loop():
    src = ast.unparse(_func("__init__.py", "_prewarm_persisted_state"))
    assert "habituation._load()" in src
    setup = ast.unparse(_func("__init__.py", "async_setup_entry"))
    assert "async_add_executor_job(_prewarm_persisted_state)" in setup


def test_habituation_load_is_cached_after_first_read(load, tmp_path, monkeypatch):
    hab = load("habituation")
    path = tmp_path / "habituation.json"
    path.write_text('{"k": {"quiet": true}}')
    monkeypatch.setattr(hab, "STATE_FILE", str(path))
    monkeypatch.setattr(hab, "_state", None)
    assert hab._load() == {"k": {"quiet": True}}
    path.unlink()                    # a second read would now find nothing
    assert hab._load() == {"k": {"quiet": True}}
