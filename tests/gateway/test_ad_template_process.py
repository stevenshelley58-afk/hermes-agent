import base64
import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
import gateway.ad_template_process as process
from gateway.ad_template_process import (
    AdTemplateProcessError, deterministic_documents, import_template,
    validate_artifacts, validate_final_review, validate_iterations, SoleProcessOrchestrator, vision_message,
)
from gateway.tool_run_api import ToolRunAPIMixin


def treatment_observation():
    return {
        "brand_silhouette_features": ["roof"],
        "phone_badge": {"shape": "circle", "fillTreatment": "filled"},
        "mail_badge": {"shape": "circle", "fillTreatment": "filled"},
        "web_badge": {"shape": "circle", "fillTreatment": "filled"},
        "location_badge": {"shape": "absent", "fillTreatment": "absent"},
        "cta_badge": {"shape": "absent", "fillTreatment": "absent"},
    }


def evidence(score=9.4, reason="Improve spacing"):
    actionable_change = (
        "placement=feed; layers=feed-bg; "
        "current={x:0,y:0,width:1080,height:1350}; "
        "target={x:0,y:0,width:1080,height:1350}; "
        f"change={reason}"
    )
    required_changes = [actionable_change] if score < 9.5 else []
    differences = [reason] if score < 9.5 else []
    source_inventory = {
        "macro_regions": [
            {
                "region": region,
                "source_components": [region],
                "feed_components": [region],
                "story_components": [region],
                "source_count": 1,
                "feed_count": 1,
                "story_count": 1,
                "status": "match",
                "material": False,
                "findings": [],
                "required_change_refs": [],
            }
            for region in process.COMPARATOR_MACRO_INVENTORY_REGIONS
        ],
        "micro_checks": [
            {
                "check": check,
                "source_observation": (
                    treatment_observation()
                    if check == "mark_badge_treatment" else f"source {check}"
                ),
                "feed_observation": (
                    treatment_observation()
                    if check == "mark_badge_treatment" else f"feed {check}"
                ),
                "story_observation": (
                    treatment_observation()
                    if check == "mark_badge_treatment" else f"story {check}"
                ),
                "status": "mismatch" if differences and check == "typography_spacing" else "match",
                "material": bool(differences and check == "typography_spacing"),
                "findings": differences if differences and check == "typography_spacing" else [],
                "required_change_refs": [1] if differences and check == "typography_spacing" else [],
            }
            for check in process.COMPARATOR_MICRO_INVENTORY_CHECKS
        ],
    }
    return {
        "rubric": {field: score for field in process.RUBRIC_FIELDS},
        "macro": {field: score for field in process.MACRO_FIELDS},
        "critical_regions": [{"region": "full composition", "status": "pass", "findings": []}],
        "regressions": [],
        "ranked_changes": required_changes,
        "decision": "accept" if score >= 9.5 else "revise",
        "reason": reason,
        "differences": differences,
        "required_changes": required_changes,
        "hard_failures": [],
        "visible_strings": {
            "source": ["SOURCE"], "feed": ["FEED"], "story": ["STORY"],
        },
        "source_inventory": source_inventory,
        "semantic_glyph_inventory": {
            check: {
                "source_observation": f"source {check} role",
                "feed_observation": f"feed {check} role",
                "story_observation": f"story {check} role",
                "status": "match",
                "findings": [],
            }
            for check in process.FINAL_SEMANTIC_GLYPH_CHECKS
        },
        "mark_badge_treatment": {
            "source_observation": treatment_observation(),
            "feed_observation": treatment_observation(),
            "story_observation": treatment_observation(),
            "status": "match",
            "findings": [],
        },
    }

def remap_inventory(review, *, check="typography_spacing"):
    """Keep a mutated test review's inventory aligned with its material changes."""
    findings = list(review.get("differences") or [])
    refs = list(range(1, len(review.get("required_changes") or []) + 1))
    for item in review["source_inventory"]["macro_regions"]:
        item.update(status="match", material=False, findings=[], required_change_refs=[])
    for item in review["source_inventory"]["micro_checks"]:
        is_target = item["check"] == check and bool(findings or refs)
        item.update(
            status="mismatch" if is_target else "match",
            material=bool(is_target and refs),
            findings=findings if is_target else [],
            required_change_refs=refs if is_target else [],
        )
    return review

def iteration(score=9.4, number=1):
    return {"iteration": number, "comparison": evidence(score), "decision": "accepted" if score >= 9.5 else "revise"}

def test_only_real_stages_and_durable_source_preview(tmp_path):
    assert process.STAGES == ("source", "build", "render", "compare", "final-check", "live")
    assert ToolRunAPIMixin._tool_stage_order() == list(process.STAGES)
    assert ToolRunAPIMixin._canonical_tool_stage("story-draft") == "build"
    assert ToolRunAPIMixin._canonical_tool_stage("final-review") == "final-check"
    assert ToolRunAPIMixin._tool_stage_from_process_event("final-review.retried", "final-check") == "final-check"
    assert ToolRunAPIMixin._canonical_tool_stage("import") == "live"
    source = tmp_path / "upload.JPEG"
    source.write_bytes(b"source-pixels")
    target = ToolRunAPIMixin._copy_source_preview(tmp_path / "run", source)
    assert target == tmp_path / "run" / "previews" / "source.jpeg"
    assert target.read_bytes() == b"source-pixels"

def metadata(title="Smoke"):
    return {
        "title": title, "description": "", "gallerySamples": {},
        "metaCopyDefaults": {"primaryText": [], "headlines": [], "descriptions": [], "cta": "LEARN_MORE"},
        "aiWritingGuidance": {"summary": "", "fields": {}},
        "publishRequirements": {
            "objective": "OUTCOME_TRAFFIC", "specialAdCategory": None,
            "instantForm": {"required": False, "dependency": None},
            "destination": {"required": True, "kind": "website", "dependency": "landing_page_url"},
            "requiredCtaTypes": ["LEARN_MORE"],
        },
        "replacementAssets": [], "realAssetRefs": [],
    }

def semantic_colours():
    return {"background": "#FFFFFF", "primary": "#1A56DB", "secondary": "#6B7280", "accent": "#F59E0B", "mainText": "#111827", "inverseText": "#FFFFFF"}

def valid_candidate(template_id="candidate"):
    def layout(placement, height):
        return {"placement": placement, "layers": [{"type": "plate", "layerId": f"{placement}-bg", "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    return {"template": {"schema": "blockwise.ad-template", "templateId": template_id, "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata()}, "assets": []}


def source_invariant_candidate():
    candidate = valid_candidate("source-invariants")
    template = candidate["template"]
    template["fonts"] = [{"file": "manrope-400.woff2"}]
    template["textInputs"] = [
        {"key": "brand_name", "label": "Brand", "placeholder": "REAL ESTATE", "maxLength": 20},
        {
            "key": "features", "label": "Features",
            "placeholder": "• One\n• Two\n• Three\n• Four\n• Five\n• Six", "maxLength": 120,
        },
        {"key": "price", "label": "Price", "placeholder": "$1.599.999", "maxLength": 20},
    ]
    for placement, height, font_size in (("feed", 1350, 24), ("story", 1920, 32)):
        layout = template[f"{placement}Layout"]
        for index, (input_key, max_lines) in enumerate((
            ("brand_name", 1), ("features", 6), ("price", 1),
        )):
            layout["layers"].append({
                "type": "text", "layerId": f"{placement}-{input_key}",
                "inputKey": input_key, "font": {"file": "manrope-400.woff2"},
                "fontSize": font_size, "lineHeight": 1.2, "tracking": 0,
                "alignment": "left", "maxCharacters": 120, "maxLines": max_lines,
                "colourRole": "mainText", "overflowBehaviour": "refuse",
                "geometry": {"x": 100, "y": 300 + index * 260, "width": 700, "height": 220},
            })
        for index, icon in enumerate(("phone", "mail", "globe", "pin", "arrow")):
            layout["layers"].append({
                "type": "icon", "layerId": f"{placement}-{icon}-icon",
                "icon": icon, "colourRole": "mainText",
                "geometry": {
                    "x": 100 + index * 80,
                    "y": 1500 if placement == "story" else height - 180,
                    "width": 40, "height": 40,
                },
            })
    template["feedLayout"]["layers"].append({
        "type": "vector", "layerId": "feed-column-divider", "shape": "line",
        "colourRole": "mainText", "opacity": 1,
        "geometry": {"x": 540, "y": 850, "width": 2, "height": 260},
    })
    return candidate


def test_source_inventory_drives_exact_pre_render_invariants():
    review = evidence(8.8, "Repair source-visible invariants")
    review["visible_strings"]["source"] = [
        "REAL ESTATE", "$1.599.999",
        "• One\n• Two\n• Three\n• One\n• Two\n• Three",
    ]
    checks = {item["check"]: item for item in review["source_inventory"]["micro_checks"]}
    checks["brand_text"]["source_observation"] = (
        "A visible REAL ESTATE wordmark sits beneath the roof emblem"
    )
    checks["dividers"]["source_observation"] = (
        "A visible vertical divider separates ABOUT from PROPERTY FEATURES"
    )
    contact = next(
        item for item in review["source_inventory"]["macro_regions"]
        if item["region"] == "contact_footer"
    )
    contact["source_components"] = [
        "filled circular phone pictogram", "filled circular mail pictogram",
        "filled circular globe pictogram",
    ]

    compact = process._compact_revision_feedback({"current_review": review})
    invariants = json.loads(compact)["source_invariants"]
    assert invariants == {
        "brand_text_required": True,
        "divider_required": True,
        "feature_bullet_count": 6,
        "price_strings": ["$1.599.999"],
        "semantic_glyph_roles": ["phone", "mail", "web"],
    }
    assert process._source_invariants_from_feedback(
        process._compact_revision_feedback(compact)
    ) == invariants
    process.validate_builder_candidate(
        source_invariant_candidate(), source_invariants=invariants,
    )


def test_feature_list_normalization_splits_literal_and_inline_bullets():
    candidate = source_invariant_candidate()
    candidate["template"]["textInputs"][1]["placeholder"] = (
        "• One  • Two\\n• Three  • Four  • Five  • Six"
    )
    for layout_name in ("feedLayout", "storyLayout"):
        candidate["template"][layout_name]["layers"][2]["maxLines"] = 2
    invariants = {"feature_bullet_count": 6}

    normalized = process.normalize_list_placeholders(candidate, invariants)

    assert candidate["template"]["storyLayout"]["layers"][2]["maxLines"] == 2
    assert normalized["template"]["textInputs"][1]["placeholder"] == (
        "• One\n• Two\n• Three\n• Four\n• Five\n• Six"
    )
    for layout_name in ("feedLayout", "storyLayout"):
        assert normalized["template"][layout_name]["layers"][2]["maxLines"] == 6
    process.validate_builder_candidate(normalized, source_invariants=invariants)


def test_repair_prompt_repeats_all_source_invariants():
    invariants = {
        "brand_text_required": True,
        "divider_required": True,
        "feature_bullet_count": 6,
        "price_strings": ["$1.599.999"],
        "semantic_glyph_roles": ["phone", "mail", "web"],
    }
    prompt = process.generator_prompt(
        run_id="run", project_id="blockwise", brief="", placements=["feed", "story"],
        source="source.png", feedback=json.dumps({"source_invariants": invariants}),
        validation_feedback="story feature inventory has 2 stacked bullets",
        repair_attempt=2, rejected_candidate=source_invariant_candidate(),
    )

    assert "MANDATORY SOURCE-DERIVED INVARIANTS FOR EVERY REPAIR" in prompt
    assert '"feature_bullet_count":6' in prompt
    assert '"price_strings":["$1.599.999"]' in prompt
    assert '"semantic_glyph_roles":["phone","mail","web"]' in prompt
    assert "one real newline-delimited bullet per item" in prompt


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("features", "source inventory requires exactly 6 distinct stacked bullets"),
        ("duplicate-features", "feature inventory repeats bullet content"),
        ("brand", "must preserve the source-visible brand lockup"),
        ("phone", "semantic phone role requires a real phone icon layer"),
        ("divider", "must preserve the source-visible divider"),
        ("price", "price punctuation must exactly preserve"),
    ],
)
def test_pre_render_source_invariants_reject_material_regressions(mutation, message):
    candidate = source_invariant_candidate()
    invariants = {
        "brand_text_required": True,
        "divider_required": True,
        "feature_bullet_count": 6,
        "price_strings": ["$1.599.999"],
        "semantic_glyph_roles": ["phone", "mail", "web"],
    }
    if mutation == "features":
        candidate["template"]["textInputs"][1]["placeholder"] = "• One\n• Two\n• Three\n• Four"
    elif mutation == "duplicate-features":
        candidate["template"]["textInputs"][1]["placeholder"] = (
            "• One\n• Two\n• Three\n• One\n• Two\n• Three"
        )
    elif mutation == "brand":
        candidate["template"]["feedLayout"]["layers"] = [
            layer for layer in candidate["template"]["feedLayout"]["layers"]
            if layer.get("layerId") != "feed-brand_name"
        ]
    elif mutation == "phone":
        candidate["template"]["feedLayout"]["layers"] = [
            layer for layer in candidate["template"]["feedLayout"]["layers"]
            if layer.get("layerId") != "feed-phone-icon"
        ]
    elif mutation == "divider":
        candidate["template"]["feedLayout"]["layers"] = [
            layer for layer in candidate["template"]["feedLayout"]["layers"]
            if layer.get("layerId") != "feed-column-divider"
        ]
    else:
        candidate["template"]["textInputs"][2]["placeholder"] = "$1,599,999"
    with pytest.raises(AdTemplateProcessError, match=re.escape(message)):
        process.validate_builder_candidate(candidate, source_invariants=invariants)


def test_cta_text_does_not_invent_an_icon_but_explicit_graphic_placeholder_does():
    candidate = source_invariant_candidate()
    template = candidate["template"]
    template["textInputs"].append({
        "key": "contactCta", "label": "Contact CTA",
        "placeholder": "Connect us for more info", "maxLength": 40,
    })
    for placement, font_size in (("feed", 24), ("story", 32)):
        layout = template[f"{placement}Layout"]
        layout["layers"] = [
            layer for layer in layout["layers"]
            if layer.get("layerId") != f"{placement}-arrow-icon"
        ]
        layout["layers"].append({
            "type": "text", "layerId": f"{placement}-contact-cta",
            "inputKey": "contactCta", "font": {"file": "manrope-400.woff2"},
            "fontSize": font_size, "lineHeight": 1.2, "tracking": 0,
            "alignment": "left", "maxCharacters": 40, "maxLines": 1,
            "colourRole": "mainText", "overflowBehaviour": "refuse",
            "geometry": {"x": 500, "y": 1200 if placement == "feed" else 1400,
                         "width": 400, "height": 60},
        })
    invariants = {"semantic_glyph_roles": ["phone", "mail", "web"]}

    process.validate_builder_candidate(candidate, source_invariants=invariants)

    template["feedLayout"]["layers"].append({
        "type": "vector", "layerId": "feed-cta-icon-placeholder", "shape": "ring",
        "colourRole": "mainText", "opacity": 1,
        "geometry": {"x": 920, "y": 1200, "width": 36, "height": 36},
    })
    with pytest.raises(
        AdTemplateProcessError,
        match="semantic cta role requires a real cta icon layer",
    ):
        process.validate_builder_candidate(candidate, source_invariants=invariants)


def test_pre_render_rejects_center_compressed_story_but_allows_ui_bands():
    candidate = source_invariant_candidate()
    for index, layer in enumerate(candidate["template"]["storyLayout"]["layers"][1:]):
        layer["geometry"]["y"] = 650 + index * 20
    with pytest.raises(AdTemplateProcessError, match="center-compressed or letterboxed"):
        process.validate_builder_candidate(candidate)

    candidate = source_invariant_candidate()
    assert process.validate_builder_candidate(candidate) is candidate


def test_comparator_price_transcription_must_match_guarded_rendered_placeholder():
    candidate = source_invariant_candidate()
    review = process._assessment(evidence(9.6, "Pass"), "comparator", require_change_list=True)
    invariants = {"price_strings": ["$1.599.999"]}
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="price transcription contradicts the guarded rendered placeholder",
    ):
        process._validate_comparator_source_invariant_observations(
            review, candidate, invariants,
        )

    review["visible_strings"]["feed"] = ["$1.599.999"]
    review["visible_strings"]["story"] = ["$1.599.999"]
    process._validate_comparator_source_invariant_observations(
        review, candidate, invariants,
    )

