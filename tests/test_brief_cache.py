"""Morning brief cache: one generate call per input set, never one per Streamlit rerun."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def dash(monkeypatch):
    module = importlib.import_module("src.app.dashboard")
    calls = []

    def fake_generate(snapshot, mode="auto"):
        calls.append(snapshot)
        return "brief text", "llm"

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr("src.brief.generate.generate_brief", fake_generate)
    monkeypatch.setattr("src.brief.snapshot.build_snapshot", lambda conn, **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(
        "evals.verify.verify_brief",
        lambda brief, snap: SimpleNamespace(passed=True, errors=[], warnings=["w"], summary=lambda: "PASS"),
    )
    monkeypatch.setattr(module.da, "connect", lambda: FakeConn())
    module._morning_brief.clear()
    yield module, calls
    module._morning_brief.clear()


def test_same_inputs_call_generate_once(dash):
    module, calls = dash
    first = module._morning_brief("2026-07-29", 0.01, 48, 42, 5.625)
    second = module._morning_brief("2026-07-29", 0.01, 48, 42, 5.625)
    assert len(calls) == 1
    assert first == second
    assert first["brief"] == "brief text" and first["source"] == "llm"
    assert first["warnings"] == ["w"]


def test_changed_input_or_new_day_regenerates(dash):
    module, calls = dash
    module._morning_brief("2026-07-29", 0.01, 48, 42, 5.625)
    module._morning_brief("2026-07-29", 0.01, 48, 42, 5.5)
    module._morning_brief("2026-07-30", 0.01, 48, 42, 5.625)
    assert len(calls) == 3
