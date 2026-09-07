from __future__ import annotations

import copy
from pathlib import Path

from PIL import Image
import pytest

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _comparison, _review, _template


def _review_with_issue(*, targets_marker=...):
    result = _review(accept=False)
    issue = result["issues"][0]
    if targets_marker is not ...:
        issue["targets"] = copy.deepcopy(targets_marker)
    return result


def test_guarded_patch_missing_replace_path_gets_feedback_and_retries(monkeypatch):
    candidate = {"template": _template(), "assets": []}
    calls = []
    events = []

    monkeypatch.setattr(
        process,
        "vision_message",
        lambda text, paths, **_kwargs: [
            {"type": "text", "text": text},
            {"type": "test_paths", "paths": list(paths)},
        ],
    )

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if len(calls) == 1:
            return {
                "operations": [{
                    "op": "replace",
                    "path": "/template/feedLayout/layers/99/geometry/x",
                    "value": 4,
                }],
            }
        return {
            "operations": [{
                "op": "replace",
                "path": "/template/metadata/description",
                "value": "merged-repair",
            }],
        }

    patch, repaired = process._call_applied_patch(
        call_agent,
        instance="final-merged-patch",
        prompt="repair the merged reviewer issues",
        paths=[],
        route={"provider": "test", "model": "builder"},
        candidate=candidate,
        emit=lambda kind, node, data: events.append((kind, node, data)),
    )

    assert [item[0] for item in calls] == [
        "final-merged-patch",
        "final-merged-patch-format-retry",
    ]
    assert "revision path" in calls[1][1][0]["text"]
    assert "does not exist" in calls[1][1][0]["text"]
    assert patch["operations"][0]["path"] == "/template/metadata/description"
    assert repaired["template"]["metadata"]["description"] == "merged-repair"
    assert not any(kind == "revision.skipped" for kind, _, _ in events)


def test_structured_review_targets_are_authoritative_and_legacy_absence_survives():
    candidate = {"template": _template(), "assets": []}
    target_issue = {
        "placement": "feed",
        "layerIds": ["feed-hero"],
        "category": "geometry",
        "instruction": "The wording deliberately mentions x before width; use the explicit target.",
        "severity": "material",
        "targets": [{
            "layerId": "feed-hero",
            "property": "geometry/width",
            "value": 900,
        }],
    }
    review = _review(accept=False)
    review["issues"] = [target_issue]
    normalized = process.validate_review(review, candidate=candidate)
    assert normalized["issues"][0]["targets"] == target_issue["targets"]

    legacy_review = _review_with_issue()
    legacy = process.validate_review(legacy_review, candidate=candidate)
    assert legacy["issues"][0]["instruction"] == legacy_review["issues"][0]["instruction"]

    structural = {
        **target_issue,
        "category": "colourEffects",
        "instruction": "Rebind the shared accent semantic colour across the selected layers.",
        "targets": [],
    }
    structural_review = _review(accept=False)
    structural_review["issues"] = [structural]
    structural_result = process.validate_review(structural_review, candidate=candidate)
    assert structural_result["issues"][0]["targets"] == []

    invalid = copy.deepcopy(target_issue)
    invalid["targets"] = [{
        "layerId": "feed-hero",
        "property": "geometry/width",
        "value": "900",
    }]
    invalid_review = _review(accept=False)
    invalid_review["issues"] = [invalid]
    with pytest.raises(process.AdTemplateProcessError, match="target"):
        process.validate_review(invalid_review, candidate=candidate)


def test_review_prompt_renders_structured_target_contract_without_format_error():
    candidate = {"template": _template(), "assets": []}
    prompt = process.review_prompt(
        final=False,
        candidate=candidate,
        reference={
            "sourcePlacement": "feed",
            "targetPlacement": "story",
            "canvas": {"width": 1080, "height": 1920},
            "regions": [],
            "preserve": ["all visible geometry"],
        },
        metrics={},
    )
    assert "targets (an array of {layerId, property, value})" in prompt
    assert "targets are authoritative" in prompt
    assert "{{layerId" not in prompt
    assert "Return JSON only" in prompt