def fake_render(candidate, workspace, calls):
    calls.append(candidate)
    workspace.mkdir(parents=True, exist_ok=True)
    feed = workspace / "feed.png"
    story = workspace / "story.png"
    feed.write_bytes(b"feed")
    story.write_bytes(b"story")
    return {
        "template": candidate["template"], "assets": candidate["assets"],
        "previews": [
            {"name": feed.name, "path": str(feed), "placement": "feed"},
            {"name": story.name, "path": str(story), "placement": "story"},
        ],
        "render": {"feed": str(feed), "story": str(story)},
        "template_path": str(workspace / "artifact.json"),
    }

def overlap_candidate(template_id="overlap-candidate"):
    candidate = valid_candidate(template_id)
    candidate["template"]["imageInputs"] = [
        {"key": "hero", "label": "Hero", "acceptedTypes": ["image/jpeg"]},
        {"key": "thumb", "label": "Thumbnail", "acceptedTypes": ["image/jpeg"]},
    ]
    candidate["template"]["feedLayout"]["layers"].extend([
        {
            "type": "image_slot", "layerId": "feed-hero", "inputKey": "hero",
            "geometry": {"x": 72, "y": 184, "width": 600, "height": 400}, "mask": "none",
            "minSourceWidth": 600, "minSourceHeight": 400,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
        {
            "type": "vector", "layerId": "feed-price-panel",
            "geometry": {"x": 72, "y": 184, "width": 600, "height": 400},
            "shape": "rect", "colourRole": "primary", "opacity": 0.9,
        },
        {
            "type": "image_slot", "layerId": "feed-thumb-1", "inputKey": "thumb",
            "geometry": {"x": 72, "y": 584, "width": 180, "height": 220}, "mask": "none",
            "minSourceWidth": 180, "minSourceHeight": 220,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
    ])
    return candidate

def test_one_comparator_per_iteration_and_final_review_only_after_pass():
    assert validate_iterations([iteration()])[0]["comparison"]["score"] == 9.4
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([{"iteration": 1, "comparison": {"score": 9.4, "reason": "x"}, "reviewers": []}])
    review = validate_final_review({"reviewers": [{"id": "reviewer-a", "route": "a/m", **evidence(9.6, "good")}, {"id": "reviewer-b", "route": "b/m", **evidence(9.7, "good")}]}, accepted=True)
    assert review["decision"] == "accepted"

def test_quality_gate_rejects_one_weak_dimension_even_when_mean_passes():
    weak = evidence(9.6, "Delivered-size copy remains too small")
    weak["rubric"]["layout_geometry"] = 9.1
    weak["differences"] = ["The delivered-size copy block remains visibly misaligned"]
    weak["required_changes"] = [
        "placement=feed; layers=feed-bg; current={x:0,y:0,width:1080,height:1350}; "
        "target={x:0,y:0,width:1080,height:1340}; change=Correct the visible layout geometry"
    ]
    weak["ranked_changes"] = weak["required_changes"]
    weak["decision"] = "revise"
    remap_inventory(weak)
    record = validate_iterations([{"iteration": 1, "comparison": weak, "decision": "revise"}])[0]
    assert record["comparison"]["score"] >= process.THRESHOLD
    assert record["comparison"]["minimum_score"] == 9.1
    assert record["decision"] == "revise"

    final_weak = json.loads(json.dumps(weak))
    final_weak["required_changes"] = []
    reviewers = [
        {"id": "reviewer-a", "route": "a/m", **final_weak},
        {"id": "reviewer-b", "route": "b/m", **evidence(9.8, "Strong")},
    ]
    assert validate_final_review({"reviewers": reviewers}, accepted=True)["decision"] == "revise"


def test_final_review_rejects_placeholder_semantic_glyphs_despite_passing_scores():
    weak = evidence(9.8, "Numeric pass")
    weak["semantic_glyph_inventory"]["phone"].update(
        status="mismatch",
        findings=["Source phone handset became a hollow ring in Feed and Story"],
    )
    final = validate_final_review({
        "reviewers": [
            {"id": "reviewer-a", "route": "a/vision", **weak},
            {"id": "reviewer-b", "route": "b/vision", **evidence(9.8, "Independent pass")},
        ],
    }, accepted=True)

    assert final["decision"] == "revise"
    assert final["reviewers"][0]["semantic_glyph_mismatch"] is True


@pytest.mark.parametrize(
    ("placement", "mutate"),
    [
        (
            "feed",
            lambda observation: observation["phone_badge"].update(fillTreatment="outline"),
        ),
        (
            "story",
            lambda observation: observation.update(
                brand_silhouette_features=["single-roof"],
            ),
        ),
    ],
)
def test_final_review_cannot_match_changed_fill_treatment_or_brand_silhouette(
    placement, mutate,
):
    weak = evidence(9.8, "Numeric pass")
    mutate(weak["mark_badge_treatment"][f"{placement}_observation"])

    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="cannot declare match when brand silhouette features, badge shapes, or fill treatments differ",
    ):
        validate_final_review({
            "reviewers": [
                {"id": "reviewer-a", "route": "a/vision", **weak},
                {"id": "reviewer-b", "route": "b/vision", **evidence(9.8, "Independent pass")},
            ],
        }, accepted=True)


@pytest.mark.parametrize("raw_value", (..., None, "move it", {"change": "move it"}, 1))
def test_final_review_ignores_optional_change_field_without_weakening_gate(raw_value):
    weak = evidence(9.2, "Final spacing still differs visibly")
    if raw_value is ...:
        weak.pop("required_changes")
    else:
        weak["required_changes"] = raw_value
    result = validate_final_review({
        "reviewers": [
            {"id": "reviewer-a", "route": "a/vision", **weak},
            {"id": "reviewer-b", "route": "b/vision", **evidence(9.7, "Independent pass")},
        ],
    }, accepted=True)

    assert result["decision"] == "revise"
    assert result["reviewers"][0]["required_changes"] == []
    assert len(result["reviewers"]) == 2


@pytest.mark.parametrize("raw_value", (..., None))
def test_comparator_does_not_normalize_absent_change_list(raw_value):
    weak = evidence(9.2, "Comparator found a visible spacing mismatch")
    if raw_value is ...:
        weak.pop("required_changes")
    else:
        weak["required_changes"] = raw_value
    with pytest.raises(
        AdTemplateProcessError,
        match="comparator must provide a concrete required_changes list",
    ):
        validate_iterations([
            {"iteration": 1, "comparison": weak, "decision": "revise"}
        ])


def test_autonomous_loop_can_converge_after_thirty_iterations(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([9.0] * 30 + [9.7])
    renders = []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-long-run", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_long",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert len(result["iterations"]) == 31
    assert result["iterations"][-1]["decision"] == "accepted"
    assert result["import"]["status"] == "imported"


def test_source_match_and_concrete_change_list_are_hard_gates():
    weak_match = evidence(9.8, "Header and image grid still differ")
    weak_match["rubric"]["feed_source_likeness"] = 9.4
    weak_match["required_changes"] = []
    with pytest.raises(
        AdTemplateProcessError,
        match="comparator must provide a concrete required_changes list",
    ):
        validate_iterations([
            {"iteration": 1, "comparison": weak_match, "decision": "revise"}
        ])

    unfinished = evidence(9.8, "Footer remains too tall")
    unfinished["required_changes"] = [
        "placement=feed; layers=feed-footer; current={x:72,y:1120,width:936,height:140}; "
        "target={x:72,y:1140,width:936,height:120}; change=Reduce footer height to match the source"
    ]
    unfinished["ranked_changes"] = unfinished["required_changes"]
    unfinished["differences"] = ["Footer remains too tall"]
    unfinished["decision"] = "revise"
    remap_inventory(unfinished)
    record = validate_iterations([
        {"iteration": 1, "comparison": unfinished, "decision": "revise"}
    ])[0]
    assert record["decision"] == "revise"

    vague = evidence(9.4, "Footer remains too tall")
    vague["required_changes"] = ["Reduce footer height to match the source"]
    vague["ranked_changes"] = vague["required_changes"]
    with pytest.raises(AdTemplateProcessError, match="placement, layers, current geometry, target geometry"):
        validate_iterations([{"iteration": 1, "comparison": vague, "decision": "revise"}])


def test_comparator_target_overlap_rejects_exact_hero_thumbnail_collision():
    candidate = overlap_candidate()
    assessment = evidence(8.6, "Extend the hero and price panel")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Extend the hero and price panel to y=804"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    parsed = process._assessment(assessment, "comparator", require_change_list=True)
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match=r"newly overlaps feed layers (feed-hero and feed-thumb-1|feed-price-panel and feed-thumb-1)",
    ):
        process._validate_required_change_targets(parsed, candidate)

    preexisting = overlap_candidate("preexisting-overlap")
    for layer in preexisting["template"]["feedLayout"]["layers"]:
        if layer.get("layerId") in {"feed-hero", "feed-price-panel"}:
            layer["geometry"]["height"] = 620
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:620}; "
        "target={x:72,y:184,width:600,height:600}; "
        "change=Reduce an overlap that already exists"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        preexisting,
    )


def test_comparator_allows_intentional_vector_frame_over_image():
    candidate = overlap_candidate("vector-frame-overlap")
    assessment = evidence(8.9, "Lower the gallery frame over the hero edge")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:500,width:600,height:180}; "
        "change=Overlap the source-visible frame across the lower hero edge"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_allows_source_justified_overlapping_image_collage():
    candidate = overlap_candidate("source-overlapping-collage")
    assessment = evidence(8.9, "Match the overlapping source collage")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Create the source-visible overlapping photo collage by intentionally overlapping the hero with the thumbnail row"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_approximate_current_geometry_uses_actual_document_baseline():
    candidate = overlap_candidate("approximate-current")
    assessment = evidence(8.9, "Tighten the hero crop")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero; "
        "current={x:70,y:180,width:605,height:405}; "
        "target={x:72,y:184,width:600,height:380}; "
        "change=Tighten the hero without touching the thumbnail row"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_repairs_unambiguous_cross_placement_label():
    candidate = overlap_candidate("placement-label-slip")
    candidate["template"]["storyLayout"]["layers"].append({
        "type": "vector", "layerId": "story-border",
        "geometry": {"x": 72, "y": 240, "width": 936, "height": 1380},
        "shape": "rounded", "colourRole": "primary", "opacity": 1,
    })
    assessment = evidence(8.9, "Story border needs refinement")
    assessment["required_changes"] = [
        "placement=feed; layers=story-border; "
        "current={x:72,y:240,width:936,height:1380}; "
        "target={x:72,y:240,width:936,height:1380}; "
        "change=Reduce the Story border radius without moving it"
    ]
    assessment["ranked_changes"] = assessment["required_changes"]
    parsed = process._assessment(assessment, "comparator", require_change_list=True)

    normalized, corrections = process._normalize_required_change_placements(
        parsed, candidate,
    )

    assert normalized["required_changes"][0].startswith("placement=story;")
    assert normalized["ranked_changes"] == normalized["required_changes"]
    assert corrections == [{
        "from": "feed", "to": "story", "layers": ["story-border"],
    }]
    process._validate_required_change_targets(normalized, candidate)


def test_comparator_retries_self_inconsistent_overlap_and_persists_event(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    events, calls, renders = [], [], []

    bad = evidence(8.6, "Extend the hero and price panel")
    bad["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Extend the hero and price panel to y=804"
    ]
    bad["ranked_changes"] = bad["required_changes"]

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return overlap_candidate(f"candidate-{instance}")
        if instance == "comparator-1":
            return bad
        return evidence(9.7, "Self-consistent pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-overlap", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_overlap",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    comparator_calls = [name for name, _ in calls if name.startswith("comparator-")]
    assert comparator_calls == ["comparator-1", "comparator-1-retry-1"]
    retry_events = [data for kind, data in events if kind == "comparator.retried"]
    assert retry_events == [{
        "iteration": 1,
        "attempt": 1,
        "reason": "required change newly overlaps feed layers feed-hero and feed-thumb-1",
    }]
    retry_prompt = next(prompt for name, prompt in calls if name == "comparator-1-retry-1")
    assert "previous response was rejected" in retry_prompt
    assert "feed-hero and feed-thumb-1" in retry_prompt
    assert "schema/self-consistency correction of the same comparison" in retry_prompt
    compared_events = [data for kind, data in events if kind == "iteration.compared"]
    assert len(compared_events) == 1
    assert compared_events[0]["source_inventory"] == result["iterations"][0]["comparison"]["source_inventory"]
    assert result["iterations"][0]["decision"] == "accepted"


def test_comparator_transport_timeout_escalates_once_to_quality_route(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    events, calls, renders = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance == "comparator-1":
            raise process.AdTemplateTransportError("model role transport attempt exhausted")
        return evidence(9.7, "Quality route completed the comparison")

    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process, "import_template",
        lambda output, run_id, project_id: {
            "template_id": "tpl-transport-fallback", "status": "imported",
        },
    )
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run",
        run_id="trun_transport_fallback", project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, data)),
    ).run(
        source=str(source), brief="", placements=["feed", "story"],
        routes=_quality_routes(), require_quality_route=True,
    )

    assert ("comparator-1", "openai-codex/gpt-5.6-luna") in calls
    assert (
        "comparator-1-transport-fallback", "openai-codex/gpt-5.6-sol"
    ) in calls
    escalated = [data for kind, data in events if kind == "comparator.route-escalated"]
    assert escalated == [{
        "iteration": 1,
        "from_provider": "openai-codex", "from_model": "gpt-5.6-luna",
        "to_provider": "openai-codex", "to_model": "gpt-5.6-sol",
        "reason": "model role transport attempt exhausted",
    }]
    assert result["iterations"][0]["decision"] == "accepted"


def test_comparator_schema_recovery_survives_three_invalid_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    events, calls, renders = [], [], []
    invalid = evidence(8.6, "Move the hero closer to the source")
    invalid["required_changes"] = ["Move the hero lower"]
    invalid["ranked_changes"] = invalid["required_changes"]
    comparator_calls = 0

    def call_agent(instance, prompt, route):
        nonlocal comparator_calls
        calls.append((instance, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return overlap_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            comparator_calls += 1
            if comparator_calls <= 3:
                return invalid
        return evidence(9.7, "Schema-correct pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-comparator-retry", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_comparator_three_retries",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    comparator_entries = [item for item in calls if item[0].startswith("comparator-")]
    assert len(comparator_entries) == 4
    assert comparator_entries[-1][0] == "comparator-1-retry-3"
    assert "placement=Feed or Story" in comparator_entries[-1][1]
    assert "current={x,y,width,height}" in comparator_entries[-1][1]
    retry_events = [data for kind, data in events if kind == "comparator.retried"]
    assert len(retry_events) == 3
    assert [item["attempt"] for item in retry_events] == [1, 2, 3]
    assert all(
        item["reason"] == "comparator required_changes must name placement, layers, current geometry, target geometry, and change"
        for item in retry_events
    )
    assert len(renders) == 1
    assert result["iterations"][0]["decision"] == "accepted"


def test_asset_envelope_is_mechanically_mirrored_without_changing_content():
    candidate = valid_candidate("asset-normalization")
    declaration = {
        "assetKey": "hero",
        "fileName": "home/mt-lawley-federation.webp",
        "mimeType": "image/webp",
    }
    candidate["assets"] = [declaration]
    candidate["template"].pop("assets")
    normalized = process.normalize_asset_declarations(candidate)
    assert normalized["template"]["assets"] == {
        "hero": {"fileName": declaration["fileName"], "mimeType": declaration["mimeType"]}
    }
    assert normalized["assets"] == [declaration]

    reverse = valid_candidate("asset-normalization-reverse")
    reverse["template"]["assets"] = normalized["template"]["assets"]
    assert process.normalize_asset_declarations(reverse)["assets"] == [declaration]

def test_bare_model_scores_are_rejected():
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([iteration(9.8)]) if False else validate_iterations([{"iteration": 1, "comparison": {"score": 10, "reason": "looks good"}}])
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "a", "route": "a/m", "score": 10, "reason": "ok"}, {"id": "b", "route": "b/m", "score": 10, "reason": "ok"}]}, accepted=True)
    with pytest.raises(AdTemplateProcessError):
        process._assessment({"rubric": {**evidence(9.8)["rubric"], "overall": 10}, "reason": "extra score"}, "comparator")

