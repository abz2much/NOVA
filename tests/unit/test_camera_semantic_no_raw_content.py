"""Source-level guard: the vision-analysis hook into semantic learning must
never pass the raw analysis/summary text (the vision model's actual
description) as camera_semantic.record_event's `detail` — only the short,
fixed-vocabulary `category` label. camera.py can't be fully imported in the
unit sandbox (aiohttp + heavy HA deps chain), so this is proven at the
source-text level, same convention as test_websocket_admin_gate.py.
"""
import re
from pathlib import Path

_CAMERA_PY = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "camera.py"


def test_vision_hook_detail_is_the_category_not_raw_text():
    src = _CAMERA_PY.read_text()
    m = re.search(r'camera_semantic\.record_event\(\s*hass,\s*label=_vision_label.*?\)', src, re.S)
    assert m, "expected the vision-analysis camera_semantic.record_event call"
    call_text = m.group(0)
    assert 'detail=judgment["category"]' in call_text
    for forbidden in ("summary", "analysis"):
        assert forbidden not in call_text, (
            f"vision hook must never pass raw '{forbidden}' text into record_event"
        )


def test_bus_event_hook_detail_is_bounded_label_not_full_payload():
    src = _CAMERA_PY.read_text()
    start = src.index("camera_semantic.record_event(\n            hass, label=label")
    call_text = src[start:start + 300]
    # Only the label string itself (sliced), never the raw event `data` dict.
    assert 'data.get("label")' in call_text
    assert "detail=str(data" in call_text
    assert "**data" not in call_text and "detail=data)" not in call_text and "detail=data," not in call_text
