"""Learned aliases are read off the event loop (v8.7.1).

HA flagged _find_entity opening the learned-aliases file inside the event
loop. try_local now reads the file once in the executor and passes the
aliases in; _find_entity itself never touches the disk.
"""
import builtins

import pytest

from fakes import FakeHass


@pytest.fixture
def le(load):
    return load("local_engine")


def test_find_entity_uses_given_aliases_without_reading_disk(le, monkeypatch):
    hass = FakeHass()
    hass.states.set("light.study_lamp", "off", friendly_name="Study Lamp")

    def _no_open(*a, **k):
        raise AssertionError("_find_entity opened a file")

    monkeypatch.setattr(builtins, "open", _no_open)
    assert le._find_entity(hass, "desk", None, aliases={"desk": "light.study_lamp"}) \
        == ("light.study_lamp", "Study Lamp")


def test_find_entity_without_aliases_still_matches_by_name(le, monkeypatch):
    hass = FakeHass()
    hass.states.set("light.study_lamp", "off", friendly_name="Study Lamp")
    monkeypatch.setattr(builtins, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("open")))
    assert le._find_entity(hass, "study lamp", "light") == ("light.study_lamp", "Study Lamp")


async def test_try_local_reads_aliases_in_the_executor(le, monkeypatch):
    hass = FakeHass()
    hass.states.set("light.study_lamp", "off", friendly_name="Study Lamp")
    jobs = []
    real = hass.async_add_executor_job

    async def _record(func, *args):
        jobs.append(func.__name__)
        return await real(func, *args)

    monkeypatch.setattr(hass, "async_add_executor_job", _record)
    monkeypatch.setattr(le, "_read_aliases", lambda: {"desk": "light.study_lamp"})
    result = await le.try_local(hass, "desk")   # a bare name is a state query
    assert jobs == ["<lambda>"]                  # the aliases, read in the executor
    assert result is not None and "study lamp" in result.text.lower()
