from __future__ import annotations

import hashlib
import copy

from PIL import Image
import pytest

import gateway.ad_template_generator_process as process
from gateway.ad_template_output_qa import AdTemplateOutputQaError, run_ad_output_qa, validate_output_qa, reviewed_unknowns_match


def _candidate() -> dict:
    def layout(placement: str, height: int) -> dict:
        return {"placement": placement, "safeZones": [], "layers": [{
            "type": "plate", "layerId": f"{placement}-base", "protected": True,
            "geometry": {"x": 0, "y": 0, "width": 1080, "height": height},
        }]}

    return {"template": {
        "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920),
        "semanticColours": {},
    }}


def _renders(tmp_path, *, transparent: bool = False) -> dict[str, str]:
    paths = {}
    for placement, size in (("feed", (1080, 1350)), ("story", (1080, 1920))):
        image = Image.new("RGBA", size, (30, 40, 50, 255))
        if transparent:
            image.putpixel((0, 0), (255, 255, 255, 0))
        path = tmp_path / f"{placement}.png"
        image.save(path)
        paths[placement] = str(path)
    return paths


def test_fresh_final_render_qa_rejects_transparent_corner_despite_99_review(tmp_path):
    result = run_ad_output_qa(_candidate(), _renders(tmp_path, transparent=True))
    assert result["status"] == "fail"
    with pytest.raises(AdTemplateOutputQaError):
        validate_output_qa(result)


def test_current_render_is_rechecked_instead_of_reusing_prior_pass(tmp_path):
    candidate = _candidate()
    # Keep separate output directories so replacing a preview cannot make the
    # second check accidentally read the earlier image.
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = run_ad_output_qa(candidate, _renders(tmp_path / "first"))
    second = run_ad_output_qa(candidate, _renders(tmp_path / "second", transparent=True))
    assert first["status"] == "pass"
    assert second["status"] == "fail"


def test_comparison_prompt_embeds_release_pinned_checklist_hash():
    text, digest = process._trusted_ad_output_qa_checklist()
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest
    prompt = process.review_prompt(
        final=False, candidate=_candidate(),
        reference={"sourcePlacement": "feed", "targetPlacement": "story"},
        metrics={},
    )
    assert f"sha256={digest}" in prompt
    assert "Unknown, missing or unsupported measurements are not passes" in prompt


def test_visual_clearance_is_bound_to_reviewed_candidate_and_pixels(tmp_path):
    candidate = _candidate()
    for placement in ("feed", "story"):
        candidate["template"][placement + "Layout"]["layers"].extend([
            {"type": "vector", "shape": "pill", "layerId": "badge", "colourRole": "primary",
             "geometry": {"x": 100, "y": 100, "width": 300, "height": 60}},
            {"type": "text", "layerId": "label", "alignment": "center", "effects": {"shadow": {}},
             "geometry": {"x": 100, "y": 110, "width": 300, "height": 40}},
        ])
    renders = _renders(tmp_path)
    reviewed = run_ad_output_qa(candidate, renders)
    assert reviewed["status"] == "needs_review"
    assert reviewed_unknowns_match(reviewed, copy.deepcopy(reviewed))
    changed = copy.deepcopy(candidate)
    changed["template"]["feedLayout"]["layers"][-1]["geometry"]["y"] += 1
    assert not reviewed_unknowns_match(run_ad_output_qa(changed, renders), reviewed)
    with Image.open(renders["feed"]) as image:
        altered = image.copy()
    altered.putpixel((10, 10), (70, 80, 90, 255))
    altered.save(renders["feed"])
    assert not reviewed_unknowns_match(run_ad_output_qa(candidate, renders), reviewed)