def test_adversarial_reviewer_identity_route_and_self_score_fail():
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "same", "route": "a/m", **evidence(10, "ok")}, {"id": "same", "route": "b/m", **evidence(10, "ok")}]}, accepted=True)
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "a", "route": "a/m", **evidence(10, "ok")}, {"id": "b", "route": "a/m", **evidence(10, "ok")}]}, accepted=True)

def test_nonexistent_artifact_and_import_fail(tmp_path, monkeypatch):
    with pytest.raises(AdTemplateProcessError):
        validate_artifacts({"previews": [{"path": str(tmp_path / "missing.png"), "placement": "feed"}]}, tmp_path)
    monkeypatch.delenv("BLOCKWISE_TEMPLATE_IMPORT_URL", raising=False)
    with pytest.raises(AdTemplateProcessError):
        import_template({"template": {}}, run_id="trun-test", project_id="blockwise")

def test_layered_documents_are_stable():
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "stable", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": {"background": "#FFFFFF"}, "assets": {}, "fonts": [], "metadata": {}}
    first = deterministic_documents(template)
    assert first == deterministic_documents(template)
    assert list(first) == ["feed.json", "story.json", "template.json"]
    with pytest.raises(AdTemplateProcessError): deterministic_documents({"feed": {}, "story": {}})

def test_catalog_asset_resolution_rejects_unknown_and_traversal(tmp_path, monkeypatch):
    catalog = Path(process.__file__).resolve().parents[1] / "assets" / "ad-template-generator" / "catalog"
    template = {"schema": "blockwise.ad-template", "assets": {"property-photo": {"fileName": "interior/kitchen.webp", "mimeType": "image/webp"}}}
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(catalog))
    resolved = process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "interior/kitchen.webp", "mimeType": "image/webp"}])
    assert resolved[0]["bytesBase64"].startswith("UklGR")
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "missing", "fileName": "interior/kitchen.webp", "mimeType": "image/webp"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets({"schema": "blockwise.ad-template", "assets": {"x": {"fileName": "../property-photo.webp", "mimeType": "image/webp"}}}, [{"assetKey": "x", "fileName": "../property-photo.webp", "mimeType": "image/webp"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "interior/kitchen.webp", "mimeType": "image/png"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "interior/kitchen.webp", "mimeType": "image/webp", "bytesBase64": ""}])


def test_required_logo_capability_fails_before_provider_iteration(tmp_path, monkeypatch):
    committed = (
        Path(process.__file__).resolve().parents[1]
        / "assets" / "ad-template-generator" / "catalog"
    )
    catalog = tmp_path / "catalog-without-multi-gable"
    shutil.copytree(committed, catalog)
    (catalog / "brand" / "neutral-multi-gable.png").unlink()
    manifest_path = catalog / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"] = [
        asset for asset in manifest["assets"]
        if asset["fileName"] != "brand/neutral-multi-gable.png"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(catalog))
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    provider_calls = []
    brief = (
        "Emblem must be the source-free transparent multi-peak/two-gable silhouette; "
        "generic single-roof or baked cream backdrop fails. "
        "C15_TAIL_SENTINEL=TWO_GABLE_ONLY."
    )

    with pytest.raises(
        AdTemplateProcessError,
        match="add one source-free allowlisted brand/logo asset with roles=multi_peak,two_gable",
    ):
        SoleProcessOrchestrator(
            call_agent=lambda *args: provider_calls.append(args),
            workspace=tmp_path / "run",
            run_id="trun_capability_missing",
            project_id="blockwise",
            emit=lambda *_args: None,
        ).run(
            source=str(source), brief=brief, placements=["feed", "story"],
            routes=_quality_routes(),
        )

    assert provider_calls == []


def test_required_logo_capability_reaches_provider_when_allowlisted(tmp_path, monkeypatch):
    catalog = (
        Path(process.__file__).resolve().parents[1]
        / "assets" / "ad-template-generator" / "catalog"
    )
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(catalog))
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    provider_calls = []
    brief = (
        "Emblem must be the source-free transparent multi-peak/two-gable silhouette; "
        "generic single-roof or baked cream backdrop fails."
    )

    def provider_reached(*args):
        provider_calls.append(args)
        raise RuntimeError("provider reached")

    with pytest.raises(RuntimeError, match="provider reached"):
        SoleProcessOrchestrator(
            call_agent=provider_reached,
            workspace=tmp_path / "run",
            run_id="trun_capability_present",
            project_id="blockwise",
            emit=lambda *_args: None,
        ).run(
            source=str(source), brief=brief, placements=["feed", "story"],
            routes=_quality_routes(),
        )

    assert provider_calls and provider_calls[0][0] == "builder-1"


