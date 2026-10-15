"""A provider's "request too large" (413) and the slimmer retry.

The agent must recognise size errors and nothing else, trim the home state
out of the prompt for the retry while keeping every other section, and
offer a tool list small enough to fit.
"""
import json

import pytest

HOME = "## Current home state\n"


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.mark.parametrize("text", [
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand`'}}",
    "maximum context length exceeded",
    "This prompt is too long",
    "HTTP 413 Payload Too Large",
])
def test_size_errors_are_recognised(agent, text):
    assert agent._is_too_large(Exception(text)) is True


@pytest.mark.parametrize("text", [
    "completed using 413 tokens",      # a number, not a status
    "Connection timed out",            # connectivity has its own path
    "model not found",
])
def test_other_errors_are_not_size_errors(agent, text):
    assert agent._is_too_large(Exception(text)) is False


def test_the_home_state_goes_and_the_other_sections_stay(agent):
    prompt = ("Persona line.\n\n" + HOME + "Areas: Hall, Study\nLights: p, q\n\n"
              "## Tools\nCall control_device.\n")
    trimmed = agent._strip_home_state(prompt)
    assert "Areas: Hall" not in trimmed and "Lights: p, q" not in trimmed
    assert trimmed.startswith("Persona line.\n\n" + HOME)
    assert trimmed.endswith("## Tools\nCall control_device.\n")
    assert "search_entities" in trimmed


def test_the_home_state_as_the_last_section_goes_too(agent):
    trimmed = agent._strip_home_state("Persona line.\n\n" + HOME + "Lights: p\n")
    assert "Lights: p" not in trimmed and trimmed.startswith("Persona line.")


def test_a_prompt_without_home_state_is_left_alone(agent):
    prompt = "Persona line.\n\n## Tools\nCall control_device.\n"
    assert agent._strip_home_state(prompt) == prompt


def test_a_missing_gemini_thought_signature_is_a_tool_format_error(agent):
    # Answered without tools, never taken as being offline (which would trip
    # the breaker).
    exc = Exception("400 - Function call is missing a thought_signature in "
                    "functionCall parts. This is required for tools to work correctly.")
    assert agent._is_tool_format_error(exc) is True
    assert agent._is_connectivity_error(exc) is False


def test_the_slim_tool_list_is_real_and_much_smaller(agent):
    every = {spec["function"]["name"] for spec in agent.NOVA_TOOLS}
    assert agent._SLIM_TOOLS <= every
    slim = agent._scoped_tool_list(agent._SLIM_TOOLS)
    assert len(slim) == len(agent._SLIM_TOOLS)
    # The retry only helps if it is far smaller than the full request.
    assert 3 * len(json.dumps(slim)) < len(json.dumps(agent._scoped_tool_list(None)))
