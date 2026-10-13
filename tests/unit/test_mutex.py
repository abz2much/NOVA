"""Acceptance tests for automation/mutex.py, written from its behaviour spec."""
import importlib.util
import pathlib

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def mx(load):
    return load("automation.mutex")


@pytest.fixture
def reg(mx):
    return mx.EntityLockRegistry()


def test_priority_values(mx):
    assert [(p.name, p.value) for p in mx.Priority] == [
        ("PREDICTIVE", 10), ("ROUTINE", 20), ("INTENT", 30), ("VISUAL", 40), ("SAFETY", 50)]
    assert issubclass(mx.Priority, int)


def test_package_reexports(load, mx):
    pkg = load("automation")
    assert pkg.EntityLockRegistry is mx.EntityLockRegistry
    assert pkg.LockToken is mx.LockToken
    assert pkg.Priority is mx.Priority


def test_loads_standalone_stdlib_only():
    spec = importlib.util.spec_from_file_location("spec_mutex_standalone", COMP / "automation" / "mutex.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.EntityLockRegistry().try_acquire("light.hall", 1) is not None


def test_lock_token_fields(mx):
    t = mx.LockToken("light.hall", 30)
    assert (t.entity, t.priority, t.valid) == ("light.hall", 30, True)


def test_fresh_registry_is_empty(reg):
    assert reg.is_locked("light.hall") is False
    assert reg.held_priority("light.hall") is None


def test_acquire_free(reg, mx):
    t = reg.try_acquire("light.hall", mx.Priority.ROUTINE)
    assert t is not None and t.valid is True
    assert t.entity == "light.hall"
    assert t.priority == 20 and type(t.priority) is int
    assert reg.is_locked("light.hall") is True
    assert reg.held_priority("light.hall") == 20


@pytest.mark.parametrize("held,incoming", [(30, 30), (30, 20), (50, 10), (40, 40)])
def test_equal_or_lower_refused(reg, held, incoming):
    first = reg.try_acquire("lock.back_door", held)
    assert reg.try_acquire("lock.back_door", incoming) is None
    assert first.valid is True
    assert reg.held_priority("lock.back_door") == held


def test_higher_preempts(reg, mx):
    low = reg.try_acquire("cover.blind", mx.Priority.PREDICTIVE)
    high = reg.try_acquire("cover.blind", mx.Priority.VISUAL)
    assert high is not None and high.valid is True
    assert low.valid is False
    assert reg.held_priority("cover.blind") == 40


def test_preemption_chain(reg, mx):
    a = reg.try_acquire("x.y", mx.Priority.PREDICTIVE)
    b = reg.try_acquire("x.y", mx.Priority.INTENT)
    c = reg.try_acquire("x.y", mx.Priority.SAFETY)
    assert (a.valid, b.valid, c.valid) == (False, False, True)
    assert reg.try_acquire("x.y", 99) is not None


def test_plain_int_priorities_compare(reg, mx):
    reg.try_acquire("light.hall", mx.Priority.INTENT)
    assert reg.try_acquire("light.hall", 29) is None
    t = reg.try_acquire("light.hall", 35)
    assert t is not None
    assert reg.try_acquire("light.hall", mx.Priority.INTENT) is None
    assert reg.try_acquire("light.hall", mx.Priority.VISUAL) is not None


def test_entities_independent(reg):
    reg.try_acquire("light.hall", 50)
    assert reg.try_acquire("light.landing", 10) is not None
    assert reg.try_acquire("Light.Hall", 10) is not None   # ids compared exactly


def test_release_frees(reg):
    t = reg.try_acquire("light.hall", 20)
    reg.release(t)
    assert t.valid is False
    assert reg.is_locked("light.hall") is False
    assert reg.held_priority("light.hall") is None
    assert reg.try_acquire("light.hall", 10) is not None


def test_release_none(reg):
    reg.release(None)
    t = reg.try_acquire("light.hall", 20)
    reg.release(None)
    assert t.valid and reg.is_locked("light.hall")


def test_release_preempted_does_not_disturb_holder(reg):
    old = reg.try_acquire("lock.side_gate", 10)
    new = reg.try_acquire("lock.side_gate", 50)
    reg.release(old)
    assert old.valid is False and new.valid is True
    assert reg.held_priority("lock.side_gate") == 50


def test_double_release(reg):
    t = reg.try_acquire("light.hall", 20)
    reg.release(t)
    u = reg.try_acquire("light.hall", 10)
    reg.release(t)
    assert u.valid is True and reg.held_priority("light.hall") == 10


def test_release_foreign_token(reg, mx):
    held = reg.try_acquire("light.hall", 30)
    stranger = mx.LockToken("light.hall", 30)
    reg.release(stranger)
    assert stranger.valid is False
    assert held.valid is True and reg.is_locked("light.hall")


async def test_guard_acquires_and_releases(reg, mx):
    async with reg.guard("lock.back_door", mx.Priority.INTENT) as tok:
        assert tok is not None and tok.valid
        assert reg.held_priority("lock.back_door") == 30
    assert tok.valid is False
    assert reg.is_locked("lock.back_door") is False


async def test_guard_refused_yields_none(reg, mx):
    holder = reg.try_acquire("lock.back_door", mx.Priority.SAFETY)
    async with reg.guard("lock.back_door", mx.Priority.INTENT) as tok:
        assert tok is None
    assert holder.valid is True
    assert reg.held_priority("lock.back_door") == 50


async def test_guard_releases_on_exception(reg):
    with pytest.raises(ValueError):
        async with reg.guard("light.hall", 20):
            raise ValueError("boom")
    assert reg.is_locked("light.hall") is False


async def test_guard_preempted_inside_block(reg):
    async with reg.guard("light.hall", 10) as tok:
        winner = reg.try_acquire("light.hall", 40)
        assert tok.valid is False
    assert winner.valid is True
    assert reg.held_priority("light.hall") == 40