def test_builder_contract_is_strict_and_prompts_require_quality_scores():
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "strict", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata("Strict")}
    assert process.validate_template_artifact(template) is template
    wrong_story = json.loads(json.dumps(template)); wrong_story["storyLayout"]["placement"] = "feed"
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(wrong_story)
    unsafe_zone = json.loads(json.dumps(template)); unsafe_zone["storyLayout"]["safeZones"][0]["height"] = 1921
    with pytest.raises(AdTemplateProcessError, match=r"storyLayout\.safeZones\[0\].*y \+ height must be <= 1920"):
        process.validate_template_artifact(unsafe_zone)
    extra_metadata = json.loads(json.dumps(template)); extra_metadata["metadata"]["version"] = 2
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(extra_metadata)
    inline = json.loads(json.dumps(template)); inline["metadata"]["gallerySamples"]["bytesBase64"] = "forbidden"
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(inline)
    for final in (False, True):
        prompt = process.review_prompt(final=final, candidate={
            "template": {**template, "privateNote": "never expose"},
            "assets": [{"assetKey": "hero", "fileName": "home/open-home-living.webp", "mimeType": "image/webp", "bytesBase64": "forbidden"}],
        })
        for field in process.RUBRIC_FIELDS:
            assert field in prompt
        assert "hard failure" in prompt.lower()
        assert "source pixels flattened into the output" in prompt
        assert "The primary objective is not generic ad quality" in prompt
        assert "Neutral replacement photography" in prompt
        assert "required_changes" in prompt
        assert "Assess Story dead space only inside the content-safe band y=240..1620" in prompt
        assert "UI-unsafe bands, not mandatory blank bands" in prompt
        assert "never request them in required_changes" in prompt
        assert "never request an obvious source typo, clipped string, or duplicated feature" in prompt.lower()
        assert "placement=feed|story; layers=comma-separated layerIds" in prompt
        assert "check every proposed target rectangle against every existing layer" in prompt
        assert "Never propose a target that newly overlaps an image slot or opaque vector panel" in prompt
        assert "same non-logo photograph repeated in distinct visible slots" in prompt
        assert "Transcribe every visibly rendered word" in prompt
        assert "missing/split/clipped/truncated/garbled visible text" in prompt
        assert "absolute canvas pixels" in prompt
        if final:
            assert "do not return required_changes" in prompt
            assert "Hermes derives the next builder brief" in prompt
            assert "Return JSON only with exactly reason, differences, visible_strings, semantic_glyph_inventory, mark_badge_treatment, hard_failures, and rubric" in prompt
            assert "filled versus outline treatment" in prompt
        else:
            assert "required_changes must contain at least one actionable item" in prompt
            assert "mark_badge_treatment" in prompt
            assert "brand_silhouette_features" in prompt
            assert "A source multi-part brand silhouette simplified to a generic mark" in prompt
        assert '"templateId":"strict"' in prompt
        assert '"assetKey":"hero"' in prompt
        assert "privateNote" not in prompt
        assert "bytesBase64" not in prompt
    builder = process.generator_prompt(run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png")
    for key in process.METADATA_FIELDS:
        assert key in builder
    assert "never emit bytesBase64 anywhere" in builder
    assert "interior/living-bright.webp" in builder
    assert "procedural/tower-skyline.webp" in builder
    assert "roles=living_room,lounge,bright_interior" in builder
    assert "usage=neutral-placeholder" in builder
    assert "home/open-home-living.webp" not in builder
    assert "brand/neutral-real-estate.png" in builder
    assert "Never leave a source-visible logo region blank" in builder
    assert 'Feed is 1080x1350 with safeZones=[{"x":72,"y":96,"width":936,"height":1158}]' in builder
    assert 'Story is 1080x1920 with safeZones=[{"x":72,"y":240,"width":936,"height":1380}]' in builder
    assert "Geometry is always {x,y,width,height} from the top-left" in builder
    assert "Do not redesign, simplify, modernise, improve, or reinterpret the source" in builder
    assert "structural inspiration, not a quality ceiling" not in builder
    assert '"template": {...}, "assets": []' in builder
    assert "mask must be exactly rounded_rect, circle, or none" in builder
    assert 'defaultCrop must be exactly {"x":0,"y":0,"width":1,"height":1}' in builder
    assert "overflowBehaviour must be exactly refuse, truncate, or scale_down" in builder
    assert 'fonts must always be a JSON list such as [{"file":"manrope-400.woff2"}' in builder
    assert "The only bundled font filenames allowed" in builder
    assert "Every other font filename is forbidden" in builder
    for font_file in process.ALLOWED_FONT_FILES:
        assert font_file in builder
    assert "shape must be exactly one of rect, rounded, circle, line, pill, notched, wave, or ring" in builder
    assert "Every layout requires one full-canvas background plate" in builder
    assert "ring is circular-only and requires square geometry" in builder
    assert "A plate is only a plain rectangular fill and cannot express corners" in builder
    assert "never use same-bounds stacked filled plates" in builder
    assert "lineHeight is a unitless multiplier between 0.8 and 2.5" in builder
    assert "Every multiline text layer with maxLines>1 requires lineHeight>=1.0" in builder
    assert "fontSize must be at least 24 native canvas pixels in Feed and 32 in Story" in builder
    assert "Every icon layer must use exactly arrow, check, phone, mail, globe, or pin" in builder
    assert "Every image_slot inputKey must be declared exactly once in imageInputs" in builder
    assert "real logo layer" in builder
    assert "preserve its layout regions" in builder
    assert "image-slot count and shapes" in builder
    assert "set specialAdCategory to HOUSING" in builder
    assert "Every text layer inputKey must be declared exactly once in textInputs" in builder
    assert 'Each realAssetRefs entry must contain exactly {"inputKey":"declaredKey"' in builder
    assert "Every layer assetKey, image defaultAssetKey, gallery sample assetKey" in builder
    assert "Story must not retain the Feed's horizontal split" in builder
    feedback = json.dumps({
        "rubric": {field: 8.7 for field in process.RUBRIC_FIELDS},
        "minimum_score": 8.7,
        "hard_failures": ["failure detail"],
        "differences": ["difference detail"],
        "required_changes": [
            "placement=story; layers=story-headline; current={x:72,y:260,width:700,height:140}; "
            "target={x:72,y:280,width:720,height:120}; change=Move the headline"
        ],
        "reason": "r" * 6000 + "UNTRUNCATED-END",
    })
    feedback_builder = process.generator_prompt(
        run_id="run", project_id="blockwise", brief="", placements=["feed", "story"],
        source="source.png", feedback=feedback,
    )
    for key in ("rubric", "minimum_score", "hard_failures", "required_changes", "reason"):
        assert f'"{key}"' in feedback_builder
    assert '"differences"' not in feedback_builder
    assert "UNTRUNCATED-END" not in feedback_builder
    brief_tail = "C13_REPAIR_BRIEF_TAIL_SENTINEL"
    repair_brief = ("repair-brief-" + ("x" * 3000) + brief_tail)
    repair_builder = process.generator_prompt(
        run_id="run", project_id="blockwise", brief=repair_brief, placements=["feed", "story"], source="source.png",
        validation_feedback="template is invalid", repair_attempt=1,
        rejected_candidate={"template": {"templateId": "broken", "bytesBase64": "forbidden", "privateNote": "secret"}, "assets": []},
    )
    assert "IMMEDIATELY PRIOR REJECTED CANDIDATE" in repair_builder
    assert '"templateId":"broken"' in repair_builder
    assert '"bytesBase64":' not in repair_builder
    assert "privateNote" not in repair_builder
    assert "secret" not in repair_builder
    assert "The only bundled font filenames allowed" in repair_builder
    assert brief_tail in repair_builder
    for font_file in process.ALLOWED_FONT_FILES:
        assert font_file in repair_builder
    oversized_candidate = {
        "template": {
            "metadata": {
                "metaCopyDefaults": {
                    "primaryText": ["x" * 8000 for _ in range(20)],
                },
            },
        },
        "assets": [],
    }
    with pytest.raises(AdTemplateProcessError, match=r"safe candidate context exceeds 100000 characters"):
        process.generator_prompt(
            run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png",
            prior_candidate=oversized_candidate,
        )
    invalid_fonts = json.loads(json.dumps(template))
    invalid_fonts["fonts"] = {"body": {"file": "manrope-400.woff2"}}
    with pytest.raises(AdTemplateProcessError, match=r"fonts must be a JSON list.*never an object or map"):
        process.validate_template_artifact(invalid_fonts)

    invalid_slot = json.loads(json.dumps(template))
    invalid_slot["feedLayout"]["layers"].append({
        "type": "image_slot", "layerId": "feed-hero", "inputKey": "heroImage",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 100},
        "mask": "rounded", "minSourceWidth": 100, "minSourceHeight": 100,
        "defaultCrop": "cover", "allowedPlacementOverrides": ["story"],
    })
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.mask must be one of rounded_rect"):
        process.validate_template_artifact(invalid_slot)

    invalid_crop = json.loads(json.dumps(invalid_slot))
    invalid_crop["feedLayout"]["layers"][1]["mask"] = "rounded_rect"
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.defaultCrop must contain exactly"):
        process.validate_template_artifact(invalid_crop)

    invalid_override = json.loads(json.dumps(invalid_crop))
    invalid_override["feedLayout"]["layers"][1]["defaultCrop"] = {"x": 0, "y": 0, "width": 1, "height": 1}
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.allowedPlacementOverrides\[0\] must be crop or position"):
        process.validate_template_artifact(invalid_override)

    invalid_text = json.loads(json.dumps(template))
    invalid_text["feedLayout"]["layers"].append({
        "type": "text", "layerId": "feed-headline", "inputKey": "headline",
        "font": {"file": "manrope-700.woff2"}, "fontSize": 32, "lineHeight": 1.1,
        "tracking": 0, "alignment": "center", "maxCharacters": 80, "maxLines": 2,
        "colourRole": "mainText", "overflowBehaviour": "ellipsis",
        "geometry": {"x": 10, "y": 10, "width": 400, "height": 100},
    })
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.overflowBehaviour must be refuse, truncate, or scale_down"):
        process.validate_template_artifact(invalid_text)

    invalid_line_height = json.loads(json.dumps(invalid_text))
    invalid_line_height["feedLayout"]["layers"][1]["overflowBehaviour"] = "truncate"
    invalid_line_height["feedLayout"]["layers"][1]["lineHeight"] = 29
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.lineHeight=29 must be a unitless multiplier between 0\.8 and 2\.5"):
        process.validate_template_artifact(invalid_line_height)

    overlapping_multiline = json.loads(json.dumps(invalid_text))
    overlapping_multiline["feedLayout"]["layers"][1]["overflowBehaviour"] = "truncate"
    overlapping_multiline["feedLayout"]["layers"][1]["lineHeight"] = 0.8
    with pytest.raises(
        AdTemplateProcessError,
        match=r'feedLayout\.layers\[1\] text layer "feed-headline" in feed has lineHeight=0\.8; '
        r'multiline maxLines=2 requires lineHeight>=1\.0',
    ):
        process.validate_template_artifact(overlapping_multiline)

    micro_feed_text = json.loads(json.dumps(invalid_line_height))
    micro_feed_text["feedLayout"]["layers"][1]["lineHeight"] = 1.2
    micro_feed_text["feedLayout"]["layers"][1]["fontSize"] = 18
    with pytest.raises(AdTemplateProcessError, match=r"fontSize must be at least 24 native canvas pixels"):
        process.validate_template_artifact(micro_feed_text)

    micro_story_text = json.loads(json.dumps(template))
    micro_story_text["storyLayout"]["layers"].append({
        "type": "text", "layerId": "story-contact", "inputKey": "contact",
        "font": {"file": "manrope-400.woff2"}, "fontSize": 28, "lineHeight": 1.2,
        "tracking": 0, "alignment": "left", "maxCharacters": 100, "maxLines": 2,
        "colourRole": "mainText", "overflowBehaviour": "refuse",
        "geometry": {"x": 72, "y": 1400, "width": 600, "height": 100},
    })
    with pytest.raises(AdTemplateProcessError, match=r"fontSize must be at least 32 native canvas pixels"):
        process.validate_template_artifact(micro_story_text)
    invalid_vector = json.loads(json.dumps(template))
    invalid_vector["feedLayout"]["layers"].append({
        "type": "vector", "layerId": "feed-rule",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 4},
        "shape": "rectangle", "colourRole": "accent", "opacity": 1,
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.shape="rectangle" must be one of rect, rounded'):
        process.validate_template_artifact(invalid_vector)

    elongated_ring = json.loads(json.dumps(template))
    elongated_ring["feedLayout"]["layers"].append({
        "type": "vector", "layerId": "feed-border",
        "geometry": {"x": 22, "y": 22, "width": 1036, "height": 1306},
        "shape": "ring", "colourRole": "accent", "opacity": 1,
    })
    with pytest.raises(AdTemplateProcessError, match=r"shape=ring is circular-only and requires square geometry"):
        process.validate_template_artifact(elongated_ring)

    missing_background = json.loads(json.dumps(template))
    missing_background["storyLayout"]["layers"][0] = {
        "type": "vector", "layerId": "story-fill",
        "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920},
        "shape": "rect", "colourRole": "background", "opacity": 1,
    }
    with pytest.raises(AdTemplateProcessError, match=r"storyLayout must contain one full-canvas background plate"):
        process.validate_template_artifact(missing_background)

    hard_line_overflow = json.loads(json.dumps(template))
    hard_line_overflow["textInputs"] = [{
        "key": "facts", "label": "Facts",
        "placeholder": "One\nTwo\nThree\nFour\nFive\nSix", "maxLength": 160,
    }]
    hard_line_overflow["fonts"] = [{"file": "manrope-400.woff2"}]
    for placement, max_lines in (("feed", 6), ("story", 3)):
        hard_line_overflow[f"{placement}Layout"]["layers"].append({
            "type": "text", "layerId": f"{placement}-facts", "inputKey": "facts",
            "font": {"file": "manrope-400.woff2"}, "fontSize": 32, "lineHeight": 1.2,
            "tracking": 0, "alignment": "left", "maxCharacters": 160,
            "maxLines": max_lines, "colourRole": "mainText", "overflowBehaviour": "scale_down",
            "geometry": {"x": 72, "y": 240 if placement == "story" else 96, "width": 600, "height": 500},
        })
    with pytest.raises(AdTemplateProcessError, match=r'visible text input "facts" placeholder has 6 hard lines.*maxLines=3'):
        process.validate_template_artifact(hard_line_overflow)

    invalid_icon = json.loads(json.dumps(template))
    invalid_icon["feedLayout"]["layers"].append({
        "type": "icon", "layerId": "feed-phone",
        "geometry": {"x": 10, "y": 10, "width": 40, "height": 40},
        "icon": "fax", "colourRole": "mainText",
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.icon="fax" must be one of arrow, check, phone, mail, globe, pin'):
        process.validate_template_artifact(invalid_icon)

    invalid_logo = json.loads(json.dumps(template))
    invalid_logo["feedLayout"]["layers"].append({
        "type": "logo", "layerId": "feed-logo", "inputKey": "brandLogo",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 100},
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.inputKey="brandLogo" for logo is undeclared; declare it exactly once in imageInputs'):
        process.validate_template_artifact(invalid_logo)

    blank_logo = json.loads(json.dumps(invalid_logo))
    blank_logo["imageInputs"] = [{"key": "brandLogo", "label": "Brand logo", "acceptedTypes": ["image/png"]}]
    with pytest.raises(AdTemplateProcessError, match=r'visible logo input "brandLogo" must declare a non-blank defaultAssetKey'):
        process.validate_template_artifact(blank_logo)
    blank_logo["assets"] = {
        "brand": {"fileName": "brand/neutral-real-estate.png", "mimeType": "image/png"},
    }
    blank_logo["imageInputs"][0]["defaultAssetKey"] = "brand"
    assert process.validate_template_artifact(blank_logo) is blank_logo

    real_text_ref = json.loads(json.dumps(template))
    real_text_ref["textInputs"] = [{"key": "address", "label": "Address", "placeholder": "1 Example St", "maxLength": 120}]
    real_text_ref["metadata"]["realAssetRefs"] = [{"inputKey": "address", "kind": "property_address", "required": True}]
    assert process.validate_template_artifact(real_text_ref) is real_text_ref

    invalid_real_ref = json.loads(json.dumps(template))
    invalid_real_ref["metadata"]["realAssetRefs"] = [{"inputKey": "missing", "kind": "property_photo", "required": True}]
    with pytest.raises(AdTemplateProcessError, match=r'metadata\.realAssetRefs\[0\]\.inputKey="missing" is undeclared'):
        process.validate_template_artifact(invalid_real_ref)

    duplicate_input = json.loads(json.dumps(real_text_ref))
    duplicate_input["imageInputs"] = [{"key": "address", "label": "Address image", "acceptedTypes": ["image/png"]}]
    with pytest.raises(AdTemplateProcessError, match=r'input key "address" must be unique across imageInputs and textInputs'):
        process.validate_template_artifact(duplicate_input)


def test_story_essential_layers_stay_inside_content_safe_zone_but_visual_layers_may_bleed():
    template = valid_candidate("story-safe-zone")["template"]
    template["storyLayout"]["safeZones"] = [dict(process.STORY_CONTENT_SAFE_ZONE)]
    template["imageInputs"] = [
        {"key": "story-photo", "label": "Story photo", "acceptedTypes": ["image/jpeg"]},
        {"key": "story-logo", "label": "Story logo", "acceptedTypes": ["image/png"], "defaultAssetKey": "brand"},
    ]
    template["assets"] = {
        "brand": {"fileName": "brand/neutral-real-estate.png", "mimeType": "image/png"},
    }
    template["textInputs"] = [
        {"key": "story-headline", "label": "Story headline", "placeholder": "Just listed", "maxLength": 80},
    ]
    template["fonts"] = [{"file": "manrope-700.woff2"}]
    decorative_layers = [
        {
            "type": "image_slot", "layerId": "story-photo", "inputKey": "story-photo",
            "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920}, "mask": "none",
            "minSourceWidth": 1080, "minSourceHeight": 1920,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
        {
            "type": "vector", "layerId": "story-decor", "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920},
            "shape": "rect", "colourRole": "accent", "opacity": 0.2,
        },
    ]
    essential_layers = [
        {
            "type": "text", "layerId": "story-headline", "inputKey": "story-headline",
            "font": {"file": "manrope-700.woff2"}, "fontSize": 64, "lineHeight": 1.1,
            "tracking": 0, "alignment": "left", "maxCharacters": 80, "maxLines": 2,
            "colourRole": "mainText", "overflowBehaviour": "refuse",
            "geometry": {"x": 72, "y": 240, "width": 720, "height": 160},
        },
        {
            "type": "logo", "layerId": "story-logo", "inputKey": "story-logo",
            "geometry": {"x": 820, "y": 260, "width": 160, "height": 100},
        },
        {
            "type": "icon", "layerId": "story-pin", "icon": "pin", "colourRole": "accent",
            "geometry": {"x": 72, "y": 1540, "width": 48, "height": 48},
        },
    ]
    template["storyLayout"]["layers"].extend(decorative_layers + essential_layers)
    assert process.validate_template_artifact(template) is template

    for layer_id, geometry in (
        ("story-headline", {"x": 72, "y": 220, "width": 720, "height": 160}),
        ("story-logo", {"x": 820, "y": 1580, "width": 160, "height": 100}),
        ("story-pin", {"x": 40, "y": 1540, "width": 48, "height": 48}),
    ):
        invalid = json.loads(json.dumps(template))
        layer = next(item for item in invalid["storyLayout"]["layers"] if item["layerId"] == layer_id)
        layer["geometry"] = geometry
        with pytest.raises(AdTemplateProcessError, match="Story content-safe zone"):
            process.validate_template_artifact(invalid)


def test_production_035_split_letter_tracking_is_rejected_before_render():
    template = valid_candidate("meta-035")['template']
    template["textInputs"] = [
        {"key": "headline", "label": "Headline", "placeholder": "NEW", "maxLength": 80},
    ]
    template["fonts"] = [{"file": "manrope-700.woff2"}]
    template["feedLayout"]["layers"].append({
        "type": "text", "layerId": "feed-headline", "inputKey": "headline",
        "font": {"file": "manrope-700.woff2"}, "fontSize": 42, "lineHeight": 1.1,
        "tracking": 48, "alignment": "left", "maxCharacters": 80, "maxLines": 1,
        "colourRole": "mainText", "overflowBehaviour": "refuse",
        "geometry": {"x": 72, "y": 96, "width": 400, "height": 80},
    })
    with pytest.raises(AdTemplateProcessError, match="absolute canvas-pixel value between -4 and 4"):
        process.validate_template_artifact(template)


def test_production_149_repeated_default_photos_are_rejected_before_render():
    template = valid_candidate("meta-149")['template']
    template["assets"] = {
        "property": {"fileName": "home/open-home-living.webp", "mimeType": "image/webp"},
    }
    template["imageInputs"] = [
        {"key": "hero", "label": "Hero", "acceptedTypes": ["image/webp"], "defaultAssetKey": "property"},
        {"key": "detail", "label": "Detail", "acceptedTypes": ["image/webp"], "defaultAssetKey": "property"},
    ]
    for index, input_key in enumerate(("hero", "detail")):
        template["feedLayout"]["layers"].append({
            "type": "image_slot", "layerId": f"feed-{input_key}", "inputKey": input_key,
            "geometry": {"x": 72 + index * 420, "y": 200, "width": 400, "height": 300},
            "mask": "none", "minSourceWidth": 400, "minSourceHeight": 300,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        })
    with pytest.raises(AdTemplateProcessError, match="must not repeat default asset"):
        process.validate_template_artifact(template)


@pytest.mark.parametrize("defect", ["blank_text", "missing_logo"])
def test_production_180_blank_visible_roles_are_rejected_before_render(defect):
    template = valid_candidate("meta-180")['template']
    if defect == "blank_text":
        template["textInputs"] = [
            {"key": "price", "label": "Price", "placeholder": "   ", "maxLength": 80},
        ]
        template["fonts"] = [{"file": "manrope-700.woff2"}]
        template["feedLayout"]["layers"].append({
            "type": "text", "layerId": "feed-price", "inputKey": "price",
            "font": {"file": "manrope-700.woff2"}, "fontSize": 42, "lineHeight": 1.1,
            "tracking": 0, "alignment": "left", "maxCharacters": 80, "maxLines": 1,
            "colourRole": "mainText", "overflowBehaviour": "refuse",
            "geometry": {"x": 72, "y": 900, "width": 400, "height": 80},
        })
        expected = "must have a non-blank placeholder"
    else:
        template["assets"] = {
            "brand": {"fileName": "brand/neutral-real-estate.png", "mimeType": "image/png"},
        }
        template["imageInputs"] = [{
            "key": "brand", "label": "Brand", "acceptedTypes": ["image/png"],
            "defaultAssetKey": "brand",
        }]
        template["metadata"]["realAssetRefs"] = [
            {"inputKey": "brand", "kind": "brand_logo", "required": True},
        ]
        expected = "must have a visible logo layer"
    with pytest.raises(AdTemplateProcessError, match=expected):
        process.validate_template_artifact(template)


def test_review_evidence_requires_visible_string_transcription():
    missing = evidence(9.7, "Looks ready")
    missing.pop("visible_strings")
    with pytest.raises(process.ReviewEvidenceError, match="must transcribe visible source, Feed, and Story strings"):
        process.validate_iterations([{"iteration": 1, "comparison": missing, "decision": "accepted"}])


def test_review_vision_paths_append_renderer_meta_shells_after_native_renders(tmp_path):
    paths = []
    for name in ("source.png", "feed.png", "story.png", "feed-shell.png", "story-shell.png"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(str(path))
    candidate = {
        "render": {"feed": paths[1], "story": paths[2]},
        "review_previews": [
            {"path": paths[3], "placement": "meta-feed"},
            {"path": paths[4], "placement": "meta-story"},
        ],
    }
    assert process.review_vision_paths(paths[0], candidate) == paths


def test_review_vision_paths_places_immutable_best_before_current_and_shells(tmp_path):
    names = (
        "source.png", "best-feed.png", "best-story.png", "current-feed.png",
        "current-story.png", "editor.png", "meta.png",
    )
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(str(path))
    previous_best = {"render": {"feed": paths[1], "story": paths[2]}}
    candidate = {
        "render": {"feed": paths[3], "story": paths[4]},
        "review_previews": [
            {"path": paths[5], "placement": "editor"},
            {"path": paths[6], "placement": "meta-feed"},
        ],
    }
    assert process.review_vision_paths(paths[0], candidate, previous_best) == paths


def test_hierarchical_comparator_blocks_regression_and_critical_region_despite_high_mean():
    regressed = evidence(9.4, "Restore previous-best logo integrity")
    regressed["rubric"] = {field: 9.8 for field in process.RUBRIC_FIELDS}
    regressed["macro"] = {field: 9.8 for field in process.MACRO_FIELDS}
    regressed["regressions"] = ["Logo integrity regressed versus previous-best"]
    regressed["decision"] = "revise"
    parsed = process._assessment(regressed, "comparator", require_change_list=True)
    assert parsed["score"] == 0
    assert parsed["macro_regression"] is True
    assert process._passes_quality_gate(parsed) is False

    blocked = evidence(9.4, "Restore full price glyphs")
    blocked["rubric"] = {field: 9.8 for field in process.RUBRIC_FIELDS}
    blocked["macro"] = {field: 9.8 for field in process.MACRO_FIELDS}
    blocked["critical_regions"] = [{
        "region": "price", "status": "blocker", "findings": ["Last digit is clipped"],
    }]
    blocked["decision"] = "revise"
    parsed = process._assessment(blocked, "comparator", require_change_list=True)
    assert parsed["score"] == 0
    assert parsed["critical_blocker"] is True
    assert process._passes_quality_gate(parsed) is False


def test_hierarchical_comparator_keeps_all_material_changes_beyond_top_three():
    review = evidence(9.0)
    review["required_changes"] *= 4
    review["ranked_changes"] = list(review["required_changes"])
    remap_inventory(review)
    parsed = process._assessment(review, "comparator", require_change_list=True)
    assert len(parsed["required_changes"]) == 4


def test_comparator_inventory_requires_all_explicit_micro_checks_and_change_coverage():
    missing_check = evidence(9.0, "Missing source-visible neutral brand text")
    missing_check["source_inventory"]["micro_checks"] = [
        item for item in missing_check["source_inventory"]["micro_checks"]
        if item["check"] != "brand_text"
    ]
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="must cover brand text, mark/badge treatment, dividers, bullet count/stacking",
    ):
        process._assessment(missing_check, "comparator", require_change_list=True)

    uncovered = evidence(9.0, "Divider missing between About and Features")
    uncovered["source_inventory"]["micro_checks"][3].update(
        status="mismatch", material=True,
        findings=["Divider missing between About and Features"],
        required_change_refs=[],
    )
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="material mismatch must map to a concrete required_change",
    ):
        process._assessment(uncovered, "comparator", require_change_list=True)

    semantic = evidence(9.0, "Hollow rings replace distinct phone, mail, and web glyphs")
    remap_inventory(semantic, check="semantic_glyphs")
    parsed = process._assessment(semantic, "comparator", require_change_list=True)
    glyph_check = parsed["source_inventory"]["micro_checks"][-1]
    assert glyph_check["check"] == "semantic_glyphs"
    assert glyph_check["material"] is True
    assert glyph_check["required_change_refs"] == [1]


@pytest.mark.parametrize(
    ("placement", "field", "value"),
    [
        ("feed", "brand_silhouette_features", ["door", "single-roof"]),
        ("story", "phone_badge", {"shape": "none", "fillTreatment": "none"}),
    ],
)
def test_comparator_cannot_match_changed_brand_silhouette_or_icon_badge(
    placement, field, value,
):
    review = evidence(9.6, "Source micro treatments match")
    treatment = next(
        item for item in review["source_inventory"]["micro_checks"]
        if item["check"] == "mark_badge_treatment"
    )
    treatment[f"{placement}_observation"][field] = value

    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="cannot declare match when brand silhouette features or semantic icon badge treatments differ",
    ):
        process._assessment(review, "comparator", require_change_list=True)


def test_comparator_inventory_requires_verbatim_difference_and_every_change_reference():
    review = evidence(9.0, "Six source bullets collapsed into two run-on lines")
    review["differences"] = [
        *review["differences"], "Price punctuation differs from the source",
    ]
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="differences must each appear verbatim",
    ):
        process._assessment(review, "comparator", require_change_list=True)

    review = evidence(9.0, "Six source bullets collapsed into two run-on lines")
    review["required_changes"].append(review["required_changes"][0])
    review["ranked_changes"] = list(review["required_changes"])
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match="required_changes must each be referenced",
    ):
        process._assessment(review, "comparator", require_change_list=True)


def test_hierarchical_comparator_rejects_macro_weakness_despite_passing_micro_mean():
    review = evidence(9.4, "Restore native Story hierarchy")
    review["rubric"] = {field: 9.8 for field in process.RUBRIC_FIELDS}
    review["macro"] = {field: 9.8 for field in process.MACRO_FIELDS}
    review["macro"]["native_story_composition"] = 9.1
    parsed = process._assessment(review, "comparator", require_change_list=True)
    assert parsed["score"] == 9.8
    assert process._passes_quality_gate(parsed) is False



def test_orchestrator_calls_real_roles_and_persists_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    # Exercise the real Hermes renderer-CLI boundary without coupling this
    # repository's platform-neutral test to a Blockwise checkout at a fixed
    # Linux path.  The renderer itself is covered in Blockwise; here the fake
    # process writes its public receipt contract and real native PNGs.
    monkeypatch.setenv(
        "AD_TEMPLATE_GENERATOR_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(tmp_path / 'renderer-stub.py'))}",
    )

    def renderer_process(argv, **_kwargs):
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for placement, dimensions in (("feed", (1080, 1350)), ("story", (1080, 1920))):
            path = out_dir / f"{placement}.png"
            Image.new("RGB", dimensions, "white").save(path, format="PNG")
            outputs[placement] = {
                "path": str(path), "width": dimensions[0], "height": dimensions[1],
            }
        (out_dir / "receipt.json").write_text(
            json.dumps({"outputs": outputs}), encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(process.subprocess, "run", renderer_process)
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl_real", "status": "imported"})
    calls = []
    events = []
    def layout(placement, height):
        return {"placement": placement, "layers": [{"type": "plate", "layerId": f"{placement}-bg", "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "candidate", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata()}

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance.startswith("builder-"):
            return {"template": template, "assets": []}
        if instance.startswith("comparator-"):
            return evidence(9.6, "Composition is ready")
        return evidence(9.7, "Final review is ready")

    brief_tail = "C13_ALL_REVIEW_ROLES_TAIL_SENTINEL"
    run_brief = "immutable-brief-" + ("x" * 3000) + brief_tail
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_test",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief=run_brief, placements=["square"], routes=[
        {"provider": "builder", "model": "cheap-a"},
        {"provider": "compare", "model": "cheap-b"},
        {"provider": "review-a", "model": "cheap-c"},
        {"provider": "review-b", "model": "cheap-d"},
    ])
    assert [item[0].split("-")[0] for item in calls] == ["builder", "comparator", "final", "final"]
    assert all(isinstance(item[1], list) for item in calls)
    assert len(calls[0][1]) == 2 and len(calls[1][1]) == 4 and len(calls[2][1]) == 4 and len(calls[3][1]) == 4
    assert all(brief_tail in calls[index][1][0]["text"] for index in (0, 1, 2, 3))
    assert all(part["type"] == "image_url" for part in calls[1][1][1:])
    compared = [item for item in events if item[0] == "iteration.compared"]
    assert len(compared) == 1
    assert {
        "rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason",
    }.issubset(compared[0][1])
    completed = [item for item in events if item[0] == "final-review.completed"]
    assert len(completed) == 1 and len(completed[0][1]["reviewers"]) == 2
    assert [
        item["route"] for item in result["final_review"]["reviewers"]
    ] == ["review-a/cheap-c", "review-b/cheap-d"]
    assert result["import"] == {"template_id": "tpl_real", "status": "imported"}
    assert all(Path(item["path"]).is_file() for item in result["previews"])
    dimensions = {}
    for item in result["previews"]:
        raw = Path(item["path"]).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        dimensions[item["placement"]] = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
    assert dimensions == {"feed": (1080, 1350), "story": (1080, 1920)}

def test_vision_roles_reject_filename_only_inputs(tmp_path):
    with pytest.raises(AdTemplateProcessError):
        vision_message("inspect this", [str(tmp_path / "filename-only.png")])


def test_five_image_comparator_transport_is_bounded_below_one_megabyte(tmp_path):
    source = tmp_path / "large.png"
    Image.effect_noise((2400, 2400), 100).convert("RGB").save(source, format="PNG")
    message = vision_message("compare", [str(source)] * 5, bounded=True)
    assert len(json.dumps(message).encode("utf-8")) < 1_000_000
    for item in message[1:]:
        encoded = item["image_url"]["url"].split(",", 1)[1]
        assert len(base64.b64decode(encoded)) <= process.VISION_MAX_SERIALIZED_IMAGE_BYTES


def test_revision_builder_request_keeps_exact_candidate_but_stays_below_transport_budget(tmp_path):
    source = tmp_path / "large-source.png"
    Image.effect_noise((2600, 2600), 100).convert("RGB").save(source, format="PNG")
    prior = valid_candidate("exact-prior-candidate")
    feedback = json.dumps({
        "instruction": "Revise the immutable best-so-far candidate",
        "best_score": 9.1,
        "current_review": {
            "rubric": {field: 9.1 for field in process.RUBRIC_FIELDS},
            "macro": {field: 9.2 for field in process.MACRO_FIELDS},
            "minimum_score": 9.1,
            "hard_failures": [],
            "differences": ["x" * 4000 for _ in range(4)],
            "required_changes": [
                "placement=story; layers=story-hero; current={x:72,y:350,width:620,height:470}; "
                "target={x:72,y:350,width:936,height:620}; change=stack the price card below a full-width hero"
            ],
            "ranked_changes": ["duplicate verbose field" * 200],
            "critical_regions": [{"region": "story", "status": "pass", "findings": ["x" * 3000]}],
            "regressions": [],
            "reason": "r" * 8000,
        },
    })
    brief_tail = "C13_VALID_REVISION_BRIEF_TAIL_SENTINEL"
    revision_brief = "006 listing " + ("x" * 3000) + brief_tail
    prompt = process.generator_prompt(
        run_id="run", project_id="blockwise", brief=revision_brief,
        placements=["feed", "story"], source=str(source), feedback=feedback,
        prior_candidate=prior,
    )
    message = vision_message(prompt, [str(source)], bounded=True)
    serialized = json.dumps(message).encode("utf-8")

    assert '"templateId":"exact-prior-candidate"' in prompt
    assert brief_tail in prompt
    assert "PRIOR VALID CANDIDATE" in prompt
    assert '"differences"' not in prompt
    assert len(prompt.encode("utf-8")) < 80_000
    assert len(serialized) < process.VISION_MAX_SERIALIZED_MESSAGE_BYTES
    encoded = message[1]["image_url"]["url"].split(",", 1)[1]
    assert len(base64.b64decode(encoded)) <= process.VISION_MAX_SERIALIZED_IMAGE_BYTES


def test_live_006_repeated_feed_topology_is_detected_until_story_reflows_vertically():
    def layer(kind, key, geometry):
        return {"type": kind, "layerId": f"layer-{key}", "inputKey": key, "geometry": geometry}

    feed = [
        layer("text", "listingTitle", {"x": 72, "y": 70, "width": 936, "height": 76}),
        layer("image_slot", "propertyHero", {"x": 86, "y": 183, "width": 545, "height": 389}),
        layer("logo", "brandLogo", {"x": 680, "y": 207, "width": 224, "height": 104}),
        layer("image_slot", "livingPhoto", {"x": 86, "y": 582, "width": 281, "height": 227}),
        layer("image_slot", "kitchenPhoto", {"x": 376, "y": 582, "width": 281, "height": 227}),
        layer("image_slot", "bathroomPhoto", {"x": 666, "y": 582, "width": 280, "height": 227}),
        layer("text", "aboutCopy", {"x": 86, "y": 929, "width": 375, "height": 184}),
        layer("text", "featuresCopy", {"x": 618, "y": 929, "width": 328, "height": 184}),
    ]
    story = [
        layer("text", "listingTitle", {"x": 72, "y": 246, "width": 936, "height": 75}),
        layer("image_slot", "propertyHero", {"x": 72, "y": 350, "width": 620, "height": 470}),
        layer("logo", "brandLogo", {"x": 742, "y": 385, "width": 224, "height": 108}),
        layer("image_slot", "livingPhoto", {"x": 72, "y": 832, "width": 304, "height": 250}),
        layer("image_slot", "kitchenPhoto", {"x": 384, "y": 832, "width": 304, "height": 250}),
        layer("image_slot", "bathroomPhoto", {"x": 696, "y": 832, "width": 312, "height": 250}),
        layer("text", "aboutCopy", {"x": 72, "y": 1195, "width": 430, "height": 184}),
        layer("text", "featuresCopy", {"x": 600, "y": 1195, "width": 408, "height": 184}),
    ]
    candidate = {"template": {
        "feedLayout": {"layers": feed}, "storyLayout": {"layers": story},
    }}
    assert process._story_repeats_feed_topology(candidate) is True

    next(item for item in story if item["inputKey"] == "brandLogo")["geometry"].update(
        {"x": 72, "y": 840, "width": 300, "height": 108}
    )
    next(item for item in story if item["inputKey"] == "aboutCopy")["geometry"].update(
        {"x": 72, "y": 1120, "width": 936, "height": 184}
    )
    next(item for item in story if item["inputKey"] == "featuresCopy")["geometry"].update(
        {"x": 72, "y": 1320, "width": 936, "height": 184}
    )
    assert process._story_repeats_feed_topology(candidate) is False
    assert "repeats the Feed topology" in process.review_prompt(final=False, candidate=valid_candidate())


def test_blockwise_import_contract_uses_hmac_and_camel_case_receipt(monkeypatch, tmp_path):
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"templateId":"tpl-1","assetCount":2,"replayed":false}'
    def fake_urlopen(request, timeout):
        seen["authorization"] = request.headers.get("Authorization")
        seen["timestamp"] = request.headers.get("X-blockwise-timestamp")
        seen["nonce"] = request.headers.get("X-blockwise-nonce")
        seen["scope"] = request.headers.get("X-blockwise-scope")
        seen["signature"] = request.headers.get("X-blockwise-signature")
        seen["host"] = request.headers.get("Host")
        seen["body"] = json.loads(request.data.decode())
        return Response()
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://blockwise.test/import")
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_HOST", "blockwise.sale")
    secret = "a-secure-internal-secret-that-is-over-32-chars"
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", secret)
    monkeypatch.setattr(process.urllib.request, "urlopen", fake_urlopen)
    feed = tmp_path / "feed.png"; story = tmp_path / "story.png"
    feed.write_bytes(b"feed-png"); story.write_bytes(b"story-png")
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "trun-test", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {"feed": {"fileName": "feed.png", "mimeType": "image/png"}, "story": {"fileName": "story.png", "mimeType": "image/png"}}, "fonts": [], "metadata": metadata()}
    output = {"template": template, "assets": [{"assetKey": "feed", "fileName": "feed.png", "mimeType": "image/png", "bytesBase64": "ZmVlZC1wbmc="}, {"assetKey": "story", "fileName": "story.png", "mimeType": "image/png", "bytesBase64": "c3RvcnktcG5n"}], "previews": [{"path": str(feed), "placement": "feed"}, {"path": str(story), "placement": "story"}]}
    receipt = process.import_template(output, run_id="trun-test", project_id="blockwise")
    assert seen["authorization"] is None
    assert seen["host"] == "blockwise.sale"
    assert seen["scope"] == "adstudio.templates"
    assert seen["timestamp"].isdigit()
    assert len(seen["nonce"]) == 32
    raw_body = json.dumps(seen["body"]).encode()
    signed = "\n".join((
        "v1", seen["timestamp"], seen["nonce"], seen["scope"], "POST", "/import",
        process.hashlib.sha256(raw_body).hexdigest(),
    ))
    assert seen["signature"] == process.hmac.new(
        secret.encode(), signed.encode(), process.hashlib.sha256
    ).hexdigest()
    assert seen["body"]["template"]["schema"] == "blockwise.ad-template"
    assert "feedLayout" in seen["body"]["template"] and "storyLayout" in seen["body"]["template"]
    assert "version" not in seen["body"]["template"] and "inputs" not in seen["body"]["template"] and "Meta" not in seen["body"]["template"]
    assert set(seen["body"]) == {"template", "assets"}
    assert {asset["assetKey"] for asset in seen["body"]["assets"]} == {"feed", "story"}
    assert all(asset["bytesBase64"] for asset in seen["body"]["assets"])
    assert receipt == {"template_id": "tpl-1", "status": "imported", "asset_count": 2, "replayed": False}


def test_blockwise_replayed_import_is_a_valid_ready_receipt(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"templateId":"tpl-replay","assetCount":1,"replayed":true}'
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://127.0.0.1:8080/import")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    monkeypatch.setattr(process.urllib.request, "urlopen", lambda request, timeout: Response())
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "tpl-replay", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {"feed": {"fileName": "feed.png", "mimeType": "image/png"}}, "fonts": [], "metadata": metadata()}
    receipt = process.import_template(
        {"template": template, "assets": [{"assetKey": "feed", "fileName": "feed.png", "mimeType": "image/png", "bytesBase64": "ZmVlZA=="}], "previews": []},
        run_id="run-replay", project_id="blockwise",
    )
    assert receipt == {"template_id": "tpl-replay", "status": "replayed", "asset_count": 1, "replayed": True}


