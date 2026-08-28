from __future__ import annotations

import copy

import pytest

from analysis import recover_crown_condition10_history as subject


def _document() -> dict:
    return {
        "schema": "crown-condition-10-result-overrides-v1",
        "verified_at": "2026-08-28T10:50:00+08:00",
        "results": [
            {
                "fixture": "1",
                "grade": "Lost",
                "terminal_status": "FT_90",
                "sources": ["https://example.com/result"],
            },
            {
                "fixture": "2",
                "grade": "PENDING",
                "terminal_status": "POSTPONED",
                "sources": ["https://example.com/postponed"],
            },
        ],
    }


def test_install_overrides_grades_only_unresolved_rows(monkeypatch):
    original = subject.engine._grade
    monkeypatch.setattr(subject.engine, "_grade", lambda match: match.get("grade"))
    overrides, prior = subject._install_audit_result_overrides(
        _document(), {"1", "2"},
    )
    assert set(overrides) == {"1", "2"}
    assert prior({"grade": ("Won", "2026-08-28T10:00:00+08:00", "x")}) == (
        "Won", "2026-08-28T10:00:00+08:00", "x",
    )
    recovered = subject.engine._grade({
        "fixture": "1", "kickoff": "2026-08-22T00:00:00+08:00",
    })
    assert recovered[0] == "Lost"
    assert recovered[1] == _document()["verified_at"]
    assert len(recovered[2]) == 64
    assert subject.engine._grade({
        "fixture": "2", "kickoff": "2026-08-22T23:00:00+08:00",
    }) is None
    monkeypatch.setattr(subject.engine, "_grade", original)


def test_override_cannot_replace_existing_grade(monkeypatch):
    monkeypatch.setattr(
        subject.engine, "_grade",
        lambda match: ("Won", "2026-08-28T10:00:00+08:00", "normal"),
    )
    subject._install_audit_result_overrides(_document(), {"1", "2"})
    with pytest.raises(ValueError, match="replace a settled grade"):
        subject.engine._grade({
            "fixture": "1", "kickoff": "2026-08-22T00:00:00+08:00",
        })


def test_override_document_is_not_mutated(monkeypatch):
    monkeypatch.setattr(subject.engine, "_grade", lambda match: None)
    document = _document()
    before = copy.deepcopy(document)
    subject._install_audit_result_overrides(document, {"1", "2"})
    assert document == before
