"""Chinese translations (8.19.0).

Traditional Chinese (zh-Hant) uses Taiwan style wording, as Home Assistant's
own zh-Hant does, and must contain no Simplified characters. The list in
zh_simplified_only.txt is every character OpenCC's Taiwan conversion (s2tw)
changes, less ten that are standard in Taiwan too (台里污准岩托游踪霉干).

Both Chinese scripts use full width punctuation next to Chinese text.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMP = ROOT / "custom_components" / "nova"
SIMPLIFIED = set((pathlib.Path(__file__).parent / "zh_simplified_only.txt").read_text(encoding="utf-8").strip())

TRADITIONAL = [COMP / "frontend" / "i18n" / "zh-hant.json", COMP / "translations" / "zh-Hant.json"]
CHINESE = TRADITIONAL + [COMP / "frontend" / "i18n" / "zh.json", COMP / "translations" / "zh-Hans.json"]
CJK = r"[一-鿿]"


def _values(path: pathlib.Path):
    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, str):
            yield node
    return list(walk(json.loads(path.read_text(encoding="utf-8"))))


def _simplified_in(text: str) -> list[str]:
    return sorted({c for c in text if c in SIMPLIFIED})


@pytest.mark.parametrize("path", TRADITIONAL, ids=lambda p: p.name)
def test_traditional_has_no_simplified_characters(path):
    bad = {v: _simplified_in(v) for v in _values(path) if _simplified_in(v)}
    assert not bad, f"{path.name} has Simplified characters: {list(bad.items())[:10]}"


@pytest.mark.parametrize("path", CHINESE, ids=lambda p: p.parent.name + "/" + p.name)
def test_full_width_punctuation_next_to_chinese(path):
    # A comma, full stop, colon, semicolon, question or exclamation mark
    # straight after a Chinese character must be the full width form.
    bad = [v for v in _values(path) if re.search(CJK + r"[,.:;?!]", v)]
    assert not bad, f"{path.name} uses half width punctuation after Chinese text: {bad[:10]}"


def test_the_checks_catch_what_they_should():
    assert _simplified_in("设置") and not _simplified_in("設定")
    assert re.search(CJK + r"[,.:;?!]", "已保存.") and not re.search(CJK + r"[,.:;?!]", "已保存。")