def test_blockwise_import_host_rejects_header_injection(monkeypatch):
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://127.0.0.1:8080/import")
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_HOST", "blockwise.sale\r\nX-Bad: yes")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    with pytest.raises(AdTemplateProcessError):
        process.import_template({"template": {}, "assets": []}, run_id="run", project_id="blockwise")


def test_late_builder_return_cannot_render_or_import(tmp_path, monkeypatch):
    """A cooperative stop observed after a role returns gates all side effects."""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    stopped = {"value": False}
    rendered = []
    imported = []

    def call_agent(instance, prompt, route):
        assert instance == "builder-1"
        assert prompt[1]["image_url"]["url"].startswith("data:image/png;base64,")
        stopped["value"] = True
        return {"template": {}, "assets": []}

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: rendered.append((candidate, workspace)),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: imported.append(output),
    )

    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_cancelled",
        project_id="blockwise",
        emit=lambda *_args: None,
        should_stop=lambda: stopped["value"],
    )
    with pytest.raises(AdTemplateProcessError, match="cancelled"):
        orchestrator.run(
            source=str(source),
            brief="",
            placements=["feed", "story"],
            routes=[
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            ],
        )

    assert rendered == []
    assert imported == []


def test_schema_invalid_candidate_repairs_before_one_render_and_comparator(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = json.loads(json.dumps(valid_candidate("bad")))
    bad["template"]["feedLayout"]["safeZones"][0] = {"x": 48, "y": 48, "right": 1032, "bottom": 1302}
    bad["template"]["metadata"]["private"] = "must-not-persist"
    bad["template"]["metadata"]["hash"] = "must-not-persist"
    bad["template"]["metadata"]["path"] = "/tmp/private-source.png"
    bad["template"]["metadata"]["renderPath"] = "/tmp/private-render.png"
    bad["template"]["metadata"]["dataUri"] = "data:image/png;base64,c2VjcmV0"
    bad["template"]["metadata"]["base64"] = "c2VjcmV0"
    bad["template"]["metadata"]["accessToken"] = "secret-access"
    bad["template"]["metadata"]["apiToken"] = "secret-api"
    good = valid_candidate("good")
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance == "builder-1":
            return bad
        if instance == "builder-1-repair-1":
            return good
        if instance == "comparator-1":
            return evidence(9.6, "Valid Feed and Story match")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-good", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_repair",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision"},
        {"provider": "review-b", "model": "vision"},
    ])

    assert [item[0] for item in calls[:3]] == ["builder-1", "builder-1-repair-1", "comparator-1"]
    repair_prompt = calls[1][1][0]["text"]
    exact_reason = "feedLayout.safeZones[0] must contain exactly x, y, width, and height (missing height, width; unexpected bottom, right)"
    assert exact_reason in repair_prompt
    assert "IMMEDIATELY PRIOR REJECTED CANDIDATE" in repair_prompt
    assert '"templateId":"bad"' in repair_prompt
    assert '"safeZones":[{"x":48,"y":48}]' in repair_prompt
    assert '"right":' not in repair_prompt and '"bottom":' not in repair_prompt
    assert "must-not-persist" not in repair_prompt
    assert '"private":' not in repair_prompt and '"hash":' not in repair_prompt
    for forbidden in ("path", "renderPath", "dataUri", "base64", "accessToken", "apiToken"):
        assert f'"{forbidden}":' not in repair_prompt
    assert "data:image/png;base64" not in repair_prompt
    assert len(renders) == 1 and renders[0]["template"]["templateId"] == "good"
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len(imports) == 1 and result["import"]["template_id"] == "tpl-good"
    kinds = [item[0] for item in events]
    assert kinds.index("candidate.rejected") < kinds.index("iteration.started") < kinds.index("iteration.rendered") < kinds.index("iteration.compared")
    rejected = next(item[2] for item in events if item[0] == "candidate.rejected")
    assert rejected == {"iteration": 1, "attempt": 1, "reason": exact_reason, "decision": "repair"}
    evidence_path = tmp_path / "run" / "iterations" / "01" / "rejected-candidate-01.json"
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["candidate"]["template"]["feedLayout"]["safeZones"][0] == {"x": 48, "y": 48}
    assert "private" not in persisted["candidate"]["template"]["metadata"]
    assert "hash" not in persisted["candidate"]["template"]["metadata"]
    persisted_blob = json.dumps(persisted, sort_keys=True)
    for forbidden in ("path", "renderPath", "dataUri", "base64", "accessToken", "apiToken"):
        assert f'"{forbidden}":' not in persisted_blob
    assert "data:image/png;base64" not in persisted_blob


