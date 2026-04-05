from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json

import pytest

from src.llm import (
    _safe_json_loads,
    _extract_test_cases_array,
    _extract_json_objects,
    _recover_cases_batch_from_raw,
)


def test_safe_json_loads_simple_object():
    payload = '{"a": 1, "b": "x"}'
    assert _safe_json_loads(payload) == {"a": 1, "b": "x"}


def test_safe_json_loads_markdown_block_and_trailing_comma():
    raw = """```json
{
  "a": 1,
  "b": [1, 2, 3,],
}
```"""
    data = _safe_json_loads(raw)
    assert data["a"] == 1
    assert data["b"] == [1, 2, 3]


def test_extract_test_cases_array_and_objects():
    raw = json.dumps(
        {
            "meta": "x",
            "test_cases": [
                {"id": "1", "title": "T1", "steps": "s", "expected": "e"},
                {"id": "2", "title": "T2", "steps": "s2", "expected": "e2"},
            ],
        }
    )
    arr = _extract_test_cases_array(raw)
    assert arr.startswith("[") and arr.endswith("]")
    objs = _extract_json_objects(arr)
    assert len(objs) == 2
    parsed = [_safe_json_loads(o) for o in objs]
    assert {p["id"] for p in parsed} == {"1", "2"}


def test_recover_cases_batch_from_raw_partial_recovery():
    # 第二个对象 deliberately 搞坏，确保恢复逻辑能跳过坏对象保留好对象
    raw = """
{
  "test_cases": [
    {
      "id": "TC-001",
      "priority": "P0",
      "module": "M1",
      "title": "OK",
      "summary": "sum",
      "preconditions": "pre",
      "steps": ["s1", "s2"],
      "expected": ["e1"],
      "actual_result": "",
      "test_type": "功能",
      "data": "",
      "remarks": ""
    },
    {
      "id": "TC-002",
      "priority": "P0",
      "module": "M1",
      "title": "BAD",
      "summary": "sum",
      "preconditions": "pre",
      "steps": "not-a-list",
      "expected": ["e1"],
      "actual_result": "",
      "test_type": "功能",
      "data": "",
      "remarks": ""
    }
  ]
}
"""
    recovered = _recover_cases_batch_from_raw(raw)
    cases = recovered["test_cases"]
    # 至少恢复一个合法的用例
    assert len(cases) >= 1
    ids = {c["id"] for c in cases}
    assert "TC-001" in ids

