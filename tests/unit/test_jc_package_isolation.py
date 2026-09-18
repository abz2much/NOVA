"""Regression tests for conftest.py's `_JCPackage` — the synthetic `jc`
package's sys.modules-backed attribute proxy.

Root cause this exists for: Python's real import machinery permanently
sticks a submodule onto its parent package as an attribute the first time
ANY code does a genuine `from . import X` for a name not yet in sys.modules.
Since every component module's `from . import X` resolves via
getattr(jc_package, "X") first (falling back to sys.modules only if that
attribute is unset), that first real import — triggered by loading ANY
component module that happens to import X, not necessarily a test that
cares about X at all — permanently shadows sys.modules["jc.X"] for every
later test that tries to substitute a stub there, independent of test
ordering and file collection order. See tests/conftest.py's _JCPackage
docstring and _load()'s docstring for the full history (this was a real,
repeatedly-hit test-suite fragility, not a hypothetical).

These tests exercise `jc` (the real module conftest.py installs into
sys.modules — not a fresh instance) directly, so they prove the exact
object every other test file relies on.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def jc_pkg():
    return sys.modules["jc"]


def test_attribute_access_reads_through_to_sys_modules(jc_pkg, monkeypatch):
    """The whole point of the proxy: sys.modules["jc.X"] is authoritative for
    jc.X attribute reads, regardless of what (if anything) is stored in the
    package's own __dict__."""
    stub = types.ModuleType("jc.probe_a")
    stub.marker = "stub-a"
    monkeypatch.setitem(sys.modules, "jc.probe_a", stub)
    assert jc_pkg.probe_a is stub
    assert jc_pkg.probe_a.marker == "stub-a"


def test_attribute_reverts_when_sys_modules_entry_is_removed(jc_pkg, monkeypatch):
    """monkeypatch.setitem's own teardown (deleting the sys.modules key,
    since nothing was there before) must leave jc.X genuinely absent
    afterward — not silently falling back to some other stale value."""
    stub = types.ModuleType("jc.probe_b")
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "jc.probe_b", stub)
        assert jc_pkg.probe_b is stub
    # monkeypatch.context() has reverted sys.modules["jc.probe_b"] here.
    assert "jc.probe_b" not in sys.modules
    with pytest.raises(AttributeError):
        jc_pkg.probe_b


def test_a_real_import_does_not_permanently_shadow_a_later_stub(jc_pkg, monkeypatch):
    """The exact bug this class fixes: simulate CPython's own import
    machinery sticking a real module onto jc_pkg.__dict__ as a side effect
    (what happens the first time any component module does a genuine
    `from . import X`), then prove a later test's plain
    monkeypatch.setitem(sys.modules, "jc.X", stub) still wins — the stale
    __dict__ entry must never resurface while sys.modules has a value."""
    real_looking = types.ModuleType("jc.probe_c")
    real_looking.marker = "real"
    # Simulate CPython's own "parent.child = module" side effect directly on
    # the object's __dict__ (never via _JCPackage's own __setattr__, since
    # that isn't overridden — this mirrors exactly what the import system
    # itself would do, bypassing any of our test helpers).
    jc_pkg.__dict__["probe_c"] = real_looking
    try:
        stub = types.ModuleType("jc.probe_c")
        stub.marker = "stub"
        monkeypatch.setitem(sys.modules, "jc.probe_c", stub)
        assert jc_pkg.probe_c is stub, (
            "a real module already sitting in jc_pkg.__dict__ must not "
            "shadow a test's sys.modules-based stub"
        )
        assert jc_pkg.probe_c.marker == "stub"
    finally:
        del jc_pkg.__dict__["probe_c"]


def test_dunder_and_private_names_bypass_the_proxy(jc_pkg):
    """__path__ (and any other dunder/underscore-prefixed name) must resolve
    normally — the proxy only intercepts plain submodule-shaped names, never
    the package's own bookkeeping attributes."""
    assert jc_pkg.__path__  # set once at session start; must still work
    assert jc_pkg.__name__ == "jc"


def test_setattr_on_jc_pkg_is_not_the_supported_pattern(jc_pkg, monkeypatch):
    """Documents the interaction this class's docstring warns about: pairing
    monkeypatch.setattr(jc_pkg, name, ...) with monkeypatch.setitem(sys.
    modules, f"jc.{name}", ...) in the SAME test corrupts monkeypatch's own
    snapshot (it captures the just-set sys.modules value as "the old value"
    via the proxied getattr, then restores exactly that at teardown instead
    of removing the attribute) — visible here as a __dict__ entry that
    outlives monkeypatch's own teardown. Every real test file in this suite
    uses monkeypatch.setitem(sys.modules, ...) alone, which has no such
    issue (proven by the tests above) -- this test exists only to pin down
    why the setattr companion must never be reintroduced."""
    assert "probe_d" not in jc_pkg.__dict__
    stub = types.ModuleType("jc.probe_d")
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "jc.probe_d", stub)
        m.setattr(jc_pkg, "probe_d", stub, raising=False)
    # sys.modules correctly reverted...
    assert "jc.probe_d" not in sys.modules
    # ...but the setattr call's poisoned snapshot leaves __dict__ polluted.
    assert jc_pkg.__dict__.get("probe_d") is stub
    del jc_pkg.__dict__["probe_d"]  # clean up after documenting the trap
