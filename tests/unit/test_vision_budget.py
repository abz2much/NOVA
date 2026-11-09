"""The daily limit on the camera analyses Nova starts by itself (vision_budget).

A busy camera, a faulty sensor or a storm of motion events must not be able to
run up an unlimited bill with a paid vision model. Each camera already has a
gap between its own reactions; this is the one limit over all of them.
"""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def vb(load):
    mod = load("vision_budget")
    mod.reset()
    yield mod
    mod.reset()


NOON = datetime(2026, 11, 9, 12, 0)


def test_the_default_limit_is_three_hundred_a_day(vb):
    assert vb.DEFAULT_DAILY_LIMIT == 300
    assert vb.limit_from(None) == 300


@pytest.mark.parametrize("raw, expected", [
    (50, 50), ("50", 50), (50.0, 50), ("  75 ", 75),
    (0, 0), ("0", 0),                      # zero means no limit
    (-5, 300), ("many", 300), ("", 300), (None, 300), (True, 300),
    (float("nan"), 300), (float("inf"), 300),
])
def test_limit_from(vb, raw, expected):
    assert vb.limit_from(raw) == expected


def test_it_allows_up_to_the_limit_then_refuses(vb):
    assert [vb.take(3, now=NOON) for _ in range(5)] == [True, True, True, False, False]
    assert vb.used(now=NOON) == 3


def test_a_refused_call_is_not_counted(vb):
    for _ in range(4):
        vb.take(2, now=NOON)
    assert vb.used(now=NOON) == 2


def test_exhausted_looks_without_using_a_call(vb):
    assert vb.exhausted(2, now=NOON) is False
    assert vb.exhausted(2, now=NOON) is False
    assert vb.used(now=NOON) == 0
    vb.take(2, now=NOON)
    vb.take(2, now=NOON)
    assert vb.exhausted(2, now=NOON) is True


def test_zero_means_no_limit(vb):
    assert all(vb.take(0, now=NOON) for _ in range(1000))
    assert vb.exhausted(0, now=NOON) is False


def test_the_count_starts_again_the_next_day(vb):
    vb.take(1, now=NOON)
    assert vb.take(1, now=NOON) is False
    tomorrow = NOON + timedelta(days=1)
    assert vb.take(1, now=tomorrow) is True


def test_the_limit_can_be_raised_during_the_day(vb):
    vb.take(1, now=NOON)
    assert vb.take(1, now=NOON) is False
    assert vb.take(5, now=NOON) is True


def test_the_refusal_is_reported_once_a_day(vb):
    vb.take(1, now=NOON)
    assert vb.note_refusal(now=NOON) is True
    assert vb.note_refusal(now=NOON) is False
    assert vb.note_refusal(now=NOON + timedelta(days=1)) is True


def test_status_for_diagnostics(vb):
    vb.take(10, now=NOON)
    assert vb.status(10, now=NOON) == {"limit": 10, "used": 1, "left": 9}
    assert vb.status(0, now=NOON) == {"limit": 0, "used": 1, "left": None}