def test_renderer_rejection_revises_without_advancing_checkpoint_or_importing(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    rejected = valid_candidate("renderer-rejected")
    corrected = valid_candidate("renderer-corrected")
    calls, renders, imports, events, operations = [], [], [], [], []
    reason = "feed text layer feed-email cannot fit at the 24px readability floor"

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance == "builder-1":
            return rejected
        if instance == "builder-1-repair-1":
            operations.append("second-builder")
            return corrected
        if instance == "comparator-1":
            return evidence(9.6, "Corrected render passes")
        return evidence(9.7, "Independent final pass")

    def render(candidate, workspace):
        if candidate["template"]["templateId"] == "renderer-rejected":
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "artifact.json").write_text(
                json.dumps(candidate, sort_keys=True), encoding="utf-8"
            )
            operations.append("renderer-rejected")
            raise process.AdTemplateRendererRejection(reason)
        operations.append("renderer-corrected")
        return fake_render(candidate, workspace, renders)

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process,
        "persist_iteration_checkpoint",
        lambda *args, **kwargs: operations.append("checkpoint") or tmp_path / "checkpoint.json",
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: imports.append(output)
        or {"template_id": "tpl-renderer-corrected", "status": "imported"},
    )

    result = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_renderer_repair",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "cheap"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision"},
        {"provider": "review-b", "model": "independent"},
        {"provider": "builder", "model": "quality"},
    ])

    assert [item[0] for item in calls[:3]] == [
        "builder-1", "builder-1-repair-1", "comparator-1",
    ]
    repair_prompt = calls[1][1][0]["text"]
    assert "DETERMINISTIC RENDERER REPAIR 1" in repair_prompt
    assert reason in repair_prompt
    assert '"templateId":"renderer-rejected"' in repair_prompt
    assert operations.index("renderer-rejected") < operations.index("second-builder")
    assert operations.index("renderer-corrected") < operations.index("checkpoint")
    assert operations.count("checkpoint") == 1
    revised = next(data for kind, _node, data in events if kind == "iteration.revised")
    assert revised == {
        "iteration": 1,
        "attempt": 1,
        "reason": reason,
        "reasons": [reason],
        "decision": "revise",
        "category": "renderer_rejection",
        "evidence": "iterations/01/renderer-rejection-01.json",
        "revision_instruction": (
            "Resolve ALL 1 deterministic renderer rejections before returning the next "
            "complete candidate. Every numbered target is mandatory; changing only the first "
            f"target is insufficient. TARGET 1: {reason}"
        ),
        "target_unchanged": False,
        "unchanged_targets": [],
    }
    evidence_path = tmp_path / "run" / revised["evidence"]
    rejection_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert rejection_evidence["reason"] == reason
    assert rejection_evidence["reasons"] == [reason]
    assert rejection_evidence["unchanged_targets"] == []
    assert rejection_evidence["artifact"] == "renderer-rejected-artifact-01.json"
    assert rejection_evidence["candidate"]["template"]["templateId"] == "renderer-rejected"
    assert (evidence_path.parent / rejection_evidence["artifact"]).is_file()
    assert len(result["iterations"]) == 1
    assert result["iterations"][0]["iteration"] == 1
    assert len(imports) == 1


def test_renderer_repair_fails_early_on_identical_candidate_and_error(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    candidate = valid_candidate("renderer-repeat")
    reason = "feed text layer feed-email cannot fit at the 24px readability floor"
    calls, imports, events = [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        return json.loads(json.dumps(candidate))

    def render(value, workspace):
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "artifact.json").write_text(
            json.dumps(value, sort_keys=True), encoding="utf-8",
        )
        raise process.AdTemplateRendererRejection(reason)

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process, "import_template",
        lambda output, run_id, project_id: imports.append(output),
    )
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_renderer_repeat",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    )

    with pytest.raises(
        AdTemplateProcessError,
        match="repeated an identical renderer-rejected candidate/error",
    ):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == ["builder-1", "builder-1-repair-1"]
    assert imports == []
    revised = [data for kind, data in events if kind == "iteration.revised"]
    assert len(revised) == 2
    assert revised[0].get("repeated") is None
    assert revised[1]["repeated"] is True


def test_renderer_rejection_reason_is_allowlisted_and_path_free():
    stderr = (
        "file:///opt/releases/private/renderer.js:222\n"
        "Error: feed text layer feed-email cannot fit at the 24px readability floor\n"
        "    at renderText (/secret/path/renderer.js:222:15)"
    )
    assert process._deterministic_renderer_rejection(stderr) == (
        "feed text layer feed-email cannot fit at the 24px readability floor"
    )
    assert process._deterministic_renderer_rejection(
        "Error: Cannot find module /secret/path/renderer.js"
    ) is None


def test_renderer_rejection_feedback_targets_layer_and_detects_unchanged_retry():
    candidate = valid_candidate("renderer-target")
    candidate["template"]["feedLayout"]["layers"].append({
        "type": "text",
        "layerId": "feed-brand-name",
        "inputKey": "brandName",
        "font": {"file": "manrope-600.woff2"},
        "fontSize": 34,
        "lineHeight": 1.1,
        "tracking": 1,
        "alignment": "center",
        "maxCharacters": 18,
        "maxLines": 1,
        "colourRole": "mainText",
        "overflowBehaviour": "scale_down",
        "geometry": {"x": 682, "y": 316, "width": 300, "height": 42},
    })
    candidate["template"]["textInputs"].append({
        "key": "brandName",
        "label": "Real estate brand name",
        "placeholder": "REAL ESTATE",
        "maxLength": 18,
    })
    reason = "feed text layer feed-brand-name cannot fit at the 24px readability floor"

    first, signature, first_unchanged = process._renderer_rejection_instruction(
        candidate, reason
    )
    second, second_signature, second_unchanged = process._renderer_rejection_instruction(
        candidate, reason, previous_target_signature=signature
    )

    assert first_unchanged is False
    assert second_unchanged is True
    assert second_signature == signature
    for expected in (
        '"placement":"feed"',
        '"layerId":"feed-brand-name"',
        '"inputKey":"brandName"',
        '"geometry":{"height":42,"width":300,"x":682,"y":316}',
        '"font":{"file":"manrope-600.woff2"}',
        '"fontSize":34',
        '"maxLines":1',
        '"minimumHeight":27',
        '"fit":"scale_down"',
        '"placeholder":"REAL ESTATE"',
        "raise geometry.height to at least 27px",
        "widen the text box",
        "shorten the neutral placeholder/input contract",
        "keep the rendered font at or above the readability floor",
    ):
        assert expected in first
    assert "UNCHANGED NAMED LAYER DETECTED" not in first
    assert "UNCHANGED NAMED LAYER DETECTED" in second


def test_multiline_renderer_rejection_is_allowlisted_and_targets_line_height():
    candidate = valid_candidate("renderer-multiline")
    candidate["template"]["storyLayout"]["layers"].append({
        "type": "text", "layerId": "story-features", "inputKey": "features",
        "font": {"file": "manrope-400.woff2"}, "fontSize": 32, "lineHeight": 0.8,
        "tracking": 0, "alignment": "left", "maxCharacters": 160, "maxLines": 6,
        "colourRole": "mainText", "overflowBehaviour": "scale_down",
        "geometry": {"x": 72, "y": 1296, "width": 936, "height": 160},
    })
    candidate["template"]["textInputs"].append({
        "key": "features", "label": "Features",
        "placeholder": "One\nTwo\nThree\nFour\nFive\nSix", "maxLength": 160,
    })
    reason = "story text layer story-features with maxLines 6 must use lineHeight at least 1"
    stderr = "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED " + json.dumps({
        "code": "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED",
        "violations": [{
            "placement": "story", "layerId": "story-features",
            "kind": "multiline_line_height_below_minimum", "maxLines": 6,
            "lineHeight": 0.8, "minimumLineHeight": 1, "reason": reason,
        }],
    })

    assert process._deterministic_renderer_rejections(stderr) == [reason]
    instruction, signature, unchanged = process._renderer_rejection_instruction(
        candidate, reason,
    )
    assert signature is not None and unchanged is False
    assert '"lineHeight":0.8' in instruction
    assert '"maxLines":6' in instruction
    assert "set this named multiline layer lineHeight to at least 1.0" in instruction

    candidate["template"]["storyLayout"]["layers"][-1]["lineHeight"] = 1.15
    fit_reason = "story text layer story-features cannot fit at the 32px readability floor"
    fit_instruction, _, _ = process._renderer_rejection_instruction(
        candidate, fit_reason,
    )
    assert '"minimumHeight":221' in fit_instruction
    assert "raise geometry.height to at least 221px" in fit_instruction


def test_renderer_aggregated_rejection_targets_every_layer_and_tracks_each_noop():
    candidate = valid_candidate("renderer-aggregate")
    candidate["template"]["feedLayout"]["layers"].append({
        "type": "text",
        "layerId": "feed-email-text",
        "inputKey": "emailAddress",
        "font": {"file": "manrope-500.woff2"},
        "fontSize": 24,
        "lineHeight": 1,
        "tracking": 0,
        "alignment": "left",
        "maxCharacters": 42,
        "maxLines": 1,
        "colourRole": "mainText",
        "overflowBehaviour": "scale_down",
        "geometry": {"x": 392, "y": 1208, "width": 278, "height": 30},
    })
    candidate["template"]["storyLayout"]["layers"].append({
        "type": "text",
        "layerId": "story-address",
        "inputKey": "propertyAddress",
        "font": {"file": "manrope-500.woff2"},
        "fontSize": 32,
        "lineHeight": 1.1,
        "tracking": 0,
        "alignment": "left",
        "maxCharacters": 80,
        "maxLines": 2,
        "colourRole": "mainText",
        "overflowBehaviour": "scale_down",
        "geometry": {"x": 382, "y": 928, "width": 570, "height": 58},
    })
    candidate["template"]["textInputs"].extend([
        {
            "key": "emailAddress",
            "label": "Email",
            "placeholder": "hello@exampleproperty.com",
            "maxLength": 42,
        },
        {
            "key": "propertyAddress",
            "label": "Address",
            "placeholder": "123 Example Street, Sample City, ST 12345",
            "maxLength": 80,
        },
    ])
    reasons = [
        "feed text layer feed-email-text cannot fit at the 24px readability floor",
        "story text layer story-address cannot fit at the 32px readability floor",
    ]
    stderr = "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED " + json.dumps({
        "code": "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED",
        "violations": [
            {
                "placement": "feed",
                "layerId": "feed-email-text",
                "kind": "cannot_fit",
                "readabilityFloorPx": 24,
                "reason": reasons[0],
            },
            {
                "placement": "story",
                "layerId": "story-address",
                "kind": "cannot_fit",
                "readabilityFloorPx": 32,
                "reason": reasons[1],
            },
        ],
    })
    assert process._deterministic_renderer_rejections(stderr) == reasons

    first, signatures, unchanged = process._renderer_rejection_instructions(
        candidate, reasons
    )
    second, second_signatures, second_unchanged = (
        process._renderer_rejection_instructions(
            candidate, reasons, previous_target_signatures=signatures
        )
    )

    assert "Resolve ALL 2 deterministic renderer rejections" in first
    assert "TARGET 1:" in first and "TARGET 2:" in first
    assert first.index("feed-email-text") < first.index("story-address")
    assert '"placeholder":"hello@exampleproperty.com"' in first
    assert '"placeholder":"123 Example Street, Sample City, ST 12345"' in first
    assert unchanged == []
    assert second_signatures == signatures
    assert second_unchanged == ["feed:feed-email-text", "story:story-address"]
    assert second.count("UNCHANGED NAMED LAYER DETECTED") == 2

    candidate["template"]["feedLayout"]["layers"][-1]["geometry"]["width"] = 420
    third, _third_signatures, third_unchanged = process._renderer_rejection_instructions(
        candidate, reasons, previous_target_signatures=second_signatures
    )
    assert third_unchanged == ["story:story-address"]
    assert third.count("UNCHANGED NAMED LAYER DETECTED") == 1


def test_renderer_layout_rejections_enrich_bounds_and_overlap_targets():
    candidate = valid_candidate("renderer-layout-rejections")
    candidate["template"]["textInputs"].extend([
        {
            "key": "propertyAddress", "label": "Address",
            "placeholder": "123 Example Street,\nSample City, ST 12345", "maxLength": 80,
        },
        {
            "key": "aboutCopy", "label": "About",
            "placeholder": "A neutral six-line property description.", "maxLength": 180,
        },
        {
            "key": "featuresHeading", "label": "Features heading",
            "placeholder": "PROPERTY FEATURES", "maxLength": 30,
        },
    ])
    candidate["template"]["storyLayout"]["layers"].extend([
        {
            "type": "text", "layerId": "story-address", "inputKey": "propertyAddress",
            "font": {"file": "manrope-400.woff2"}, "fontSize": 32,
            "lineHeight": 1.1, "tracking": 0, "alignment": "right",
            "maxCharacters": 80, "maxLines": 2, "colourRole": "mainText",
            "overflowBehaviour": "scale_down",
            "geometry": {"x": 624, "y": 928, "width": 344, "height": 76},
        },
        {
            "type": "text", "layerId": "story-about-copy", "inputKey": "aboutCopy",
            "font": {"file": "manrope-400.woff2"}, "fontSize": 32,
            "lineHeight": 1.2, "tracking": 0, "alignment": "left",
            "maxCharacters": 180, "maxLines": 6, "colourRole": "mainText",
            "overflowBehaviour": "scale_down",
            "geometry": {"x": 72, "y": 1120, "width": 420, "height": 232},
        },
        {
            "type": "text", "layerId": "story-features-heading",
            "inputKey": "featuresHeading", "font": {"file": "manrope-700.woff2"},
            "fontSize": 32, "lineHeight": 1.1, "tracking": 0, "alignment": "left",
            "maxCharacters": 30, "maxLines": 1, "colourRole": "mainText",
            "overflowBehaviour": "scale_down",
            "geometry": {"x": 560, "y": 1246, "width": 420, "height": 46},
        },
    ])
    reasons = [
        "story text layer story-address painted bounds exceed geometry by 4px on right",
        (
            "story essential text layers story-about-copy and story-features-heading "
            "overlap by 106px vertically"
        ),
    ]
    stderr = "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED " + json.dumps({
        "code": "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED",
        "violations": [
            {
                "placement": "story", "layerId": "story-address",
                "kind": "painted_bounds_outside_geometry", "overflowPx": 4,
                "edge": "right", "reason": reasons[0],
            },
            {
                "placement": "story", "firstLayerId": "story-about-copy",
                "secondLayerId": "story-features-heading",
                "kind": "essential_text_overlap", "overlapPx": 106,
                "axis": "vertical", "reason": reasons[1],
            },
        ],
    })

    assert process._deterministic_renderer_rejections(stderr) == reasons
    first, signatures, unchanged = process._renderer_rejection_instructions(
        candidate, reasons,
    )
    assert unchanged == []
    assert list(signatures) == [
        "story:story-address",
        "story:story-about-copy+story-features-heading",
    ]
    for expected in (
        '"layerId":"story-address"',
        '"geometry":{"height":76,"width":344,"x":624,"y":928}',
        '"placeholder":"123 Example Street,\\nSample City, ST 12345"',
        '"paintedOverflowPx":4',
        '"overflowEdge":"right"',
        "clearing at least 4px on right",
        '"layerId":"story-about-copy"',
        '"layerId":"story-features-heading"',
        '"overlapPx":106',
        '"axis":"vertically"',
        "separate the two named layers vertically by at least 106px",
    ):
        assert expected in first

    second, second_signatures, second_unchanged = process._renderer_rejection_instructions(
        candidate, reasons, previous_target_signatures=signatures,
    )
    assert second_signatures == signatures
    assert second_unchanged == [
        "story:story-address",
        "story:story-about-copy+story-features-heading",
    ]
    assert second.count("UNCHANGED NAMED LAYER DETECTED") == 1
    assert second.count("UNCHANGED NAMED LAYERS DETECTED") == 1

    candidate["template"]["storyLayout"]["layers"][-3]["geometry"]["width"] = 352
    candidate["template"]["storyLayout"]["layers"][-1]["geometry"]["y"] = 1380
    _third, _third_signatures, third_unchanged = process._renderer_rejection_instructions(
        candidate, reasons, previous_target_signatures=second_signatures,
    )
    assert third_unchanged == []


