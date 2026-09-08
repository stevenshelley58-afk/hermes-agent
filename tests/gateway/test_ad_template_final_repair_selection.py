from __future__ import annotations

import copy
from pathlib import Path

from PIL import Image
import pytest

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _comparison, _review, _template


def _reusable_validation():
    names = ("short", "max", "unicode", "optional-empty")
    return {
        "schema": "blockwise.reusable-template-validation.v1",
        "status": "passed",
        "scenarioLimit": 4,
        "counts": {"total": 4, "passed": 4, "failed": 0},
        "scenarios": [
            {"name": name, "identity": f"identity-{name}", "status": "passed"}
            for name in names
        ],
    }


def _run_final_repair_case(tmp_path, monkeypatch, *, repair_score, comparison_to_best, icon_repair=False, trace=None):
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1350), "white").save(source)
    original = {"template": _template(), "assets": []}
    trace = trace if trace is not None else {}
    calls = trace.setdefault("calls", [])
    events = trace.setdefault("events", [])
    imported = trace.setdefault("imported", [])
    render_checks = trace.setdefault("render_checks", [])

    monkeypatch.setattr(
        process,
        "vision_message",
        lambda text, paths, **_kwargs: [
            {"type": "text", "text": text},
            {"type": "test_paths", "paths": list(paths)},
        ],
    )
    monkeypatch.setattr(process, "build_source_map", lambda _source: {
        "sourceMapVersion": process.SOURCE_MAP_VERSION,
        "ocrStatus": "not_run",
        "ocr": [],
        "edgeRegions": [],
    })
    monkeypatch.setattr(
        process,
        "materialize_source_photo_plan",
        lambda **_kwargs: str(tmp_path / "photo-plan.json"),
    )
    monkeypatch.setattr(
        process,
        "build_ephemeral_qa_candidate",
        lambda candidate, **_kwargs: (candidate, {}),
    )
    monkeypatch.setattr(
        process,
        "prepare_demo_assets",
        lambda candidate, **_kwargs: (candidate, {}),
    )
    monkeypatch.setattr(process, "_comparison_views", lambda *args, **kwargs: [])
    monkeypatch.setattr(process, "_comparison_metrics", lambda **kwargs: {})
    monkeypatch.setattr(
        process,
        "validate_reusable_template",
        lambda *args, **kwargs: _reusable_validation(),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda candidate, **kwargs: (
            imported.append(candidate["template"]["metadata"]["description"])
            or {
                "template_id": candidate["template"]["templateId"],
                "status": "imported",
                "asset_count": 0,
                "replayed": False,
                "library_status": "quarantined",
                "run_id": kwargs["run_id"],
            }
        ),
    )
    monkeypatch.setattr(
        process,
        "review_template_action",
        lambda **kwargs: {"templateId": kwargs["template_id"], "status": "passed"},
    )

    def render(candidate, workspace, *, asset_overrides=None):
        del asset_overrides
        current_description[0] = candidate["template"]["metadata"]["description"]
        for layer in candidate["template"]["feedLayout"]["layers"]:
            if layer.get("type") == "icon":
                render_checks.append(layer["icon"])
                if layer["icon"] == "check-square":
                    raise process.AdTemplateRendererRejection([
                        "feedLayout icon must be arrow, check, tick, phone, mail, globe or location",
                    ])
        workspace.mkdir(parents=True, exist_ok=True)
        feed = workspace / "feed.png"
        story = workspace / "story.png"
        Image.new("RGB", (1080, 1350), "white").save(feed)
        Image.new("RGB", (1080, 1920), "white").save(story)
        artifact = workspace / "artifact.json"
        artifact.write_text(process._safe_json(candidate), encoding="utf-8")
        return {
            "render": {"feed": str(feed), "story": str(story)},
            "previews": [],
            "review_previews": [],
            "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_renderer", render)
    current_description = ["initial"]

    def call_agent(instance, prompt, route):
        del prompt, route
        calls.append(instance)
        if instance == "aspect-reference":
            return {
                "sourcePlacement": "feed",
                "targetPlacement": "story",
                "canvas": {"width": 1080, "height": 1920},
                "regions": [{
                    "regionId": "main",
                    "sourceRole": "main",
                    "target": {"x": 0, "y": 0, "width": 1080, "height": 1920},
                    "zIndex": 0,
                }],
                "preserve": ["all visible geometry and effects"],
            }
        if instance == "builder-initial":
            return copy.deepcopy(original)
        if instance.startswith("comparator-") and instance != "comparator-final-repair":
            return _comparison(accept=True, comparison_to_best="not_applicable")
        if instance.startswith("final-reviewer-"):
            if current_description[0] == "initial":
                result = _review(accept=False)
                result["issues"][0]["targets"] = []
                result["issues"][0]["category"] = "colourEffects"
                result["issues"][0]["instruction"] = (
                    "Rebind the shared accent semantic colour across the selected layers."
                )
                return result
            return _review(accept=True)
        if instance.startswith("final-merged-patch"):
            operations = [{
                "op": "replace",
                "path": "/template/metadata/description",
                "value": "repaired-candidate",
            }]
            if icon_repair:
                operations.append({
                    "op": "add", "path": "/template/feedLayout/layers/-",
                    "value": {
                        "type": "icon", "layerId": "feed-box-check",
                        "geometry": {"x": 20, "y": 20, "width": 30, "height": 30},
                        "icon": "check-square" if instance == "final-merged-patch" else "check",
                        "colourRole": "mainText", "opacity": 1,
                    },
                })
            return {"operations": operations}
        if instance == "comparator-final-repair":
            result = _comparison(
                accept=repair_score >= 9.8,
                comparison_to_best=comparison_to_best,
            )
            result["scores"] = {key: repair_score for key in result["scores"]}
            return result
        raise AssertionError(f"unexpected agent role: {instance}")

    result = process.AdTemplateGeneratorOrchestrator(
        call_agent=call_agent,
        call_image_model=lambda *_args: pytest.fail("image generation is not expected"),
        workspace=tmp_path / "run",
        run_id="trun_final_repair_selection",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(
        source=str(source),
        brief="clone",
        placements=["feed", "story"],
        routes=[
            {"provider": "test", "model": "image"},
            {"provider": "test", "model": "builder"},
            {"provider": "test", "model": "comparator"},
            {"provider": "test", "model": "final-a"},
            {"provider": "test", "model": "final-b"},
        ],
    )
    return result, imported, calls, events


@pytest.mark.parametrize(("repair_score", "comparison_to_best"), [
    (9.8, "same"), (9.8, "worse"),
])
def test_same_score_final_repair_is_kept_and_imported(
    tmp_path, monkeypatch, repair_score, comparison_to_best,
):
    result, imported, calls, events = _run_final_repair_case(
        tmp_path, monkeypatch, repair_score=repair_score, comparison_to_best=comparison_to_best,
    )

    assert imported == ["repaired-candidate"]
    assert result["template"]["metadata"]["description"] == "repaired-candidate"
    assert not any(kind == "regression.reverted" for kind, _, _ in events)
    assert "comparator-final-repair" in calls
    assert calls.count("final-merged-patch") == 1


def test_below_gate_final_repair_cannot_inherit_acceptance(tmp_path, monkeypatch):
    trace = {}
    with pytest.raises(process.AdTemplateProcessError, match="comparator still scores below|did not accept"):
        _run_final_repair_case(
            tmp_path, monkeypatch, repair_score=9.6, comparison_to_best="same", trace=trace,
        )
    assert trace["imported"] == []
