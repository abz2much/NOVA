"""speech_text: markdown from the model must never reach the speaker or the
Spoken History panel (both show / read the text literally)."""
from __future__ import annotations


def test_real_reply_from_spoken_history(load):
    st = load("speech_text")
    raw = ("I'm not quite following, Sir. Are you asking me to: - **Control "
           "something in the home** — lights, climate, media? - **Check a "
           "status** — what's happening in a room or with a device? - "
           "**Something else entirely**? If you could clarify, I'm ready to help.")
    out = st.speech_text(raw)
    assert "*" not in out
    assert " - " not in out
    assert "Control something in the home — lights, climate, media?" in out


def test_newline_bullets_and_headings(load):
    st = load("speech_text")
    out = st.speech_text("## Options\n- **Weather**\n- Your schedule\n1. Home status")
    assert "#" not in out and "*" not in out
    assert "\n" not in out
    assert "Weather" in out and "Home status" in out


def test_plain_text_untouched(load):
    st = load("speech_text")
    s = "Welcome home, sir. The morning is cloudy at 12.5°C."
    assert st.speech_text(s) == s


def test_non_string_and_empty_pass_through(load):
    st = load("speech_text")
    assert st.speech_text("") == ""
    assert st.speech_text(None) is None


def test_links_and_code_flattened(load):
    st = load("speech_text")
    assert st.speech_text("See [the docs](http://x.y) and `lights`") == "See the docs and lights"