def test_initial_non_json_builder_output_retries_cheap_then_escalates_without_extra_reviews(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    good = valid_candidate("structured-recovery")
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            if len([item for item in calls if item[0].startswith("builder-")]) <= 2:
                raise process.AdTemplateStructuredOutputError(
                    "Builder did not return one structured JSON result"
                )
            return good
        if instance.startswith("comparator-"):
            return evidence(9.6, "Recovered candidate passes")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: imports.append(output)
        or {"template_id": "tpl-structured-recovery", "status": "imported"},
    )
    result = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_structured_recovery",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(
        source=str(source),
        brief="",
        placements=["feed", "story"],
        routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
        require_quality_route=True,
    )

    assert calls[:3] == [
        ("builder-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-output-retry-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-output-retry-2", "openai-codex/gpt-5.6-sol"),
    ]
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len([item for item in calls if item[0].startswith("final-reviewer-")]) == 2
    assert len(renders) == 1 and len(imports) == 1
    assert result["builder_escalated"] is True
    assert result["builder_route"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }
    retry = next(item for item in events if item[0] == "builder.output-retry")
    assert retry[2] == {
        "iteration": 1,
        "attempt": 2,
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reason": "structured_output_invalid",
    }
    escalation = next(item for item in events if item[0] == "builder.escalated")
    assert escalation[2]["reason"] == "structured_output_invalid"


def test_invalid_builder_contract_retries_cheap_then_uses_quality_builder(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("invalid-contract")
    bad["template"]["storyLayout"]["safeZones"] = [
        {"x": 0, "y": 0, "width": 1080, "height": 1921}
    ]
    good = valid_candidate("quality-contract")
    calls, renders, events = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance in {"builder-1", "builder-1-repair-1"}:
            return json.loads(json.dumps(bad))
        if instance == "builder-1-repair-2":
            return good
        if instance.startswith("comparator-"):
            return evidence(9.6, "Quality builder candidate passes")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: {
            "template_id": "tpl-quality-contract",
            "status": "imported",
        },
    )
    SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_contract_recovery",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(
        source=str(source),
        brief="",
        placements=["feed", "story"],
        routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
        require_quality_route=True,
    )

    assert calls[:3] == [
        ("builder-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-repair-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-repair-2", "openai-codex/gpt-5.6-sol"),
    ]
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len([item for item in calls if item[0].startswith("final-reviewer-")]) == 2
    assert len(renders) == 1
    escalation = next(item for item in events if item[0] == "builder.escalated")
    assert escalation[2]["reason"] == "candidate_contract_invalid"


def test_visual_revision_prompt_carries_prior_valid_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    first = valid_candidate("visual-v1")
    second = valid_candidate("visual-v2")
    calls, renders, imports = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance == "builder-1":
            return first
        if instance == "comparator-1":
            return evidence(9.0, "Layout needs revision")
        if instance == "builder-2":
            return second
        if instance == "comparator-2":
            return evidence(9.6, "Revision matches")
        return evidence(9.7, "Independent final pass")

    def render_with_private_runtime_fields(candidate, workspace):
        output = fake_render(candidate, workspace, renders)
        output["assets"] = [{
            "assetKey": "hero", "fileName": "home/open-home-living.webp", "mimeType": "image/webp",
            "bytesBase64": "c2VjcmV0", "dataUri": "data:image/webp;base64,c2VjcmV0",
            "base64": "c2VjcmV0", "accessToken": "secret-access", "apiToken": "secret-api",
            "path": str(workspace / "asset.webp"), "renderPath": str(workspace / "render.webp"),
        }]
        output["receipt"] = {"path": str(workspace / "receipt.json"), "accessToken": "secret-access"}
        output["dataUri"] = "data:image/png;base64,c2VjcmV0"
        return output

    monkeypatch.setattr(process, "run_generator_cli", render_with_private_runtime_fields)
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-visual-v2", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_visual_revision",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision"},
        {"provider": "review-b", "model": "vision"},
    ])

    builder_one = next(text for instance, text in calls if instance == "builder-1")
    builder_two = next(text for instance, text in calls if instance == "builder-2")
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" not in builder_one
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" in builder_two
    assert '"templateId":"visual-v1"' in builder_two
    assert "Layout needs revision" in builder_two
    assert str(tmp_path) not in builder_two
    for forbidden in ("bytesBase64", "dataUri", "base64", "accessToken", "apiToken", "path", "renderPath"):
        assert f'"{forbidden}":' not in builder_two
    assert len(result["iterations"]) == 2
    trace_blob = json.dumps(result["iterations"], sort_keys=True)
    for forbidden in ("bytesBase64", "dataUri", "base64", "accessToken", "apiToken", "path", "renderPath"):
        assert f'"{forbidden}":' not in trace_blob
    assert str(tmp_path) not in trace_blob
    for index, item in enumerate(result["iterations"], start=1):
        assert item["candidate"]["previews"] == [
            {"name": f"iteration-{index:02d}-feed.png", "placement": "feed"},
            {"name": f"iteration-{index:02d}-story.png", "placement": "story"},
        ]
    assert result["import"]["template_id"] == "tpl-visual-v2" and len(imports) == 1
    assert imports[0]["assets"][0]["bytesBase64"] == "c2VjcmV0"
    assert imports[0]["assets"][0]["path"].startswith(str(tmp_path))
    assert imports[0]["receipt"]["accessToken"] == "secret-access"


def test_final_review_revision_prompt_carries_accepted_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    first = valid_candidate("final-v1")
    second = valid_candidate("final-v2")
    calls, renders, imports = [], [], []
    final_calls = {"count": 0}

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance == "builder-1":
            return first
        if instance == "builder-2":
            return second
        if instance.startswith("comparator-"):
            return evidence(9.6, "Comparator pass")
        final_calls["count"] += 1
        if final_calls["count"] == 1:
            return evidence(9.0, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-v2", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_revision",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    builder_two = next(text for instance, text in calls if instance == "builder-2")
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" in builder_two
    assert '"templateId":"final-v1"' in builder_two
    assert "Final spacing needs revision" in builder_two
    assert str(tmp_path) not in builder_two
    assert result["iterations"][0]["final_review_failed"] is True
    assert result["iterations"][1]["decision"] == "accepted"
    assert final_calls["count"] == 4
    assert result["import"]["template_id"] == "tpl-final-v2" and len(imports) == 1


def test_final_check_resume_reuses_checkpoint_score_when_reviewers_request_revision(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, renders, imports = [], [], []
    reviewer_calls = 0
    checkpoint = valid_candidate("resume-checkpoint")
    checkpoint_feed = tmp_path / "checkpoint-feed.png"
    checkpoint_story = tmp_path / "checkpoint-story.png"
    checkpoint_feed.write_bytes(b"feed")
    checkpoint_story.write_bytes(b"story")
    checkpoint["render"] = {"feed": str(checkpoint_feed), "story": str(checkpoint_story)}

    def call_agent(instance, prompt, route):
        nonlocal reviewer_calls
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("resume-revised")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        reviewer_calls += 1
        if reviewer_calls <= 2:
            return evidence(9.2, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-resumed", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_resume_final",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(
        source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision-a"},
            {"provider": "review-b", "model": "vision-b"},
        ],
        history=[iteration(9.7)], revision_candidate=checkpoint,
        total_iterations=1, previous_score=9.7, resume_final_check=True,
    )

    assert reviewer_calls == 4
    assert [item[0] for item in calls if item[0].startswith("builder-")] == ["builder-2"]
    assert len(renders) == 1 and len(imports) == 1
    assert result["iterations"][0]["final_review_failed"] is True
    assert result["iterations"][1]["decision"] == "accepted"
    assert result["import"]["template_id"] == "tpl-resumed"


def test_build_restart_continues_at_iteration_three_from_persisted_best(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    best = valid_candidate("persisted-best")
    best_feed = tmp_path / "best-feed.png"
    best_story = tmp_path / "best-story.png"
    best_feed.write_bytes(b"feed")
    best_story.write_bytes(b"story")
    best["render"] = {"feed": str(best_feed), "story": str(best_story)}
    calls, renders = [], []

    def call_agent(instance, _prompt, _route):
        calls.append(instance)
        if instance.startswith("builder-"):
            return valid_candidate("resumed-third")
        return evidence(9.7, "Completion pass")

    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process, "import_template",
        lambda *_args, **_kwargs: {"template_id": "tpl-third", "status": "imported"},
    )
    result = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_resume_build",
        project_id="blockwise",
        emit=lambda *_args: None,
    ).run(
        source=str(source),
        brief="",
        placements=["feed", "story"],
        routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision-a"},
            {"provider": "review-b", "model": "vision-b"},
        ],
        history=[iteration(8.4, 1), iteration(9.1, 2)],
        revision_candidate=best,
        total_iterations=2,
        previous_score=9.1,
        best_iteration=2,
    )
    assert calls[0] == "builder-3"
    assert result["iterations"][-1]["iteration"] == 3
    assert (tmp_path / "run" / "iterations" / "03" / "checkpoint.json").is_file()


def test_negative_final_review_without_geometry_changes_revises_after_both_reviewers(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []
    invalid = evidence(9.2, "Final spacing needs revision")
    invalid.pop("required_changes")

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("final-output-retry")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if instance.startswith("final-reviewer-") and len([
            item for item in calls if item[0].startswith("final-reviewer-")
        ]) == 1:
            return invalid
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-retry", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_retry",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    assert [name.split("-")[0] for name, _, _ in calls].count("builder") == 2
    assert len(renders) == 2 and len(imports) == 1
    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert len(final_calls) == 4
    first_round = next(
        data for kind, data in events
        if kind == "final-review.completed" and data["decision"] == "revise"
    )
    assert len(first_round["reviewers"]) == 2
    assert first_round["reviewers"][0]["required_changes"] == []
    retried = [data for kind, data in events if kind == "final-review.retried"]
    assert retried == []
    revision_prompt = next(
        text for name, text, _route in calls if name == "builder-2"
    )
    assert "Final spacing needs revision" in revision_prompt
    assert '"required_changes": []' in revision_prompt
    assert '"minimum_score": 9.2' in revision_prompt
    assert '"rubric":' in revision_prompt
    assert "When a final reviewer returns a negative verdict" in revision_prompt
    assert result["iterations"][0]["final_review_failed"] is True
    assert result["iterations"][1]["decision"] == "accepted"
    assert result["final_review"]["decision"] == "accepted"


def test_non_json_first_final_reviewer_escalates_to_quality_route_without_rebuilding(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            return valid_candidate("final-route-escalation")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "deepseek/deepseek-v4-flash-vision-exp":
            raise process.AdTemplateStructuredOutputError(
                "Builder did not return one structured JSON result"
            )
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-route-escalation", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_route_escalation",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ], require_quality_route=True)

    assert len([item for item in calls if item[0].startswith("builder-")]) == 1
    final_routes = [route for instance, route in calls if instance.startswith("final-reviewer-")]
    assert final_routes == [
        "deepseek/deepseek-v4-flash-vision-exp",
        "openai-codex/gpt-5.6-sol",
        "openai-codex/gpt-5.6-luna",
    ]
    escalations = [data for kind, data in events if kind == "final-review.route-escalated"]
    assert escalations == [{
        "from_route": "deepseek/deepseek-v4-flash-vision-exp",
        "to_route": "openai-codex/gpt-5.6-sol",
        "reason": "Builder did not return one structured JSON result",
    }]
    assert [item["route"] for item in result["final_review"]["reviewers"]] == [
        "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-luna",
    ]
    assert len(renders) == 1 and len(imports) == 1


@pytest.mark.parametrize("failure_kind", ["non_json", "schema_invalid"])
def test_exhausted_invalid_final_b_falls_back_once_to_independent_quality_route(
    tmp_path, monkeypatch, failure_kind,
):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance.startswith("builder-"):
            return valid_candidate("final-b-protocol-fallback")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "deepseek/deepseek-v4-flash-vision-exp":
            if failure_kind == "non_json":
                raise process.AdTemplateStructuredOutputError(
                    "Builder did not return one structured JSON result"
                )
            invalid = evidence(9.7, "Invalid final evidence")
            invalid["differences"] = "not-a-list"
            return invalid
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-b-protocol-fallback", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run",
        run_id="trun_final_b_protocol_fallback", project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="IMMUTABLE_BRIEF_TAIL", placements=["feed", "story"], routes=[
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ], require_quality_route=True)

    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    deepseek_calls = [item for item in final_calls if item[2].startswith("deepseek/")]
    fallback_calls = [item for item in final_calls if "-protocol-fallback-" in item[0]]
    assert len(deepseek_calls) == process.MAX_FINAL_REVIEW_OUTPUT_RETRIES + 1
    assert len(fallback_calls) == 1
    assert fallback_calls[0][2] == "openai-codex/gpt-5.6-sol"
    assert fallback_calls[0][1] == deepseek_calls[0][1]
    assert "IMMUTABLE_BRIEF_TAIL" in fallback_calls[0][1][0]["text"]
    assert [item["route"] for item in result["final_review"]["reviewers"]] == [
        "openai-codex/gpt-5.6-luna", "openai-codex/gpt-5.6-sol",
    ]
    escalations = [data for kind, data in events if kind == "final-review.route-escalated"]
    assert len(escalations) == 1
    assert escalations[0]["category"] == "protocol-fallback"
    assert escalations[0]["from_route"] == "deepseek/deepseek-v4-flash-vision-exp"
    assert escalations[0]["to_route"] == "openai-codex/gpt-5.6-sol"
    assert len(renders) == 1 and len(imports) == 1


def test_valid_low_score_final_b_does_not_use_protocol_fallback(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            return valid_candidate("final-b-valid-low-score")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "deepseek/deepseek-v4-flash-vision-exp":
            return evidence(9.2, "Genuine final rejection")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "MAX_FINAL_REVIEW_ROUNDS", 0)
    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "unexpected", "status": "imported"})
    with pytest.raises(
        process.AdTemplateProcessError,
        match="final reviewers failed after the bounded automatic revision loop",
    ):
        SoleProcessOrchestrator(
            call_agent=call_agent, workspace=tmp_path / "run",
            run_id="trun_final_b_valid_low_score", project_id="blockwise",
            emit=lambda kind, node, data: events.append((kind, data)),
        ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ], require_quality_route=True)

    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert [route for _instance, route in final_calls] == [
        "openai-codex/gpt-5.6-luna",
        "deepseek/deepseek-v4-flash-vision-exp",
    ]
    assert not any(kind == "final-review.route-escalated" for kind, _data in events)
    assert len(renders) == 1 and imports == []


