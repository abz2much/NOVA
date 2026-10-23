"""v6.47.1: sending a local model name to a cloud provider is corrected, and a
"model not found" error is told apart from a connection problem. This came
from a real 404, "models/gemma4:26b is not found", from Google's API after the
model name from a GPU server was set against a cloud provider."""
import pytest

GOOGLE_404 = (
    "Error code: 404 - [{'error': {'code': 404, 'message': "
    "'models/gemma4:26b is not found for API version v1main, or is not "
    "supported for generateContent.', 'status': 'NOT_FOUND'}}]")


@pytest.fixture
def route(load):
    return load("llm_provider").normalize_routing


@pytest.fixture
def agent(load):
    return load("agent")


# ── normalize_routing ────────────────────────────────────────────────────────

def test_an_ollama_tag_on_a_cloud_provider_goes_to_ollama(route):
    provider, _url, note = route("gemini", "gemma4:26b", None)
    assert provider == "ollama"
    assert note and "gemma4:26b" in note and "ollama" in note


def test_the_configured_endpoint_travels_with_the_rerouted_model(route):
    provider, url, _note = route("groq", "llama3.3:70b", "http://gpu.local:11434/v1")
    assert (provider, url) == ("ollama", "http://gpu.local:11434/v1")


@pytest.mark.parametrize("provider, model", [
    ("gemini", "gemini-2.0-flash"),
    ("groq", "llama-3.3-70b-versatile"),
    ("anthropic", "claude-sonnet-4-6"),
])
def test_a_cloud_model_on_its_cloud_provider_is_left_alone(route, provider, model):
    routed, _url, note = route(provider, model, None)
    assert routed == provider and note is None


def test_the_ollama_provider_keeps_its_own_tagged_models(route):
    routed, _url, note = route("ollama", "gemma4:26b", None)
    assert routed == "ollama" and note is None


def test_a_custom_provider_keeps_tagged_names(route):
    # A custom endpoint may well use colon tagged model names.
    routed, _url, note = route("custom", "mymodel:latest", None)
    assert routed == "custom" and note is None


def test_other_uses_of_a_colon_are_not_mistaken_for_an_ollama_tag(route):
    # A path or URL with a colon is not tag syntax.
    routed, _url, note = route("gemini", "models/gemini:pro extra", None)
    assert routed == "gemini" and note is None


# ── telling "model not found" from "cannot connect" ─────────────────────────

def test_googles_404_is_a_model_not_found(agent):
    assert agent._is_model_not_found(Exception(GOOGLE_404)) is True


@pytest.mark.parametrize("text", ["Connection timed out", "503 service unavailable"])
def test_a_connection_problem_is_not_a_model_not_found(agent, text):
    assert agent._is_model_not_found(Exception(text)) is False


def test_the_two_kinds_of_failure_never_overlap(agent):
    exc = Exception("404 model does not exist")
    assert agent._is_model_not_found(exc) is True
    assert agent._is_connectivity_error(exc) is False
