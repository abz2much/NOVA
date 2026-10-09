"""Panel text set after the first render goes through the translator (8.17.0).

The first render is translated by _localizeDOM. Anything set later must use
the helpers in frontend/src/panel/render.js, or no translation reaches it:

  _setHtml(el, html)   markup: sets it, then translates the new text nodes
  _setText(el, text)   plain text
  _tx(text)            one whole string, for a title or a dialog message
  _t / _tHtml          text with a value inside it

A line that must set text directly says why with "// i18n-ok: <reason>".
"""
from __future__ import annotations

import pathlib
import re

import pytest

PANEL = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "panel"

RAW = [
    (re.compile(r"\.(?:innerHTML|outerHTML|textContent|innerText)\s*=(?!=)"), "sets text directly"),
    (re.compile(r"\binsertAdjacent(?:HTML|Text)\("), "inserts text directly"),
    (re.compile(r"\.(?:title|placeholder)\s*=(?!=)(?!\s*(?:this|self)\._t[xH]?\w*\()"), "sets a title without the translator"),
    (re.compile(r"setAttribute\(\s*[\"'](?:title|placeholder|aria-label)[\"']\s*,(?!\s*(?:this|self)\._t)"),
     "sets a title without the translator"),
    (re.compile(r"\b(?:confirm|alert|prompt)\((?!\s*(?:this|self)\._t[xH]?\w*\()"), "shows a dialog without the translator"),
]


def _offences(text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("//", 1)[0] if "i18n-ok:" not in line else ""
        if not code.strip() or code.lstrip().startswith("*"):
            continue
        for pattern, why in RAW:
            if pattern.search(code):
                out.append(f"{n}: {why}: {line.strip()[:120]}")
    return out


@pytest.mark.parametrize("path", sorted(PANEL.glob("*.js")), ids=lambda p: p.name)
def test_late_text_goes_through_the_translator(path):
    bad = _offences(path.read_text(encoding="utf-8"))
    assert not bad, (
        f"{path.name}: use _setHtml, _setText, _tx or _t, or say why with // i18n-ok: \n  "
        + "\n  ".join(bad))


def test_the_check_catches_each_kind_of_direct_text():
    for line in ('el.innerHTML = "<b>x</b>";', 'el.textContent = "Saved.";',
                 'btn.title = "Help";', 'if (!window.confirm("Sure?")) return;',
                 'el.setAttribute("title", "Help");', 'el.insertAdjacentHTML("beforeend", x);'):
        assert _offences(line), line
    for line in ('this._setText(el, "Saved.");', 'btn.title = this._tx("Help");',
                 'if (!window.confirm(this._tx("Sure?"))) return;',
                 'if (el.textContent === t) return;', 'node.textContent = x; // i18n-ok: typing effect'):
        assert not _offences(line), line