def test_first_final_reviewer_transport_timeout_escalates_once_to_distinct_quality_route(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            return valid_candidate("final-transport-escalation")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "openai-codex/gpt-5.6-luna":
            raise process.AdTemplateTransportError(
                "model role transport attempt exhausted"
            )
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process, "import_template",
        lambda output, run_id, project_id: imports.append(output) or {
            "template_id": "tpl-final-transport-escalation", "status": "imported",
        },
    )
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run",
        run_id="trun_final_transport_escalation", project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, data)),
    ).run(
        source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
        require_quality_route=True,
    )

    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert [route for _instance, route in final_calls] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
        "deepseek/deepseek-v4-flash-vision-exp",
    ]
    assert len({item["id"] for item in result["final_review"]["reviewers"]}) == 2
    assert [item["route"] for item in result["final_review"]["reviewers"]] == [
        "openai-codex/gpt-5.6-sol",
        "deepseek/deepseek-v4-flash-vision-exp",
    ]
    assert [data for kind, data in events if kind == "final-review.route-escalated"] == [{
        "from_route": "openai-codex/gpt-5.6-luna",
        "to_route": "openai-codex/gpt-5.6-sol",
        "reason": "model role transport attempt exhausted",
    }]
    assert len(renders) == 1 and len(imports) == 1


def test_final_review_schema_recovery_survives_three_invalid_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []
    invalid = evidence(9.2, "Final spacing needs revision")
    invalid["differences"] = "Footer spacing differs"
    reviewer_a_calls = 0

    def call_agent(instance, prompt, route):
        nonlocal reviewer_a_calls
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("final-output-three-retries")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "review-a/vision-a":
            reviewer_a_calls += 1
            if reviewer_a_calls <= 3:
                return invalid
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-three-retries", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_three_retries",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    assert [name.split("-")[0] for name, _, _ in calls].count("builder") == 1
    assert len(renders) == 1 and len(imports) == 1
    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert len(final_calls) == 5
    assert "-retry-3" in final_calls[3][0]
    assert "exactly reason, differences, visible_strings, semantic_glyph_inventory, mark_badge_treatment, hard_failures, and the eight-field rubric" in final_calls[3][1]
    assert "Do not return required_changes" in final_calls[3][1]
    retried = [data for kind, data in events if kind == "final-review.retried"]
    assert len(retried) == 3
    assert [item["attempt"] for item in retried] == [1, 2, 3]
    rejected = [data for kind, data in events if kind == "final-review.schema-rejected"]
    assert len(rejected) == 3
    assert {item["field"] for item in rejected} == {"differences"}
    assert {item["category"] for item in rejected} == {"mandatory-field-invalid"}
    assert result["iterations"][0]["decision"] == "accepted"
    assert result["final_review"]["decision"] == "accepted"


def test_schema_repair_is_bounded_and_invalid_candidates_have_no_visual_side_effects(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("always-bad")
    bad["template"]["storyLayout"]["safeZones"] = [{"x": 0, "y": 0, "width": 1080, "height": 1921}]
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        candidate = json.loads(json.dumps(bad))
        candidate["template"]["templateId"] = f"always-bad-{len(calls)}"
        return candidate

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: renders.append(candidate))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output))
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_bounded",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    )
    with pytest.raises(AdTemplateProcessError, match="schema-invalid after 6 repairs"):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == [
        "builder-1", "builder-1-repair-1", "builder-1-repair-2",
        "builder-1-repair-3", "builder-1-repair-4", "builder-1-repair-5",
        "builder-1-repair-6",
    ]
    assert renders == [] and imports == []
    assert [item[0] for item in events].count("candidate.rejected") == 7
    assert not any(item[0] in {"iteration.started", "iteration.rendered", "iteration.compared", "final-review.started"} for item in events)
    assert len(list((tmp_path / "run" / "iterations" / "01").glob("rejected-candidate-*.json"))) == 7


def test_schema_repair_fails_early_on_identical_candidate_and_error(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("identical-bad")
    bad["template"]["storyLayout"]["safeZones"] = [
        {"x": 0, "y": 0, "width": 1080, "height": 1921}
    ]
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        return json.loads(json.dumps(bad))

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: renders.append(candidate))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output))
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_repeat",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    )

    with pytest.raises(
        AdTemplateProcessError,
        match="repeated an identical schema-invalid candidate/error",
    ):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == ["builder-1", "builder-1-repair-1"]
    assert renders == [] and imports == []
    rejected = [data for kind, data in events if kind == "candidate.rejected"]
    assert len(rejected) == 2
    assert rejected[0].get("repeated") is None
    assert rejected[1]["repeated"] is True


def test_stop_during_schema_repair_prevents_render_import_and_rejection_write(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("bad")
    bad["template"]["feedLayout"]["safeZones"] = [{"x": -1, "y": 0, "width": 1080, "height": 1350}]
    stopped = {"value": False}
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        if instance == "builder-1":
            return bad
        stopped["value"] = True
        return valid_candidate("late-good")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: renders.append(candidate))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output))
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_stop_repair",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
        should_stop=lambda: stopped["value"],
    )
    with pytest.raises(AdTemplateProcessError, match="cancelled"):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == ["builder-1", "builder-1-repair-1"]
    assert renders == [] and imports == []
    assert [item[0] for item in events].count("candidate.rejected") == 1
    assert not (tmp_path / "run" / "iterations" / "01" / "rejected-candidate-02.json").exists()



def _quality_routes():
    return [
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ]


def test_builder_quality_escalation_ignores_steady_material_improvement(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([7.0, 7.5, 8.0, 9.6])
    builder_calls, events, renders = [], [], []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            return valid_candidate(f"steady-{len(builder_calls)}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), "Material improvement")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-steady", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_steady",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert [route for _, route, _ in builder_calls] == ["openai-codex/gpt-5.6-luna"] * 4
    assert not any(kind == "builder.escalated" for kind, _ in events)
    assert result["builder_escalated"] is False
    assert len(result["iterations"]) == 4
    assert all(item["builder_route"]["model"] == "gpt-5.6-luna" for item in result["iterations"])


@pytest.mark.parametrize(
    ("scores", "event_iteration"),
    [([8.0, 7.9, 9.6], 2), ([8.0, 8.2, 8.4, 9.6], 3)],
)
def test_builder_quality_escalates_on_regression_or_two_low_gains(tmp_path, monkeypatch, scores, event_iteration):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    score_iter = iter(scores)
    builder_calls, events, renders = [], [], []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(score_iter), f"comparison {instance}")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-escalated", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_escalate",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    escalations = [data for kind, data in events if kind == "builder.escalated"]
    assert len(escalations) == 1
    assert escalations[0] == {
        "iteration": event_iteration,
        "from_provider": "openai-codex", "from_model": "gpt-5.6-luna",
        "to_provider": "openai-codex", "to_model": "gpt-5.6-sol",
        "reason": "regression" if event_iteration == 2 else "insufficient_improvement",
        "previous_score": scores[event_iteration - 2], "score": scores[event_iteration - 1],
    }
    assert [route for _, route, _ in builder_calls[:event_iteration]] == ["openai-codex/gpt-5.6-luna"] * event_iteration
    assert [route for _, route, _ in builder_calls[event_iteration:]] == ["openai-codex/gpt-5.6-sol"] * (len(builder_calls) - event_iteration)
    assert result["builder_escalated"] is True
    assert result["builder_route"]["model"] == "gpt-5.6-sol"
    assert len(result["iterations"]) == len(scores)


def test_regressed_candidate_is_traced_but_next_revision_uses_immutable_best(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([8.6, 8.1, 9.6])
    calls, renders, events = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-best", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_best",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    builder_prompts = {instance: prompt for instance, _, prompt in calls if instance.startswith("builder-")}
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-2"]
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-3"]
    assert '"templateId":"candidate-builder-2"' not in builder_prompts["builder-3"]
    assert "change=comparison comparator-1" in builder_prompts["builder-3"]
    assert "comparison comparator-2" in builder_prompts["builder-3"]
    assert len(builder_prompts["builder-3"].encode("utf-8")) < 80_000
    for field in ("rubric", "minimum_score", "hard_failures", "required_changes", "reason"):
        assert f'"{field}"' in builder_prompts["builder-2"]
    compared_events = [data for kind, data in events if kind == "iteration.compared"]
    assert len(compared_events) == 3
    for event in compared_events:
        assert {
            "rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason",
        }.issubset(event)
    assert [route for instance, route, _ in calls if instance.startswith("builder-")] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
    ]
    assert [item["candidate"]["template"]["templateId"] for item in result["iterations"]] == [
        "candidate-builder-1", "candidate-builder-2", "candidate-builder-3",
    ]
    assert [item["comparison"]["score"] for item in result["iterations"]] == [8.6, 8.1, 9.6]
    assert [instance.split("-")[0] for instance, _, _ in calls] == [
        "builder", "comparator", "builder", "comparator", "builder", "comparator", "final", "final",
    ]


def test_blocked_but_improved_candidate_remains_restart_best_by_quality_subscore(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, renders = [], []
    comparisons = iter([evidence(8.0, "First draft"), evidence(9.1, "Better with one blocker")])

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"], route))
        if instance == "builder-3":
            raise process.AdTemplateTransportError("stop after durable checkpoint")
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            result = next(comparisons)
            if instance == "comparator-2":
                result["critical_regions"] = [{
                    "region": "logo footprint", "status": "blocker",
                    "findings": ["One editable logo sizing correction remains"],
                }]
            return result
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    with pytest.raises(process.AdTemplateTransportError, match="durable checkpoint"):
        SoleProcessOrchestrator(
            call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_quality_best",
            project_id="blockwise", emit=lambda *_args: None,
        ).run(
            source=str(source), brief="", placements=["feed", "story"],
            routes=_quality_routes(), require_quality_route=True,
        )

    checkpoint = json.loads(
        (tmp_path / "run" / "iterations" / "02" / "checkpoint.json").read_text("utf-8")
    )
    assert checkpoint["best_iteration"] == 2
    assert checkpoint["previous_score"] == 9.1
    assert checkpoint["best_quality_score"] == 9.1
    assert checkpoint["record"]["comparison"]["score"] == 0.0
    assert checkpoint["record"]["comparison"]["quality_score"] == 9.1
    third_prompt = next(prompt for instance, prompt, _route in calls if instance == "builder-3")
    assert '"templateId":"candidate-builder-2"' in third_prompt
    assert '"best_quality_score":9.1' in third_prompt


def test_final_review_revision_continues_from_the_reviewed_then_current_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([9.54, 9.28, 9.6])
    final_scores = iter([9.0, 9.7, 9.7, 9.7])
    calls, renders = [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        score = next(final_scores)
        return evidence(score, "Revise spacing" if score < 9.5 else "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-final-best", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_best",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    builder_prompts = {instance: prompt for instance, _, prompt in calls if instance.startswith("builder-")}
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-2"]
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-3"]
    assert '"templateId":"candidate-builder-2"' not in builder_prompts["builder-3"]
    assert [item["candidate"]["template"]["templateId"] for item in result["iterations"]] == [
        "candidate-builder-1", "candidate-builder-2", "candidate-builder-3",
    ]
    assert result["iterations"][0]["final_review_failed"] is True
    assert [route for instance, route, _ in calls if instance.startswith("builder-")] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
    ]


def test_builder_quality_escalation_is_sticky_through_repairs_and_final_review_revision(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([8.0, 7.9, 9.6, 9.6])
    builder_calls, events, renders = [], [], []
    final_calls = {"count": 0}

    invalid_quality = valid_candidate("quality-invalid")
    invalid_quality["template"]["storyLayout"]["safeZones"] = [{"x": 0, "y": 0, "width": 1080, "height": 1921}]

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            if instance == "builder-3":
                return invalid_quality
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        final_calls["count"] += 1
        if final_calls["count"] == 1:
            return evidence(9.0, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-sticky", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_sticky",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert [instance for instance, _, _ in builder_calls] == [
        "builder-1", "builder-2", "builder-3", "builder-3-repair-1", "builder-4",
    ]
    assert [route for _, route, _ in builder_calls] == [
        "openai-codex/gpt-5.6-luna", "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol",
    ]
    assert len([1 for kind, _ in events if kind == "builder.escalated"]) == 1
    assert len(result["iterations"]) == 4
    assert [item["iteration"] for item in result["iterations"]] == [1, 2, 3, 4]
    assert result["iterations"][2]["final_review_failed"] is True
    assert result["iterations"][3]["builder_escalated"] is True
    builder_four_prompt = next(prompt for instance, _, prompt in builder_calls if instance == "builder-4")
    assert '"templateId":"candidate-builder-3-repair-1"' in builder_four_prompt
    assert "Final spacing needs revision" in builder_four_prompt
    assert result["builder_route"]["model"] == "gpt-5.6-sol"


def test_required_quality_route_fails_before_any_builder_call(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls = []
    orchestrator = SoleProcessOrchestrator(
        call_agent=lambda *args: calls.append(args), workspace=tmp_path / "run",
        run_id="trun_missing_quality", project_id="blockwise", emit=lambda *_args: None,
    )
    with pytest.raises(AdTemplateProcessError, match="requires a configured quality route"):
        orchestrator.run(
            source=str(source), brief="", placements=["feed", "story"],
            routes=_quality_routes()[:4], require_quality_route=True,
        )
    assert calls == []


def test_build_from_best_budget_appends_iteration_after_normal_max(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    workspace = tmp_path / "run"
    old_checkpoint = workspace / "iterations" / "02" / "checkpoint.json"
    old_checkpoint.parent.mkdir(parents=True)
    old_checkpoint.write_text("historical-boundary", encoding="utf-8")
    (workspace / "previews").mkdir(parents=True)
    for placement in ("feed", "story"):
        (workspace / "previews" / f"iteration-02-{placement}.png").write_bytes(
            f"historical-{placement}".encode()
        )
    seed = valid_candidate("persisted-best")
    seed["render"] = {
        placement: str(workspace / "previews" / f"iteration-02-{placement}.png")
        for placement in ("feed", "story")
    }
    history = []
    for iteration in (1, 2):
        comparison = evidence(9.0, f"historical comparison {iteration}")
        history.append({
            "iteration": iteration,
            "candidate": process._candidate_trace_projection(seed),
            "comparison": comparison,
            "decision": "revise",
        })
    calls, renders = [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt))
        if instance.startswith("builder-"):
            return valid_candidate("appended-iteration")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Appended candidate passes")
        return evidence(9.7, "Independent final passes")

    monkeypatch.setattr(process, "MAX_ITERATIONS", 2)
    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, iteration_workspace: fake_render(
            candidate, iteration_workspace, renders,
        ),
    )
    monkeypatch.setattr(
        process, "import_template",
        lambda output, run_id, project_id: {
            "template_id": "tpl-appended", "status": "imported",
        },
    )

    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=workspace, run_id="trun_append_after_max",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(
        source=str(source), brief="", placements=["feed", "story"],
        routes=_quality_routes(), require_quality_route=True,
        total_iterations=2, history=history, revision_candidate=seed,
        selected_builder_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
        best_iteration=2, best_quality_score=9.0,
        iteration_budget_extension=1,
    )

    assert [item[0] for item in calls[:2]] == ["builder-3", "comparator-3"]
    assert len(calls) == 4
    assert all(item[0].startswith("final-reviewer-") for item in calls[2:])
    assert result["iterations"][-1]["iteration"] == 3
    assert (workspace / "iterations" / "03" / "checkpoint.json").is_file()
    assert old_checkpoint.read_text(encoding="utf-8") == "historical-boundary"


def test_terminal_final_rejection_persists_feedback_before_bounded_failure(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    workspace = tmp_path / "run"

    def call_agent(instance, _prompt, _route):
        if instance.startswith("builder-"):
            return valid_candidate("terminal-final")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator accepts actual pixels")
        return evidence(9.0, "Final reviewer requires address correction")

    monkeypatch.setattr(process, "MAX_FINAL_REVIEW_ROUNDS", 0)
    monkeypatch.setattr(
        process, "run_generator_cli",
        lambda candidate, iteration_workspace: fake_render(
            candidate, iteration_workspace, [],
        ),
    )
    with pytest.raises(
        process.AdTemplateProcessError,
        match="final reviewers failed after the bounded automatic revision loop",
    ):
        SoleProcessOrchestrator(
            call_agent=call_agent, workspace=workspace, run_id="trun_terminal_final",
            project_id="blockwise", emit=lambda *_args: None,
        ).run(
            source=str(source), brief="", placements=["feed", "story"],
            routes=_quality_routes(), require_quality_route=True,
        )

    checkpoint = json.loads(
        (workspace / "iterations" / "01" / "checkpoint.json").read_text("utf-8")
    )
    assert checkpoint["record"]["final_review_failed"] is True
    assert "Final reviewer requires address correction" in checkpoint["feedback"]