def test_unpatchable_structured_batch_uses_guarded_repair_then_recompares(
    monkeypatch, tmp_path,
):
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1350), "white").save(source)
    candidate = {"template": _template(), "assets": []}
    calls = []
    comparison_count = 0

    monkeypatch.setattr(
        process,
        "vision_message",
        lambda text, paths, **_kwargs: [
            {"type": "text", "text": text},
            {"type": "test_paths", "paths": list(paths)},
        ],
    )
    monkeypatch.setattr(
        process,
        "build_source_map",
        lambda _source: {
            "sourceMapVersion": process.SOURCE_MAP_VERSION,
            "ocrStatus": "not_run",
            "ocr": [],
            "edgeRegions": [],
        },
    )
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
    monkeypatch.setattr(
        process,
        "_comparison_views",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        process,
        "_comparison_metrics",
        lambda **_kwargs: {
            "feed": {"scope": "test"},
            "story": {"mode": "native-reflow", "pixelComparison": False},
        },
    )
    monkeypatch.setattr(
        process,
        "validate_reusable_template",
        lambda *args, **kwargs: {"status": "passed", "scenarios": []},
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, **kwargs: {
            "template_id": output["template"]["templateId"],
            "status": "imported",
            "asset_count": 0,
            "replayed": False,
            "library_status": "quarantined",
            "run_id": "trun_guarded",
        },
    )
    monkeypatch.setattr(
        process,
        "review_template_action",
        lambda **kwargs: {"templateId": kwargs["template_id"], "status": "passed"},
    )

    def render(candidate, workspace, *, asset_overrides=None):
        del asset_overrides
        workspace.mkdir(parents=True, exist_ok=True)
        output = workspace / "rendered"
        output.mkdir(exist_ok=True)
        feed = output / "feed.png"
        story = output / "story.png"
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

    def call_agent(instance, prompt, route):
        nonlocal comparison_count
        calls.append((instance, prompt, route))
        if instance.startswith("aspect-reference"):
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
            return copy.deepcopy(candidate)
        if instance == "comparator-final-repair":
            result = _comparison(accept=True, comparison_to_best="better")
            result["scores"] = {key: 9.9 for key in result["scores"]}
            return result
        if instance.startswith("comparator-"):
            comparison_count += 1
            if comparison_count == 1:
                result = _comparison(accept=False, comparison_to_best="not_applicable")
                result["patch"] = None
                result["issues"][0]["instruction"] = (
                    "Rebind the shared accent semantic colour across the selected layers."
                )
                result["issues"][0]["targets"] = []
                return result
            return _comparison(accept=True, comparison_to_best="better")
        if instance.startswith("final-merged-patch"):
            return {
                "operations": [{
                    "op": "replace" if instance == "final-merged-patch" else "add",
                    "path": "/template/feedLayout/layers/1/opacity",
                    "value": 0.8,
                }],
            }
        if instance.startswith("guarded-refinement-"):
            return {
                "operations": [{
                    "op": "replace",
                    "path": "/template/metadata/description",
                    "value": "guarded-repair",
                }],
            }
        if instance.startswith("final-reviewer-"):
            if instance.endswith("-1"):
                result = _review(accept=False)
                result["issues"][0] = {
                    "placement": "feed",
                    "layerIds": ["feed-hero"],
                    "category": "geometry",
                    "instruction": "Correct the measured hero transparency.",
                    "severity": "material",
                    "targets": [{
                        "layerId": "feed-hero",
                        "property": "opacity",
                        "value": 0.8,
                    }],
                }
                return result
            return _review(accept=True)
        raise AssertionError(f"unexpected agent role: {instance}")

    events = []
    result = process.AdTemplateGeneratorOrchestrator(
        call_agent=call_agent,
        call_image_model=lambda *_args: (_ for _ in ()).throw(
            AssertionError("image model must not be used")
        ),
        workspace=tmp_path / "run",
        run_id="trun_guarded",
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

    assert comparison_count == 2
    assert [name for name, _, _ in calls if name.startswith("guarded-refinement-")] == [
        "guarded-refinement-1"
    ]
    guarded_prompt = next(
        prompt for name, prompt, _ in calls if name.startswith("guarded-refinement-")
    )
    assert "unpatchable" in guarded_prompt[0]["text"].lower()
    assert result["template"]["metadata"]["description"] == "guarded-repair"
    assert result["template"]["feedLayout"]["layers"][1]["opacity"] == 0.8
    assert [name for name, _, _ in calls if name.startswith("final-merged-patch")] == [
        "final-merged-patch", "final-merged-patch-format-retry",
    ]
    retry_prompt = next(prompt for name, prompt, _ in calls if name == "final-merged-patch-format-retry")
    assert "revision replace path does not exist: /template/feedLayout/layers/1/opacity" in retry_prompt[0]["text"]
    final_merge_prompt = next(
        prompt for name, prompt, _ in calls if name == "final-merged-patch"
    )
    assert "opacity" in final_merge_prompt[0]["text"]
    assert "propertyTargets" in final_merge_prompt[0]["text"]
    assert result["iterations"][0]["comparison"]["issues"][0]["targets"] == []
    assert any(
        kind == "iteration.compared"
        and data.get("iteration") == 2
        and data.get("decision") == "accept"
        for kind, _, data in events
    )


@pytest.mark.parametrize(("scenario", "relative"), [
    ("identical", "same"), ("identical", "worse"),
    ("different", "same"), ("different", "worse"),
    ("rebased", "worse"), ("final-already-current", "same"),
])
def test_recovery_rechecks_real_orchestrator_without_empty_or_noop_repairs(
    monkeypatch, tmp_path, scenario, relative,
):
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1350), "white").save(source)
    candidate = {"template": _template(), "assets": []}
    candidate["template"]["feedLayout"]["layers"][1]["geometry"]["width"] = 900
    workspace = tmp_path / "run"
    workspace.mkdir()
    process.persist_checkpoint(workspace, {"comparisonBudgetUsed": 5})
    calls, events, observed_x = [], [], []
    monkeypatch.setattr(process, "vision_message", lambda text, paths, **kwargs: [
        {"type": "text", "text": text}, {"type": "test_paths", "paths": list(paths)},
    ])
    monkeypatch.setattr(process, "build_source_map", lambda _source: {
        "sourceMapVersion": process.SOURCE_MAP_VERSION, "ocrStatus": "not_run",
        "ocr": [], "edgeRegions": [],
    })
    monkeypatch.setattr(process, "materialize_source_photo_plan", lambda **kwargs: str(tmp_path / "plan.json"))
    monkeypatch.setattr(process, "build_ephemeral_qa_candidate", lambda item, **kwargs: (item, {}))
    monkeypatch.setattr(process, "prepare_demo_assets", lambda item, **kwargs: (item, {}))
    monkeypatch.setattr(process, "_comparison_views", lambda *args, **kwargs: [])
    monkeypatch.setattr(process, "_comparison_metrics", lambda **kwargs: {})
    monkeypatch.setattr(process, "validate_reusable_template", lambda *args, **kwargs: {"status": "passed", "scenarios": []})
    monkeypatch.setattr(process, "import_template", lambda output, **kwargs: {
        "template_id": output["template"]["templateId"], "status": "imported",
        "asset_count": 0, "replayed": False, "library_status": "quarantined",
        "run_id": "trun_recheck",
    })
    monkeypatch.setattr(process, "review_template_action", lambda **kwargs: {
        "templateId": kwargs["template_id"], "status": "passed",
    })

    def render(item, target, *, asset_overrides=None):
        target.mkdir(parents=True, exist_ok=True)
        paths = {}
        for placement, height in [("feed", 1350), ("story", 1920)]:
            path = target / (placement + ".png")
            Image.new("RGB", (1080, height), "white").save(path)
            paths[placement] = str(path)
        artifact = target / "artifact.json"
        artifact.write_text(process._safe_json(item), encoding="utf-8")
        observed_x.append(item["template"]["feedLayout"]["layers"][1]["geometry"]["x"])
        return {"render": paths, "previews": [], "review_previews": [], "template_path": str(artifact)}

    monkeypatch.setattr(process, "run_renderer", render)
    comparison_count = 0

    def issue_at(x):
        return {"placement": "feed", "layerIds": ["feed-hero"],
                "category": "geometry", "instruction": "Use the measured hero position.",
                "severity": "material", "targets": [
                    {"layerId": "feed-hero", "property": "geometry/x", "value": x},
                ]}

    def call_agent(instance, prompt, route):
        nonlocal comparison_count
        calls.append(instance)
        if instance == "aspect-reference":
            return {
                "sourcePlacement": "feed", "targetPlacement": "story",
                "canvas": {"width": 1080, "height": 1920},
                "regions": [{"regionId": "main", "sourceRole": "main",
                             "target": {"x": 0, "y": 0, "width": 1080, "height": 1920},
                             "zIndex": 0}],
                "preserve": ["all visible geometry and effects"],
            }
        if instance == "builder-initial":
            return copy.deepcopy(candidate)
        if instance.startswith("comparator-"):
            comparison_count += 1
            # Initial review saves x=0. Only the different/rebased cases ask
            # for x=12, which the exact compiler applies without a model.
            revising = comparison_count == 1 or (scenario == "rebased" and comparison_count == 2)
            result = _comparison(accept=not revising, comparison_to_best=(
                "not_applicable" if comparison_count == 1 else relative
            ))
            result["patch"] = None
            result["scores"] = {key: 9.2 if revising else 9.6 for key in result["scores"]}
            if revising:
                x = 12 if comparison_count == 1 and scenario in {"different", "rebased"} else 0
                result["issues"] = [issue_at(x)]
            return result
        if instance.startswith("final-reviewer-"):
            if scenario == "final-already-current" and instance.endswith("-1"):
                result = _review(accept=False)
                result["issues"] = [issue_at(0)]
                return result
            return _review(accept=True)
        raise AssertionError(f"unnecessary repair or unexpected provider call: {instance}")

    result = process.AdTemplateGeneratorOrchestrator(
        call_agent=call_agent,
        call_image_model=lambda *args: pytest.fail("no image generation expected"),
        workspace=workspace, run_id="trun_recheck", project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(source=str(source), brief="clone", placements=["feed", "story"], routes=[
        {"provider": "test", "model": name}
        for name in ["image", "builder", "comparator", "final-a", "final-b"]
    ])
    expected_comparisons = 3 if scenario in {"different", "rebased"} else 2
    assert comparison_count == expected_comparisons
    assert process.load_checkpoint(workspace)["comparisonBudgetUsed"] == 5 + expected_comparisons
    assert result["template"]["feedLayout"]["layers"][1]["geometry"]["x"] == 0
    assert len([name for name in calls if name.startswith("final-reviewer-")]) == (
        4 if scenario == "final-already-current" else 2
    )
    assert result["import"]["library_status"] == "quarantined"
    assert any(kind == "iteration.recheck-requested" for kind, _, _ in events)
    assert all(not name.startswith(("guarded-refinement-", "layer-refinement-", "final-merged-patch")) for name in calls)
    if scenario in {"different", "rebased"}:
        assert 12 in observed_x
        assert result["iterations"][1]["discarded"] is True
        assert any(kind == "candidate.patch-applied" and data["source"] == "measured-targets" for kind, _, data in events)
    assert not result["iterations"][-1].get("discarded")


def test_stall_diagnosis_repeats_review_neutral_photo_and_font_constraints():
    candidate = {"template": _template(), "assets": []}
    reference = {
        "sourcePlacement": "feed",
        "targetPlacement": "story",
        "canvas": {"width": 1080, "height": 1920},
        "regions": [],
        "preserve": ["all visible geometry"],
    }
    ordinary = process.review_prompt(
        final=False, candidate=candidate, reference=reference, metrics={}
    )
    diagnosis = process.stall_diagnosis_prompt(
        best_candidate=candidate,
        best_review=_review(accept=False),
        best_iteration=6,
        recent_rejects=[],
    )
    for phrase in (
        "Neutral replacement/catalog photographs are not likeness defects",
        "Unavailable or proprietary font differences are not capability blockers",
    ):
        assert phrase in ordinary
        assert phrase in diagnosis
    assert len(diagnosis) < 100_000
