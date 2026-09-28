"""Spoken briefings must be grounded in the facts Nova gathered (v7.127.1).

The arrival briefing on 2026-09-28 invented unread messages from an
accountant, a dental appointment and a dripping tap, and called a thermostat
keypad lock "the front door". Neither briefing prompt told the model to stick
to the context it was given, and the proactive prompt even supplied a sample
reading ("18 degrees") for it to echo.
"""
from pathlib import Path

_SRC = Path("custom_components/nova")


def test_rule_forbids_invented_items(load):
    rule = load("const").BRIEFING_GROUNDING_RULE
    assert "ONLY the facts given in the context" in rule
    for item in ("messages", "appointments", "maintenance", "temperatures"):
        assert item in rule


def test_both_briefing_prompts_carry_the_rule():
    for name in ("briefing.py", "proactive_briefing.py"):
        src = (_SRC / name).read_text()
        assert "{BRIEFING_GROUNDING_RULE}" in src, name
        assert "temperature=0.6" not in src, name


def test_proactive_prompt_supplies_no_sample_reading():
    src = (_SRC / "proactive_briefing.py").read_text()
    assert "18 degrees" not in src
