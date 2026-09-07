from __future__ import annotations
import copy
from gateway.ad_template_text_evidence import build_text_alignment_evidence

def _candidate(placement="feed"):
    return {"template": {"textInputs": [{"key": "headline", "placeholder": "OPEN HOUSE"}], f"{placement}Layout": {"layers": [{"type": "text", "layerId": f"{placement}-headline", "inputKey": "headline", "geometry": {"x": 80, "y": 450, "width": 850, "height": 180}}]}}}

def _word(text, x, y, confidence=95, width=70, height=28):
    return {"text": text, "x": x, "y": y, "width": width, "height": height, "confidence": confidence}

def _maps(source_words, candidate_words, placement="feed"):
    height = 1350 if placement == "feed" else 1920
    return ({"canvas": {"width": 1080, "height": height}, "ocr": source_words}, {"canvas": {"width": 1080, "height": height}, "ocr": candidate_words})

def test_alignment_reports_offsets_and_size_ratios_without_mutation():
    candidate = _candidate()
    source, rendered = _maps([_word("OPEN", 120, 520), _word("HOUSE", 210, 520)], [_word("open", 130, 560), _word("house", 220, 560, width=77)])
    before = copy.deepcopy(candidate)
    evidence = build_text_alignment_evidence(candidate, source, rendered, "feed")
    assert candidate == before
    assert evidence["sourcePlacement"] == "feed"
    assert evidence["entries"][0]["layerId"] == "feed-headline"
    assert evidence["entries"][0]["matchedTokenCount"] == 2
    assert evidence["entries"][0]["coverage"] == 1.0
    assert evidence["entries"][0]["offset"] == {"dx": 10, "dy": 40}
    assert evidence["entries"][0]["sizeRatios"]["width"] > 1

def test_duplicate_source_tokens_are_skipped_instead_of_guessed():
    source, rendered = _maps([_word("OPEN", 100, 500), _word("OPEN", 200, 500)], [_word("OPEN", 120, 520)])
    assert build_text_alignment_evidence(_candidate(), source, rendered, "feed")["entries"] == []

def test_low_confidence_and_neutral_copy_do_not_create_evidence():
    source, rendered = _maps([_word("OPEN", 100, 500), _word("HOUSE", 200, 500)], [_word("OPEN", 120, 520, confidence=79), _word("NEUTRAL", 200, 520)])
    assert build_text_alignment_evidence(_candidate(), source, rendered, "feed")["entries"] == []

def test_candidate_tokens_outside_layer_box_are_not_matched():
    source, rendered = _maps([_word("OPEN", 100, 500), _word("HOUSE", 200, 500)], [_word("OPEN", 120, 520), _word("HOUSE", 200, 800)])
    assert build_text_alignment_evidence(_candidate(), source, rendered, "feed")["entries"] == []

def test_one_unique_long_word_can_supply_adequate_evidence():
    candidate = _candidate()
    candidate["template"]["textInputs"][0]["placeholder"] = "OPENHOUSE"
    source, rendered = _maps([_word("OPENHOUSE", 100, 500)], [_word("open-house", 120, 520)])
    assert build_text_alignment_evidence(candidate, source, rendered, "feed")["entries"][0]["matchedTokenCount"] == 1

def test_story_source_placement_uses_story_canvas():
    candidate = _candidate("story")
    source, rendered = _maps([_word("OPEN", 100, 500), _word("HOUSE", 200, 500)], [_word("OPEN", 110, 540), _word("HOUSE", 210, 540)], "story")
    evidence = build_text_alignment_evidence(candidate, source, rendered, "story")
    assert evidence["sourcePlacement"] == "story"
    assert evidence["entries"][0]["placement"] == "story"

def test_repeated_candidate_placements_are_ambiguous_and_output_bounded():
    candidate = {"template": {"feedLayout": {"layers": [{"type": "text", "layerId": f"headline-{i}", "geometry": {"x": 80, "y": 450 + i * 20, "width": 850, "height": 100}} for i in range(20)]}}}
    source, rendered = _maps([_word("OPEN", 100, 500), _word("HOUSE", 200, 500)], [_word("OPEN", 120, 470), _word("HOUSE", 220, 470)])
    evidence = build_text_alignment_evidence(candidate, source, rendered, "feed")
    assert len(evidence["entries"]) <= 16
    assert evidence == build_text_alignment_evidence(candidate, source, rendered, "feed")

def test_resolved_copy_controls_meaningful_coverage():
    candidate = _candidate()
    candidate["template"]["textInputs"][0]["placeholder"] = "OPEN HOUSE EXTRA"
    source, rendered = _maps(
        [_word("OPEN", 100, 500), _word("HOUSE", 200, 500)],
        [_word("OPEN", 120, 520), _word("HOUSE", 220, 520)],
    )
    assert build_text_alignment_evidence(candidate, source, rendered, "feed")["entries"] == []


def test_nonfinite_coordinates_are_rejected():
    source, rendered = _maps(
        [_word("OPEN", 100, 500), _word("HOUSE", 200, 500)],
        [_word("OPEN", float("nan"), 520), _word("HOUSE", 220, 520)],
    )
