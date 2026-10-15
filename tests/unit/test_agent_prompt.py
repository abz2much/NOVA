"""Source guards on the agent's system prompt.

The prompt is plain text in the agent package, so these read the package
source and check that the sections Nova's behaviour depends on are still
written there: the reasoning discipline ahead of the rules, never guessing
entity ids, questions not being commands, and weather time questions going
to the forecast. Removing one by accident fails here instead of quietly
changing how Nova reasons.
"""
import pathlib

import pytest

_PKG = (pathlib.Path(__file__).resolve().parents[2]
        / "custom_components" / "nova" / "agent_runtime")
SOURCE = "\n".join(path.read_text(encoding="utf-8")
                   for path in sorted(_PKG.rglob("*.py")))


@pytest.mark.parametrize("phrase", [
    "## How you reason",
    # investigate, observe or infer, verify, confirm, fail safe, admit gaps
    "INVESTIGATE before concluding", "OBSERVE", "INFER", "VERIFY",
    "CONFIRM the result", "fail safe", "say so plainly",
    # never guess an entity id: search for it
    "Never guess entity_ids",
    # a question about a device is answered, not acted on (6.70.2)
    "Questions are not commands",
    "A question about a device is NOT a request to change it",
    "last_changed",
    # "what time will it rain" is a forecast, not the clock (6.75.0)
    "'What time' is not always the clock", "current clock time", "NEVER",
])
def test_the_prompt_still_says(phrase):
    assert phrase in SOURCE


def test_the_reasoning_discipline_comes_before_the_rules():
    assert SOURCE.index("## How you reason") < SOURCE.index("## Critical rules")


def test_the_state_tool_returns_what_the_prompt_points_at():
    # The prompt says to read last_changed, so the tool must return it.
    assert '"last_changed"' in SOURCE
    assert "state.last_changed.isoformat()" in SOURCE


def test_the_forecast_tool_the_prompt_names_exists():
    for piece in ('_tool("weather_forecast"', '"weather_forecast":',
                  "_exec_weather_forecast"):
        assert piece in SOURCE
