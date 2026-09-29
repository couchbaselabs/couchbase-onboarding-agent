from __future__ import annotations

import datetime as dt

from app.core.connectors.util import apply_type_hints, json_safe_typed, make_key, parse_iso_datetime


def test_type_hints_round_trip():
    when = dt.datetime(2026, 1, 2, 3, 4, 5)

    def hook(value):
        if isinstance(value, dt.datetime):
            return value.isoformat(), "date"
        return None

    types: dict[str, str] = {}
    body = json_safe_typed({"a": when, "nested": {"b/c": [when]}}, hook, types)
    assert body["a"] == when.isoformat()
    assert types == {"/a": "date", "/nested/b~1c/0": "date"}
    restored = apply_type_hints(body, types, lambda v, t: parse_iso_datetime(v))
    assert restored["a"] == when and restored["nested"]["b/c"][0] == when


def test_type_hints_skip_paths_that_disappeared():
    assert apply_type_hints({"x": 1}, {"/gone": "date"}, lambda v, t: 0) == {"x": 1}


def test_make_key_limits_length():
    key = make_key("c", "x" * 400)
    assert len(key.encode()) <= 250 and "~" in key
    assert make_key("c", "x" * 400) == key
