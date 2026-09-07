from __future__ import annotations

from tests.gateway.test_ad_template_final_repair_selection import _run_final_repair_case


def test_final_patch_renderer_contract_failure_retries_through_real_orchestrator(tmp_path, monkeypatch):
    trace = {}
    result, imported, calls, events = _run_final_repair_case(
        tmp_path, monkeypatch, repair_score=9.8, comparison_to_best="same",
        icon_repair=True, trace=trace,
    )
    assert calls.count("final-merged-patch") == 1
    assert calls.count("final-merged-patch-format-retry") == 1
    assert trace["render_checks"][0] == "check-square"
    assert set(trace["render_checks"][1:]) == {"check"}
    assert imported == ["repaired-candidate"]
    icons = [layer for layer in result["template"]["feedLayout"]["layers"] if layer["type"] == "icon"]
    assert [layer["icon"] for layer in icons] == ["check"]
    assert any(kind == "role.output-retried" for kind, _, _ in events)
    assert result["final_review"]["decision"] == "accepted"
    assert result["import"]["library_status"] == "quarantined"
    assert result["reusable_validation"]["counts"] == {"total": 4, "passed": 4, "failed": 0}
